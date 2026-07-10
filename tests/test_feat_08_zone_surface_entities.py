"""FEAT-08 (#88) — first-class zone surface entities.

Exercises the two additive entities:

- ``NavimowZoneTotalAreaSensor`` — per-zone ``<slug>_zone_<id>_surface``,
  ``ceil(size_estimate_m2)`` from the last complete pass, ``None`` until
  the first complete pass lands, precise float + timestamp in attrs.
- ``NavimowZonesTotalAreaSensor`` — aggregate ``<slug>_zones_surface_totale``,
  ``ceil(Σ size_estimate_m2)``, ``translation_key`` set, ``per_zone`` map
  + ``zone_ids`` list in attrs.

Plus the two wire seams:

- Setup adds one ``NavimowZoneTotalAreaSensor`` per restored boundary and one
  static ``NavimowZonesTotalAreaSensor``.
- Runtime discovery adds the new per-zone entity alongside the trio.

Registry-level: ``ZoneRecord.size_estimate_updated_ms`` is stamped at the
start of the visit that most recently reached
``cmp_max >= COMPLETE_PASS_CMP``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.const import UnitOfArea

from custom_components.navimow.const import DOMAIN
from custom_components.navimow.sensor import (
    NavimowZonesAggregateSensor,
    NavimowZonesTotalAreaSensor,
    NavimowZoneTotalAreaSensor,
    async_setup_entry,
)
from custom_components.navimow.zone_registry import (
    COMPLETE_PASS_CMP,
    ZoneRecord,
    ZoneRegistry,
)

# --------------------------------------------------------------------- #
# Fixtures                                                              #
# --------------------------------------------------------------------- #


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


def _seg(
    boundary_id: int,
    first_time: int,
    last_time: int,
    cmp_max: int,
    sub_entry: float,
    sub_exit: float,
) -> dict:
    return {
        "boundary_id": boundary_id,
        "first_time": first_time,
        "last_time": last_time,
        "cmp_max": cmp_max,
        "sub_entry": sub_entry,
        "sub_exit": sub_exit,
    }


def _run(segments: list[dict], *, result: str = "completed") -> dict:
    return {"result": result, "zones": segments}


# --------------------------------------------------------------------- #
# 1. Registry: size_estimate_updated_ms stamping                        #
# --------------------------------------------------------------------- #


def test_registry_stamps_size_estimate_updated_ms_on_complete_pass() -> None:
    reg = ZoneRegistry()
    reg.ingest_run(_run([_seg(1, 1_000, 10_000, COMPLETE_PASS_CMP, 0.0, 228.0)]))
    z1 = reg.zones[1]
    assert z1.size_estimate_m2 == 228.0
    # min(first_time) of the segments that reached the complete-pass
    # threshold — aligned with last_mowed_ms semantics (start of the
    # visit, not exit; HARD-12).
    assert z1.size_estimate_updated_ms == 1_000


def test_registry_leaves_stamp_none_when_no_complete_pass_yet() -> None:
    reg = ZoneRegistry()
    reg.ingest_run(
        _run(
            [_seg(1, 1_000, 6_000, 7_000, 0.0, 140.0)],
            result="interrupted",
        )
    )
    z1 = reg.zones[1]
    assert z1.size_estimate_m2 is None
    assert z1.size_estimate_updated_ms is None


def test_registry_preserves_stamp_across_interrupted_next_pass() -> None:
    """An interrupted pass must NOT clobber a prior complete-pass stamp
    (mirrors `size_estimate_m2` last-wins semantics — see
    `test_interrupted_pass_keeps_prior_size_estimate` in
    `test_zone_registry.py`).
    """
    reg = ZoneRegistry()
    reg.ingest_run(_run([_seg(1, 1_000, 10_000, 10_000, 0.0, 228.0)]))
    reg.ingest_run(
        _run(
            [_seg(1, 20_000, 26_000, 6_000, 0.0, 140.0)],
            result="interrupted",
        )
    )
    z1 = reg.zones[1]
    assert z1.size_estimate_m2 == 228.0  # unchanged
    assert z1.size_estimate_updated_ms == 1_000  # from the first pass


def test_registry_advances_stamp_on_next_complete_pass() -> None:
    """A fresh complete pass wins — both the estimate and the stamp
    advance to the newer visit's start time. This is what makes the
    entity auto-correct after an app-side zone reshape."""
    reg = ZoneRegistry()
    reg.ingest_run(_run([_seg(1, 1_000, 10_000, 10_000, 0.0, 200.0)]))
    reg.ingest_run(_run([_seg(1, 50_000, 60_000, 10_000, 0.0, 250.0)]))
    z1 = reg.zones[1]
    assert z1.size_estimate_m2 == 250.0
    assert z1.size_estimate_updated_ms == 50_000


# --------------------------------------------------------------------- #
# 2. NavimowZoneTotalAreaSensor contracts                                    #
# --------------------------------------------------------------------- #


def _rec_with_estimate(
    boundary_id: int,
    *,
    size_estimate: float | None,
    updated_ms: int | None = None,
    surface: float | None = None,
) -> ZoneRecord:
    return ZoneRecord(
        boundary_id=boundary_id,
        last_mowed_ms=updated_ms,
        last_surface_m2=surface,
        last_duration_s=None,
        last_cmp_max=COMPLETE_PASS_CMP if size_estimate is not None else 0,
        size_estimate_m2=size_estimate,
        size_estimate_updated_ms=updated_ms if size_estimate is not None else None,
        last_result="completed",
    )


def test_area_sensor_state_ceils_size_estimate() -> None:
    coord = _make_coordinator(
        {1: _rec_with_estimate(1, size_estimate=227.82, updated_ms=1_779_694_000_000)}
    )
    ent = NavimowZoneTotalAreaSensor(coord, _make_entry(), 1)
    assert ent.native_value == 228
    assert ent.native_unit_of_measurement == UnitOfArea.SQUARE_METERS
    assert ent.device_class == SensorDeviceClass.AREA


def test_area_sensor_state_none_before_first_complete_pass() -> None:
    """No fake ``0`` fallback — the state stays honest at
    ``unknown`` until a complete pass lands."""
    coord = _make_coordinator({3: ZoneRecord(boundary_id=3)})
    ent = NavimowZoneTotalAreaSensor(coord, _make_entry(), 3)
    assert ent.native_value is None
    # The record IS present, so the entity is `available`.
    assert ent.available is True


def test_area_sensor_state_none_when_boundary_missing() -> None:
    coord = _make_coordinator({})
    ent = NavimowZoneTotalAreaSensor(coord, _make_entry(), 1)
    assert ent.native_value is None
    assert ent.extra_state_attributes is None
    assert ent.available is False


def test_area_sensor_attributes_carry_precise_float_and_timestamp() -> None:
    # 1_779_694_000_000 ms = 2026-05-25 07:26:40 UTC
    coord = _make_coordinator(
        {1: _rec_with_estimate(1, size_estimate=227.82, updated_ms=1_779_694_000_000)}
    )
    ent = NavimowZoneTotalAreaSensor(coord, _make_entry(), 1)
    attrs = ent.extra_state_attributes
    assert attrs["boundary_id"] == 1
    # FEAT-08 uniform naming: precise float uses `area_precise` on
    # every area sensor.
    assert attrs["area_precise"] == 227.82
    got = attrs["last_complete_pass_at"]
    assert isinstance(got, datetime)
    assert got.tzinfo == UTC
    assert got == datetime(2026, 5, 25, 7, 26, 40, tzinfo=UTC)


def test_area_sensor_attributes_timestamp_none_before_first_complete_pass() -> None:
    """When the record exists but no complete pass has been seen yet,
    ``last_complete_pass_at`` must be ``None`` — never fabricated from
    ``last_mowed_ms`` (an interrupted last-mow is not a size calibration
    moment)."""
    rec = ZoneRecord(
        boundary_id=3,
        last_mowed_ms=1_779_700_000_000,  # partial visit stamp
        last_surface_m2=96.0,
        last_cmp_max=7_000,
        # size_estimate_m2 is None → size_estimate_updated_ms stays None too.
    )
    coord = _make_coordinator({3: rec})
    ent = NavimowZoneTotalAreaSensor(coord, _make_entry(), 3)
    attrs = ent.extra_state_attributes
    assert attrs["area_precise"] is None
    assert attrs["last_complete_pass_at"] is None


def test_area_sensor_unique_id_uses_total_area_suffix() -> None:
    coord = _make_coordinator(
        {7: _rec_with_estimate(7, size_estimate=100.0, updated_ms=1_000)}
    )
    ent = NavimowZoneTotalAreaSensor(coord, _make_entry(), 7)
    # FEAT-08 naming: `_total_area`, migrated from the pre-comment
    # placeholder `_surface`. Migration handled in `__init__.py`.
    assert ent.unique_id == f"{DOMAIN}_REDACTED-ROBOT-SERIAL_zone_7_total_area"


def test_area_sensor_fallback_name_carries_surface_suffix() -> None:
    coord = _make_coordinator(
        {3: _rec_with_estimate(3, size_estimate=123.5, updated_ms=1_000)}
    )
    ent = NavimowZoneTotalAreaSensor(coord, _make_entry(), 3)
    # `Zone #<id>` fallback + ` surface` HARD-11 suffix.
    assert ent.name == "Zone #3 surface"


def test_area_sensor_operator_rename_flows_to_display_name() -> None:
    """PR 4 options-flow rename must propagate to the entity title —
    the entity inherits from ``_NavimowZoneEntity`` so this happens for
    free, but the FEAT-08 regression test pins the wiring."""
    coord = _make_coordinator(
        {1: _rec_with_estimate(1, size_estimate=228.0, updated_ms=1_000)}
    )
    entry = _make_entry(options={"zones": {"1": {"name": "Prunier"}}})
    ent = NavimowZoneTotalAreaSensor(coord, entry, 1)
    assert ent.name == "Prunier surface"


# --------------------------------------------------------------------- #
# 3. NavimowZonesTotalAreaSensor contracts                              #
# --------------------------------------------------------------------- #


def test_total_area_sensor_sums_ceiled_state() -> None:
    coord = _make_coordinator(
        {
            1: _rec_with_estimate(1, size_estimate=227.82, updated_ms=1_000),
            3: _rec_with_estimate(3, size_estimate=123.54, updated_ms=2_000),
        }
    )
    ent = NavimowZonesTotalAreaSensor(coord)
    # ceil(227.82 + 123.54) = ceil(351.36) = 352
    assert ent.native_value == 352
    assert ent.native_unit_of_measurement == UnitOfArea.SQUARE_METERS
    assert ent.device_class == SensorDeviceClass.AREA


def test_total_area_sensor_zero_when_no_zones_yet() -> None:
    coord = _make_coordinator({})
    coord.config_entry = _make_entry()
    ent = NavimowZonesTotalAreaSensor(coord)
    assert ent.native_value == 0
    # FEAT-08 comment (#88, 2026-07-10): attrs are `zone_ids`,
    # `zone_names` (parallel list), and `area_precise` (float sum,
    # 0.0 when the registry is empty).
    assert ent.extra_state_attributes == {
        "zone_ids": [],
        "zone_names": [],
        "area_precise": 0,
    }


def test_total_area_sensor_zones_without_estimate_contribute_zero() -> None:
    coord = _make_coordinator(
        {
            1: _rec_with_estimate(1, size_estimate=228.0, updated_ms=1_000),
            3: ZoneRecord(boundary_id=3),  # no estimate yet
        }
    )
    coord.config_entry = _make_entry()
    ent = NavimowZonesTotalAreaSensor(coord)
    assert ent.native_value == 228
    attrs = ent.extra_state_attributes
    assert attrs["zone_ids"] == [1, 3]
    # `zone_names` follows the same order as `zone_ids`. Fallback
    # to `Zone #<id>` when no operator name is set.
    assert attrs["zone_names"] == ["Zone #1", "Zone #3"]
    # `area_precise` == precise sum before ceil.
    assert attrs["area_precise"] == 228.0


def test_total_area_sensor_zone_names_reflect_operator_renames() -> None:
    """The `zone_names` attr must show the renamed zone as soon as the
    options flow updates. The refresh dispatcher wiring is verified
    separately; this test just pins the read path."""
    coord = _make_coordinator(
        {
            1: _rec_with_estimate(1, size_estimate=228.0, updated_ms=1_000),
            3: _rec_with_estimate(3, size_estimate=124.0, updated_ms=2_000),
        }
    )
    coord.config_entry = _make_entry(
        options={"zones": {"1": {"name": "Prunier"}, "3": {"name": "Figuier"}}}
    )
    ent = NavimowZonesTotalAreaSensor(coord)
    attrs = ent.extra_state_attributes
    assert attrs["zone_ids"] == [1, 3]
    assert attrs["zone_names"] == ["Prunier", "Figuier"]


def test_total_area_sensor_translation_key_set() -> None:
    """Static aggregate must ship named — PR #50 lesson."""
    coord = _make_coordinator({})
    ent = NavimowZonesTotalAreaSensor(coord)
    assert ent.translation_key == "zones_total_area"


def test_total_area_sensor_unique_id() -> None:
    coord = _make_coordinator({})
    ent = NavimowZonesTotalAreaSensor(coord)
    assert ent.unique_id == f"{DOMAIN}_REDACTED-ROBOT-SERIAL_zones_total_area"


# --------------------------------------------------------------------- #
# 4. Setup wiring                                                       #
# --------------------------------------------------------------------- #


async def test_async_setup_entry_adds_area_sensor_per_zone_plus_total_area() -> None:
    coord = _make_coordinator(
        {
            1: _rec_with_estimate(1, size_estimate=227.82, updated_ms=1_000),
            3: _rec_with_estimate(3, size_estimate=123.54, updated_ms=2_000),
        }
    )
    device = coord.device

    hass = MagicMock()
    hass.data = {
        DOMAIN: {
            "test-entry": {"devices": [device], "coordinators": {device.id: coord}}
        }
    }
    config_entry = MagicMock()
    config_entry.entry_id = "test-entry"
    async_add_entities = MagicMock()

    with patch(
        "custom_components.navimow.sensor.async_dispatcher_connect",
        return_value=lambda: None,
    ):
        await async_setup_entry(hass, config_entry, async_add_entities)

    added = list(async_add_entities.call_args.args[0])
    area_entities = [e for e in added if isinstance(e, NavimowZoneTotalAreaSensor)]
    total_area = [e for e in added if isinstance(e, NavimowZonesTotalAreaSensor)]
    count_agg = [e for e in added if isinstance(e, NavimowZonesAggregateSensor)]
    # One area sensor per pre-restored boundary.
    assert {e._boundary_id for e in area_entities} == {1, 3}
    # Both aggregates ship — the count sensor (existing) and the new
    # total-area sensor (FEAT-08). Distinct entities on purpose.
    assert len(total_area) == 1
    assert len(count_agg) == 1


async def test_async_setup_entry_no_zones_still_adds_total_area_aggregate() -> None:
    coord = _make_coordinator({})
    device = coord.device

    hass = MagicMock()
    hass.data = {
        DOMAIN: {
            "test-entry": {"devices": [device], "coordinators": {device.id: coord}}
        }
    }
    config_entry = MagicMock()
    config_entry.entry_id = "test-entry"
    async_add_entities = MagicMock()

    with patch(
        "custom_components.navimow.sensor.async_dispatcher_connect",
        return_value=lambda: None,
    ):
        await async_setup_entry(hass, config_entry, async_add_entities)

    added = list(async_add_entities.call_args.args[0])
    area_entities = [e for e in added if isinstance(e, NavimowZoneTotalAreaSensor)]
    total_area = [e for e in added if isinstance(e, NavimowZonesTotalAreaSensor)]
    assert area_entities == []
    assert len(total_area) == 1


# --------------------------------------------------------------------- #
# 5. Discovery: the quartet fires end-to-end                            #
# --------------------------------------------------------------------- #


async def test_runtime_discovery_adds_area_sensor_alongside_trio() -> None:
    """Full end-to-end via ``async_setup_entry`` — the runtime-discovery
    callback captured from the dispatcher wiring must, when fired with a
    fresh boundary, add exactly four sensors including the new
    ``NavimowZoneTotalAreaSensor``. Mirrors the setup-time quartet.
    """
    coord = _make_coordinator({})  # empty registry — force runtime discovery
    device = coord.device

    hass = MagicMock()
    hass.data = {
        DOMAIN: {
            "test-entry": {"devices": [device], "coordinators": {device.id: coord}}
        }
    }
    config_entry = MagicMock()
    config_entry.entry_id = "test-entry"
    async_add_entities = MagicMock()

    captured: list = []

    def _fake_connect(_hass, _signal, cb):
        captured.append(cb)
        return lambda: None

    with patch(
        "custom_components.navimow.sensor.async_dispatcher_connect",
        side_effect=_fake_connect,
    ):
        await async_setup_entry(hass, config_entry, async_add_entities)

    # Setup call already fired for the static aggregates.
    async_add_entities.reset_mock()
    # First captured callback is the discovery listener (the wiring in
    # `_wire_zone_discovery` registers it before the forget listeners).
    on_discovery = captured[0]
    on_discovery(3)

    async_add_entities.assert_called_once()
    added = async_add_entities.call_args.args[0]
    assert len(added) == 4
    assert any(isinstance(e, NavimowZoneTotalAreaSensor) for e in added)


# --------------------------------------------------------------------- #
# 6. Forget: the area sensor is swept along with the trio               #
# --------------------------------------------------------------------- #


async def test_forget_removes_surface_entity_from_registry() -> None:
    """PR 4's forget-zone flow must sweep the ``_surface`` entity too
    (issue #88): a lingering ``unavailable`` after the record is dropped
    would be visible to the operator in the sensor list.
    """
    coord = _make_coordinator(
        {1: _rec_with_estimate(1, size_estimate=228.0, updated_ms=1_000)}
    )
    device = coord.device

    hass = MagicMock()
    hass.data = {
        DOMAIN: {
            "test-entry": {"devices": [device], "coordinators": {device.id: coord}}
        }
    }
    config_entry = MagicMock()
    config_entry.entry_id = "test-entry"
    async_add_entities = MagicMock()

    captured: list = []

    def _fake_connect(_hass, _signal, cb):
        captured.append(cb)
        return lambda: None

    ent_reg = MagicMock()
    ent_reg.async_get_entity_id.return_value = "sensor.dummy"

    with (
        patch(
            "custom_components.navimow.sensor.async_dispatcher_connect",
            side_effect=_fake_connect,
        ),
        patch(
            "homeassistant.helpers.entity_registry.async_get",
            return_value=ent_reg,
        ),
    ):
        await async_setup_entry(hass, config_entry, async_add_entities)

        # The forget listener is the second connect on the same
        # SIGNAL_ZONE_FORGOTTEN topic — `_wire_zone_forget` registers it
        # after `_wire_zone_discovery` has already registered a
        # bookkeeping one. The one that hits the entity registry is the
        # second forget callback (index 2 in registration order:
        # discovery, forget-book, forget-registry). Fire it INSIDE the
        # patch so the deferred `entity_registry.async_get` call in
        # `_on_forget` resolves to our mock.
        on_forget = captured[2]
        on_forget(1)

    # FEAT-08 naming: the four suffixes are `_last_area`,
    # `_last_duration`, `_last_mowed`, `_total_area`.
    probed = {call.args[2] for call in ent_reg.async_get_entity_id.call_args_list}
    assert f"{DOMAIN}_{device.id}_zone_1_last_area" in probed
    assert f"{DOMAIN}_{device.id}_zone_1_last_duration" in probed
    assert f"{DOMAIN}_{device.id}_zone_1_last_mowed" in probed
    assert f"{DOMAIN}_{device.id}_zone_1_total_area" in probed
