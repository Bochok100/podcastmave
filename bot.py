"""
Podcast Bot v3.43 — The 6-Box Fix
- Исправлен баг пустых ячеек 2FA: теперь бот кликает и вводит каждую из 6 цифр в отдельный квадратик (обходит React-защиту Adobe).
- Тройной Enter после ввода кода для гарантии срабатывания формы.
- Жесткий выброс ошибки, если пароль не появился после кода.
- Сохранена полная загрузка файла без системных окон и анти-фриз таймер.
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
import gc
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

# ── Настройки ──────────────────────────────────
TELEGRAM_TOKEN  = os.getenv("TELEGRAM_TOKEN")
OPENAI_KEY      = os.getenv("OPENAI_API_KEY")
MAVE_EMAIL      = os.getenv("MAVE_EMAIL")
MAVE_PASSWORD   = os.getenv("MAVE_PASSWORD")
ADOBE_EMAIL     = os.getenv("ADOBE_EMAIL", "")
ADOBE_PASSWORD  = os.getenv("ADOBE_PASSWORD", "")
ALLOWED_USER_ID = int(os.getenv("ALLOWED_USER_ID", "0"))

STATE_FILE = "adobe_state.json"

EDIT_TITLE, EDIT_DESC = range(2)
pending = {}
adobe_2fa_state = {}

STYLE_PROMPT = """
Ты — редактор подкаста Василия. Темы разные: крипта, ИИ, технологии, семья, жизнь.
По расшифровке придумай заголовок и описание.

Стиль заголовков (только пример, не копируй):
- Как на самом деле работают сделки
- Агенты ИИ: почему без них уже нельзя
- Майнинг в России: быть или не быть?
- Что такое Long и Short на самом деле

Правила: конкретно, без воды, тема диктует заголовок.
НЕ пиши "топ", "секреты", "шокирующий".
Описание: 2 предложения, разговорный тон.

Ответ строго:
ЗАГОЛОВОК: [текст]
ОПИСАНИЕ: [текст]

ТРАНСКРИПЦИЯ:
"""

# ── Асинхронный HTTP сервер для Render ──────────
async def handle_client(reader, writer):
    try:
        response = "HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\n\r\nBot alive!\r\n"
        writer.write(response.encode('utf8'))
        await writer.drain()
    except Exception:
        pass
    finally:
        writer.close()
        await writer.wait_closed()

async def run_dummy_server():
    port = int(os.environ.get("PORT", 10000))
    server = await asyncio.start_server(handle_client, '0.0.0.0', port)
    print(f"✅ Асинхронный сервер Health Check запущен на порту {port}")
    async with server:
        await server.serve_forever()

async def post_init(app: Application):
    asyncio.create_task(run_dummy_server())

# ── Утилиты ────────────────────────────────────
async def download_voice(update, context) -> Path:
    voice = update.message.voice or update.message.audio
    f = await context.bot.get_file(voice.file_id)
    tmp = tempfile.NamedTemporaryFile(suffix=".ogg", delete=False)
    await f.download_to_drive(tmp.name)
    return Path(tmp.name)

async def to_mp3(src: Path) -> Path:
    dst = src.with_suffix(".mp3")
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y", "-i", str(src),
            "-codec:a", "libmp3lame", "-b:a", "128k",
            "-ar", "44100", "-ac", "1", str(dst),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
        if proc.returncode != 0:
            err_msg = stderr.decode('utf-8', errors='ignore')[-200:]
            raise RuntimeError(f"ffmpeg ошибка: {err_msg}")
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except: pass
        raise RuntimeError("ffmpeg завис — таймаут 60 секунд")
    
    if not dst.exists() or dst.stat().st_size < 1000:
        raise RuntimeError("ffmpeg не создал MP3 файл")
    
    gc.collect() 
    return dst

# ── Adobe Podcast ──────────────────────────────
async def enhance_audio(mp3: Path, user_id: int, notify) -> Path:
    if not ADOBE_EMAIL or not ADOBE_PASSWORD:
        raise RuntimeError("ОШИБКА: ADOBE_EMAIL или ADOBE_PASSWORD не заполнены в настройках Render!")

    adobe = mp3.parent / (mp3.stem + "_adobe.mp3")
    out   = mp3.parent / (mp3.stem + "_studio.mp3")

    async def shot(path, caption):
        try:
            if 'page' in locals() and not page.is_closed():
                await page.screenshot(path=path, timeout=10000)
                await notify(path, caption)
        except Exception:
            pass

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=False,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--disable-software-rasterizer",
                "--renderer-process-limit=1",
                "--js-flags=--max-old-space-size=150 --expose-gc",
                "--disable-site-isolation-trials",                 
                "--disk-cache-size=5242880",                       
                "--disable-blink-features=AutomationControlled",
                "--disable-extensions",
                "--disable-background-networking",
                "--disable-default-apps",
                "--no-first-run",
            ]
        )
        
        context_args = {
            "viewport": {"width": 1366, "height": 768},
            "locale": "en-US",
            "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        }
        if os.path.exists(STATE_FILE):
            print("Adobe: Обнаружен файл сессии, подгружаем куки...")
            context_args["storage_state"] = STATE_FILE

        ctx = await browser.new_context(**context_args)
        
        await ctx.add_init_script("""
            Object.defineProperty(navigator,'webdriver',{get:()=>undefined});
            Object.defineProperty(navigator,'plugins',{get:()=>[1,2,3,4,5]});
            Object.defineProperty(navigator,'languages',{get:()=>['en-US','en']});
            window.chrome={runtime:{}};
        """)
        page = await ctx.new_page()

        try:
            # ── 1. Открываем Adobe Podcast ──
            print("Adobe: открываем страницу...")
            await page.goto("https://podcast.adobe.com/enhance", timeout=60000)
            await page.wait_for_load_state("domcontentloaded")
            await asyncio.sleep(4)
            await shot("/tmp/adobe_last.png", f"Adobe: открыли. URL: {page.url}")

            # ── 2. Старый добрый клик по Sign In ──
            print("Adobe: Нажимаем Sign In...")
            try:
                await page.evaluate("""() => {
                    const el = [...document.querySelectorAll('a,button')]
                        .find(e => /^sign in|entrar|log in$/i.test(e.innerText?.trim()));
                    if (el) el.click();
                }""")
                await asyncio.sleep(5)
            except Exception:
                pass

            # ── 3. Email ──
            try:
                print("Adobe: ищем поле email (до 30 сек)...")
                email_target = None
                for _ in range(30):
                    inputs = await page.locator('input[type="email"], input[name="username"], input[id*="email" i], input[name="email" i]').all()
                    for inp in inputs:
                        if await inp.is_visible():
                            email_target = inp
                            break
                    if email_target:
                        break
                    await asyncio.sleep(1)

                if email_target:
                    print("Adobe: видимое поле email найдено. Вводим...")
                    await email_target.evaluate("node => node.focus()")
                    await asyncio.sleep(0.5)
                    await email_target.click(force=True)
                    await asyncio.sleep(0.5)
                    await page.keyboard.press("Control+A")
                    await page.keyboard.press("Backspace")
                    await page.keyboard.type(ADOBE_EMAIL.strip(), delay=100)
                    await asyncio.sleep(1)
                    
                    print("Adobe: Жмем Enter...")
                    await page.keyboard.press("Enter")
                    await asyncio.sleep(5)
                    await shot("/tmp/adobe_last.png", f"Adobe: после email. URL: {page.url}")
                else:
                    print("Adobe: Видимое поле email не найдено. Идем дальше.")
            except Exception as e:
                print(f"Adobe email error: {str(e)[:100]}")

            # ── 4. Умный навигатор ──
            print("Adobe: сканируем следующий шаг...")
            step = "unknown"
            for _ in range(30):
                pwd_inputs = await page.locator('input[type="password"]').all()
                for p in pwd_inputs:
                    if await p.is_visible():
                        step = "password"
                        break
                if step == "password": break
                
                btns = await page.locator('button:has-text("Continue"), button:has-text("Continuar")').all()
                for b in btns:
                    if await b.is_visible() and await page.locator('text=/Verify|Confirme|identity/i').count() > 0:
                        step = "continue_2fa"
                        break
                if step == "continue_2fa": break
                
                c_inps = await page.locator('input[type="text"], input[type="number"], input[type="tel"]').all()
                for c in c_inps:
                    if await c.is_visible() and await page.locator('text=/Verify|Confirme|identity|code/i').count() > 0:
                        step = "code_2fa"
                        break
                if step == "code_2fa": break
                
                if "enhance" in page.url and "auth" not in page.url:
                    ui_ready = False
                    if await page.locator('input[type="file"]').count() > 0: ui_ready = True
                    if await page.locator('text=/Choose files|Escolher arquivos/i').count() > 0: ui_ready = True
                    if ui_ready:
                        step = "done"
                        break
                
                await asyncio.sleep(1)
            
            print(f"Adobe: Следующий шаг определен как -> {step}")

            # ── 5. Нажимаем Continue для 2FA ──
            if step == "continue_2fa":
                print("Adobe: нажимаем Continue для отправки 2FA...")
                btns = await page.locator('button:has-text("Continue"), button:has-text("Continuar")').all()
                for btn in btns:
                    if await btn.is_visible():
                        await btn.click(force=True)
                        break
                
                for _ in range(15):
                    await asyncio.sleep(1)
                    c_inps = await page.locator('input[type="text"], input[type="number"], input[type="tel"]').all()
                    for c in c_inps:
                        if await c.is_visible():
                            step = "code_2fa"
                            break
                    if step == "code_2fa": break

            # ── 6. Ввод 2FA кода (THE 6-BOX FIX) ──
            if step == "code_2fa":
                await shot("/tmp/adobe_last.png", "⚠️ Adobe запросил код с почты! Пришли его сюда обычным текстом (3 минуты).")
                ev = asyncio.Event()
                adobe_2fa_state[user_id] = {"
