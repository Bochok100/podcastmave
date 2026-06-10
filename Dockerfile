# Официальный образ Microsoft с предустановленным Chromium
FROM mcr.microsoft.com/playwright/python:v1.60.0-jammy

WORKDIR /app

# Отключаем буферизацию логов, чтобы видеть ошибки мгновенно
ENV PYTHONUNBUFFERED=1

# Устанавливаем ffmpeg для аудио и xvfb для виртуального монитора
RUN apt-get update && apt-get install -y ffmpeg xvfb

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# ЗАПУСК ЧЕРЕЗ ВИРТУАЛЬНЫЙ МОНИТОР (xvfb-run) с флагом -u
CMD ["xvfb-run", "-a", "python", "-u", "bot.py"]
