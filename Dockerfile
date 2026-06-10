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

# Штатный запуск: -a автоматически подбирает дисплей и обходит проблему с .X99-lock
CMD xvfb-run -a -s "-screen 0 1366x768x24 -ac" python -u bot.py
