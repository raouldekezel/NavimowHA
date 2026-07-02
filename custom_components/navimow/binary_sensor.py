"""Binary sensor platform for Navimow integration (FEAT-03)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, VEHICLE_STATE_CHARGING
from .coordinator import NavimowCoordinator


@dataclass(frozen=True, kw_only=True)
class NavimowBinarySensorEntityDescription(BinarySensorEntityDescription):
    """Describes Navimow binary sensor entity."""

    is_on_fn: Callable[[NavimowCoordinator], bool | None]


BINARY_SENSOR_DESCRIPTIONS: tuple[NavimowBinarySensorEntityDescription, ...] = (
    NavimowBinarySensorEntityDescription(
        key="cloud_connected",
        translation_key="cloud_connected",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        entity_category=EntityCategory.DIAGNOSTIC,
        # `sdk.is_connected` returns a bool from the SDK's internal MQTT
        # client (mqtt-fra.navimow.com WSS). It reflects the state at the
        # moment the coordinator ticks (default 30 s).
        is_on_fn=lambda coordinator: bool(
            getattr(coordinator.sdk, "is_connected", False)
        ),
    ),
    NavimowBinarySensorEntityDescription(
        key="charging",
        translation_key="charging",
        device_class=BinarySensorDeviceClass.BATTERY_CHARGING,
        # FEAT-01: only `vehicleState` (from the /location channel) distinguishes
        # docked+charging (2) from docked+idle. The /state channel says
        # `isDocked` in both cases. Returns None until the coordinator has
        # seen at least one /location payload — HA renders that as "unknown".
        is_on_fn=lambda coordinator: (
            coordinator.vehicle_state == VEHICLE_STATE_CHARGING
            if getattr(coordinator, "vehicle_state", None) is not None
            else None
        ),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Navimow binary sensors from a config entry."""
    data = hass.data[DOMAIN][config_entry.entry_id]
    devices = data["devices"]
    coordinators: dict[str, NavimowCoordinator] = data["coordinators"]

    entities: list[NavimowBinarySensor] = []
    for device in devices:
        coordinator = coordinators[device.id]
        for description in BINARY_SENSOR_DESCRIPTIONS:
            entities.append(
                NavimowBinarySensor(
                    coordinator=coordinator,
                    entity_description=description,
                )
            )
    async_add_entities(entities)


class NavimowBinarySensor(CoordinatorEntity[NavimowCoordinator], BinarySensorEntity):
    """Representation of a Navimow binary sensor."""

    entity_description: NavimowBinarySensorEntityDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: NavimowCoordinator,
        entity_description: NavimowBinarySensorEntityDescription,
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
        # cloud_connected must remain available even when the coordinator
        # has no MQTT state yet — that IS the diagnostic signal.
        return True

    @property
    def is_on(self) -> bool | None:
        return self.entity_description.is_on_fn(self.coordinator)
