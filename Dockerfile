FROM python:3.10-slim
RUN apt-get update && apt-get install -y ffmpeg
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN playwright install --with-deps chromium
COPY . .
CMD sh -c "python -m http.server $PORT & python bot.py"
