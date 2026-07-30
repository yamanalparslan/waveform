"""Bulguları "kurtarılabilir yıllık gelir"e çeviren katman.

Arayüzün üst şeridindeki tek sayı buradan gelir: *bu santralde şu an ne kadar
para yerde duruyor?* Anomali motoru "-%8 kirlilik" der, cihaz sağlığı "3 nolu
invertör çevrimdışı" der; santral sahibi ise sıralanmış bir iş listesi ve
yanında ₺ bekler. Bu modül o dönüşümü yapar ve işleri **Acil / Orta / Uzun
vadeli** kovalarına ayırır.

Üç tasarım kararı modülün tamamını belirliyor:

**1. Çift sayma engellenir (en kritik kısım).** Bulgular birbirinden bağımsız
üretilir ama aynı kWh'i paylaşırlar. 3 nolu invertör çevrimdışıysa o cihazın
üretimi *tamamen* kayıptır; aynı gün saha genelinde "-%10 kirlilik" bulunduysa
kirliliğin iddia ettiği kaybın bir kısmı zaten çevrimdışı cihazın payıdır.
Ham toplama, gerçek kaybın 2–3 katını gösterir ve ekranın tüm güvenilirliğini
bitirir. `normalize_claims` bunu üç kuralla keser:

  * bir cihazın iddiaları o cihazın günlük üretimini aşamaz,
  * saha geneli iddialar yalnızca cihaz iddialarından **artan** üretimden pay
    alır,
  * ölçülen günlük açık biliniyorsa saha geneli iddialar onunla sınırlanır —
    kaybetmediğiniz enerjiyi geri kazanamazsınız.

Cihaz iddiaları ölçülen açıkla sınırlanmaz: onların kaynağı üreticinin kendi
sinyali (çevrimdışı/arıza kodu), dijital ikizin beklentisi değil. İkiz
yanlışsa açık küçük görünür ama cihaz gerçekten durmuştur.

**2. Yıllık gelir kalıcılık katsayısıyla iskonto edilir.** Günlük kayıp × 365
demek, bulgunun bir yıl boyunca aynen süreceğini varsaymaktır. Bu yalnızca
"düzeltilmezse" senaryosu için doğrudur ve türe göre değişir: çevrimdışı cihaz
müdahale edilmezse yıl boyu çevrimdışı kalır (1,0), gölgelenme mevsimseldir
(0,7), kirlilik ilk yağmurda kısmen kalkar (0,4).

**3. Güven seviyesi ikizin kendi hatasından türetilir.** nMAE %12 olan bir
modelde "-%8 kirlilik" bulgusu modelin gürültüsünden küçüktür; ₺ göstermek
ama güveni söylememek yanıltıcı olurdu. Cihaz kaynaklı bulgular bu kuraldan
muaftır (ikizden bağımsız ölçüm).

Modül **saf**tır: veritabanı/Influx bilmez, tablo bazlı test edilebilir.
Kayıp modeli ve öneri metinleri `PLAYBOOK`'ta tek yerde durur; `web/advice.py`
aynı sözlüğü kullanır, kopyalamaz.
"""

import logging
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from luminmind.analytics.accuracy import AccuracyScore
from luminmind.analytics.classifiers import KIND_MICROCRACK, KIND_SHADING, KIND_SOILING
from luminmind.analytics.inverter_health import (
    KIND_INV_ERROR,
    KIND_INV_OFFLINE,
    KIND_INV_OVERHEAT,
)
from luminmind.core.models import AnomalyEvent

logger = logging.getLogger(__name__)

# Türkiye ortalaması: 1 kWp kurulu güç günde kabaca 4,5 kWh üretir. Kaba bir
# ölçek — amaç kesin muhasebe değil, işleri önem sırasına dizmek.
PEAK_SUN_HOURS_TR = 4.5
DAYS_PER_YEAR = 365.0

# Aşırı ısınan invertör kapanmaz, gücünü kısar — kabaca beşte bir kayıp sayarız.
OVERHEAT_DERATE = 0.2

# ------------------------------ bulgu türleri ------------------------------
# Cihaz ve sapma türleri kendi modüllerinden gelir; aşağıdakiler bu katmanda
# doğar (doğruluk/kalibrasyon çıktısından türetilirler, anomali tablosunda yok).
KIND_HOUR_BIAS = "hour_bias"
KIND_CALIBRATION_SCALE = "calibration_scale"
KIND_DEGRADATION = "degradation"

# ------------------------------ öncelik ------------------------------
PRIORITY_IMMEDIATE = "immediate"
PRIORITY_MID = "mid"
PRIORITY_LONG = "long"
PRIORITY_ORDER = (PRIORITY_IMMEDIATE, PRIORITY_MID, PRIORITY_LONG)

PRIORITY_LABELS = {
    PRIORITY_IMMEDIATE: "Acil",
    PRIORITY_MID: "Orta vadeli",
    PRIORITY_LONG: "Uzun vadeli",
}
PRIORITY_CHIPS = {PRIORITY_IMMEDIATE: "crit", PRIORITY_MID: "warn", PRIORITY_LONG: "info"}

# Kalıcılık katsayısı: bulgu düzeltilmezse yılın ne kadarında sürer?
PERSISTENCE = {PRIORITY_IMMEDIATE: 1.0, PRIORITY_MID: 0.7, PRIORITY_LONG: 0.4}

# Bu büyüklükteki bir kayıpta sebebin ne olduğu ikincildir — kanama önce durur.
# Şiddet kapısıyla birlikte uygulanır (aşağıdaki `classify_priority`), yoksa
# rutin bir kirlilik bulgusu da "Acil" olur ve uzun vadeli kova hiç dolmaz.
IMMEDIATE_LOSS_FRACTION = 0.05

# ------------------------------ güven ------------------------------
CONFIDENCE_HIGH = "high"
CONFIDENCE_MEDIUM = "medium"
CONFIDENCE_LOW = "low"
CONFIDENCE_UNKNOWN = "unknown"

CONFIDENCE_LABELS = {
    CONFIDENCE_HIGH: "Yüksek güven",
    CONFIDENCE_MEDIUM: "Orta güven",
    CONFIDENCE_LOW: "Zayıf kanıt",
    CONFIDENCE_UNKNOWN: "Doğrulanmadı",
}

# Modelin kendi hatası bu eşiklerin altındaysa bulguya güvenilir.
CONFIDENCE_HIGH_NMAE_PCT = 5.0
CONFIDENCE_MEDIUM_NMAE_PCT = 10.0

# ------------------------------ kapsam ve kayıp modeli ------------------------------
SCOPE_DEVICE = "device"
SCOPE_SITE = "site"

LOSS_DEVICE_FULL = "device_full"  # cihazın tüm üretimi kayıp
LOSS_DEVICE_DERATE = "device_derate"  # cihaz üretiminin bir kısmı kayıp
LOSS_SITE_DEVIATION = "site_deviation"  # saha üretiminin sapma oranı kadarı


@dataclass(frozen=True)
class Playbook:
    """Bir bulgu türü hakkındaki sabit bilgi.

    `title` içinde `{device}` yer tutucusu bulunabilir; saha geneli bulgularda
    bulunmaz. `recoverable=False` olanlar listede görünür ama kurtarılabilir
    gelire katkı vermez — yaşa bağlı bozunumu "geri kazanmak" mümkün değildir,
    onu gelir gibi göstermek rakamı şişirir.
    """

    title: str
    recommendation: str
    scope: str
    loss_model: str
    priority: str
    recoverable: bool = True


PLAYBOOK: dict[str, Playbook] = {
    KIND_INV_OFFLINE: Playbook(
        title="{device} veri göndermiyor",
        recommendation=(
            "Cihazın şalterini ve internet bağlantısını yerinde kontrol ettirin; "
            "sorun sürerse servisi arayın."
        ),
        scope=SCOPE_DEVICE,
        loss_model=LOSS_DEVICE_FULL,
        priority=PRIORITY_IMMEDIATE,
    ),
    KIND_INV_ERROR: Playbook(
        title="{device} arıza bildiriyor",
        recommendation="Servisi arayıp cihazın verdiği hata kodunu iletin.",
        scope=SCOPE_DEVICE,
        loss_model=LOSS_DEVICE_FULL,
        priority=PRIORITY_IMMEDIATE,
    ),
    KIND_INV_OVERHEAT: Playbook(
        title="{device} fazla ısındı",
        recommendation=(
            "Cihazın havalandırmasını ve fan filtrelerini temizletin; "
            "önündeki hava akışını kapatan bir şey var mı bakın."
        ),
        scope=SCOPE_DEVICE,
        loss_model=LOSS_DEVICE_DERATE,
        priority=PRIORITY_MID,
    ),
    KIND_MICROCRACK: Playbook(
        title="Panellerde kalıcı verim kaybı şüphesi",
        recommendation="Panel üreticisine garanti başvurusu için sahada ölçüm yaptırın.",
        scope=SCOPE_SITE,
        loss_model=LOSS_SITE_DEVIATION,
        priority=PRIORITY_IMMEDIATE,
    ),
    KIND_SHADING: Playbook(
        title="Paneller gölgede kalıyor",
        recommendation=(
            "Panellerin önünde büyüyen ağaç, yeni yapı veya biriken malzeme "
            "olup olmadığını kontrol ettirin."
        ),
        scope=SCOPE_SITE,
        loss_model=LOSS_SITE_DEVIATION,
        priority=PRIORITY_MID,
    ),
    KIND_SOILING: Playbook(
        title="Paneller kirlenmiş görünüyor",
        recommendation="Panel temizliği planlayın — genellikle bir günlük iş.",
        scope=SCOPE_SITE,
        loss_model=LOSS_SITE_DEVIATION,
        priority=PRIORITY_LONG,
    ),
    KIND_HOUR_BIAS: Playbook(
        title="Belirli saatlerde tekrarlayan üretim kaybı",
        recommendation=(
            "Kaybın yoğunlaştığı saatlerde gölgeleme, invertör güç kısıtlaması "
            "veya dizi arızası olup olmadığını inceleyin."
        ),
        scope=SCOPE_SITE,
        loss_model=LOSS_SITE_DEVIATION,
        priority=PRIORITY_MID,
    ),
    KIND_CALIBRATION_SCALE: Playbook(
        title="Santral fizik modelinin altında üretiyor",
        recommendation=(
            "Kurulu güç, panel/invertör envanteri ve DC kablo kayıplarını sahada "
            "doğrulayın; fark gerçekse yapısal bir kayıp var."
        ),
        scope=SCOPE_SITE,
        loss_model=LOSS_SITE_DEVIATION,
        priority=PRIORITY_LONG,
    ),
    KIND_DEGRADATION: Playbook(
        title="Panellerde yaşa bağlı verim kaybı",
        recommendation=(
            "Bu kayıp normaldir. Üreticinin garanti eğrisiyle karşılaştırın; "
            "eğrinin altındaysanız garanti başvurusu yapın."
        ),
        scope=SCOPE_SITE,
        loss_model=LOSS_SITE_DEVIATION,
        priority=PRIORITY_LONG,
        recoverable=False,  # yaşlanma geri alınamaz, gelir gibi sayılmaz
    ),
}

# Tanımadığımız bir tür geldiğinde sessizce düşürmek yerine genel bir kayıt
# üretiriz; kullanıcı en azından "bir şey var" bilgisini görür.
FALLBACK_PLAYBOOK = Playbook(
    title="{device} beklenenden az üretiyor",
    recommendation="Sahayı kontrol ettirin.",
    scope=SCOPE_SITE,
    loss_model=LOSS_SITE_DEVIATION,
    priority=PRIORITY_MID,
)


def playbook_for(kind: str) -> Playbook:
    """Bulgu türünün kayıp modeli ve önerisi; bilinmiyorsa genel kayıt."""
    return PLAYBOOK.get(kind, FALLBACK_PLAYBOOK)


# ------------------------------ girdi yapıları ------------------------------


@dataclass(frozen=True)
class LossFinding:
    """Bu katmanın girdisi: kaynağı ne olursa olsun tek bir bulgu.

    `AnomalyEvent` satırından, `HealthFinding`'den veya kalibrasyon çıktısından
    üretilebilir — modülün veritabanı bilmemesi bu ara yapı sayesinde.
    """

    kind: str
    site_key: str
    severity: str = "warning"
    deviation_pct: float = 0.0
    device_id: str | None = None
    started_at: datetime | None = None
    evidence: Mapping[str, Any] = field(default_factory=dict)
    event_id: uuid.UUID | None = None


def device_id_of(event: AnomalyEvent) -> str | None:
    """Olay kanıtından cihaz numarası; saha geneli olaylarda None.

    `evidence` serbest bir JSON sözlüğü ve cihaz numarası orada dize olarak
    tutuluyor. Çıkarımı üç ayrı yerde (iş listesi, aksiyon planı, cihaz sayfası)
    elle yapmak, birinde `0` gibi bir numaranın sessizce yok sayılmasına yol
    açardı — bu yüzden tek kapı.
    """
    if isinstance(event.evidence, dict):
        raw = event.evidence.get("device_id")
        if raw not in (None, ""):
            return str(raw)
    return None


def finding_from_event(event: AnomalyEvent, site_key: str) -> LossFinding:
    """Anomali satırını bu katmanın girdisine çevirir.

    `site_key` çağırandan gelir: olayın `site_id`'si bir UUID, zaman serisi
    anahtarı ise `Site.series_key`. Eşlemeyi burada yapmak için veritabanına
    dokunmak gerekirdi; modülün saf kalması bu ayrımla sağlanıyor.
    """
    return LossFinding(
        kind=event.kind,
        site_key=site_key,
        severity=event.severity,
        deviation_pct=event.deviation_pct,
        device_id=device_id_of(event),
        started_at=event.started_at,
        evidence=event.evidence if isinstance(event.evidence, dict) else {},
        event_id=event.id,
    )


@dataclass(frozen=True)
class SiteContext:
    """Bir sahanın kayıp fiyatlamasında gereken bağlamı."""

    series_key: str
    name: str
    capacity_kwp: float | None
    tariff_try_kwh: float
    device_count: int = 1
    peak_sun_hours: float = PEAK_SUN_HOURS_TR
    # Dijital ikizden gelen yıllık beklenti; yoksa kaba günlük modelden türetilir.
    annual_expected_kwh: float | None = None
    # Ölçülen günlük açık (beklenen − gerçek, kWh). Saha geneli iddiaların üst
    # sınırı; bilinmiyorsa yalnızca kapasite sınırı uygulanır.
    measured_shortfall_kwh: float | None = None
    # İkizin kendi hatası — güven seviyesi buradan türetilir.
    accuracy_nmae_pct: float | None = None

    @property
    def daily_potential_kwh(self) -> float:
        """Sahanın kaba günlük üretim potansiyeli (kWh)."""
        return (self.capacity_kwp or 0.0) * self.peak_sun_hours

    @property
    def annual_reference_kwh(self) -> float:
        """Yüzdelerin paydası: yıllık beklenen üretim."""
        if self.annual_expected_kwh and self.annual_expected_kwh > 0.0:
            return self.annual_expected_kwh
        return self.daily_potential_kwh * DAYS_PER_YEAR


# ------------------------------ çıktı ------------------------------


@dataclass(frozen=True)
class Insight:
    """Kullanıcıya gösterilen tek bir aksiyon kalemi."""

    kind: str
    title: str
    priority: str
    site_key: str
    site_name: str
    device_id: str | None
    daily_loss_kwh: float
    recoverable_kwh_year: float
    recoverable_try_year: float
    recoverable_pct: float
    confidence: str
    recommendation: str
    severity: str
    evidence: dict[str, Any] = field(default_factory=dict)
    started_at: datetime | None = None
    event_id: uuid.UUID | None = None

    @property
    def priority_label(self) -> str:
        return PRIORITY_LABELS.get(self.priority, self.priority)

    @property
    def priority_chip(self) -> str:
        return PRIORITY_CHIPS.get(self.priority, "muted")

    @property
    def confidence_label(self) -> str:
        return CONFIDENCE_LABELS.get(self.confidence, self.confidence)

    @property
    def priority_rank(self) -> int:
        try:
            return PRIORITY_ORDER.index(self.priority)
        except ValueError:
            return len(PRIORITY_ORDER)


@dataclass(frozen=True)
class PortfolioInsights:
    """Portföy düzeyinde toplanmış aksiyon planı."""

    insights: list[Insight]
    recoverable_try_year: float
    recoverable_kwh_year: float
    recoverable_pct: float
    try_by_priority: dict[str, float]
    kwh_by_priority: dict[str, float]
    count_by_priority: dict[str, int]

    @property
    def total_count(self) -> int:
        return len(self.insights)

    def top(self, limit: int = 5) -> list[Insight]:
        return self.insights[:limit]


# ------------------------------ kayıp modeli ------------------------------


def device_daily_kwh(site_daily_kwh: float, device_count: int) -> float:
    """Bir cihazın günlük üretim payı — saha üretimi cihazlara eşit dağıtılır.

    Cihaz başına kurulu güç bilinseydi ağırlıklı dağıtım yapılırdı; Tescom
    API'si cihaz anma gücü vermiyor, eşit dağıtım en az varsayım içeren seçenek.
    """
    if device_count <= 0:
        return 0.0
    return site_daily_kwh / device_count


def daily_loss_kwh(
    playbook: Playbook,
    deviation_pct: float,
    site_daily_kwh: float,
    device_daily_kwh_value: float,
) -> float:
    """Bulgunun ham günlük kayıp tahmini (kWh) — normalizasyondan önce."""
    if playbook.loss_model == LOSS_DEVICE_FULL:
        return device_daily_kwh_value
    if playbook.loss_model == LOSS_DEVICE_DERATE:
        return device_daily_kwh_value * OVERHEAT_DERATE
    return site_daily_kwh * abs(deviation_pct) / 100.0


# ------------------------------ çift sayma normalizasyonu ------------------------------


@dataclass(frozen=True)
class LossClaim:
    """Tek bir bulgunun kayıp iddiası, kapsamıyla birlikte."""

    scope: str
    device_id: str | None
    daily_loss_kwh: float


def normalize_claims(
    claims: Sequence[LossClaim],
    site_daily_kwh: float,
    device_daily_kwh_value: float,
    measured_shortfall_kwh: float | None = None,
) -> list[float]:
    """Örtüşen kayıp iddialarını fiziksel sınırlara oturtur.

    Girdiyle **aynı sırada** düzeltilmiş günlük kayıplar döner. Kurallar
    modülün üst açıklamasındaki gerekçelerle uygulanır:

    1. Bir cihazın tüm iddiaları o cihazın günlük üretimini aşamaz (aynı
       cihazda hem aşırı ısınma hem arıza kodu olabilir).
    2. Saha geneli iddialar cihaz iddialarından artan üretimden pay alır;
       çevrimdışı bir cihazın payı kirlilikle ikinci kez kaybedilemez.
    3. Ölçülen günlük açık verildiyse saha geneli iddialar `açık − cihaz
       iddiaları` ile sınırlanır. Açığın tamamı cihazlarla açıklanıyorsa saha
       geneli iddialar sıfırlanır — aynı kWh'i iki kez satmamak için.

    Cihaz iddiaları ölçülen açıkla **kısıtlanmaz**: kaynakları üreticinin
    sinyali, ikizin beklentisi değil.
    """
    values = [max(0.0, claim.daily_loss_kwh) for claim in claims]
    if site_daily_kwh <= 0.0:
        return [0.0] * len(values)

    # 1) cihaz başına tavan
    per_device: dict[str, list[int]] = {}
    for index, claim in enumerate(claims):
        if claim.scope == SCOPE_DEVICE:
            per_device.setdefault(claim.device_id or "", []).append(index)

    device_cap = device_daily_kwh_value if device_daily_kwh_value > 0.0 else site_daily_kwh
    device_cap = min(device_cap, site_daily_kwh)
    claimed_by_devices = 0.0
    for indices in per_device.values():
        total = sum(values[i] for i in indices)
        if total > device_cap:
            factor = device_cap / total if total > 0.0 else 0.0
            for i in indices:
                values[i] *= factor
            total = device_cap
        claimed_by_devices += total

    # 2–3) saha geneli iddialar için kalan havuz
    site_indices = [i for i, claim in enumerate(claims) if claim.scope != SCOPE_DEVICE]
    site_total = sum(values[i] for i in site_indices)
    if site_indices and site_total > 0.0:
        pool = max(0.0, site_daily_kwh - claimed_by_devices)
        if measured_shortfall_kwh is not None:
            pool = min(pool, max(0.0, measured_shortfall_kwh - claimed_by_devices))
        if site_total > pool:
            factor = pool / site_total
            for i in site_indices:
                values[i] *= factor

    return [round(value, 4) for value in values]


# ------------------------------ sınıflandırma ------------------------------


def classify_priority(
    playbook: Playbook,
    severity: str,
    daily_loss_kwh_value: float,
    site_daily_kwh: float,
) -> str:
    """Türün taban önceliği; büyük ve kritik kayıplarda `immediate`'e yükselir.

    Yükseltme **hem** şiddet **hem** büyüklük şartına bağlı. Yalnız büyüklüğe
    bakmak, sınıflandırıcının eşiği (%5) ile çakışıp her kirlilik bulgusunu
    "Acil" yapar ve üç kovalı ayrımı anlamsızlaştırırdı.
    """
    if not playbook.recoverable:
        return playbook.priority  # geri kazanılamayan kayıp acil olamaz
    if severity == "critical" and site_daily_kwh > 0.0:
        if daily_loss_kwh_value / site_daily_kwh >= IMMEDIATE_LOSS_FRACTION:
            return PRIORITY_IMMEDIATE
    return playbook.priority


def confidence_for(scope: str, nmae_pct: float | None, deviation_pct: float) -> str:
    """Bulgunun kanıt gücü.

    Cihaz kaynaklı bulgular ikizden bağımsızdır (üretici doğrudan "çevrimdışı"
    veya "arıza kodu" diyor) → yüksek güven. Sapma kaynaklı bulgular ikizin
    doğruluğu kadar güvenilirdir; bulgu modelin kendi hatasından küçükse
    gürültüden ayırt edilemez.
    """
    if scope == SCOPE_DEVICE:
        return CONFIDENCE_HIGH
    if nmae_pct is None:
        return CONFIDENCE_UNKNOWN
    if abs(deviation_pct) <= nmae_pct:
        return CONFIDENCE_LOW
    if nmae_pct < CONFIDENCE_HIGH_NMAE_PCT:
        return CONFIDENCE_HIGH
    if nmae_pct < CONFIDENCE_MEDIUM_NMAE_PCT:
        return CONFIDENCE_MEDIUM
    return CONFIDENCE_LOW


def shortfall_from_score(score: AccuracyScore | None) -> float | None:
    """Skor tahtasından ölçülen günlük açık (kWh); fazla üretimde 0.

    Doğruluk skoru zaten "beklenen ne, gerçek ne" sorusunu günlük enerji
    düzeyinde cevaplıyor — normalizasyonun üst sınırı için ikinci bir hesap
    yapmak, iki sayının zamanla ayrışması demekti.
    """
    if score is None:
        return None
    return max(0.0, score.energy_expected_kwh - score.energy_actual_kwh)


# ------------------------------ toplayıcılar ------------------------------


def sort_insights(insights: Sequence[Insight]) -> list[Insight]:
    """Önce öncelik, sonra en çok para: ekranın okunma sırası."""
    return sorted(insights, key=lambda i: (i.priority_rank, -i.recoverable_try_year))


def site_insights(context: SiteContext, findings: Sequence[LossFinding]) -> list[Insight]:
    """Bir sahanın bulgularını fiyatlanmış, önceliklendirilmiş listeye çevirir."""
    site_daily = context.daily_potential_kwh
    per_device = device_daily_kwh(site_daily, context.device_count)
    playbooks = [playbook_for(f.kind) for f in findings]

    claims = [
        LossClaim(
            scope=playbook.scope,
            device_id=finding.device_id,
            daily_loss_kwh=daily_loss_kwh(
                playbook, finding.deviation_pct, site_daily, per_device
            ),
        )
        for finding, playbook in zip(findings, playbooks, strict=True)
    ]
    normalized = normalize_claims(
        claims, site_daily, per_device, context.measured_shortfall_kwh
    )

    reference = context.annual_reference_kwh
    insights: list[Insight] = []
    for finding, playbook, loss in zip(findings, playbooks, normalized, strict=True):
        priority = classify_priority(playbook, finding.severity, loss, site_daily)
        kwh_year = loss * DAYS_PER_YEAR * PERSISTENCE[priority] if playbook.recoverable else 0.0
        insights.append(
            Insight(
                kind=finding.kind,
                title=playbook.title.format(device=_device_label(finding.device_id)),
                priority=priority,
                site_key=context.series_key,
                site_name=context.name,
                device_id=finding.device_id,
                daily_loss_kwh=round(loss, 3),
                recoverable_kwh_year=round(kwh_year, 1),
                recoverable_try_year=round(kwh_year * context.tariff_try_kwh, 2),
                recoverable_pct=round(kwh_year / reference * 100.0, 2) if reference > 0 else 0.0,
                confidence=confidence_for(
                    playbook.scope, context.accuracy_nmae_pct, finding.deviation_pct
                ),
                recommendation=playbook.recommendation,
                severity=finding.severity,
                evidence=dict(finding.evidence),
                started_at=finding.started_at,
                event_id=finding.event_id,
            )
        )
    return sort_insights(insights)


def portfolio_insights(
    contexts: Sequence[SiteContext], findings: Sequence[LossFinding]
) -> PortfolioInsights:
    """Tüm sahaların bulgularını tek aksiyon planına toplar.

    Normalizasyon **saha bazında** yapılır: bir sahanın kirliliği başka bir
    sahanın çevrimdışı cihazıyla örtüşmez, ortak bir havuza atmak yanlış olurdu.
    """
    by_key = {c.series_key: c for c in contexts}
    grouped: dict[str, list[LossFinding]] = {key: [] for key in by_key}
    for finding in findings:
        bucket = grouped.get(finding.site_key)
        if bucket is None:
            # Kapasitesi bilinmeyen seriden gelen bulgu fiyatlanamaz; sessizce
            # düşmek yerine loglanır, yoksa eksik ₺ fark edilmez.
            logger.warning("bağlamı olmayan seri için bulgu atlandı: %s", finding.site_key)
            continue
        bucket.append(finding)

    collected: list[Insight] = []
    for key, bucket in grouped.items():
        if bucket:
            collected.extend(site_insights(by_key[key], bucket))

    try_by_priority = {p: 0.0 for p in PRIORITY_ORDER}
    kwh_by_priority = {p: 0.0 for p in PRIORITY_ORDER}
    count_by_priority = {p: 0 for p in PRIORITY_ORDER}
    for insight in collected:
        try_by_priority[insight.priority] = (
            try_by_priority.get(insight.priority, 0.0) + insight.recoverable_try_year
        )
        kwh_by_priority[insight.priority] = (
            kwh_by_priority.get(insight.priority, 0.0) + insight.recoverable_kwh_year
        )
        count_by_priority[insight.priority] = count_by_priority.get(insight.priority, 0) + 1

    total_kwh = sum(kwh_by_priority.values())
    reference = sum(by_key[key].annual_reference_kwh for key in by_key)
    return PortfolioInsights(
        insights=sort_insights(collected),
        recoverable_try_year=round(sum(try_by_priority.values()), 2),
        recoverable_kwh_year=round(total_kwh, 1),
        recoverable_pct=round(total_kwh / reference * 100.0, 2) if reference > 0 else 0.0,
        try_by_priority={p: round(v, 2) for p, v in try_by_priority.items()},
        kwh_by_priority={p: round(v, 1) for p, v in kwh_by_priority.items()},
        count_by_priority=count_by_priority,
    )


def _device_label(device_id: str | None) -> str:
    return f"{device_id} nolu invertör" if device_id else "Santral"
