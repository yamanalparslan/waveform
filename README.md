# LuminMind

Bulut API tabanlı GES (güneş enerji santrali) izleme, dijital ikiz ve BESS/enerji arbitrajı platformu.

- **Durum:** planlama aşaması — detaylı yol haritası için [PLAN.md](PLAN.md)
- **Stack:** Python 3.11+, FastAPI, Celery + Redis, PostgreSQL, InfluxDB, pvlib, pandas

## Fazlar

1. Bulut API adaptörleri (Huawei FusionSolar, SMA — mock-first ingestion)
2. Hibrit veritabanı (PostgreSQL meta + InfluxDB zaman serileri)
3. GES dijital ikiz motoru (pvlib + Open-Meteo)
4. BESS modelleme ve BMS kalibrasyonu (Coulomb Counting + EKF)
5. Karşılaştırma motoru + EPİAŞ arbitraj algoritması
6. Backend API + docker-compose konteynerizasyonu
