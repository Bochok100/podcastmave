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

# Бронебойный запуск: удаляем лок -> стартуем монитор -> ждем 2 сек -> запускаем бота
CMD ["/bin/bash", "-c", "rm -f /tmp/.X99-lock; Xvfb :99 -screen 0 1366x768x24 -ac & sleep 2; DISPLAY=:99 python -u bot.py"]
