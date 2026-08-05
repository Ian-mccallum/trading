"""Chart geometry, computed server-side.

The math lives in Python (testable, one implementation) and the browser only
draws the resulting normalized points. That keeps the interesting logic — axis
framing, downsampling, direction — under unit test rather than buried in a
script tag.

Framing policy, which matters more than it sounds: the y-axis is NOT fitted
tightly to the data. With one-share paper positions the equity series moves by
tens of dollars on a six-figure account, and a tight fit would render that as
a dramatic mountain range. ``pad_fraction`` plus a floor on the visible range
keeps a flat series looking flat, which is the honest rendering.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

#: Minimum visible y-range as a fraction of the series value. A series varying
#: by less than this renders flat instead of being zoomed into a mountain.
#:
#: Calibration: with one-share positions the account moves by tens of dollars
#: on six figures (~0.03%), which must read flat. A genuine 1-2% portfolio move
#: must NOT. 0.25% sits between the two: comfortably above the noise, well
#: below any move worth looking at.
MIN_RANGE_FRACTION = 0.0025
#: Points kept after downsampling; more than this is invisible detail at
#: dashboard widths and just inflates the payload.
MAX_POINTS = 240


@dataclass(frozen=True)
class SeriesPoint:
    ts: str
    value: float
    x: float  # 0..1
    y: float  # 0..1, already flipped so 0 is the bottom of the chart


@dataclass(frozen=True)
class LineChart:
    points: list[SeriesPoint]
    first: float
    last: float
    minimum: float
    maximum: float
    y_min: float  # framed axis bounds, not data bounds
    y_max: float
    direction: str  # up | down | flat
    change_abs: float
    change_pct: float
    flat: bool  # true when movement is below the noise floor

    @property
    def has_shape(self) -> bool:
        return len(self.points) >= 2


@dataclass(frozen=True)
class AllocationSlice:
    label: str
    value: float
    fraction: float
    is_cash: bool = False


@dataclass(frozen=True)
class VolumeBar:
    label: str
    executed: int
    rejected: int
    total: int
    executed_fraction: float = field(default=0.0)


def downsample(values: list, limit: int = MAX_POINTS) -> list:
    """Evenly thin a series, always keeping the first and last points so the
    endpoints (which drive the displayed change) stay exact."""
    if len(values) <= limit:
        return list(values)
    step = (len(values) - 1) / (limit - 1)
    picked = [values[round(i * step)] for i in range(limit)]
    picked[-1] = values[-1]
    return picked


def build_line_chart(
    series: list[tuple[datetime, Decimal | float]],
    pad_fraction: float = 0.12,
) -> LineChart | None:
    """Normalize a time series into 0..1 chart space.

    Returns None for fewer than two points — the caller shows the
    "not enough history" state rather than a degenerate axis.
    """
    if len(series) < 2:
        return None

    thinned = downsample(series)
    values = [float(v) for _, v in thinned]
    first, last = values[0], values[-1]
    minimum, maximum = min(values), max(values)

    # Frame the axis honestly: never tighter than the noise floor.
    span = maximum - minimum
    reference = abs(maximum) or 1.0
    min_span = reference * MIN_RANGE_FRACTION
    flat = span < min_span
    if flat:
        centre = (maximum + minimum) / 2
        y_min, y_max = centre - min_span / 2, centre + min_span / 2
    else:
        pad = span * pad_fraction
        y_min, y_max = minimum - pad, maximum + pad

    axis_span = y_max - y_min or 1.0
    denominator = len(thinned) - 1
    points = [
        SeriesPoint(
            ts=ts.isoformat(),
            value=value,
            x=index / denominator,
            # Flip so 1.0 is the top of the chart in drawing space.
            y=(value - y_min) / axis_span,
        )
        for index, ((ts, _), value) in enumerate(zip(thinned, values, strict=True))
    ]

    change_abs = last - first
    change_pct = (change_abs / first) if first else 0.0
    if flat or change_abs == 0:
        direction = "flat"
    else:
        direction = "up" if change_abs > 0 else "down"

    return LineChart(
        points=points,
        first=first,
        last=last,
        minimum=minimum,
        maximum=maximum,
        y_min=y_min,
        y_max=y_max,
        direction=direction,
        change_abs=change_abs,
        change_pct=change_pct,
        flat=flat,
    )


def build_allocation(
    positions: list[tuple[str, Decimal | float]], cash: Decimal | float
) -> list[AllocationSlice]:
    """Portfolio weights including cash, largest holding first.

    Cash is always included and always last: a dashboard that shows only
    holdings implies full deployment, which is usually false here.
    """
    holdings = [(symbol, float(value)) for symbol, value in positions if float(value) > 0]
    cash_value = max(float(cash), 0.0)
    total = sum(value for _, value in holdings) + cash_value
    if total <= 0:
        return []
    holdings.sort(key=lambda item: item[1], reverse=True)
    slices = [
        AllocationSlice(label=symbol, value=value, fraction=value / total)
        for symbol, value in holdings
    ]
    slices.append(
        AllocationSlice(
            label="Cash", value=cash_value, fraction=cash_value / total, is_cash=True
        )
    )
    return slices


def build_volume(
    buckets: list[tuple[str, int, int]],
) -> list[VolumeBar]:
    """Executed-vs-rejected counts per day, scaled against the busiest day so
    a quiet day reads as quiet rather than being stretched to full height."""
    if not buckets:
        return []
    peak = max((executed + rejected) for _, executed, rejected in buckets) or 1
    return [
        VolumeBar(
            label=label,
            executed=executed,
            rejected=rejected,
            total=executed + rejected,
            executed_fraction=(executed + rejected) / peak,
        )
        for label, executed, rejected in buckets
    ]
