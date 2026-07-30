# LuminMind

Bulut API tabanlı GES (güneş enerji santrali) izleme, dijital ikiz ve BESS/enerji arbitrajı platformu.

- **Durum:** Faz 0–6 tamamlandı — tüm plan uygulandı — detaylı yol haritası için [PLAN.md](PLAN.md)
- **Stack:** Python 3.12+, FastAPI, Celery + Redis, PostgreSQL, InfluxDB, pvlib, pandas

## Fazlar

1. ✅ Bulut API adaptörleri (Huawei FusionSolar, SMA — mock-first ingestion)
2. ✅ Hibrit veritabanı (PostgreSQL meta + InfluxDB zaman serileri)
3. ✅ GES dijital ikiz motoru (pvlib + Open-Meteo)
4. ✅ BESS modelleme ve BMS kalibrasyonu (Coulomb Counting + EKF)
5. ✅ Karşılaştırma motoru + EPİAŞ arbitraj algoritması
6. ✅ Backend API + docker-compose konteynerizasyonu

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
# init servisi: alembic migration + Influx bucket'ları (lm_raw/lm_hourly/lm_daily) + seed
# beat: 15 dk'da bir mock adaptörden veri çekip lm_raw'a yazar
# beat: her gece 00:30 UTC'de dünü lm_hourly/lm_daily'ye downsample eder
# beat: saat başı Open-Meteo tahminiyle günün beklenen üretimini twin_expected'a yazar
# beat: gece 01:00 UTC anomali analizi; 12:00 UTC yarının arbitraj planı
# ui:   http://localhost:8000/ui — tesis sahibine yönelik Türkçe panel
#       (Bugün, Harita, Geçmiş & Kazanç, santral detayı, Yapılacaklar, batarya planı)
# api:  http://localhost:8000/docs — OpenAPI dokümantasyonu (JWT ile /api/v1/*)
#       varsayılan giriş: admin@luminmind.local / admin (seed)
```

Gerçek üretici API'lerine geçiş: `.env` içinde `LM_USE_MOCK_VENDORS=false` yapıp
`HUAWEI_*` / `SMA_*` kimlik bilgilerini doldurmak yeterli — kod değişikliği gerekmez.

## Telefondan izleme (dışa açma)

Panel Cloudflare Tunnel ile yayınlanır: sunucuda **hiçbir port açılmaz**, modem
ayarı ve sertifika işi olmaz; bağlantı içeriden dışarıya kurulur.

**1 — Sırları üret ve `.env`'e yaz** (bu adım atlanamaz):

```bash
python -m luminmind.scripts.new_secrets   # çıktıyı .env içine yapıştırın
```

`LM_ENV=prod` iken uygulama `changeme` değerleriyle **açılmayı reddeder**
(`core/hardening.py`). Bu kasıtlı bir frendir; hata mesajı neyin eksik olduğunu
söyler.

**2 — Cloudflare tarafı.** Alan adınızı Cloudflare'e ekleyin →
[Zero Trust](https://one.dash.cloudflare.com) → Networks → Tunnels → *Create a
tunnel* → Cloudflared. Public hostname olarak `ges.alanadiniz.com` verin,
service `http://api:8000` olsun. Verilen token'ı `.env`'e yazın:

```dotenv
CLOUDFLARE_TUNNEL_TOKEN=eyJhIjoi...
LM_PUBLIC_URL=https://ges.alanadiniz.com
LM_ENV=prod
```

`LM_PUBLIC_URL` dolduğu anda sertleştirme kendiliğinden devreye girer: oturum
çerezi `Secure`, HSTS açık, host doğrulaması aktif.

**3 — Başlat:**

```bash
docker compose --profile public up -d --build
```

**4 — İlk giriş.** `https://ges.alanadiniz.com` → seed admin ile girin, ardından
**Kullanıcılar** sayfasından kendinize güçlü parolalı bir hesap açıp seed
hesabını silin.

**5 — Telefona kısayol.** Safari/Chrome'da sayfayı açıp "Ana Ekrana Ekle"
deyin; uygulama gibi tam ekran açılır (`/ui/manifest.webmanifest`).

### Güvenlik notları

- `docker-compose.yml` artık PostgreSQL, InfluxDB ve Redis portlarını **host'a
  yayınlamıyor**. Bu servislerin kimlik doğrulaması ya yok ya zayıf; internete
  bakan bir makinede yayınlamak veritabanını kaybetmenin en hızlı yoludur.
  API de yalnızca `127.0.0.1:8000`'e bağlanır — dışarıya çıkış tünelden geçer.
- Giriş formu IP başına 15 dakikada 8 denemeyle sınırlıdır
  (`LM_LOGIN_MAX_ATTEMPTS`). Gerçek IP Cloudflare'in `CF-Connecting-IP`
  başlığından okunur.
- **Önerilen ek katman:** Cloudflare Access ile tünelin önüne e-posta doğrulamalı
  bir kapı koyun. Böylece panel parolası internetten hiç denenemez.
- Oturum süresi `LM_SESSION_TTL_MIN` (varsayılan 1 gün) — telefonda sürekli
  çıkış yapmamak için. Kısaltmak güvenliği artırır, konforu düşürür.

## Üretim tahmini (dijital ikiz)

Tahmin motoru açık bir pvlib zinciridir (`twin/pipeline.py`); adımlar ve neden
her birinin gerekli olduğu dosyanın başındaki açıklamada. Kısaca:

| Katman | Ne yapar | Nerede |
| --- | --- | --- |
| Hava verisi | Open-Meteo 15 dk; eksik veri **NaN**, ışınım aralık ortalaması | `twin/weather.py` |
| Fizik | Güneş geometrisi (aralık ortası) → ışınım kapanış denetimi/Erbs → transpozisyon (arazide `infinite_sheds` ile sıra-arası gölge) → bileşen bazlı IAM → spektral → kirlilik → SAPM+Prilliman hücre sıcaklığı → PVWatts DC → **invertör kırpması** → AC kayıpları | `twin/pipeline.py` |
| Kirlilik | Kimber: kuru günde birikir, eşik üstü yağışta sıfırlanır | `twin/soiling.py` |
| Kalibrasyon | Tesis bazlı ölçek + saatlik bias; artımlı, sınırlı, açık anomali pencereleri hariç | `twin/calibration.py` |
| Belirsizlik | Bağımsız hava modellerinden P10/P50/P90 bandı | `twin/expected.py` |
| Skor tahtası | nMAE / nRMSE / **nMBE** / R² / enerji hatası / persistence'a karşı skill / band kapsaması | `analytics/accuracy.py` |

Influx serileri: `twin_expected` (D+0, karşılaştırma motorunun girdisi),
`twin_forecast` (`horizon_days` etiketli D+1..D+N), `twin_accuracy` (günlük skor).

Modelin iyileşip iyileşmediği **yalnızca** `twin_accuracy` serisinden anlaşılır.
Bir değişiklik yaptıktan sonra bakılacak sayı `nmae_pct` (isabet) ve `nmbe_pct`
(sistematik kayma); `skill_vs_reference ≤ 0` ise fizik modeli "dün ne olduysa
bugün de o olur" demekten daha kötü çalışıyordur.

Zamanlanmış görevler: saat başı ikiz (D+0..D+2), 01:00 anomali, 01:30 doğruluk
skoru, pazartesi 02:00 kalibrasyon, 12:00 arbitraj planı.

## Arbitraj

LP artık PV ile birlikte çözülür (`analytics/arbitrage/optimizer.py`): karar
değişkenleri şebekeden şarj, PV'den şarj, deşarj ve doğrudan PV ihracatıdır.
Böylece bağlantı limiti nedeniyle kırpılacak enerji bataryaya alınıp pahalı
saatte satılabilir — bir GES+BESS tesisinde kazancın büyük kısmı buradadır.
Tesis kaydındaki `grid_export_limit_kw` ve `feed_in_tariff_try_kwh` kısıt/fiyat
olarak modele girer.

## Arayüz tasarım ilkesi

Panel tesis sahibine/yatırımcıya göre yazılır, mühendise değil:

- Her sayı önce **₺** olarak gösterilir. Tarife `LM_DEFAULT_TARIFF_TRY_KWH`
  (varsayılan 2,9 ₺/kWh) veya santral kaydındaki `feed_in_tariff_try_kwh`.
- Anomaliler ham olay kütüğü değil, **yapılacak iş** olarak sunulur:
  *ne oldu → ne kaybettiriyor → ne yapmalı*. Metinler `web/advice.py` içinde tek yerde.
- Teknik ayrıntı (kanıt tabloları, PR yüzdesi, panel dizisi parametreleri) silinmez,
  katlanabilir kutulara alınır — varsayılan görünümü kirletmez.
- Yeni bir metin yazarken ölçüt: santral sahibi tek okumada anlıyor mu?
