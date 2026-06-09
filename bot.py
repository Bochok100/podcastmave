"""
Podcast Bot v2 — автоматизация подкаста
- Студийная обработка звука через FFmpeg (без внешних API)
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
TELEGRAM_TOKEN  = os.getenv("TELEGRAM_TOKEN")
OPENAI_KEY      = os.getenv("OPENAI_API_KEY")
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
Описание должно состоять из 2-3 предложений. Раскрой суть подкаста, задай интригующий вопрос
или укажи, какую пользу получит слушатель.

=== ФОРМАТ ОТВЕТА (СТРОГО!) ===
Твой ответ должен содержать только две строки, без лишних приветствий, символов и кавычек:

ЗАГОЛОВОК: [Твой придуманный заголовок]
ОПИСАНИЕ: [Твое придуманное описание]

ТРАНСКРИПЦИЯ АУДИО ДЛЯ ОБРАБОТКИ:
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
# 2. Конвертация OGG → MP3
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
        raise RuntimeError(f"ffmpeg конвертация: {result.stderr[-300:]}")
    if not mp3_path.exists() or mp3_path.stat().st_size < 1000:
        raise RuntimeError("ffmpeg не создал MP3")
    return mp3_path


# ──────────────────────────────────────────────
# 3. Студийная обработка звука (только FFmpeg)
#
# Цепочка фильтров:
#   highpass=80Гц       — срезаем гул/рокот микрофона
#   lowpass=14000Гц     — убираем лишние высокочастотные шипы
#   afftdn              — нейросетевой шумодав FFmpeg (встроенный)
#   eq 300Гц -5dB       — убираем «картонный» призвук
#   eq 3500Гц +3dB      — добавляем ясность и присутствие голоса
#   acompressor         — компрессор: голос становится плотным и ровным
#   loudnorm -16 LUFS   — стандарт громкости подкастов (Spotify/Apple)
# ──────────────────────────────────────────────
def enhance_audio(mp3_path: Path) -> Path:
    studio_path = mp3_path.parent / (mp3_path.stem + "_studio.mp3")

    filters = ",".join([
        "highpass=f=80",
        "lowpass=f=14000",
        "afftdn=nf=-25",
        "equalizer=f=300:width_type=o:width=2:g=-5",
        "equalizer=f=3500:width_type=o:width=2:g=3",
        "acompressor=threshold=-18dB:ratio=3:attack=5:release=100:makeup=2",
        "loudnorm=I=-16:TP=-1.5:LRA=11",
    ])

    result = subprocess.run(
        ["ffmpeg", "-y", "-i", str(mp3_path),
         "-af", filters,
         "-codec:a", "libmp3lame", "-b:a", "128k", "-ar", "44100", "-ac", "1",
         str(studio_path)],
        capture_output=True, text=True
    )

    if result.returncode != 0 or not studio_path.exists():
        print(f"enhance_audio ffmpeg stderr: {result.stderr[-500:]}")
        print("Обработка не удалась — используем исходный MP3")
        return mp3_path

    size = studio_path.stat().st_size
    print(f"enhance_audio: готово, {size} байт → {studio_path.name}")
    return studio_path


# ──────────────────────────────────────────────
# 4. Транскрипция через Whisper
# ──────────────────────────────────────────────
def transcribe(mp3_path: Path) -> str:
    client = openai.OpenAI(api_key=OPENAI_KEY)
    # Гарантируем имя файла с расширением .mp3
    safe_path = mp3_path.parent / (mp3_path.stem + "_ready.mp3")
    if safe_path != mp3_path:
        shutil.copy2(mp3_path, safe_path)
    with open(safe_path, "rb") as f:
        return client.audio.transcriptions.create(
            model="whisper-1",
            file=f,
            language="ru"
        ).text


# ──────────────────────────────────────────────
# 5. Генерация заголовка и описания через GPT-4o
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
# 6. Загрузка в mave.digital через Playwright
# ──────────────────────────────────────────────
async def upload_to_mave(mp3_path: Path, title: str, description: str) -> bool:
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(viewport={"width": 1280, "height": 900})
        context.set_default_timeout(60000)
        page = await context.new_page()
        try:
            # Логин
            await page.goto("https://app.mave.digital/login")
            await page.fill('input[type="email"]', MAVE_EMAIL)
            await page.fill('input[type="password"]', MAVE_PASSWORD)
            await page.click('button[type="submit"]')
            await page.wait_for_url("**/dashboard**")
            await page.wait_for_load_state("networkidle")
            await asyncio.sleep(3)
            print("mave: залогинились")

            # Убиваем модальное окно через JS
            await page.evaluate("""() => {
                document.querySelectorAll('[id^="q-portal--dialog"]').forEach(e => e.remove());
                document.querySelectorAll('.q-overlay, .q-dialog__backdrop').forEach(e => e.remove());
                document.body.classList.remove('q-body--prevent-scroll', 'q-body--force-scrollbar-x');
            }""")
            await asyncio.sleep(1)
            await page.screenshot(path="/tmp/mave_01.png")

            # Клик "Добавить выпуск"
            await page.locator('text=Добавить выпуск').first.click()
            await asyncio.sleep(3)
            await page.wait_for_load_state("networkidle")
            await page.screenshot(path="/tmp/mave_02.png")
            print(f"mave: после клика URL={page.url}")

            # Загрузка файла
            await page.wait_for_selector('input[type="file"]', timeout=15000)
            await page.set_input_files('input[type="file"]', str(mp3_path))
            print(f"mave: файл отправлен {mp3_path.name}")
            await asyncio.sleep(5)

            # Ждём признаков что загрузка завершена
            print("mave: ждём завершения загрузки файла...")
            for i in range(24):
                await asyncio.sleep(5)
                html = await page.content()
                if any(x in html for x in [
                    "audio-player", "waveform", "upload-progress-done",
                    "Название выпуска", "episode-title", "Опубликовать", "Сохранить"
                ]):
                    print(f"mave: загрузка завершена (шаг {i+1})")
                    break
                print(f"mave: ждём... ({i+1}/24)")

            await page.screenshot(path="/tmp/mave_03.png")
            html = await page.content()
            print(f"mave: HTML фрагмент: {html[1000:2500]}")

            # Заголовок
            for sel in [
                'input[placeholder*="Название"]',
                'input[placeholder*="название"]',
                'input[name="title"]',
                'input[type="text"]',
            ]:
                try:
                    el = page.locator(sel).first
                    if await el.count() > 0:
                        await el.clear()
                        await el.fill(title)
                        print(f"mave: заголовок → '{sel}'")
                        break
                except Exception:
                    continue

            # Описание
            for sel in [
                '.ProseMirror',
                'div[contenteditable="true"]',
                'textarea[placeholder*="писание"]',
                'textarea[name="description"]',
            ]:
                try:
                    el = page.locator(sel).first
                    if await el.count() > 0:
                        await el.click()
                        await el.fill(description)
                        print(f"mave: описание → '{sel}'")
                        break
                except Exception:
                    continue

            await page.screenshot(path="/tmp/mave_04.png")

            # Публикуем
            published = False
            for btn_text in ["Опубликовать", "Сохранить", "Publish", "Save"]:
                try:
                    btn = page.locator(f'button:has-text("{btn_text}")').first
                    if await btn.count() > 0:
                        await btn.click()
                        published = True
                        print(f"mave: кнопка '{btn_text}' нажата")
                        break
                except Exception:
                    continue

            if not published:
                raise RuntimeError("Не найдена кнопка публикации")

            await asyncio.sleep(5)
            await page.screenshot(path="/tmp/mave_done.png")
            print(f"mave: готово, URL={page.url}")
            return True

        except Exception as e:
            await page.screenshot(path="/tmp/mave_error.png")
            print(f"Ошибка mave: {e}")
            raise RuntimeError(f"{e}")
        finally:
            await browser.close()


# ──────────────────────────────────────────────
# 7. Обработчик голосового сообщения
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

        await msg.edit_text("🎙️ Обрабатываю звук (шумодав + компрессор + нормализация)...")
        studio_mp3 = enhance_audio(mp3)  # синхронная функция

        await msg.edit_text("📝 Транскрибирую (Whisper)...")
        transcript = transcribe(studio_mp3)

        await msg.edit_text("✍️ Генерирую заголовок и описание (GPT-4o)...")
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
        preview = (
            f"🎙 *Готов к публикации*\n\n"
            f"*Заголовок:*\n{title}\n\n"
            f"*Описание:*\n{description}\n\n"
            f"Одобряешь?"
        )
        await update.message.reply_document(
            document=open(studio_mp3, "rb"),
            filename=f"{title[:40]}.mp3"
        )
        await update.message.reply_text(
            preview, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard
        )
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

    await query.edit_message_text("⏳ Загружаю в mave.digital (~1 минута)...")
    try:
        for f in ["/tmp/mave_error.png", "/tmp/mave_done.png"]:
            if os.path.exists(f): os.remove(f)

        await upload_to_mave(data["mp3"], data["title"], data["description"])

        caption = f"✅ *Опубликовано!*\n\n_{data['title']}_\n\nСкоро появится на Spotify и Apple Podcasts."
        if os.path.exists("/tmp/mave_done.png"):
            await query.message.reply_photo(
                photo=open("/tmp/mave_done.png", "rb"),
                caption=caption, parse_mode=ParseMode.MARKDOWN
            )
            await query.delete()
        else:
            await query.edit_message_text(caption, parse_mode=ParseMode.MARKDOWN)

        pending.pop(user_id, None)

    except Exception as e:
        if os.path.exists("/tmp/mave_error.png"):
            await query.message.reply_photo(
                photo=open("/tmp/mave_error.png", "rb"),
                caption=f"❌ Ошибка: {e}\n\nСкриншот момента сбоя прикреплён."
            )
            await query.delete()
        else:
            await query.edit_message_text(f"❌ Ошибка загрузки: {e}")


async def button_edit(update: Update, tg_context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    data = pending.get(user_id)
    await query.edit_message_text(
        f"✏️ Текущий заголовок:\n*{data['title']}*\n\nНапиши новый (или /skip):",
        parse_mode=ParseMode.MARKDOWN
    )
    return EDIT_TITLE


async def edit_title(update: Update, tg_context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if update.message.text != "/skip":
        pending[user_id]["title"] = update.message.text
    data = pending[user_id]
    await update.message.reply_text(
        f"📝 Текущее описание:\n_{data['description']}_\n\nНапиши новое (или /skip):",
        parse_mode=ParseMode.MARKDOWN
    )
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
    await update.message.reply_text(
        f"🎙 *Обновлено:*\n\n*Заголовок:* {data['title']}\n*Описание:* {data['description']}\n\nПубликуем?",
        parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard
    )
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

    print("Бот v2 запущен (FFmpeg студийная обработка + фотоотчёт)...")
    app.run_polling()


if __name__ == "__main__":
    main()
