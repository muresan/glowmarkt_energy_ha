"""Shared fixtures: a frozen clock, the fake Glowmarkt API and a config entry.

Scenario: it is Thursday 2026-10-29 14:20 UTC (GMT, so local = UTC). Readings exist from
2026-09-01; like the live account they lag by hours: electricity up to the half-hour starting
00:30, gas up to the one starting 01:00. The API reports 0 for every half-hour after that.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import pytest
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.glowmarkt_energy.const import (
    CONF_APPLICATION_ID,
    DEFAULT_APPLICATION_ID,
    DOMAIN,
)

from .fake_glowmarkt import PASSWORD, USERNAME, FakeGlowmarkt

# The test plugin logs every SQL statement; the statistics tests would drown in it.
logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)

UTC = timezone.utc
NOW =datetime(2026, 10, 29, 14, 20, tzinfo=UTC)
DATA_START = datetime(2026, 9, 1, tzinfo=UTC)
ELEC_DATA_END = datetime(2026, 10, 29, 1, 0, tzinfo=UTC)  # lastTs 00:30
GAS_DATA_END = datetime(2026, 10, 29, 1, 30, tzinfo=UTC)  # lastTs 01:00


def elec_kwh(slot: datetime) -> float:
    """Half-hourly electricity: (hour + 1) / 100 kWh, so an hour is 0.02 * (hour + 1), a day 6."""
    return (slot.hour + 1) / 100


def gas_kwh(slot: datetime) -> float:
    """Half-hourly gas: 0.5 kWh, so 1 kWh per hour and 24 kWh per day."""
    return 0.5


@pytest.fixture(autouse=True)
def mock_recorder_before_hass(async_setup_recorder_instance) -> None:
    """Prepare the recorder database before `hass` (the integration depends on recorder)."""


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


@pytest.fixture
def fake_api(aioclient_mock, freezer) -> FakeGlowmarkt:
    freezer.move_to(NOW)
    fake = FakeGlowmarkt()
    fake.fill("electricity", DATA_START, ELEC_DATA_END, elec_kwh)
    fake.fill("gas", DATA_START, GAS_DATA_END, gas_kwh)
    fake.register(aioclient_mock)
    return fake


@pytest.fixture
def config_entry(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=USERNAME,
        title=USERNAME,
        data={
            CONF_USERNAME: USERNAME,
            CONF_PASSWORD: PASSWORD,
            CONF_APPLICATION_ID: DEFAULT_APPLICATION_ID,
        },
    )
    entry.add_to_hass(hass)
    return entry


async def setup_entry(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """Set up the integration and wait until its statistics are written."""
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    await async_wait_recording_done(hass)


@pytest.fixture
async def setup_integration(
    recorder_mock, hass: HomeAssistant, fake_api: FakeGlowmarkt, config_entry: MockConfigEntry
) -> MockConfigEntry:
    await setup_entry(hass, config_entry)
    return config_entry
