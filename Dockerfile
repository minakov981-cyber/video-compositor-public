FROM python:3.11-slim

RUN apt-get update && apt-get install -y \
    ffmpeg \
    fontconfig \
    libfreetype6 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p uploads output temp fonts

EXPOSE 8080

# --workers 3: дозволяє 3 одночасні HTTP-запити (включно з file upload) замість 1.
# Число 3 — стартова точка для Hobby-плану: /compose блокує worker лише на прийомі
# файлу + create_compose_session, бо рендер іде в фоновий тред і worker одразу вільний.
# --timeout 120: знижено з 300, бо після переходу на Postgres-сесії /compose
# завершується швидко (upload + DB insert + старт треду); 300с більше не потрібен.
CMD ["gunicorn", "app:app", "--bind", "0.0.0.0:8080", "--workers", "3", "--timeout", "120"]
