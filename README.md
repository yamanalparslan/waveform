# LuminMind

Bulut API tabanlı GES (güneş enerji santrali) izleme, dijital ikiz ve BESS/enerji arbitrajı platformu.

- **Durum:** Faz 0 + Faz 1 tamamlandı — detaylı yol haritası için [PLAN.md](PLAN.md)
- **Stack:** Python 3.11+, FastAPI, Celery + Redis, PostgreSQL, InfluxDB, pvlib, pandas

## Fazlar

1. ✅ Bulut API adaptörleri (Huawei FusionSolar, SMA — mock-first ingestion)
2. Hibrit veritabanı (PostgreSQL meta + InfluxDB zaman serileri)
3. GES dijital ikiz motoru (pvlib + Open-Meteo)
4. BESS modelleme ve BMS kalibrasyonu (Coulomb Counting + EKF)
5. Karşılaştırma motoru + EPİAŞ arbitraj algoritması
6. Backend API + docker-compose konteynerizasyonu

## Geliştirme

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

ruff check .   # lint
mypy           # strict tip kontrolü
pytest         # birim testleri
```

## Çalıştırma (dikey dilim)

```bash
cp .env.example .env
docker compose up --build
# beat 15 dk'da bir mock adaptörden veri çekip normalize edilmiş kayıtları loglar
```

Gerçek üretici API'lerine geçiş: `.env` içinde `LM_USE_MOCK_VENDORS=false` yapıp
`HUAWEI_*` / `SMA_*` kimlik bilgilerini doldurmak yeterli — kod değişikliği gerekmez.
