"""Sensor platform for Navimow integration."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, UnitOfLength
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, SIGNAL_POSITION_UPDATE
from .coordinator import NavimowCoordinator


@dataclass(frozen=True, kw_only=True)
class NavimowSensorEntityDescription(SensorEntityDescription):
    """Describes Navimow sensor entity."""

    value_fn: Callable[[NavimowCoordinator], Any]


SENSOR_DESCRIPTIONS: tuple[NavimowSensorEntityDescription, ...] = (
    NavimowSensorEntityDescription(
        key="battery",
        translation_key="battery",
        device_class=SensorDeviceClass.BATTERY,
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda coordinator: (
            state.battery if (state := coordinator.get_device_state()) else None
        ),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Navimow sensors from a config entry."""
    data = hass.data[DOMAIN][config_entry.entry_id]
    devices = data["devices"]
    coordinators: dict[str, NavimowCoordinator] = data["coordinators"]

    entities: list[SensorEntity] = []
    for device in devices:
        coordinator = coordinators[device.id]
        for description in SENSOR_DESCRIPTIONS:
            entities.append(
                NavimowSensor(
                    coordinator=coordinator,
                    entity_description=description,
                )
            )
        # FEAT-01 — the position sensor is dispatcher-driven (throttled)
        # rather than coordinator-driven, so it does not share the tick
        # cadence of the other sensors.
        entities.append(NavimowPositionSensor(coordinator))
    async_add_entities(entities)


class NavimowSensor(CoordinatorEntity[NavimowCoordinator], SensorEntity):
    """Representation of a Navimow sensor."""

    entity_description: NavimowSensorEntityDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: NavimowCoordinator,
        entity_description: NavimowSensorEntityDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = entity_description

        device = coordinator.device
        self._attr_unique_id = f"{DOMAIN}_{device.id}_{entity_description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, device.id)},
            name=device.name,
            manufacturer="Navimow",
            model=device.model or "Unknown",
            sw_version=device.firmware_version or None,
            serial_number=device.serial_number or device.id,
        )

    @property
    def available(self) -> bool:
        if self.coordinator.get_device_state() is not None:
            return True
        return super().available

    @property
    def native_value(self) -> Any:
        """Return sensor value from coordinator."""
        return self.entity_description.value_fn(self.coordinator)


class NavimowPositionSensor(SensorEntity):
    """Robot position on the local map (FEAT-01).

    Decoupled from the coordinator tick: /location type 1 arrives every 2 s
    and is throttled to ~5 s in the coordinator before being pushed via
    dispatcher to this entity. Excluded from the recorder in the documented
    configuration (`recorder: exclude: entities:`) — otherwise ~3600 state
    changes per mowing run.

    This is NOT a `device_tracker`: the local (station-relative) meters
    coordinate system is not lat/lon. Downstream cards should read the
    `x`/`y`/`theta` attributes.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "position"
    _attr_native_unit_of_measurement = UnitOfLength.METERS
    _attr_icon = "mdi:robot-mower"

    def __init__(self, coordinator: NavimowCoordinator) -> None:
        device = coordinator.device
        self._device_id = device.id
        self._position: dict[str, Any] | None = coordinator.position
        self._attr_unique_id = f"{DOMAIN}_{device.id}_position"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, device.id)},
            name=device.name,
            manufacturer="Navimow",
            model=device.model or "Unknown",
            sw_version=device.firmware_version or None,
            serial_number=device.serial_number or device.id,
        )

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                f"{SIGNAL_POSITION_UPDATE}_{self._device_id}",
                self._handle_position,
            )
        )

    @callback
    def _handle_position(self, position: dict[str, Any]) -> None:
        self._position = position
        self.async_write_ha_state()

    @property
    def available(self) -> bool:
        return self._position is not None

    @property
    def native_value(self) -> Any:
        return self._position.get("distance") if self._position else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if not self._position:
            return None
        return {
            "x": self._position.get("x"),
            "y": self._position.get("y"),
            "theta": self._position.get("theta"),
            "vehicle_state": self._position.get("vehicle_state"),
        }
