# 🎙️ Podcast Bot — запуск с нуля

**Итог:** отправляешь голосовое → Adobe улучшает звук → Whisper транскрибирует → GPT-4o пишет заголовок → нажимаешь кнопку → публикуется в mave → уходит на Spotify, Apple, Яндекс.

---

## Шаг 1 — Токены и аккаунты

| Что | Где взять | Время |
|-----|-----------|-------|
| Telegram Bot Token | @BotFather → /newbot | 2 мин |
| Твой Telegram ID | @userinfobot → /start | 30 сек |
| OpenAI API Key | platform.openai.com → API Keys | 3 мин |
| Adobe ID (бесплатный) | adobe.com → Sign up | 3 мин |

---

## Шаг 2 — GitHub

1. github.com → **New repository** → `podcast-bot` → **Private**
2. Загрузи файлы: `bot.py`, `requirements.txt`, `Dockerfile`
3. **Commit changes**

---

## Шаг 3 — Render

1. render.com → **Login with GitHub**
2. **New → Web Service** → выбери `podcast-bot`
3. Environment: **Docker**
4. Нажми **Create Web Service**

### Переменные окружения (Variables):

| Key | Value |
|-----|-------|
| `TELEGRAM_TOKEN` | токен от BotFather |
| `ALLOWED_USER_ID` | твой числовой ID из @userinfobot |
| `OPENAI_API_KEY` | ключ из platform.openai.com |
| `MAVE_EMAIL` | email от mave.digital |
| `MAVE_PASSWORD` | пароль от mave.digital |
| `ADOBE_EMAIL` | email от adobe.com |
| `ADOBE_PASSWORD` | пароль от adobe.com |

---

## Шаг 4 — Тест

1. Открой бота в Telegram → `/start`
2. Запиши голосовое (10-60 сек) → отправь
3. Бот пишет статус каждого шага
4. Через 3-5 минут получишь MP3 + заголовок + описание
5. Нажми **✅ Опубликовать в mave**
6. Через 24ч (обычно быстрее) появится на площадках

---

## Если что-то сломалось

**Бот не отвечает** → Render → Logs

**Adobe не работает** → бот пришлёт скриншот страницы прямо в Telegram

**Conflict в логах** → два экземпляра бота запущены → проверь нет ли двух сервисов в Render с одним токеном
