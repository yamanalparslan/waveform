FROM python:3.12-slim AS base

WORKDIR /app
# pip'in varsayılan soket zaman aşımı 15 sn. pandas/scipy/numpy tekerlekleri
# onlarca MB olduğu için yavaş ya da dalgalı bağlantıda indirme düzenli olarak
# `ReadTimeoutError` ile düşüyor ve tüm derleme başarısız oluyor. Zaman aşımını
# ve yeniden deneme sayısını yükseltmek bunu giderir; hızlı bağlantıda hiçbir
# maliyeti yok (bekleme yalnızca hata durumunda devreye giriyor).
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 \
    PIP_DEFAULT_TIMEOUT=120 PIP_RETRIES=8

COPY pyproject.toml README.md alembic.ini ./
COPY src ./src
COPY alembic ./alembic
RUN pip install .

# Varsayılan komut worker; beat ve (Faz 6'da) api compose'ta command ile ezilir
CMD ["celery", "-A", "luminmind.workers.celery_app", "worker", "--loglevel=INFO"]
