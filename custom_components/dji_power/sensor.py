"""Sensor platform for DJI Power Station."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    RestoreSensor,
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    PERCENTAGE,
    UnitOfEnergy,
    UnitOfPower,
    UnitOfTemperature,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MANUFACTURER, CHARGE_TYPE_MAP, CONF_SN, CONF_DEVICE_NAME
from .coordinator import DJIPowerCoordinator

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class DJIPowerSensorDescription(SensorEntityDescription):
    """Describe a DJI Power sensor."""
    state_key: str = ""


# ---------------------------------------------------------------------------
# Instantaneous sensors (measurement)
# ---------------------------------------------------------------------------

SENSOR_DESCRIPTIONS: tuple[DJIPowerSensorDescription, ...] = (
    DJIPowerSensorDescription(
        key="soc",
        state_key="soc",
        name="State of Charge",
        device_class=SensorDeviceClass.BATTERY,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=PERCENTAGE,
        # HA auto-manages the battery icon with device_class=BATTERY
    ),
    DJIPowerSensorDescription(
        key="power_in",
        state_key="power_in",
        name="Input Power",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.WATT,
        icon="mdi:transmission-tower-import",
    ),
    DJIPowerSensorDescription(
        key="power_out",
        state_key="power_out",
        name="Output Power",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.WATT,
        icon="mdi:transmission-tower-export",
    ),
    DJIPowerSensorDescription(
        key="temperature",
        state_key="temperature",
        name="Battery Temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        # suggested_unit forces HA to default to °C on first registration,
        # regardless of the HA system unit (metric vs. imperial)
        suggested_unit_of_measurement=UnitOfTemperature.CELSIUS,
        icon="mdi:thermometer",
    ),
    DJIPowerSensorDescription(
        key="remain_time",
        state_key="remain_time",
        name="Remaining Time",
        device_class=SensorDeviceClass.DURATION,
        state_class=SensorStateClass.MEASUREMENT,
        # The API returns remaining time in whole minutes
        native_unit_of_measurement=UnitOfTime.MINUTES,
        icon="mdi:timer",
    ),
    DJIPowerSensorDescription(
        key="charge_type",
        state_key="charge_type",
        name="Charge Source",
        icon="mdi:power-plug",
        device_class=None,
        state_class=None,
    ),
)

# ---------------------------------------------------------------------------
# Energy accumulator sensors (total_increasing) — used by the HA Energy
# Dashboard "Batteriesystem konfigurieren" dialog.
#
# The coordinator integrates the instantaneous Watt values over time to
# produce running kWh totals.  These sensors use RestoreSensor so the
# totals survive HA restarts without resetting to zero.
# ---------------------------------------------------------------------------

ENERGY_SENSOR_DESCRIPTIONS: tuple[DJIPowerSensorDescription, ...] = (
    DJIPowerSensorDescription(
        key="energy_in",
        state_key="energy_in",
        name="Energy In",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        icon="mdi:battery-arrow-up",
    ),
    DJIPowerSensorDescription(
        key="energy_out",
        state_key="energy_out",
        name="Energy Out",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        icon="mdi:battery-arrow-down",
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up DJI Power sensors."""
    coordinator: DJIPowerCoordinator = hass.data[DOMAIN][entry.entry_id]
    sn = entry.data[CONF_SN]
    device_name = entry.data[CONF_DEVICE_NAME]

    entities: list = [
        DJIPowerSensor(coordinator, description, sn, device_name)
        for description in SENSOR_DESCRIPTIONS
    ]
    entities.extend(
        DJIPowerEnergySensor(coordinator, description, sn, device_name)
        for description in ENERGY_SENSOR_DESCRIPTIONS
    )
    async_add_entities(entities)


def _device_info(sn: str, device_name: str) -> dict:
    return {
        "identifiers": {(DOMAIN, sn)},
        "name": device_name,
        "manufacturer": MANUFACTURER,
        "model": device_name,
        "serial_number": sn,
    }


class DJIPowerSensor(CoordinatorEntity[DJIPowerCoordinator], SensorEntity):
    """Instantaneous sensor entity for a DJI Power Station."""

    entity_description: DJIPowerSensorDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: DJIPowerCoordinator,
        description: DJIPowerSensorDescription,
        sn: str,
        device_name: str,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._sn = sn
        self._attr_unique_id = f"{sn}_{description.key}"
        self._attr_device_info = _device_info(sn, device_name)

    @property
    def native_value(self) -> Any:
        state = self.coordinator.data or {}
        val = state.get(self.entity_description.state_key)
        if val is None:
            return None
        # charge_type is an int → map to human-readable string
        if self.entity_description.key == "charge_type":
            return CHARGE_TYPE_MAP.get(int(val), str(val))
        return val


class DJIPowerEnergySensor(CoordinatorEntity[DJIPowerCoordinator], RestoreSensor):
    """Cumulative energy sensor (kWh) for the HA Energy Dashboard.

    Uses RestoreSensor so the running total survives HA restarts.
    On startup, the last recorded value is read from the HA recorder and
    written back into the coordinator so accumulation continues seamlessly.
    """

    entity_description: DJIPowerSensorDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: DJIPowerCoordinator,
        description: DJIPowerSensorDescription,
        sn: str,
        device_name: str,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._sn = sn
        self._attr_unique_id = f"{sn}_{description.key}"
        self._attr_device_info = _device_info(sn, device_name)

    async def async_added_to_hass(self) -> None:
        """Restore the last known kWh total from the HA recorder on startup."""
        await super().async_added_to_hass()
        if (last := await self.async_get_last_sensor_data()) is not None:
            if last.native_value is not None:
                try:
                    restored = float(last.native_value)
                    key = self.entity_description.state_key
                    # Seed the coordinator so new increments add on top of
                    # what was recorded before the restart.
                    self.coordinator.state.setdefault(key, restored)
                    _LOGGER.debug(
                        "Restored %s energy total: %.4f kWh", key, restored
                    )
                except (ValueError, TypeError):
                    pass

    @property
    def native_value(self) -> float:
        """Return the accumulated energy total in kWh."""
        val = (self.coordinator.data or {}).get(self.entity_description.state_key, 0.0)
        return round(float(val or 0.0), 4)
