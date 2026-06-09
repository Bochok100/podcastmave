"""
Podcast Bot v2 — автоматизация подкаста с согласованием и загрузкой в mave
Отправь голосовое → одобри → бот сам заливает в mave.digital
"""

import os
import asyncio
import tempfile
import subprocess
from pathlib import Path

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, MessageHandler, CallbackQueryHandler,
    ConversationHandler, MessageHandler as MH,
    filters, ContextTypes,
)
from telegram.constants import ParseMode

import openai
import httpx
from playwright.async_api import async_playwright

# ──────────────────────────────────────────────
# Настройки
# ──────────────────────────────────────────────
TELEGRAM_TOKEN  = os.getenv("TELEGRAM_TOKEN")
OPENAI_KEY      = os.getenv("OPENAI_API_KEY")
AUPHONIC_USER   = os.getenv("AUPHONIC_USER")
AUPHONIC_PASS   = os.getenv("AUPHONIC_PASS")
MAVE_EMAIL      = os.getenv("MAVE_EMAIL")
MAVE_PASSWORD   = os.getenv("MAVE_PASSWORD")
MAVE_PODCAST_ID = os.getenv("MAVE_PODCAST_ID")
ALLOWED_USER_ID = int(os.getenv("ALLOWED_USER_ID", "0"))

EDIT_TITLE, EDIT_DESC = range(2)

STYLE_PROMPT = """
Ты — профессиональный редактор и автор подкастов про криптовалюту, инвестиции и технологии. 
Я отправляю тебе черновую текстовую расшифровку аудиосообщения. 

ТВОЯ ЗАДАЧА:
1. Внимательно изучить смысл текста.
2. Придумать цепляющий Заголовок.
3. Написать краткое Описание подкаста.

=== ПРАВИЛА ДЛЯ ЗАГОЛОВКА ===
Он должен быть интригующим, коротким и понятным новичкам. Избегай дешевого кликбейта.
Ориентируйся на этот стиль (это ТОЛЬКО примеры стиля, не копируй их!):
- Как на самом деле работают сделки
- RWA: почему реальные активы — самый спокойный рост в крипте
- Что такое Long и Short на самом деле
- Кто такие «киты» в криптовалюте — мифические богачи или нечто иное?
- Будущие тренды в крипте: куда смотреть до хайпа
- Стейкинг и фарминг: объясняю на пальцах

=== ПРАВИЛА ДЛЯ ОПИСАНИЯ ===
Описание должно состоять из 2-3 предложений. Раскрой суть подкаста, задай интригующий вопрос или укажи, какую пользу получит слушатель. 

=== ФОРМАТ ОТВЕТА (СТРОГО!) ===
Твой ответ должен содержать только две строки, без лишних приветствий, символов и кавычек. Строго в таком виде:

ЗАГОЛОВОК: [Твой придуманный заголовок]
ОПИСАНИЕ: [Твое придуманное описание]

ТРАНСКРИПЦИЯ АУДИО ДЛЯ ОБРАБОТКИ:
"""

pending = {}

async def download_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> Path:
    voice = update.message.voice or update.message.audio
    file = await context.bot.get_file(voice.file_id)
    tmp = tempfile.NamedTemporaryFile(suffix=".ogg", delete=False)
    await file.download_to_drive(tmp.name)
    return Path(tmp.name)

def convert_to_mp3(ogg_path: Path) -> Path:
    mp3_path = ogg_path.with_suffix(".mp3")
    subprocess.run(
        ["ffmpeg", "-y", "-i", str(ogg_path), "-q:a", "2", str(mp3_path)],
        check=True, capture_output=True
    )
    return mp3_path

async def enhance_audio(mp3_path: Path) -> Path:
    enhanced_path = mp3_path.with_stem(mp3_path.stem + "_studio")
    async with httpx.AsyncClient(timeout=300) as client:
        resp = await client.post(
            "https://auphonic.com/api/simple/productions.json",
            auth=(AUPHONIC_USER, AUPHONIC_PASS),
            data={"action": "start", "output_basename": enhanced_path.stem},
            files={"input_file": open(mp3_path, "rb")},
        )
        resp.raise_for_status()
        uuid = resp.json()["data"]["uuid"]
        for _ in range(60):
            await asyncio.sleep(5)
            s = await client.get(
                f"https://auphonic.com/api/production/{uuid}.json",
                auth=(AUPHONIC_USER, AUPHONIC_PASS),
            )
            status = s.json()["data"]["status_string"]
            if status == "Done":
                url = s.json()["data"]["output_files"][0]["download_url"]
                r = await client.get(url, auth=(AUPHONIC_USER, AUPHONIC_PASS))
                enhanced_path.write_bytes(r.content)
                return enhanced_path
            if status in ("Error", "Failed"):
                raise RuntimeError(f"Auphonic: {status}")
    return mp3_path

def transcribe(mp3_path: Path) -> str:
    client = openai.OpenAI(api_key=OPENAI_KEY)
    with open(mp3_path, "rb") as f:
        return client.audio.transcriptions.create(
            model="whisper-1", file=f, language="ru"
        ).text

def generate_metadata(transcript: str) -> tuple[str, str]:
    client = openai.OpenAI(api_key=OPENAI_KEY)
    response = client.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "user", "content": STYLE_PROMPT + transcript}],
        max_tokens=512,
        temperature=0.7
    )
    text = response.choices[0].message.content
    title = description = ""
    
    # Защита от маркдауна и парсинг строк
    for line in text.splitlines():
        clean_line = line.strip().replace("**", "")
        if clean_line.upper().startswith("ЗАГОЛОВОК:"):
            title = clean_line[10:].strip()
        elif clean_line.upper().startswith("ОПИСАНИЕ:"):
            description = clean_line[9:].strip()
            
    return title, description

async def upload_to_mave(mp3_path: Path, title: str, description: str) -> bool:
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()
        context.set_default_timeout(60000)
        page = await context.new_page()
        try:
            # 1. Логинимся
            await page.goto("https://app.mave.digital/login")
            await page.fill('input[type="email"]', MAVE_EMAIL)
            await page.fill('input[type="password"]', MAVE_PASSWORD)
            await page.click('button[type="submit"]')

            # 2. Ждём dashboard (новый интерфейс mave)
            await page.wait_for_url("**/dashboard**")
            await page.wait_for_load_state("networkidle")

            # 3. Кликаем "Добавить выпуск" в левом меню
            await page.locator('text=Добавить выпуск').first.click()

            # 4. Ждём всплывающее окно с полем загрузки файла
            await page.wait_for_selector('input[type="file"]')
            await page.set_input_files('input[type="file"]', str(mp3_path))

            # 5. Пауза + ждём завершения загрузки файла (до 2 минут)
            await asyncio.sleep(5)
            await page.wait_for_selector(
                ".upload-progress-done, .audio-player, "
                "button:has-text('Опубликовать'):not([disabled]), "
                "button:has-text('Сохранить'):not([disabled])",
                timeout=120000
            )

            # 6. Заголовок
            title_input = page.locator(
                'input[placeholder*="название"], input[placeholder*="Название"], input[name="title"]'
            ).first
            await title_input.fill(title)

            # 7. Описание — mave использует редактор ProseMirror
            desc_input = page.locator(
                '.ProseMirror, textarea[name="description"], textarea[placeholder*="описание"]'
            ).first
            await desc_input.fill(description)

            # 8. Публикуем
            publish_btn = page.locator(
                'button:has-text("Опубликовать"), button:has-text("Сохранить")'
            ).first
            await publish_btn.click()

            # 9. Ждём сохранения
            await asyncio.sleep(5)
            return True

        except Exception as e:
            print(f"Ошибка mave: {e}")
            raise RuntimeError(f"Ошибка загрузки в mave: {e}")
        finally:
            await browser.close()

async def handle_voice(update: Update, tg_context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if ALLOWED_USER_ID and user_id != ALLOWED_USER_ID:
        return

    msg = await update.message.reply_text("⏳ Обрабатываю...")
    try:
        ogg = await download_voice(update, tg_context)
        await msg.edit_text("🔄 Конвертирую аудио...")
        mp3 = convert_to_mp3(ogg)

        await msg.edit_text("🎙️ Улучшаю звук...")
        studio_mp3 = await enhance_audio(mp3) if AUPHONIC_USER else mp3

        await msg.edit_text("📝 Транскрибирую (Whisper)...")
        transcript = transcribe(studio_mp3)

        await msg.edit_text("✍️ Генерирую заголовок и описание (ChatGPT)...")
        title, description = generate_metadata(transcript)

        pending[user_id] = {
            "mp3": studio_mp3,
            "title": title,
            "description": description,
        }
        await msg.delete()

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Опубликовать в mave", callback_data="publish"),
                InlineKeyboardButton("✏️ Изменить", callback_data="edit"),
            ],
            [InlineKeyboardButton("❌ Отмена", callback_data="cancel")],
        ])
        preview = f"🎙 *Готов к публикации*\n\n*Заголовок:*\n{title}\n\n*Описание:*\n{description}\n\nОдобряешь?"
        await update.message.reply_document(document=open(studio_mp3, "rb"), filename=f"{title[:40]}.mp3")
        await update.message.reply_text(preview, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)
    except Exception as e:
        await msg.edit_text(f"❌ Ошибка: {e}")

async def button_publish(update: Update, tg_context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    data = pending.get(user_id)
    if not data:
        await query.edit_message_text("❌ Данные устарели. Запиши заново.")
        return
    await query.edit_message_text("⏳ Загружаю в mave.digital...")
    try:
        await upload_to_mave(data["mp3"], data["title"], data["description"])
        await query.edit_message_text(f"✅ *Опубликовано!*\n\n_{data['title']}_\n\nСкоро появится на площадках.", parse_mode=ParseMode.MARKDOWN)
        pending.pop(user_id, None)
    except Exception as e:
        await query.edit_message_text(f"❌ Ошибка загрузки: {e}\n\nПопробуй залить вручную.")

async def button_edit(update: Update, tg_context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    data = pending.get(user_id)
    await query.edit_message_text(f"✏️ Текущий заголовок:\n*{data['title']}*\n\nНапиши новый заголовок (или /skip):", parse_mode=ParseMode.MARKDOWN)
    return EDIT_TITLE

async def edit_title(update: Update, tg_context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if update.message.text != "/skip":
        pending[user_id]["title"] = update.message.text
    data = pending[user_id]
    await update.message.reply_text(f"📝 Текущее описание:\n_{data['description']}_\n\nНапиши новое описание (или /skip):", parse_mode=ParseMode.MARKDOWN)
    return EDIT_DESC

async def edit_desc(update: Update, tg_context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if update.message.text != "/skip":
        pending[user_id]["description"] = update.message.text
    data = pending[user_id]
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Опубликовать", callback_data="publish"), InlineKeyboardButton("❌ Отмена", callback_data="cancel")]])
    await update.message.reply_text(f"🎙 *Обновлено:*\n\n*Заголовок:* {data['title']}\n*Описание:* {data['description']}\n\nПубликуем?", parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)
    return ConversationHandler.END

async def button_cancel(update: Update, tg_context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    pending.pop(user_id, None)
    await query.edit_message_text("❌ Отменено.")
    return ConversationHandler.END

def main():
    app = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .connect_timeout(60.0)
        .read_timeout(60.0)
        .write_timeout(60.0)
        .get_updates_connect_timeout(60.0)
        .get_updates_read_timeout(60.0)
        .pool_timeout(60.0)
        .build()
    )

    conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(button_edit, pattern="^edit$")],
        states={
            EDIT_TITLE: [MH(filters.TEXT & ~filters.COMMAND, edit_title)],
            EDIT_DESC:  [MH(filters.TEXT & ~filters.COMMAND, edit_desc)],
        },
        fallbacks=[CallbackQueryHandler(button_cancel, pattern="^cancel$")],
    )

    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_voice))
    app.add_handler(conv)
    app.add_handler(CallbackQueryHandler(button_publish, pattern="^publish$"))
    app.add_handler(CallbackQueryHandler(button_cancel, pattern="^cancel$"))
    
    print("Бот v2 запущен (только OpenAI)...")
    app.run_polling()

if __name__ == "__main__":
    main()
