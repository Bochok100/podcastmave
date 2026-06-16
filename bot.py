"""
Podcast Bot v13.4
- ИСПРАВЛЕН IMAP: проверка свежести письма (не старше 10 минут)
- ИСПРАВЛЕНЫ отступы в _fetch
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

# ── Planer (Brazil UTC-3) ──────────────────────
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

# ── IMAP Mail ──────────────────────────────────
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
            now_utc = datetime.datetime.now(datetime.timezone.utc)
            for mid in reversed(mail_ids[-3:]):
                status, msg_data = mail.fetch(mid, '(RFC822)')
                for response_part in msg_data:
                    if not isinstance(response_part, tuple):
                        continue
                    msg = email.message_from_bytes(response_part[1])
                    # Проверяем свежесть письма
                    date_str = msg.get("Date", "")
                    try:
                        msg_date = parsedate_to_datetime(date_str)
                        age_minutes = (now_utc - msg_date).total_seconds() / 60
                        print(f"IMAP: письмо от {date_str}, возраст {age_minutes:.1f} мин")
                        if age_minutes > 10:
                            print(f"IMAP: письмо слишком старое ({age_minutes:.1f} мин), пропускаем")
                            continue
                    except Exception as e:
                        print(f"IMAP: не смогли распарсить дату: {e}")
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
                        print(f"IMAP: найден свежий код {match.group(0)}")
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

# ── Utilities ──────────────────────────────────
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
