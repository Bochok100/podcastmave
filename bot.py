"""
Podcast Bot v13.5
- ИСПРАВЛЕНО ЗАВИСАНИЕ (добавлены таймауты на все клики Playwright)
- ИСПРАВЛЕН IMAP: добавлен сокет-таймаут против вечных зависаний сети
- Улучшен универсальный ввод OTP через клавиатуру
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
            mail = imaplib.IMAP4
