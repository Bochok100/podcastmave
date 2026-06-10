"""
Podcast Bot v2 (Xvfb + Adobe 2FA + Drag-and-Drop + Веб-камера)
"""

import os
import json
import asyncio
import tempfile
import subprocess
import shutil
import threading
import time
import base64
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, MessageHandler, CallbackQueryHandler,
    ConversationHandler, MessageHandler as MH,
    filters, ContextTypes,
)
from telegram.constants import ParseMode

import openai
from playwright.async_api import async_playwright

# ──────────────────────────────────────────────
# Настройки
# ──────────────────────────────────────────────
TELEGRAM_TOKEN     = os.getenv("TELEGRAM_TOKEN")
OPENAI_KEY         = os.getenv("OPENAI_API_KEY")
MAVE_EMAIL         = os.getenv("MAVE_EMAIL")
MAVE_PASSWORD      = os.getenv("MAVE_PASSWORD")
ADOBE_EMAIL        = os.getenv("ADOBE_EMAIL", "")
ADOBE_PASSWORD     = os.getenv("ADOBE_PASSWORD", "")
ADOBE_COOKIES_JSON = os.getenv("ADOBE_COOKIES_JSON")
ALLOWED_USER_ID    = int(os.getenv("ALLOWED_USER_ID", "0"))

EDIT_TITLE, EDIT_DESC = range(2)

STYLE_PROMPT = """
Ты — редактор подкаста Василия. Темы бывают разные: крипта, ИИ, технологии, семья, жизнь, бытовые вопросы — всё что угодно.
ТВОЯ ЗАДАЧА: по расшифровке придумать заголовок и описание.

=== СТИЛЬ ЗАГОЛОВКА ===
Учись у этих примеров (только стиль, не копируй):
- Как на самом деле работают сделки
- Что такое Long и Short на самом деле
- Будущие тренды в крипте: куда смотреть до хайпа
- Агенты ИИ: почему без них уже нельзя
- Майнинг в России: быть или не быть?

Закономерности: конкретно, без воды, иногда "на самом деле", иногда вопрос.
Тема диктует заголовок — не тяни всё к крипте если тема другая.
НЕ пиши "топ", "секреты", "шокирующий".

=== СТИЛЬ ОПИСАНИЯ ===
2 предложения: суть выпуска + зачем слушать. Разговорный тон.

=== ФОРМАТ (СТРОГО) ===
ЗАГОЛОВОК: [заголовок]
ОПИСАНИЕ: [описание]

ТРАНСКРИПЦИЯ:
"""

pending = {}
adobe_2fa_state = {}


# ──────────────────────────────────────────────
# HTTP-сервер (Render требует открытый порт)
# + /screen показывает последний скриншот Adobe
# ──────────────────────────────────────────────
class DummyHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/screen':
            for f in ['/tmp/adobe_before_upload.png', '/tmp/adobe_error.png', '/tmp/adobe_state.png']:
                if os.path.exists(f):
                    self.send_response(200)
                    self.send_header("Content-Type", "image/png")
                    self.end_headers()
                    self.wfile.write(open(f, 'rb').read())
                    return
            self.send_response(404)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write("Скриншот ещё не готов. Отправь голосовое боту.".encode())
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write("Bot is alive! Скриншот Adobe: /screen".encode())

    def log_message(self, format, *args):
        return


def run_dummy_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(('0.0.0.0', port), DummyHandler)
    print(f"✅ HTTP-сервер запущен на порту {port}")
    server.serve_forever()


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
    subprocess.run(
        ["ffmpeg", "-y", "-i", str(input_path),
         "-codec:a", "libmp3lame", "-b:a", "128k", "-ar", "44100", "-ac", "1", str(mp3_path)],
        capture_output=True, text=True
    )
    return mp3_path


# ──────────────────────────────────────────────
# 3. Adobe Podcast Enhance через Playwright
# ──────────────────────────────────────────────
async def enhance_audio(mp3_path: Path, user_id: int, send_screenshot=None) -> Path:
    if not ADOBE_EMAIL.strip() or not ADOBE_PASSWORD.strip():
        raise RuntimeError("В Render не заданы ADOBE_EMAIL или ADOBE_PASSWORD")

    adobe_path  = mp3_path.parent / (mp3_path.stem + "_adobe.mp3")
    studio_path = mp3_path.parent / (mp3_path.stem + "_studio.mp3")

    async def notify(path: str, caption: str):
        if send_screenshot and os.path.exists(path):
            try:
                await send_screenshot(path, caption)
            except Exception:
                pass

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=False,  # Xvfb даёт виртуальный экран
            args=['--no-sandbox', '--disable-setuid-sandbox', '--disable-dev-shm-usage',
                  '--disable-blink-features=AutomationControlled']
        )
        context = await browser.new_context(
            viewport={"width": 1366, "height": 768},
            locale="en-US",
            timezone_id="America/New_York",
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        )
        # Скрываем webdriver через init script (замена playwright-stealth)
        await context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
            Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
            window.chrome = {runtime: {}};
        """)

        page = await context.new_page()
        try:
            # Загружаем куки если есть
            if ADOBE_COOKIES_JSON:
                try:
                    await context.add_cookies(json.loads(ADOBE_COOKIES_JSON))
                    print("Adobe: куки загружены")
                except Exception as e:
                    print(f"Adobe: куки не загрузились — {e}")

            # Переходим на enhance
            print("Adobe: переход на podcast.adobe.com/enhance...")
            await page.goto("https://podcast.adobe.com/enhance", timeout=60000)
            await page.wait_for_load_state("domcontentloaded")
            await asyncio.sleep(5)

            await page.screenshot(path="/tmp/adobe_state.png")
            await notify("/tmp/adobe_state.png", f"Adobe: открыли страницу. URL: {page.url}")
            print(f"Adobe: URL = {page.url}")

            # Нажимаем Sign in если есть на странице
            if "auth" not in page.url and "ims" not in page.url:
                try:
                    await page.evaluate("""() => {
                        const el = Array.from(document.querySelectorAll('a, button'))
                            .find(e => e.innerText && e.innerText.trim().toLowerCase() === 'sign in');
                        if (el) el.click();
                    }""")
                    await asyncio.sleep(4)
                    print(f"Adobe: после Sign in клика URL = {page.url}")
                except Exception:
                    pass

            # ── Авторизация ──
            login_needed = await page.locator('input[type="email"], input[name="username"]').count() > 0
            if not login_needed and ("auth" in page.url or "ims" in page.url):
                login_needed = True

            if login_needed:
                print("Adobe: вводим email...")
                email_field = page.locator('input[type="email"], input[name="username"]').first
                await email_field.wait_for(state="visible", timeout=15000)
                await email_field.click()
                await asyncio.sleep(1)
                await email_field.press_sequentially(ADOBE_EMAIL.strip(), delay=80)
                await asyncio.sleep(2)

                # Continue
                for sel in ['button:has-text("Continue")', 'button:has-text("Продолжить")',
                            '#btn-id-forward', 'button[type="submit"]']:
                    try:
                        el = page.locator(sel).first
                        if await el.count() > 0:
                            await el.click()
                            print(f"Adobe: Continue нажат '{sel}'")
                            break
                    except Exception:
                        continue
                await asyncio.sleep(4)

                # 2FA — подтверждение личности
                if await page.locator('text=/Verify your identity|Подтверждение личности/i').count() > 0:
                    print("Adobe: экран 2FA — нажимаем Continue для отправки кода...")
                    btn = page.locator('button:has-text("Continue"), button:has-text("Продолжить")').first
                    if await btn.count() > 0:
                        await btn.click()
                        await asyncio.sleep(4)

                # 2FA — ввод кода
                code_input = page.locator('input[type="text"][autocomplete*="one-time"], input[name*="code"], input[maxlength="6"]').first
                if await code_input.count() > 0:
                    print("Adobe: запрашиваем 2FA код у пользователя...")
                    await page.screenshot(path="/tmp/adobe_2fa.png")
                    await notify("/tmp/adobe_2fa.png",
                                 "⚠️ Adobe запросил 2FA код!\nПроверь почту и пришли мне код обычным текстом (2 минуты).")
                    event = asyncio.Event()
                    adobe_2fa_state[user_id] = {"event": event, "code": ""}
                    try:
                        await asyncio.wait_for(event.wait(), timeout=120.0)
                        received_code = adobe_2fa_state[user_id]["code"].strip()
                        await code_input.fill(received_code)
                        await asyncio.sleep(1)
                        sub = page.locator('button:has-text("Continue"), button:has-text("Submit"), button:has-text("Verify"), button[type="submit"]').first
                        if await sub.count() > 0:
                            await sub.click()
                        await asyncio.sleep(5)
                        print(f"Adobe: 2FA код '{received_code}' отправлен")
                    except asyncio.TimeoutError:
                        raise RuntimeError("Таймаут 2FA: код не пришёл за 2 минуты")
                    finally:
                        adobe_2fa_state.pop(user_id, None)

                # Пароль
                pwd = page.locator('input[type="password"], #password').first
                if await pwd.count() > 0:
                    print("Adobe: вводим пароль...")
                    await pwd.click()
                    await asyncio.sleep(1)
                    await pwd.press_sequentially(ADOBE_PASSWORD.strip(), delay=80)
                    await asyncio.sleep(2)
                    for sel in ['button:has-text("Sign in")', 'button:has-text("Continue")',
                                'button:has-text("Войти")', 'button[type="submit"]']:
                        try:
                            el = page.locator(sel).first
                            if await el.count() > 0:
                                await el.click()
                                print(f"Adobe: Sign in нажат '{sel}'")
                                break
                        except Exception:
                            continue
                    await asyncio.sleep(6)

                # Ждём редиректа на enhance
                print("Adobe: ждём редиректа...")
                for _ in range(20):
                    url = page.url
                    if "enhance" in url or "podcast.adobe.com" in url:
                        break
                    # Закрываем промежуточные экраны
                    try:
                        await page.evaluate("""() => {
                            const btn = Array.from(document.querySelectorAll('button,a'))
                                .find(e => /не сейчас|not now|skip|пропустить|продолжить|continue|yes|да/i.test(e.innerText));
                            if (btn) btn.click();
                        }""")
                    except Exception:
                        pass
                    await asyncio.sleep(1)

            # Принудительный переход если не на enhance
            if "enhance" not in page.url:
                print("Adobe: принудительный переход на enhance...")
                await page.goto("https://podcast.adobe.com/enhance", timeout=60000)
                await page.wait_for_load_state("domcontentloaded")
                await asyncio.sleep(5)

            # Проверяем что залогинились
            if await page.locator('input[type="email"], input[name="username"]').count() > 0:
                await page.screenshot(path="/tmp/adobe_error.png")
                await notify("/tmp/adobe_error.png", "❌ Не удалось войти в Adobe. Проверь /screen")
                raise RuntimeError("Adobe: не удалось войти — неверный email или пароль")

            # Закрываем баннеры
            try:
                await page.evaluate("""() => {
                    document.querySelectorAll('button').forEach(b => {
                        if (/Accept|Agree|Got it|Понятно|Close|Skip/i.test(b.innerText)) b.click();
                    });
                }""")
                await asyncio.sleep(2)
            except Exception:
                pass

            await page.screenshot(path="/tmp/adobe_before_upload.png")
            await notify("/tmp/adobe_before_upload.png", "Adobe: залогинились, загружаем файл...")
            print("Adobe: страница enhance открыта, загружаем файл...")

            # ── Загрузка файла (3 стратегии) ──
            uploaded = False

            # Стратегия 1: скрытый input[type=file]
            for frame in page.frames:
                try:
                    inputs = await frame.locator('input[type="file"]').all()
                    for inp in inputs:
                        try:
                            await inp.set_input_files(str(mp3_path), timeout=5000)
                            print(f"✅ Файл загружен через input (фрейм: {frame.url[:40]})")
                            uploaded = True
                            break
                        except Exception:
                            pass
                    if uploaded:
                        break
                except Exception:
                    continue

            # Стратегия 2: file chooser через клик
            if not uploaded:
                print("Adobe: пробуем file chooser...")
                for frame in page.frames:
                    try:
                        btn = frame.locator('text=/Choose files|Выбрать|Upload/i').first
                        if await btn.count() > 0:
                            async with page.expect_file_chooser(timeout=5000) as fc_info:
                                await btn.click(force=True)
                            fc = await fc_info.value
                            await fc.set_files(str(mp3_path))
                            print("✅ Файл загружен через file chooser")
                            uploaded = True
                            break
                    except Exception:
                        continue

            # Стратегия 3: drag-and-drop через JS
            if not uploaded:
                print("Adobe: drag-and-drop через JS DataTransfer...")
                with open(mp3_path, "rb") as f:
                    b64 = base64.b64encode(f.read()).decode()
                await page.evaluate(f"""async () => {{
                    const b64 = "{b64}";
                    const bin = atob(b64);
                    const arr = new Uint8Array(bin.length);
                    for (let i = 0; i < bin.length; i++) arr[i] = bin.charCodeAt(i);
                    const file = new File([arr], "{mp3_path.name}", {{type:"audio/mpeg"}});
                    const dt = new DataTransfer();
                    dt.items.add(file);
                    ['dragenter','dragover','drop'].forEach(ev =>
                        document.body.dispatchEvent(new DragEvent(ev, {{bubbles:true, cancelable:true, dataTransfer:dt}}))
                    );
                }}""")
                print("✅ Drag-and-drop выполнен")
                uploaded = True

            await asyncio.sleep(3)
            await page.screenshot(path="/tmp/adobe_uploaded.png")
            await notify("/tmp/adobe_uploaded.png", "Adobe: файл отправлен, ждём обработки...")

            # Нажимаем Enhance
            for frame in page.frames:
                for sel in ['button:has-text("Enhance speech")', 'button:has-text("Enhance")',
                            'button:has-text("Clean up")', 'button[type="submit"]']:
                    try:
                        btn = frame.locator(sel).first
                        if await btn.count() > 0:
                            await btn.evaluate("node => node.click()")
                            print(f"Adobe: Enhance нажат '{sel}'")
                            break
                    except Exception:
                        continue

            # Ждём кнопку Download (до 5 минут, скрины каждые 30 сек)
            print("Adobe: ждём обработку (до 5 минут)...")
            download_locator = None
            for i in range(60):
                await asyncio.sleep(5)
                for frame in page.frames:
                    dl = frame.locator('button:has-text("Download"), a:has-text("Download")').last
                    if await dl.count() > 0:
                        download_locator = dl
                        break
                if download_locator:
                    print(f"Adobe: Download найден (шаг {i+1})")
                    break
                if i % 6 == 5:
                    await page.screenshot(path=f"/tmp/adobe_wait_{i}.png")
                    await notify(f"/tmp/adobe_wait_{i}.png", f"Adobe: обрабатываем... ({(i+1)*5} сек)")
                    print(f"Adobe: ждём ({i+1}/60)")

            if not download_locator:
                await page.screenshot(path="/tmp/adobe_timeout.png")
                await notify("/tmp/adobe_timeout.png", "Adobe: таймаут 5 минут — Download не появился")
                raise RuntimeError("Adobe: кнопка Download не появилась за 5 минут")

            await page.screenshot(path="/tmp/adobe_done.png")
            await notify("/tmp/adobe_done.png", "Adobe: обработка завершена, скачиваем!")

            # Скачиваем
            async with page.expect_download(timeout=120000) as dl_info:
                await download_locator.evaluate("node => node.click()")
            dl = await dl_info.value
            await dl.save_as(str(adobe_path))
            size = adobe_path.stat().st_size
            print(f"Adobe: скачан файл {size} байт")

            if size < 10000:
                raise RuntimeError(f"Adobe вернул слишком маленький файл ({size} байт)")

            # loudnorm поверх Adobe
            result = subprocess.run(
                ["ffmpeg", "-y", "-i", str(adobe_path),
                 "-af", "loudnorm=I=-16:TP=-1.5:LRA=11",
                 "-codec:a", "libmp3lame", "-b:a", "128k", "-ar", "44100", "-ac", "1",
                 str(studio_path)],
                capture_output=True, text=True
            )
            if result.returncode == 0 and studio_path.exists():
                print(f"Adobe+loudnorm: {studio_path.stat().st_size} байт")
                return studio_path
            return adobe_path

        except Exception as e:
            try:
                await page.screenshot(path="/tmp/adobe_error.png")
                await notify("/tmp/adobe_error.png", f"❌ Adobe ошибка: {str(e)[:100]}")
            except Exception:
                pass
            raise RuntimeError(f"Adobe: {str(e)[:200]}")
        finally:
            await browser.close()


# ──────────────────────────────────────────────
# 4. Транскрипция Whisper
# ──────────────────────────────────────────────
def transcribe(mp3_path: Path) -> str:
    client = openai.OpenAI(api_key=OPENAI_KEY)
    safe = mp3_path.parent / (mp3_path.stem + "_w.mp3")
    shutil.copy2(mp3_path, safe)
    with open(safe, "rb") as f:
        return client.audio.transcriptions.create(
            model="whisper-1", file=f, language="ru"
        ).text


# ──────────────────────────────────────────────
# 5. Генерация метаданных GPT-4o
# ──────────────────────────────────────────────
def generate_metadata(transcript: str) -> tuple[str, str]:
    client = openai.OpenAI(api_key=OPENAI_KEY)
    resp = client.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "user", "content": STYLE_PROMPT + transcript}],
        max_tokens=512, temperature=0.7
    )
    text = resp.choices[0].message.content
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
        browser = await p.chromium.launch(
            headless=False,
            args=['--no-sandbox', '--disable-setuid-sandbox', '--disable-dev-shm-usage']
        )
        context = await browser.new_context(viewport={"width": 1280, "height": 900}, locale="ru-RU")
        page = await context.new_page()
        try:
            await page.goto("https://app.mave.digital/login")
            await page.fill('input[type="email"]', MAVE_EMAIL)
            await page.fill('input[type="password"]', MAVE_PASSWORD)
            await page.click('button[type="submit"]')
            await page.wait_for_load_state("domcontentloaded")
            await asyncio.sleep(4)

            await page.evaluate("""() => {
                document.querySelectorAll('[id^="q-portal--dialog"]').forEach(e => e.remove());
                document.querySelectorAll('.q-overlay, .q-dialog__backdrop').forEach(e => e.remove());
                document.body.classList.remove('q-body--prevent-scroll', 'q-body--force-scrollbar-x');
            }""")
            await asyncio.sleep(1)

            await page.locator('text=Добавить выпуск').first.click()
            await asyncio.sleep(3)
            await page.wait_for_selector('input[type="file"]', state='attached', timeout=15000)
            await page.set_input_files('input[type="file"]', str(mp3_path))
            await asyncio.sleep(2)

            for sel in ['button:has-text("Загрузить файл")', 'button:has-text("Загрузить")']:
                try:
                    btn = page.locator(sel).first
                    if await btn.count() > 0:
                        await btn.evaluate("node => node.click()")
                        break
                except Exception:
                    continue

            for i in range(36):
                await asyncio.sleep(5)
                html = await page.content()
                if any(x in html for x in ["Название выпуска", "episode-title", "upload-progress-done"]):
                    break

            for sel in ['input[placeholder*="Название выпуска"]', 'input[placeholder*="Название"]', 'input[name="title"]']:
                try:
                    el = page.locator(sel).first
                    if await el.count() > 0:
                        await el.clear()
                        await el.fill(title)
                        break
                except Exception:
                    continue

            for sel in ['.ProseMirror', 'div[contenteditable="true"]', 'textarea[name="description"]']:
                try:
                    el = page.locator(sel).first
                    if await el.count() > 0:
                        await el.click()
                        await el.fill(description)
                        break
                except Exception:
                    continue

            published = False
            for btn_text in ["Опубликовать", "Сохранить выпуск", "Сохранить"]:
                try:
                    btn = page.locator(f'button:has-text("{btn_text}")').first
                    if await btn.count() > 0:
                        await btn.evaluate("node => node.click()")
                        published = True
                        break
                except Exception:
                    continue

            if not published:
                raise RuntimeError("Кнопка публикации mave не найдена")
            await asyncio.sleep(5)
            await page.screenshot(path="/tmp/mave_done.png")
            return True
        except Exception as e:
            await page.screenshot(path="/tmp/mave_error.png")
            raise RuntimeError(f"{e}")
        finally:
            await browser.close()


# ──────────────────────────────────────────────
# Telegram handlers
# ──────────────────────────────────────────────
async def handle_voice(update: Update, tg_context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if ALLOWED_USER_ID and user_id != ALLOWED_USER_ID:
        return
    msg = await update.message.reply_text("⏳ Начинаю обработку...")
    try:
        ogg = await download_voice(update, tg_context)
        await msg.edit_text("🔄 Конвертирую в MP3...")
        mp3 = convert_to_mp3(ogg)

        await msg.edit_text("🎙️ Adobe Podcast: улучшаю звук (3-5 минут)...")

        async def send_screenshot(path: str, caption: str):
            try:
                if os.path.exists(path):
                    await update.message.reply_photo(photo=open(path, "rb"), caption=f"ℹ️ {caption}")
            except Exception:
                pass

        studio_mp3 = await enhance_audio(mp3, user_id=user_id, send_screenshot=send_screenshot)

        await msg.edit_text("📝 Транскрибирую (Whisper)...")
        transcript = transcribe(studio_mp3)

        await msg.edit_text("✍️ Генерирую заголовок и описание (GPT-4o)...")
        title, description = generate_metadata(transcript)

        pending[user_id] = {"mp3": studio_mp3, "title": title, "description": description}
        await msg.delete()

        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Опубликовать в mave", callback_data="publish"),
            InlineKeyboardButton("✏️ Изменить", callback_data="edit"),
        ], [InlineKeyboardButton("❌ Отмена", callback_data="cancel")]])
        preview = (f"🎙 *Готов к публикации*\n\n"
                   f"*Заголовок:*\n{title}\n\n*Описание:*\n{description}\n\nОдобряешь?")
        await update.message.reply_document(document=open(studio_mp3, "rb"), filename=f"{title[:40]}.mp3")
        await update.message.reply_text(preview, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)
    except Exception as e:
        await msg.edit_text(f"❌ Ошибка: {e}")


async def handle_global_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Перехватывает 2FA код от пользователя"""
    user_id = update.effective_user.id
    if user_id in adobe_2fa_state:
        code = update.message.text.strip()
        adobe_2fa_state[user_id]["code"] = code
        adobe_2fa_state[user_id]["event"].set()
        await update.message.reply_text("✅ Код принят, отправляю в Adobe...")


async def button_publish(update: Update, tg_context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    data = pending.get(user_id)
    if not data:
        await query.edit_message_text("❌ Сессия устарела. Запиши заново.")
        return
    await query.edit_message_text("⏳ Загружаю в mave.digital...")
    try:
        await upload_to_mave(data["mp3"], data["title"], data["description"])
        caption = f"✅ *Опубликовано!*\n\n_{data['title']}_\n\nСкоро на Spotify и Apple Podcasts."
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
        if os.path.exists("/tmp/mave_error.png"):
            await query.message.reply_photo(
                photo=open("/tmp/mave_error.png", "rb"),
                caption=f"❌ Ошибка mave: {e}"
            )
            await query.message.delete()
        else:
            await query.edit_message_text(f"❌ Ошибка: {e}")


async def button_edit(update: Update, tg_context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = pending.get(query.from_user.id)
    await query.edit_message_text(
        f"✏️ Заголовок:\n*{data['title']}*\n\nНапиши новый (или /skip):",
        parse_mode=ParseMode.MARKDOWN
    )
    return EDIT_TITLE


async def edit_title(update: Update, tg_context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if update.message.text != "/skip":
        pending[user_id]["title"] = update.message.text
    data = pending[user_id]
    await update.message.reply_text(
        f"📝 Описание:\n_{data['description']}_\n\nНапиши новое (или /skip):",
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
    threading.Thread(target=run_dummy_server, daemon=True).start()

    print("🚀 Запускаем бота...")
    try:
        app = Application.builder().token(TELEGRAM_TOKEN).build()

        # 2FA перехватчик — высокий приоритет (group=-1)
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_global_text), group=-1)

        conv = ConversationHandler(
            entry_points=[CallbackQueryHandler(button_edit, pattern="^edit$")],
            states={
                EDIT_TITLE: [MH(filters.TEXT & ~filters.COMMAND, edit_title)],
                EDIT_DESC:  [MH(filters.TEXT & ~filters.COMMAND, edit_desc)],
            },
            fallbacks=[CallbackQueryHandler(button_cancel, pattern="^cancel$")],
            per_chat=True,
        )
        app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_voice))
        app.add_handler(conv)
        app.add_handler(CallbackQueryHandler(button_publish, pattern="^publish$"))
        app.add_handler(CallbackQueryHandler(button_cancel, pattern="^cancel$"))

        print("✅ Бот запущен!")
        app.run_polling()

    except Exception as e:
        print(f"❌ КРИТИЧЕСКАЯ ОШИБКА: {e}")
        while True:
            time.sleep(60)


if __name__ == "__main__":
    main()
