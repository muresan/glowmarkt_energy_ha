"""Config flow (sign-in, re-auth) and options flow."""
from __future__ import annotations

from unittest.mock import patch

import aiohttp
import pytest
from homeassistant import config_entries
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.glowmarkt_energy.const import (
    CONF_APPLICATION_ID,
    CONF_BACKFILL_DAYS,
    CONF_SCAN_INTERVAL,
    CONF_TIMEZONE,
    DEFAULT_APPLICATION_ID,
    DOMAIN,
)

from .fake_glowmarkt import BASE, PASSWORD, USERNAME, FakeGlowmarkt

pytestmark = pytest.mark.usefixtures("recorder_mock")  # flows load the recorder dependency

USER_INPUT = {
    CONF_USERNAME: USERNAME,
    CONF_PASSWORD: PASSWORD,
    CONF_APPLICATION_ID: DEFAULT_APPLICATION_ID,
}


@pytest.fixture
def no_setup():
    with patch(
        "custom_components.glowmarkt_energy.async_setup_entry", return_value=True
    ) as mock_setup:
        yield mock_setup


async def test_user_flow(hass: HomeAssistant, fake_api: FakeGlowmarkt, no_setup) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == USERNAME
    assert result["data"] == USER_INPUT
    assert result["result"].unique_id == USERNAME
    assert len(no_setup.mock_calls) == 1


async def test_user_flow_invalid_auth(hass: HomeAssistant, fake_api: FakeGlowmarkt) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**USER_INPUT, CONF_PASSWORD: "wrong"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}


async def test_user_flow_cannot_connect(hass: HomeAssistant, aioclient_mock) -> None:
    aioclient_mock.post(f"{BASE}/auth", exc=aiohttp.ClientConnectionError())
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    assert result["errors"] == {"base": "cannot_connect"}


async def test_user_flow_already_configured(
    hass: HomeAssistant, fake_api: FakeGlowmarkt, config_entry: MockConfigEntry
) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**USER_INPUT, CONF_USERNAME: USERNAME.upper()}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_reauth(
    hass: HomeAssistant, fake_api: FakeGlowmarkt, config_entry: MockConfigEntry, no_setup
) -> None:
    fake_api.password = "new-secret"
    result = await config_entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PASSWORD: "still-wrong"}
    )
    assert result["errors"] == {"base": "invalid_auth"}
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PASSWORD: "new-secret"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert config_entry.data[CONF_PASSWORD] == "new-secret"


async def test_options_flow(
    hass: HomeAssistant, config_entry: MockConfigEntry, no_setup
) -> None:
    result = await hass.config_entries.options.async_init(config_entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    options = {CONF_SCAN_INTERVAL: 60, CONF_TIMEZONE: "Mars/Olympus", CONF_BACKFILL_DAYS: 90}
    result = await hass.config_entries.options.async_configure(result["flow_id"], options)
    assert result["errors"] == {CONF_TIMEZONE: "invalid_timezone"}
    options[CONF_TIMEZONE] = "Europe/London"
    result = await hass.config_entries.options.async_configure(result["flow_id"], options)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert config_entry.options == options
