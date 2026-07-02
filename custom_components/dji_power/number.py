"""Number platform for DJI Power Station settings."""
from __future__ import annotations

from homeassistant.components.number import NumberEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_DEVICE_NAME, CONF_SN, DOMAIN, MANUFACTURER
from .coordinator import DJIPowerCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up DJI Power number entities."""
    coordinator: DJIPowerCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([
        DJIPowerChargeLimitNumber(
            coordinator,
            entry.data[CONF_SN],
            entry.data[CONF_DEVICE_NAME],
        )
    ])


class DJIPowerChargeLimitNumber(
    CoordinatorEntity[DJIPowerCoordinator], NumberEntity
):
    """Maximum battery recharge level."""

    _attr_has_entity_name = True
    _attr_name = "Charge Limit"
    _attr_icon = "mdi:battery-lock"
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_native_min_value = 50
    _attr_native_max_value = 100
    _attr_native_step = 5

    def __init__(
        self,
        coordinator: DJIPowerCoordinator,
        sn: str,
        device_name: str,
    ) -> None:
        super().__init__(coordinator)
        self._sn = sn
        self._attr_unique_id = f"{sn}_charge_limit"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, sn)},
            "name": device_name,
            "manufacturer": MANUFACTURER,
            "model": device_name,
            "serial_number": sn,
        }

    @property
    def native_value(self) -> float | None:
        """Return the reported maximum recharge level."""
        value = (self.coordinator.data or {}).get("charge_limit")
        return float(value) if value is not None else None

    async def async_set_native_value(self, value: float) -> None:
        """Set the maximum recharge level."""
        limit = int(value)
        try:
            await self.coordinator.api.set_charge_limit(self._sn, limit)
        except Exception:
            self.coordinator.publish_charge_limit(limit)
        else:
            self.coordinator.state["charge_limit"] = limit
            self.coordinator.async_set_updated_data(self.coordinator.state)
