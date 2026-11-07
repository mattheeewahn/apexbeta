FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Only the files the app needs (see .dockerignore)
COPY server.py index.html admin.html terms.txt apex.png apex2.png default-avatar.png ./

# Socket.IO needs exactly ONE worker process; concurrency comes from threads.
CMD ["sh", "-c", "gunicorn -k gthread -w 1 --threads 100 --timeout 120 -b 0.0.0.0:${PORT:-8000} server:app"]
