from datetime import datetime
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query

from luminmind.analytics.comparison import plant_actual_from_samples
from luminmind.api.deps import (
    TimeseriesSource,
    get_current_user,
    get_influx,
    get_tz,
    in_tz,
)
from luminmind.api.routers.plants import get_plant_or_404
from luminmind.api.schemas import ComparisonPoint, SeriesPoint
from luminmind.core.models import Plant, User

router = APIRouter(prefix="/plants/{plant_id}", tags=["timeseries"])

# Çözünürlük başına geçerli metrikler (Influx measurement field'larıyla eşleşir)
_METRICS: dict[str, frozenset[str]] = {
    "15m": frozenset({"ac_power_kw", "dc_power_kw", "temp_c", "energy_total_kwh"}),
    "1h": frozenset({"ac_power_kw_mean", "ac_power_kw_max", "energy_kwh"}),
    "1d": frozenset({"energy_kwh", "peak_ac_power_kw"}),
}
_MIN_EXPECTED_KW = 10.0


@router.get("/timeseries", response_model=list[SeriesPoint])
async def timeseries(
    plant: Annotated[Plant, Depends(get_plant_or_404)],
    _user: Annotated[User, Depends(get_current_user)],
    source: Annotated[TimeseriesSource, Depends(get_influx)],
    zone: Annotated[ZoneInfo | None, Depends(get_tz)],
    start: Annotated[datetime, Query()],
    end: Annotated[datetime, Query()],
    metric: Annotated[str, Query()] = "ac_power_kw",
    resolution: Annotated[Literal["15m", "1h", "1d"], Query()] = "15m",
) -> list[SeriesPoint]:
    if metric not in _METRICS[resolution]:
        raise HTTPException(
            status_code=422,
            detail=f"metric {metric!r} not available at {resolution}; "
            f"valid: {sorted(_METRICS[resolution])}",
        )
    series = await source.query_plant_series(
        plant.vendor_plant_id, metric, start, end, resolution
    )
    return [SeriesPoint(ts=in_tz(ts, zone), value=value) for ts, value in series]


@router.get("/comparison", response_model=list[ComparisonPoint])
async def comparison(
    plant: Annotated[Plant, Depends(get_plant_or_404)],
    _user: Annotated[User, Depends(get_current_user)],
    source: Annotated[TimeseriesSource, Depends(get_influx)],
    zone: Annotated[ZoneInfo | None, Depends(get_tz)],
    start: Annotated[datetime, Query()],
    end: Annotated[datetime, Query()],
) -> list[ComparisonPoint]:
    expected = (await source.query_twin_window(start, end)).get(plant.vendor_plant_id, {})
    actual = plant_actual_from_samples(await source.query_raw_window(start, end)).get(
        plant.vendor_plant_id, {}
    )
    points: list[ComparisonPoint] = []
    for ts in sorted(expected):
        expected_kw = expected[ts]
        actual_kw = actual.get(ts)
        deviation = (
            None
            if actual_kw is None or expected_kw < _MIN_EXPECTED_KW
            else round((actual_kw - expected_kw) / expected_kw * 100.0, 2)
        )
        points.append(
            ComparisonPoint(
                ts=in_tz(ts, zone),
                actual_kw=actual_kw,
                expected_kw=expected_kw,
                deviation_pct=deviation,
            )
        )
    return points
