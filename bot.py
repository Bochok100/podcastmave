"""
Podcast Bot v2
- Студийный звук через ElevenLabs API (официально, без блокировок)
- Загрузка в mave.digital с фотоотчётом
"""

import os
import asyncio
import tempfile
import subprocess
import shutil
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
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY")
ALLOWED_USER_ID    = int(os.getenv("ALLOWED_USER_ID", "0"))

EDIT_TITLE, EDIT_DESC = range(2)

STYLE_PROMPT = """
Ты — редактор подкаста Василия. Темы бывают разные: крипта, ИИ, технологии, семья, жизнь, бытовые вопросы — всё что угодно.

ТВОЯ ЗАДАЧА: по расшифровке придумать заголовок и описание.

=== СТИЛЬ ЗАГОЛОВКА ===
Учись у этих примеров (это стиль, не копируй их):
- Как на самом деле работают сделки
- Что такое Long и Short на самом деле
- Будущие тренды в крипте: куда смотреть до хайпа
- Агенты ИИ: почему без них уже нельзя
- Майнинг в России: быть или не быть?
- Кто такие «киты» — мифические богачи или нечто иное?

Закономерности стиля:
- Конкретно, без воды, понятно новичку
- Иногда "на самом деле" — снимает мифы
- Иногда риторический вопрос
- НЕ пиши "топ", "секреты", "шокирующий"
- Тема диктует заголовок — не тяни всё к крипте если тема другая

=== СТИЛЬ ОПИСАНИЯ ===
2 предложения: суть выпуска + зачем слушать. Разговорный тон.

=== ФОРМАТ ОТВЕТА (СТРОГО!) ===
Только две строки, без лишних слов:

ЗАГОЛОВОК: [заголовок]
ОПИСАНИЕ: [описание]

ТРАНСКРИПЦИЯ:
"""

pending = {}

# ──────────────────────────────────────────────
# 1. Скачать голосовое
# ──────────────────────────────────────────────
async def download_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> Path:
    voice = update.message.voice or update.message.audio
    file = await context.bot.get_file(voice.file_id)
    tmp = tempfile.NamedTemporaryFile(suffix=".ogg", delete=False)
    await file.download_to_drive(tmp.name)
    return Path(tmp.name)

# ──────────────────────────────────────────────
# 2. Подготовка аудио (Идеальный формат для ElevenLabs)
# ──────────────────────────────────────────────
def convert_to_mp3(input_path: Path) -> Path:
    mp3_path = input_path.with_suffix(".mp3")
    result = subprocess.run(
        ["ffmpeg", "-y", "-i", str(input_path),
         "-codec:a", "libmp3lame", "-b:a", "128k", "-ar", "44100", "-ac", "1",
         str(mp3_path)],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg: {result.stderr[-300:]}")
    return mp3_path

# ──────────────────────────────────────────────
# 3. ElevenLabs Студийный звук
# ──────────────────────────────────────────────
async def enhance_audio(mp3_path: Path) -> Path:
    if not ELEVENLABS_API_KEY:
        raise RuntimeError("Ключ ELEVENLABS_API_KEY не найден в настройках Render!")

    studio_path = mp3_path.with_stem(mp3_path.stem + "_studio")

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

        if resp.status_code != 200:
            raise RuntimeError(f"Отказ ElevenLabs (Код {resp.status_code}): {resp.text}")

        studio_path.write_bytes(resp.content)
        return studio_path

# ──────────────────────────────────────────────
# 4. Транскрипция (Whisper)
# ──────────────────────────────────────────────
def transcribe(mp3_path: Path) -> str:
    client = openai.OpenAI(api_key=OPENAI_KEY)
    safe_path = mp3_path.parent / (mp3_path.stem + "_w.mp3")
    shutil.copy2(mp3_path, safe_path)
    with open(safe_path, "rb") as f:
        return client.audio.transcriptions.create(
            model="whisper-1", file=f, language="ru"
        ).text

# ──────────────────────────────────────────────
# 5. Генерация заголовка и описания (GPT)
# ──────────────────────────────────────────────
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
        clean = line.strip().replace("**", "")
        if clean.upper().startswith("ЗАГОЛОВОК:"):
            title = clean[10:].strip()
        elif clean.upper().startswith("ОПИСАНИЕ:"):
            description = clean[9:].strip()
    return title, description

# ──────────────────────────────────────────────
# 6. Загрузка в mave.digital
# ──────────────────────────────────────────────
async def upload_to_mave(mp3_path: Path, title: str, description: str) -> bool:
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        )
        context.set_default_timeout(60000)
        page = await context.new_page()
        try:
            await page.goto("https://app.mave.digital/login")
            await page.fill('input[type="email"]', MAVE_EMAIL)
            await page.fill('input[type="password"]', MAVE_PASSWORD)
            await page.click('button[type="submit"]')
            await page.wait_for_url("**/dashboard**")
            await page.wait_for_load_state("networkidle")
            await asyncio.sleep(3)

            await page.evaluate("""() => {
                document.querySelectorAll('[id^="q-portal--dialog"]').forEach(e => e.remove());
                document.querySelectorAll('.q-overlay, .q-dialog__backdrop').forEach(e => e.remove());
                document.body.classList.remove('q-body--prevent-scroll', 'q-body--force-scrollbar-x');
            }""")
            await asyncio.sleep(1)

            await page.locator('text=Добавить выпуск').first.click()
            await asyncio.sleep(3)
            await page.wait_for_load_state("networkidle")

            await page.wait_for_selector('input[type="file"]', timeout=15000)
            await page.set_input_files('input[type="file"]', str(mp3_path))
            await asyncio.sleep(2)

            for sel in ['button:has-text("Загрузить файл")', 'button:has-text("Загрузить")', 'button[type="submit"]', '.upload-btn', 'button.q-btn:has-text("Загруз")']:
                try:
                    btn = page.locator(sel).first
                    if await btn.count() > 0:
                        await btn.click()
                        break
                except Exception:
                    continue

            await asyncio.sleep(3)

            for i in range(36):
                await asyncio.sleep(5)
                html = await page.content()
                if any(x in html for x in ["Название выпуска", "episode-title", "audio-player", "waveform", "Опубликовать", "Сохранить выпуск", "Описание выпуска", "upload-progress-done"]):
                    break

            for sel in ['input[placeholder*="Название выпуска"]', 'input[placeholder*="Название"]', 'input[placeholder*="название"]', 'input[name="title"]', 'input[type="text"]']:
                try:
                    el = page.locator(sel).first
                    if await el.count() > 0:
                        await el.clear()
                        await el.fill(title)
                        break
                except Exception:
                    continue

            for sel in ['.ProseMirror', 'div[contenteditable="true"]', 'textarea[placeholder*="Описание"]', 'textarea[placeholder*="описание"]', 'textarea[name="description"]']:
                try:
                    el = page.locator(sel).first
                    if await el.count() > 0:
                        await el.click()
                        await el.fill(description)
                        break
                except Exception:
                    continue

            published = False
            for btn_text in ["Опубликовать", "Сохранить выпуск", "Сохранить", "Publish"]:
                try:
                    btn = page.locator(f'button:has-text("{btn_text}")').first
                    if await btn.count() > 0:
                        await btn.click()
                        published = True
                        break
                except Exception:
                    continue

            if not published:
                raise RuntimeError("Не найдена кнопка публикации — смотри скриншот")

            await asyncio.sleep(5)
            await page.screenshot(path="/tmp/mave_done.png")
            return True

        except Exception as e:
            await page.screenshot(path="/tmp/mave_error.png")
            raise RuntimeError(f"{e}")
        finally:
            await browser.close()

# ──────────────────────────────────────────────
# 7. Обработчик голосового
# ──────────────────────────────────────────────
async def handle_voice(update: Update, tg_context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if ALLOWED_USER_ID and user_id != ALLOWED_USER_ID:
        return

    msg = await update.message.reply_text("⏳ Обрабатываю...")
    try:
        ogg = await download_voice(update, tg_context)

        await msg.edit_text("🔄 Конвертирую аудио...")
        mp3 = convert_to_mp3(ogg)

        await msg.edit_text("🎙️ ElevenLabs: Студийная обработка...")
        studio_mp3 = await enhance_audio(mp3)

        await msg.edit_text("📝 Транскрибирую (Whisper)...")
        transcript = transcribe(studio_mp3)

        await msg.edit_text("✍️ Генерирую заголовок и описание (GPT-4o)...")
        title, description = generate_metadata(transcript)

        pending[user_id] = {"mp3": studio_mp3, "title": title, "description": description}
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

# ──────────────────────────────────────────────
# Кнопки
# ──────────────────────────────────────────────
async def button_publish(update: Update, tg_context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    data = pending.get(user_id)

    if not data:
        await query.edit_message_text("❌ Данные устарели. Запиши заново.")
        return

    await query.edit_message_text("⏳ Загружаю в mave.digital (~1-2 минуты)...")
    try:
        for f in ["/tmp/mave_error.png", "/tmp/mave_done.png"]:
            if os.path.exists(f): os.remove(f)

        await upload_to_mave(data["mp3"], data["title"], data["description"])

        caption = f"✅ *Опубликовано!*\n\n_{data['title']}_\n\nСкоро появится на Spotify и Apple Podcasts."
        if os.path.exists("/tmp/mave_done.png"):
            await query.message.reply_photo(photo=open("/tmp/mave_done.png", "rb"), caption=caption, parse_mode=ParseMode.MARKDOWN)
            await query.message.delete()
        else:
            await query.edit_message_text(caption, parse_mode=ParseMode.MARKDOWN)

        pending.pop(user_id, None)

    except Exception as e:
        screenshot = "/tmp/mave_error.png"
        if os.path.exists(screenshot):
            await query.message.reply_photo(photo=open(screenshot, "rb"), caption=f"❌ Ошибка Mave: {e}\n\nСкриншот прикреплён.")
            await query.message.delete()
        else:
            await query.edit_message_text(f"❌ Ошибка загрузки: {e}")

async def button_edit(update: Update, tg_context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = pending.get(query.from_user.id)
    await query.edit_message_text(f"✏️ Текущий заголовок:\n*{data['title']}*\n\nНапиши новый (или /skip):", parse_mode=ParseMode.MARKDOWN)
    return EDIT_TITLE

async def edit_title(update: Update, tg_context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if update.message.text != "/skip":
        pending[user_id]["title"] = update.message.text
    data = pending[user_id]
    await update.message.reply_text(f"📝 Текущее описание:\n_{data['description']}_\n\nНапиши новое (или /skip):", parse_mode=ParseMode.MARKDOWN)
    return EDIT_DESC

async def edit_desc(update: Update, tg_context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if update.message.text != "/skip":
        pending[user_id]["description"] = update.message.text
    data = pending[user_id]
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Опубликовать", callback_data="publish"),
        InlineKeyboardButton("❌ Отмена", callback_data="cancel")
    ]])
    await update.message.reply_text(f"🎙 *Обновлено:*\n\n*Заголовок:* {data['title']}\n*Описание:* {data['description']}\n\nПубликуем?", parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)
    return ConversationHandler.END

async def button_cancel(update: Update, tg_context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    pending.pop(query.from_user.id, None)
    await query.edit_message_text("❌ Отменено.")
    return ConversationHandler.END

# ──────────────────────────────────────────────
# Запуск
# ──────────────────────────────────────────────
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

    print("Бот v2 запущен (Только ElevenLabs API + Mave)...")
    app.run_polling()

if __name__ == "__main__":
    main()
