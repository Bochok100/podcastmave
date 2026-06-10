# Официальный образ Microsoft с предустановленным Chromium
FROM mcr.microsoft.com/playwright/python:v1.60.0-jammy

WORKDIR /app

# КРИТИЧЕСКИ ВАЖНО: Отключаем любые интерактивные вопросы при установке (чтобы сборка не висла)
ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1

# Устанавливаем ffmpeg и xvfb в тихом режиме
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg xvfb && rm -rf /var/lib/apt/lists/*

# Устанавливаем библиотеки Python
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Копируем код
COPY . .

# Запуск через виртуальный монитор
CMD ["xvfb-run", "-a", "python", "-u", "bot.py"]
