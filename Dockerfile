# Берем официальный образ ТОЧНО ТОЙ ВЕРСИИ, которую просит система (v1.60.0)
FROM mcr.microsoft.com/playwright/python:v1.60.0-jammy

WORKDIR /app

# Устанавливаем ffmpeg для работы со звуком
RUN apt-get update && apt-get install -y ffmpeg

# Копируем файл с библиотеками и устанавливаем их
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Копируем остальной код нашего бота
COPY . .

# Запускаем бота
CMD ["python", "bot.py"]
