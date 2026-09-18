FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Cloud Run injecte PORT (8080 par défaut) et attend que le conteneur écoute dessus.
CMD exec gunicorn app:app --workers 2 --threads 4 --timeout 60 --bind 0.0.0.0:${PORT:-8080}
