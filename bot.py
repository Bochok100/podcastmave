"""
Podcast Bot v2 — автоматизация подкаста
- Студийная обработка аудио через ElevenLabs (с выводом ошибок в Telegram)
- Загрузка в mave.digital с ФОТООТЧЕТОМ после публикации
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
TELEGRAM_TOKEN     = os.getenv("TELEGRAM_TOKEN")
OPENAI_KEY         = os.getenv("OPENAI_API_KEY")
MAVE_EMAIL         = os.getenv("MAVE_EMAIL")
MAVE_PASSWORD      = os.getenv("MAVE_PASSWORD")
MAVE_PODCAST_ID    = os.getenv("MAVE_PODCAST_ID")
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY")
ALLOWED_USER_ID    = int(os.getenv("ALLOWED_USER_ID", "0"))

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

def convert_to_mp3(input_path: Path) -> Path:
    mp3_path = input_path.with_suffix(".mp3")
    result = subprocess.run(
        ["ffmpeg", "-y", "-i", str(input_path),
         "-ar", "44100", "-ac", "1", "-q:a", "4", str(mp3_path)],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg ошибка: {result.stderr[-300:]}")
    if not mp3_path.exists() or mp3_path.stat().st_size < 1000:
        raise RuntimeError("ffmpeg не создал файл MP3")
    return mp3_path

async def enhance_audio(mp3_path: Path) -> Path:
    """ Нейросетевое улучшение звука. Выдает жесткую ошибку, если что-то не так. """
    if not ELEVENLABS_API_KEY:
        raise RuntimeError("Ключ ELEVENLABS_API_KEY не найден в настройках Render!")

    enhanced_path = mp3_path.with_stem(mp3_path.stem + "_studio")

    async with httpx.AsyncClient(timeout=300) as client:
        with open(mp3_path, "rb") as f:
            resp = await client.post(
                "https://api.elevenlabs.io/v1/audio-isolation",
                headers={
                    "xi-api-key": ELEVENLABS_API_KEY,
                    "Accept": "audio/mpeg"
                },
                files={"audio": (mp3_path.name, f, "audio/mpeg")},
            )

        # Если ElevenLabs недоволен, прерываем всё и выводим ошибку в Telegram
        if resp.status_code != 200:
            error_msg = f"Отказ ElevenLabs (Код {resp.status_code}): {resp.text}"
            raise RuntimeError(error_msg)

        enhanced_path.write_bytes(resp.content)
        return enhanced_path

def transcribe(mp3_path: Path) -> str:
    client = openai.OpenAI(api_key=OPENAI_KEY)
    safe_path = mp3_path.parent / (mp3_path.stem + "_ready.mp3")
    if safe_path != mp3_path:
        import shutil
        shutil.copy2(mp3_path, safe_path)
    with open(safe_path, "rb") as f:
        return client.audio.transcriptions.create(
            model="whisper-1",
            file=f,
            language="ru"
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
        context = await browser.new_context(viewport={"width": 1280, "height": 900})
        context.set_default_timeout(60000)
        page = await context.new_page()
        try:
            # 1. Логин
            await page.goto("https://app.mave.digital/login")
            await page.fill('input[type="email"]', MAVE_EMAIL)
            await page.fill('input[type="password"]', MAVE_PASSWORD)
            await page.click('button[type="submit"]')
            await page.wait_for_url("**/dashboard**")
            await page.wait_for_load_state("networkidle")
            await asyncio.sleep(3)

            # 2. Очистка всплывающих окон
            await page.evaluate("""() => {
                document.querySelectorAll('[id^="q-portal--dialog"]').forEach(e => e.remove());
                document.querySelectorAll('.q-overlay, .q-dialog__backdrop').forEach(e => e.remove());
                document.body.classList.remove('q-body--prevent-scroll', 'q-body--force-scrollbar-x');
            }""")
            await asyncio.sleep(1)

            # 3. Клик "Добавить выпуск"
            await page.locator('text=Добавить выпуск').first.click()
            await asyncio.sleep(3)
            await page.wait_for_load_state("networkidle")

            # 4. Поиск формы и загрузка
            await page.wait_for_selector('input[type="file"]', timeout=15000)
            await page.set_input_files('input[type="file"]', str(mp3_path))
            await asyncio.sleep(5)

            # 5. Ожидание загрузки аудио
            for i in range(24):
                await asyncio.sleep(5)
                html = await page.content()
                if any(x in html for x in ["audio-player", "upload-progress-done", "waveform", "Опубликовать", "Сохранить", "Название выпуска", "episode-title"]):
                    break

            # 6. Вставка заголовка
            for sel in ['input[placeholder*="Название"]', 'input[placeholder*="название"]', 'input[name="title"]', 'input[type="text"]']:
                try:
                    el = page.locator(sel).first
                    if await el.count() > 0:
                        await el.fill(title)
                        break
                except Exception:
                    continue

            # 7. Вставка описания
            for sel in ['.ProseMirror', 'div[contenteditable="true"]', 'textarea[placeholder*="писание"]', 'textarea[name="description"]']:
                try:
                    el = page.locator(sel).first
                    if await el.count() > 0:
                        await el.fill(description)
                        break
                except Exception:
                    continue

            # 8. Публикация
            published = False
            for btn_text in ["Опубликовать", "Сохранить", "Publish", "Save"]:
                try:
                    btn = page.locator(f'button:has-text("{btn_text}")').first
                    if await btn.count() > 0:
                        await btn.click()
                        published = True
                        break
                except Exception:
                    continue

            # Даем сайту 5 секунд на реакцию и ДЕЛАЕМ ФИНАЛЬНЫЙ СКРИНШОТ
            await asyncio.sleep(5)
            await page.screenshot(path="/tmp/mave_done.png")
            
            if not published:
                raise RuntimeError("Не смог найти ни кнопку 'Опубликовать', ни 'Сохранить'.")

            return True

        except Exception as e:
            await page.screenshot(path="/tmp/mave_error.png")
            raise RuntimeError(f"{e}")
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

        await msg.edit_text("🎙️ Улучшаю звук (ElevenLabs Студия)...")
        studio_mp3 = await enhance_audio(mp3)

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
        
    await query.edit_message_text("⏳ Загружаю в mave.digital (это займет около минуты)...")
    try:
        # Удаляем старые скрины
        if os.path.exists("/tmp/mave_error.png"): os.remove("/tmp/mave_error.png")
        if os.path.exists("/tmp/mave_done.png"): os.remove("/tmp/mave_done.png")
            
        await upload_to_mave(data["mp3"], data["title"], data["description"])
        
        # Отправляем ФОТООТЧЕТ
        success_text = f"✅ *Действие завершено!*\n\n_{data['title']}_\n\nПосмотри на скриншот ниже, чтобы увидеть результат на сайте."
        if os.path.exists("/tmp/mave_done.png"):
            await query.message.reply_photo(photo=open("/tmp/mave_done.png", "rb"), caption=success_text, parse_mode=ParseMode.MARKDOWN)
            await query.delete() # удаляем старое сообщение "Загружаю..."
        else:
            await query.edit_message_text(success_text, parse_mode=ParseMode.MARKDOWN)
            
        pending.pop(user_id, None)
    except Exception as e:
        if os.path.exists("/tmp/mave_error.png"):
            await query.message.reply_photo(
                photo=open("/tmp/mave_error.png", "rb"), 
                caption=f"❌ Ошибка Mave! Скриншот браузера в момент сбоя прикреплен."
            )
            await query.delete()
        else:
            await query.edit_message_text(f"❌ Ошибка загрузки: {e}")

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
        per_message=False,
        per_chat=True,
    )

    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_voice))
    app.add_handler(conv)
    app.add_handler(CallbackQueryHandler(button_publish, pattern="^publish$"))
    app.add_handler(CallbackQueryHandler(button_cancel, pattern="^cancel$"))
    
    print("Бот v2 запущен (ElevenLabs Проверка + Фотоотчет Mave)...")
    app.run_polling()

if __name__ == "__main__":
    main()
