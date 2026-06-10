FROM mcr.microsoft.com/playwright/python:v1.60.0-jammy

WORKDIR /app

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1
ENV DISPLAY=:99

RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg xvfb && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Xvfb стартует первым, ждём 2 секунды, потом бот
CMD sh -c "Xvfb :99 -screen 0 1366x768x24 -ac +extension GLX +render -noreset & sleep 2 && python -u bot.py"
