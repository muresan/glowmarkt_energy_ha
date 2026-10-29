"""Data update coordinator: polls the API, exposes sensor data, imports hourly statistics."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import StatisticData, StatisticMetaData
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_last_statistics,
    statistics_during_period,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

try:  # Home Assistant 2025.4+
    from homeassistant.components.recorder.models import StatisticMeanType
except ImportError:  # pragma: no cover - older Home Assistant
    StatisticMeanType = None  # type: ignore[assignment,misc]

from .api import GlowmarktApi, GlowmarktAuthError, GlowmarktError
from .const import (
    CLASSIFIERS,
    CONF_BACKFILL_DAYS,
    CONF_SCAN_INTERVAL,
    CONF_TIMEZONE,
    DCC_CATCHUP_AFTER_HOURS,
    DCC_CATCHUP_MIN_INTERVAL_MINUTES,
    DEFAULT_BACKFILL_DAYS,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_TIMEZONE,
    DOMAIN,
    HOURLY_CHUNK_DAYS,
    MAX_CATCHUP_DAYS,
    PERIOD_1H,
    REIMPORT_HOURS,
)
from .util import (
    UNIT_CLASSES,
    Tariff,
    complete_hours_end,
    cumulative,
    floor_to_step,
    hourly_points,
    normalise_unit,
    parse_tariff,
    period_windows,
    standing_charges,
    standing_shares,
    to_naive_utc,
    unit_scale,
)

_LOGGER = logging.getLogger(__name__)


@dataclass
class Resource:
    """A consumption or cost resource (electricity or gas) of a virtual entity."""

    resource_id: str
    classifier: str
    ve_id: str
    ve_name: str
    api_unit: str = "kWh"  # as reported by the API, e.g. kWh or pence

    @property
    def label(self) -> str:
        return CLASSIFIERS[self.classifier]

    @property
    def is_cost(self) -> bool:
        return self.classifier.endswith(".cost")

    @property
    def unit(self) -> str:
        return normalise_unit(self.api_unit)

    @property
    def scale(self) -> float:
        return unit_scale(self.api_unit)

    @property
    def statistic_id(self) -> str:
        suffix = self.resource_id.replace("-", "")[:8].lower()
        return f"{DOMAIN}:{self.classifier.replace('.', '_')}_{suffix}"

    @property
    def statistic_name(self) -> str:
        return f"{self.label} (hourly)" if self.is_cost else f"{self.label} consumption (hourly)"


@dataclass
class ResourceData:
    """Latest values for one resource, already converted to `Resource.unit`."""

    last_ts: int | None  # start of the latest half-hour with a reading (UTC epoch)
    local_date: date  # meter-local date the totals were fetched on
    # (UTC epoch of hour start, value, of which standing charge) - as imported into statistics
    latest_hour: tuple[int, float, float] | None
    totals: dict[str, float | None]  # today / yesterday / week / month / year
    tariff: Tariff | None = None  # consumption resources only; rates in pence


class GlowmarktCoordinator(DataUpdateCoordinator[dict[str, ResourceData]]):
    """Fetch data for all electricity / gas consumption and cost resources."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, api: GlowmarktApi) -> None:
        interval = entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            config_entry=entry,
            update_interval=timedelta(minutes=interval),
        )
        self.api = api
        self.resources: list[Resource] = []
        self._tz = None
        self._backfill_days = entry.options.get(CONF_BACKFILL_DAYS, DEFAULT_BACKFILL_DAYS)
        self._catchup_requested: dict[str, datetime] = {}  # resource id -> last request

    async def _async_update_data(self) -> dict[str, ResourceData]:
        try:
            if self._tz is None:
                name = self.config_entry.options.get(CONF_TIMEZONE, DEFAULT_TIMEZONE)
                self._tz = await dt_util.async_get_time_zone(name)
                if self._tz is None:
                    raise UpdateFailed(f"Unknown timezone: {name}")
            if not self.resources:
                await self._async_discover()
                if not self.resources:
                    raise UpdateFailed("No electricity or gas consumption resources found")

            data: dict[str, ResourceData] = {}
            errors: list[str] = []
            for res in self.resources:
                try:
                    data[res.resource_id] = await self._async_update_resource(res)
                except GlowmarktAuthError:
                    raise
                except GlowmarktError as err:
                    _LOGGER.warning("Update of %s failed: %s", res.statistic_id, err)
                    errors.append(str(err))
                    if self.data and res.resource_id in self.data:
                        data[res.resource_id] = self.data[res.resource_id]
            if not data:
                raise UpdateFailed("; ".join(errors))
            return data
        except GlowmarktAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except GlowmarktError as err:
            raise UpdateFailed(str(err)) from err

    async def _async_discover(self) -> None:
        """Find the electricity / gas consumption and cost resources of the account."""
        found: list[Resource] = []
        for ve in await self.api.get_virtual_entities():
            try:
                raw = await self.api.get_ve_resources(ve["veId"])
            except GlowmarktAuthError:
                raise
            except GlowmarktError:
                raw = [r for r in (ve.get("resources") or []) if isinstance(r, dict)]
            for item in raw:
                classifier = item.get("classifier")
                unit = item.get("baseUnit")
                if not classifier:
                    try:
                        detail = await self.api.get_resource(item["resourceId"])
                    except GlowmarktAuthError:
                        raise
                    except GlowmarktError:
                        continue
                    classifier, unit = detail.get("classifier"), detail.get("baseUnit")
                if classifier not in CLASSIFIERS:
                    continue
                found.append(
                    Resource(
                        resource_id=item["resourceId"],
                        classifier=classifier,
                        ve_id=ve["veId"],
                        ve_name=ve.get("name") or "Smart meter",
                        api_unit=unit or ("pence" if classifier.endswith(".cost") else "kWh"),
                    )
                )
        _LOGGER.debug("Discovered resources: %s", [r.statistic_id for r in found])
        self.resources = found

    async def _async_update_resource(self, res: Resource) -> ResourceData:
        now = dt_util.utcnow()
        local_date = now.astimezone(self._tz).date()
        last_ts = await self.api.get_last_time(res.resource_id)
        if not res.is_cost:  # the cost resource is derived from the same readings
            await self._async_request_catchup(res, last_ts, now)
        previous = (self.data or {}).get(res.resource_id)
        if previous and previous.last_ts == last_ts and previous.local_date == local_date:
            return previous  # no new readings since the last poll

        # Hourly resolution: import as long-term statistics for the Energy dashboard. Hours after
        # the latest reading are reported as 0 by the API, so stop at the last complete hour.
        latest_hour = None
        if last_ts is not None:
            latest_hour = await self._async_import_hourly(res, complete_hours_end(last_ts))

        # Period totals: day / week / month / year, aggregated by the API itself. For cost
        # resources these include the daily standing charge; the hourly values do not.
        totals: dict[str, float | None] = {}
        for window in period_windows(self._tz, now):
            result = await self.api.get_readings(
                res.resource_id, window.start, window.end, window.period, window.offset
            )
            totals[window.key] = (
                round(sum(v for _, v in result.points) * res.scale, 4) if result.points else None
            )

        tariff = previous.tariff if previous else None
        if not res.is_cost:
            try:
                tariff = parse_tariff(await self.api.get_tariff(res.resource_id))
            except GlowmarktAuthError:
                raise
            except GlowmarktError as err:
                _LOGGER.debug("Tariff of %s unavailable: %s", res.statistic_id, err)
        return ResourceData(
            last_ts=last_ts,
            local_date=local_date,
            latest_hour=latest_hour,
            totals=totals,
            tariff=tariff,
        )

    async def _async_request_catchup(
        self, res: Resource, last_ts: int | None, now: datetime
    ) -> None:
        """Ask Glowmarkt to fetch newer readings from the DCC when ours are old."""
        if last_ts is not None:
            data_end = dt_util.utc_from_timestamp(last_ts + 1800)
            if now - data_end < timedelta(hours=DCC_CATCHUP_AFTER_HOURS):
                return
        requested = self._catchup_requested.get(res.resource_id)
        if requested and now - requested < timedelta(minutes=DCC_CATCHUP_MIN_INTERVAL_MINUTES):
            return
        self._catchup_requested[res.resource_id] = now
        try:
            await self.api.catchup(res.resource_id)
        except GlowmarktAuthError:
            raise
        except GlowmarktError as err:
            _LOGGER.debug("Catch-up request for %s failed: %s", res.statistic_id, err)

    async def _async_import_hourly(
        self, res: Resource, until: datetime
    ) -> tuple[int, float, float] | None:
        """Import hourly values before `until` as external statistics (cumulative `sum`)."""
        sid = res.statistic_id
        instance = get_instance(self.hass)

        last = await instance.async_add_executor_job(
            get_last_statistics, self.hass, 1, sid, True, {"sum"}
        )
        if last.get(sid):
            last_start = dt_util.utc_from_timestamp(last[sid][0]["start"])
            start = max(
                last_start - timedelta(hours=REIMPORT_HOURS),
                until - timedelta(days=MAX_CATCHUP_DAYS),
            )
        else:
            start = until - timedelta(days=self._backfill_days)
            # Cost resources report 0 before the meter's first reading, not "no data".
            first_ts = await self.api.get_first_time(res.resource_id)
            if first_ts is not None:
                start = max(start, floor_to_step(dt_util.utc_from_timestamp(first_ts), 3600))

        points: dict[int, float] = {}
        cursor = start
        while cursor < until:
            chunk_end = min(cursor + timedelta(days=HOURLY_CHUNK_DAYS), until)
            # `to` is inclusive, so stop one second before the next chunk's first bucket.
            result = await self.api.get_readings(
                res.resource_id,
                to_naive_utc(cursor),
                to_naive_utc(chunk_end - timedelta(seconds=1)),
                PERIOD_1H,
                nulls=True,
            )
            if result.unit:
                res.api_unit = result.unit
            points.update(result.points)
            cursor = chunk_end

        hourly = [
            (start_dt, value * res.scale)
            for start_dt, value in hourly_points(list(points.items()), start, until)
        ]
        if not hourly:
            return None
        shares = [0.0] * len(hourly)
        if res.is_cost:
            # The API's hourly cost excludes the standing charge; spread each local day's charge
            # over its hours so that a day in the Energy dashboard matches the bill.
            history = await self._async_standing_charges(res)
            shares = [
                s * res.scale for s in standing_shares([h for h, _ in hourly], self._tz, history)
            ]
            hourly = [(h, v + s) for (h, v), s in zip(hourly, shares, strict=True)]

        # Continue the running total from the last hour stored before our window.
        base = 0.0
        prior = await instance.async_add_executor_job(
            statistics_during_period,
            self.hass,
            start - timedelta(days=30),
            start,
            {sid},
            "hour",
            None,
            {"sum"},
        )
        rows = prior.get(sid)
        if rows and rows[-1].get("sum") is not None:
            base = float(rows[-1]["sum"])

        stats = [
            StatisticData(start=s, state=total, sum=total)
            for s, total, _ in cumulative(hourly, base)
        ]
        async_add_external_statistics(self.hass, self._metadata(res), stats)
        last_start_dt, last_value = hourly[-1]
        return int(last_start_dt.timestamp()), last_value, shares[-1]

    async def _async_standing_charges(self, res: Resource) -> list[tuple[date, float]]:
        """Daily standing charge history of a cost resource (API units)."""
        try:
            history = standing_charges(await self.api.get_tariff_list(res.resource_id))
        except GlowmarktAuthError:
            raise
        except GlowmarktError as err:
            _LOGGER.warning("Tariff history of %s unavailable: %s", res.statistic_id, err)
            return []
        if not history:
            _LOGGER.warning("No standing charge in the tariff history of %s", res.statistic_id)
        return history

    @staticmethod
    def _metadata(res: Resource) -> StatisticMetaData:
        meta: dict = {
            "has_sum": True,
            "name": res.statistic_name,
            "source": DOMAIN,
            "statistic_id": res.statistic_id,
            "unit_of_measurement": res.unit,
        }
        # The metadata schema differs between Home Assistant versions.
        annotations = getattr(StatisticMetaData, "__annotations__", {})
        if "mean_type" in annotations and StatisticMeanType is not None:
            meta["mean_type"] = StatisticMeanType.NONE
        elif "has_mean" in annotations:
            meta["has_mean"] = False
        if "unit_class" in annotations:
            meta["unit_class"] = UNIT_CLASSES.get(res.unit)
        return StatisticMetaData(**meta)  # type: ignore[typeddict-item]
