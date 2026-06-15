Podcast Bot v13.0 — The Time Lord
- ИНТЕГРАЦИЯ КАНАЛА: Автоматическая публикация постов в Telegram с обложкой.
- ШАБЛОН: Жестко заданный шаблон поста со ссылками, весь текст жирным шрифтом.
- ОТЛОЖЕННЫЙ ПОСТИНГ: Возможность задать время публикации по Бразилии (UTC-3).
Podcast Bot v13.1 — The Perfect Post
- ИСПРАВЛЕН БАГ ФОРМАТИРОВАНИЯ: Убран лишний html.escape, который ломал жирный шрифт.
- КЛИКАБЕЛЬНЫЕ ССЫЛКИ: Длинные URL спрятаны внутрь текста (гиперссылки).
- ОТЛОЖЕННЫЙ ПОСТИНГ: Бразилия (UTC-3).
"""

import os
@@ -61,7 +61,7 @@
ТРАНСКРИПЦИЯ:
"""

# ВЕСЬ ТЕКСТ В ТЕГАХ <b> ДЛЯ ЖИРНОГО ШРИФТА
# ИДЕАЛЬНЫЙ ШАБЛОН: Весь текст жирный (<b>...</b>), ссылки спрятаны в текст (<a href="...">...</a>)
POST_TEMPLATE = """<b>НОВЫЙ ВЫПУСК УЖЕ НА ПЛОЩАДКАХ:

{title}
@@ -70,11 +70,11 @@

Слушайте там, где удобно:

🎵 ЯНДЕКС МУЗЫКА - https://music.yandex.ru/album/40139714
🎵 <a href="https://music.yandex.ru/album/40139714">ЯНДЕКС МУЗЫКА</a>

🎵 SPOTIFY - https://open.spotify.com/show/34qEduYapGFILe2jyH1L0h?si=NQ9Lz4hXT0W-8pgOfY5vvg
🎵 <a href="https://open.spotify.com/show/34qEduYapGFILe2jyH1L0h?si=NQ9Lz4hXT0W-8pgOfY5vvg">SPOTIFY</a>

🎵 Apple Podcasts - https://podcasts.apple.com/us/podcast/vasiliy-crypto-%D0%BD%D0%B5-%D0%B1%D1%83%D0%B4%D1%8C-%D1%82%D0%BE%D0%BB%D0%BF%D0%BE%D0%B9/id1865729420</b>"""
🎵 <a href="https://podcasts.apple.com/us/podcast/vasiliy-crypto-%D0%BD%D0%B5-%D0%B1%D1%83%D0%B4%D1%8C-%D1%82%D0%BE%D0%BB%D0%BF%D0%BE%D0%B9/id1865729420">Apple Podcasts</a></b>"""

# ── Планировщик (Бразилия UTC-3) ───────────────
def get_delay_seconds(target_time_str: str) -> int:
@@ -196,382 +196,4 @@ async def shot(path, caption):
                for _ in range(20):
                    el = page.locator('input[type="email"], input[name="username"]').first
                    if await el.count() > 0 and await el.is_visible():
                        await el.focus(); await page.keyboard.press("Control+A"); await page.keyboard.press("Backspace"); await page.keyboard.type(ADOBE_EMAIL.strip(), delay=100); await page.keyboard.press("Enter")
                        break
                    await asyncio.sleep(1)

                await asyncio.sleep(5)
                step = "unknown"
                for _ in range(60):
                    html_page = await page.content()
                    if "verify your identity" in html_page.lower():
                        cb = page.locator('button').filter(has_text=re.compile(r"^Continue$", re.IGNORECASE)).first
                        if await cb.count() > 0: await cb.click(force=True); await asyncio.sleep(4); continue 
                    if await page.locator('input[type="password"]').count() > 0: step = "password"; break
                    if await page.locator('input[type="text"]').count() > 0 and "code" in html_page.lower(): step = "code_2fa"; break
                    if "enhance" in page.url and "auth" not in page.url: step = "done"; break
                    await asyncio.sleep(1)

                if step == "code_2fa":
                    await shot("/tmp/adobe_last.png", "📧 [V13.0] Проверяю Gmail на наличие кода...")
                    auto_code = await fetch_adobe_code_from_email()
                    if auto_code:
                        await notify(None, f"✅ Нашел код: {auto_code}")
                        final_code = auto_code
                    else:
                        await notify(None, "⚠️ Письмо не пришло. Пришли код вручную:")
                        ev = asyncio.Event()
                        adobe_2fa_state[user_id] = {"event": ev, "code": ""}
                        await asyncio.wait_for(ev.wait(), timeout=180)
                        final_code = adobe_2fa_state[user_id]["code"].strip()

                    try: await page.evaluate("document.querySelector('input[type=\"text\"]').focus();")
                    except: pass
                    await page.keyboard.type(final_code, delay=200)
                    await asyncio.sleep(2)
                    await page.keyboard.press("Enter")
                    
                    for _ in range(30):
                        await asyncio.sleep(1)
                        if "enhance" in page.url: step = "done"; break
                        if await page.locator('input[type="password"]').count() > 0: step = "password"; break

                if step == "password":
                    for _ in range(15):
                        el = page.locator('input[type="password"]').first
                        if await el.count() > 0:
                            await el.focus(); await page.keyboard.type(ADOBE_PASSWORD.strip(), delay=100); await asyncio.sleep(1); await page.keyboard.press("Enter")
                            break
                        await asyncio.sleep(1)
                    await asyncio.sleep(8)

                for _ in range(15):
                    if "enhance" in page.url and "auth" not in page.url: break
                    try: await page.evaluate("const el = [...document.querySelectorAll('button, a')].find(e => /skip|continue/i.test(e.innerText)); if (el) el.click();")
                    except: pass
                    await asyncio.sleep(1)

                if "enhance" not in page.url: await page.goto("https://podcast.adobe.com/enhance", timeout=60000); await asyncio.sleep(5)

            for _ in range(25):
                if await page.locator('input[type="file"]').count() > 0: break
                await asyncio.sleep(1)

            await ctx.storage_state(path=STATE_FILE)

            # ЗАГРУЗКА
            file_input = page.locator('input[type="file"]').first
            await file_input.evaluate("node => node.style.display = 'block'")
            await file_input.set_input_files(str(mp3), timeout=15000)
            await asyncio.sleep(3)
            
            try: await page.evaluate("const el = [...document.querySelectorAll('button')].find(e => /Enhance speech/i.test(e.innerText)); if (el) el.click();")
            except: pass

            # Ждём Download
            for _ in range(36):
                await asyncio.sleep(5)
                if await page.evaluate("const el = [...document.querySelectorAll('button, a')].find(e => /^Download$/i.test(e.innerText?.trim())); return el && el.offsetParent !== null;"): break

            async with page.expect_download(timeout=120000) as dl_info:
                await page.evaluate("const el = [...document.querySelectorAll('button, a')].find(e => /^Download$/i.test(e.innerText?.trim())); if (el) el.click();")
            await dl_info.value.save_as(str(adobe))

            r = await asyncio.create_subprocess_exec("ffmpeg", "-y", "-i", str(adobe), "-af", "loudnorm=I=-16:TP=-1.5:LRA=11", "-codec:a", "libmp3lame", "-b:a", "128k", "-ar", "44100", "-ac", "1", str(out), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            await asyncio.wait_for(r.communicate(), timeout=120)

            return out if out.exists() else adobe
        finally:
            try: await ctx.close(); await browser.close()
            except: pass

# ── mave.digital ───────────────────────────────
async def upload_to_mave(mp3: Path, title: str, desc: str) -> bool:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, args=["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage"])
        ctx = await browser.new_context(viewport={"width": 1280, "height": 900}, locale="ru-RU")
        page = await ctx.new_page()
        try:
            await page.goto("https://app.mave.digital/login")
            await page.fill('input[type="email"]', MAVE_EMAIL)
            await page.fill('input[type="password"]', MAVE_PASSWORD)
            await page.click('button[type="submit"]')
            await asyncio.sleep(4)
            await page.evaluate("document.querySelectorAll('[id^=\"q-portal\"], .q-overlay').forEach(e=>e.remove());")
            await page.locator('text=Добавить выпуск').first.click()
            await asyncio.sleep(3)
            
            await page.set_input_files('input[type="file"]', str(mp3))
            await asyncio.sleep(2)
            
            for sel in ['button:has-text("Загрузить файл")', 'button:has-text("Загрузить")']:
                try:
                    if await page.locator(sel).count() > 0: await page.locator(sel).first.click(); break
                except: pass

            for _ in range(30):
                await asyncio.sleep(5)
                if any(x in await page.content() for x in ["Название выпуска", "upload-progress-done"]): break

            try:
                title_loc = page.get_by_placeholder(re.compile(r"название", re.IGNORECASE)).first
                if await title_loc.count() > 0: await title_loc.fill(title)
            except: pass

            try:
                desc_loc = page.locator('.ProseMirror, textarea[name="description"]').first
                if await desc_loc.count() > 0: await desc_loc.fill(desc)
            except: pass

            for txt in ["Опубликовать", "Сохранить"]:
                try:
                    btn = page.locator(f'button:has-text("{txt}")').first
                    if await btn.count() > 0: await btn.click(); break
                except: pass

            await asyncio.sleep(5)
            return True
        finally:
            await browser.close()

# ── Транскрипция и GPT ─────────────────────────
async def transcribe(mp3: Path) -> str:
    client = openai.AsyncOpenAI(api_key=OPENAI_KEY)
    with open(mp3, "rb") as f:
        res = await client.audio.transcriptions.create(model="whisper-1", file=f, language="ru")
    return res.text

async def generate_metadata(transcript: str) -> tuple[str, str]:
    client = openai.AsyncOpenAI(api_key=OPENAI_KEY)
    resp = await client.chat.completions.create(model="gpt-4o", messages=[{"role": "user", "content": STYLE_PROMPT + transcript}], max_tokens=512, temperature=0.7)
    title = desc = ""
    for line in resp.choices[0].message.content.splitlines():
        c = line.strip().replace("**", "")
        if c.upper().startswith("ЗАГОЛОВОК:"): title = c[10:].strip()
        elif c.upper().startswith("ОПИСАНИЕ:"): desc = c[9:].strip()
    return title, desc

# ── Telegram handlers ──────────────────────────
async def handle_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if ALLOWED_USER_ID and update.effective_user.id != ALLOWED_USER_ID: return
    file = await update.message.photo[-1].get_file()
    await file.download_to_drive(COVER_FILE)
    await update.message.reply_text("✅ Картинка-обложка для канала сохранена!")

async def handle_voice(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if ALLOWED_USER_ID and uid != ALLOWED_USER_ID: return
    msg = await update.message.reply_text("⏳ [V13.0] Начинаю...")
    try:
        ogg = await download_voice(update, ctx)
        await msg.edit_text("🔄 [V13.0] MP3...")
        mp3 = await to_mp3(ogg)

        await msg.edit_text("🎙️ [V13.0] Adobe Podcast Enhance...")
        async def notify(path, caption):
            try:
                if path and os.path.exists(path):
                    with open(path, "rb") as f: await update.message.reply_photo(photo=f, caption=caption)
                elif caption:
                    await update.message.reply_text(caption)
            except: pass

        studio = await enhance_audio(mp3, uid, notify)

        await msg.edit_text("📝 [V13.0] Whisper транскрипция...")
        text = await transcribe(studio)

        await msg.edit_text("✍️ [V13.0] GPT-4o заголовок...")
        title, desc = await generate_metadata(text)

        pending[uid] = {"mp3": studio, "title": title, "description": desc}
        await msg.delete()

        kb = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Опубликовать в mave", callback_data="publish"), InlineKeyboardButton("✏️ Изменить", callback_data="edit")], [InlineKeyboardButton("❌ Отмена", callback_data="cancel")]])
        with open(studio, "rb") as f: await update.message.reply_document(document=f, filename=f"{title[:40]}.mp3")
        await update.message.reply_text(f"🎙 *Готов*\n\n*Заголовок:*\n{title}\n\n*Описание:*\n{desc}", parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
    except Exception as e:
        try: await msg.edit_text(f"❌ Ошибка: {str(e)}")
        except: await update.message.reply_text(f"❌ Ошибка: {str(e)}")

async def handle_global_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    text = update.message.text.strip()
    
    # Обработка кода 2FA
    if uid in adobe_2fa_state and adobe_2fa_state[uid]["event"] and not adobe_2fa_state[uid]["event"].is_set():
        adobe_2fa_state[uid]["code"] = text
        adobe_2fa_state[uid]["event"].set()
        await update.message.reply_text("✅ Код принят вручную! Возвращаюсь в Adobe...")
        return

    # Обработка ввода времени для планировщика
    if uid in pending and pending[uid].get("wait_time"):
        delay = get_delay_seconds(text)
        if delay < 0:
            await update.message.reply_text("❌ Неверный формат. Напиши время в формате ЧЧ:ММ (например, 14:30):")
            return
            
        pending[uid]["wait_time"] = False
        data = pending[uid]
        
        asyncio.create_task(delayed_publish(delay, CHANNEL_ID, COVER_FILE, data["post_text"], ctx.bot))
        
        target_time = (datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=-3))) + datetime.timedelta(seconds=delay)).strftime("%H:%M")
        await update.message.reply_text(f"✅ Супер! Пост запланирован на {target_time} (время Бразилии).\nЯ отправлю его автоматически.")
        pending.pop(uid, None)

async def btn_publish(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    data = pending.get(uid)
    if not data: return await q.edit_message_text("❌ Сессия устарела.")
    await q.edit_message_text("⏳ [V13.0] Загружаю в mave...")
    try:
        await upload_to_mave(data["mp3"], data["title"], data["description"])
        try: Path(data["mp3"]).unlink()
        except: pass
        
        # Шаблон с жирным текстом
        post_text = POST_TEMPLATE.format(title=data["title"])
        pending[uid]["post_text"] = post_text
        
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Опубликовать сейчас", callback_data="publish_now")],
            [InlineKeyboardButton("🕒 Запланировать (Бразилия)", callback_data="schedule")],
            [InlineKeyboardButton("✏️ Изменить текст", callback_data="edit_post")],
            [InlineKeyboardButton("❌ Отмена", callback_data="cancel_post")]
        ])
        
        await q.message.delete()
        if os.path.exists(COVER_FILE):
            with open(COVER_FILE, "rb") as img:
                await q.message.reply_photo(photo=img, caption=f"✅ <b>В Mave загружено!</b>\n\nЧерновик для канала:\n\n{post_text}", parse_mode=ParseMode.HTML, reply_markup=kb)
        else:
            await q.message.reply_text(f"✅ <b>В Mave загружено!</b>\n\nЧерновик для канала:\n\n{post_text}", parse_mode=ParseMode.HTML, reply_markup=kb)

    except Exception as e:
        await q.message.reply_text(f"❌ Ошибка Mave: {e}")

async def btn_publish_now(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    data = pending.get(uid)
    
    if not CHANNEL_ID:
        return await q.message.reply_text("❌ Ошибка: Не настроен CHANNEL_ID в сервере!")

    try:
        if os.path.exists(COVER_FILE):
            with open(COVER_FILE, "rb") as img:
                await ctx.bot.send_photo(chat_id=CHANNEL_ID, photo=img, caption=data['post_text'], parse_mode=ParseMode.HTML)
        else:
            await ctx.bot.send_message(chat_id=CHANNEL_ID, text=data['post_text'], parse_mode=ParseMode.HTML)
        
        await q.edit_message_reply_markup(reply_markup=None)
        await q.message.reply_text("🚀 Пост успешно улетел в канал!")
        pending.pop(uid, None)
    except Exception as e:
        await q.message.reply_text(f"❌ Ошибка публикации: {e}. Проверь, админ ли бот в канале!")

async def btn_schedule(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    pending[q.from_user.id]["wait_time"] = True
    await q.message.reply_text("🕒 Напиши время публикации (по Бразилии) в формате ЧЧ:ММ (например, 18:30):")

async def btn_edit_post(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await q.message.reply_text("Отправь новый текст поста (или /skip):")
    return EDIT_POST

async def save_post(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if update.message.text != "/skip":
        # Если юзер ввел новый текст, оборачиваем его в жирный шрифт, чтобы стиль сохранился
        new_text = html.escape(update.message.text)
        pending[uid]["post_text"] = f"<b>{new_text}</b>"

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Опубликовать сейчас", callback_data="publish_now")],
        [InlineKeyboardButton("🕒 Запланировать (Бразилия)", callback_data="schedule")],
        [InlineKeyboardButton("✏️ Изменить текст", callback_data="edit_post")],
        [InlineKeyboardButton("❌ Отмена", callback_data="cancel_post")]
    ])
    
    if os.path.exists(COVER_FILE):
        with open(COVER_FILE, "rb") as img:
            await update.message.reply_photo(photo=img, caption=f"Обновленный черновик:\n\n{pending[uid]['post_text']}", parse_mode=ParseMode.HTML, reply_markup=kb)
    else:
        await update.message.reply_text(f"Обновленный черновик:\n\n{pending[uid]['post_text']}", parse_mode=ParseMode.HTML, reply_markup=kb)
    return ConversationHandler.END

async def btn_cancel_post(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    pending.pop(q.from_user.id, None)
    await q.edit_message_reply_markup(reply_markup=None)
    await q.message.reply_text("✅ Отменено. Выпуск остался только в Mave.")

async def btn_edit(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await q.edit_message_text(f"✏️ Заголовок:\n*{pending[q.from_user.id]['title']}*\n\nНовый (или /skip):", parse_mode=ParseMode.MARKDOWN)
    return EDIT_TITLE

async def edit_title(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if update.message.text != "/skip": pending[uid]["title"] = update.message.text
    await update.message.reply_text(f"📝 Описание:\n_{pending[uid]['description']}_\n\nНовое (или /skip):", parse_mode=ParseMode.MARKDOWN)
    return EDIT_DESC

async def edit_desc(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if update.message.text != "/skip": pending[uid]["description"] = update.message.text
    d = pending[uid]
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Опубликовать", callback_data="publish"), InlineKeyboardButton("❌ Отмена", callback_data="cancel")]])
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
    print("🚀 [V13.0] Бот запускается...")
    app = Application.builder().token(TELEGRAM_TOKEN).post_init(post_init).build()
    
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo, block=False))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_global_text, block=False), group=1)
    
    conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(btn_edit, pattern="^edit$"),
            CallbackQueryHandler(btn_edit_post, pattern="^edit_post$")
        ],
        states={
            EDIT_TITLE: [MH(filters.TEXT | filters.COMMAND, edit_title)], 
            EDIT_DESC: [MH(filters.TEXT | filters.COMMAND, edit_desc)],
            EDIT_POST: [MH(filters.TEXT | filters.COMMAND, save_post)],
        },
        fallbacks=[CallbackQueryHandler(btn_cancel, pattern="^cancel$")], per_chat=True
    )
    
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_voice, block=False))
    app.add_handler(conv)
    app.add_handler(CallbackQueryHandler(btn_publish, pattern="^publish$"))
    app.add_handler(CallbackQueryHandler(btn_cancel, pattern="^cancel$"))
    app.add_handler(CallbackQueryHandler(btn_publish_now, pattern="^publish_now$"))
    app.add_handler(CallbackQueryHandler(btn_schedule, pattern="^schedule$"))
    app.add_handler(CallbackQueryHandler(btn_cancel_post, pattern="^cancel_post$"))
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
