# Официальный образ Microsoft с предустановленным Chromium
FROM mcr.microsoft.com/playwright/python:v1.60.0-jammy

WORKDIR /app

# Устанавливаем ffmpeg для аудио и xvfb для виртуального монитора!
RUN apt-get update && apt-get install -y ffmpeg xvfb

# Копируем и устанавливаем Python-библиотеки
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Копируем остальной код
COPY . .

# ЗАПУСК ЧЕРЕЗ ВИРТУАЛЬНЫЙ МОНИТОР (xvfb-run)
CMD ["xvfb-run", "-a", "python", "bot.py"]
