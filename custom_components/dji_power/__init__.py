"""DJI Power Station integration."""
from __future__ import annotations

import logging

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import DJIPowerAPI, DJIAuthError, DJIAPIError
from .const import DOMAIN, CONF_MEMBER_TOKEN, CONF_SN, CONF_DEVICE_NAME
from .coordinator import DJIPowerCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS = ["sensor", "binary_sensor"]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up DJI Power Station from a config entry."""
    token = entry.data[CONF_MEMBER_TOKEN]
    sn = entry.data[CONF_SN]
    device_name = entry.data[CONF_DEVICE_NAME]

    session = async_get_clientsession(hass)
    api = DJIPowerAPI(token, session)

    # Validate token on startup
    try:
        valid = await api.validate_token()
    except Exception as exc:
        raise ConfigEntryNotReady(f"Cannot reach DJI API: {exc}") from exc

    if not valid:
        raise ConfigEntryAuthFailed("x-member-token is invalid or expired")

    coordinator = DJIPowerCoordinator(hass, api, sn, device_name)

    # Initial REST fetch
    await coordinator.async_config_entry_first_refresh()

    # Start MQTT for live updates
    try:
        await coordinator.async_start_mqtt()
    except Exception as exc:
        _LOGGER.warning("MQTT startup failed (will rely on REST polling): %s", exc)

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    coordinator: DJIPowerCoordinator = hass.data[DOMAIN][entry.entry_id]
    await coordinator.async_stop_mqtt()

    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)
    return unload_ok
