FROM python:3.11-slim AS base

WORKDIR /app
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install .

# Varsayılan komut worker; beat ve (Faz 6'da) api compose'ta command ile ezilir
CMD ["celery", "-A", "luminmind.workers.celery_app", "worker", "--loglevel=INFO"]
