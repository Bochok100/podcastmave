FROM python:3.10-slim

WORKDIR /app

# Устанавливаем ffmpeg для конвертации аудио и эквалайзера
RUN apt-get update && apt-get install -y ffmpeg

# Копируем список библиотек и устанавливаем их
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# СКАЧИВАЕМ FIREFOX И ВСЕ ЕГО ЗАВИСИМОСТИ (ДЛЯ ОБХОДА CLOUDFLARE)
RUN playwright install firefox --with-deps

# Копируем остальной код бота (bot.py)
COPY . .

# Команда для запуска бота
CMD ["python", "bot.py"]
