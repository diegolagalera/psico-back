FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=5001

WORKDIR /app

# Instalar curl para healthchecks de Coolify
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

COPY . .

# Asegurar el directorio local de respaldo (para los PDFs semilla iniciales antes de activar R2)
RUN mkdir -p /app/protected_media

EXPOSE 5001

HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
    CMD curl -f http://localhost:${PORT:-5001}/api/health || exit 1

CMD ["sh", "-c", "gunicorn \"run:app\" --bind 0.0.0.0:${PORT:-5001} --workers 2 --threads 4 --timeout 300 --access-logfile - --error-logfile - --capture-output --log-level info"]

