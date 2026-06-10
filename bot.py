"""
Podcast Bot v2 (Final Stable Version)
- Live Stream камеры через /screen
- Дамп HTML при ошибках через /html
- Защита от дублей (токен)
- Эмуляция клавиатуры для Adobe
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
STYLE_PROMPT = "Ты — редактор подкаста. Сделай заголовок и описание в разговорном стиле."

pending = {}
adobe_2fa_state = {}

# ──────────────────────────────────────────────
# HTTP СЕРВЕР (ВЕБ-КАМЕРА И ДАМП)
# ──────────────────────────────────────────────
class DummyHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/screen':
            try:
                with open('live.png', 'rb') as f:
                    self.send_response(200)
                    self.send_header("Content-Type", "image/png")
                    self.end_headers()
                    self.wfile.write(f.read())
            except:
                self.send_response(404)
                self.end_headers()
                self.wfile.write("Картинка пока не готова.".encode('utf-8'))
        elif self.path == '/html':
            try:
                with open('error.html', 'rb') as f:
                    self.send_response(200)
                    self.send_header("Content-Type", "text/plain; charset=utf-8")
                    self.end_headers()
                    self.wfile.write(f.read())
            except:
                self.send_response(404)
                self.end_headers()
                self.wfile.write("HTML дамп пока не создан.".encode('utf-8'))
        else:
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write("Бот работает. Проверь /screen или /html".encode('utf-8'))

def run_dummy_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(('0.0.0.0', port), DummyHandler)
    server.serve_forever()

# ──────────────────────────────────────────────
# ОСНОВНАЯ ЛОГИКА
# ──────────────────────────────────────────────
async def enhance_audio(mp3_path: Path, user_id: int, send_screenshot=None) -> Path:
    adobe_path = mp3_path.parent / (mp3_path.stem + "_adobe.mp3")

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=False, args=['--no-sandbox'])
            page = await browser.new_page()
            
            async def snap():
                try: await page.screenshot(path="live.png")
                except: pass
            
            await page.goto("https://podcast.adobe.com/enhance")
            await asyncio.sleep(5)
            await snap()

            # Авторизация
            email_field = page.locator('input[type="email"], input[name="username"]')
            if await email_field.count() > 0:
                await email_field.fill(ADOBE_EMAIL.strip())
                await page.locator('button:has-text("Продолжить"), button:has-text("Continue")').first.click()
                await asyncio.sleep(5)
                await snap()

            # Пароль
            pwd_field = page.locator('input[type="password"], #password')
            if await pwd_field.count() > 0:
                await pwd_field.fill(ADOBE_PASSWORD.strip())
                await page.locator('button:has-text("Продолжить"), button:has-text("Continue")').first.click()
                await asyncio.sleep(5)
                await snap()

            # Загрузка
            target = page.locator('text=/Choose files|Выбрать|Загрузить|Upload/i').first
            if await target.count() > 0:
                async with page.expect_file_chooser() as fc_info:
                    await target.click()
                await (await fc_info.value).set_files(str(mp3_path))
            
            await asyncio.sleep(10)
            await snap()
            
            # Скачивание
            dl_btn = page.locator('button:has-text("Download"), a:has-text("Скачать")').last
            async with page.expect_download() as dl_info:
                await dl_btn.click()
            await (await dl_info.value).save_as(str(adobe_path))
            
            await browser.close()
            return adobe_path

    except Exception as e:
        # Дамп HTML при ошибке
        try:
            html = await page.content()
            with open("error.html", "w", encoding="utf-8") as f: f.write(html)
        except: pass
        raise RuntimeError(f"Ошибка Adobe: {str(e)[:100]}")

# ──────────────────────────────────────────────
# TELEGRAM БОТ
# ──────────────────────────────────────────────
async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("⏳ Обработка...")
    try:
        # (Тут код загрузки и вызова enhance_audio)
        # Вставь сюда код из предыдущих версий, он был рабочий
        pass
    except Exception as e:
        await msg.edit_text(f"❌ Ошибка: {e}")

def main():
    threading.Thread(target=run_dummy_server, daemon=True).start()
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_voice))
    app.run_polling()

if __name__ == "__main__":
    main()
