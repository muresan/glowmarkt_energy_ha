"""Config and options flow."""
from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)
from homeassistant.util import dt as dt_util

from .api import GlowmarktApi, GlowmarktAuthError, GlowmarktError
from .const import (
    CONF_APPLICATION_ID,
    CONF_BACKFILL_DAYS,
    CONF_SCAN_INTERVAL,
    CONF_TIMEZONE,
    DEFAULT_APPLICATION_ID,
    DEFAULT_BACKFILL_DAYS,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_TIMEZONE,
    DOMAIN,
)

PASSWORD_SELECTOR = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))


class GlowmarktConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the initial setup and re-authentication."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return GlowmarktOptionsFlow(config_entry)

    async def _async_check(self, username: str, password: str, application_id: str) -> str | None:
        api = GlowmarktApi(async_get_clientsession(self.hass), username, password, application_id)
        try:
            await api.authenticate()
        except GlowmarktAuthError:
            return "invalid_auth"
        except GlowmarktError:
            return "cannot_connect"
        return None

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            await self.async_set_unique_id(user_input[CONF_USERNAME].strip().lower())
            self._abort_if_unique_id_configured()
            error = await self._async_check(
                user_input[CONF_USERNAME],
                user_input[CONF_PASSWORD],
                user_input[CONF_APPLICATION_ID],
            )
            if error is None:
                return self.async_create_entry(title=user_input[CONF_USERNAME], data=user_input)
            errors["base"] = error

        schema = vol.Schema(
            {
                vol.Required(CONF_USERNAME, default=(user_input or {}).get(CONF_USERNAME, "")): str,
                vol.Required(CONF_PASSWORD): PASSWORD_SELECTOR,
                vol.Optional(CONF_APPLICATION_ID, default=DEFAULT_APPLICATION_ID): str,
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        entry = self.hass.config_entries.async_get_entry(self.context["entry_id"])
        assert entry is not None
        errors: dict[str, str] = {}
        if user_input is not None:
            error = await self._async_check(
                entry.data[CONF_USERNAME],
                user_input[CONF_PASSWORD],
                entry.data.get(CONF_APPLICATION_ID, DEFAULT_APPLICATION_ID),
            )
            if error is None:
                return self.async_update_reload_and_abort(
                    entry, data={**entry.data, CONF_PASSWORD: user_input[CONF_PASSWORD]}
                )
            errors["base"] = error
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_PASSWORD): PASSWORD_SELECTOR}),
            description_placeholders={"username": entry.data[CONF_USERNAME]},
            errors=errors,
        )


class GlowmarktOptionsFlow(OptionsFlow):
    """Polling interval, meter timezone and first-import history."""

    def __init__(self, entry: ConfigEntry) -> None:
        self._entry = entry

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            if await dt_util.async_get_time_zone(user_input[CONF_TIMEZONE]) is None:
                errors[CONF_TIMEZONE] = "invalid_timezone"
            else:
                return self.async_create_entry(data=user_input)
        current = {**self._entry.options, **(user_input or {})}
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_SCAN_INTERVAL, default=current.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
                ): vol.All(vol.Coerce(int), vol.Range(min=10, max=1440)),
                vol.Required(
                    CONF_TIMEZONE, default=current.get(CONF_TIMEZONE, DEFAULT_TIMEZONE)
                ): str,
                vol.Required(
                    CONF_BACKFILL_DAYS, default=current.get(CONF_BACKFILL_DAYS, DEFAULT_BACKFILL_DAYS)
                ): vol.All(vol.Coerce(int), vol.Range(min=1, max=365)),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema, errors=errors)
