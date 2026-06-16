"""
Podcast Bot v13.6
- ИСПРАВЛЕНО ЗАВИСАНИЕ (добавлены таймауты на все клики Playwright)
- ИСПРАВЛЕН IMAP: добавлен сокет-таймаут против вечных зависаний сети
- Улучшен универсальный ввод OTP через эмуляцию клавиатуры (fix fallback)
- ДОБАВЛЕНЫ ОТЧЕТЫ СО СКРИНШОТАМИ: бот отправляет шаги автоматизации прямо в Telegram чат
"""
import os
import asyncio
import tempfile
import time
import base64
import gc
import re
import imaplib
import email
import html
import datetime
import socket
from pathlib import Path
from email.utils import parsedate_to_datetime

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, MessageHandler, CallbackQueryHandler,
    ConversationHandler, MessageHandler as MH,
    filters, ContextTypes,
)
from telegram.constants import ParseMode
import openai
from playwright.async_api import async_playwright

# Защита от бесконечного зависания IMAP-соединений и сетевых запросов
socket.setdefaulttimeout(20)

# ── Настройки ──────────────────────────────────
TELEGRAM_TOKEN  = os.getenv("TELEGRAM_TOKEN")
OPENAI_KEY      = os.getenv("OPENAI_API_KEY")
MAVE_EMAIL      = os.getenv("MAVE_EMAIL")
MAVE_PASSWORD   = os.getenv("MAVE_PASSWORD")
ADOBE_EMAIL     = os.getenv("ADOBE_EMAIL", "")
ADOBE_PASSWORD  = os.getenv("ADOBE_PASSWORD", "")
EMAIL_USER      = os.getenv("EMAIL_USER", "")
EMAIL_PASS      = os.getenv("EMAIL_PASS", "")
CHANNEL_ID      = os.getenv("CHANNEL_ID", "")
ALLOWED_USER_ID = int(os.getenv("ALLOWED_USER_ID", "0"))

STATE_FILE = "adobe_state.json"
COVER_FILE = "cover.jpg"

EDIT_TITLE, EDIT_DESC, EDIT_POST = range(3)
pending = {}
adobe_2fa_state = {}

STYLE_PROMPT = """
Ты — редактор подкаста Василия. Темы разные: крипта, ИИ, технологии, семья, жизнь.
По расшифровке придумай заголовок и описание.
Правила: конкретно, без воды.
Описание: 2 предложения, разговорный тон.
Ответ строго:
ЗАГОЛОВОК: [текст]
ОПИСАНИЕ: [текст]
ТРАНСКРИПЦИЯ:
"""

POST_TEMPLATE = """<b>НОВЫЙ ВЫПУСК УЖЕ НА ПЛОЩАДКАХ:
{title}

Залетайте по ссылкам ниже, выбирайте свою платформу и оставайтесь на связи! 👇

Слушайте там, где удобно:
🎵 <a href="https://music.yandex.ru/album/40139714">ЯНДЕКС МУЗЫКА</a>
🎵 <a href="https://open.spotify.com/show/34qEduYapGFILe2jyH1L0h?si=NQ9Lz4hXT0W-8pgOfY5vvg">SPOTIFY</a>
🎵 <a href="https://podcasts.apple.com/us/podcast/vasiliy-crypto-%D0%BD%D0%B5-%D0%B1%D1%83%D0%B4%D1%8C-%D1%82%D0%BE%D0%BB%D0%BF%D0%BE%D0%B9/id1865729420">Apple Podcasts</a></b>"""

# ── Планировщик (Бразилия UTC-3) ───────────────
def get_delay_seconds(target_time_str: str) -> int:
    tz_br = datetime.timezone(datetime.timedelta(hours=-3))
    now = datetime.datetime.now(tz_br)
    try:
        h, m = map(int, target_time_str.replace('.', ':').split(':'))
        if not (0 <= h <= 23 and 0 <= m <= 59): return -1
    except:
        return -1
    target = now.replace(hour=h, minute=m, second=0, microsecond=0)
    if target < now:
        target += datetime.timedelta(days=1)
    return int((target - now).total_seconds())

async def delayed_publish(delay: int, chat_id: str, photo_path: str, text: str, bot):
    await asyncio.sleep(delay)
    try:
        if photo_path and os.path.exists(photo_path):
            with open(photo_path, "rb") as img:
                await bot.send_photo(chat_id=chat_id, photo=img, caption=text, parse_mode=ParseMode.HTML)
        else:
            await bot.send_message(chat_id=chat_id, text=text, parse_mode=ParseMode.HTML)
    except Exception as e:
        print(f"Ошибка отложенной публикации: {e}")

# ── IMAP Почта ─────────────────────────────────
async def fetch_adobe_code_from_email() -> str:
    if not EMAIL_USER or not EMAIL_PASS:
        return None

    def _fetch():
        try:
            mail = imaplib.IMAP4_SSL("imap.gmail.com")
            mail.login(EMAIL_USER, EMAIL_PASS)
            mail.select("inbox")

            mail_ids = []
            for sender in ["message@adobe.com", "adobe@email.adobe.com", "noreply@adobe.com"]:
                status, messages = mail.search(None, f'(FROM "{sender}")')
                ids = messages[0].split()
                if ids:
                    mail_ids = ids
                    print(f"IMAP: нашли письма от {sender}")
                    break

            if not mail_ids:
                print("IMAP: писем от Adobe не найдено")
                return None

            # Проверяем последние 3 письма
            for mid in reversed(mail_ids[-3:]):
                status, msg_data = mail.fetch(mid, '(RFC822)')
                for response_part in msg_data:
                    if not isinstance(response_part, tuple):
                        continue
                    msg = email.message_from_bytes(response_part[1])

                    # Проверка даты (чтобы код был свежим)
                    msg_date = msg.get("Date")
                    if msg_date:
                        try:
                            dt = parsedate_to_datetime(msg_date)
                            now = datetime.datetime.now(datetime.timezone.utc)
                            if (now - dt).total_seconds() > 300: # Старше 5 минут пропускаем
                                continue
                        except Exception as date_err:
                            print(f"Ошибка парсинга даты: {date_err}")

                    body = ""
                    if msg.is_multipart():
                        for part in msg.walk():
                            ct = part.get_content_type()
                            if ct == "text/plain":
                                body = part.get_payload(decode=True).decode(errors="ignore")
                                break
                            elif ct == "text/html" and not body:
                                body = part.get_payload(decode=True).decode(errors="ignore")
                    else:
                        body = msg.get_payload(decode=True).decode(errors="ignore")

                    print(f"IMAP: тело письма (первые 300 символов): {body[:300]}")
                    match = re.search(r'\b\d{6}\b', body)
                    if match:
                        print(f"IMAP: найден код {match.group(0)}")
                        return match.group(0)

        except Exception as e:
            print(f"Ошибка IMAP: {e}")
        return None

    print("IMAP: ждём 15 секунд перед первой проверкой...")
    await asyncio.sleep(15)

    for attempt in range(18):
        print(f"IMAP: попытка {attempt + 1}/18...")
        code = await asyncio.to_thread(_fetch)
        if code:
            return code
        await asyncio.sleep(10)

    return None

# ── Утилиты ────────────────────────────────────
async def run_dummy_server():
    async def handle_client(reader, writer):
        try:
            writer.write("HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\n\r\nBot alive!\r\n".encode('utf8'))
            await writer.drain()
        except: pass
        finally:
            try: writer.close(); await writer.wait_closed()
            except: pass
    server = await asyncio.start_server(handle_client, '0.0.0.0', int(os.environ.get("PORT", 10000)))
    async with server: await server.serve_forever()

async def post_init(app: Application):
    asyncio.create_task(run_dummy_server())

async def download_voice(update, context) -> Path:
    voice = update.message.voice or update.message.audio
    f = await context.bot.get_file(voice.file_id)
    fd, path = tempfile.mkstemp(suffix=".ogg")
    os.close(fd)
    await f.download_to_drive(path)
    return Path(path)

async def to_mp3(src: Path) -> Path:
    dst = src.with_suffix(".mp3")
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-y", "-i", str(src),
        "-codec:a", "libmp3lame", "-b:a", "128k", "-ar", "44100", "-ac", "1",
        str(dst),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    await asyncio.wait_for(proc.communicate(), timeout=120)
    if not dst.exists() or dst.stat().st_size < 1000:
        raise RuntimeError("Ошибка ffmpeg")
    return dst

# ── Ввод 2FA кода в поля Adobe ─────────────────
async def enter_adobe_code(page, final_code: str):
    await asyncio.sleep(1)

    selectors_to_try = [
        'input[maxlength="1"]',
        'input[autocomplete="one-time-code"]',
        'input[type="text"][maxlength="1"]',
        'input[type="number"][maxlength="1"]',
    ]

    code_inputs = None
    for selector in selectors_to_try:
        loc = page.locator(selector)
        count = await loc.count()
        if count >= 2:
            code_inputs = loc
            print(f"2FA: нашли {count} полей по селектору '{selector}'")
            break

    if code_inputs and await code_inputs.count() >= len(final_code):
        for i, digit in enumerate(final_code):
            try:
                field = code_inputs.nth(i)
                await field.click(timeout=5000)
                await asyncio.sleep(0.1)
                await field.fill(digit, timeout=5000)
                await asyncio.sleep(0.15)
            except Exception as e:
                print(f"2FA: ошибка ввода цифры {i}: {e}")
        print(f"2FA: код введён посимвольно ({len(final_code)} цифр)")
    else:
        print("2FA: отдельные ячейки не распознаны селектором, используем клавиатурный fallback...")
        try:
            fallback_loc = page.locator('input[type="text"], input[type="number"], input[id*="code"]').first
            if await fallback_loc.count() > 0:
                await fallback_loc.click(timeout=5000)
                await asyncio.sleep(0.2)
            
            # ИСХОДНЫЙ ФИКС: Пишем через type, имитируя нажатие клавиш для правильного распределения цифр по ячейкам
            await page.keyboard.type(final_code, delay=150)
            print("2FA: код напечатан посимвольно через keyboard.type")
        except Exception as e:
            print(f"2FA: ошибка fallback ввода: {e}")
            await page.keyboard.type(final_code, delay=200)

    await asyncio.sleep(1)

    confirm_btn = page.locator('button').filter(
        has_text=re.compile(r"continue|verify|submit|confirm|подтвердить", re.IGNORECASE)
    ).first
    if await confirm_btn.count() > 0 and await confirm_btn.is_visible():
        await confirm_btn.click(timeout=5000)
        print("2FA: нажали кнопку подтверждения")
    else:
        await page.keyboard.press("Enter")
        print("2FA: нажали Enter для подтверждения")

# ── Adobe Podcast ──────────────────────────────
async def enhance_audio(mp3: Path, user_id: int, notify) -> Path:
    adobe = mp3.parent / (mp3.stem + "_adobe.mp3")
    out   = mp3.parent / (mp3.stem + "_studio.mp3")

    async def shot(path, caption):
        try:
            await page.screenshot(path=path, timeout=10000)
            await notify(path, caption)
        except Exception as e:
            print(f"Ошибка генерации/отправки скриншота: {e}")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True, # Оставляем headless=True для контейнера без GUI
            args=[
                "--no-sandbox",
                "--disable-gpu",
                "--renderer-process-limit=1",
                "--js-flags=--max-old-space-size=250",
                "--disable-blink-features=AutomationControlled"
            ]
        )
        context_args = {
            "viewport": {"width": 1366, "height": 768},
            "locale": "en-US",
            "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        }
        if os.path.exists(STATE_FILE):
            context_args["storage_state"] = STATE_FILE
        ctx = await browser.new_context(**context_args)
        page = await ctx.new_page()

        try:
            await page.goto("https://podcast.adobe.com/enhance", timeout=60000)
            await asyncio.sleep(4)
            await shot("/tmp/step1_opened.png", "🌐 <b>[Шаг 1]</b> Открыл страницу Adobe Enhance. Проверяю сессию...")

            is_logged_in = True
            try:
                sign_btn = page.locator('a, button, span').filter(
                    has_text=re.compile(r"^sign in$|^entrar$|^log in$", re.IGNORECASE)
                ).first
                if await sign_btn.count() > 0 and await sign_btn.is_visible():
                    is_logged_in = False
            except: pass

            if not is_logged_in:
                await shot("/tmp/step2_login.png", "🔑 Сессия не найдена. Нажимаю кнопку входа 'Sign In'...")
                try:
                    await sign_btn.click(force=True, timeout=5000)
                except: pass

                for _ in range(30):
                    await asyncio.sleep(1)
                    if "auth" in page.url or "login" in page.url:
                        break

                for _ in range(20):
                    el = page.locator('input[type="email"], input[name="username"]').first
                    if await el.count() > 0 and await el.is_visible():
                        await el.focus(timeout=5000)
                        await page.keyboard.press("Control+A")
                        await page.keyboard.press("Backspace")
                        await page.keyboard.type(ADOBE_EMAIL.strip(), delay=100)
                        await page.keyboard.press("Enter")
                        break
                    await asyncio.sleep(1)

                await asyncio.sleep(5)
                step = "unknown"
                for _ in range(60):
                    html_page = await page.content()

                    if "verify your identity" in html_page.lower():
                        cb = page.locator('button').filter(
                            has_text=re.compile(r"^Continue$", re.IGNORECASE)
                        ).first
                        if await cb.count() > 0:
                            await cb.click(force=True, timeout=5000)
                            await asyncio.sleep(4)
                            continue

                    if await page.locator('input[type="password"]').count() > 0:
                        step = "password"
                        break

                    has_single_inputs = await page.locator('input[maxlength="1"]').count() >= 2
                    has_otp_input = await page.locator('input[autocomplete="one-time-code"]').count() > 0
                    has_code_context = "code" in html_page.lower() and await page.locator('input[type="text"]').count() > 0

                    if has_single_inputs or has_otp_input or has_code_context:
                        step = "code_2fa"
                        break

                    if "enhance" in page.url and "auth" not in page.url:
                        step = "done"
                        break

                    await asyncio.sleep(1)

                if step == "code_2fa":
                    await shot("/tmp/step3_2fa_waiting.png", "📧 Adobe запросил 2FA-код защиты. Начинаю опрос IMAP почты...")
                    auto_code = await fetch_adobe_code_from_email()

                    if auto_code:
                        await notify(None, f"✅ IMAP успешно перехватил код: <code>{auto_code}</code>. Ввожу на страницу...")
                        final_code = auto_code
                    else:
                        await shot("/tmp/step3_manual_2fa.png", "⚠️ Код не найден автоматически. Пожалуйста, отправьте 6 цифр в ответ:")
                        ev = asyncio.Event()
                        adobe_2fa_state[user_id] = {"event": ev, "code": ""}
                        await asyncio.wait_for(ev.wait(), timeout=180)
                        final_code = adobe_2fa_state[user_id]["code"].strip()

                    await enter_adobe_code(page, final_code)
                    await asyncio.sleep(3)
                    await shot("/tmp/step3_2fa_submitted.png", "📥 Код отправлен. Ожидаю редирект...")

                    for _ in range(30):
                        await asyncio.sleep(1)
                        if "enhance" in page.url:
                            step = "done"
                            break
                        if await page.locator('input[type="password"]').count() > 0:
                            step = "password"
                            break

                if step == "password":
                    await shot("/tmp/step4_password_field.png", "🔑 Страница запроса пароля. Ввожу основной пароль Adobe...")
                    for _ in range(15):
                        el = page.locator('input[type="password"]').first
                        if await el.count() > 0:
                            await el.focus(timeout=5000)
                            await page.keyboard.type(ADOBE_PASSWORD.strip(), delay=100)
                            await asyncio.sleep(1)
                            await page.keyboard.press("Enter")
                            break
                        await asyncio.sleep(1)
                    await asyncio.sleep(8)

                for _ in range(15):
                    if "enhance" in page.url and "auth" not in page.url:
                        break
                    try:
                        await page.evaluate(
                            "const el = [...document.querySelectorAll('button, a')]"
                            ".find(e => /skip|continue/i.test(e.innerText)); if (el) el.click();"
                        )
                    except: pass
                    await asyncio.sleep(1)

                if "enhance" not in page.url:
                    await page.goto("https://podcast.adobe.com/enhance", timeout=60000)
                    await asyncio.sleep(5)

            for _ in range(25):
                if await page.locator('input[type="file"]').count() > 0:
                    break
                await asyncio.sleep(1)

            await ctx.storage_state(path=STATE_FILE)
            await shot("/tmp/step5_upload_ready.png", "⏳ Авторизация завершена! Начинаю заливку аудио на сервера Adobe...")

            # ЗАГРУЗКА ФАЙЛА
            file_input = page.locator('input[type="file"]').first
            await file_input.evaluate("node => node.style.display = 'block'")
            await file_input.set_input_files(str(mp3), timeout=15000)
            await asyncio.sleep(3)

            try:
                await page.evaluate(
                    "const el = [...document.querySelectorAll('button')]"
                    ".find(e => /Enhance speech/i.test(e.innerText)); if (el) el.click();"
                )
            except: pass

            # Ожидание кнопки скачивания
            await shot("/tmp/step6_processing.png", "⚙️ Файл загружен. Adobe запустил нейросети 'Enhance speech'. Жду кнопку скачивания...")
            for _ in range(36):
                await asyncio.sleep(5)
                if await page.evaluate(
                    "const el = [...document.querySelectorAll('button, a')]"
                    ".find(e => /^Download$/i.test(e.innerText?.trim()));"
                    "return el && el.offsetParent !== null;"
                ):
                    break

            await shot("/tmp/step7_download_ready.png", "🎉 Обработка завершена! Скачиваю улучшенный аудиофайл обратно...")
            async with page.expect_download(timeout=120000) as dl_info:
                await page.evaluate(
                    "const el = [...document.querySelectorAll('button, a')]"
                    ".find(e => /^Download$/i.test(e.innerText?.trim())); if (el) el.click();"
                )
            await dl_info.value.save_as(str(adobe))

            await notify(None, "🎵 Файл успешно скачан. Запускаю финальную нормализацию громкости через ffmpeg (loudnorm)...")
            r = await asyncio.create_subprocess_exec(
                "ffmpeg", "-y", "-i", str(adobe),
                "-af", "loudnorm=I=-16:TP=-1.5:LRA=11",
                "-codec:a", "libmp3lame", "-b:a", "128k", "-ar", "44100", "-ac", "1",
                str(out),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            await asyncio.wait_for(r.communicate(), timeout=120)

            return out if out.exists() else adobe

        finally:
            try:
                await ctx.close()
                await browser.close()
            except: pass

# ── mave.digital ───────────────────────────────
async def upload_to_mave(mp3: Path, title: str, desc: str) -> bool:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage"]
        )
        ctx = await browser.new_context(viewport={"width": 1280, "height": 900}, locale="ru-RU")
        page = await ctx.new_page()
        try:
            await page.goto("https://app.mave.digital/login")
            await page.fill('input[type="email"]', MAVE_EMAIL, timeout=5000)
            await page.fill('input[type="password"]', MAVE_PASSWORD, timeout=5000)
            await page.click('button[type="submit"]', timeout=5000)
            await asyncio.sleep(4)
            # (Продолжение логики mave из вашей исходной кодовой базы...)
            return True
        except Exception as e:
            print(f"Ошибка Mave: {e}")
            return False
        finally:
            await ctx.close()
            await browser.close()

# ── Основной обработчик медиа (Голос/Аудио) ─────
async def handle_audio_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    
    if ALLOWED_USER_ID and user_id != ALLOWED_USER_ID:
        await update.message.reply_text("У вас нет доступа к этому боту.")
        return

    # Динамическая функция отправки отчетов скриншотов
    async def telegram_notify(photo_path, caption):
        try:
            if photo_path and os.path.exists(photo_path):
                with open(photo_path, "rb") as photo:
                    await context.bot.send_photo(chat_id=chat_id, photo=photo, caption=caption, parse_mode=ParseMode.HTML)
            else:
                await context.bot.send_message(chat_id=chat_id, text=caption, parse_mode=ParseMode.HTML)
        except Exception as e:
            print(f"Не удалось отправить уведомление в Telegram: {e}")

    status_msg = await update.message.reply_text("📥 Загружаю аудиофайл и конвертирую в нужный формат...")
    
    try:
        src_path = await download_voice(update, context)
        mp3_path = await to_mp3(src_path)
        
        await status_msg.edit_text("🤖 Запускаю автоматизацию Playwright для очистки звука на Adobe...")
        
        # Запуск Adobe с трансляцией скриншотов в этот чат
        clean_mp3 = await enhance_audio(mp3_path, user_id, telegram_notify)
        
        await context
