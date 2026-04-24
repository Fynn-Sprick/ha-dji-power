"""Config flow for DJI Power Station."""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import DJIPowerAPI, DJIAuthError, DJIAPIError
from .const import DOMAIN, CONF_MEMBER_TOKEN, CONF_SN, CONF_DEVICE_NAME

_LOGGER = logging.getLogger(__name__)

STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_MEMBER_TOKEN): vol.All(str, vol.Length(min=20)),
    }
)


class DJIPowerConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for DJI Power Station."""

    VERSION = 1

    def __init__(self) -> None:
        super().__init__()
        self._member_token: str = ""
        self._devices: list[dict] = []

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Step 1 — enter x-member-token."""
        errors: dict[str, str] = {}

        if user_input is not None:
            token = user_input[CONF_MEMBER_TOKEN].strip()
            session = async_get_clientsession(self.hass)
            api = DJIPowerAPI(token, session)

            try:
                devices = await api.get_devices()
            except DJIAuthError:
                errors["base"] = "invalid_auth"
            except DJIAPIError as exc:
                _LOGGER.exception("API error during config: %s", exc)
                errors["base"] = "cannot_connect"
            except Exception as exc:
                _LOGGER.exception("Unexpected error during config: %s", exc)
                errors["base"] = "unknown"
            else:
                if not devices:
                    errors["base"] = "no_devices"
                else:
                    self._member_token = token
                    self._devices = devices
                    if len(devices) == 1:
                        # Only one device — skip selection step
                        return await self._create_entry(devices[0])
                    return await self.async_step_device()

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_SCHEMA,
            errors=errors,
            description_placeholders={
                "help_url": "https://github.com/fynnsprick/ha-dji-power#getting-your-token"
            },
        )

    async def async_step_device(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Step 2 — select device (when user has multiple devices)."""
        choices = {
            dev["base_info"]["sn"]: f"{dev['base_info']['name']} ({dev['base_info']['sn']})"
            for dev in self._devices
        }

        if user_input is not None:
            sn = user_input[CONF_SN]
            device = next(d for d in self._devices if d["base_info"]["sn"] == sn)
            return await self._create_entry(device)

        return self.async_show_form(
            step_id="device",
            data_schema=vol.Schema({vol.Required(CONF_SN): vol.In(choices)}),
        )

    async def _create_entry(self, device: dict) -> FlowResult:
        """Create config entry for selected device."""
        base = device["base_info"]
        sn = base["sn"]
        name = base["name"]

        await self.async_set_unique_id(sn)
        self._abort_if_unique_id_configured()

        return self.async_create_entry(
            title=name,
            data={
                CONF_MEMBER_TOKEN: self._member_token,
                CONF_SN: sn,
                CONF_DEVICE_NAME: name,
            },
        )

    # ------------------------------------------------------------------
    # Re-authentication flow
    # Triggered automatically by HA when ConfigEntryAuthFailed is raised.
    # ------------------------------------------------------------------

    async def async_step_reauth(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Start re-auth — immediately go to the confirm step."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Re-auth step: ask the user for a fresh x-member-token."""
        errors: dict[str, str] = {}

        if user_input is not None:
            token = user_input[CONF_MEMBER_TOKEN].strip()
            session = async_get_clientsession(self.hass)
            api = DJIPowerAPI(token, session)

            try:
                devices = await api.get_devices()
            except DJIAuthError:
                errors["base"] = "invalid_auth"
            except DJIAPIError as exc:
                _LOGGER.exception("API error during re-auth: %s", exc)
                errors["base"] = "cannot_connect"
            except Exception as exc:
                _LOGGER.exception("Unexpected error during re-auth: %s", exc)
                errors["base"] = "unknown"
            else:
                if not devices:
                    errors["base"] = "no_devices"
                else:
                    # Update the existing entry's token and reload
                    return self.async_update_reload_and_abort(
                        self._get_reauth_entry(),
                        data_updates={CONF_MEMBER_TOKEN: token},
                    )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=STEP_USER_SCHEMA,
            errors=errors,
            description_placeholders={
                "help_url": "https://github.com/fynnsprick/ha-dji-power#getting-your-token"
            },
        )
