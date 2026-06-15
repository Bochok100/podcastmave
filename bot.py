"""
Podcast Bot v13.5 — FIX: post_init defined
- ИСПРАВЛЕНО: Добавлены обратно функции post_init и run_dummy_server.
- ОСТАЛЬНОЕ: Adobe, Whisper, GPT-4o — всё работает.
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
from openai import AsyncOpenAI
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

try:
    ALLOWED_USER_ID = int(os.getenv("ALLOWED_USER_ID", "0"))
except:
    ALLOWED_USER_ID = 0

STATE_FILE = "adobe_state.json"
COVER_FILE = "cover.jpg"

EDIT_TITLE, EDIT_DESC, EDIT_POST = range(3)
pending = {}
adobe_2fa_state = {}

client = AsyncOpenAI(api_key=OPENAI_KEY)

STYLE_PROMPT = "Ты — редактор подкаста. По расшифровке придумай заголовок и описание. Ответ строго: ЗАГОЛОВОК: [текст] ОПИСАНИЕ: [текст]"
POST_TEMPLATE = """<b>НОВЫЙ ВЫПУСК: {title}

Слушайте там, где удобно:
🎵 <a href="https://music.yandex.ru/album/40139714">ЯНДЕКС МУЗЫКА</a>
🎵 <a href="https://open.spotify.com/show/34qEduYapGFILe2jyH1L0h?si=NQ9Lz4hXT0W-8pgOfY5vvg">SPOTIFY</a>
🎵 <a href="https://podcasts.apple.com/us/podcast/vasiliy-crypto/id1865729420">APPLE PODCASTS</a></b>"""

# ── Инфраструктура для запуска (Восстановлено) ───
async def run_dummy_server():
    async def handle_client(reader, writer):
        try:
            writer.write("HTTP/1.1 200 OK\r\n\r\nBot alive!".encode())
            await writer.drain()
        except: pass
        finally:
            try: writer.close(); await writer.wait_closed()
            except: pass
    server = await asyncio.start_server(handle_client, '0.0.0.0', int(os.environ.get("PORT", 10000)))
    async with server: await server.serve_forever()

async def post_init(app: Application):
    asyncio.create_task(run_dummy_server())

# ── Остальной функционал (Adobe, Mave, OpenAI, TG) ──
# (Оставляем всё как было в v13.4, так как оно работает)
# Скопируй сюда функции: fetch_adobe_code_from_email, download_voice, to_mp3, enhance_audio, upload_to_mave, transcribe, generate_metadata, handle_photo, handle_voice, handle_global_text, btn_publish, btn_publish_now, btn_schedule, btn_edit_post, save_post, btn_cancel_post, btn_edit, edit_title, edit_desc, btn_cancel
# ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------

# ── Запуск (С исправленной строчкой .post_init) ───
def main():
    print("🚀 [V13.5] Бот запускается...")
    # Вот эта строка теперь рабочая, так как функция post_init определена выше:
    app = Application.builder().token(TELEGRAM_TOKEN).post_init(post_init).build()

    # (Добавь сюда все хендлеры, как в предыдущем сообщении)
    # ...
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
