"""
Podcast Bot v2 (Stabilized Visual Auth)
- Визуальный поиск кнопок (решение проблемы белого экрана)
- Iframe-загрузчик (самый надежный способ)
"""

import os
import json
import asyncio
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes
from playwright.async_api import async_playwright
from playwright_stealth import Stealth

# Настройки
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
ADOBE_EMAIL    = os.getenv("ADOBE_EMAIL", "")
ADOBE_PASSWORD = os.getenv("ADOBE_PASSWORD", "")

# ──────────────────────────────────────────────
# ЛОГИКА ADOBE (Тот самый обход белого экрана)
# ──────────────────────────────────────────────
async def enhance_audio(mp3_path: Path) -> Path:
    adobe_path = mp3_path.parent / (mp3_path.stem + "_adobe.mp3")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=['--no-sandbox'])
        context = await browser.new_context(viewport={"width": 1366, "height": 768})
        page = await context.new_page()
        await Stealth().apply_stealth_async(page)
        
        # 1. Заход и ожидание прогрузки
        await page.goto("https://podcast.adobe.com/enhance", timeout=60000)
        await asyncio.sleep(5)

        # 2. Визуальный поиск кнопки Sign in (обход "белого экрана")
        await page.evaluate("""() => {
            const btns = Array.from(document.querySelectorAll('a, button'));
            const signIn = btns.find(el => el.innerText && el.innerText.trim().toLowerCase() === 'sign in');
            if (signIn) signIn.click();
        }""")
        await asyncio.sleep(5)

        # 3. Ввод данных
        email_field = page.locator('input[type="email"], input[name="username"]')
        if await email_field.count() > 0:
            await email_field.fill(ADOBE_EMAIL.strip())
            await page.locator('button:has-text("Продолжить"), button:has-text("Continue")').first.click()
            await asyncio.sleep(5)

        pwd_field = page.locator('input[type="password"], #password')
        if await pwd_field.count() > 0:
            await pwd_field.fill(ADOBE_PASSWORD.strip())
            await page.locator('button:has-text("Продолжить"), button:has-text("Continue")').first.click()
            await asyncio.sleep(5)

        # 4. Загрузка через поиск скрытого input в ифреймах
        uploaded = False
        for frame in page.frames:
            input_file = frame.locator('input[type="file"]')
            if await input_file.count() > 0:
                await input_file.set_input_files(str(mp3_path))
                uploaded = True
                break
        
        if not uploaded:
            raise RuntimeError("Не удалось найти поле загрузки")

        await asyncio.sleep(15) # Ждем обработку

        # 5. Скачивание
        dl_btn = page.locator('button:has-text("Download"), a:has-text("Скачать")').last
        async with page.expect_download() as dl_info:
            await dl_btn.click()
        await (await dl_info.value).save_as(str(adobe_path))
        
        await browser.close()
        return adobe_path

# ──────────────────────────────────────────────
# БОТ
# ──────────────────────────────────────────────
async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("⏳ Начинаю...")
    try:
        file = await update.message.voice.get_file()
        await file.download_to_drive("input.ogg")
        subprocess.run(["ffmpeg", "-y", "-i", "input.ogg", "input.mp3"])
        
        studio_mp3 = await enhance_audio(Path("input.mp3"))
        await update.message.reply_document(document=open(studio_mp3, "rb"))
        await msg.delete()
    except Exception as e:
        await msg.edit_text(f"❌ Ошибка: {e}")

def main():
    # Запуск сервера просто для того, чтобы Render не убил процесс
    threading.Thread(target=lambda: HTTPServer(('0.0.0.0', int(os.getenv("PORT", 10000))), 
                 BaseHTTPRequestHandler).serve_forever(), daemon=True).start()
    
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_voice))
    app.run_polling()

if __name__ == "__main__":
    main()
