# 🎙️ Podcast Bot — запуск с нуля до работающего бота

**Что будет в итоге:** отправляешь голосовое в Telegram →
через 2-3 минуты получаешь MP3 + заголовок + описание →
нажимаешь одну кнопку → эпизод появляется в mave.digital →
автоматически уходит на Spotify, Apple Podcasts, Яндекс.Музыку.

---

## Шаг 1 — Получи 3 токена (15 минут)

### 1.1 Telegram Bot Token

1. Открой Telegram → найди **@BotFather**
2. Напиши `/newbot`
3. Дай имя боту: например `Vasily Podcast Bot`
4. Дай username: например `vasily_podcast_bot`
5. BotFather пришлёт токен вида:
   ```
   7123456789:AAHdqTcvCHfkHJDrMz2a5XVs-example
   ```
6. **Сохрани этот токен**

### 1.2 Твой Telegram ID

1. Найди в Telegram **@userinfobot**
2. Напиши `/start`
3. Он ответит твоим ID вида: `Id: 123456789`
4. **Сохрани это число**

### 1.3 OpenAI API Key (для Whisper — транскрипция)

1. Зайди на **platform.openai.com**
2. Зарегистрируйся / войди
3. Слева: **API Keys → Create new secret key**
4. Скопируй ключ вида `sk-proj-...`
5. **Сохрани**

> Цена: ~$0.006 за минуту аудио. Выпуск 5 минут = $0.03 (≈3 рубля)

---

## Шаг 2 — Узнай свой MAVE_PODCAST_ID (2 минуты)

1. Зайди на **app.mave.digital**
2. Открой свой подкаст
3. Посмотри на URL в браузере — он выглядит примерно так:
   ```
   https://app.mave.digital/podcasts/vasiliycrypto/episodes
   ```
4. Всё что после `/podcasts/` и до `/episodes` — это твой ID
5. В твоём случае это скорее всего: **`vasiliycrypto`**

---

## Шаг 3 — GitHub (5 минут)

### 3.1 Создай аккаунт
Зайди на **github.com** → Sign up (если нет аккаунта)

### 3.2 Создай репозиторий
1. Нажми зелёную кнопку **New** (или **+** → New repository)
2. Repository name: `podcast-bot`
3. Оставь **Private** (никто не увидит твои файлы)
4. Нажми **Create repository**

### 3.3 Загрузи файлы
1. На странице репозитория нажми **uploading an existing file**
2. Перетащи оба файла: `bot.py` и `requirements.txt`
3. Нажми **Commit changes**

---

## Шаг 4 — Railway (хостинг, бот работает 24/7)

### 4.1 Регистрация
1. Зайди на **railway.app**
2. Нажми **Login** → **Login with GitHub**
3. Разреши доступ

### 4.2 Создай проект
1. Нажми **New Project**
2. Выбери **Deploy from GitHub repo**
3. Выбери `podcast-bot`
4. Railway начнёт деплой (пока не добавили переменные — упадёт, это нормально)

### 4.3 Добавь переменные окружения
1. Кликни на созданный сервис
2. Вкладка **Variables**
3. Нажми **New Variable** и добавь каждую строку:

| Key | Value |
|-----|-------|
| `TELEGRAM_TOKEN` | токен от BotFather |
| `ALLOWED_USER_ID` | твой числовой ID из @userinfobot |
| `ANTHROPIC_API_KEY` | ключ из console.anthropic.com |
| `OPENAI_API_KEY` | ключ из platform.openai.com |
| `MAVE_EMAIL` | твой email от mave |
| `MAVE_PASSWORD` | твой пароль от mave |
| `MAVE_PODCAST_ID` | vasiliycrypto (или что нашёл в URL) |
| `AUPHONIC_USER` | логин auphonic (если есть) |
| `AUPHONIC_PASS` | пароль auphonic (если есть) |

### 4.4 Добавь команду запуска
1. Вкладка **Settings**
2. В поле **Start Command** напиши:
   ```
   pip install playwright && playwright install chromium && python bot.py
   ```
3. Нажми **Save**

### 4.5 Передеплой
1. Вкладка **Deployments**
2. Нажми **Redeploy**
3. Подожди 2-3 минуты
4. В логах должно появиться: `Бот v2 запущен...`

---

## Шаг 5 — Тест (1 минута)

1. Открой своего бота в Telegram (тот username что придумал в шаге 1.1)
2. Нажми **Start**
3. Запиши голосовое сообщение (10-30 секунд)
4. Отправь его боту
5. Бот ответит: `⏳ Обрабатываю...`
6. Через 1-3 минуты придёт MP3 + кнопки:
   - **✅ Опубликовать в mave**
   - **✏️ Изменить**
   - **❌ Отмена**

---

## Если что-то пошло не так

**Бот не отвечает** → проверь Railway логи: вкладка Deployments → кликни на деплой → Logs

**Ошибка загрузки в mave** → Railway сохранит скриншот. Напиши мне в Claude — пришли текст ошибки из логов, починим за 5 минут.

**Auphonic не работает** → не страшно, просто удали из Variables строки AUPHONIC_USER и AUPHONIC_PASS. Бот будет работать без улучшения звука.

---

## Итоговая схема работы

```
Ты: отправляешь голосовое
       ↓
Бот: скачивает → конвертирует → Auphonic (студийное) → Whisper (текст) → Claude (заголовок)
       ↓
Бот: присылает MP3 + "Опубликовать / Изменить / Отмена"
       ↓
Ты: нажимаешь ✅
       ↓
Бот: Playwright логинится в mave → заливает MP3 → вставляет текст → публикует
       ↓
mave: сам рассылает на Spotify + Apple Podcasts + Яндекс.Музыку
```

**Твоё участие: 2 действия** — отправить голосовое и нажать одну кнопку.
