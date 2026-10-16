FROM public.ecr.aws/docker/library/python:3.11-slim

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

ENV PYTHONUNBUFFERED=1

# start.py runs gunicorn (web) and worker.py (render queue) together; see start.py
CMD ["python", "start.py"]
