"""
Podcast Bot v2 (Strict Login + Iframe Scanner + Token Conflict Safe)
- Сканирование ВСЕХ фреймов на скрытые поля input[type="file"]
- Предварительный скриншот экрана загрузки
- Жесткий контроль авторизации
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
adobe_2fa_state = {}

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
        return

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
async def enhance_audio(mp3_path: Path, user_id: int, send_screenshot=None) -> Path:
    if not ADOBE_EMAIL or not ADOBE_PASSWORD:
        raise RuntimeError("❌ ОШИБКА: В Render не заполнены переменные ADOBE_EMAIL или ADOBE_PASSWORD!")

    adobe_path  = mp3_path.parent / (mp3_path.stem + "_adobe.mp3")
    studio_path = mp3_path.parent / (mp3_path.stem + "_studio.mp3")

    async def notify(path: str, caption: str):
        if send_screenshot and os.path.exists(path):
            try:
                await send_screenshot(path, caption)
                print(f"📸 Скриншот успешно отправлен: {caption}")
            except Exception as e:
                print(f"❌ Сбой отправки скриншота в Телеграм: {e}")

    try:
        print("Adobe: Открываем Chromium в HEADED режиме (Дисплей :99)...")
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=False,
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
                    except Exception:
                        pass

                print("Adobe: Переход на страницу Enhance...")
                await page.goto("https://podcast.adobe.com/enhance", timeout=60000)
                await page.wait_for_load_state("domcontentloaded")
                await asyncio.sleep(5)

                if "auth" not in page.url:
                    try:
                        await page.evaluate("""() => {
                            const elements = Array.from(document.querySelectorAll('a, button'));
                            const signIn = elements.find(el => el.innerText && el.innerText.trim().toLowerCase() === 'sign in');
                            if (signIn) { signIn.click(); }
                        }""")
                        await asyncio.sleep(5)
                    except Exception:
                        pass

                # =================================================================
                # ЖЕСТКИЙ БЛОК АВТОРИЗАЦИИ
                # =================================================================
                if "auth" in page.url or "login" in page.url or "ims" in page.url:
                    print(f"Adobe: Экран авторизации. Вводим почту: {ADOBE_EMAIL}")
                    email_field = page.locator('input[type="email"], input[name="username"]').first
                    await email_field.wait_for(state="visible", timeout=15000)
                    await email_field.fill(ADOBE_EMAIL)
                    await asyncio.sleep(2)
                    
                    continue_btn = page.locator('button:has-text("Продолжить"), button:has-text("Continue"), button[type="submit"], #btn-id-forward').first
                    await continue_btn.click()
                    await asyncio.sleep(5)
                    
                    if await page.locator('text=Подтверждение личности').count() > 0 or await page.locator('button:has-text("Продолжить")').count() > 0:
                        print("Adobe: Запрашиваем 2FA код...")
                        try:
                            await page.locator('button:has-text("Продолжить")').first.click()
                        except:
                            pass
                            
                        await asyncio.sleep(4)
                        
                        try:
                            await page.screenshot(path="/tmp/adobe_2fa_input.png", timeout=5000)
                            await notify("/tmp/adobe_2fa_input.png", "⚠️ Adobe отправил проверочный код на твою почту! Пришли мне его ОБЫЧНЫМ ТЕКСТОМ (в течение 2 минут).")
                        except Exception:
                            pass
                        
                        event = asyncio.Event()
                        adobe_2fa_state[user_id] = {"event": event, "code": ""}
                        
                        try:
                            await asyncio.wait_for(event.wait(), timeout=120.0)
                            received_code = adobe_2fa_state[user_id]["code"].strip()
                            
                            code_field = page.locator('input[type="text"], input[type="number"], input[name*="code"]').first
                            await code_field.fill(received_code)
                            await asyncio.sleep(1)
                            
                            sub_btn = page.locator('button:has-text("Продолжить"), button[type="submit"], button:has-text("Submit")').first
                            await sub_btn.click()
                            
                            await asyncio.sleep(5)
                        except asyncio.TimeoutError:
                            raise RuntimeError("Таймаут: вы не успели прислать код за 2 минуты.")
                        finally:
                            adobe_2fa_state.pop(user_id, None)

                    if await page.locator('input[type="password"], #password').count() > 0:
                        print("Adobe: Вводим пароль...")
                        pwd_field = page.locator('input[type="password"], #password').first
                        await pwd_field.fill(ADOBE_PASSWORD)
                        await asyncio.sleep(1)
                        
                        login_btn = page.locator('button:has-text("Продолжить"), button:has-text("Войти"), button:has-text("Sign in"), button:has-text("Continue"), button[type="submit"]').first
                        await login_btn.click()

                    print("Adobe: Ждем завершения редиректов (до 20 сек)...")
                    for _ in range(20):
                        if "enhance" in page.url and "auth" not in page.url and "ims" not in page.url:
                            break
                        if "adobelogin" in page.url or "auth" in page.url:
                            try:
                                await page.evaluate("""() => {
                                    const targets = Array.from(document.querySelectorAll('button, a, span'));
                                    const interstitialBtn = targets.find(el => {
                                        const t = el.innerText ? el.innerText.trim().toLowerCase() : '';
                                        return t === 'не сейчас' || t === 'пропустить' || t === 'not now' || t === 'skip' || t === 'напомнить позже' || t === 'да' || t === 'yes' || t === 'продолжить';
                                    });
                                    if (interstitialBtn) interstitialBtn.click();
                                }""")
                            except:
                                pass
                        await asyncio.sleep(1)
                    await asyncio.sleep(3)

                if "enhance" not in page.url or "auth" in page.url:
                    print("Adobe: Принудительный переход на панель Enhance...")
                    await page.goto("https://podcast.adobe.com/enhance", timeout=60000)
                    await page.wait_for_load_state("domcontentloaded")
                    await asyncio.sleep(5)
                    
                if "auth" in page.url or "login" in page.url:
                    raise RuntimeError("❌ Бот застрял на странице авторизации! Проверьте правильность ADOBE_EMAIL и ADOBE_PASSWORD.")

                # =================================================================
                # ПРЕДВАРИТЕЛЬНЫЙ СКРИНШОТ
                # =================================================================
                print("Adobe: Кабинет открыт. Делаю снимок экрана перед загрузкой...")
                try:
                    await page.screenshot(path="/tmp/adobe_before_upload.png", timeout=5000)
                    await notify("/tmp/adobe_before_upload.png", "ℹ️ Экран перед загрузкой файла. Ищу кнопки...")
                except Exception as e:
                    print(f"Не удалось сделать предварительный скриншот: {e}")

                # =================================================================
                # МУЛЬТИ-ЗАГРУЗКА: ФРЕЙМЫ + СКРЫТЫЕ ПОЛЯ + DRAG&DROP
                # =================================================================
                uploaded = False
                
                # ШАГ 1: Поиск скрытых полей input[type="file"] ВЕЗДЕ (во всех фреймах)
                print("Шаг 1: Ищем скрытый input[type='file'] во всех фреймах...")
                for frame in page.frames:
                    try:
                        file_inputs = await frame.locator('input[type="file"]').all()
                        for inp in file_inputs:
                            try:
                                await inp.set_input_files(str(mp3_path), timeout=5000)
                                print(f"✅ Файл загружен в скрытый input (фрейм: {frame.url[:30]})")
                                uploaded = True
                                break
                            except Exception:
                                pass
                        if uploaded: break
                    except Exception:
                        continue

                # ШАГ 2: Системный File Chooser через клик (если скрытые поля не сработали)
                if not uploaded:
                    print("Шаг 2: Пробуем кликнуть по кнопке Choose files...")
                    for frame in page.frames:
                        try:
                            target = frame.locator('text=/Choose files|Выбрать|Загрузить|Upload/i').first
                            if await target.count() > 0:
                                async with page.expect_file_chooser(timeout=5000) as fc_info:
                                    await target.click(force=True)
                                file_chooser = await fc_info.value
                                await file_chooser.set_files(str(mp3_path))
                                print("✅ Файл загружен через системное окно!")
                                uploaded = True
                                break
                        except Exception:
                            continue

                # ШАГ 3: Ультимативный Drag-and-Drop (насильный сброс файла)
                if not uploaded:
                    print("Шаг 3: Активируем Drag-and-Drop...")
                    try:
                        with open(mp3_path, "rb") as f:
                            file_base64 = base64.b64encode(f.read()).decode("utf-8")
                        
                        await page.evaluate("""async ([base64, filename, mime]) => {
                            const dataUrl = `data:${mime};base64,${base64}`;
                            const res = await fetch(dataUrl);
                            const blob = await res.blob();
                            const file = new File([blob], filename, { type: mime });
                            const dt = new DataTransfer();
                            dt.items.add(file);
                            
                            const enterEvent = new DragEvent('dragenter', { bubbles: true, cancelable: true, dataTransfer: dt });
                            const overEvent = new DragEvent('dragover', { bubbles: true, cancelable: true, dataTransfer: dt });
                            const dropEvent = new DragEvent('drop', { bubbles: true, cancelable: true, dataTransfer: dt });
                            
                            document.body.dispatchEvent(enterEvent);
                            document.body.dispatchEvent(overEvent);
                            document.body.dispatchEvent(dropEvent);
                        }""", [file_base64, mp3_path.name, "audio/mpeg"])
                        
                        print("✅ Файл физически сброшен в окно браузера!")
                        uploaded = True
                        await asyncio.sleep(4)
                    except Exception as e:
                        print(f"Шаг 3 завершился ошибкой: {e}")

                if not uploaded:
                    raise RuntimeError("Adobe заблокировал интерфейс загрузки. Все методы исчерпаны.")
                # =================================================================

                await asyncio.sleep(3)

                for frame in page.frames:
                    for sel in ['button:has-text("Enhance speech")', 'button:has-text("Enhance")', 'button[type="submit"]']:
                        try:
                            btn = frame.locator(sel).first
                            if await btn.count() > 0: await btn.evaluate("node => node.click()"); break
                        except Exception: continue

                print("Adobe: Ожидание скачивания готового аудио (до 5 минут)...")
                download_locator = None
                for i in range(60): 
                    await asyncio.sleep(5)
                    for frame in page.frames:
                        dl_btn = frame.locator('button:has-text("Download"), a:has-text("Download"), button:has-text("Скачать"), a:has-text("Скачать")').last
                        if await dl_btn.count() > 0:
                            download_locator = dl_btn
                            break
                    if download_locator:
                        break

                if not download_locator:
                    raise RuntimeError("Adobe не отдал файл за 5 минут")

                async with page.expect_download(timeout=120000) as dl_info:
                    await download_locator.evaluate("node => node.click()")
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
                original_error = str(e)[:150]
                print(f"Сработал except: {original_error}")
                try:
                    await page.screenshot(path="/tmp/adobe_error.png", timeout=5000)
                    await notify("/tmp/adobe_error.png", f"Критический сбой Adobe:")
                except Exception:
                    pass
                raise RuntimeError(f"{original_error}")
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
            await page.wait_for_load_state("domcontentloaded")
            await asyncio.sleep(4)

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
                    if await btn.count() > 0: await btn.evaluate("node => node.click()"); break
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
                    if await btn.count() > 0: await btn.evaluate("node => node.click()"); published = True; break
                except Exception: continue

            if not published: raise RuntimeError("Кнопка публикации mave не найдена")
            await asyncio.sleep(5)
            return True
        except Exception as e:
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

        studio_mp3 = await enhance_audio(mp3, user_id=user_id, send_screenshot=send_screenshot)

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

async def handle_global_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id in adobe_2fa_state:
        code_text = update.message.text.strip()
        adobe_2fa_state[user_id]["code"] = code_text
        adobe_2fa_state[user_id]["event"].set() 
        await update.message.reply_text("✅ Код принят, отправляю на проверку в Adobe...")
        return

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
        await upload_to_mave(data["mp3"], data["title"], data["description"])
        caption = f"✅ *Успешно выгружено!*\n\n_{data['title']}_"
        await query.edit_message_text(caption, parse_mode=ParseMode.MARKDOWN)
        pending.pop(user_id, None)
    except Exception as e:
        await query.edit_message_text(f"❌ Ошибка Mave: {e}")

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
    threading.Thread(target=run_dummy_server, daemon=True).start()
    
    print("🚀 Запускаем Telegram-бота...")
    try:
        app = Application.builder().token(TELEGRAM_TOKEN).build()
        
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_global_text), group=-1)

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
        
        print("✅ Бот успешно подключился к серверам Telegram!")
        app.run_polling()
        
    except Exception as e:
        print(f"\n❌❌❌ КРИТИЧЕСКАЯ ОШИБКА ЗАПУСКА: {e} ❌❌❌")
        print("Бот остановлен. Проверьте, не запущен ли он на вашем компьютере!")
        while True:
            time.sleep(60)

if __name__ == "__main__":
    main()
