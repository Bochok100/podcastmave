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
    """
    Улучшение звука через Adobe Podcast Enhance API.
    Бесплатно, без регистрации, работает через публичный endpoint.
    Убирает шум, улучшает голос до студийного качества.
    """
    enhanced_path = mp3_path.parent / (mp3_path.stem + "_studio.mp3")

    async with httpx.AsyncClient(timeout=300, follow_redirects=True) as client:
        print(f"Adobe Podcast: загружаем файл {mp3_path.name} ({mp3_path.stat().st_size} байт)")

        # Загружаем файл
        with open(mp3_path, "rb") as f:
            upload_resp = await client.post(
                "https://podcast.adobe.com/api/v1/enhance",
                headers={
                    "Accept": "application/json",
                },
                files={"file": (mp3_path.name, f, "audio/mpeg")},
            )

        print(f"Adobe Podcast: HTTP {upload_resp.status_code}")

        if upload_resp.status_code not in (200, 201, 202):
            print(f"Adobe Podcast: ошибка загрузки {upload_resp.status_code}: {upload_resp.text[:300]}")
            return mp3_path

        data = upload_resp.json()
        print(f"Adobe Podcast: ответ = {str(data)[:300]}")

        # Получаем job ID или сразу файл
        job_id = data.get("jobId") or data.get("id") or data.get("job_id")

        if not job_id:
            # Может быть сразу вернул файл
            download_url = data.get("url") or data.get("download_url")
            if download_url:
                r = await client.get(download_url)
                enhanced_path.write_bytes(r.content)
                if enhanced_path.stat().st_size > 10000:
                    print(f"Adobe Podcast: файл сохранён {enhanced_path.stat().st_size} байт")
                    return enhanced_path
            print("Adobe Podcast: нет job_id и нет url, используем исходный")
            return mp3_path

        # Ждём завершения job
        print(f"Adobe Podcast: job_id={job_id}, ждём...")
        for attempt in range(60):
            await asyncio.sleep(5)
            status_resp = await client.get(
                f"https://podcast.adobe.com/api/v1/enhance/{job_id}",
                headers={"Accept": "application/json"},
            )
            status_data = status_resp.json()
            status = status_data.get("status", "unknown")
            print(f"Adobe Podcast: статус {attempt+1} = {status}")

            if status in ("succeeded", "completed", "done", "finished"):
                download_url = (
                    status_data.get("url") or
                    status_data.get("download_url") or
                    status_data.get("outputUrl")
                )
                if not download_url:
                    print("Adobe Podcast: нет URL для скачивания, используем исходный")
                    return mp3_path

                r = await client.get(download_url)
                enhanced_path.write_bytes(r.content)
                size = enhanced_path.stat().st_size
                print(f"Adobe Podcast: файл сохранён {size} байт")

                if size < 10000:
                    print("Adobe Podcast: файл слишком маленький, используем исходный")
                    return mp3_path

                return enhanced_path

            if status in ("failed", "error"):
                print(f"Adobe Podcast: ошибка обработки, используем исходный")
                return mp3_path

    print("Adobe Podcast: таймаут, используем исходный")
    return mp3_path


def transcribe(mp3_path: Path) -> str:
    client = openai.OpenAI(api_key=OPENAI_KEY)
    # Копируем файл с явным именем .mp3 — Whisper определяет формат строго по имени файла
    safe_path = mp3_path.parent / (mp3_path.stem + ".mp3")
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

            # 2. Убиваем все модалки через JS
            await page.evaluate("""() => {
                document.querySelectorAll('[id^="q-portal--dialog"]').forEach(e => e.remove());
                document.querySelectorAll('.q-overlay, .q-dialog__backdrop').forEach(e => e.remove());
                document.body.classList.remove('q-body--prevent-scroll', 'q-body--force-scrollbar-x');
            }""")
            await asyncio.sleep(1)
            await page.screenshot(path="/tmp/mave_01_dashboard.png")

            # 3. Клик "Добавить выпуск"
            await page.locator('text=Добавить выпуск').first.click()
            await asyncio.sleep(3)
            await page.wait_for_load_state("networkidle")
            await page.screenshot(path="/tmp/mave_02_after_click.png")
            print(f"mave: после клика, URL={page.url}")

            # 4. Ищем поле загрузки
            await page.wait_for_selector('input[type="file"]', timeout=15000)
            await page.screenshot(path="/tmp/mave_03_form.png")
            print("mave: форма найдена")

            # 5. Загружаем файл
            await page.set_input_files('input[type="file"]', str(mp3_path))
            print(f"mave: файл отправлен: {mp3_path.name}")
            await asyncio.sleep(5)

            # 6. Ждём завершения загрузки — проверяем HTML каждые 5 сек
            print("mave: ждём завершения загрузки...")
            for i in range(24):
                await asyncio.sleep(5)
                html = await page.content()
                if any(x in html for x in [
                    "audio-player", "upload-progress-done", "waveform",
                    "Опубликовать", "Сохранить", "Название выпуска", "episode-title"
                ]):
                    print(f"mave: загрузка завершена (шаг {i+1})")
                    break
                print(f"mave: ещё ждём ({i+1}/24)...")

            await page.screenshot(path="/tmp/mave_04_after_upload.png")
            html = await page.content()
            print(f"mave: HTML (500 символов): {html[:500]}")

            # 7. Заголовок
            for sel in ['input[placeholder*="Название"]', 'input[placeholder*="название"]',
                        'input[name="title"]', 'input[type="text"]']:
                try:
                    el = page.locator(sel).first
                    if await el.count() > 0:
                        await el.fill(title)
                        print(f"mave: заголовок → {sel}")
                        break
                except Exception:
                    continue

            # 8. Описание
            for sel in ['.ProseMirror', 'div[contenteditable="true"]',
                        'textarea[placeholder*="писание"]', 'textarea[name="description"]']:
                try:
                    el = page.locator(sel).first
                    if await el.count() > 0:
                        await el.fill(description)
                        print(f"mave: описание → {sel}")
                        break
                except Exception:
                    continue

            await page.screenshot(path="/tmp/mave_05_filled.png")

            # 9. Публикуем
            for btn_text in ["Опубликовать", "Сохранить", "Publish", "Save"]:
                try:
                    btn = page.locator(f'button:has-text("{btn_text}")').first
                    if await btn.count() > 0:
                        await btn.click()
                        print(f"mave: кнопка '{btn_text}' нажата")
                        break
                except Exception:
                    continue

            await asyncio.sleep(5)
            await page.screenshot(path="/tmp/mave_06_done.png")
            print(f"mave: готово, URL={page.url}")
            return True

        except Exception as e:
            await page.screenshot(path="/tmp/mave_error.png")
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
        per_message=False,
        per_chat=True,
    )

    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_voice))
    app.add_handler(conv)
    app.add_handler(CallbackQueryHandler(button_publish, pattern="^publish$"))
    app.add_handler(CallbackQueryHandler(button_cancel, pattern="^cancel$"))
    
    print("Бот v2 запущен (только OpenAI)...")
    app.run_polling()

if __name__ == "__main__":
    main()
