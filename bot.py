"""
Podcast Bot v2 (DEBUG MODE: Real-time Screenshot Stream)
- Бот шлет скриншоты в чат на каждом этапе
- Стабильная навигация с увеличенными таймаутами
- HTML-дамп на случай падения
"""

import os
import json
import asyncio
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, MessageHandler, filters, ContextTypes
from telegram.constants import ParseMode

from playwright.async_api import async_playwright
from playwright_stealth import Stealth

# Настройки
TELEGRAM_TOKEN     = os.getenv("TELEGRAM_TOKEN")
ADOBE_EMAIL        = os.getenv("ADOBE_EMAIL", "")
ADOBE_PASSWORD     = os.getenv("ADOBE_PASSWORD", "")

# ──────────────────────────────────────────────
# HTTP СЕРВЕР
# ──────────────────────────────────────────────
class DummyHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write("Бот работает. Проверь логи в Telegram".encode('utf-8'))

def run_dummy_server():
    port = int(os.environ.get("PORT", 10000))
    HTTPServer(('0.0.0.0', port), DummyHandler).serve_forever()

# ──────────────────────────────────────────────
# ЛОГИКА ADOBE
# ──────────────────────────────────────────────
async def enhance_audio(mp3_path: Path, update: Update) -> Path:
    adobe_path = mp3_path.parent / (mp3_path.stem + "_adobe.mp3")

    async def snap(name):
        try:
            await page.screenshot(path="live.png")
            await update.message.reply_photo(photo=open("live.png", "rb"), caption=f"Этап: {name}")
            await asyncio.sleep(1)
        except: pass

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=['--no-sandbox'])
        page = await browser.new_page()
        await Stealth().apply_stealth_async(page)
        
        try:
            # 1. Навигация
            print("Adobe: Переход на сайт...")
            await page.goto("https://podcast.adobe.com/enhance", timeout=60000)
            await snap("Сайт открыт")

            # 2. Логин
            if await page.locator('text=Sign in').count() > 0:
                await page.locator('text=Sign in').first.click()
                await snap("Клик Sign in")
                await asyncio.sleep(3)

            # 3. Ввод Email
            email_field = page.locator('input[type="email"], input[name="username"]')
            if await email_field.count() > 0:
                await email_field.fill(ADOBE_EMAIL.strip())
                await snap("Email введен")
                await page.locator('button:has-text("Продолжить"), button:has-text("Continue")').first.click()
                await asyncio.sleep(5)
                await snap("После ввода Email")

            # 4. Пароль
            pwd_field = page.locator('input[type="password"], #password')
            if await pwd_field.count() > 0:
                await pwd_field.fill(ADOBE_PASSWORD.strip())
                await snap("Пароль введен")
                await page.locator('button:has-text("Продолжить"), button:has-text("Continue")').first.click()
                await asyncio.sleep(8)
                await snap("После ввода пароля")

            # 5. Загрузка файла
            target = page.locator('text=/Choose files|Выбрать|Загрузить|Upload/i').first
            if await target.count() > 0:
                async with page.expect_file_chooser() as fc_info:
                    await target.click()
                await (await fc_info.value).set_files(str(mp3_path))
                await snap("Файл загружен")
            
            await asyncio.sleep(15)
            await snap("Ожидание завершения обработки")
            
            # 6. Скачивание
            dl_btn = page.locator('button:has-text("Download"), a:has-text("Скачать")').last
            async with page.expect_download() as dl_info:
                await dl_btn.click()
            await (await dl_info.value).save_as(str(adobe_path))
            
            await browser.close()
            return adobe_path

        except Exception as e:
            # Дамп HTML если упали
            try:
                html = await page.content()
                with open("error.html", "w", encoding="utf-8") as f: f.write(html)
            except: pass
            raise RuntimeError(f"Adobe ошибка: {str(e)[:100]}")

# ──────────────────────────────────────────────
# БОТ
# ──────────────────────────────────────────────
async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("⏳ Начинаю процесс...")
    try:
        # Получаем файл
        file = await update.message.voice.get_file()
        await file.download_to_drive("input.ogg")
        subprocess.run(["ffmpeg", "-y", "-i", "input.ogg", "input.mp3"])
        
        # Запуск Adobe
        studio_mp3 = await enhance_audio(Path("input.mp3"), update)
        
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
