"""Pure helpers (no Home Assistant imports) so they can be unit-tested on their own."""
from __future__ import annotations

import calendar
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone, tzinfo
from typing import Any

from .const import PERIOD_1D, PERIOD_1M, PERIOD_1W

TIME_FORMAT = "%Y-%m-%dT%H:%M:%S"

_UNIT_ALIASES = {
    "kwh": "kWh",
    "wh": "Wh",
    "mwh": "MWh",
    "m3": "m³",
    "m³": "m³",
    "ft3": "ft³",
    "ft³": "ft³",
    "pence": "GBP",
    "gbp": "GBP",
}
# Cost resources report pence; Home Assistant wants an ISO 4217 currency.
_UNIT_SCALES = {"pence": 0.01}
UNIT_CLASSES = {"kWh": "energy", "Wh": "energy", "MWh": "energy", "m³": "volume", "ft³": "volume"}


def normalise_unit(unit: str | None) -> str:
    """Map the API's unit string onto a Home Assistant unit (default kWh)."""
    if not unit:
        return "kWh"
    return _UNIT_ALIASES.get(unit.strip().lower(), unit)


def unit_scale(unit: str | None) -> float:
    """Factor turning a value in the API's unit into `normalise_unit(unit)`."""
    return _UNIT_SCALES.get((unit or "").strip().lower(), 1.0)


@dataclass(frozen=True)
class PeriodWindow:
    """One aggregated query: the current/previous day, week, month or year."""

    key: str
    period: str
    start: datetime  # naive wall-clock time in the meter's timezone
    end: datetime  # naive wall-clock time in the meter's timezone
    offset: int  # minutes between local time and UTC, API sign convention (BST = -60)


def offset_minutes(tz: tzinfo, local_naive: datetime) -> int:
    """Return the API's `offset` value for a local wall-clock time (UTC+1 -> -60)."""
    delta = local_naive.replace(tzinfo=tz).utcoffset()
    return -int(delta.total_seconds() // 60) if delta else 0


def period_windows(tz: tzinfo, now_utc: datetime) -> list[PeriodWindow]:
    """Windows covering today, yesterday, this week (Mon-Sun), this month and this year."""
    today = now_utc.astimezone(tz).date()
    monday = today - timedelta(days=today.weekday())
    spec: list[tuple[str, str, date, date]] = [
        ("today", PERIOD_1D, today, today),
        ("yesterday", PERIOD_1D, today - timedelta(days=1), today - timedelta(days=1)),
        ("week", PERIOD_1W, monday, monday + timedelta(days=6)),
        (
            "month",
            PERIOD_1M,
            today.replace(day=1),
            today.replace(day=calendar.monthrange(today.year, today.month)[1]),
        ),
        ("year", PERIOD_1M, date(today.year, 1, 1), date(today.year, 12, 31)),
    ]
    windows = []
    for key, period, first, last in spec:
        start = datetime(first.year, first.month, first.day)
        end = datetime(last.year, last.month, last.day, 23, 59, 59)
        windows.append(PeriodWindow(key, period, start, end, offset_minutes(tz, start)))
    return windows


def floor_to_step(dt: datetime, seconds: int) -> datetime:
    """Floor an aware datetime to a multiple of `seconds` since the epoch (UTC)."""
    ts = int(dt.timestamp())
    return datetime.fromtimestamp(ts - ts % seconds, tz=timezone.utc)


def to_naive_utc(dt: datetime) -> datetime:
    """Aware datetime -> naive UTC datetime (what the API expects without an offset)."""
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


def complete_hours_end(last_ts: int) -> datetime:
    """End (exclusive) of the hourly buckets that are fully covered by readings.

    `last_ts` is the start of the latest half-hour with a reading (GET /resource/{id}/last-time).
    Later buckets are reported as 0 by the API until the smart-meter data arrives.
    """
    end = last_ts + 1800
    return datetime.fromtimestamp(end - end % 3600, tz=timezone.utc)


def hourly_points(
    points: list[tuple[int, float]],
    not_before: datetime | None = None,
    before: datetime | None = None,
) -> list[tuple[datetime, float]]:
    """De-duplicate, sort and keep only hour-aligned (UTC) buckets in [not_before, before)."""
    unique: dict[int, float] = {}
    for ts, value in points:
        if ts % 3600:
            continue
        unique[ts] = value
    out = [(datetime.fromtimestamp(ts, tz=timezone.utc), v) for ts, v in sorted(unique.items())]
    if not_before is not None:
        out = [p for p in out if p[0] >= not_before]
    if before is not None:
        out = [p for p in out if p[0] < before]
    return out


def cumulative(
    points: list[tuple[datetime, float]], base: float
) -> list[tuple[datetime, float, float]]:
    """Turn interval values into (start, running total, interval value) rows."""
    total = base
    rows = []
    for start, value in points:
        total += value
        rows.append((start, round(total, 6), value))
    return rows


@dataclass(frozen=True)
class Tariff:
    """Current tariff of a resource, in the API's units (pence)."""

    name: str | None
    type: str | None  # e.g. flatrate; the API also knows time-of-use, block and dynamic tariffs
    effective_from: str | None
    unit_rate: float | None  # per kWh
    standing_charge: float | None  # per day
    details: list[dict[str, Any]] = field(default_factory=list)  # planDetail entries as returned


def _number(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def local_day_hours(tz: tzinfo, day: date) -> int:
    """Length of a local day in hours (23 or 25 on clock-change days)."""
    start = datetime(day.year, day.month, day.day, tzinfo=tz)
    return round(((start + timedelta(days=1)).timestamp() - start.timestamp()) / 3600)


def standing_charges(entries: list[dict[str, Any]]) -> list[tuple[date, float]]:
    """(effective date, daily standing charge) from GET /resource/{id}/tariff-list, oldest first."""
    history = []
    for entry in entries:
        tariff = parse_tariff([entry])
        if tariff is None or tariff.standing_charge is None or not tariff.effective_from:
            continue
        try:
            effective = date.fromisoformat(tariff.effective_from[:10])
        except ValueError:
            continue
        history.append((effective, tariff.standing_charge))
    return sorted(history)


def standing_on(history: list[tuple[date, float]], day: date) -> float | None:
    """Standing charge for a local day. Like the API, use the oldest tariff for earlier days."""
    if not history:
        return None
    charge = history[0][1]
    for effective, value in history:
        if effective > day:
            break
        charge = value
    return charge


def standing_shares(
    hours: list[datetime], tz: tzinfo, history: list[tuple[date, float]]
) -> list[float]:
    """Each hour's equal share of its local day's standing charge."""
    day_hours: dict[date, int] = {}
    shares = []
    for start in hours:
        day = start.astimezone(tz).date()
        if day not in day_hours:
            day_hours[day] = local_day_hours(tz, day)
        shares.append((standing_on(history, day) or 0.0) / day_hours[day])
    return shares


def parse_tariff(entries: list[dict[str, Any]]) -> Tariff | None:
    """Pick the most recent entry of GET /resource/{id}/tariff and read its current rates."""
    entries = [e for e in entries if isinstance(e, dict)]
    if not entries:
        return None
    latest = max(entries, key=lambda e: str(e.get("from") or e.get("effectiveDate") or ""))
    details: list[dict[str, Any]] = []
    # `structure` replaces `plan`, which the API marks as a legacy structure.
    for plan in latest.get("structure") or latest.get("plan") or []:
        if isinstance(plan, dict):
            details.extend(d for d in plan.get("planDetail") or [] if isinstance(d, dict))
    current = latest.get("currentRates") if isinstance(latest.get("currentRates"), dict) else {}
    rate = _number(current.get("rate"))
    if rate is None:
        rate = next((_number(d["rate"]) for d in details if "rate" in d), None)
    standing = _number(current.get("standingCharge"))
    if standing is None:
        standing = next((_number(d["standing"]) for d in details if "standing" in d), None)
    return Tariff(
        name=latest.get("name") or latest.get("displayName"),
        type=latest.get("type"),
        effective_from=latest.get("from") or latest.get("effectiveDate"),
        unit_rate=rate,
        standing_charge=standing,
        details=details,
    )
