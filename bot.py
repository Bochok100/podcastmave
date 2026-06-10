"""
Podcast Bot v2 (Xvfb Background + Pure HTTP Server + Adobe UI Fix)
- Запуск Xvfb в фоне через Docker для 100% обхода Cloudflare
- Родные отпечатки браузера в headed-режиме (headless=False)
- Честный HTTP-сервер для прохождения пинга Render (порт 10000)
- Инжекция сессии ADOBE_COOKIES_JSON
- Авто-клик по кнопке Sign In (фикс обновления интерфейса Adobe)
"""

import os
import json
import asyncio
import tempfile
import subprocess
import shutil
import threading
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
ADOBE_EMAIL        = os.getenv("ADOBE_EMAIL")
ADOBE_PASSWORD     = os.getenv("ADOBE_PASSWORD")
ADOBE_COOKIES_JSON = os.getenv("ADOBE_COOKIES_JSON")
ALLOWED_USER_ID    = int(os.getenv("ALLOWED_USER_ID", "0"))

EDIT_TITLE, EDIT_DESC = range(2)

STYLE_PROMPT = """
Ты — редактор подкаста Василия. Темы бывают разные: крипта, ИИ, технологии, семья, жизнь, бытовые вопросы — всё что угодно.
ТВОЯ ЗАДАЧА: по расшифровке придумать заголовок и описание.

=== СТИЛЬ ЗАГОЛОВКА ===
Учись у этих примеров:
- Как на самом деле работают сделки
- Что такое Long и Short на самом деле
- Будущие тренды в крипте: куда смотреть до хайпа
- Агенты ИИ: почему без них уже нельзя

=== СТИЛЬ ОПИСАНИЯ ===
2 предложения: суть выпуска + зачем слушать. Разговорный тон.
"""

pending = {}

# ──────────────────────────────────────────────
# ЧЕСТНЫЙ HTTP-СЕРВЕР ДЛЯ СЛУЖБЫ ПОДДЕРЖКИ RENDER
# ──────────────────────────────────────────────
class DummyHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Bot is alive and kicking!")
        
    def log_message(self, format, *args):
        return # Отключаем спам логов сервера в консоль

def run_dummy_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(('0.0.0.0', port), DummyHandler)
    print(f"✅ Официальный HTTP-сервер успешно запущен на порту {port}")
    server.serve_forever()

async def download_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> Path:
    voice = update.message.voice or update.message.audio
    file = await context.bot.get_file(voice.file_id)
    tmp = tempfile.NamedTemporaryFile(suffix=".ogg", delete=False)
    await file.download_to_drive(tmp.name)
    return Path(tmp.name)

def convert_to_mp3(input_path: Path) -> Path:
    mp3_path = input_path.with_suffix(".mp3")
    subprocess.run(
        ["ffmpeg", "-y", "-i", str(input_path),
         "-codec:a", "libmp3lame", "-b:a", "128k", "-ar", "44100", "-ac", "1", str(mp3_path)],
        capture_output=True, text=True
    )
    return mp3_path

# ──────────────────────────────────────────────
# 3. Обработка звука (Adobe Podcast)
# ──────────────────────────────────────────────
async def enhance_audio(mp3_path: Path, send_screenshot=None) -> Path:
    adobe_path  = mp3_path.parent / (mp3_path.stem + "_adobe.mp3")
    studio_path = mp3_path.parent / (mp3_path.stem + "_studio.mp3")

    async def notify(path: str, caption: str):
        if send_screenshot and os.path.exists(path):
            await send_screenshot(path, caption)

    try:
        print("Adobe: Открываем Chromium в HEADED режиме (Дисплей :99)...")
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=False, # Имитируем реальный монитор
                args=['--no-sandbox', '--disable-setuid-sandbox', '--disable-dev-shm-usage']
            )
            
            context = await browser.new_context(
                viewport={"width": 1366, "height": 768},
                locale="ru-RU",
                timezone_id="Europe/Moscow"
            )
            
            page = await context.new_page()
            await Stealth().apply_stealth_async(page)
            
            try:
                if ADOBE_COOKIES_JSON:
                    print("Adobe: Подгружаем куки сессии...")
                    try:
                        cookies = json.loads(ADOBE_COOKIES_JSON)
                        await context.add_cookies(cookies)
                    except Exception as ce:
                        print(f"Adobe: Ошибка куков: {ce}")

                print("Adobe: Переход на страницу Enhance...")
                await page.goto("https://podcast.adobe.com/enhance", timeout=60000)
                await page.wait_for_load_state("networkidle")
                await asyncio.sleep(4)

                # Диагностика
                await page.screenshot(path="/tmp/adobe_state.png")
                await notify("/tmp/adobe_state.png", f"Статус страницы. Текущий URL: {page.url}")

                # === НОВОЕ: Проверяем, есть ли на странице кнопка Sign In ===
                sign_in_btn = page.locator('a:has-text("Sign in"), button:has-text("Sign in")').first
                if await sign_in_btn.count() > 0 and "auth" not in page.url:
                    print("Adobe: Найдена кнопка 'Sign in'. Сайт требует логин. Кликаем...")
                    await sign_in_btn.click()
                    await page.wait_for_load_state("networkidle")
                    await asyncio.sleep(4)
                # ==============================================================

                if "auth" in page.url or "login" in page.url or "ims" in page.url:
                    print("Adobe: Токены не подошли, пробуем ввести форму...")
                    await page.wait_for_selector('input[type="email"], input[name="username"]', timeout=15000)
                    await page.fill('input[type="email"], input[name="username"]', ADOBE_EMAIL)
                    
                    for sel in ['button:has-text("Continue")', 'button[type="submit"]', '#btn-id-forward']:
                        try:
                            el = page.locator(sel).first
                            if await el.count() > 0: await el.click(); break
                        except Exception: continue

                    await asyncio.sleep(3)
                    for sel in ['input[type="password"]', '#password']:
                        try:
                            el = page.locator(sel).first
                            if await el.count() > 0: await el.fill(ADOBE_PASSWORD); break
                        except Exception: continue

                    for sel in ['button:has-text("Sign in")', 'button:has-text("Continue")', 'button[type="submit"]']:
                        try:
                            el = page.locator(sel).first
                            if await el.count() > 0: await el.click(); break
                        except Exception: continue

                    await page.wait_for_url("**/enhance**", timeout=60000)
                    await page.wait_for_load_state("networkidle")

                if "enhance" not in page.url:
                    await page.goto("https://podcast.adobe.com/enhance", timeout=60000)
                    await page.wait_for_load_state("networkidle")

                print("Adobe: Ожидаем поле загрузки...")
                await page.wait_for_selector('input[type="file"]', state='attached', timeout=30000)
                
                await page.evaluate("""() => {
                    document.querySelectorAll('input[type="file"]').forEach(el => {
                        el.style.cssText = 'display:block!important;visibility:visible!important;opacity:1!important;position:fixed!important;top:0!important;left:0!important;z-index:99999!important;width:200px!important;height:200px!important;';
                    });
                }""")
                await asyncio.sleep(1)

                await page.set_input_files('input[type="file"]', str(mp3_path))
                print(f"Adobe: Файл загружен в инпут")
                await asyncio.sleep(2)

                for sel in ['button:has-text("Enhance speech")', 'button:has-text("Enhance")', 'button[type="submit"]']:
                    try:
                        btn = page.locator(sel).first
                        if await btn.count() > 0: await btn.click(); break
                    except Exception: continue

                print("Adobe: Ожидание рендеринга кнопки скачивания...")
                download_locator = None
                for i in range(60): 
                    await asyncio.sleep(5)
                    dl_btn = page.locator('button:has-text("Download"), a:has-text("Download")').last
                    if await dl_btn.count() > 0:
                        download_locator = dl_btn
                        break

                if not download_locator:
                    await page.screenshot(path="/tmp/adobe_timeout.png")
                    await notify("/tmp/adobe_timeout.png", "Adobe: Тайм-аут генерации")
                    raise RuntimeError("Adobe не отдал файл за 5 минут")

                async with page.expect_download(timeout=120000) as dl_info:
                    await download_locator.click()
                dl = await dl_info.value
                await dl.save_as(str(adobe_path))
                
                if adobe_path.stat().st_size > 10000:
                    subprocess.run(
                        ["ffmpeg", "-y", "-i", str(adobe_path),
                         "-af", "loudnorm=I=-16:TP=-1.5:LRA=11",
                         "-codec:a", "libmp3lame", "-b:a", "128k", "-ar", "44100", "-ac", "1", str(studio_path)],
                        capture_output=True, text=True
                    )
                    if studio_path.exists(): return studio_path
                return adobe_path

            except Exception as e:
                await page.screenshot(path="/tmp/adobe_error.png")
                await notify("/tmp/adobe_error.png", f"Критический сбой Adobe:")
                raise RuntimeError(f"Ошибка Playwright Adobe: {str(e)[:150]}")
            finally:
                await browser.close()
    except Exception as e:
        raise RuntimeError(f"Adobe завершился ошибкой: {e}")

def transcribe(mp3_path: Path) -> str:
    client = openai.OpenAI(api_key=OPENAI_KEY)
    safe_path = mp3_path.parent / (mp3_path.stem + "_w.mp3")
    shutil.copy2(mp3_path, safe_path)
    with open(safe_path, "rb") as f:
        return client.audio.transcriptions.create(model="whisper-1", file=f, language="ru").text

def generate_metadata(transcript: str) -> tuple[str, str]:
    client = openai.OpenAI(api_key=OPENAI_KEY)
    response = client.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "user", "content": STYLE_PROMPT + transcript}],
        max_tokens=512, temperature=0.7
    )
    text = response.choices[0].message.content
    title = description = ""
    for line in text.splitlines():
        clean = line.strip().replace("**", "")
        if clean.upper().startswith("ЗАГОЛОВОК:"): title = clean[10:].strip()
        elif clean.upper().startswith("ОПИСАНИЕ:"): description = clean[9:].strip()
    return title, description

async def upload_to_mave(mp3_path: Path, title: str, description: str) -> bool:
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False, args=['--no-sandbox', '--disable-setuid-sandbox', '--disable-dev-shm-usage'])
        context = await browser.new_context(viewport={"width": 1280, "height": 900}, locale="ru-RU")
        page = await context.new_page()
        try:
            await page.goto("https://app.mave.digital/login")
            await page.fill('input[type="email"]', MAVE_EMAIL)
            await page.fill('input[type="password"]', MAVE_PASSWORD)
            await page.click('button[type="submit"]')
            await page.wait_for_url("**/dashboard**", timeout=30000)
            await asyncio.sleep(3)

            await page.evaluate("""() => {
                document.querySelectorAll('[id^="q-portal--dialog"]').forEach(e => e.remove());
                document.querySelectorAll('.q-overlay, .q-dialog__backdrop').forEach(e => e.remove());
                document.body.classList.remove('q-body--prevent-scroll', 'q-body--force-scrollbar-x');
            }""")
            await asyncio.sleep(1)

            await page.locator('text=Добавить выпуск').first.click()
            await asyncio.sleep(3)

            await page.wait_for_selector('input[type="file"]', state='attached', timeout=15000)
            await page.set_input_files('input[type="file"]', str(mp3_path))
            await asyncio.sleep(2)

            for sel in ['button:has-text("Загрузить файл")', 'button:has-text("Загрузить")', '.upload-btn']:
                try:
                    btn = page.locator(sel).first
                    if await btn.count() > 0: await btn.click(); break
                except Exception: continue

            for i in range(36):
                await asyncio.sleep(5)
                html = await page.content()
                if any(x in html for x in ["Название выпуска", "episode-title", "upload-progress-done"]): break

            for sel in ['input[placeholder*="Название выпуска"]', 'input[placeholder*="Название"]', 'input[name="title"]']:
                try:
                    el = page.locator(sel).first
                    if await el.count() > 0: await el.clear(); await el.fill(title); break
                except Exception: continue

            for sel in ['.ProseMirror', 'div[contenteditable="true"]', 'textarea[name="description"]']:
                try:
                    el = page.locator(sel).first
                    if await el.count() > 0: await el.click(); await el.fill(description); break
                except Exception: continue

            published = False
            for btn_text in ["Опубликовать", "Сохранить выпуск", "Сохранить"]:
                try:
                    btn = page.locator(f'button:has-text("{btn_text}")').first
                    if await btn.count() > 0: await btn.click(); published = True; break
                except Exception: continue

            if not published: raise RuntimeError("Кнопка публикации mave не найдена")
            await asyncio.sleep(5)
            await page.screenshot(path="/tmp/mave_done.png")
            return True
        except Exception as e:
            await page.screenshot(path="/tmp/mave_error.png")
            raise RuntimeError(f"{e}")
        finally:
            await browser.close()

async def handle_voice(update: Update, tg_context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if ALLOWED_USER_ID and user_id != ALLOWED_USER_ID: return
    msg = await update.message.reply_text("⏳ Начинаю обработку...")
    try:
        ogg = await download_voice(update, tg_context)
        await msg.edit_text("🔄 Превращаю в MP3...")
        mp3 = convert_to_mp3(ogg)

        await msg.edit_text("🎙️ Улучшаю звук в ИИ Adobe Podcast...")

        async def send_screenshot(path: str, caption: str):
            try:
                if os.path.exists(path): await update.message.reply_photo(photo=open(path, "rb"), caption=f"ℹ️ {caption}")
            except Exception: pass

        studio_mp3 = await enhance_audio(mp3, send_screenshot=send_screenshot)

        await msg.edit_text("📝 Делаю расшифровку текста...")
        transcript = transcribe(studio_mp3)

        await msg.edit_text("✍️ Создаю метаданные под твой стиль...")
        title, description = generate_metadata(transcript)

        pending[user_id] = {"mp3": studio_mp3, "title": title, "description": description}
        await msg.delete()

        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Опубликовать в mave", callback_data="publish"),
            InlineKeyboardButton("✏️ Изменить", callback_data="edit")
        ], [InlineKeyboardButton("❌ Отмена", callback_data="cancel")]])
        preview = f"🎙 *Выпуск готов к публикации*\n\n*Заголовок:*\n{title}\n\n*Описание:*\n{description}\n\nВыгружаем?"
        await update.message.reply_document(document=open(studio_mp3, "rb"), filename=f"{title[:40]}.mp3")
        await update.message.reply_text(preview, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)
    except Exception as e:
        await msg.edit_text(f"❌ Ошибка: {e}")

async def button_publish(update: Update, tg_context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    data = pending.get(user_id)
    if not data:
        await query.edit_message_text("❌ Сессия устарела.")
        return
    await query.edit_message_text("⏳ Загружаю на mave.digital...")
    try:
        for f in ["/tmp/mave_error.png", "/tmp/mave_done.png"]:
            if os.path.exists(f): os.remove(f)
        await upload_to_mave(data["mp3"], data["title"], data["description"])
        caption = f"✅ *Успешно выгружено!*\n\n_{data['title']}_"
        if os.path.exists("/tmp/mave_done.png"):
            await query.message.reply_photo(photo=open("/tmp/mave_done.png", "rb"), caption=caption, parse_mode=ParseMode.MARKDOWN)
            await query.message.delete()
        else:
            await query.edit_message_text(caption, parse_mode=ParseMode.MARKDOWN)
        pending.pop(user_id, None)
    except Exception as e:
        if os.path.exists("/tmp/mave_error.png"):
            await query.message.reply_photo(photo=open("/tmp/mave_error.png", "rb"), caption=f"❌ Ошибка Mave: {e}")
            await query.message.delete()
        else:
            await query.edit_message_text(f"❌ Ошибка: {e}")

async def button_edit(update: Update, tg_context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = pending.get(query.from_user.id)
    await query.edit_message_text(f"✏️ Текущий заголовок:\n*{data['title']}*\n\nНапиши новый заголовок (или /skip):", parse_mode=ParseMode.MARKDOWN)
    return EDIT_TITLE

async def edit_title(update: Update, tg_context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if update.message.text != "/skip": pending[user_id]["title"] = update.message.text
    data = pending[user_id]
    await update.message.reply_text(f"📝 Текущее описание:\n_{data['description']}_\n\nНапиши новое описание (или /skip):", parse_mode=ParseMode.MARKDOWN)
    return EDIT_DESC

async def edit_desc(update: Update, tg_context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if update.message.text != "/skip": pending[user_id]["description"] = update.message.text
    data = pending[user_id]
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Опубликовать", callback_data="publish"), InlineKeyboardButton("❌ Отмена", callback_data="cancel")]])
    await update.message.reply_text(f"🎙 *Данные обновлены:*\n\n*Заголовок:* {data['title']}\n*Описание:* {data['description']}\n\nПубликуем?", parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)
    return ConversationHandler.END

async def button_cancel(update: Update, tg_context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    pending.pop(query.from_user.id, None)
    await query.edit_message_text("❌ Отменено.")
    return ConversationHandler.END

def main():
    # Стартуем честный HTTP сервер в параллельном потоке
    threading.Thread(target=run_dummy_server, daemon=True).start()
    
    print("🚀 Запускаем Telegram-бота...")
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(button_edit, pattern="^edit$")],
        states={
            EDIT_TITLE: [MH(filters.TEXT & ~filters.COMMAND, edit_title)],
            EDIT_DESC:  [MH(filters.TEXT & ~filters.COMMAND, edit_desc)],
        },
        fallbacks=[CallbackQueryHandler(button_cancel, pattern="^cancel$")],
        per_chat=True
    )
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_voice))
    app.add_handler(conv)
    app.add_handler(CallbackQueryHandler(button_publish, pattern="^publish$"))
    app.add_handler(CallbackQueryHandler(button_cancel, pattern="^cancel$"))
    app.run_polling()

if __name__ == "__main__":
    main()
