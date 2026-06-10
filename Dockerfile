# Официальный образ Microsoft с предустановленным Chromium
FROM mcr.microsoft.com/playwright/python:v1.60.0-jammy

WORKDIR /app

# Отключаем интерактивные вопросы и буферизацию логов
ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1

# Устанавливаем ffmpeg и xvfb
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg xvfb && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Железобетонный запуск через базовый shell:
# 1. rm -rf /tmp/.X* -> жестко чистим любые старые блокировки
# 2. Xvfb :99... &   -> запускаем монитор в фоне
# 3. sleep 2         -> даем ему 2 секунды на включение
# 4. python bot.py   -> запускаем нашего готового бота
CMD sh -c "rm -rf /tmp/.X* && Xvfb :99 -screen 0 1366x768x24 -ac & sleep 2 && DISPLAY=:99 python -u bot.py"
