"""
Podcast Bot v13.7
- ВОССТАНОВЛЕН обрезанный bot.py (на main файл обрывался на handle_audio и бот не стартовал)
- ИСПРАВЛЕН ВХОД ADOBE: больше не открываем маркетинговый лендинг /enhance
- Вход определяем по полю загрузки файла, а не по тексту Sign in (он есть даже в сессии)
- Ждём селекторы вместо длинных sleep; ищем поля в iframe
- Кликаем Continue после email/пароля, а не только Enter
- Сессия Adobe сохраняется в adobe_data/ и переживает пересборку Docker
- IMAP: быстрее опрос, таймаут сокета, корректное закрытие
"""
import os
import asyncio
import tempfile
import time
import re
import imaplib
import email
import html
import datetime
import socket
from pathlib import Path
from email.utils import parsedate_to_datetime

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, MessageHandler, CallbackQueryHandler,
    ConversationHandler, MessageHandler as MH,
    filters, ContextTypes, CommandHandler,
)
from telegram.constants import ParseMode
import openai
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout

socket.setdefaulttimeout(20)

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

DATA_DIR = Path(os.getenv("ADOBE_DATA_DIR", "adobe_data"))
STATE_FILE = str(DATA_DIR / "adobe_state.json")
PROFILE_DIR = str(DATA_DIR / "profile")
COVER_FILE = "cover.jpg"

ADOBE_APP_URL = "https://podcast.adobe.com/en/enhancespeech"
ADOBE_FALLBACK_URLS = [
    "https://podcast.adobe.com/en/enhance",
    "https://podcast.adobe.com/enhance",
]

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)

STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
window.chrome = window.chrome || { runtime: {} };
Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
"""

EMAIL_SELECTORS = [
    "#EmailPage-EmailField",
    'input[data-id="EmailPage-EmailField"]',
    'input[name="username"]',
    'input[type="email"]',
    'input[id*="email" i]',
    'input[name="email"]',
    'input[autocomplete="username"]',
    'input[autocomplete="email"]',
]

PASSWORD_SELECTORS = [
    "#PasswordPage-PasswordField",
    'input[data-id="PasswordPage-PasswordField"]',
    'input[type="password"]',
    'input[name="password"]',
    'input[autocomplete="current-password"]',
]

EMAIL_CONTINUE_SELECTORS = [
    'button[data-id="EmailPage-ContinueButton"]',
    'button[data-id="EmailPage-Continue"]',
    'button[type="submit"]',
]

PASSWORD_CONTINUE_SELECTORS = [
    'button[data-id="PasswordPage-ContinueButton"]',
    'button[data-id="PasswordPage-Continue"]',
    'button[type="submit"]',
]

SIGN_IN_TEXT = re.compile(
    r"^(sign in|sign up|log in|entrar|войти|get started|continue with email|"
    r"use your email|already have an account)$",
    re.IGNORECASE,
)

EDIT_TITLE, EDIT_DESC, EDIT_POST = range(3)
pending = {}
adobe_2fa_state = {}

STYLE_PROMPT = """
Ты — редактор подкаста Василия. Темы разные: крипта, ИИ, технологии, семья, жизнь.
По расшифровке придумай заголовок и описание.
Правила: конкретно, без воды.
Описание: 2 предложения, разговорный тон.
Ответ строго:
ЗАГОЛОВОК: [текст]
ОПИСАНИЕ: [текст]
ТРАНСКРИПЦИЯ:
"""

POST_TEMPLATE = """<b>НОВЫЙ ВЫПУСК УЖЕ НА ПЛОЩАДКАХ:
{title}

Залетайте по ссылкам ниже, выбирайте свою платформу и оставайтесь на связи! 👇

Слушайте там, где удобно:
🎵 <a href="https://music.yandex.ru/album/40139714">ЯНДЕКС МУЗЫКА</a>
🎵 <a href="https://open.spotify.com/show/34qEduYapGFILe2jyH1L0h?si=NQ9Lz4hXT0W-8pgOfY5vvg">SPOTIFY</a>
🎵 <a href="https://podcasts.apple.com/us/podcast/vasiliy-crypto-%D0%BD%D0%B5-%D0%B1%D1%83%D0%B4%D1%8C-%D1%82%D0%BE%D0%BB%D0%BF%D0%BE%D0%B9/id1865729420">Apple Podcasts</a></b>"""


# ── Планировщик (Бразилия UTC-3) ───────────────
def get_delay_seconds(target_time_str: str) -> int:
    tz_br = datetime.timezone(datetime.timedelta(hours=-3))
    now = datetime.datetime.now(tz_br)
    try:
        h, m = map(int, target_time_str.replace('.', ':').split(':'))
        if not (0 <= h <= 23 and 0 <= m <= 59):
            return -1
    except Exception:
        return -1
    target = now.replace(hour=h, minute=m, second=0, microsecond=0)
    if target < now:
        target += datetime.timedelta(days=1)
    return int((target - now).total_seconds())


async def delayed_publish(delay: int, chat_id: str, photo_path: str, text: str, bot):
    await asyncio.sleep(delay)
    try:
        if photo_path and os.path.exists(photo_path):
            with open(photo_path, "rb") as img:
                await bot.send_photo(chat_id=chat_id, photo=img, caption=text, parse_mode=ParseMode.HTML)
        else:
            await bot.send_message(chat_id=chat_id, text=text, parse_mode=ParseMode.HTML)
    except Exception as e:
        print(f"Ошибка отложенной публикации: {e}")


# ── IMAP Почта ─────────────────────────────────
async def fetch_adobe_code_from_email() -> str:
    if not EMAIL_USER or not EMAIL_PASS:
        return None

    def _fetch():
        mail = None
        try:
            mail = imaplib.IMAP4_SSL("imap.gmail.com", timeout=20)
            mail.login(EMAIL_USER, EMAIL_PASS)
            mail.select("inbox")

            mail_ids = []
            for sender in ["message@adobe.com", "adobe@email.adobe.com", "noreply@adobe.com"]:
                status, messages = mail.search(None, f'(FROM "{sender}")')
                ids = messages[0].split()
                if ids:
                    mail_ids = ids
                    print(f"IMAP: нашли письма от {sender}")
                    break

            if not mail_ids:
                print("IMAP: писем от Adobe не найдено")
                return None

            now_utc = datetime.datetime.now(datetime.timezone.utc)

            for mid in reversed(mail_ids[-5:]):
                status, msg_data = mail.fetch(mid, "(RFC822)")
                for response_part in msg_data:
                    if not isinstance(response_part, tuple):
                        continue
                    msg = email.message_from_bytes(response_part[1])

                    date_str = msg.get("Date", "")
                    try:
                        msg_date = parsedate_to_datetime(date_str)
                        if msg_date.tzinfo is None:
                            msg_date = msg_date.replace(tzinfo=datetime.timezone.utc)
                        age_minutes = (now_utc - msg_date).total_seconds() / 60
                        print(f"IMAP: письмо от {date_str}, возраст {age_minutes:.1f} мин")
                        if age_minutes > 8:
                            continue
                    except Exception as e:
                        print(f"IMAP: не смогли распарсить дату: {e}")

                    body = ""
                    if msg.is_multipart():
                        for part in msg.walk():
                            ct = part.get_content_type()
                            if ct == "text/plain":
                                body = part.get_payload(decode=True).decode(errors="ignore")
                                break
                            elif ct == "text/html" and not body:
                                body = part.get_payload(decode=True).decode(errors="ignore")
                    else:
                        body = msg.get_payload(decode=True).decode(errors="ignore")

                    print(f"IMAP: тело письма (первые 300 символов): {body[:300]}")
                    match = re.search(r"\b\d{6}\b", body)
                    if match:
                        print(f"IMAP: найден свежий код {match.group(0)}")
                        return match.group(0)
        except Exception as e:
            print(f"Ошибка IMAP: {e}")
        finally:
            if mail is not None:
                try:
                    mail.logout()
                except Exception:
                    pass
        return None

    print("IMAP: первая проверка через 5 секунд...")
    await asyncio.sleep(5)

    for attempt in range(12):
        print(f"IMAP: попытка {attempt + 1}/12...")
        code = await asyncio.to_thread(_fetch)
        if code:
            return code
        await asyncio.sleep(5)

    return None


# ── Утилиты ────────────────────────────────────
def ensure_data_dir():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    Path(PROFILE_DIR).mkdir(parents=True, exist_ok=True)
    for lock in Path(PROFILE_DIR).glob("Singleton*"):
        try:
            lock.unlink()
        except Exception:
            pass


async def run_dummy_server():
    async def handle_client(reader, writer):
        try:
            writer.write("HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\n\r\nBot alive!\r\n".encode("utf8"))
            await writer.drain()
        except Exception:
            pass
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass
    server = await asyncio.start_server(handle_client, "0.0.0.0", int(os.environ.get("PORT", 10000)))
    async with server:
        await server.serve_forever()


async def post_init(app: Application):
    asyncio.create_task(run_dummy_server())


async def download_voice(update, context) -> Path:
    voice = update.message.voice or update.message.audio
    f = await context.bot.get_file(voice.file_id)
    fd, path = tempfile.mkstemp(suffix=".ogg")
    os.close(fd)
    await f.download_to_drive(path)
    return Path(path)


async def to_mp3(src: Path) -> Path:
    dst = src.with_suffix(".mp3")
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-y", "-i", str(src),
        "-codec:a", "libmp3lame", "-b:a", "128k", "-ar", "44100", "-ac", "1",
        str(dst),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    await asyncio.wait_for(proc.communicate(), timeout=120)
    if not dst.exists() or dst.stat().st_size < 1000:
        raise RuntimeError("Ошибка ffmpeg")
    return dst


# ── Playwright helpers ─────────────────────────
async def locate_first(page, selectors):
    frames = [page] + [f for f in page.frames if f != page.main_frame]
    for frame in frames:
        for sel in selectors:
            try:
                loc = frame.locator(sel).first
                if await loc.count() > 0 and await loc.is_visible():
                    return loc
            except Exception:
                continue
    return None


async def click_text(page, pattern, timeout=4000):
    frames = [page] + [f for f in page.frames if f != page.main_frame]
    rx = re.compile(pattern, re.IGNORECASE)
    for frame in frames:
        try:
            loc = frame.locator("button, a, span, div[role='button']").filter(has_text=rx).first
            if await loc.count() > 0 and await loc.is_visible():
                await loc.click(timeout=timeout, force=True)
                return True
        except Exception:
            continue
    return False


async def dismiss_overlays(page):
    for pattern in (
        r"^accept all$", r"^accept$", r"^agree$", r"^got it$",
        r"^ok$", r"^allow$", r"^i agree$",
    ):
        try:
            if await click_text(page, pattern, timeout=1500):
                await asyncio.sleep(0.4)
        except Exception:
            pass
    try:
        btn = page.locator("#onetrust-accept-btn-handler").first
        if await btn.count() > 0 and await btn.is_visible():
            await btn.click(timeout=2000)
    except Exception:
        pass


async def fill_and_submit(page, field_selectors, continue_selectors, value, label):
    field = await locate_first(page, field_selectors)
    if not field:
        return False
    await field.click(timeout=5000)
    await asyncio.sleep(0.15)
    try:
        await field.fill("", timeout=3000)
    except Exception:
        await page.keyboard.press("Control+A")
        await page.keyboard.press("Backspace")
    await field.type(value.strip(), delay=70)
    await asyncio.sleep(0.3)
    clicked = False
    btn = await locate_first(page, continue_selectors)
    if btn:
        try:
            await btn.click(timeout=5000)
            clicked = True
        except Exception:
            pass
    if not clicked:
        await page.keyboard.press("Enter")
    print(f"login: {label} отправлен")
    return True


async def adobe_upload_ready(page) -> bool:
    try:
        loc = page.locator('input[type="file"]').first
        return await loc.count() > 0
    except Exception:
        return False


async def on_adobe_auth(page) -> bool:
    url = (page.url or "").lower()
    if any(x in url for x in ("auth.services.adobe", "adobelogin", "/ims/", "auth-light")):
        return True
    if await locate_first(page, EMAIL_SELECTORS + PASSWORD_SELECTORS):
        return True
    return False


async def goto_adobe_app(page):
    last_err = None
    for url in [ADOBE_APP_URL, *ADOBE_FALLBACK_URLS]:
        try:
            print(f"login: открываю {url}")
            await page.goto(url, timeout=45000, wait_until="domcontentloaded")
            await asyncio.sleep(1.5)
            await dismiss_overlays(page)
            return
        except Exception as e:
            last_err = e
            print(f"login: не открылся {url}: {e}")
    if last_err:
        raise last_err


# ── Ввод 2FA кода в поля Adobe ─────────────────
async def enter_adobe_code(page, final_code: str):
    await asyncio.sleep(0.8)

    selectors_to_try = [
        'input[maxlength="1"]',
        'input[autocomplete="one-time-code"]',
        'input[type="text"][maxlength="1"]',
        'input[type="number"][maxlength="1"]',
        'input[data-id*="Code"]',
    ]

    code_inputs = None
    frames = [page] + [f for f in page.frames if f != page.main_frame]
    for frame in frames:
        for selector in selectors_to_try:
            loc = frame.locator(selector)
            try:
                count = await loc.count()
            except Exception:
                continue
            if count >= 2:
                code_inputs = loc
                print(f"2FA: нашли {count} полей по селектору '{selector}'")
                break
        if code_inputs:
            break

    if code_inputs and await code_inputs.count() >= len(final_code):
        for i, digit in enumerate(final_code):
            try:
                field = code_inputs.nth(i)
                await field.click(timeout=4000)
                await asyncio.sleep(0.08)
                await field.fill(digit, timeout=4000)
                await asyncio.sleep(0.1)
            except Exception as e:
                print(f"2FA: ошибка ввода цифры {i}: {e}")
        print(f"2FA: код введён посимвольно ({len(final_code)} цифр)")
    else:
        print("2FA: отдельные ячейки не найдены, keyboard.type fallback...")
        fallback = await locate_first(page, [
            'input[autocomplete="one-time-code"]',
            'input[type="text"]',
            'input[type="number"]',
            'input[id*="code" i]',
        ])
        if fallback:
            await fallback.click(timeout=4000)
            await asyncio.sleep(0.15)
        await page.keyboard.type(final_code, delay=120)

    await asyncio.sleep(0.6)
    if not await click_text(page, r"continue|verify|submit|confirm|подтвердить"):
        await page.keyboard.press("Enter")
        print("2FA: нажали Enter для подтверждения")


async def detect_login_step(page) -> str:
    if await adobe_upload_ready(page) and not await on_adobe_auth(page):
        return "done"
    if await locate_first(page, PASSWORD_SELECTORS):
        return "password"
    html_page = ""
    try:
        html_page = (await page.content()).lower()
    except Exception:
        pass
    has_otp = await locate_first(page, [
        'input[maxlength="1"]',
        'input[autocomplete="one-time-code"]',
        'input[data-id*="Code"]',
    ])
    if has_otp or ("verify your identity" in html_page) or (
        "code" in html_page and await locate_first(page, ['input[type="text"]'])
    ):
        return "code_2fa"
    if await locate_first(page, EMAIL_SELECTORS):
        return "email"
    return "unknown"


async def finish_adobe_login(page, user_id, notify, shot):
    """Пройти IMS: email → пароль/2FA → студия. Не крутимся дольше ~90 сек на шаг."""
    if not ADOBE_EMAIL or not ADOBE_PASSWORD:
        raise RuntimeError("Не заданы ADOBE_EMAIL / ADOBE_PASSWORD")

    await dismiss_overlays(page)
    if await adobe_upload_ready(page) and not await on_adobe_auth(page):
        print("login: уже внутри студии")
        return

    if not await on_adobe_auth(page):
        await shot("/tmp/step2_need_login.png", "🔑 Сессии нет. Жму Sign in...")
        opened = await click_text(
            page,
            r"^(sign in|log in|entrar|войти|get started|sign up)$",
        )
        if not opened:
            # запасной клик по любой ссылке входа
            try:
                await page.locator('a[href*="auth"], a[href*="signin"], a[href*="login"]').first.click(timeout=4000)
                opened = True
            except Exception:
                pass
        if opened:
            try:
                await page.wait_for_url(
                    re.compile(r"auth|login|ims|signin", re.I),
                    timeout=15000,
                )
            except PlaywrightTimeout:
                await asyncio.sleep(1.5)

    email_sent = False
    password_sent = False
    deadline = time_mono() + 90

    while time_mono() < deadline:
        await dismiss_overlays(page)
        step = await detect_login_step(page)
        print(f"login: шаг={step} url={page.url[:90]}")

        if step == "done":
            return

        if "verify your identity" in (await safe_content(page)):
            await click_text(page, r"^continue$")
            await asyncio.sleep(1)
            continue

        if step == "email" and not email_sent:
            await shot("/tmp/step3_email.png", "✉️ Ввожу email Adobe...")
            email_sent = await fill_and_submit(
                page, EMAIL_SELECTORS, EMAIL_CONTINUE_SELECTORS, ADOBE_EMAIL, "email"
            )
            await asyncio.sleep(1.2)
            continue

        if step == "password" and not password_sent:
            await shot("/tmp/step4_password.png", "🔑 Ввожу пароль Adobe...")
            password_sent = await fill_and_submit(
                page, PASSWORD_SELECTORS, PASSWORD_CONTINUE_SELECTORS, ADOBE_PASSWORD, "password"
            )
            await asyncio.sleep(2)
            continue

        if step == "code_2fa":
            await shot("/tmp/step5_2fa.png", "📧 Adobe просит код. Смотрю почту...")
            auto_code = await fetch_adobe_code_from_email()
            if auto_code:
                await notify(None, f"✅ Код из почты: <code>{auto_code}</code>. Ввожу...")
                final_code = auto_code
            else:
                await shot("/tmp/step5_manual_2fa.png", "⚠️ Код не пришёл. Пришли 6 цифр ответом:")
                ev = asyncio.Event()
                adobe_2fa_state[user_id] = {"event": ev, "code": ""}
                await asyncio.wait_for(ev.wait(), timeout=180)
                final_code = adobe_2fa_state[user_id]["code"].strip()
            await enter_adobe_code(page, final_code)
            await asyncio.sleep(2)
            continue

        for btn_text in (
            r"^continue$", r"^done$", r"^skip$", r"^ok$", r"^got it$",
            r"don't ask again", r"stay signed in", r"^yes$", r"^no thanks$",
            r"^continue with email$", r"^use email$",
        ):
            if await click_text(page, btn_text, timeout=1200):
                await asyncio.sleep(0.8)
                break
        else:
            await asyncio.sleep(0.8)

    if await adobe_upload_ready(page):
        return

    await goto_adobe_app(page)
    if await adobe_upload_ready(page):
        return

    await shot("/tmp/login_stuck.png", f"⚠️ Вход завис. URL: {page.url[:80]}")
    raise RuntimeError(f"Adobe не пустил в студию. URL: {page.url}")


def time_mono() -> float:
    return time.monotonic()


async def safe_content(page) -> str:
    try:
        return (await page.content()).lower()
    except Exception:
        return ""


# ── Adobe Podcast ──────────────────────────────
async def enhance_audio(mp3: Path, user_id: int, notify) -> Path:
    adobe = mp3.parent / (mp3.stem + "_adobe.mp3")
    out = mp3.parent / (mp3.stem + "_studio.mp3")
    ensure_data_dir()

    page = None

    async def shot(path, caption):
        try:
            if page:
                await page.screenshot(path=path, timeout=8000)
            await notify(path, caption)
        except Exception as e:
            print(f"Ошибка скриншота: {e}")
            try:
                await notify(None, caption)
            except Exception:
                pass

    async with async_playwright() as pw:
        ctx = await pw.chromium.launch_persistent_context(
            user_data_dir=PROFILE_DIR,
            headless=False,
            viewport={"width": 1366, "height": 768},
            locale="en-US",
            user_agent=USER_AGENT,
            extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
            args=[
                "--no-sandbox",
                "--disable-gpu",
                "--disable-dev-shm-usage",
                "--renderer-process-limit=1",
                "--js-flags=--max-old-space-size=250",
                "--disable-blink-features=AutomationControlled",
            ],
        )
        await ctx.add_init_script(STEALTH_JS)
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()

        try:
            await goto_adobe_app(page)
            await shot("/tmp/step1_opened.png", "🌐 Открыл Adobe Enhance Speech. Проверяю сессию...")

            if not await adobe_upload_ready(page):
                await finish_adobe_login(page, user_id, notify, shot)
                if not await adobe_upload_ready(page):
                    await goto_adobe_app(page)

            try:
                await page.wait_for_selector('input[type="file"]', timeout=25000, state="attached")
            except PlaywrightTimeout:
                await shot("/tmp/no_upload.png", "⚠️ Поле загрузки так и не появилось")
                raise RuntimeError("Adobe открылся, но форма загрузки не появилась")

            try:
                await ctx.storage_state(path=STATE_FILE)
            except Exception as e:
                print(f"Не удалось сохранить storage_state: {e}")

            await shot("/tmp/step6_upload.png", "⏳ Вход готов. Заливаю аудио в Adobe...")

            file_input = page.locator('input[type="file"]').first
            await file_input.evaluate("node => node.style.display = 'block'")
            await file_input.set_input_files(str(mp3), timeout=15000)
            await asyncio.sleep(2)

            if not await click_text(page, r"enhance speech"):
                try:
                    await page.evaluate(
                        "const el = [...document.querySelectorAll('button')]"
                        ".find(e => /Enhance speech/i.test(e.innerText)); if (el) el.click();"
                    )
                except Exception:
                    pass

            await shot("/tmp/step7_processing.png", "⚙️ Adobe обрабатывает речь. Жду Download...")
            downloaded = False
            for _ in range(40):
                await asyncio.sleep(4)
                if await page.evaluate(
                    "const el = [...document.querySelectorAll('button, a')]"
                    ".find(e => /^Download$/i.test(e.innerText?.trim()));"
                    "return !!(el && el.offsetParent !== null);"
                ):
                    downloaded = True
                    break

            if not downloaded:
                await shot("/tmp/no_download.png", "⚠️ Кнопка Download не появилась")
                raise RuntimeError("Adobe не закончил обработку (нет Download)")

            await shot("/tmp/step8_download.png", "🎉 Готово. Скачиваю улучшенный файл...")
            async with page.expect_download(timeout=120000) as dl_info:
                await page.evaluate(
                    "const el = [...document.querySelectorAll('button, a')]"
                    ".find(e => /^Download$/i.test(e.innerText?.trim())); if (el) el.click();"
                )
            await dl_info.value.save_as(str(adobe))

            await notify(None, "🎵 Файл скачан. Нормализую громкость (loudnorm)...")
            r = await asyncio.create_subprocess_exec(
                "ffmpeg", "-y", "-i", str(adobe),
                "-af", "loudnorm=I=-16:TP=-1.5:LRA=11",
                "-codec:a", "libmp3lame", "-b:a", "128k", "-ar", "44100", "-ac", "1",
                str(out),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            await asyncio.wait_for(r.communicate(), timeout=120)
            return out if out.exists() else adobe

        finally:
            try:
                await ctx.close()
            except Exception:
                pass


# ── mave.digital ───────────────────────────────
async def upload_to_mave(mp3: Path, title: str, desc: str) -> bool:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage"],
        )
        ctx = await browser.new_context(viewport={"width": 1280, "height": 900}, locale="ru-RU")
        page = await ctx.new_page()
        try:
            await page.goto("https://app.mave.digital/login", timeout=30000, wait_until="domcontentloaded")
            email_field = page.locator('input[type="email"]').first
            await email_field.wait_for(state="visible", timeout=15000)
            await email_field.fill(MAVE_EMAIL, timeout=5000)
            await page.fill('input[type="password"]', MAVE_PASSWORD, timeout=5000)
            await page.click('button[type="submit"]', timeout=5000)
            try:
                await page.wait_for_url(re.compile(r"mave\.digital/(?!login)"), timeout=20000)
            except PlaywrightTimeout:
                if "login" in page.url:
                    raise RuntimeError("Mave не пустил: остались на /login. Проверь MAVE_EMAIL / MAVE_PASSWORD")

            await asyncio.sleep(1.5)
            await page.evaluate(
                "document.querySelectorAll('[id^=\"q-portal\"], .q-overlay').forEach(e=>e.remove());"
            )
            await page.locator("text=Добавить выпуск").first.click(timeout=15000)
            await asyncio.sleep(2)

            await page.set_input_files('input[type="file"]', str(mp3))
            await asyncio.sleep(1.5)

            for sel in ['button:has-text("Загрузить файл")', 'button:has-text("Загрузить")']:
                try:
                    if await page.locator(sel).count() > 0:
                        await page.locator(sel).first.click(timeout=5000)
                        break
                except Exception:
                    pass

            for _ in range(24):
                await asyncio.sleep(5)
                if any(x in await page.content() for x in ["Название выпуска", "upload-progress-done"]):
                    break

            try:
                title_loc = page.get_by_placeholder(re.compile(r"название", re.IGNORECASE)).first
                if await title_loc.count() > 0:
                    await title_loc.fill(title)
            except Exception:
                pass

            try:
                desc_loc = page.locator('.ProseMirror, textarea[name="description"]').first
                if await desc_loc.count() > 0:
                    await desc_loc.fill(desc)
            except Exception:
                pass

            for txt in ["Опубликовать", "Сохранить"]:
                try:
                    btn = page.locator(f'button:has-text("{txt}")').first
                    if await btn.count() > 0:
                        await btn.click(timeout=5000)
                        break
                except Exception:
                    pass

            await asyncio.sleep(4)
            return True
        finally:
            await ctx.close()
            await browser.close()


# ── Транскрипция и GPT ─────────────────────────
async def transcribe(mp3: Path) -> str:
    client = openai.AsyncOpenAI(api_key=OPENAI_KEY)
    with open(mp3, "rb") as f:
        res = await client.audio.transcriptions.create(
            model="whisper-1", file=f, language="ru"
        )
    return res.text


async def generate_metadata(transcript: str) -> tuple[str, str]:
    client = openai.AsyncOpenAI(api_key=OPENAI_KEY)
    resp = await client.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "user", "content": STYLE_PROMPT + transcript}],
        max_tokens=512,
        temperature=0.7
    )
    title = desc = ""
    for line in resp.choices[0].message.content.splitlines():
        c = line.strip().replace("**", "")
        if c.upper().startswith("ЗАГОЛОВОК:"):
            title = c[10:].strip()
        elif c.upper().startswith("ОПИСАНИЕ:"):
            desc = c[9:].strip()
    return title, desc


# ── Telegram handlers ──────────────────────────
def user_allowed(update: Update) -> bool:
    if not ALLOWED_USER_ID:
        return True
    return update.effective_user and update.effective_user.id == ALLOWED_USER_ID


async def handle_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not user_allowed(update):
        await update.message.reply_text("У вас нет доступа к этому боту.")
        return
    await update.message.reply_text(
        "🎙️ Podcast Bot v13.7 готов.\n"
        "Пришли голосовое или аудио — улучшу звук, сделаю заголовок и описание."
    )


async def handle_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not user_allowed(update):
        return
    file = await update.message.photo[-1].get_file()
    await file.download_to_drive(COVER_FILE)
    await update.message.reply_text("✅ Картинка-обложка для канала сохранена!")


async def handle_voice(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not user_allowed(update):
        return
    msg = await update.message.reply_text("⏳ [V13.7] Начинаю...")
    try:
        ogg = await download_voice(update, ctx)
        await msg.edit_text("🔄 [V13.7] Конвертирую в MP3...")
        mp3 = await to_mp3(ogg)

        await msg.edit_text("🎙️ [V13.7] Adobe Enhance Speech — вход и обработка...")

        async def notify(path, caption):
            try:
                if path and os.path.exists(path):
                    with open(path, "rb") as f:
                        await update.message.reply_photo(photo=f, caption=caption, parse_mode=ParseMode.HTML)
                elif caption:
                    await update.message.reply_text(caption, parse_mode=ParseMode.HTML)
            except Exception:
                pass

        studio = await enhance_audio(mp3, uid, notify)

        await msg.edit_text("📝 [V13.7] Whisper транскрипция...")
        text = await transcribe(studio)

        await msg.edit_text("✍️ [V13.7] GPT-4o заголовок...")
        title, desc = await generate_metadata(text)

        pending[uid] = {"mp3": studio, "title": title, "description": desc}
        await msg.delete()

        kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Опубликовать в mave", callback_data="publish"),
                InlineKeyboardButton("✏️ Изменить", callback_data="edit")
            ],
            [InlineKeyboardButton("❌ Отмена", callback_data="cancel")]
        ])
        with open(studio, "rb") as f:
            await update.message.reply_document(document=f, filename=f"{title[:40]}.mp3")
        await update.message.reply_text(
            f"🎙 *Готов*\n\n*Заголовок:*\n{title}\n\n*Описание:*\n{desc}",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb
        )
    except Exception as e:
        try:
            await msg.edit_text(f"❌ Ошибка: {str(e)}")
        except Exception:
            await update.message.reply_text(f"❌ Ошибка: {str(e)}")


async def handle_global_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return
    uid = update.effective_user.id
    text = update.message.text.strip()

    if uid in adobe_2fa_state and adobe_2fa_state[uid]["event"] and not adobe_2fa_state[uid]["event"].is_set():
        adobe_2fa_state[uid]["code"] = text
        adobe_2fa_state[uid]["event"].set()
        await update.message.reply_text("✅ Код принят вручную! Возвращаюсь в Adobe...")
        return

    if uid in pending and pending[uid].get("wait_time"):
        delay = get_delay_seconds(text)
        if delay < 0:
            await update.message.reply_text(
                "❌ Неверный формат. Напиши время в формате ЧЧ:ММ (например, 14:30):"
            )
            return

        pending[uid]["wait_time"] = False
        data = pending[uid]

        asyncio.create_task(
            delayed_publish(delay, CHANNEL_ID, COVER_FILE, data["post_text"], ctx.bot)
        )

        target_time = (
            datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=-3)))
            + datetime.timedelta(seconds=delay)
        ).strftime("%H:%M")
        await update.message.reply_text(
            f"✅ Супер! Пост запланирован на {target_time} (время Бразилии).\nЯ отправлю его автоматически."
        )
        pending.pop(uid, None)


async def btn_publish(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    data = pending.get(uid)
    if not data:
        return await q.edit_message_text("❌ Сессия устарела.")
    await q.edit_message_text("⏳ [V13.7] Загружаю в mave...")
    try:
        await upload_to_mave(data["mp3"], data["title"], data["description"])
        try:
            Path(data["mp3"]).unlink()
        except Exception:
            pass

        safe_title = html.escape(data["title"])
        post_text = POST_TEMPLATE.format(title=safe_title)
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
                await q.message.reply_photo(
                    photo=img,
                    caption=f"✅ <b>В Mave загружено!</b>\n\nЧерновик для канала:\n\n{post_text}",
                    parse_mode=ParseMode.HTML,
                    reply_markup=kb
                )
        else:
            await q.message.reply_text(
                f"✅ <b>В Mave загружено!</b>\n\nЧерновик для канала:\n\n{post_text}",
                parse_mode=ParseMode.HTML,
                reply_markup=kb
            )
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
                await ctx.bot.send_photo(
                    chat_id=CHANNEL_ID,
                    photo=img,
                    caption=data["post_text"],
                    parse_mode=ParseMode.HTML
                )
        else:
            await ctx.bot.send_message(
                chat_id=CHANNEL_ID,
                text=data["post_text"],
                parse_mode=ParseMode.HTML
            )

        await q.edit_message_reply_markup(reply_markup=None)
        await q.message.reply_text("🚀 Пост успешно улетел в канал!")
        pending.pop(uid, None)
    except Exception as e:
        await q.message.reply_text(
            f"❌ Ошибка публикации: {e}. Проверь, админ ли бот в канале!"
        )


async def btn_schedule(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    pending[q.from_user.id]["wait_time"] = True
    await q.message.reply_text(
        "🕒 Напиши время публикации (по Бразилии) в формате ЧЧ:ММ (например, 18:30):"
    )


async def btn_edit_post(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await q.message.reply_text("Отправь новый текст поста (или /skip):")
    return EDIT_POST


async def save_post(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if update.message.text != "/skip":
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
            await update.message.reply_photo(
                photo=img,
                caption=f"Обновленный черновик:\n\n{pending[uid]['post_text']}",
                parse_mode=ParseMode.HTML,
                reply_markup=kb
            )
    else:
        await update.message.reply_text(
            f"Обновленный черновик:\n\n{pending[uid]['post_text']}",
            parse_mode=ParseMode.HTML,
            reply_markup=kb
        )
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
    await q.edit_message_text(
        f"✏️ Заголовок:\n*{pending[q.from_user.id]['title']}*\n\nНовый (или /skip):",
        parse_mode=ParseMode.MARKDOWN
    )
    return EDIT_TITLE


async def edit_title(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if update.message.text != "/skip":
        pending[uid]["title"] = update.message.text
    await update.message.reply_text(
        f"📝 Описание:\n_{pending[uid]['description']}_\n\nНовое (или /skip):",
        parse_mode=ParseMode.MARKDOWN
    )
    return EDIT_DESC


async def edit_desc(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if update.message.text != "/skip":
        pending[uid]["description"] = update.message.text
    d = pending[uid]
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Опубликовать", callback_data="publish"),
            InlineKeyboardButton("❌ Отмена", callback_data="cancel")
        ]
    ])
    await update.message.reply_text(
        f"*Заголовок:* {d['title']}\n*Описание:* {d['description']}\n\nПубликуем?",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=kb
    )
    return ConversationHandler.END


async def btn_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    pending.pop(q.from_user.id, None)
    await q.edit_message_text("❌ Отменено.")
    return ConversationHandler.END


# ── Запуск ─────────────────────────────────────
def main():
    ensure_data_dir()
    print("🚀 [V13.7] Бот запускается...")
    if not TELEGRAM_TOKEN:
        raise RuntimeError("TELEGRAM_TOKEN не задан")
    app = Application.builder().token(TELEGRAM_TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("start", handle_start, block=False))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo, block=False))
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_global_text, block=False),
        group=1
    )

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
        fallbacks=[CallbackQueryHandler(btn_cancel, pattern="^cancel$")],
        per_chat=True
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
