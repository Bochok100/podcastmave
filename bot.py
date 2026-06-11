"""
Podcast Bot v3.13 — The Flawless Focus
- Убраны команды clear(), которые сбрасывали фокус курсора
- Ввод текста жестко привязан к локатору через press_sequentially
- Метод клавиши Enter оставлен (работает идеально)
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

# ── HTTP сервер ──────────
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/screen':
            for f in ['/tmp/adobe_last.png', '/tmp/adobe_error.png']:
                if os.path.exists(f):
                    self.send_response(200)
                    self.send_header("Content-Type", "image/png")
                    self.end_headers()
                    self.wfile.write(open(f, 'rb').read())
                    return
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write("Bot alive. Screen: /screen".encode())

    def log_message(self, *a):
        return

def run_server():
    HTTPServer(('0.0.0.0', int(os.environ.get("PORT", 10000))), Handler).serve_forever()

# ── Утилиты ────────────────────────────────────
async def download_voice(update, context) -> Path:
    voice = update.message.voice or update.message.audio
    f = await context.bot.get_file(voice.file_id)
    tmp = tempfile.NamedTemporaryFile(suffix=".ogg", delete=False)
    await f.download_to_drive(tmp.name)
    return Path(tmp.name)

def to_mp3(src: Path) -> Path:
    dst = src.with_suffix(".mp3")
    subprocess.run(["ffmpeg", "-y", "-i", str(src),
                    "-codec:a", "libmp3lame", "-b:a", "128k",
                    "-ar", "44100", "-ac", "1", str(dst)],
                   capture_output=True)
    return dst

# ── Adobe Podcast ──────────────────────────────
async def enhance_audio(mp3: Path, user_id: int, notify) -> Path:
    if not ADOBE_EMAIL or not ADOBE_PASSWORD:
        raise RuntimeError("ОШИБКА: ADOBE_EMAIL или ADOBE_PASSWORD не заполнены в настройках Render!")

    adobe = mp3.parent / (mp3.stem + "_adobe.mp3")
    out   = mp3.parent / (mp3.stem + "_studio.mp3")

    async def shot(path, caption):
        try:
            await page.screenshot(path=path)
            await notify(path, caption)
        except Exception:
            pass

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=False,
            args=["--no-sandbox", "--disable-setuid-sandbox",
                  "--disable-dev-shm-usage",
                  "--disable-blink-features=AutomationControlled"]
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
            print("Adobe: открываем enhance...")
            await page.goto("https://podcast.adobe.com/enhance", timeout=60000)
            await page.wait_for_load_state("domcontentloaded")
            await asyncio.sleep(4)
            await shot("/tmp/adobe_last.png", f"Adobe открыт. URL: {page.url}")

            # ── 2. Нажимаем Sign In если нужно ──
            try:
                await page.evaluate("""() => {
                    const el = [...document.querySelectorAll('a,button')]
                        .find(e => /^sign in$/i.test(e.innerText?.trim()));
                    if (el) el.click();
                }""")
                await asyncio.sleep(4)
            except Exception:
                pass

            # ── 3. Вводим email (БЕЗ CLEAR, ЖЕСТКАЯ ПРИВЯЗКА К ПОЛЮ) ──
            try:
                print("Adobe: ждем появление поля email...")
                email_field = page.locator('input[type="email"],input[name="username"]').first
                await email_field.wait_for(state="visible", timeout=8000)
                
                print("Adobe: поле email найдено. Вводим...")
                await email_field.click()
                await asyncio.sleep(0.5)
                
                # Печатаем строго в это поле
                await email_field.press_sequentially(ADOBE_EMAIL.strip(), delay=100)
                await asyncio.sleep(1)

                print("Adobe: Жмем Enter...")
                await email_field.press("Enter")
                await asyncio.sleep(4)
                await shot("/tmp/adobe_last.png", "Adobe: после email")
            except Exception as e:
                print(f"Adobe: Поле email не найдено ({e}). Идем дальше.")

            # ── 4. Экран подтверждения личности (перед кодом) ──
            try:
                btn_verify = page.locator('button:has-text("Continue"),button:has-text("Продолжить")').first
                if await page.locator('text=/Verify your identity|Подтверждение личности/i').count() > 0 and await btn_verify.count() > 0:
                    print("Adobe: экран подтверждения — нажимаем Continue...")
                    await btn_verify.click(force=True)
                    await asyncio.sleep(4)
            except Exception:
                pass

            # ── 5. Ввод 2FA кода ──
            if await page.locator('text=/Verify your identity/i').count() > 0:
                print("Adobe: нужен 2FA код...")
                await shot("/tmp/adobe_last.png", "⚠️ Adobe запросил код с почты!\nПришли мне его обычным текстом (2 минуты).")
                ev = asyncio.Event()
                adobe_2fa_state[user_id] = {"event": ev, "code": ""}
                try:
                    await asyncio.wait_for(ev.wait(), timeout=120)
                    code = adobe_2fa_state[user_id]["code"].strip()
                    
                    visible_inputs = await page.locator('input:visible').all()
                    if len(visible_inputs) >= 6:
                        for i, digit in enumerate(code[:6]):
                            await visible_inputs[i].fill(digit)
                            await asyncio.sleep(0.1)
                        await visible_inputs[5].press("Enter")
                    else:
                        code_input = page.locator('input[type="text"]').first
                        await code_input.click()
                        await code_input.press_sequentially(code, delay=150)
                        await code_input.press("Enter")
                    
                    await asyncio.sleep(1)
                    
                    for _ in range(10):
                        await asyncio.sleep(1)
                        if await page.locator('text=/invalid code/i').count() > 0:
                            raise RuntimeError("Adobe не принял код (пишет Invalid code). Начни загрузку заново.")
                        if await page.locator('text=/Verify your identity/i').count() == 0:
                            break
                            
                    print(f"Adobe: 2FA код отправлен и принят")
                except asyncio.TimeoutError:
                    raise RuntimeError("2FA таймаут: код не пришёл за 2 минуты")
                finally:
                    adobe_2fa_state.pop(user_id, None)

            # ── 6. Вводим пароль (БЕЗ CLEAR, ЖЕСТКАЯ ПРИВЯЗКА К ПОЛЮ) ──
            try:
                print("Adobe: ждем появление поля пароля...")
                pwd_field = page.locator('input[type="password"],#password').first
                await pwd_field.wait_for(state="visible", timeout=8000)
                
                print("Adobe: поле пароля найдено. Вводим...")
                await pwd_field.click()
                await asyncio.sleep(0.5)
                
                # Печатаем строго в это поле
                await pwd_field.press_sequentially(ADOBE_PASSWORD.strip(), delay=100)
                await asyncio.sleep(1)
                
                print("Adobe: Жмем Enter...")
                await pwd_field.press("Enter")
                await asyncio.sleep(6)
            except Exception as e:
                print(f"Adobe: Поле пароля не появилось ({e}).")

            # Закрываем промежуточные экраны
            for _ in range(10):
                try:
                    await page.evaluate("""() => {
                        const btn = [...document.querySelectorAll('button,a')]
                            .find(e => /not now|skip|пропустить|continue|продолжить/i.test(e.innerText?.trim()));
                        if (btn) btn.click();
                    }""")
                except Exception:
                    pass
                if "enhance" in page.url:
                    break
                await asyncio.sleep(1)

            if "enhance" not in page.url:
                await page.goto("https://podcast.adobe.com/enhance", timeout=60000)
                await page.wait_for_load_state("domcontentloaded")
                await asyncio.sleep(4)

            # ── ЗАЩИТА ОТ СЛЕПОТЫ ПЕРЕД ЗАГРУЗКОЙ ──
            if await page.locator('input[type="email"],input[name="username"]').count() > 0 or await page.locator('text=/Sign in/i').count() > 2:
                await shot("/tmp/adobe_error.png", "❌ Бот застрял на экране авторизации!")
                raise RuntimeError("Ошибка авторизации: не удалось войти (возможно, форма зависла).")

            # СОХРАНЕНИЕ СЕССИИ (КУКИ)
            await ctx.storage_state(path=STATE_FILE)
            print("✅ Adobe: сессия успешно сохранена!")

            # Закрываем баннеры
            try:
                await page.evaluate("""() => {
                    document.querySelectorAll('button').forEach(b => {
                        if (/Accept|Agree|Got it|Close|Skip/i.test(b.innerText)) b.click();
                    });
                }""")
                await asyncio.sleep(2)
            except Exception: pass

            await shot("/tmp/adobe_last.png", "Adobe: Загружаем файл...")

            # ── 7. Загрузка файла ──
            uploaded = False
            for frame in page.frames:
                try:
                    for inp in await frame.locator('input[type="file"]').all():
                        try:
                            await inp.set_input_files(str(mp3), timeout=5000)
                            uploaded = True
                            break
                        except Exception: pass
                    if uploaded: break
                except Exception: continue

            if not uploaded:
                for frame in page.frames:
                    try:
                        btn = frame.locator('text=/Choose files|Upload/i').first
                        if await btn.count() > 0:
                            async with page.expect_file_chooser(timeout=5000) as fc_info:
                                await btn.click(force=True)
                            await (await fc_info.value).set_files(str(mp3))
                            uploaded = True
                            break
                    except Exception: continue

            if not uploaded:
                with open(mp3, "rb") as f:
                    b64 = base64.b64encode(f.read()).decode()
                await page.evaluate(f"""async () => {{
                    const bin = atob("{b64}"), arr = new Uint8Array(bin.length);
                    for (let i=0;i<bin.length;i++) arr[i]=bin.charCodeAt(i);
                    const file = new File([arr],"{mp3.name}",{{type:"audio/mpeg"}});
                    const dt = new DataTransfer(); dt.items.add(file);
                    ['dragenter','dragover','drop'].forEach(ev =>
                        document.body.dispatchEvent(new DragEvent(ev,{{bubbles:true,cancelable:true,dataTransfer:dt}}))
                    );
                }}""")
                uploaded = True

            await asyncio.sleep(3)
            
            # ── 8. Нажимаем Enhance ──
            for frame in page.frames:
                for sel in ['button:has-text("Enhance speech")', 'button:has-text("Enhance")', 'button[type="submit"]']:
                    try:
                        btn = frame.locator(sel).first
                        if await btn.count() > 0:
                            await btn.evaluate("n=>n.click()")
                            break
                    except Exception: continue

            # ── 9. Ждём Download (до 10 минут) ──
            dl_btn = None
            print("Adobe: ждём обработку...")
            for i in range(120):
                await asyncio.sleep(5)
                for frame in page.frames:
                    b = frame.locator('button:has-text("Download"), a:has-text("Download"), [aria-label="Download"]').last
                    if await b.count() > 0:
                        dl_btn = b
                        break
                if dl_btn: 
                    break
                if i % 6 == 5: 
                    await shot("/tmp/adobe_last.png", f"Adobe: обрабатываем... {(i+1)*5} сек")

            if not dl_btn:
                raise RuntimeError("Adobe: Download не появился за 10 минут")

            await shot("/tmp/adobe_last.png", "✅ Adobe обработал! Скачиваем...")

            # ── 10. Скачиваем ──
            async with page.expect_download(timeout=120000) as dl_info:
                await dl_btn.evaluate("n=>n.click()")
            dl = await dl_info.value
            await dl.save_as(str(adobe))
            size = adobe.stat().st_size

            if size < 10000:
                raise RuntimeError(f"Adobe вернул пустой файл ({size} байт)")

            r = subprocess.run(
                ["ffmpeg", "-y", "-i", str(adobe),
                 "-af", "loudnorm=I=-16:TP=-1.5:LRA=11",
                 "-codec:a", "libmp3lame", "-b:a", "128k", "-ar", "44100", "-ac", "1", str(out)],
                capture_output=True
            )
            return out if r.returncode == 0 and out.exists() else adobe

        except Exception as e:
            try:
                await page.screenshot(path="/tmp/adobe_error.png")
                await notify("/tmp/adobe_error.png", f"❌ Adobe ошибка: {str(e)[:120]}")
            except Exception: pass
            raise RuntimeError(f"Adobe: {e}")
        finally:
            await browser.close()

# ── mave.digital ───────────────────────────────
async def upload_to_mave(mp3: Path, title: str, desc: str) -> bool:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=False,
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"]
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
            await browser.close()

# ── Транскрипция ───────────────────────────────
def transcribe(mp3: Path) -> str:
    client = openai.OpenAI(api_key=OPENAI_KEY)
    safe = mp3.parent / (mp3.stem + "_w.mp3")
    shutil.copy2(mp3, safe)
    with open(safe, "rb") as f:
        return client.audio.transcriptions.create(model="whisper-1", file=f, language="ru").text

# ── Метаданные ─────────────────────────────────
def generate_metadata(transcript: str) -> tuple[str, str]:
    client = openai.OpenAI(api_key=OPENAI_KEY)
    resp = client.chat.completions.create(
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
        mp3 = to_mp3(ogg)

        await msg.edit_text("🎙️ Adobe Podcast Enhance (3-5 мин)...")

        async def notify(path, caption):
            try:
                if os.path.exists(path):
                    await update.message.reply_photo(photo=open(path, "rb"), caption=f"ℹ️ {caption}")
            except Exception: pass

        studio = await enhance_audio(mp3, uid, notify)

        await msg.edit_text("📝 Whisper транскрипция...")
        text = transcribe(studio)

        await msg.edit_text("✍️ GPT-4o заголовок...")
        title, desc = generate_metadata(text)

        pending[uid] = {"mp3": studio, "title": title, "description": desc}
        await msg.delete()

        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Опубликовать в mave", callback_data="publish"),
            InlineKeyboardButton("✏️ Изменить", callback_data="edit"),
        ], [InlineKeyboardButton("❌ Отмена", callback_data="cancel")]])

        await update.message.reply_document(document=open(studio, "rb"), filename=f"{title[:40]}.mp3")
        await update.message.reply_text(
            f"🎙 *Готов*\n\n*Заголовок:*\n{title}\n\n*Описание:*\n{desc}\n\nОдобряешь?",
            parse_mode=ParseMode.MARKDOWN, reply_markup=kb
        )
    except Exception as e:
        await msg.edit_text(f"❌ Ошибка: {e}")

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
        caption = f"✅ *Опубликовано!*\n\n_{data['title']}_"
        if os.path.exists("/tmp/mave_done.png"):
            await q.message.reply_photo(photo=open("/tmp/mave_done.png", "rb"), caption=caption, parse_mode=ParseMode.MARKDOWN)
            await q.message.delete()
        else:
            await q.edit_message_text(caption, parse_mode=ParseMode.MARKDOWN)
        pending.pop(uid, None)
    except Exception as e:
        if os.path.exists("/tmp/mave_error.png"):
            await q.message.reply_photo(photo=open("/tmp/mave_error.png", "rb"), caption=f"❌ mave ошибка: {e}")
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
    return ConversationHandler.END

# ── Запуск ─────────────────────────────────────
def main():
    threading.Thread(target=run_server, daemon=True).start()
    print("🚀 Бот запускается...")
    try:
        app = Application.builder().token(TELEGRAM_TOKEN).build()
        
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
        app.run_polling()
    except Exception as e:
        print(f"❌ КРИТИЧЕСКАЯ ОШИБКА: {e}")
        while True:
            time.sleep(60)

if __name__ == "__main__":
    main()
