"""Fizibilite: LCOE, NPV, IRR ve geri ödeme süresi.

Üretim projeksiyonunu (`prospect.simulate`) nakit akışına çevirir ve yatırım
kararının dayandığı dört sayıyı üretir.

**Model reel (enflasyondan arındırılmış) kurulur.** Türkiye'de nominal
iskonto oranıyla çalışmak, tarifeyi de aynı enflasyonla büyütmeyi zorunlu kılar;
iki büyük ve belirsiz sayının farkı sonucu domine eder ve model kırılganlaşır.
Reel kurguda kullanıcı yalnızca "elektrik fiyatı enflasyonun üstünde yılda kaç
puan artar" sorusuna cevap verir — bu tahmin edilebilir bir büyüklüktür.
Enflasyon oranı hiçbir yere girmez, dolayısıyla yanlış tahmin edilemez.

**Öztüketim ile şebekeye satış ayrı fiyatlanır.** Türkiye'de ticari çatı
GES'lerinin ekonomisi buna dayanıyor: tesisin kendi tükettiği kWh perakende
tarifeden *tasarruf* sayılır (dağıtım bedeli, fonlar ve vergiler dahil olduğu
için yüksektir), şebekeye verilen fazla ise mahsuplaşma birim fiyatından
gelir yazar (belirgin şekilde düşüktür). Öztüketim oranını değiştirmek IRR'ı
tarifeyi değiştirmekten daha çok oynatır; bu yüzden tek bir "elektrik fiyatı"
alanı yerine iki tarife ve bir pay olarak modellendi.

`RevenueModel` ve `CostModel` varsayılanları 2026 ortası Türkiye ticari ölçek
mertebeleridir ve **teklif üretmeden önce EPC'nin kendi rakamlarıyla
değiştirilmelidir**; rapor bunları açıkça basar.
"""

import logging
from collections.abc import Sequence
from dataclasses import dataclass, replace

from luminmind.prospect.simulate import SimulationResult, YearProjection

logger = logging.getLogger(__name__)

# IRR aramasında taranan reel getiri aralığı. Üst sınır fahiş görünse de
# öztüketimli çatı projelerinde %60'ı aşan reel IRR gerçekten çıkabiliyor.
_IRR_BRACKET = (-0.95, 2.0)
# Yakınsama *oran* genişliğinden okunur, NPV'nin mutlak değerinden değil: NPV
# ₺ cinsinden milyonlar mertebesindedir, ona 1e-7 gibi bir eşik koymak asla
# sağlanmaz ve döngü sessizce hep azami adıma kadar koşar. Oranda 1e-7 ise
# yüzdenin beş binde birinden ince — fazlasıyla yeterli.
_IRR_RATE_TOLERANCE = 1e-7
_IRR_MAX_ITERATIONS = 200


@dataclass(frozen=True)
class CostModel:
    """Yatırım ve işletme maliyetleri (₺, reel).

    `capex_per_kwp_try` anahtar teslim kurulum bedelidir: panel, invertör,
    konstrüksiyon, DC/AC malzeme, işçilik, proje ve devreye alma.
    """

    capex_per_kwp_try: float = 18_000.0
    # Yıllık işletme-bakım: temizlik, izleme, sigorta, arıza müdahalesi.
    opex_per_kwp_year_try: float = 320.0
    # İnvertör ömrü tipik olarak 12–15 yıl; değişim maliyeti ayrı kalem.
    inverter_replacement_year: int | None = 13
    inverter_replacement_per_kwp_try: float = 1_800.0
    # Ömür sonu hurda/sökme değeri (pozitif = gelir).
    residual_value_per_kwp_try: float = 0.0

    def capex_try(self, dc_kwp: float) -> float:
        return self.capex_per_kwp_try * dc_kwp

    def opex_try(self, dc_kwp: float) -> float:
        return self.opex_per_kwp_year_try * dc_kwp


@dataclass(frozen=True)
class RevenueModel:
    """Tarife yapısı ve gelir varsayımları (₺/kWh, reel).

    `self_consumption_share` tesisin ürettiğini kendi tüketebildiği orandır.
    Gerçek değeri tesisin yük profiliyle üretim profilinin örtüşmesine bağlıdır;
    saatlik tüketim verisi olmadan tam hesaplanamaz, bu yüzden parametre olarak
    duruyor. Sanayi tesislerinde (gündüz vardiyası) 0,7–0,9; depo/ofiste 0,4–0,6
    tipiktir.
    """

    retail_tariff_try_kwh: float = 3.40  # öztüketimde kaçınılan perakende bedel
    export_tariff_try_kwh: float = 1.60  # şebekeye verilen fazlanın birim geliri
    self_consumption_share: float = 0.75
    # Elektrik fiyatının enflasyon *üstü* yıllık reel artışı.
    real_price_escalation: float = 0.01

    def __post_init__(self) -> None:
        if not 0.0 <= self.self_consumption_share <= 1.0:
            raise ValueError(
                f"öztüketim payı [0, 1] aralığında olmalı, {self.self_consumption_share} verildi"
            )

    def blended_tariff_try_kwh(self) -> float:
        """Öztüketim ve satış tarifesinin paya göre ağırlıklı ortalaması."""
        share = self.self_consumption_share
        return share * self.retail_tariff_try_kwh + (1.0 - share) * self.export_tariff_try_kwh

    def revenue_try(self, energy_kwh: float, year: int) -> float:
        """Yıl `year` (1 tabanlı) için reel gelir."""
        escalation = (1.0 + self.real_price_escalation) ** (year - 1)
        return energy_kwh * self.blended_tariff_try_kwh() * escalation


@dataclass(frozen=True)
class FinanceParams:
    """İskonto ve senaryo parametreleri."""

    # Reel iskonto oranı = sermayenin enflasyon üstü beklenen getirisi.
    discount_rate_real: float = 0.12
    # Üretim senaryosu: 0,5 = P50 (beklenen), 0,90 = P90 (finansman senaryosu).
    exceedance: float = 0.50


@dataclass(frozen=True)
class CashflowYear:
    """Tek yılın nakit akışı (₺, reel)."""

    year: int
    energy_kwh: float
    revenue_try: float
    opex_try: float
    replacement_try: float
    net_try: float
    discounted_net_try: float
    cumulative_net_try: float  # iskontosuz kümülatif (geri ödeme bunun üstünden)


@dataclass(frozen=True)
class FinanceResult:
    """Fizibilite sonucu — tesis sahibinin tek okumada anlaması gereken sayılar."""

    capex_try: float
    dc_capacity_kwp: float
    rows: tuple[CashflowYear, ...]
    npv_try: float
    irr_real: float | None
    lcoe_try_kwh: float
    payback_years: float | None
    discount_rate_real: float
    exceedance: float
    blended_tariff_try_kwh: float

    @property
    def lifetime_energy_kwh(self) -> float:
        return sum(r.energy_kwh for r in self.rows)

    @property
    def lifetime_revenue_try(self) -> float:
        return sum(r.revenue_try for r in self.rows)

    @property
    def year_one_revenue_try(self) -> float:
        return self.rows[0].revenue_try if self.rows else 0.0

    @property
    def specific_capex_try_kwp(self) -> float:
        return self.capex_try / self.dc_capacity_kwp if self.dc_capacity_kwp > 0 else 0.0

    @property
    def is_viable(self) -> bool:
        """NPV pozitif mi — yatırım iskonto oranını aşıyor mu."""
        return self.npv_try > 0.0


def _discount(value: float, year: int, rate: float) -> float:
    """Yıl sonu iskontolama (yıl 1 → bir dönem)."""
    return value / (1.0 + rate) ** year


def net_present_value(
    capex_try: float, rows: Sequence[CashflowYear]
) -> float:
    return sum(r.discounted_net_try for r in rows) - capex_try


def internal_rate_of_return(
    capex_try: float, net_flows: Sequence[float]
) -> float | None:
    """Reel IRR — NPV'yi sıfırlayan iskonto oranı.

    İkiye bölme (bisection) kullanılıyor, Newton değil: nakit akışı yalnızca
    başta negatif olduğu için NPV oranın monoton azalan fonksiyonudur ve
    ikiye bölme bu durumda koşulsuz yakınsar. Newton, invertör değişim yılında
    negatife dönen akışlarda türevi işaret değiştirdiğinde ıraksayabilir.

    Aralıkta işaret değişimi yoksa `None` döner: IRR tanımsızdır (proje hiçbir
    oranda başa baş gelmiyor ya da her oranda kârlı). Sessizce 0 dönmek
    "getirisi yok" gibi okunurdu.
    """

    def npv_at(rate: float) -> float:
        return sum(
            _discount(flow, year, rate) for year, flow in enumerate(net_flows, start=1)
        ) - capex_try

    low, high = _IRR_BRACKET
    npv_low, npv_high = npv_at(low), npv_at(high)
    if npv_low * npv_high > 0.0:
        return None

    for _ in range(_IRR_MAX_ITERATIONS):
        middle = (low + high) / 2.0
        if (high - low) < _IRR_RATE_TOLERANCE:
            return middle
        npv_middle = npv_at(middle)
        if npv_middle * npv_low > 0.0:
            low, npv_low = middle, npv_middle
        else:
            high = middle
    return (low + high) / 2.0


def levelised_cost(
    capex_try: float,
    rows: Sequence[CashflowYear],
    discount_rate: float,
) -> float:
    """LCOE: iskontolanmış toplam maliyet / iskontolanmış toplam enerji (₺/kWh).

    Gelir hesaba girmez — LCOE üretim maliyetidir, kârlılık değil. Tarifeyle
    karşılaştırılarak okunur: LCOE tarifenin altındaysa proje kendi elektriğini
    şebekeden almaktan ucuza üretiyor.

    Enerji de iskontolanır; iskontolanmamış enerjiyle bölmek yaygın bir hatadır
    ve LCOE'yi sistematik olarak düşük gösterir (ileri yılların enerjisi bugünün
    parasıyla aynı ağırlıkta sayılır).
    """
    discounted_cost = capex_try + sum(
        _discount(r.opex_try + r.replacement_try, r.year, discount_rate) for r in rows
    )
    discounted_energy = sum(
        _discount(r.energy_kwh, r.year, discount_rate) for r in rows
    )
    return discounted_cost / discounted_energy if discounted_energy > 0 else 0.0


def _payback_years(capex_try: float, rows: Sequence[CashflowYear]) -> float | None:
    """İskontosuz geri ödeme süresi, yıl içi doğrusal aradeğerlemeyle.

    Kümülatif akış artıya geçtiği yılı bulup o yıl içinde oranlar; tam yıl
    döndürmek 3,1 yıl ile 3,9 yılı aynı göstermek olurdu.
    """
    for row in rows:
        if row.cumulative_net_try >= 0.0:
            previous = row.cumulative_net_try - row.net_try
            if row.net_try <= 0.0:
                return float(row.year)
            return row.year - 1 + (-previous / row.net_try)
    return None


def evaluate(
    simulation: SimulationResult,
    costs: CostModel | None = None,
    revenue: RevenueModel | None = None,
    params: FinanceParams | None = None,
) -> FinanceResult:
    """Üretim projeksiyonunu fizibilite göstergelerine çevirir.

    Enerji girdisi `simulation.projection`'dan gelir — `year_one_kwh` değil.
    Projeksiyon yıl ortası yaşı ve kırpma etkileşimini içerir; sıfır yaşlı
    değeri gelire vermek 25 yılın tamamını yukarı kaydırırdı.
    """
    costs = costs or CostModel()
    revenue = revenue or RevenueModel()
    params = params or FinanceParams()

    dc_kwp = simulation.dc_capacity_kwp
    capex = costs.capex_try(dc_kwp)
    annual_opex = costs.opex_try(dc_kwp)
    scenario_factor = (
        1.0
        if params.exceedance == 0.5
        else simulation.uncertainty.percentile_factor(params.exceedance)
    )

    rows: list[CashflowYear] = []
    cumulative = -capex
    projection: tuple[YearProjection, ...] = simulation.projection
    for item in projection:
        energy = item.energy_kwh * scenario_factor
        gross = revenue.revenue_try(energy, item.year)
        replacement = (
            costs.inverter_replacement_per_kwp_try * dc_kwp
            if item.year == costs.inverter_replacement_year
            else 0.0
        )
        residual = (
            costs.residual_value_per_kwp_try * dc_kwp
            if item.year == len(projection)
            else 0.0
        )
        net = gross - annual_opex - replacement + residual
        cumulative += net
        rows.append(
            CashflowYear(
                year=item.year,
                energy_kwh=energy,
                revenue_try=gross,
                opex_try=annual_opex,
                replacement_try=replacement,
                net_try=net,
                discounted_net_try=_discount(net, item.year, params.discount_rate_real),
                cumulative_net_try=cumulative,
            )
        )

    result = FinanceResult(
        capex_try=capex,
        dc_capacity_kwp=dc_kwp,
        rows=tuple(rows),
        npv_try=net_present_value(capex, rows),
        irr_real=internal_rate_of_return(capex, [r.net_try for r in rows]),
        lcoe_try_kwh=levelised_cost(capex, rows, params.discount_rate_real),
        payback_years=_payback_years(capex, rows),
        discount_rate_real=params.discount_rate_real,
        exceedance=params.exceedance,
        blended_tariff_try_kwh=revenue.blended_tariff_try_kwh(),
    )
    logger.info(
        "fizibilite: %.0f kWp · yatırım %.2f M₺ · NPV %.2f M₺ · IRR %s · LCOE %.3f ₺/kWh · "
        "geri ödeme %s yıl",
        dc_kwp,
        capex / 1e6,
        result.npv_try / 1e6,
        f"{result.irr_real * 100:.1f}%" if result.irr_real is not None else "tanımsız",
        result.lcoe_try_kwh,
        f"{result.payback_years:.1f}" if result.payback_years is not None else "yok",
    )
    return result


def sensitivity(
    simulation: SimulationResult,
    costs: CostModel | None = None,
    revenue: RevenueModel | None = None,
    params: FinanceParams | None = None,
    deltas: tuple[float, ...] = (-0.20, -0.10, 0.0, 0.10, 0.20),
) -> dict[str, tuple[tuple[float, float], ...]]:
    """Tek değişkenli duyarlılık: her parametre için (değişim, NPV) çiftleri.

    Hangi varsayımın sonucu taşıdığını gösterir. Fizibilite tartışmalarında
    asıl soru "NPV kaç" değil, "hangi varsayım yanlışsa bu sayı çöker" olur.
    """
    base_costs = costs or CostModel()
    base_revenue = revenue or RevenueModel()
    base_params = params or FinanceParams()

    def scale_capex(d: float) -> tuple[CostModel, RevenueModel]:
        return (
            replace(base_costs, capex_per_kwp_try=base_costs.capex_per_kwp_try * (1 + d)),
            base_revenue,
        )

    def scale_opex(d: float) -> tuple[CostModel, RevenueModel]:
        return (
            replace(
                base_costs,
                opex_per_kwp_year_try=base_costs.opex_per_kwp_year_try * (1 + d),
            ),
            base_revenue,
        )

    def scale_retail(d: float) -> tuple[CostModel, RevenueModel]:
        return base_costs, replace(
            base_revenue,
            retail_tariff_try_kwh=base_revenue.retail_tariff_try_kwh * (1 + d),
        )

    def scale_share(d: float) -> tuple[CostModel, RevenueModel]:
        # Pay [0, 1] dışına taşamaz; RevenueModel doğrulaması aksi halde fırlatır
        shifted = min(1.0, max(0.0, base_revenue.self_consumption_share * (1 + d)))
        return base_costs, replace(base_revenue, self_consumption_share=shifted)

    axes = {
        "Yatırım maliyeti": scale_capex,
        "Perakende tarife": scale_retail,
        "Öztüketim payı": scale_share,
        "İşletme maliyeti": scale_opex,
    }
    return {
        label: tuple(
            (d, evaluate(simulation, *build(d), base_params).npv_try) for d in deltas
        )
        for label, build in axes.items()
    }
