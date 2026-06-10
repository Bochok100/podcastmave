"""
Podcast Bot v2 (DEBUG MODE: Full Screenshot Stream)
- Бот делает скриншот на КАЖДОМ шаге авторизации и шлет его в чат
- Короткие таймауты
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
from playwright_stealth import Stealth

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
STYLE_PROMPT = "Ты — редактор подкаста. Сделай заголовок и описание."

pending = {}
adobe_2fa_state = {}

# ──────────────────────────────────────────────
# HTTP СЕРВЕР (ДЛЯ ОБХОДА RENDER SLEEP)
# ──────────────────────────────────────────────
class DummyHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is alive!")

def run_dummy_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(('0.0.0.0', port), DummyHandler)
    server.serve_forever()

# ──────────────────────────────────────────────
# ОСНОВНАЯ ЛОГИКА С ОТПРАВКОЙ СКРИНОВ
# ──────────────────────────────────────────────
async def enhance_audio(mp3_path: Path, user_id: int, update: Update) -> Path:
    adobe_path = mp3_path.parent / (mp3_path.stem + "_adobe.mp3")

    async def snap(step_name):
        try:
            path = f"debug_{step_name}.png"
            await page.screenshot(path=path)
            await update.message.reply_photo(photo=open(path, "rb"), caption=f"🔍 Шаг: {step_name}")
            await asyncio.sleep(1) # Небольшая пауза чтобы не спамить
        except Exception as e:
            print(f"Ошибка отправки скрина: {e}")

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=False, args=['--no-sandbox'])
            page = await browser.new_page()
            await Stealth().apply_stealth_async(page)
            
            await page.goto("https://podcast.adobe.com/enhance")
            await snap("Загрузка сайта")

            # Авторизация
            if await page.locator('text=Sign in').count() > 0:
                await page.locator('text=Sign in').first.click()
                await snap("Клик Sign in")

            email_field = page.locator('input[type="email"], input[name="username"]')
            if await email_field.count() > 0:
                await email_field.fill(ADOBE_EMAIL.strip())
                await snap("Ввод почты")
                await page.locator('button:has-text("Продолжить"), button:has-text("Continue")').first.click()
                await snap("Клик Продолжить после почты")
                await asyncio.sleep(5)

            pwd_field = page.locator('input[type="password"], #password')
            if await pwd_field.count() > 0:
                await pwd_field.fill(ADOBE_PASSWORD.strip())
                await snap("Ввод пароля")
                await page.locator('button:has-text("Продолжить"), button:has-text("Continue")').first.click()
                await snap("Клик Продолжить после пароля")
                await asyncio.sleep(5)

            # Загрузка
            await snap("Экран перед загрузкой")
            target = page.locator('text=/Choose files|Выбрать|Загрузить|Upload/i').first
            if await target.count() > 0:
                async with page.expect_file_chooser() as fc_info:
                    await target.click()
                await (await fc_info.value).set_files(str(mp3_path))
                await snap("Файл выбран")
            
            await asyncio.sleep(10)
            await snap("После загрузки (ждем Enhance)")
            
            # Скачивание
            dl_btn = page.locator('button:has-text("Download"), a:has-text("Скачать")').last
            async with page.expect_download() as dl_info:
                await dl_btn.click()
            await (await dl_info.value).save_as(str(adobe_path))
            
            await browser.close()
            return adobe_path

    except Exception as e:
        raise RuntimeError(f"Adobe: {str(e)[:100]}")

# ──────────────────────────────────────────────
# TELEGRAM БОТ
# ──────────────────────────────────────────────
async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("⏳ Начинаю поток скриншотов...")
    try:
        # download and convert (оставляем старое)
        ogg = await update.message.voice.get_file()
        await ogg.download_to_drive("input.ogg")
        subprocess.run(["ffmpeg", "-y", "-i", "input.ogg", "input.mp3"])
        
        studio_mp3 = await enhance_audio(Path("input.mp3"), update.effective_user.id, update)
        
        await update.message.reply_document(document=open(studio_mp3, "rb"))
        await msg.delete()
    except Exception as e:
        await msg.edit_text(f"❌ Ошибка: {e}")

def main():
    threading.Thread(target=run_dummy_server, daemon=True).start()
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_voice))
    app.run_polling()

if __name__ == "__main__":
    main()
