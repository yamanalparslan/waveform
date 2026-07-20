# LuminMind — GES İzleme, Dijital İkiz ve BESS/Arbitraj Platformu Planı

> Bulut API tabanlı güneş enerji santrali (GES) izleme, dijital ikiz, BESS modelleme ve
> enerji arbitrajı platformu. Bu doküman implementasyon öncesi onaya sunulan yol haritasıdır.

**Onaylanan başlangıç kararları** (soru-cevap sonucu):

| Konu | Karar |
|---|---|
| İnvertör API'leri (Huawei/SMA) | Gerçek kimlik bilgisi yok → **mock/kayıtlı JSON yanıtlarla başla**, adaptör arayüzü gerçek API'ye birebir uysun |
| Hava durumu / ışınım | **Open-Meteo** (ücretsiz, anahtarsız, GHI/DNI/DHI + tahmin) |
| EPİAŞ Şeffaflık (GÖP/GİP) | Servis hesabı yok → **örnek fiyat verisiyle mock başla** |
| BESS hücre verisi | Gerçek 8S 21700 CSV'leri henüz yok → **sentetik profillerle doğrula**, kalibrasyon gerçek veri gelince |

**Teknoloji kısıtları:** Python 3.11+, tam tip anotasyonu, Pydantic v2, SQLAlchemy 2.x (async),
FastAPI, Celery + Redis, PostgreSQL, InfluxDB 2.x, `httpx`, `pvlib`, `pandas`. Dış bağımlılık minimal.

---

## 1. Mimari Genel Bakış

### 1.1 Servis Diyagramı

```mermaid
flowchart TB
    subgraph external [Dış Servisler]
        HUAWEI[Huawei FusionSolar\nNorthbound API]
        SMA[SMA Sunny Portal\nennexOS API]
        OM[Open-Meteo\nHava/Işınım API]
        EPIAS[EPİAŞ Şeffaflık\nGÖP/GİP Fiyatları]
    end

    subgraph compose [docker-compose]
        BEAT[Celery Beat\nzamanlayıcı]
        WORKER[Celery Worker\ningestion / twin / analytics]
        REDIS[(Redis\nbroker + cache)]
        PG[(PostgreSQL\nmeta + auth)]
        INFLUX[(InfluxDB\nzaman serileri)]
        API[FastAPI\nREST API]
    end

    DASH[Dashboard Ekibi\nWeb UI]

    BEAT -->|görev kuyruğu| REDIS
    REDIS --> WORKER
    WORKER -->|httpx async| HUAWEI
    WORKER -->|httpx async| SMA
    WORKER -->|httpx async| OM
    WORKER -->|httpx async| EPIAS
    WORKER -->|ölçümler| INFLUX
    WORKER -->|olaylar, planlar| PG
    API --> PG
    API --> INFLUX
    DASH -->|JWT| API
```

### 1.2 Veri Akışı

```mermaid
flowchart LR
    A[Üretici API'leri] -->|15 dk Celery Beat| B[Adapter\nfetch + auth + retry]
    B --> C[Normalizasyon\nPydantic kanonik model]
    C --> D[(InfluxDB\nraw ölçümler)]
    W[Open-Meteo] --> T[Dijital İkiz\npvlib beklenen üretim]
    T --> D
    D --> K[Karşılaştırma Motoru\nbeklenen vs gerçek]
    K -->|anomali olayları| P[(PostgreSQL)]
    E[EPİAŞ fiyatları] --> R[Arbitraj Motoru]
    D -->|SoC/SoH| R
    R -->|şarj/deşarj planı| P
    P --> F[FastAPI]
    D --> F
    F --> G[Dashboard]
```

---

## 2. Monorepo Dizin Yapısı

```
luminmind/
├── PLAN.md
├── README.md
├── pyproject.toml              # tek paket, uv/pip uyumlu; ruff + mypy ayarları
├── docker-compose.yml
├── Dockerfile                  # api ve worker aynı imaj, farklı command
├── .env.example
├── alembic.ini
├── alembic/                    # migration'lar
├── src/luminmind/
│   ├── config.py               # pydantic-settings ile tüm ayarlar
│   ├── adapters/               # Faz 1
│   │   ├── base.py             # VendorAdapter (ABC): authenticate(), fetch_plants(), fetch_telemetry()
│   │   ├── huawei.py           # FusionSolar Northbound (XSRF token akışı)
│   │   ├── sma.py              # ennexOS (OAuth2 + refresh)
│   │   ├── auth.py             # token yaşam döngüsü, OAuth2 refresh yardımcıları
│   │   ├── retry.py            # exponential backoff + rate-limit farkındalığı
│   │   └── normalize.py        # üretici JSON → kanonik Pydantic şemaları
│   ├── core/
│   │   ├── schemas/            # kanonik Pydantic v2 modelleri (TelemetryPoint, PlantMeta, …)
│   │   ├── models/             # SQLAlchemy 2.x ORM (aşağıdaki ER şeması)
│   │   ├── db.py               # async engine + session factory
│   │   ├── influx.py           # InfluxDB istemci sarmalayıcı (write/query API)
│   │   └── security.py         # şifre hash, JWT, Fernet token şifreleme
│   ├── twin/                   # Faz 3
│   │   ├── weather.py          # Open-Meteo istemcisi (ışınım + sıcaklık + rüzgar)
│   │   ├── plant_model.py      # pvlib ModelChain kurulumu (panel datasheet → PVSystem)
│   │   ├── components.py       # tek hat bileşenleri: invertör verimi, kablo/trafo kayıpları, sayaç noktası
│   │   └── expected.py         # beklenen 15 dk üretim serisi (pandas)
│   ├── bess/                   # Faz 4
│   │   ├── models.py           # hücre/paket/konteyner parametre veri sınıfları
│   │   ├── coulomb.py          # Coulomb Counting SoC
│   │   ├── ekf.py              # Genişletilmiş Kalman Filtresi (2-RC eşdeğer devre)
│   │   ├── soh.py              # kapasite/iç direnç bazlı SoH kestirimi
│   │   ├── calibration.py      # CSV'den parametre çıkarımı (OCV-SoC eğrisi, R0/R1/C1)
│   │   ├── scaling.py          # hücre → 8S paket → rack → MW konteyner ölçekleme
│   │   └── synthetic.py        # sentetik şarj/deşarj profili üreteci (doğrulama için)
│   ├── analytics/              # Faz 5
│   │   ├── comparison.py       # beklenen vs gerçek sapma serileri
│   │   ├── classifiers.py      # kural tabanlı + istatistiksel sınıflandırma (çatlak/gölge/kir)
│   │   └── arbitrage/
│   │       ├── epias.py        # Şeffaflık 2.0 istemcisi (GÖP MCP, GİP AOF)
│   │       ├── mock_prices.py  # örnek/kayıtlı fiyat verisi kaynağı
│   │       └── optimizer.py    # LP tabanlı şarj/deşarj zamanlama (scipy.optimize.linprog)
│   ├── api/                    # Faz 6 (iskeleti Faz 1'de kurulur)
│   │   ├── main.py             # FastAPI app factory
│   │   ├── deps.py             # DB session, auth dependency'leri
│   │   └── routers/            # auth, plants, timeseries, anomalies, bess, arbitrage
│   └── workers/
│       ├── celery_app.py       # Celery yapılandırması (Redis broker)
│       ├── schedule.py         # Beat takvimi (15 dk ingestion, saatlik twin, gece downsample…)
│       └── tasks/
│           ├── ingestion.py    # üretici verisi çek + normalize + Influx'a yaz
│           ├── twin.py         # beklenen üretim hesabı
│           ├── comparison.py   # anomali tespiti
│           ├── downsample.py   # 15 dk → saatlik/günlük agregasyon
│           └── arbitrage.py    # günlük fiyat çek + plan üret
├── tests/
│   ├── fixtures/               # mock üretici JSON'ları, örnek EPİAŞ fiyatları, sentetik hücre CSV
│   ├── unit/                   # modül bazlı birim testleri
│   └── integration/            # compose ayağa kalkmış Postgres/Influx ile uçtan uca dilim testleri
└── data/                       # gerçek hücre CSV'leri (gelince), panel datasheet'leri
```

---

## 3. Veri Modelleri

### 3.1 PostgreSQL ER Şeması

```mermaid
erDiagram
    USERS ||--o{ PLANTS : owns
    PLANTS ||--o{ INVERTERS : has
    PLANTS ||--o{ PV_ARRAYS : has
    PLANTS ||--o{ BATTERY_SYSTEMS : has
    PLANTS ||--o{ ANOMALY_EVENTS : reports
    PLANTS ||--o| VENDOR_CREDENTIALS : uses
    BATTERY_SYSTEMS ||--o{ ARBITRAGE_PLANS : schedules
    ARBITRAGE_PLANS ||--o{ ARBITRAGE_SLOTS : contains

    USERS {
        uuid id PK
        text email UK
        text hashed_password
        text role "admin | viewer"
        timestamptz created_at
    }
    PLANTS {
        uuid id PK
        uuid owner_id FK
        text name
        text vendor "huawei | sma"
        text vendor_plant_id
        float latitude
        float longitude
        float dc_capacity_kwp
        float ac_capacity_kw
        text timezone "Europe/Istanbul"
    }
    INVERTERS {
        uuid id PK
        uuid plant_id FK
        text vendor_device_id
        text model "ör. Schneider CL-60E"
        float ac_capacity_kw
        jsonb efficiency_curve
    }
    PV_ARRAYS {
        uuid id PK
        uuid plant_id FK
        uuid inverter_id FK
        int modules_per_string
        int strings
        float tilt_deg
        float azimuth_deg
        jsonb module_params "pdc0, gamma_pdc, NOCT…"
    }
    VENDOR_CREDENTIALS {
        uuid id PK
        uuid plant_id FK
        text vendor
        text auth_type "oauth2 | session"
        bytea encrypted_payload "Fernet ile şifreli"
        timestamptz token_expires_at
    }
    BATTERY_SYSTEMS {
        uuid id PK
        uuid plant_id FK
        text chemistry "NMC-21700"
        int cells_series
        int cells_parallel
        int pack_count
        float rated_energy_kwh
        float rated_power_kw
        jsonb model_params "EKF: R0,R1,C1,OCV eğrisi"
    }
    ANOMALY_EVENTS {
        uuid id PK
        uuid plant_id FK
        text kind "microcrack | shading | soiling"
        text severity "info | warning | critical"
        float deviation_pct
        timestamptz started_at
        timestamptz ended_at
        text status "open | acked | resolved"
        jsonb evidence "tetikleyen istatistikler"
    }
    ARBITRAGE_PLANS {
        uuid id PK
        uuid battery_id FK
        date plan_date
        text market "DAM | IDM"
        float expected_revenue_try
        timestamptz created_at
    }
    ARBITRAGE_SLOTS {
        uuid id PK
        uuid plan_id FK
        timestamptz slot_start
        text action "charge | discharge | idle"
        float power_kw
        float price_try_mwh
    }
```

Zaman serileri PostgreSQL'e **yazılmaz**; yalnızca meta veri, olaylar ve planlar burada tutulur.

### 3.2 InfluxDB Tasarımı

**Bucket'lar ve retention:**

| Bucket | İçerik | Retention |
|---|---|---|
| `lm_raw` | 15 dk ham ölçümler | 400 gün |
| `lm_hourly` | saatlik ortalama/toplam | 3 yıl |
| `lm_daily` | günlük enerji ve KPI'lar | sınırsız |

**Measurement'lar (`lm_raw`):**

| Measurement | Tag'ler | Field'lar |
|---|---|---|
| `pv_telemetry` | `plant_id`, `inverter_id`, `vendor` | `ac_power_kw`, `dc_power_kw`, `dc_voltage_v`, `dc_current_a`, `energy_total_kwh`, `temp_c` |
| `bess_telemetry` | `plant_id`, `battery_id` | `power_kw`, `voltage_v`, `current_a`, `soc_pct`, `soh_pct`, `temp_c` |
| `twin_expected` | `plant_id`, `model_version` | `expected_ac_kw`, `poa_irradiance_wm2`, `cell_temp_c` |
| `weather` | `plant_id`, `source=open-meteo` | `ghi_wm2`, `dni_wm2`, `dhi_wm2`, `temp_c`, `wind_ms`, `cloud_pct` |
| `market_prices` | `market` (DAM/IDM) | `price_try_mwh` |

**Downsampling kuralı (Celery gece görevi, Flux task yerine — tercih edilen kontrol bizde):**
- `lm_raw` → `lm_hourly`: güçler `mean()`, enerji `last()-first()`, saat başına.
- `lm_hourly` → `lm_daily`: günlük üretim (kWh), beklenen üretim, sapma yüzdesi, PR (performance ratio).
- Görev idempotent: aynı gün yeniden çalışırsa üzerine yazar.

---

## 4. API Sözleşmesi Taslağı (FastAPI)

Tüm endpoint'ler `/api/v1` altında; auth `Bearer JWT`. Şema özetleri Pydantic v2 modelleri olarak tanımlanacak.

| Metod | Path | Açıklama | Request → Response özeti |
|---|---|---|---|
| POST | `/auth/login` | Giriş | `{email, password}` → `{access_token, refresh_token}` |
| POST | `/auth/refresh` | Token yenile | `{refresh_token}` → `{access_token}` |
| GET | `/auth/me` | Profil | → `{id, email, role}` |
| GET | `/plants` | Tesis listesi | → `[{id, name, vendor, dc_capacity_kwp, last_seen_at, today_energy_kwh}]` |
| GET | `/plants/{id}` | Tesis detayı | → meta + invertörler + BESS özeti |
| GET | `/plants/{id}/timeseries` | Zaman serisi | `?metric=ac_power_kw&start&end&resolution=15m\|1h\|1d` → `[{ts, value}]` |
| GET | `/plants/{id}/comparison` | Beklenen vs gerçek | `?start&end` → `[{ts, actual_kw, expected_kw, deviation_pct}]` |
| GET | `/plants/{id}/anomalies` | Anomali raporları | `?status&kind&start&end` → `[AnomalyEvent]` |
| PATCH | `/anomalies/{id}` | Durum güncelle | `{status}` → `AnomalyEvent` |
| GET | `/plants/{id}/bess` | BESS anlık durum | → `{soc_pct, soh_pct, power_kw, temp_c, updated_at}` |
| GET | `/prices` | Piyasa fiyatları | `?market=DAM\|IDM&date` → `[{ts, price_try_mwh}]` |
| GET | `/plants/{id}/arbitrage/plan` | Günlük plan | `?date` → `{plan_date, expected_revenue_try, slots: [{slot_start, action, power_kw}]}` |
| POST | `/plants/{id}/arbitrage/replan` | Planı yeniden hesapla | → 202 + task id |
| GET | `/health` | Sağlık | → `{status, influx, postgres, redis}` |

Admin (ileride): tesis/kimlik bilgisi CRUD — Faz 6 kapsamında yalnızca `POST /plants` + `PUT /plants/{id}/credentials`.

---

## 5. Faz Bazlı Görev Kırılımı

Her faz **çalışan, test edilmiş bir dikey dilimle** biter. Bağımlılık zinciri:
**F0 → F1 → F2 → (F3, F4 paralel) → F5 → F6** (F6'nın iskeleti F1'den itibaren büyür).

### Faz 0 — İskelet (yarım gün)
- [x] `pyproject.toml` (ruff, mypy strict, pytest, pytest-asyncio yapılandırması)
- [x] Paket iskeleti (`src/luminmind/…`), `README.md`, `.env.example`, `.gitignore`
- [x] `docker-compose.yml`: PostgreSQL + InfluxDB + Redis servisleri (uygulama henüz yok)
- [x] CI: lint + typecheck + test çalıştıran basit GitHub Actions workflow

### Faz 1 — Bulut API Adaptörleri
- [x] Kanonik Pydantic şemaları: `TelemetryPoint`, `PlantMeta`, `DeviceMeta`
- [x] `VendorAdapter` ABC: `authenticate()`, `fetch_plants()`, `fetch_telemetry(since)`
- [x] Mock üretici JSON fixture'ları (Huawei `getDevRealKpi`, SMA measurements benzeri)
- [x] `HuaweiAdapter`: XSRF-token login akışı, oturum süresi yönetimi (respx ile mock test)
- [x] `SmaAdapter`: OAuth2 client-credentials + refresh akışı (mock test)
- [x] `retry.py`: exponential backoff, 429 rate-limit'te bekleme, hata loglama (structlog yerine stdlib `logging` — bağımlılık minimal)
- [x] Normalizasyon katmanı: üretici yanıtı → kanonik şema dönüşümleri + birim testleri
- [x] Celery app + Beat: 15 dk'da bir `ingest_all_plants` görevi (bu fazda çıktıyı log'a yazar)
- **Dilim çıktısı:** `docker compose up` → Beat 15 dk'da bir mock adaptörden veri çekip normalize edilmiş kaydı loglar.

### Faz 2 — Hibrit Veritabanı
- [x] SQLAlchemy 2.x async ORM modelleri (ER şemasındaki tablolar) + Alembic ilk migration
- [x] `core/influx.py`: async write/query sarmalayıcı, bucket bootstrap script'i
- [x] Ingestion görevi Influx'a yazacak şekilde bağlanır (log yerine `lm_raw`)
- [x] Downsampling Celery görevi (`lm_raw → lm_hourly → lm_daily`) + idempotentlik testi
- [x] Seed script: örnek kullanıcı + örnek tesis + mock kimlik bilgisi
- **Dilim çıktısı:** 15 dk'lık mock veriler Influx'ta; saatlik agregatlar gece görevi ile üretiliyor.

### Faz 3 — Dijital İkiz
- [x] Open-Meteo istemcisi (`httpx`, ışınım + sıcaklık + rüzgar; geçmiş + tahmin)
- [x] `plant_model.py`: PV_ARRAYS parametrelerinden `pvlib` PVSystem/ModelChain kurulumu
- [x] Kayıp zinciri: invertör verim eğrisi (Schneider), kablo/trafo sabit kayıpları, sayaç noktası (Köhler) — konfigüre edilebilir katsayılar
- [x] `expected.py`: 15 dk çözünürlükte beklenen AC üretim serisi (pandas)
- [x] Celery görevi: saatlik hava verisi çek → beklenen üretimi `twin_expected`'a yaz
- [x] Doğrulama: açık gökyüzü (clear-sky) senaryosunda pvlib referans sonuçlarıyla karşılaştırma testi
- **Dilim çıktısı:** her tesis için Influx'ta gerçek + beklenen üretim yan yana.

### Faz 4 — BESS Modelleme (F3 ile paralel yürüyebilir)
- [x] 1-RC (Thevenin) eşdeğer devre modeli veri sınıfları; OCV-SoC eğrisi temsili
- [x] `synthetic.py`: bilinen SoC yörüngeli sentetik şarj/deşarj profili üreteci
- [x] Coulomb Counting SoC + kümülatif hata testi
- [x] EKF implementasyonu (`numpy`): durum `[SoC, V_rc1]` (1-RC), ölçüm `V_terminal` — sentetik veride RMSE < %2 hedefi
- [x] SoH kestirimi: kapasite kayması + iç direnç artışı takibi
- [x] `calibration.py`: CSV formatı sözleşmesi (`t, V, I, T`) — gerçek 8S verisi gelince parametre fit akışı hazır
- [x] `scaling.py`: hücre → 8S paket → rack → MW konteyner (seri/paralel ölçekleme + verim/kayıp katsayıları)
- **Dilim çıktısı:** sentetik profil üzerinde EKF, gerçek SoC'yi tolerans içinde takip ediyor (test raporlu).

### Faz 5 — Karşılaştırma + Arbitraj
- [ ] `comparison.py`: 15 dk hizalı beklenen/gerçek sapma serisi (gece saatleri ve düşük ışınım filtreli)
- [ ] Kural tabanlı sınıflandırıcılar: kalıcı sabit oranlı kayıp → **kirlilik**; günün belirli saatinde tekrarlayan çukur → **gölgelenme**; string bazlı ani basamak düşüş → **mikro çatlak** şüphesi
- [ ] İstatistiksel taban: hareketli medyan + MAD ile eşikleme, art arda N pencere kuralı
- [ ] Anomali olaylarının PostgreSQL'e yazımı (aç/kapa yaşam döngüsü, tekrar tekilleştirme)
- [ ] EPİAŞ istemci arayüzü + `mock_prices.py` (gerçek Şeffaflık 2.0 şemasıyla aynı yapı)
- [ ] `optimizer.py`: LP formülasyonu — amaç: gelir maksimizasyonu; kısıtlar: SoC sınırları, C-rate, çevrim başına derinlik, şebeke bağlantı gücü (`scipy.optimize.linprog`)
- [ ] Celery görevi: her gün GÖP fiyatları (mock) → ertesi günün şarj/deşarj planı → `ARBITRAGE_PLANS`
- **Dilim çıktısı:** sapma senaryolu sentetik veride doğru sınıf etiketi; örnek fiyat gününde pozitif beklenen gelirli plan.

### Faz 6 — Backend API + Konteynerizasyon
- [ ] JWT auth (login/refresh/me), şifre hash (`passlib` yerine `hashlib.scrypt` — bağımlılık minimal; gerekirse `bcrypt`)
- [ ] Router'lar: plants, timeseries, comparison, anomalies, bess, prices, arbitrage (bkz. §4)
- [ ] OpenAPI şemasının dashboard ekibiyle paylaşılabilir hale getirilmesi (örnek yanıtlar dahil)
- [ ] `Dockerfile` (multi-stage) + `docker-compose.yml`'a `api`, `worker`, `beat` servisleri
- [ ] Uçtan uca duman testi: compose up → seed → mock ingestion → API'den zaman serisi + anomali + plan okunuyor
- **Dilim çıktısı:** tek `docker compose up` ile tüm platform; dashboard ekibi OpenAPI ile geliştirmeye başlayabilir.

---

## 6. Test Stratejisi

- **Birim testleri (pytest + pytest-asyncio):** her modül kendi dizininde; mypy strict + ruff CI'da zorunlu.
- **Adaptörler:** `respx` ile httpx mock'lama; fixture'lar gerçek üretici yanıt şemalarını taklit eder
  (token süresi dolması, 429, 5xx, bozuk JSON senaryoları dahil). Gerçek kimlik bilgisi geldiğinde
  aynı testler `--live` işaretiyle gerçek API'ye karşı da koşabilir.
- **Normalizasyon:** üretici JSON → kanonik model dönüşümleri için tablo bazlı (parametrize) testler.
- **Dijital ikiz:** pvlib clear-sky referansına karşı deterministik testler; kayıp zinciri katsayılarının
  sınır değer testleri.
- **EKF/BESS:** iki katman — (1) sentetik profil: gerçek SoC bilindiğinden RMSE/maks hata asserted;
  (2) gerçek 8S CSV'leri gelince kalibrasyon + geri test (holdout deşarj eğrisi) aynı test altyapısına eklenir.
- **Arbitraj:** bilinen fiyat örüntülerinde (tek tepe, çift tepe, düz) beklenen şarj/deşarj kararlarının
  doğrulanması; LP kısıt ihlali olmadığının kontrolü.
- **Entegrasyon:** compose ile ayağa kalkan Postgres/Influx/Redis'e karşı dilim testleri
  (CI'da service container olarak).

---

## 7. Riskler ve Açık Sorular

Kararlaştırılanlar (mock-first, Open-Meteo, sentetik BESS) dışında kalan açık noktalar:

1. **Huawei Northbound kısıtları:** login endpoint'i saatlik çağrı limitine sahiptir; oturum token'ı
   agresif önbelleklenmeli. Gerçek hesap açıldığında hangi rol/izin setiyle geleceği belirsiz.
2. **SMA API belirsizliği:** Sunny Portal'ın herkese açık resmi API'si yok; ennexOS API'si hesap tipine
   göre değişiyor. Adaptör arayüzü sabit; gerçek erişim tipi netleşince yalnızca `sma.py` güncellenir.
3. **Panel/invertör datasheet'leri:** pvlib modeli için modül parametreleri (Pmax, γ, NOCT) ve Schneider
   invertör verim eğrisi gerekli. **Senden beklenen:** tesis(ler)in modül modeli + string konfigürasyonu.
4. **EPİAŞ kayıt süreci:** Şeffaflık 2.0 servis hesabı başvurusu zaman alabilir; erken başvuru önerilir.
5. **MW ölçekleme varsayımları:** 8S masaüstü düzenekten konteyner ölçeğine geçişte termal davranış ve
   hücre dengesizliği birebir ölçeklenmez; ölçekleme katsayıları "mühendislik yaklaşımı" olarak
   işaretlenecek, gerçek saha verisiyle revize edilecek.
6. **Çok kiracılılık:** şimdilik tek organizasyon + rol bazlı (admin/viewer) varsayıyorum. Dashboard
   ekibinin müşteri-bazlı izolasyon ihtiyacı varsa `organizations` tablosu eklenir — erken bilmek isterim.
7. **Zaman dilimi:** tüm seriler UTC saklanır, API `Europe/Istanbul`'a çevirme parametresi sunar. GÖP
   fiyat saatleri TRT bazlıdır — dönüşüm testleri kritik.
8. **Anomali sınıflandırma doğruluğu:** kural tabanlı yaklaşım etiketli gerçek arıza verisi olmadan
   yalnızca "şüphe" seviyesinde kalır; ilk sürümde `severity` alanıyla bu belirsizlik açıkça taşınır.

## 8. Konfigürasyon ve Secrets Yönetimi

- **`.env.example`** repoda; gerçek `.env` `.gitignore`'da. docker-compose `env_file` ile okur.

```env
# Genel
LM_ENV=dev
LM_LOG_LEVEL=INFO
# PostgreSQL
POSTGRES_DSN=postgresql+asyncpg://luminmind:changeme@postgres:5432/luminmind
# InfluxDB
INFLUX_URL=http://influxdb:8086
INFLUX_ORG=luminmind
INFLUX_TOKEN=changeme
# Redis / Celery
REDIS_URL=redis://redis:6379/0
# Auth
JWT_SECRET=changeme
JWT_ACCESS_TTL_MIN=30
# Vendor token şifreleme (Fernet anahtarı)
CREDENTIALS_ENC_KEY=changeme
# Dış API'ler (mock modda boş bırakılabilir)
LM_USE_MOCK_VENDORS=true
HUAWEI_BASE_URL=
SMA_BASE_URL=
EPIAS_BASE_URL=
LM_USE_MOCK_PRICES=true
```

- **Üretici API token'ları:** `.env`'de değil, `VENDOR_CREDENTIALS` tablosunda **Fernet ile şifreli**
  saklanır (anahtar `CREDENTIALS_ENC_KEY` env'den gelir). Böylece tesis başına farklı hesap yönetilebilir,
  refresh edilen token'lar kalıcı olur.
- Ayarların tamamı `config.py`'de tek `Settings(BaseSettings)` sınıfında, tip güvenli.
- Log'larda token/şifre maskeleme zorunlu (adapter loglama yardımcıları bunu merkezi yapar).

---

**Sonraki adım:** Bu planı onaylarsan Faz 0 + Faz 1 ile başlıyorum.
