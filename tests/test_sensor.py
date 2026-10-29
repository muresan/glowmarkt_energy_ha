"""Sensor states for consumption, cost and tariff, with the integration running in HA."""
from __future__ import annotations

from datetime import date, datetime

import pytest
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import entity_registry as er

from custom_components.glowmarkt_energy.const import DOMAIN

from .conftest import UTC
from .fake_glowmarkt import ELEC, ELEC_COST, GAS, GAS_COST, FakeGlowmarkt

pytestmark = pytest.mark.usefixtures("setup_integration")

ELEC_RATE, ELEC_STANDING = 26.11, 61.41  # pence, from tests/fixtures/tariff_electricity.json
GAS_RATE, GAS_STANDING = 7.871, 29.886


def state_of(hass: HomeAssistant, resource_id: str, key: str) -> State | None:
    entity_id = er.async_get(hass).async_get_entity_id("sensor", DOMAIN, f"{resource_id}_{key}")
    return hass.states.get(entity_id) if entity_id else None


def value_of(hass: HomeAssistant, resource_id: str, key: str) -> float:
    return float(state_of(hass, resource_id, key).state)


async def test_consumption_sensors(hass: HomeAssistant) -> None:
    # Latest complete hour is 00:00-01:00 UTC for both commodities: data stops at 00:30 / 01:00.
    last = state_of(hass, ELEC, "last_hour")
    assert float(last.state) == pytest.approx(0.02)
    assert last.attributes["unit_of_measurement"] == "kWh"
    assert last.attributes["device_class"] == "energy"
    assert "state_class" not in last.attributes  # the Energy dashboard uses the statistics
    assert last.attributes["period_start"] == "2026-10-29T00:00:00+00:00"
    assert last.attributes["period_end"] == "2026-10-29T01:00:00+00:00"
    assert value_of(hass, GAS, "last_hour") == pytest.approx(1.0)

    assert value_of(hass, ELEC, "today") == pytest.approx(0.02)  # two half-hours so far
    assert value_of(hass, ELEC, "yesterday") == pytest.approx(6.0)
    assert value_of(hass, ELEC, "week") == pytest.approx(3 * 6.0 + 0.02)  # Mon 26th - today
    # September and October up to the last reading; P1M buckets, as P1Y returns 0 for costs.
    assert value_of(hass, ELEC, "year") == pytest.approx(30 * 6.0 + 28 * 6.0 + 0.02)
    assert value_of(hass, GAS, "today") == pytest.approx(1.5)
    assert value_of(hass, GAS, "yesterday") == pytest.approx(24.0)


async def test_cost_sensors(hass: HomeAssistant) -> None:
    # Last hour = usage x rate plus the hour's share of the daily standing charge, pence -> GBP.
    last = state_of(hass, ELEC_COST, "last_hour")
    assert last.attributes["unit_of_measurement"] == "GBP"
    assert last.attributes["device_class"] == "monetary"
    assert float(last.state) == pytest.approx((0.02 * ELEC_RATE + ELEC_STANDING / 24) / 100)
    assert last.attributes["standing_charge"] == pytest.approx(ELEC_STANDING / 24 / 100, abs=1e-6)
    assert value_of(hass, GAS_COST, "last_hour") == pytest.approx(
        (GAS_RATE + GAS_STANDING / 24) / 100
    )
    assert "standing_charge" not in state_of(hass, ELEC, "last_hour").attributes

    # Daily and longer totals come from the API and include the standing charge.
    assert value_of(hass, ELEC_COST, "today") == pytest.approx(
        (0.02 * ELEC_RATE + ELEC_STANDING) / 100, abs=1e-4
    )
    assert value_of(hass, ELEC_COST, "yesterday") == pytest.approx(
        (6.0 * ELEC_RATE + ELEC_STANDING) / 100, abs=1e-4
    )
    assert value_of(hass, ELEC_COST, "week") == pytest.approx(
        (18.02 * ELEC_RATE + 4 * ELEC_STANDING) / 100, abs=1e-4
    )
    assert value_of(hass, GAS_COST, "yesterday") == pytest.approx(
        (24.0 * GAS_RATE + GAS_STANDING) / 100, abs=1e-4
    )
    assert value_of(hass, ELEC_COST, "year") > value_of(hass, ELEC_COST, "month") > 0


async def test_tariff_sensors(hass: HomeAssistant) -> None:
    rate = state_of(hass, ELEC, "unit_rate")
    assert float(rate.state) == pytest.approx(0.2611)
    assert rate.attributes["unit_of_measurement"] == "GBP/kWh"
    assert rate.attributes["tariff_name"] == "fromDCC"
    assert rate.attributes["tariff_type"] == "flatrate"
    assert rate.attributes["effective_from"] == "2026-10-19 00:00:00"
    assert "plan_detail" not in rate.attributes  # only shown for time-of-use / dynamic tariffs
    standing = state_of(hass, ELEC, "standing_charge")
    assert float(standing.state) == pytest.approx(0.6141)
    assert standing.attributes["unit_of_measurement"] == "GBP/day"

    gas_rate = state_of(hass, GAS, "unit_rate")
    assert float(gas_rate.state) == pytest.approx(0.07871)
    assert gas_rate.attributes["rate_GBP_per_m3"] == pytest.approx(0.84963)
    assert value_of(hass, GAS, "standing_charge") == pytest.approx(0.29886)

    # Tariff sensors hang off the consumption resource only.
    assert state_of(hass, ELEC_COST, "unit_rate") is None


async def test_data_until_and_entity_set(hass: HomeAssistant) -> None:
    assert state_of(hass, ELEC, "data_until").state == "2026-10-29T01:00:00+00:00"
    assert state_of(hass, GAS, "data_until").state == "2026-10-29T01:30:00+00:00"
    registry = er.async_get(hass)
    entry = registry.async_get(registry.async_get_entity_id("sensor", DOMAIN, f"{ELEC}_data_until"))
    assert entry.entity_category is EntityCategory.DIAGNOSTIC

    unique_ids = {e.unique_id for e in registry.entities.values() if e.platform == DOMAIN}
    totals = {"last_hour", "today", "yesterday", "week", "month", "year"}
    consumption_only = {"unit_rate", "standing_charge", "data_until"}
    expected = {f"{r}_{k}" for r in (ELEC, GAS) for k in totals | consumption_only}
    expected |= {f"{r}_{k}" for r in (ELEC_COST, GAS_COST) for k in totals}
    assert unique_ids == expected  # no 30-minute sensor any more
    device = state_of(hass, ELEC, "today").entity_id
    assert device.startswith("sensor.dcc_sourced_electricity")


async def test_day_rollover(hass: HomeAssistant, fake_api: FakeGlowmarkt, freezer) -> None:
    """At local midnight the totals move on even though no new readings arrived."""
    freezer.move_to(datetime(2026, 10, 30, 0, 10, tzinfo=UTC))
    fake_api.today = date(2026, 10, 30)
    await hass.data[DOMAIN][next(iter(hass.data[DOMAIN]))].async_refresh()
    await hass.async_block_till_done()

    assert value_of(hass, ELEC, "today") == 0
    assert value_of(hass, ELEC, "yesterday") == pytest.approx(0.02)
    assert value_of(hass, ELEC_COST, "today") == pytest.approx(ELEC_STANDING / 100)
    assert value_of(hass, ELEC, "last_hour") == pytest.approx(0.02)  # still the 00:00 hour
