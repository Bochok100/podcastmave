"""
Podcast Bot v4.1 — The Regex Fix
- ИСПРАВЛЕНА СИНТАКСИЧЕСКАЯ ОШИБКА: флаг игнорирования регистра (?i) заменен на нативный re.IGNORECASE, чтобы предотвратить ошибки трансляции Regex в движок JavaScript (V8).
- Сохранен State Machine Engine: бот оценивает экран и сам решает, что вводить, без линейных таймеров.
- Сохранен прямой SPA-роутинг и обход React-защиты 2FA.
"""

import os
import json
import asyncio
import tempfile
import subprocess
import shutil
import time
import base64
import gc
import re
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
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass

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
    
    fd, path = tempfile.mkstemp(suffix=".ogg")
    os.close(fd) 
    
    print(f"Скачиваем аудио в {path}...")
    await f.download_to_drive(path)
    print("Скачивание аудио завершено.")
    return Path(path)

async def to_mp3(src: Path) -> Path:
    dst = src.with_suffix(".mp3")
    print(f"Конвертация {src} -> {dst}")
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y", "-i", str(src),
            "-codec:a", "libmp3lame", "-b:a", "128k",
            "-ar", "44100", "-ac", "1", str(dst),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
        if proc.returncode != 0:
            err_msg = stderr.decode('utf-8', errors='ignore')[-200:]
            raise RuntimeError(f"Ошибка конвертации: {err_msg}")
    except FileNotFoundError:
        raise RuntimeError("⚠️ ffmpeg не установлен на сервере! Добавьте 'RUN apt-get update && apt-get install -y ffmpeg' в ваш Dockerfile.")
    except asyncio.TimeoutError:
        try: proc.kill()
        except: pass
        raise RuntimeError("ffmpeg завис (таймаут 120 секунд).")
    except Exception as e:
        raise RuntimeError(f"Системная ошибка конвертера: {e}")
    
    if not dst.exists() or dst.stat().st_size < 1000:
        raise RuntimeError("ffmpeg отработал, но MP3 файл пустой или не создался.")
    
    print("Конвертация успешно завершена.")
    gc.collect() 
    return dst

# ── Adobe Podcast (STATE MACHINE ENGINE) ──────────
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
                "--js-flags=--max-old-space-size=250 --expose-gc",
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
            print("Adobe: Открываем страницу...")
            await page.goto("https://podcast.adobe.com/enhance", timeout=60000)
            await page.wait_for_load_state("domcontentloaded")
            await asyncio.sleep(4)
            await shot("/tmp/adobe_last.png", f"Adobe: открыли. URL: {page.url}")

            # === MASTER STATE MACHINE ===
            print("Adobe: Запуск State Machine Engine...")
            auth_success = False

            for step_attempts in range(40):
                url = page.url
                html = await page.content()

                # СТАТУС 1: Мы успешно авторизованы и в студии
                if await page.locator('input[type="file"]').count() > 0 or \
                   await page.locator('text=/Choose files|Escolher arquivos/i').count() > 0:
                    print("Adobe: Интерфейс загрузки готов!")
                    auth_success = True
                    break

                # СТАТУС 2: Пропуск промежуточных экранов (Skip, Not Now)
                skip_btn = page.locator('button, a').filter(has_text=re.compile(r"^not now$|^skip$|^remind me later$|^lembrar depois$|^pular$", re.IGNORECASE)).first
                if await skip_btn.is_visible():
                    print("Adobe: Пропускаем промежуточный экран...")
                    await skip_btn.click(force=True)
                    await asyncio.sleep(3)
                    continue

                # СТАТУС 3: Требуется нажать Sign In (если мы на главной)
                sign_btn = page.locator('a, button').filter(has_text=re.compile(r"sign in|entrar|log in", re.IGNORECASE)).first
                if await sign_btn.is_visible() and "auth" not in url and "login" not in url:
                    print("Adobe: Нажимаем Sign In...")
                    auth_url = await page.evaluate("""() => {
                        const link = Array.from(document.querySelectorAll('a')).find(a => a.href && (a.href.includes('auth.services') || a.href.includes('login')));
                        return link ? link.href : null;
                    }""")
                    if auth_url:
                        await page.goto(auth_url)
                    else:
                        await page.evaluate("""() => { document.querySelectorAll('[id*="onetrust"], [class*="cookie"], [class*="overlay"]').forEach(e => e.remove()); }""")
                        await sign_btn.click(force=True)
                    await asyncio.sleep(4)
                    continue

                # СТАТУС 4: Поле Email
                email_inp = page.locator('input[type="email"], input[name="username"]').first
                if await email_inp.is_visible():
                    val = await email_inp.input_value()
                    if not val:
                        print("Adobe: Вводим Email...")
                        await email_inp.click(force=True)
                        await page.keyboard.press("Control+A")
                        await page.keyboard.press("Backspace")
                        await email_inp.fill(ADOBE_EMAIL.strip())
                        await asyncio.sleep(0.5)
                        await page.keyboard.press("Enter")
                    else:
                        print("Adobe: Email уже введен, жмем Enter...")
                        await page.keyboard.press("Enter")
                    await asyncio.sleep(4)
                    continue

                # СТАТУС 5: Поле Пароля
                pwd_inp = page.locator('input[type="password"]').first
                if await pwd_inp.is_visible():
                    val = await pwd_inp.input_value()
                    if not val:
                        print("Adobe: Вводим Пароль...")
                        await pwd_inp.click(force=True)
                        await pwd_inp.fill(ADOBE_PASSWORD.strip())
                        await asyncio.sleep(0.5)
                        await page.keyboard.press("Enter")
                        await asyncio.sleep(2)
                        await shot("/tmp/adobe_last.png", f"Adobe: пароль введен.")
                    else:
                        await page.keyboard.press("Enter")
                    await asyncio.sleep(5)
                    continue

                # СТАТУС 6: Кнопка Continue (Иногда появляется перед отправкой кода 2FA)
                continue_btn = page.locator('button').filter(has_text=re.compile(r"^Continue$|^Continuar$", re.IGNORECASE)).first
                if await continue_btn.is_visible() and ("verify" in html.lower() or "confirme" in html.lower()):
                    print("Adobe: Нажимаем Continue для отправки 2FA...")
                    await continue_btn.click(force=True)
                    await asyncio.sleep(4)
                    continue

                # СТАТУС 7: Поля 2FA
                tel_inps = page.locator('input[type="text"], input[type="number"], input[type="tel"]')
                if await tel_inps.count() > 0 and ("verify" in html.lower() or "confirme" in html.lower() or "code" in html.lower()):
                    await shot("/tmp/adobe_last.png", "⚠️ Adobe запросил код с почты! Пришли его сюда (3 минуты).")
                    ev = asyncio.Event()
                    adobe_2fa_state[user_id] = {"event": ev, "code": ""}
                    try:
                        await asyncio.wait_for(ev.wait(), timeout=180)
                        code = adobe_2fa_state[user_id]["code"].strip()
                        await shot("/tmp/adobe_last.png", f"⏳ Ввожу код {code[:2]}***...")

                        count = await tel_inps.count()
                        if count >= 6:
                            print("Adobe: Ввод 2FA в 6 ячеек (Обход React)...")
                            for i in range(6):
                                try:
                                    target = page.locator('input[type="text"], input[type="number"], input[type="tel"]').nth(i)
                                    await target.click(force=True)
                                    await asyncio.sleep(0.1)
                                    await page.keyboard.press(code[i])
                                except Exception:
                                    await page.keyboard.press(code[i])
                                await asyncio.sleep(0.1)
                        elif count > 0:
                            print("Adobe: Ввод 2FA в единое поле...")
                            await tel_inps.first.click(force=True)
                            await tel_inps.first.fill(code)
                        else:
                            await page.keyboard.type(code, delay=200)

                        await asyncio.sleep(3)
                        
                        # Нажатие кнопки подтверждения
                        verify_btn = page.locator('button').filter(has_text=re.compile(r"^Verify$|^Verificar$|^Submit$", re.IGNORECASE)).first
                        if await verify_btn.is_visible():
                            await verify_btn.click(force=True)
                        else:
                            await page.keyboard.press("Enter")

                        await asyncio.sleep(5)
                        
                        # Проверка на отторжение кода
                        html_after = await page.content()
                        if re.search(r"inválido|invalid|incorrect|wrong|неверный", html_after, re.IGNORECASE):
                            await shot("/tmp/adobe_error.png", "❌ Adobe: Неверный код 2FA. Запусти заново.")
                            raise RuntimeError("Adobe не принял код 2FA.")

                    except asyncio.TimeoutError:
                        raise RuntimeError("2FA таймаут: код не пришёл за 3 минуты.")
                    finally:
                        adobe_2fa_state.pop(user_id, None)
                    continue

                # СТАТУС 8: Пассивная загрузка / Редиректы Adobe IMS
                print(f"Adobe: Сканирование... (URL: {page.url})")
                await asyncio.sleep(2)

            # === ОКОНЧАНИЕ ЦИКЛА АВТОРИЗАЦИИ ===

            if not auth_success:
                await shot("/tmp/adobe_error.png", "❌ Adobe: Истекло время авторизации. Бот заблудился в интерфейсе.")
                raise RuntimeError("Сбой навигации: State Machine не достигла студии Enhance.")
            
            print("✅ Adobe: авторизация завершена!")
            await ctx.storage_state(path=STATE_FILE)

            # ── ЗАГРУЗКА ФАЙЛА ──
            print("Adobe: начинаем загрузку файла...")
            uploaded = False
            
            try:
                file_input = page.locator('input[type="file"]').first
                await file_input.wait_for(state="attached", timeout=10000)
                await file_input.evaluate("node => node.style.display = 'block'")
                await file_input.set_input_files(str(mp3), timeout=15000)
                uploaded = True
                print("Adobe: Файл загружен через прямой input!")
            except Exception as e:
                print(f"Adobe: Прямой input не сработал: {e}")

            if not uploaded:
                try:
                    async with page.expect_file_chooser(timeout=8000) as fc_info:
                        btn = page.locator('button').filter(has_text=re.compile(r"Choose files|Escolher arquivos", re.IGNORECASE)).first
                        await btn.click(force=True)
                    fc = await fc_info.value
                    await fc.set_files(str(mp3), timeout=15000)
                    uploaded = True
                    print("Adobe: Файл загружен через file_chooser!")
                except Exception as e:
                    print(f"Adobe: Перехватчик не сработал: {e}")

            if not uploaded:
                try:
                    with open(mp3, "rb") as f:
                        b64 = base64.b64encode(f.read()).decode()
                    await page.evaluate(f"""async () => {{
                        const bin = atob("{b64}");
                        const arr = new Uint8Array(bin.length);
                        for (let i=0; i<bin.length; i++) arr[i] = bin.charCodeAt(i);
                        const file = new File([arr], "{mp3.name}", {{type: "audio/mpeg"}});
                        const dt = new DataTransfer();
                        dt.items.add(file);
                        
                        const dropZone = document.querySelector('[class*="upload"], [class*="drop"]') || document.body;
                        ['dragenter', 'dragover', 'drop'].forEach(evName => {{
                            dropZone.dispatchEvent(new DragEvent(evName, {{bubbles: true, cancelable: true, dataTransfer: dt}}));
                        }});
                    }}""")
                    uploaded = True
                    del b64
                    gc.collect()
                    print("Adobe: Файл загружен через Drag & Drop!")
                except Exception as e:
                    print(f"Adobe: Drag & Drop не сработал: {e}")

            if not uploaded:
                await shot("/tmp/adobe_error.png", "❌ Ошибка: не удалось передать файл в Adobe.")
                raise RuntimeError("Adobe: все 3 метода загрузки файла провалились.")
            
            await shot("/tmp/adobe_last.png", "✅ Adobe: Файл передан! Ждем обработки...")
            await asyncio.sleep(3)
            
            # Нажимаем Enhance (если авто-обработка не началась)
            for frame in page.frames:
                for txt in ["Enhance speech", "Enhance", "Melhorar"]:
                    try:
                        btn = frame.locator('button').filter(has_text=re.compile(txt, re.IGNORECASE)).first
                        if await btn.count() > 0:
                            await asyncio.wait_for(btn.click(force=True), timeout=5.0)
                            break
                    except Exception: continue

            # ── ОЖИДАНИЕ И СКАЧИВАНИЕ ──
            dl_btn = None
            print("Adobe: ждём обработку (макс 3 минуты)...")
            for i in range(36):
                await asyncio.sleep(5)
                
                try:
                    await page.evaluate("try { window.gc(); } catch(e) {}")
                except Exception:
                    pass
                gc.collect()

                for frame in page.frames:
                    b = frame.locator('button, a').filter(has_text=re.compile(r"^Download$|^Baixar$", re.IGNORECASE)).last
                    if await b.count() > 0 and await b.is_visible():
                        dl_btn = b
                        break
                if dl_btn: 
                    break
                if i % 6 == 5: 
                    await shot("/tmp/adobe_last.png", f"Adobe: обрабатываем... {(i+1)*5} сек")

            if not dl_btn:
                await shot("/tmp/adobe_error.png", "❌ Кнопка Download не появилась!")
                raise RuntimeError("Adobe: кнопка Download не найдена. Вероятно, обработка зависла.")

            await shot("/tmp/adobe_last.png", "✅ Adobe обработал! Скачиваем...")

            async with page.expect_download(timeout=120000) as dl_info:
                await dl_btn.click(force=True)
            dl = await dl_info.value
            await dl.save_as(str(adobe))
            size = adobe.stat().st_size

            if size < 10000:
                raise RuntimeError(f"Adobe вернул пустой файл ({size} байт)")

            r = await asyncio.create_subprocess_exec(
                "ffmpeg", "-y", "-i", str(adobe),
                "-af", "loudnorm=I=-16:TP=-1.5:LRA=11",
                "-codec:a", "libmp3lame", "-b:a", "128k", "-ar", "44100", "-ac", "1", str(out),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            await asyncio.wait_for(r.communicate(), timeout=120)

            return out if out.exists() else adobe

        except Exception as e:
            import traceback
            traceback.print_exc()
            try:
                await page.screenshot(path="/tmp/adobe_error.png", timeout=5000)
                await notify("/tmp/adobe_error.png", f"❌ Adobe ошибка: {str(e)[:120]}")
            except Exception: pass
            raise RuntimeError(f"Adobe: {e}")
        finally:
            try:
                if 'ctx' in locals(): await ctx.close()
                if 'page' in locals() and not page.is_closed(): await page.close()
            except: pass
            try:
                await asyncio.wait_for(browser.close(), timeout=10.0)
            except Exception: pass
            gc.collect()

# ── mave.digital ───────────────────────────────
async def upload_to_mave(mp3: Path, title: str, desc: str) -> bool:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True, 
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--renderer-process-limit=1",
                "--js-flags=--max-old-space-size=150",
                "--disable-site-isolation-trials",
                "--disk-cache-size=5242880",
                "--disable-extensions",
                "--disable-background-networking",
                "--no-first-run",
            ]
        )
        ctx = await browser.new_context(viewport={"width": 1280, "height": 900}, locale="ru-RU")
        page = await ctx.new_page()
        try:
            await page.goto("https://app.mave.digital/login")
            await page.fill('input[type="email"]', MAVE_EMAIL)
            await page.fill('input[type="password"]', MAVE_PASSWORD)
            await page.click('button[type="submit"]')
            await page.wait_for_load_state("domcontentloaded")
            await asyncio.sleep(4)

            await page.evaluate("""() => {
                document.querySelectorAll('[id^="q-portal--dialog"]').forEach(e=>e.remove());
                document.querySelectorAll('.q-overlay,.q-dialog__backdrop').forEach(e=>e.remove());
                document.body.classList.remove('q-body--prevent-scroll','q-body--force-scrollbar-x');
            }""")
            await asyncio.sleep(1)
            await page.locator('text=Добавить выпуск').first.click()
            await asyncio.sleep(3)

            await page.wait_for_selector('input[type="file"]', state='attached', timeout=15000)
            await page.set_input_files('input[type="file"]', str(mp3))
            await asyncio.sleep(2)

            for sel in ['button:has-text("Загрузить файл")', 'button:has-text("Загрузить")']:
                try:
                    btn = page.locator(sel).first
                    if await btn.count() > 0:
                        await btn.evaluate("n=>n.click()")
                        break
                except Exception: continue

            for _ in range(36):
                await asyncio.sleep(5)
                html = await page.content()
                if any(x in html for x in ["Название выпуска", "episode-title", "upload-progress-done"]): break

            for sel in ['input[placeholder*="Название выпуска"]', 'input[placeholder*="Название"]', 'input[name="title"]']:
                try:
                    el = page.locator(sel).first
                    if await el.count() > 0:
                        await el.clear()
                        await el.fill(title)
                        break
                except Exception: continue

            for sel in ['.ProseMirror', 'div[contenteditable="true"]', 'textarea[name="description"]']:
                try:
                    el = page.locator(sel).first
                    if await el.count() > 0:
                        await el.click()
                        await el.fill(desc)
                        break
                except Exception: continue

            published = False
            for txt in ["Опубликовать", "Сохранить выпуск", "Сохранить"]:
                try:
                    btn = page.locator(f'button:has-text("{txt}")').first
                    if await btn.count() > 0:
                        await btn.evaluate("n=>n.click()")
                        published = True
                        break
                except Exception: continue

            if not published:
                raise RuntimeError("Кнопка публикации mave не найдена")
            await asyncio.sleep(5)
            await page.screenshot(path="/tmp/mave_done.png")
            return True
        except Exception as e:
            await page.screenshot(path="/tmp/mave_error.png")
            raise RuntimeError(str(e))
        finally:
            try:
                if 'ctx' in locals(): await ctx.close()
                if 'page' in locals() and not page.is_closed(): await page.close()
            except: pass
            await browser.close()
            gc.collect()

# ── Транскрипция ───────────────────────────────
async def transcribe(mp3: Path) -> str:
    client = openai.AsyncOpenAI(api_key=OPENAI_KEY)
    safe = mp3.parent / (mp3.stem + "_w.mp3")
    shutil.copy2(mp3, safe)
    
    with open(safe, "rb") as f:
        res = await client.audio.transcriptions.create(model="whisper-1", file=f, language="ru")
        text_result = res.text
        
    try:
        safe.unlink()
    except Exception:
        pass
    
    gc.collect()
    return text_result

# ── Метаданные ─────────────────────────────────
async def generate_metadata(transcript: str) -> tuple[str, str]:
    client = openai.AsyncOpenAI(api_key=OPENAI_KEY)
    resp = await client.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "user", "content": STYLE_PROMPT + transcript}],
        max_tokens=512, temperature=0.7
    )
    text = resp.choices[0].message.content
    title = desc = ""
    for line in text.splitlines():
        c = line.strip().replace("**", "")
        if c.upper().startswith("ЗАГОЛОВОК:"): title = c[10:].strip()
        elif c.upper().startswith("ОПИСАНИЕ:"): desc = c[9:].strip()
    return title, desc

# ── Telegram handlers ──────────────────────────
async def handle_voice(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if ALLOWED_USER_ID and uid != ALLOWED_USER_ID: return
    msg = await update.message.reply_text("⏳ Начинаю...")
    try:
        ogg = await download_voice(update, ctx)
        await msg.edit_text("🔄 MP3...")
        mp3 = await to_mp3(ogg)

        await msg.edit_text("🎙️ Adobe Podcast Enhance (3-5 мин)...")

        async def notify(path, caption):
            try:
                if os.path.exists(path):
                    with open(path, "rb") as f:
                        await update.message.reply_photo(photo=f, caption=f"ℹ️ {caption}")
            except Exception: pass

        try:
            studio = await asyncio.wait_for(enhance_audio(mp3, uid, notify), timeout=600.0)
        except asyncio.TimeoutError:
            raise RuntimeError("Критическое зависание: процесс занял более 10 минут (баг браузера).")

        for _f in [ogg, mp3]:
            try:
                if _f.exists() and str(_f) != str(studio):
                    _f.unlink()
            except Exception:
                pass
        gc.collect()

        await msg.edit_text("📝 Whisper транскрипция...")
        text = await transcribe(studio)

        await msg.edit_text("✍️ GPT-4o заголовок...")
        title, desc = await generate_metadata(text)
        
        del text 
        gc.collect()

        pending[uid] = {"mp3": studio, "title": title, "description": desc}
        await msg.delete()

        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Опубликовать в mave", callback_data="publish"),
            InlineKeyboardButton("✏️ Изменить", callback_data="edit"),
        ], [InlineKeyboardButton("❌ Отмена", callback_data="cancel")]])

        with open(studio, "rb") as audio_file:
            await update.message.reply_document(document=audio_file, filename=f"{title[:40]}.mp3")
            
        await update.message.reply_text(
            f"🎙 *Готов*\n\n*Заголовок:*\n{title}\n\n*Описание:*\n{desc}\n\nОдобряешь?",
            parse_mode=ParseMode.MARKDOWN, reply_markup=kb
        )
    except Exception as e:
        print(f"Глобальная ошибка работы бота: {e}")
        try:
            await msg.edit_text(f"❌ Ошибка: {str(e)}")
        except Exception:
            await update.message.reply_text(f"❌ Ошибка: {str(e)}")

async def handle_global_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid in adobe_2fa_state:
        adobe_2fa_state[uid]["code"] = update.message.text.strip()
        adobe_2fa_state[uid]["event"].set()
        await update.message.reply_text("✅ Код принят! Возвращаюсь в Adobe...")

async def btn_publish(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    data = pending.get(uid)
    if not data:
        await q.edit_message_text("❌ Сессия устарела.")
        return
    await q.edit_message_text("⏳ Загружаю в mave...")
    try:
        await upload_to_mave(data["mp3"], data["title"], data["description"])
        try:
            mp3_path = Path(data["mp3"])
            if mp3_path.exists():
                mp3_path.unlink()
                print(f"Удалён MP3: {mp3_path.name}")
        except Exception:
            pass
        caption = f"✅ *Опубликовано!*\n\n_{data['title']}_"
        
        if os.path.exists("/tmp/mave_done.png"):
            with open("/tmp/mave_done.png", "rb") as img:
                await q.message.reply_photo(photo=img, caption=caption, parse_mode=ParseMode.MARKDOWN)
            await q.message.delete()
        else:
            await q.edit_message_text(caption, parse_mode=ParseMode.MARKDOWN)
        pending.pop(uid, None)
        gc.collect()
        
    except Exception as e:
        if os.path.exists("/tmp/mave_error.png"):
            with open("/tmp/mave_error.png", "rb") as img:
                await q.message.reply_photo(photo=img, caption=f"❌ mave ошибка: {e}")
            await q.message.delete()
        else:
            await q.edit_message_text(f"❌ Ошибка: {e}")

async def btn_edit(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = pending.get(q.from_user.id)
    await q.edit_message_text(f"✏️ Заголовок:\n*{data['title']}*\n\nНовый (или /skip):", parse_mode=ParseMode.MARKDOWN)
    return EDIT_TITLE

async def edit_title(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if update.message.text != "/skip":
        pending[uid]["title"] = update.message.text
    await update.message.reply_text(f"📝 Описание:\n_{pending[uid]['description']}_\n\nНовое (или /skip):", parse_mode=ParseMode.MARKDOWN)
    return EDIT_DESC

async def edit_desc(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if update.message.text != "/skip":
        pending[uid]["description"] = update.message.text
    d = pending[uid]
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Опубликовать", callback_data="publish"),
        InlineKeyboardButton("❌ Отмена", callback_data="cancel"),
    ]])
    await update.message.reply_text(f"*Заголовок:* {d['title']}\n*Описание:* {d['description']}\n\nПубликуем?", parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
    return ConversationHandler.END

async def btn_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    pending.pop(q.from_user.id, None)
    await q.edit_message_text("❌ Отменено.")
    gc.collect()
    return ConversationHandler.END

# ── Запуск ─────────────────────────────────────
def main():
    print("🚀 Бот запускается...")
    if not TELEGRAM_TOKEN:
        print("❌ КРИТИЧЕСКАЯ ОШИБКА: TELEGRAM_TOKEN не найден в переменных окружения Render!")
        time.sleep(600)
        return

    try:
        app = Application.builder().token(TELEGRAM_TOKEN).post_init(post_init).build()
        
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_global_text, block=False), group=1)
        
        conv = ConversationHandler(
            entry_points=[CallbackQueryHandler(btn_edit, pattern="^edit$")],
            states={
                EDIT_TITLE: [MH(filters.TEXT & ~filters.COMMAND, edit_title)],
                EDIT_DESC:  [MH(filters.TEXT & ~filters.COMMAND, edit_desc)],
            },
            fallbacks=[CallbackQueryHandler(btn_cancel, pattern="^cancel$")],
            per_chat=True,
        )
        app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_voice, block=False))
        app.add_handler(conv)
        app.add_handler(CallbackQueryHandler(btn_publish, pattern="^publish$"))
        app.add_handler(CallbackQueryHandler(btn_cancel, pattern="^cancel$"))
        
        print("✅ Бот запущен!")
        app.run_polling(drop_pending_updates=True)
    except Exception as e:
        print(f"❌ КРИТИЧЕСКАЯ ОШИБКА: {e}")
        while True:
            time.sleep(60)

if __name__ == "__main__":
    main()
