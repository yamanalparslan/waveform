"""PVGIS TMY ve ufuk profili çözümlemesi.

Bu dosyanın konusu üç sessiz sistematik hata: damga ötelemesi, basınç birimi ve
kaynak yılın indekste kalması. Üçü de hata vermez, yalnızca sonucu kaydırır —
dolayısıyla yalnızca test yakalayabilir.
"""

import httpx
import pandas as pd
import pytest
import respx

from luminmind.adapters.base import AdapterError
from luminmind.prospect.pvgis import (
    PVGIS_BASE_URL,
    TMY_INTERVAL,
    TMY_REFERENCE_YEAR,
    HorizonProfile,
    PvgisClient,
    parse_horizon,
    parse_tmy,
)

KONYA_LAT, KONYA_LON = 37.87, 32.48
OFFSET_H = 0.1712  # PVGIS'in Konya için verdiği tipik öteleme

# TMY'nin ayları farklı kaynak yıllardan seçilir — indekste kalırlarsa monotonluk
# kaybolur. Çözümleyicinin hepsini tek referans yıla taşıdığı sınanıyor.
SOURCE_YEARS = {
    1: 2018, 2: 2011, 3: 2020, 4: 2009, 5: 2016, 6: 2013,
    7: 2019, 8: 2007, 9: 2021, 10: 2012, 11: 2015, 12: 2010,
}


def tmy_rows() -> list[dict[str, object]]:
    """8760 satırlık sentetik TMY. Değerler basit ama birimler gerçek PVGIS'in."""
    rows: list[dict[str, object]] = []
    for month in range(1, 13):
        days = [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1]
        for day in range(1, days + 1):
            for hour in range(24):
                # Kaba günlük ışınım profili — gece 0, öğle tepe
                ghi = max(0.0, 800.0 * (1.0 - abs(hour - 12) / 6.0))
                rows.append(
                    {
                        "time(UTC)": f"{SOURCE_YEARS[month]}{month:02d}{day:02d}:{hour:02d}10",
                        "G(h)": ghi,
                        "Gb(n)": ghi * 0.75,
                        "Gd(h)": ghi * 0.25,
                        "T2m": 15.0 + 10.0 * (hour / 24.0),
                        "WS10m": 2.5,
                        "RH": 45.0,
                        "SP": 91_000.0,  # Pa — zincir hPa bekliyor
                        "IR(h)": 300.0,  # kullanılmıyor
                        "WD10m": 180.0,  # kullanılmıyor
                    }
                )
    return rows


def tmy_payload(**overrides) -> dict[str, object]:
    payload: dict[str, object] = {
        "inputs": {
            "location": {
                "latitude": KONYA_LAT,
                "longitude": KONYA_LON,
                "elevation": 1_020.0,
                "irradiance_time_offset": OFFSET_H,
            },
            "meteo_data": {
                "radiation_db": "PVGIS-SARAH3",
                "meteo_db": "ERA5",
                "year_min": 2005,
                "year_max": 2023,
                "use_horizon": True,
            },
        },
        "outputs": {
            "tmy_hourly": tmy_rows(),
            "months_selected": [
                {"month": month, "year": year} for month, year in SOURCE_YEARS.items()
            ],
        },
    }
    for key, value in overrides.items():
        payload[key] = value
    return payload


@pytest.fixture(scope="module")
def dataset():
    return parse_tmy(tmy_payload())


# ------------------------------ Yapı ve künye ------------------------------


def test_parse_tmy_reads_the_full_year(dataset):
    assert len(dataset.weather) == 8760
    assert TMY_INTERVAL.total_seconds() == 3600.0


def test_parse_tmy_maps_columns_to_the_chain_contract(dataset):
    """`run_chain` bu sütun adlarını bekliyor; eşleme kayarsa zincir sessizce boş kalır."""
    assert set(dataset.weather.columns) == {
        "ghi", "dni", "dhi", "temp_air", "wind_speed", "relative_humidity", "pressure",
    }


def test_parse_tmy_reads_location_metadata(dataset):
    assert dataset.latitude == pytest.approx(KONYA_LAT)
    assert dataset.longitude == pytest.approx(KONYA_LON)
    assert dataset.altitude_m == pytest.approx(1_020.0)
    assert dataset.site.altitude_m == pytest.approx(1_020.0)


def test_provenance_names_the_databases_and_period(dataset):
    """Rapor bu satırı basıyor — kullanıcı hangi veriye baktığını görmeli."""
    provenance = dataset.provenance

    assert "PVGIS-SARAH3" in provenance
    assert "ERA5" in provenance
    assert "2005–2023" in provenance
    assert "1020 m" in provenance
    assert "dahil" in provenance, "TMY arazi ufkunu içeriyor"


def test_months_selected_records_the_source_years(dataset):
    assert len(dataset.months_selected) == 12
    assert dict(dataset.months_selected) == SOURCE_YEARS


# ------------------------------ Damga sözleşmesi ------------------------------


def test_index_is_moved_to_a_single_reference_year(dataset):
    """Kaynak yıl indekste kalırsa ay sınırlarında geriye atlar ve `resample` bozulur."""
    years = set(dataset.weather.index.year)

    assert years == {TMY_REFERENCE_YEAR}
    assert dataset.weather.index.is_monotonic_increasing
    assert not dataset.weather.index.has_duplicates


def test_reference_year_is_not_a_leap_year():
    """8760 satır varsayımı buna dayanıyor."""
    assert TMY_REFERENCE_YEAR % 4 != 0


def test_irradiance_time_offset_is_applied_to_the_index(dataset):
    """Damga değerin temsil ettiği an değil; öteleme uygulanmazsa geometri ~10 dk kayar.

    Yanlış öteleme iki kez zarar veriyor: güneş açısı kayıyor *ve* GHI ≈ DNI·cos z
    + DHI kapanışı bozulduğu için `reconcile_irradiance` yüksek kaliteli uydu
    DNI'sını Erbs tahminiyle eziyor.
    """
    stamp = pd.Timestamp(year=TMY_REFERENCE_YEAR, month=1, day=1, hour=0, minute=10, tz="UTC")
    expected = stamp + pd.Timedelta(hours=OFFSET_H)

    assert dataset.irradiance_time_offset_h == pytest.approx(OFFSET_H)
    assert dataset.weather.index[0] == expected
    # Ötelemenin gerçekten uygulandığı: ham damga :10, temsil edilen an :20'nin ötesi
    assert dataset.weather.index[0] - stamp == pd.Timedelta(hours=OFFSET_H)


def test_index_is_utc(dataset):
    assert str(dataset.weather.index.tz) == "UTC"


def test_missing_offset_falls_back_to_zero_with_a_warning(caplog):
    payload = tmy_payload()
    del payload["inputs"]["location"]["irradiance_time_offset"]  # type: ignore[index]

    with caplog.at_level("WARNING"):
        parsed = parse_tmy(payload)

    assert parsed.irradiance_time_offset_h == 0.0
    assert "irradiance_time_offset" in caplog.text


# ------------------------------ Birimler ------------------------------


def test_pressure_is_converted_from_pascal_to_hectopascal(dataset):
    """Bölme atlanırsa mutlak hava kütlesi 100 kat sapar."""
    assert dataset.weather["pressure"].iloc[0] == pytest.approx(910.0)
    assert dataset.weather["pressure"].max() < 1_100.0


def test_annual_ghi_sums_hourly_averages(dataset):
    """Saatlik ortalama × 1 sa = Wh/m²; kWh'a bölünür."""
    expected = float(dataset.weather["ghi"].sum()) / 1000.0
    assert dataset.annual_ghi_kwh_m2 == pytest.approx(expected)
    assert dataset.annual_ghi_kwh_m2 > 0.0


def test_mean_temperature_is_read_from_the_frame(dataset):
    assert dataset.mean_temp_c == pytest.approx(float(dataset.weather["temp_air"].mean()))


def test_unused_columns_are_dropped(dataset):
    """IR(h) ve WD10m zincire girmiyor; taşımak belleği ve karışıklığı artırır."""
    assert "IR(h)" not in dataset.weather.columns
    assert "WD10m" not in dataset.weather.columns


def test_missing_values_become_nan_not_zero():
    """Eksik veriyi 0 saymak onu geceden ayırt edilemez kılardı."""
    payload = tmy_payload()
    payload["outputs"]["tmy_hourly"][12]["G(h)"] = None  # type: ignore[index]

    parsed = parse_tmy(payload)

    assert parsed.weather["ghi"].isna().sum() == 1


# ------------------------------ Hata yolları ------------------------------


def test_parse_tmy_rejects_a_short_year():
    """Yıllık toplamlar 8760 satır varsayımına dayanıyor; eksik satır sessizce
    düşük üretim raporlardı."""
    payload = tmy_payload()
    payload["outputs"]["tmy_hourly"] = payload["outputs"]["tmy_hourly"][:8000]  # type: ignore[index]

    with pytest.raises(AdapterError, match="8760"):
        parse_tmy(payload)


def test_parse_tmy_rejects_an_empty_response():
    payload = tmy_payload()
    payload["outputs"]["tmy_hourly"] = []  # type: ignore[index]

    with pytest.raises(AdapterError, match="boş"):
        parse_tmy(payload)


def test_parse_tmy_rejects_a_missing_field():
    payload = tmy_payload()
    del payload["inputs"]["meteo_data"]  # type: ignore[index]

    with pytest.raises(AdapterError, match="meteo_data"):
        parse_tmy(payload)


def test_parse_tmy_rejects_a_malformed_timestamp():
    payload = tmy_payload()
    payload["outputs"]["tmy_hourly"][5]["time(UTC)"] = "2018-01-01 05:00"  # type: ignore[index]

    with pytest.raises(AdapterError, match="çözümlenemedi"):
        parse_tmy(payload)


def test_parse_tmy_rejects_a_leap_day():
    """29 Şubat referans yılda yok; sessizce atlamak indeksi bir gün kaydırırdı."""
    payload = tmy_payload()
    payload["outputs"]["tmy_hourly"][40]["time(UTC)"] = "20200229:1210"  # type: ignore[index]

    with pytest.raises(AdapterError, match="geçersiz"):
        parse_tmy(payload)


def test_parse_tmy_rejects_out_of_order_rows():
    """Aylar sırasız gelirse referans yıla taşındıktan sonra indeks monotonluğunu
    kaybeder; `resample`/`ffill` sessizce yanlış sonuç verir."""
    payload = tmy_payload()
    rows = payload["outputs"]["tmy_hourly"]  # type: ignore[index]
    rows[100], rows[200] = rows[200], rows[100]

    with pytest.raises(AdapterError, match="monotonik"):
        parse_tmy(payload)


# ------------------------------ Ufuk profili ------------------------------


def horizon_payload(points: list[tuple[float, float]]) -> dict[str, object]:
    return {"outputs": {"horizon_profile": [{"A": a, "H_hor": h} for a, h in points]}}


def test_parse_horizon_converts_south_reference_to_pvlib_azimuth():
    """PVGIS A = 0 güneyi, batıya pozitif; pvlib 0 = kuzey, doğuya pozitif."""
    profile = parse_horizon(horizon_payload([(0.0, 5.0), (90.0, 8.0), (-90.0, 3.0)]))

    mapping = dict(zip(profile.azimuth_deg, profile.elevation_deg, strict=True))
    assert mapping[180.0] == pytest.approx(5.0)  # güney
    assert mapping[270.0] == pytest.approx(8.0)  # batı
    assert mapping[90.0] == pytest.approx(3.0)  # doğu


def test_parse_horizon_sorts_by_azimuth():
    """`elevation_at` np.interp kullanıyor; sıralı olmayan x dizisi sessizce yanlış verir."""
    profile = parse_horizon(horizon_payload([(90.0, 8.0), (-90.0, 3.0), (0.0, 5.0)]))
    assert list(profile.azimuth_deg) == sorted(profile.azimuth_deg)


def test_parse_horizon_skips_unusable_points():
    payload = {
        "outputs": {
            "horizon_profile": [
                {"A": 0.0, "H_hor": 5.0},
                {"A": 45.0, "H_hor": None},
                {"A": None, "H_hor": 7.0},
            ]
        }
    }
    profile = parse_horizon(payload)

    assert len(profile.azimuth_deg) == 1


def test_parse_horizon_rejects_an_empty_profile():
    with pytest.raises(AdapterError, match="boş"):
        parse_horizon({"outputs": {"horizon_profile": []}})


def test_parse_horizon_rejects_an_all_invalid_profile():
    with pytest.raises(AdapterError, match="geçerli nokta yok"):
        parse_horizon({"outputs": {"horizon_profile": [{"A": None, "H_hor": None}]}})


def test_flat_horizon_does_not_warn_the_user():
    """3°'nin altı pratikte düz arazi; "ufuk engeli var" demek yanıltıcı olur."""
    flat = parse_horizon(horizon_payload([(0.0, 1.0), (90.0, 2.5), (-90.0, 0.5)]))

    assert flat.is_flat
    assert flat.max_elevation_deg == pytest.approx(2.5)


def test_raised_horizon_is_reported():
    valley = parse_horizon(horizon_payload([(0.0, 1.0), (90.0, 14.0)]))

    assert not valley.is_flat
    assert valley.max_elevation_deg == pytest.approx(14.0)


def test_elevation_at_interpolates_between_samples():
    profile = HorizonProfile(azimuth_deg=(90.0, 180.0), elevation_deg=(4.0, 10.0))
    assert profile.elevation_at(135.0) == pytest.approx(7.0)


def test_elevation_at_wraps_around_north():
    """Ufuk daireseldir; 350° ile 10° arası kuzeyden geçerek aradeğerlenmeli."""
    profile = HorizonProfile(azimuth_deg=(10.0, 350.0), elevation_deg=(2.0, 6.0))

    assert profile.elevation_at(0.0) == pytest.approx(4.0)
    assert profile.elevation_at(360.0) == pytest.approx(4.0)
    assert profile.elevation_at(-10.0) == pytest.approx(profile.elevation_at(350.0))


def test_empty_horizon_is_flat_everywhere():
    empty = HorizonProfile(azimuth_deg=(), elevation_deg=())

    assert empty.is_flat
    assert empty.max_elevation_deg == 0.0
    assert empty.elevation_at(180.0) == 0.0


# ------------------------------ İstemci ------------------------------


@respx.mock
async def test_fetch_tmy_calls_the_versioned_endpoint():
    route = respx.get(f"{PVGIS_BASE_URL}/api/v5_3/tmy").mock(
        return_value=httpx.Response(200, json=tmy_payload())
    )

    async with PvgisClient() as client:
        dataset = await client.fetch_tmy(KONYA_LAT, KONYA_LON)

    assert route.called
    params = route.calls.last.request.url.params
    assert params["lat"] == str(KONYA_LAT)
    assert params["lon"] == str(KONYA_LON)
    assert params["outputformat"] == "json"
    assert len(dataset.weather) == 8760


@respx.mock
async def test_fetch_horizon_uses_the_printhorizon_endpoint():
    route = respx.get(f"{PVGIS_BASE_URL}/api/v5_3/printhorizon").mock(
        return_value=httpx.Response(200, json=horizon_payload([(0.0, 4.0), (90.0, 6.0)]))
    )

    async with PvgisClient() as client:
        profile = await client.fetch_horizon(KONYA_LAT, KONYA_LON)

    assert route.called
    assert profile.max_elevation_deg == pytest.approx(6.0)


@respx.mock
async def test_non_json_response_becomes_an_adapter_error():
    respx.get(f"{PVGIS_BASE_URL}/api/v5_3/tmy").mock(
        return_value=httpx.Response(200, text="<html>service unavailable</html>")
    )

    async with PvgisClient() as client:
        with pytest.raises(AdapterError, match="JSON değil"):
            await client.fetch_tmy(KONYA_LAT, KONYA_LON)


@respx.mock
async def test_json_array_response_becomes_an_adapter_error():
    respx.get(f"{PVGIS_BASE_URL}/api/v5_3/tmy").mock(
        return_value=httpx.Response(200, json=[1, 2, 3])
    )

    async with PvgisClient() as client:
        with pytest.raises(AdapterError, match="sözlük değil"):
            await client.fetch_tmy(KONYA_LAT, KONYA_LON)


@respx.mock
async def test_out_of_coverage_coordinate_raises():
    """PVGIS deniz/kapsama dışı koordinatta 4xx döner (`request_with_retry` çevirir)."""
    respx.get(f"{PVGIS_BASE_URL}/api/v5_3/tmy").mock(return_value=httpx.Response(400))

    async with PvgisClient() as client:
        with pytest.raises(AdapterError):
            await client.fetch_tmy(0.0, -30.0)
