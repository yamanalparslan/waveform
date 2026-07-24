FROM python:3.11-slim AS base

WORKDIR /app
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1

COPY pyproject.toml README.md alembic.ini ./
COPY src ./src
COPY alembic ./alembic
# Modern resolver eski pip'te bazen büyük paket ağaçlarında takılıyor;
# önce pip'i güncelle, sonra kur.
RUN pip install --upgrade pip && pip install .

# Varsayılan komut worker; beat ve (Faz 6'da) api compose'ta command ile ezilir
CMD ["celery", "-A", "luminmind.workers.celery_app", "worker", "--loglevel=INFO"]
