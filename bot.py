"""
Podcast Bot v13.1 — The Perfect Post
- ИСПРАВЛЕН БАГ ФОРМАТИРОВАНИЯ: Убран лишний html.escape, который ломал жирный шрифт.
- КЛИКАБЕЛЬНЫЕ ССЫЛКИ: Длинные URL спрятаны внутрь текста (гиперссылки).
- ОТЛОЖЕННЫЙ ПОСТИНГ: Бразилия (UTC-3).
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

# ИДЕАЛЬНЫЙ ШАБЛОН: Весь текст жирный (<b>...</b>), ссылки спрятаны в текст (<a href="...">...</a>)
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
    if not EMAIL_USER or not EMAIL_PASS: return None
    def _fetch():
        try:
            mail = imaplib.IMAP4_SSL("imap.gmail.com")
            mail.login(EMAIL_USER, EMAIL_PASS)
            mail.select("inbox")
            status, messages = mail.search(None, '(UNSEEN FROM "account-recovery@adobe.com")')
            mail_ids = messages[0].split()
            if not mail_ids: return None
            status, msg_data = mail.fetch(mail_ids[-1], '(RFC822)')
            for response_part in msg_data:
                if isinstance(response_part, tuple):
                    msg = email.message_from_bytes(response_part[1])
                    body = msg.get_payload(decode=True).decode(errors="ignore") if not msg.is_multipart() else next(p.get_payload(decode=True).decode(errors="ignore") for p in msg.walk() if p.get_content_type() == "text/plain")
                    match = re.search(r'\b\d{6}\b', body)
                    if match: return match.group(0)
        except Exception as e: print(f"Ошибка IMAP: {e}")
        return None

    for _ in range(12):
        code = await asyncio.to_thread(_fetch)
        if code: return code
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
        browser = await pw.chromium.launch(headless=False, args=["--no-sandbox", "--disable-gpu", "--renderer-process-limit=1", "--js-flags=--max-old-space-size=250", "--disable-blink-features=AutomationControlled"])
        context_args = {"viewport": {"width": 1366, "height": 768}, "locale": "en-US", "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
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
                        await el.focus(); await page.keyboard.press("Control+A"); await page.keyboard.press("Backspace"); await page.keyboard
