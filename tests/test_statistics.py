"""Hourly long-term statistics (Energy dashboard) for consumption and cost."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from homeassistant.components.energy import data as energy_data
from homeassistant.components.energy import validate as energy_validate
from homeassistant.components.recorder.models import StatisticMeanType
from homeassistant.components.recorder.statistics import get_metadata, statistics_during_period
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.glowmarkt_energy.const import DOMAIN

from .conftest import ELEC_DATA_END, UTC, elec_kwh, setup_entry
from .fake_glowmarkt import ELEC_COST, FakeGlowmarkt

ELEC_SID = f"{DOMAIN}:electricity_consumption_e1ec0001"
ELEC_COST_SID = f"{DOMAIN}:electricity_consumption_cost_e1ec0c05"
GAS_SID = f"{DOMAIN}:gas_consumption_9a500001"
GAS_COST_SID = f"{DOMAIN}:gas_consumption_cost_9a500c05"
ELEC_RATE_GBP, GAS_RATE_GBP = 0.2611, 0.07871
ELEC_STANDING_GBP, GAS_STANDING_GBP = 0.6141, 0.29886
LONDON = ZoneInfo("Europe/London")
# The 30-day backfill covers 2026-09-29 01:00 - 2026-10-29 01:00 UTC: 22 of the 24 hours of
# local 29 September, 29 whole local days (including the 25-hour 25 October) and 1 hour of today.
BACKFILL_DAYS = 29 + 23 / 24


async def hourly_rows(hass: HomeAssistant, statistic_id: str) -> list[dict]:
    await async_wait_recording_done(hass)
    stats = await hass.async_add_executor_job(
        statistics_during_period,
        hass,
        datetime(2026, 1, 1, tzinfo=UTC),
        None,
        {statistic_id},
        "hour",
        None,
        {"state", "sum"},
    )
    return stats.get(statistic_id, [])


def start_of(row: dict) -> datetime:
    return datetime.fromtimestamp(row["start"], UTC)


def local_day_total(rows: list[dict], day: date) -> float:
    """Increase of the cumulative sum over one Europe/London day."""
    in_day = [r for r in rows if start_of(r).astimezone(LONDON).date() == day]
    before = [r for r in rows if start_of(r) < start_of(in_day[0])]
    return in_day[-1]["sum"] - before[-1]["sum"]


async def refresh(hass: HomeAssistant) -> None:
    coordinator = next(iter(hass.data[DOMAIN].values()))
    await coordinator.async_refresh()
    await hass.async_block_till_done()


@pytest.mark.usefixtures("setup_integration")
async def test_backfill_stops_at_last_reading(hass: HomeAssistant) -> None:
    """30 days of hourly rows, ending at the last complete hour rather than "now".

    The API answers 0 for 01:00-14:00 because the meter data has not arrived yet; importing those
    zeros would make the Energy dashboard show no usage for the morning.
    """
    rows = await hourly_rows(hass, ELEC_SID)
    assert len(rows) == 30 * 24
    assert start_of(rows[0]) == ELEC_DATA_END - timedelta(days=30)
    assert start_of(rows[-1]) == datetime(2026, 10, 29, 0, tzinfo=UTC)
    assert rows[-1]["sum"] == pytest.approx(30 * 6.0)  # every hour of the day, 30 times
    assert all(r["state"] == r["sum"] for r in rows)
    sums = [r["sum"] for r in rows]
    assert sums == sorted(sums)  # cumulative

    gas = await hourly_rows(hass, GAS_SID)
    assert start_of(gas[-1]) == datetime(2026, 10, 29, 0, tzinfo=UTC)  # 01:00 is half-filled
    assert gas[-1]["sum"] == pytest.approx(30 * 24 * 1.0)


@pytest.mark.usefixtures("setup_integration")
async def test_cost_statistics_include_standing_charge(hass: HomeAssistant) -> None:
    """Hourly cost = API pence / 100 plus an equal share of the local day's standing charge."""
    cost = await hourly_rows(hass, ELEC_COST_SID)
    energy = await hourly_rows(hass, ELEC_SID)
    assert [r["start"] for r in cost] == [r["start"] for r in energy]
    assert cost[0]["sum"] == pytest.approx(  # sums are stored rounded to 6 decimals
        energy[0]["sum"] * ELEC_RATE_GBP + ELEC_STANDING_GBP / 24, abs=1e-6
    )
    assert cost[-1]["sum"] == pytest.approx(
        30 * 6.0 * ELEC_RATE_GBP + BACKFILL_DAYS * ELEC_STANDING_GBP
    )

    # A whole local day adds up to the API's daily cost, i.e. the bill: one standing charge,
    # also on the 25-hour clock-change day (25 October 2026: 23:00 UTC on the 24th + 24 hours).
    assert local_day_total(cost, date(2026, 10, 27)) == pytest.approx(
        6.0 * ELEC_RATE_GBP + ELEC_STANDING_GBP
    )
    assert local_day_total(cost, date(2026, 10, 25)) == pytest.approx(
        (6.0 + 0.48) * ELEC_RATE_GBP + ELEC_STANDING_GBP
    )

    gas_cost = await hourly_rows(hass, GAS_COST_SID)
    assert gas_cost[-1]["sum"] == pytest.approx(
        30 * 24 * GAS_RATE_GBP + BACKFILL_DAYS * GAS_STANDING_GBP
    )


async def test_standing_charge_follows_tariff_history(
    recorder_mock, hass: HomeAssistant, fake_api: FakeGlowmarkt, config_entry: MockConfigEntry
) -> None:
    def entry(effective: str, standing: float) -> dict:
        return {"effectiveDate": effective, "plan": [{"planDetail": [{"standing": standing}]}]}

    fake_api.tariff_lists[ELEC_COST] = {
        "valid": True,
        "status": "OK",  # unsorted, as the API warns
        "data": [entry("2026-10-19 00:00:00", 61.41), entry("2026-10-01 00:00:00", 50.0)],
    }
    await setup_entry(hass, config_entry)
    cost = await hourly_rows(hass, ELEC_COST_SID)
    rate_only = 6.0 * ELEC_RATE_GBP
    assert local_day_total(cost, date(2026, 10, 10)) == pytest.approx(rate_only + 0.50)
    assert local_day_total(cost, date(2026, 10, 27)) == pytest.approx(rate_only + 0.6141)
    # Days before the oldest entry use it too, as the API's own daily cost does.
    assert local_day_total(cost, date(2026, 9, 30)) == pytest.approx(rate_only + 0.50)


async def test_backfill_starts_at_first_reading(
    recorder_mock, hass: HomeAssistant, fake_api: FakeGlowmarkt, config_entry: MockConfigEntry
) -> None:
    """Cost resources report 0, not null, before the meter's first reading: skip those days."""
    fake_api.half_hours["electricity"].clear()
    fake_api.fill("electricity", datetime(2026, 10, 20, tzinfo=UTC), ELEC_DATA_END, elec_kwh)
    await setup_entry(hass, config_entry)

    cost = await hourly_rows(hass, ELEC_COST_SID)
    assert start_of(cost[0]) == datetime(2026, 10, 20, tzinfo=UTC)
    assert len(cost) == 9 * 24 + 1
    # 23/24 of local 20 October (BST, it began 23:00 UTC on the 19th), 8 whole days, 1/24 of today
    assert cost[-1]["sum"] == pytest.approx(
        (9 * 6.0 + 0.02) * ELEC_RATE_GBP + 9 * ELEC_STANDING_GBP
    )


@pytest.mark.usefixtures("setup_integration")
async def test_statistics_metadata(hass: HomeAssistant) -> None:
    ids = {ELEC_SID, ELEC_COST_SID, GAS_SID, GAS_COST_SID}
    meta = await hass.async_add_executor_job(lambda: get_metadata(hass, statistic_ids=ids))
    assert set(meta) == ids
    elec, cost = meta[ELEC_SID][1], meta[ELEC_COST_SID][1]
    assert elec["name"] == "Electricity consumption (hourly)"
    assert elec["unit_of_measurement"] == "kWh"
    assert elec["unit_class"] == "energy"
    assert cost["name"] == "Electricity cost (hourly)"
    assert cost["unit_of_measurement"] == "GBP"
    assert cost["unit_class"] is None
    for _, m in meta.values():
        assert m["has_sum"] is True
        assert m["mean_type"] is StatisticMeanType.NONE
        assert m["source"] == DOMAIN
    assert meta[GAS_SID][1]["unit_of_measurement"] == "kWh"  # Bright reports gas in kWh


@pytest.mark.usefixtures("setup_integration")
async def test_late_data_extends_and_corrects(
    hass: HomeAssistant, fake_api: FakeGlowmarkt, freezer
) -> None:
    """New readings are appended and recent corrections re-imported without double counting."""
    before = {r["start"]: r["sum"] for r in await hourly_rows(hass, ELEC_SID)}

    # The meter catches up to 06:00, and 2026-10-28 22:00-23:00 is revised from 0.46 to 2.0 kWh.
    fake_api.fill("electricity", ELEC_DATA_END, ELEC_DATA_END + timedelta(hours=5), elec_kwh)
    fake_api.fill(
        "electricity",
        datetime(2026, 10, 28, 22, tzinfo=UTC),
        datetime(2026, 10, 28, 23, tzinfo=UTC),
        lambda _: 1.0,
    )
    freezer.move_to(datetime(2026, 10, 29, 15, 0, tzinfo=UTC))
    await refresh(hass)

    rows = await hourly_rows(hass, ELEC_SID)
    assert len(rows) == 30 * 24 + 5
    assert start_of(rows[-1]) == datetime(2026, 10, 29, 5, tzinfo=UTC)
    new_hours = sum(0.02 * (h + 1) for h in range(1, 6))
    assert rows[-1]["sum"] == pytest.approx(30 * 6.0 + (2.0 - 0.46) + new_hours)
    # Rows before the re-import window (last stored hour - 24 h) are untouched.
    old = [r for r in rows if start_of(r) < datetime(2026, 10, 28, tzinfo=UTC)]
    assert {r["start"]: r["sum"] for r in old} == {
        k: v for k, v in before.items() if k in {r["start"] for r in old}
    }

    cost = await hourly_rows(hass, ELEC_COST_SID)
    assert cost[-1]["sum"] == pytest.approx(
        rows[-1]["sum"] * ELEC_RATE_GBP + (BACKFILL_DAYS + 5 / 24) * ELEC_STANDING_GBP
    )


@pytest.mark.usefixtures("setup_integration")
async def test_unchanged_data_is_not_refetched(
    hass: HomeAssistant, fake_api: FakeGlowmarkt, freezer
) -> None:
    """A poll without new readings costs one /last-time request per resource."""
    readings_before = len(fake_api.readings_calls())
    calls_before = len(fake_api.calls)
    freezer.move_to(datetime(2026, 10, 29, 14, 40, tzinfo=UTC))  # within the catch-up interval
    await refresh(hass)

    assert len(fake_api.readings_calls()) == readings_before
    new = [path for path, _ in fake_api.calls[calls_before:]]
    assert len(new) == 4 and all(p.endswith("/last-time") for p in new)
    assert len(await hourly_rows(hass, ELEC_SID)) == 30 * 24


@pytest.mark.usefixtures("setup_integration")
async def test_energy_dashboard_accepts_statistics(hass: HomeAssistant) -> None:
    """Home Assistant's own Energy validation is happy with consumption + cost statistics."""
    assert await async_setup_component(hass, "energy", {})
    manager = await energy_data.async_get_manager(hass)
    await manager.async_update(
        {
            "energy_sources": [
                {
                    "type": "grid",
                    "stat_energy_from": ELEC_SID,
                    "stat_energy_to": None,
                    "stat_cost": ELEC_COST_SID,
                    "entity_energy_price": None,
                    "number_energy_price": None,
                    "stat_compensation": None,
                    "entity_energy_price_export": None,
                    "number_energy_price_export": None,
                    "cost_adjustment_day": 0.0,
                },
                {
                    "type": "gas",
                    "stat_energy_from": GAS_SID,
                    "stat_cost": GAS_COST_SID,
                    "entity_energy_price": None,
                    "number_energy_price": None,
                },
            ]
        }
    )
    result = (await energy_validate.async_validate(hass)).as_dict()
    assert result["energy_sources"] == [[], []]
