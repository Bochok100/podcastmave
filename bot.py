"""
Podcast Bot v13.2 (Cleaned)
"""

import os
import asyncio
import tempfile
import time
import re
import imaplib
import email
import html
import datetime
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

# Безопасное получение ALLOWED_USER_ID
try:
    ALLOWED_USER_ID = int(os.getenv("ALLOWED_USER_ID", "0"))
except:
    ALLOWED_USER_ID = 0

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

            status, messages = mail.search(None, '(FROM "message@adobe.com")')
            mail_ids = messages[0].split()
            if not mail_ids:
                print("IMAP: писем от message@adobe.com не найдено")
                return None

            status, msg_data = mail.fetch(mail_ids[-1], '(RFC822)')
            for response_part in msg_data:
                if not isinstance(response_part, tuple):
                    continue
                msg = email.message_from_bytes(response_part[1])

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

                match = re.search(r'\b\d{6}\b', body)
                if match:
                    return match.group(0)
        except Exception as e:
            print(f"Ошибка IMAP: {e}")
        return None

    for attempt in range(18):
        code = await asyncio.to_thread(_fetch)
        if code:
            return code
        await asyncio.sleep(10)
    return None

# ── Утилиты ────────────────────────────────────
async def download_voice(update, context) -> Path:
    voice = update.message.voice or update.message.audio
    f = await context.bot.get_file(voice.file_id)
    fd, path = tempfile.mkstemp(suffix=".ogg")
    os.close(fd)
    await f.download_to_drive(path)
    return Path(path)

async def to_mp3(src: Path) -> Path:
    dst = src.with_suffix(".mp3")
    proc = await asyncio.create_subprocess_exec("ffmpeg", "-y", "-i", str(src), "-codec:a", "libmp3lame", "-b:a", "128k", "-ar", "44100", "-ac", "1", str(dst), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    await asyncio.wait_for(proc.communicate(), timeout=120)
    if not dst.exists() or dst.stat().st_size < 1000: raise RuntimeError("Ошибка ffmpeg")
    return dst

# ── Adobe Podcast ──────────────────────────────
async def enhance_audio(mp3: Path, user_id: int, notify) -> Path:
    adobe = mp3.parent / (mp3.stem + "_adobe.mp3")
    out   = mp3.parent / (mp3.stem + "_studio.mp3")

    async def shot(path, caption):
        try: await page.screenshot(path=path, timeout=10000); await notify(path, caption)
        except: pass

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, args=["--no-sandbox", "--disable-gpu"])
        context_args = {"viewport": {"width": 1366, "height": 768}, "locale": "en-US"}
        if os.path.exists(STATE_FILE): context_args["storage_state"] = STATE_FILE
        ctx = await browser.new_context(**context_args)
        page = await ctx.new_page()

        try:
            await page.goto("https://podcast.adobe.com/enhance", timeout=60000)
            await asyncio.sleep(4)
            is_logged_in = True
            try:
                sign_btn = page.locator('a, button, span').filter(has_text=re.compile(r"^sign in$|^entrar$|^log in$", re.IGNORECASE)).first
                if await sign_btn.count() > 0 and await sign_btn.is_visible(): is_logged_in = False
            except: pass

            if not is_logged_in:
                try: await sign_btn.click(force=True)
                except: pass

                for _ in range(30):
                    await asyncio.sleep(1)
                    if "auth" in page.url or "login" in page.url: break

                for _ in range(20):
                    el = page.locator('input[type="email"], input[name="username"]').first
                    if await el.count() > 0 and await el.is_visible():
                        await el.focus(); await page.keyboard.press("Control+A"); await page.keyboard.press("Backspace"); await page.keyboard.type(ADOBE_EMAIL.strip(), delay=100); await page.keyboard.press("Enter")
                        break
                    await asyncio.sleep(1)

                await asyncio.sleep(5)
                step = "unknown"
                for _ in range(60):
                    html_page = await page.content()
                    if "verify your identity" in html_page.lower():
                        cb = page.locator('button').filter(has_text=re.compile(r"^Continue$", re.IGNORECASE)).first
                        if await cb.count() > 0: await cb.click(force=True); await asyncio.sleep(4); continue
                    if await page.locator('input[type="password"]').count() > 0: step = "password"; break
                    if await page.locator('input[type="text"]').count() > 0 and "code" in html_page.lower(): step = "code_2fa"; break
                    if "enhance" in page.url and "auth" not in page.url: step = "done"; break
                    await asyncio.sleep(1)

                if step == "code_2fa":
                    await notify(None, "📧 Проверяю Gmail на наличие кода...")
                    auto_code = await fetch_adobe_code_from_email()
                    if auto_code:
                        await notify(None, f"✅ Нашел код: {auto_code}")
                        final_code = auto_code
                    else:
                        await notify(None, "⚠️ Письмо не пришло. Пришли код вручную:")
                        ev = asyncio.Event()
                        adobe_2fa_state[user_id] = {"event": ev, "code": ""}
                        await asyncio.wait_for(ev.wait(), timeout=180)
                        final_code = adobe_2fa_state[user_id]["code"].strip()

                    try: await page.evaluate("document.querySelector('input[type=\"text\"]').focus();")
                    except: pass
                    await page.keyboard.type(final_code, delay=200)
                    await asyncio.sleep(2)
                    await page.keyboard.press("Enter")

                    for _ in range(30):
                        await asyncio.sleep(1)
                        if "enhance" in page.url: step = "done"; break
                        if await page.locator('input[type="password"]').count() > 0: step = "password"; break

                if step == "password":
                    for _ in range(15):
                        el = page.locator('input[type="password"]').first
                        if await el.count() > 0:
                            await el.focus(); await page.keyboard.type(ADOBE_PASSWORD.strip(), delay=100); await asyncio.sleep(1); await page.keyboard.press("Enter")
                            break
                        await asyncio.sleep(1)
                    await asyncio.sleep(8)

                for _ in range(15):
                    if "enhance" in page.url and "auth" not in page.url: break
                    try: await page.evaluate("const el = [...document.querySelectorAll('button, a')].find(e => /skip|continue/i.test(e.innerText)); if (el) el.click();")
                    except: pass
                    await asyncio.sleep(1)

                if "enhance" not in page.url: await page.goto("https://podcast.adobe.com/enhance", timeout=60000); await asyncio.sleep(5)

            for _ in range(25):
                if await page.locator('input[type="file"]').count() > 0: break
                await asyncio.sleep(1)

            await ctx.storage_state(path=STATE_FILE)

            # ЗАГРУЗКА
            file_input = page.locator('input[type="file"]').first
            await file_input.evaluate("node => node.style.display = 'block'")
            await file_input.set_input_files(str(mp3), timeout=15000)
            await asyncio.sleep(3)

            try: await page.evaluate("const el = [...document.querySelectorAll('button')].find(e => /Enhance speech/i.test(e.innerText)); if (el) el.click();")
            except: pass

            for _ in range(36):
                await asyncio.sleep(5)
                if await page.evaluate("const el = [...document.querySelectorAll('button, a')].find(e => /^Download$/i.test(e.innerText?.trim())); return el && el.offsetParent !== null;"): break

            async with page.expect_download(timeout=120000) as dl_info:
                await page.evaluate("const el = [...document.querySelectorAll('button, a')].find(e => /^Download$/i.test(e.innerText?.trim())); if (el) el.click();")
            await dl_info.value.save_as(str(adobe))

            r = await asyncio.create_subprocess_exec("ffmpeg", "-y", "-i", str(adobe), "-af", "loudnorm=I=-16:TP=-1.5:LRA=11", "-codec:a", "libmp3lame", "-b:a", "128k", "-ar", "44100", "-ac", "1", str(out), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            await asyncio.wait_for(r.communicate(), timeout=120)

            return out if out.exists() else adobe
        finally:
            try: await ctx.close(); await browser.close()
            except: pass

# ── mave.digital ───────────────────────────────
async def upload_to_mave(mp3: Path, title: str, desc: str) -> bool:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, args=["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage"])
        ctx = await browser.new_context(viewport={"width": 1280, "height": 900}, locale="ru-RU")
        page
