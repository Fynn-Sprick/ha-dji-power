"""Binary sensor platform for DJI Power Station."""
from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MANUFACTURER, CONF_SN, CONF_DEVICE_NAME
from .coordinator import DJIPowerCoordinator


@dataclass(frozen=True)
class DJIPowerBinarySensorDescription(BinarySensorEntityDescription):
    """Describe a DJI Power binary sensor."""
    state_key: str = ""


BINARY_SENSOR_DESCRIPTIONS: tuple[DJIPowerBinarySensorDescription, ...] = (
    DJIPowerBinarySensorDescription(
        key="online",
        state_key="online",
        name="Online",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
    ),
    DJIPowerBinarySensorDescription(
        key="is_charging",
        state_key="is_charging",
        name="Charging",
        device_class=BinarySensorDeviceClass.BATTERY_CHARGING,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up DJI Power binary sensors."""
    coordinator: DJIPowerCoordinator = hass.data[DOMAIN][entry.entry_id]
    sn = entry.data[CONF_SN]
    device_name = entry.data[CONF_DEVICE_NAME]

    async_add_entities(
        DJIPowerBinarySensor(coordinator, description, sn, device_name)
        for description in BINARY_SENSOR_DESCRIPTIONS
    )


class DJIPowerBinarySensor(CoordinatorEntity[DJIPowerCoordinator], BinarySensorEntity):
    """A binary sensor for a DJI Power Station."""

    entity_description: DJIPowerBinarySensorDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: DJIPowerCoordinator,
        description: DJIPowerBinarySensorDescription,
        sn: str,
        device_name: str,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._sn = sn
        self._device_name = device_name
        self._attr_unique_id = f"{sn}_{description.key}"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, sn)},
            "name": device_name,
            "manufacturer": MANUFACTURER,
            "model": device_name,
            "serial_number": sn,
        }

    @property
    def is_on(self) -> bool | None:
        state = self.coordinator.data or {}
        val = state.get(self.entity_description.state_key)
        if val is None:
            return None
        return bool(val)
