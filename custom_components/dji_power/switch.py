"""Switch platform for DJI Power Station — AC output control."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.switch import SwitchDeviceClass, SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_DEVICE_NAME, CONF_SN, DOMAIN, MANUFACTURER
from .coordinator import DJIPowerCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up DJI Power switches."""
    coordinator: DJIPowerCoordinator = hass.data[DOMAIN][entry.entry_id]
    sn = entry.data[CONF_SN]
    device_name = entry.data[CONF_DEVICE_NAME]
    async_add_entities([DJIPowerACSwitch(coordinator, sn, device_name)])


class DJIPowerACSwitch(CoordinatorEntity[DJIPowerCoordinator], SwitchEntity):
    """Switch to enable / disable AC output on a DJI Power Station."""

    _attr_has_entity_name = True
    _attr_name = "AC Output"
    _attr_icon = "mdi:power-socket"
    _attr_device_class = SwitchDeviceClass.OUTLET

    def __init__(
        self,
        coordinator: DJIPowerCoordinator,
        sn: str,
        device_name: str,
    ) -> None:
        super().__init__(coordinator)
        self._sn = sn
        self._attr_unique_id = f"{sn}_ac_output"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, sn)},
            "name": device_name,
            "manufacturer": MANUFACTURER,
            "model": device_name,
            "serial_number": sn,
        }

    @property
    def is_on(self) -> bool | None:
        """Return True if AC output is enabled.

        Sourced from power_info.interfaces[group_type=2].sw via MQTT:
          sw=0 → AC output ON (normal operation)
          sw=1 → AC output OFF (manually disabled)
        Falls back to power_out > 5 W if the field is not yet received.
        """
        state = self.coordinator.data or {}
        val = state.get("ac_output_enabled")
        if val is not None:
            return bool(val)
        # Fallback: infer from output power
        power_out = state.get("power_out", 0) or 0
        return power_out > 5

    async def _set_ac(self, enabled: bool) -> None:
        """Send AC output command via REST, falling back to MQTT publish.

        NOTE: Cloud-level AC output control has not yet been confirmed working
        for this firmware.  REST calls return 404 and MQTT commands published
        to the forward/ topic are silently ignored by the broker ACL.
        The command is attempted anyway; the actual state will be reflected by
        the next MQTT telemetry push (~1 s) without optimistic pre-update.
        """
        try:
            await self.coordinator.api.set_ac_output(self._sn, enabled)
            # REST succeeded — update optimistically
            self.coordinator.state["ac_output_enabled"] = enabled
            self.async_write_ha_state()
        except Exception:
            # REST not available — try MQTT publish as best-effort fallback.
            # Do NOT update state optimistically; let MQTT telemetry reflect
            # the real device state so the switch doesn't flicker.
            self.coordinator.publish_ac_output(enabled)

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Enable AC output (sw=0)."""
        await self._set_ac(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Disable AC output (sw=1)."""
        await self._set_ac(False)
