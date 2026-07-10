"""HARD-07 — consolidate the triplicated ``DeviceInfo`` in
``sensor.py`` into one shared helper ``_device_info``. Text and
structure only: `identifiers` is unchanged, so the device registry
sees the exact same device.

Every test exercises one seam:

- Single-source-of-truth: `_device_info` exists at module level and
  ships the full 6-field ``DeviceInfo`` shape.
- Rename regression: the FEAT-04 name ``_zone_device_info`` no longer
  exists (locks the "one helper" contract — a re-introduced duplicate
  under the old name would slip past a lazy grep).
- Byte-identical output: base sensor, position sensor, and a zone
  entity all resolve to the same ``DeviceInfo`` dict (same
  ``identifiers``, same fields, same values). This is the essential
  "no second device" guard.
- Fallbacks preserved: ``model="Unknown"`` when missing,
  ``sw_version=None`` when missing, ``serial_number`` falls back to
  ``device.id`` when unset. The three inline copies handled each
  field the same way; the helper must too.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from custom_components.navimow import sensor as sensor_module
from custom_components.navimow.const import DOMAIN
from custom_components.navimow.sensor import (
    SENSOR_DESCRIPTIONS,
    NavimowPositionSensor,
    NavimowSensor,
    NavimowZonesAggregateSensor,
    NavimowZoneSurfaceSensor,
    _device_info,
)
from custom_components.navimow.zone_registry import ZoneRecord, ZoneRegistry


def _make_coordinator(
    *,
    device_id: str = "REDACTED-ROBOT-SERIAL",
    device_name: str = "Razibus",
    model: str | None = "i210 LiDAR Pro",
    firmware_version: str | None = "1.0.0",
    serial_number: str | None = "REDACTED-ROBOT-SERIAL",
    zone_records: dict[int, ZoneRecord] | None = None,
):
    coord = MagicMock()
    coord.device.id = device_id
    coord.device.name = device_name
    coord.device.model = model
    coord.device.firmware_version = firmware_version
    coord.device.serial_number = serial_number
    coord.position = None
    coord.zone_registry = ZoneRegistry()
    if zone_records:
        coord.zone_registry.zones.update(zone_records)
    return coord


def _first_description(key: str):
    for d in SENSOR_DESCRIPTIONS:
        if d.key == key:
            return d
    raise AssertionError(f"description {key!r} not registered")


# --------------------------------------------------------------------- #
# 1. Single-source-of-truth helper                                      #
# --------------------------------------------------------------------- #


def test_device_info_helper_exists() -> None:
    assert callable(_device_info)


def test_device_info_returns_devicinfo_with_all_fields() -> None:
    """Full shape: 6 fields, values sourced from the coordinator."""
    coord = _make_coordinator()
    info = _device_info(coord)
    assert isinstance(info, dict)  # DeviceInfo is a TypedDict-ish
    # `DeviceInfo` is HA's `TypedDict`; the runtime type is `dict`.
    assert info["identifiers"] == {(DOMAIN, "REDACTED-ROBOT-SERIAL")}
    assert info["name"] == "Razibus"
    assert info["manufacturer"] == "Navimow"
    assert info["model"] == "i210 LiDAR Pro"
    assert info["sw_version"] == "1.0.0"
    assert info["serial_number"] == "REDACTED-ROBOT-SERIAL"


def test_zone_device_info_symbol_removed() -> None:
    """The FEAT-04 name ``_zone_device_info`` is folded into
    ``_device_info``. A re-introduced duplicate under the old name
    would silently split the source of truth back into two — this
    test locks the rename."""
    assert not hasattr(sensor_module, "_zone_device_info")


# --------------------------------------------------------------------- #
# 2. Fallback branches — same behaviour as the pre-HARD-07 inline copies #
# --------------------------------------------------------------------- #


def test_model_falls_back_to_unknown_when_missing() -> None:
    """Regression: the three inline copies all did
    ``device.model or "Unknown"``; the helper must too."""
    coord = _make_coordinator(model=None)
    info = _device_info(coord)
    assert info["model"] == "Unknown"

    coord2 = _make_coordinator(model="")
    info2 = _device_info(coord2)
    assert info2["model"] == "Unknown"


def test_sw_version_falls_back_to_none_when_missing() -> None:
    coord = _make_coordinator(firmware_version=None)
    info = _device_info(coord)
    assert info["sw_version"] is None


def test_serial_number_falls_back_to_device_id_when_missing() -> None:
    """Regression: the pre-HARD-07 copies all did
    ``device.serial_number or device.id`` — reversible to
    ``device.id`` when the serial slot is unset."""
    coord = _make_coordinator(device_id="fallback-id", serial_number=None)
    info = _device_info(coord)
    assert info["serial_number"] == "fallback-id"

    coord2 = _make_coordinator(device_id="fallback-id", serial_number="")
    info2 = _device_info(coord2)
    assert info2["serial_number"] == "fallback-id"


# --------------------------------------------------------------------- #
# 3. Byte-identical output across the three entity families             #
# --------------------------------------------------------------------- #


def test_three_families_produce_identical_device_info() -> None:
    """Essential HARD-07 guard: base sensor, position sensor, and a
    zone entity all resolve to the same ``DeviceInfo`` dict.

    `identifiers` matching is what guarantees "one mower device"; if
    any family drifted, HA would silently create a second device."""
    coord = _make_coordinator(zone_records={1: _make_zone_record(1)})
    entry = _make_entry()

    base = NavimowSensor(
        coordinator=coord,
        entity_description=_first_description("battery"),
    )
    position = NavimowPositionSensor(coord)
    zone_surface = NavimowZoneSurfaceSensor(coord, entry, 1)
    aggregate = NavimowZonesAggregateSensor(coord)

    infos = [
        base._attr_device_info,
        position._attr_device_info,
        zone_surface._attr_device_info,
        aggregate._attr_device_info,
    ]
    # All four dicts are the same shape and same values — they came
    # from the same helper.
    reference = infos[0]
    for i, info in enumerate(infos[1:], start=1):
        assert info == reference, f"family {i} drifted: {info!r} vs {reference!r}"


def test_all_families_share_identifiers() -> None:
    """Minimal cross-check: `identifiers` alone is what the device
    registry keys on. Even if some cosmetic field drifted, matching
    identifiers keeps everything on one device — but our helper
    guarantees more than that; this test states the essential half."""
    coord = _make_coordinator(zone_records={1: _make_zone_record(1)})
    entry = _make_entry()
    families = (
        NavimowSensor(
            coordinator=coord,
            entity_description=_first_description("battery"),
        )._attr_device_info["identifiers"],
        NavimowPositionSensor(coord)._attr_device_info["identifiers"],
        NavimowZoneSurfaceSensor(coord, entry, 1)._attr_device_info["identifiers"],
        NavimowZonesAggregateSensor(coord)._attr_device_info["identifiers"],
    )
    unique = {frozenset(ids) for ids in families}
    assert len(unique) == 1, families


# --------------------------------------------------------------------- #
# 4. No inline ``DeviceInfo(...)`` construction left in sensor.py       #
# --------------------------------------------------------------------- #


def test_devinfo_helper_is_the_only_constructor_site() -> None:
    """Structural: only one ``DeviceInfo(...)`` call site in
    sensor.py — inside `_device_info`. Adding a fresh inline copy in
    a later PR would trip this test.

    We assert on the module AST rather than the raw source text
    (per CONTRIBUTING: no source-level greps). Counts every
    ``ast.Call`` whose func resolves to a name / attribute ending in
    ``DeviceInfo``.
    """
    import ast
    import inspect

    src = inspect.getsource(sensor_module)
    tree = ast.parse(src)
    count = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id == "DeviceInfo":
                count += 1
            elif isinstance(func, ast.Attribute) and func.attr == "DeviceInfo":
                count += 1
    assert count == 1, (
        f"expected exactly one DeviceInfo(...) call in sensor.py "
        f"(inside `_device_info`); found {count}"
    )


# --------------------------------------------------------------------- #
# Local helpers                                                         #
# --------------------------------------------------------------------- #


def _make_entry(options: dict | None = None):
    entry = MagicMock()
    entry.entry_id = "test-entry"
    entry.options = options or {}
    return entry


def _make_zone_record(boundary_id: int) -> ZoneRecord:
    return ZoneRecord(
        boundary_id=boundary_id,
        last_surface_m2=227.82,
        last_cmp_max=10_000,
        last_result="completed",
    )
