"""HARD-08 — declare ``SensorDeviceClass.AREA`` on the two m² surface
sensors so HA can drive per-user unit conversion (ft², etc.) and
formatting parity across area sensors.

Two entities are touched:

- ``sensor.<slug>_weekly_area`` (FEAT-02).
- ``NavimowZoneLastAreaSensor`` per-boundary surface (FEAT-04 PR 3).

Kept out (as per the ticket):

- ``sensor.<slug>_zones.total_area`` — that's an *attribute value* on
  an integer-count sensor. Device classes belong to entities.
- ``NavimowZoneLastDurationSensor`` / ``NavimowZoneLastMowedSensor`` —
  duration / timestamp, not area.

Every test exercises one seam:

- Descriptor / class attribute contract on both sensors.
- Regression: unit stays m² and ``state_class`` unchanged (the pair
  ``(device_class, unit, state_class)`` is what HA validates —
  breaking any one of them silences the whole graph).
- Regression: neither the aggregate nor the non-area zone sub-classes
  gained ``AREA``. Locks the scope discipline of this ticket.
- Value regression: ``native_value`` still m², ``ceil``'d as before.
"""

from __future__ import annotations

import math
from unittest.mock import MagicMock

from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.const import UnitOfArea

from custom_components.navimow.sensor import (
    SENSOR_DESCRIPTIONS,
    NavimowZoneLastAreaSensor,
    NavimowZoneLastDurationSensor,
    NavimowZoneLastMowedSensor,
    NavimowZonesAggregateSensor,
)
from custom_components.navimow.zone_registry import ZoneRecord, ZoneRegistry


def _get_description(key: str):
    for d in SENSOR_DESCRIPTIONS:
        if d.key == key:
            return d
    return None


def _make_coordinator(zone_records: dict[int, ZoneRecord] | None = None):
    coord = MagicMock()
    coord.device.id = "REDACTED-ROBOT-SERIAL"
    coord.device.name = "Razibus"
    coord.device.model = "i210 LiDAR Pro"
    coord.device.firmware_version = "1.0.0"
    coord.device.serial_number = "REDACTED-ROBOT-SERIAL"
    coord.zone_registry = ZoneRegistry()
    if zone_records:
        coord.zone_registry.zones.update(zone_records)
    return coord


def _make_entry(options: dict | None = None):
    entry = MagicMock()
    entry.entry_id = "test-entry"
    entry.options = options or {}
    return entry


def _rec(boundary_id: int, *, surface: float | None = 227.82) -> ZoneRecord:
    return ZoneRecord(
        boundary_id=boundary_id,
        last_surface_m2=surface,
        last_cmp_max=10_000,
        size_estimate_m2=surface,
        last_result="completed",
    )


# --------------------------------------------------------------------- #
# 1. weekly_area descriptor                                             #
# --------------------------------------------------------------------- #


def test_weekly_area_device_class_is_area() -> None:
    d = _get_description("weekly_area")
    assert d is not None
    assert d.device_class is SensorDeviceClass.AREA


def test_weekly_area_unit_and_state_class_unchanged() -> None:
    """HA validates the trio ``(device_class, native_unit, state_class)``.
    Breaking any one silences the whole history graph — pin all three."""
    d = _get_description("weekly_area")
    assert d is not None
    assert d.native_unit_of_measurement == UnitOfArea.SQUARE_METERS
    assert d.state_class is SensorStateClass.TOTAL_INCREASING


# --------------------------------------------------------------------- #
# 2. NavimowZoneLastAreaSensor class attribute                           #
# --------------------------------------------------------------------- #


def test_zone_surface_instance_carries_area_device_class() -> None:
    """The ``SensorEntity`` base class exposes ``_attr_device_class``
    via a descriptor, so reading it off the class returns the property
    object — the real value lives on the instance. HA reads this way
    too, so this is the correct assertion shape."""
    coord = _make_coordinator({1: _rec(1)})
    entry = _make_entry()
    surf = NavimowZoneLastAreaSensor(coord, entry, 1)
    assert surf.device_class is SensorDeviceClass.AREA


def test_zone_surface_unit_and_state_class_unchanged() -> None:
    """Same trio pinning as ``weekly_area``: the whole triple has to
    stay coherent for HA to accept it."""
    coord = _make_coordinator({1: _rec(1)})
    surf = NavimowZoneLastAreaSensor(coord, _make_entry(), 1)
    assert surf.native_unit_of_measurement == UnitOfArea.SQUARE_METERS
    assert surf.state_class is SensorStateClass.MEASUREMENT


def test_zone_surface_native_value_still_m2_ceiled() -> None:
    """Value-side regression: ``ceil`` on ``last_surface_m2`` still
    lands m² (the HA unit conversion is driven by the *device class*
    metadata, not by our value_fn — so we must NOT ourselves start
    converting)."""
    coord = _make_coordinator({1: _rec(1, surface=227.82)})
    surf = NavimowZoneLastAreaSensor(coord, _make_entry(), 1)
    assert surf.native_value == math.ceil(227.82) == 228


# --------------------------------------------------------------------- #
# 3. Scope discipline — nothing else silently gained AREA              #
# --------------------------------------------------------------------- #


def test_aggregate_zone_count_not_an_area() -> None:
    """The aggregate's *state* is a zone COUNT (int, no unit). Only the
    ``total_area`` attribute is m² — and per the ticket, device classes
    belong to entities, not attributes. Assert on the instance's
    resolved ``device_class`` (the base class exposes it via a
    descriptor, so class-level ``getattr`` returns the property object
    and would trivially never equal ``AREA``)."""
    coord = _make_coordinator()
    agg = NavimowZonesAggregateSensor(coord)
    assert agg.device_class is not SensorDeviceClass.AREA


def test_duration_and_last_mowed_are_not_area() -> None:
    """The two other per-zone sub-classes carry duration / timestamp
    device classes — not area. Same instance-level assertion shape as
    the aggregate check above."""
    coord = _make_coordinator({1: _rec(1)})
    entry = _make_entry()
    dur = NavimowZoneLastDurationSensor(coord, entry, 1)
    lm = NavimowZoneLastMowedSensor(coord, entry, 1)
    assert dur.device_class is not SensorDeviceClass.AREA
    assert lm.device_class is not SensorDeviceClass.AREA


def test_no_other_descriptor_silently_became_area() -> None:
    """The AREA-eligible descriptors: ``weekly_area`` (cumulative) and
    ``last_run_area`` (promoted from `last_run_result.session_area` in
    FEAT-08 comment). Per-boundary and per-aggregate area sensors
    (``NavimowZoneLastAreaSensor`` / ``NavimowZoneTotalAreaSensor`` /
    ``NavimowZonesTotalAreaSensor``) sit outside
    ``SENSOR_DESCRIPTIONS`` (dedicated classes, not table entries)."""
    area_keys = {
        d.key for d in SENSOR_DESCRIPTIONS if d.device_class is SensorDeviceClass.AREA
    }
    assert area_keys == {"weekly_area", "last_run_area"}, area_keys
