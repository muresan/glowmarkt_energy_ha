"""Setup, unload and authentication failures while running."""
from __future__ import annotations

from datetime import timedelta

from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.glowmarkt_energy.const import DOMAIN

from .conftest import ELEC_DATA_END, NOW, elec_kwh, gas_kwh, setup_entry
from .fake_glowmarkt import ELEC, GAS, VE_ID, FakeGlowmarkt


def api_state(hass: HomeAssistant, key: str) -> State:
    entity_id = er.async_get(hass).async_get_entity_id("sensor", DOMAIN, f"{VE_ID}_{key}")
    return hass.states.get(entity_id)


async def refresh(hass: HomeAssistant) -> None:
    await hass.data[DOMAIN][next(iter(hass.data[DOMAIN]))].async_refresh()
    await hass.async_block_till_done()


async def test_setup_and_unload(hass: HomeAssistant, setup_integration: MockConfigEntry) -> None:
    assert setup_integration.state is ConfigEntryState.LOADED
    assert await hass.config_entries.async_unload(setup_integration.entry_id)
    assert setup_integration.state is ConfigEntryState.NOT_LOADED


async def test_bad_password_at_startup_starts_reauth(
    recorder_mock, hass: HomeAssistant, fake_api: FakeGlowmarkt, config_entry: MockConfigEntry
) -> None:
    fake_api.password = "changed"
    assert not await hass.config_entries.async_setup(config_entry.entry_id)
    assert config_entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress()
    assert [f["context"]["source"] for f in flows] == [SOURCE_REAUTH]


async def test_revoked_token_recovers(
    hass: HomeAssistant, setup_integration: MockConfigEntry, fake_api: FakeGlowmarkt, freezer
) -> None:
    """The API answers 404 (not 401) for an invalid token; the client logs in again."""
    fake_api.tokens.clear()
    fake_api.fill("electricity", ELEC_DATA_END, ELEC_DATA_END + timedelta(hours=1), elec_kwh)
    freezer.move_to(NOW + timedelta(hours=2))
    await refresh(hass)

    assert [p for p, _ in fake_api.calls].count("/auth") == 2
    coordinator = hass.data[DOMAIN][setup_integration.entry_id]
    assert coordinator.last_update_success
    assert coordinator.data[ELEC].last_ts == (ELEC_DATA_END + timedelta(minutes=30)).timestamp()


async def test_password_changed_while_running_starts_reauth(
    hass: HomeAssistant, setup_integration: MockConfigEntry, fake_api: FakeGlowmarkt, freezer
) -> None:
    fake_api.tokens.clear()
    fake_api.password = "changed"
    freezer.move_to(NOW + timedelta(hours=2))
    await refresh(hass)

    flows = hass.config_entries.flow.async_progress()
    assert [f["context"]["source"] for f in flows] == [SOURCE_REAUTH]
    # The data sensors go unavailable; the API status sensors stay and show the failure.
    today = er.async_get(hass).async_get_entity_id("sensor", DOMAIN, f"{ELEC}_today")
    assert hass.states.get(today).state == "unavailable"
    error = api_state(hass, "api_last_error")
    assert error.state == (NOW + timedelta(hours=2)).isoformat()
    assert error.attributes["error"] == "Authentication rejected (HTTP 401)"
    assert api_state(hass, "api_last_success").state == NOW.isoformat()


async def test_one_failing_resource_keeps_its_previous_data(
    hass: HomeAssistant, setup_integration: MockConfigEntry, fake_api: FakeGlowmarkt, freezer
) -> None:
    coordinator = hass.data[DOMAIN][setup_integration.entry_id]
    previous = coordinator.data[ELEC]
    fake_api.fail_paths[f"{ELEC}/last-time"] = 500
    freezer.move_to(NOW + timedelta(minutes=30))
    await refresh(hass)

    assert coordinator.last_update_success
    assert coordinator.data[ELEC] is previous
    # The diagnostic sensors show both: the other resources were fetched, this one failed.
    now = (NOW + timedelta(minutes=30)).isoformat()
    assert api_state(hass, "api_last_success").state == now
    error = api_state(hass, "api_last_error")
    assert error.state == now
    assert error.attributes["error"].startswith(f"HTTP 500 for /resource/{ELEC}/last-time")


async def test_api_status_when_every_request_fails(
    hass: HomeAssistant, setup_integration: MockConfigEntry, fake_api: FakeGlowmarkt, freezer
) -> None:
    fake_api.fail_paths["/last-time"] = 503
    freezer.move_to(NOW + timedelta(minutes=30))
    await refresh(hass)

    assert api_state(hass, "api_last_success").state == NOW.isoformat()  # unchanged
    assert api_state(hass, "api_last_error").state == (NOW + timedelta(minutes=30)).isoformat()


async def test_catchup_requested_when_data_is_old(
    hass: HomeAssistant, setup_integration: MockConfigEntry, fake_api: FakeGlowmarkt, freezer
) -> None:
    """Readings end at 01:00 / 01:30 and it is 14:20: ask for a DCC fetch, consumption only."""
    expected = [f"/resource/{ELEC}/catchup", f"/resource/{GAS}/catchup"]
    assert fake_api.paths("catchup") == expected

    freezer.move_to(NOW + timedelta(minutes=20))  # at most once per half-hour
    await refresh(hass)
    assert fake_api.paths("catchup") == expected

    freezer.move_to(NOW + timedelta(minutes=30))
    await refresh(hass)
    assert fake_api.paths("catchup") == expected * 2


async def test_no_catchup_when_data_is_recent(
    recorder_mock, hass: HomeAssistant, fake_api: FakeGlowmarkt, config_entry: MockConfigEntry
) -> None:
    # Readings up to 11:00 / 10:30 UTC: 3 h 20 and 3 h 50 old at 14:20.
    fake_api.fill("electricity", ELEC_DATA_END, NOW.replace(hour=11, minute=0), elec_kwh)
    fake_api.fill("gas", ELEC_DATA_END, NOW.replace(hour=10, minute=30), gas_kwh)
    await setup_entry(hass, config_entry)
    assert fake_api.paths("catchup") == []


async def test_failed_catchup_does_not_fail_the_update(
    recorder_mock, hass: HomeAssistant, fake_api: FakeGlowmarkt, config_entry: MockConfigEntry
) -> None:
    fake_api.fail_paths["/catchup"] = 500
    await setup_entry(hass, config_entry)
    assert len(fake_api.paths("catchup")) == 2
    assert config_entry.state is ConfigEntryState.LOADED
    assert hass.data[DOMAIN][config_entry.entry_id].data[ELEC].latest_hour is not None
    assert "/catchup" in api_state(hass, "api_last_error").attributes["error"]
