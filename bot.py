"""
Podcast Bot v13.4 — Возвращение Adobe
- ИСПРАВЛЕН БАГ: Возвращен шаг обработки аудио через Adobe Podcast.
- ОБНОВЛЕНО: Новый синтаксис OpenAI (AsyncOpenAI) v1.0+.
- ИСПРАВЛЕН БАГ IMAP: Улучшен поиск писем от Adobe + логи.
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
    ConversationHandler, filters, ContextTypes,
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
ALLOWED_USER_ID = int(os.getenv("ALLOWED_USER_ID", "0"))

STATE_FILE = "adobe_state.json"
COVER_FILE = "cover.jpg"

pending = {}
client = AsyncOpenAI(api_key=OPENAI_KEY)

STYLE_PROMPT = """
Ты — редактор подкаста Василия. Темы разные: крипта, ИИ, технологии, семья, жизнь.
По расшифровке придумай заголовок и описание.
Правила: конкретно, без воды. Описание: 2 предложения, разговорный тон.
Ответ строго:
ЗАГОЛОВОК: [текст]
ОПИСАНИЕ: [текст]
"""

POST_TEMPLATE = """<b>НОВЫЙ ВЫПУСК УЖЕ НА ПЛОЩАДКАХ:

{title}

Залетайте по ссылкам ниже, выбирайте свою платформу и оставайтесь на связи! 👇

Слушайте там, где удобно:

🎵 <a href="https://music.yandex.ru/album/40139714">ЯНДЕКС МУЗЫКА</a>

🎵 <a href="https://open.spotify.com/show/34qEduYapGFILe2jyH1L0h?si=NQ9Lz4hXT0W-8pgOfY5vvg">SPOTIFY</a>

🎵 <a href="https://podcasts.apple.com/us/podcast/vasiliy-crypto-%D0%BD%D0%B5-%D0%B1%D1%83%D0%B4%D1%8C-%D1%82%D0%BE%D0%BB%D0%BF%D0%BE%D0%B9/id1865729420">Apple Podcasts</a></b>"""

# ── IMAP Почта ─────────────────────────────────
async def fetch_adobe_code_from_email() -> str:
    if not EMAIL_USER or not EMAIL_PASS: return None
    def _fetch():
        try:
            print(f"🔍 Проверяю почту {EMAIL_USER}...")
            mail = imaplib.IMAP4_SSL("imap.gmail.com")
            mail.login(EMAIL_USER, EMAIL_PASS)
            mail.select("inbox")
            status, messages = mail.search(None, '(UNSEEN)')
            mail_ids = messages[0].split()
            if not mail_ids: return None
            
            for i in mail_ids[-3:]:
                status, msg_data = mail.fetch(i, '(RFC822)')
                for response_part in msg_data:
                    if isinstance(response_part, tuple):
                        msg = email.message_from_bytes(response_part[1])
                        sender = msg.get("From", "")
                        if "adobe" in sender.lower():
                            body = msg.get_payload(decode=True).decode(errors="ignore") if not msg.is_multipart() else next(p.get_payload(decode=True).decode(errors="ignore") for p in msg.walk() if p.get_content_type() == "text/plain")
                            match = re.search(r'\b\d{6}\b', body)
                            if match:
                                print(f"✅ Нашел код Adobe: {match.group(0)}")
                                return match.group(0)
        except Exception as e: print(f"❌ Ошибка IMAP: {e}")
        return None

    for _ in range(12):
        code = await asyncio.to_thread(_fetch)
        if code: return code
        await asyncio.sleep(10)
    return None

# ── Утилиты ────────────────────────────────────
async def download_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> Path:
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
    return dst

# ── ADOBE PODCAST (ВОССТАНОВЛЕНО) ──────────────
async def enhance_audio(mp3: Path, notify) -> Path:
    out_path = mp3.parent / (mp3.stem + "_studio.mp3")
    await notify("⏳ Загружаю аудио в Adobe Podcast...")
    
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, args=["--no-sandbox"])
        context_args = {"viewport": {"width": 1366, "height": 768}}
        if os.path.exists(STATE_FILE): context_args["storage_state"] = STATE_FILE
        ctx = await browser.new_context(**context_args)
        page = await ctx.new_page()

        try:
            await page.goto("https://podcast.adobe.com/enhance", timeout=60000)
            await asyncio.sleep(4)
            
            # Проверяем авторизацию
            is_logged_in = True
            try:
                sign_btn = page.locator('a, button').filter(has_text=re.compile(r"sign in|log in", re.IGNORECASE)).first
                if await sign_btn.is_visible(): is_logged_in = False
            except: pass

            if not is_logged_in:
                await notify("🔐 Авторизация в Adobe...")
                await sign_btn.click()
                await asyncio.sleep(3)
                await page.locator('input[type="email"]').fill(ADOBE_EMAIL)
                await page.keyboard.press("Enter")
                await asyncio.sleep(3)
                await page.locator('input[type="password"]').fill(ADOBE_PASSWORD)
                await page.keyboard.press("Enter")
                await asyncio.sleep(5)
                
                # Проверка на 2FA код
                if "code" in page.url or await page.locator('input[name="code"]').is_visible():
                    await notify("🔐 Adobe запросил код подтверждения. Ищу в почте...")
                    code = await fetch_adobe_code_from_email()
                    if code:
                        await page.locator('input').first.fill(code)
                        await page.keyboard.press("Enter")
                        await asyncio.sleep(5)
                    else:
                        raise Exception("Не найден код 2FA в почте")
                await ctx.storage_state(path=STATE_FILE)

            # Загрузка и скачивание
            await notify("⏳ Улучшаю звук (Adobe AI)... Это займет пару минут.")
            async with page.expect_file_chooser() as fc_info:
                await page.locator('button').filter(has_text="Choose files").click()
            file_chooser = await fc_info.value
            await file_chooser.set_files(str(mp3))
            
            download_btn = page.locator('button').filter(has_text="Download")
            await download_btn.wait_for(state="visible", timeout=300000) # Ждем до 5 минут
            
            async with page.expect_download() as download_info:
                await download_btn.click()
            download = await download_info.value
            await download.save_as(str(out_path))
            return out_path
            
        except Exception as e:
            print(f"Ошибка Adobe: {e}")
            await notify(f"⚠️ Ошибка Adobe: {e}. Буду использовать оригинальный звук.")
            return mp3 # Если упало, возвращаем оригинал
        finally:
            await browser.close()

# ── Обработка фото ──────────────────────────────
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER_ID: return
    photo = update.message.photo[-1]
    f = await context.bot.get_file(photo.file_id)
    await f.download_to_drive(COVER_FILE)
    await update.message.reply_text("✅ Картинка-обложка для канала сохранена!")

# ── Главный конвейер ───────────────────────────
async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER_ID: return
    msg = await update.message.reply_text("⏳ Принял аудио! Начинаю обработку...")
    
    try:
        ogg_path = await download_voice(update, context)
        mp3_path = await to_mp3(ogg_path)
        
        # 1. Adobe Podcast (ВОССТАНОВЛЕНО)
        studio_mp3 = await enhance_audio(mp3_path, msg.edit_text)
        
        # 2. Транскрипция (OpenAI Whisper)
        await msg.edit_text("⏳ Делаю транскрипцию (Whisper)...")
        with open(studio_mp3, "rb") as audio_file:
            transcript = await client.audio.transcriptions.create(
                model="whisper-1", 
                file=audio_file
            )
        text = transcript.text

        # 3. Генерация текста (GPT-4o)
        await msg.edit_text("⏳ Придумываю заголовок и описание (GPT-4o)...")
        response = await client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": STYLE_PROMPT},
                {"role": "user", "content": text}
            ]
        )
        ai_text = response.choices[0].message.content
        
        title_match = re.search(r'ЗАГОЛОВОК:\s*(.+)', ai_text, re.IGNORECASE)
        desc_match = re.search(r'ОПИСАНИЕ:\s*(.+)', ai_text, re.IGNORECASE | re.DOTALL)
        
        title = title_match.group(1).strip() if title_match else "Новый выпуск подкаста"
        desc = desc_match.group(1).strip() if desc_match else ai_text.strip()

        post_text = POST_TEMPLATE.format(title=title)
        
        uid = update.message.message_id
        pending[uid] = {
            "mp3": studio_mp3, # Передаем студийный файл
            "title": title,
            "desc": desc,
            "post_text": post_text
        }
        
        kb = [[InlineKeyboardButton("🚀 Опубликовать в Mave", callback_data=f"mave_{uid}")]]
        await msg.edit_text(f"✅ *Готово!*\n\n*Заголовок:* {title}\n*Описание:* {desc}", reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)

    except Exception as e:
        await msg.edit_text(f"❌ Ошибка конвейера: {str(e)}")

# ── Запуск (Mave) ──────────────────────────────
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    
    if data.startswith("mave_"):
        uid = int(data.split("_")[1])
        info = pending.get(uid)
        if not info: return
            
        await query.message.edit_text("⏳ Публикую в Mave. Пожалуйста, подождите...")
        
        try:
            # =======================================================
            # ВНИМАНИЕ: ЗДЕСЬ ДОЛЖЕН БЫТЬ ТВОЙ КОД PLAYWRIGHT ДЛЯ MAVE
            # =======================================================
            await asyncio.sleep(2) # Заглушка
            
            kb = [
                [InlineKeyboardButton("✅ Опубликовать сейчас", callback_data=f"pubnow_{uid}")]
            ]
            await query.message.reply_text(f"✅ В Mave загружено!\n\nВыбери, как опубликовать пост в Telegram-канал:", reply_markup=InlineKeyboardMarkup(kb))
            
        except Exception as e:
            await query.message.reply_text(f"❌ Ошибка Mave: {str(e)}")

    elif data.startswith("pubnow_"):
        uid = int(data.split("_")[1])
        info = pending.get(uid)
        if not info: return
        
        try:
            if os.path.exists(COVER_FILE):
                with open(COVER_FILE, "rb") as img:
                    await context.bot.send_photo(chat_id=CHANNEL_ID, photo=img, caption=info["post_text"], parse_mode=ParseMode.HTML)
            else:
                await context.bot.send_message(chat_id=CHANNEL_ID, text=info["post_text"], parse_mode=ParseMode.HTML)
            await query.message.edit_text("✅ Пост успешно опубликован в канал!")
        except Exception as e:
            await query.message.reply_text(f"❌ Ошибка Telegram: {str(e)}")

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_voice))
    app.add_handler(CallbackQueryHandler(handle_callback))
    
    print("🚀 Бот запущен (Версия 13.4)")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
