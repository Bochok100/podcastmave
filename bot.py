"""
Podcast Bot v2
- ElevenLabs Audio Isolation + FFmpeg мастеринг (студийный звук)
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
MAVE_PODCAST_ID    = os.getenv("MAVE_PODCAST_ID")
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
        raise RuntimeError(f"ffmpeg: {result.stderr[-300:]}")
    if not mp3_path.exists() or mp3_path.stat().st_size < 1000:
        raise RuntimeError("ffmpeg не создал MP3")
    return mp3_path


# ──────────────────────────────────────────────
# 3. Улучшение звука
#    Шаг 1: ElevenLabs Audio Isolation — убирает ВСЁ кроме голоса
#    Шаг 2: FFmpeg мастеринг — делает голос радийным/студийным
# ──────────────────────────────────────────────
async def enhance_audio(mp3_path: Path) -> Path:
    """
    Шаг 1: Adobe Podcast Enhance Speech через браузер (Playwright)
            Заходим на сайт, загружаем файл, скачиваем результат.
    Шаг 2: FFmpeg loudnorm — стандарт громкости подкастов (-16 LUFS)
    """
    adobe_path = mp3_path.parent / (mp3_path.stem + "_adobe.mp3")
    studio_path = mp3_path.parent / (mp3_path.stem + "_studio.mp3")

    # ── ШАГ 1: Adobe Podcast через браузер ──
    adobe_success = False
    try:
        print("Adobe Podcast: запускаем браузер...")
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(viewport={"width": 1280, "height": 900})
            page = await context.new_page()
            try:
                # Открываем Adobe Podcast Enhance Speech
                await page.goto("https://podcast.adobe.com/enhance", timeout=30000)
                await page.wait_for_load_state("networkidle")
                await asyncio.sleep(2)
                print(f"Adobe Podcast: страница загружена, URL={page.url}")
                await page.screenshot(path="/tmp/adobe_01.png")

                # Ищем поле загрузки файла
                await page.wait_for_selector('input[type="file"]', timeout=15000)
                await page.set_input_files('input[type="file"]', str(mp3_path))
                print(f"Adobe Podcast: файл загружен {mp3_path.name}")
                await asyncio.sleep(3)
                await page.screenshot(path="/tmp/adobe_02.png")

                # Нажимаем кнопку Enhance / Улучшить
                for btn in ["Enhance speech", "Enhance", "Upload", "Start", "Process"]:
                    try:
                        el = page.locator(f'button:has-text("{btn}"), [role="button"]:has-text("{btn}")').first
                        if await el.count() > 0:
                            await el.click()
                            print(f"Adobe Podcast: кнопка '{btn}' нажата")
                            break
                    except Exception:
                        continue

                await asyncio.sleep(3)
                await page.screenshot(path="/tmp/adobe_03.png")

                # Ждём обработки (до 5 минут)
                print("Adobe Podcast: ждём обработки...")
                for i in range(60):
                    await asyncio.sleep(5)
                    html = await page.content()
                    # Ищем кнопку скачивания
                    if any(x in html for x in ["Download", "download", "Скачать", "enhanced"]):
                        print(f"Adobe Podcast: обработка завершена (шаг {i+1})")
                        await page.screenshot(path="/tmp/adobe_04_done.png")
                        break
                    print(f"Adobe Podcast: ждём... ({i+1}/60)")

                # Скачиваем результат
                download_btn = None
                for sel in [
                    'a[download]',
                    'button:has-text("Download")',
                    'a:has-text("Download")',
                    '[href*=".mp3"]',
                    '[data-testid*="download"]',
                ]:
                    try:
                        el = page.locator(sel).first
                        if await el.count() > 0:
                            download_btn = el
                            print(f"Adobe Podcast: кнопка скачивания найдена → {sel}")
                            break
                    except Exception:
                        continue

                if download_btn:
                    # Перехватываем скачивание
                    async with page.expect_download(timeout=60000) as dl_info:
                        await download_btn.click()
                    download = await dl_info.value
                    await download.save_as(str(adobe_path))
                    size = adobe_path.stat().st_size
                    print(f"Adobe Podcast: скачан файл {size} байт")
                    if size > 10000:
                        adobe_success = True
                    else:
                        print("Adobe Podcast: файл слишком маленький")
                else:
                    print("Adobe Podcast: кнопка скачивания не найдена")
                    await page.screenshot(path="/tmp/adobe_no_download.png")
                    html = await page.content()
                    print(f"Adobe HTML: {html[500:2000]}")

            except Exception as e:
                print(f"Adobe Podcast браузер: {e}")
                await page.screenshot(path="/tmp/adobe_error.png")
            finally:
                await browser.close()

    except Exception as e:
        print(f"Adobe Podcast: исключение — {e}")

    source = adobe_path if adobe_success else mp3_path
    if adobe_success:
        print(f"Adobe Podcast: успех, используем {adobe_path.name}")
    else:
        print("Adobe Podcast: не удалось, используем FFmpeg мастеринг")
        # Запасной вариант — FFmpeg с полной цепочкой фильтров
        audio_filters = ",".join([
            "afftdn=nf=-25:nt=w",
            "highpass=f=80",
            "lowpass=f=16000",
            "equalizer=f=300:width_type=o:width=2:g=-4",
            "equalizer=f=3500:width_type=o:width=2:g=4",
            "equalizer=f=10000:width_type=o:width=2:g=2",
            "acompressor=threshold=-20dB:ratio=4:attack=5:release=80:makeup=3",
            "loudnorm=I=-16:TP=-1.5:LRA=11",
        ])
        result = subprocess.run(
            ["ffmpeg", "-y", "-i", str(mp3_path),
             "-af", audio_filters,
             "-codec:a", "libmp3lame", "-b:a", "128k", "-ar", "44100", "-ac", "1",
             str(studio_path)],
            capture_output=True, text=True
        )
        if result.returncode == 0 and studio_path.exists():
            print(f"FFmpeg запасной: готово {studio_path.stat().st_size} байт")
            return studio_path
        return mp3_path

    # ── ШАГ 2: loudnorm поверх Adobe результата ──
    result = subprocess.run(
        ["ffmpeg", "-y", "-i", str(source),
         "-af", "loudnorm=I=-16:TP=-1.5:LRA=11",
         "-codec:a", "libmp3lame", "-b:a", "128k", "-ar", "44100", "-ac", "1",
         str(studio_path)],
        capture_output=True, text=True
    )
    if result.returncode == 0 and studio_path.exists():
        print(f"loudnorm: готово {studio_path.stat().st_size} байт")
        return studio_path

    return source


def transcribe(mp3_path: Path) -> str:
    client = openai.OpenAI(api_key=OPENAI_KEY)
    safe_path = mp3_path.parent / (mp3_path.stem + "_w.mp3")
    shutil.copy2(mp3_path, safe_path)
    with open(safe_path, "rb") as f:
        return client.audio.transcriptions.create(
            model="whisper-1", file=f, language="ru"
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
            # 1. Логин
            await page.goto("https://app.mave.digital/login")
            await page.fill('input[type="email"]', MAVE_EMAIL)
            await page.fill('input[type="password"]', MAVE_PASSWORD)
            await page.click('button[type="submit"]')
            await page.wait_for_url("**/dashboard**")
            await page.wait_for_load_state("networkidle")
            await asyncio.sleep(3)
            print("mave: залогинились")

            # 2. Убиваем welcome-модалку через JS
            await page.evaluate("""() => {
                document.querySelectorAll('[id^="q-portal--dialog"]').forEach(e => e.remove());
                document.querySelectorAll('.q-overlay, .q-dialog__backdrop').forEach(e => e.remove());
                document.body.classList.remove('q-body--prevent-scroll', 'q-body--force-scrollbar-x');
            }""")
            await asyncio.sleep(1)

            # 3. Открываем форму нового выпуска
            await page.locator('text=Добавить выпуск').first.click()
            await asyncio.sleep(3)
            await page.wait_for_load_state("networkidle")
            await page.screenshot(path="/tmp/mave_01_form.png")
            print(f"mave: форма открыта, URL={page.url}")

            # 4. Выбираем файл через скрытый input
            await page.wait_for_selector('input[type="file"]', timeout=15000)
            await page.set_input_files('input[type="file"]', str(mp3_path))
            print(f"mave: файл выбран: {mp3_path.name}")
            await asyncio.sleep(2)
            await page.screenshot(path="/tmp/mave_02_file_selected.png")

            # 5. Нажимаем кнопку "Загрузить файл" — это ключевой шаг!
            print("mave: ищем кнопку 'Загрузить файл'...")
            upload_btn_selectors = [
                'button:has-text("Загрузить файл")',
                'button:has-text("Загрузить")',
                'button[type="submit"]',
                '.upload-btn',
                'button.q-btn:has-text("Загруз")',
            ]
            for sel in upload_btn_selectors:
                try:
                    btn = page.locator(sel).first
                    if await btn.count() > 0:
                        await btn.click()
                        print(f"mave: кнопка загрузки нажата → '{sel}'")
                        break
                except Exception:
                    continue

            await asyncio.sleep(3)
            await page.screenshot(path="/tmp/mave_03_uploading.png")

            # 6. Ждём завершения загрузки файла на сервер (до 3 минут)
            print("mave: ждём загрузки файла на сервер...")
            for i in range(36):
                await asyncio.sleep(5)
                html = await page.content()
                if any(x in html for x in [
                    "Название выпуска", "episode-title", "audio-player",
                    "waveform", "Опубликовать", "Сохранить выпуск",
                    "Описание выпуска", "upload-progress-done",
                ]):
                    print(f"mave: файл загружен, форма появилась (шаг {i+1})")
                    break
                print(f"mave: ждём... ({i+1}/36)")

            await page.screenshot(path="/tmp/mave_04_after_upload.png")
            html = await page.content()
            print(f"mave: HTML[1000:3000] = {html[1000:3000]}")

            # 7. Заголовок
            for sel in [
                'input[placeholder*="Название выпуска"]',
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

            # 8. Описание
            for sel in [
                '.ProseMirror',
                'div[contenteditable="true"]',
                'textarea[placeholder*="Описание"]',
                'textarea[placeholder*="описание"]',
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

            await page.screenshot(path="/tmp/mave_05_filled.png")

            # 9. Публикуем
            published = False
            for btn_text in ["Опубликовать", "Сохранить выпуск", "Сохранить", "Publish"]:
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
                raise RuntimeError("Не найдена кнопка публикации — смотри скриншот")

            await asyncio.sleep(5)
            await page.screenshot(path="/tmp/mave_done.png")
            print(f"mave: готово, финальный URL={page.url}")
            return True

        except Exception as e:
            await page.screenshot(path="/tmp/mave_error.png")
            print(f"Ошибка mave: {e}")
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

        await msg.edit_text("🎙️ Adobe Podcast Enhance Speech...")
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
            await query.message.reply_photo(
                photo=open("/tmp/mave_done.png", "rb"),
                caption=caption, parse_mode=ParseMode.MARKDOWN
            )
            await query.message.delete()
        else:
            await query.edit_message_text(caption, parse_mode=ParseMode.MARKDOWN)

        pending.pop(user_id, None)

    except Exception as e:
        screenshot = "/tmp/mave_error.png"
        # Отправляем самый информативный скриншот
        for s in ["/tmp/mave_05_filled.png", "/tmp/mave_04_after_upload.png",
                  "/tmp/mave_03_uploading.png", "/tmp/mave_error.png"]:
            if os.path.exists(s):
                screenshot = s
                break
        if os.path.exists(screenshot):
            await query.message.reply_photo(
                photo=open(screenshot, "rb"),
                caption=f"❌ Ошибка: {e}\n\nСкриншот момента сбоя прикреплён."
            )
            await query.message.delete()
        else:
            await query.edit_message_text(f"❌ Ошибка загрузки: {e}")


async def button_edit(update: Update, tg_context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = pending.get(query.from_user.id)
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

    print("Бот v2 запущен (Adobe Podcast + FFmpeg + фотоотчёт)...")
    app.run_polling()


if __name__ == "__main__":
    main()
