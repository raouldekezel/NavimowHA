"""Sensor platform for Navimow integration."""

from __future__ import annotations

from collections.abc import Callable
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
from homeassistant.const import PERCENTAGE, UnitOfArea, UnitOfLength
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
    attrs_fn: Callable[[NavimowCoordinator], dict[str, Any] | None] | None = None
    # HARD-02: opt-in HA state persistence. When True, the sensor inherits
    # RestoreSensor behaviour — the last observed value is written to
    # `.storage/core.restore_state` and re-applied at HA startup, so a
    # cumulative counter (e.g. `area_week`) survives a restart even though
    # the cloud is silent on /location while the robot is docked. Session-
    # scoped sensors (progression, current_zone) leave this False so a
    # stale value never masks the "idle" reality.
    restore: bool = False


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
    # === /location type 2 mowing metrics (FEAT-02) ===
    NavimowSensorEntityDescription(
        key="progression",
        translation_key="progression",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:progress-helper",
        value_fn=lambda c: (c.stats or {}).get("mowing_percentage"),
        attrs_fn=lambda c: (
            {
                "current_mow_progress": c.stats.get("current_mow_progress"),
                "surface_session": c.stats.get("area_session"),
                "action": c.stats.get("action"),
            }
            if c.stats
            else None
        ),
    ),
    NavimowSensorEntityDescription(
        key="weekly_area",
        translation_key="weekly_area",
        native_unit_of_measurement=UnitOfArea.SQUARE_METERS,
        state_class=SensorStateClass.TOTAL_INCREASING,
        icon="mdi:grass",
        value_fn=lambda c: (c.stats or {}).get("area_week"),
        # HARD-02: cumulative weekly counter must survive an HA restart.
        # The cloud stops publishing /location type-2 while the robot is
        # docked (FEAT-02 diag), so without RestoreSensor the value would
        # sit at `unknown` for potentially days until the next mow.
        restore=True,
    ),
    # BUG-06: filter `boundary=0` as the session-init sentinel. The very
    # first type-2 payload of a fresh mow carries `currentMowBoundary=0`
    # with every other field also zero (`currentMowProgress=0`,
    # `mowingPercentage=0`, `action=-1`, ...); the cloud only publishes
    # the real boundary in the second packet ~60 s later. See the FEAT-02
    # diag payload at `docs/diag/2026-05-25_feat-02_multizone-run/`
    # (line 1, `time=1779694241252`). Falsy filter (`else None`) collapses
    # both `None` and `0` into HA "unknown". `attrs_fn` keeps the raw
    # numeric so `#0` remains inspectable in developer tools.
    NavimowSensorEntityDescription(
        key="current_zone",
        translation_key="current_zone",
        icon="mdi:map-marker-radius",
        value_fn=lambda c: (
            f"#{b}" if (b := (c.stats or {}).get("boundary")) else None
        ),
        attrs_fn=lambda c: (
            {"boundary_id": c.stats.get("boundary")} if c.stats else None
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


class NavimowSensor(CoordinatorEntity[NavimowCoordinator], RestoreSensor):
    """Representation of a Navimow sensor.

    Inherits `RestoreSensor` so descriptions that opt in via
    `entity_description.restore=True` (HARD-02) survive HA restarts. For
    non-restoring descriptions the behaviour is unchanged: `native_value`
    returns whatever `value_fn` computes from the coordinator, `None`
    included.
    """

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

    async def async_added_to_hass(self) -> None:
        """Seed the restore cache from the last stored value (HARD-02)."""
        await super().async_added_to_hass()
        if not self.entity_description.restore:
            return
        last = await self.async_get_last_sensor_data()
        if last is not None and last.native_value is not None:
            self._attr_native_value = last.native_value

    @property
    def available(self) -> bool:
        if self.coordinator.get_device_state() is not None:
            return True
        return super().available

    @property
    def native_value(self) -> Any:
        """Return the live coordinator value, or the restored fallback.

        Live values always win — and they refresh the internal cache so
        the next restart resumes from the freshest value we ever saw. The
        restored fallback only fires when both `value_fn` returns `None`
        *and* the description opts in to restoration; otherwise `None`
        passes through unchanged (HA renders `unknown`).
        """
        live = self.entity_description.value_fn(self.coordinator)
        if live is not None:
            self._attr_native_value = live
            return live
        if self.entity_description.restore:
            return self._attr_native_value
        return None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self.entity_description.attrs_fn is None:
            return None
        return self.entity_description.attrs_fn(self.coordinator)


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
