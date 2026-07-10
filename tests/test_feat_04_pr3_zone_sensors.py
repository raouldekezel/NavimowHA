"""FEAT-04 PR 3 — zone sensor platform: per-zone family + aggregate.

Every test exercises one seam:

- Eager creation from restored ``zone_registry``.
- Lazy add via the ``SIGNAL_ZONE_DISCOVERED_<device_id>`` dispatcher.
- Dedup against a previously-discovered boundary.
- Per-zone value / attribute contracts (surface ``ceil``, precise attr,
  duration, last-mowed datetime).
- Aggregate contract (state = count, attrs zone_ids / total_area / per_zone).
- Fallback naming (``Zone #<id>`` until PR 4's options flow).
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.const import UnitOfArea, UnitOfTime

from custom_components.navimow.const import DOMAIN, SIGNAL_ZONE_DISCOVERED
from custom_components.navimow.sensor import (
    NavimowZoneDurationSensor,
    NavimowZoneLastMowedSensor,
    NavimowZonesAggregateSensor,
    NavimowZoneSurfaceSensor,
    _build_zone_trio,
    _wire_zone_discovery,
    async_setup_entry,
)
from custom_components.navimow.zone_registry import ZoneRecord, ZoneRegistry


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
    """Minimal ConfigEntry mock — carries just the options dict the
    per-zone entities read to derive their display name (PR 4)."""
    entry = MagicMock()
    entry.entry_id = "test-entry"
    entry.options = options or {}
    return entry


def _rec(
    boundary_id: int,
    *,
    surface: float | None = None,
    duration: int | None = None,
    mowed_ms: int | None = None,
    cmp_max: int = 10_000,
    size_estimate: float | None = None,
    result: str | None = "completed",
) -> ZoneRecord:
    return ZoneRecord(
        boundary_id=boundary_id,
        last_mowed_ms=mowed_ms,
        last_surface_m2=surface,
        last_duration_s=duration,
        last_cmp_max=cmp_max,
        size_estimate_m2=size_estimate,
        last_result=result,
    )


# --------------------------------------------------------------------- #
# 1. Per-zone entity contracts                                          #
# --------------------------------------------------------------------- #


def test_surface_state_ceils_precise_value_and_exposes_precise_attr() -> None:
    coord = _make_coordinator({1: _rec(1, surface=227.82, size_estimate=227.82)})
    ent = NavimowZoneSurfaceSensor(coord, _make_entry(), 1)
    # ceil(227.82) = 228 m², precise float retained in attr.
    assert ent.native_value == 228
    assert ent.native_unit_of_measurement == UnitOfArea.SQUARE_METERS
    attrs = ent.extra_state_attributes
    assert attrs["boundary_id"] == 1
    assert attrs["size_estimate"] == 228
    assert attrs["last_surface_precise"] == 227.82
    assert attrs["last_cmp_max"] == 10_000
    assert attrs["last_result"] == "completed"


def test_surface_returns_none_when_registry_lacks_boundary() -> None:
    coord = _make_coordinator({})
    ent = NavimowZoneSurfaceSensor(coord, _make_entry(), 1)
    assert ent.native_value is None
    assert ent.extra_state_attributes is None
    assert ent.available is False


def test_surface_returns_none_when_record_has_no_surface_yet() -> None:
    # A newly-created ZoneRecord (first sighting, no run closed) has None
    # for last_surface_m2; the entity must render `unknown`, not crash.
    coord = _make_coordinator({3: ZoneRecord(boundary_id=3)})
    ent = NavimowZoneSurfaceSensor(coord, _make_entry(), 3)
    assert ent.native_value is None
    # But the record IS there, so `available` is True and attrs render.
    assert ent.available is True
    assert ent.extra_state_attributes["boundary_id"] == 3
    assert ent.extra_state_attributes["size_estimate"] is None


def test_duration_entity_native_value_and_class() -> None:
    coord = _make_coordinator({1: _rec(1, duration=2400)})
    ent = NavimowZoneDurationSensor(coord, _make_entry(), 1)
    assert ent.native_value == 2400
    assert ent.native_unit_of_measurement == UnitOfTime.SECONDS
    assert ent.device_class == SensorDeviceClass.DURATION


def test_last_mowed_entity_returns_utc_datetime() -> None:
    # 1_779_694_000_000 ms = 2026-05-25 07:26:40 UTC
    coord = _make_coordinator({1: _rec(1, mowed_ms=1_779_694_000_000)})
    ent = NavimowZoneLastMowedSensor(coord, _make_entry(), 1)
    got = ent.native_value
    assert isinstance(got, datetime)
    assert got.tzinfo == UTC
    assert got == datetime(2026, 5, 25, 7, 26, 40, tzinfo=UTC)


def test_zone_entities_carry_fallback_name_until_pr4() -> None:
    """PR 3 → PR 4 handoff: names are ``#<id>``. PR 4 will override
    ``_attr_name`` from the options flow map."""
    coord = _make_coordinator({3: _rec(3, surface=123.5)})
    surf = NavimowZoneSurfaceSensor(coord, _make_entry(), 3)
    dur = NavimowZoneDurationSensor(coord, _make_entry(), 3)
    lm = NavimowZoneLastMowedSensor(coord, _make_entry(), 3)
    assert surf.name == "Zone #3"
    assert dur.name == "Zone #3 durée"
    assert lm.name == "Zone #3 dernière tonte"


def test_zone_entities_unique_ids_anchored_on_boundary_id() -> None:
    coord = _make_coordinator({7: _rec(7)})
    trio = _build_zone_trio(coord, _make_entry(), 7)
    ids = {e.unique_id for e in trio}
    assert ids == {
        f"{DOMAIN}_REDACTED-ROBOT-SERIAL_zone_7",
        f"{DOMAIN}_REDACTED-ROBOT-SERIAL_zone_7_duration",
        f"{DOMAIN}_REDACTED-ROBOT-SERIAL_zone_7_last_mowed",
    }


# --------------------------------------------------------------------- #
# 2. Aggregate                                                          #
# --------------------------------------------------------------------- #


def test_aggregate_state_is_zone_count() -> None:
    coord = _make_coordinator(
        {
            1: _rec(1, size_estimate=227.82),
            3: _rec(3, size_estimate=123.54),
        }
    )
    agg = NavimowZonesAggregateSensor(coord)
    assert agg.native_value == 2


def test_aggregate_attributes_sum_size_estimates_ceiled_and_carry_ids() -> None:
    coord = _make_coordinator(
        {
            1: _rec(1, size_estimate=227.82, result="completed"),
            3: _rec(3, size_estimate=123.54, result="completed"),
        }
    )
    agg = NavimowZonesAggregateSensor(coord)
    attrs = agg.extra_state_attributes
    # ceil(227.82) + ceil(123.54) = 228 + 124 = 352
    assert attrs["total_area"] == 352
    assert attrs["zone_ids"] == [1, 3]
    assert attrs["per_zone"][1]["size_estimate"] == 228
    assert attrs["per_zone"][3]["size_estimate"] == 124


def test_aggregate_ignores_zones_without_size_estimate_yet() -> None:
    """A zone born via `_forget` recreation, or one still waiting for
    its first complete pass, has ``size_estimate_m2 = None``. It counts
    in the number of zones but contributes 0 to ``total_area``."""
    coord = _make_coordinator(
        {
            1: _rec(1, size_estimate=228.0),
            3: _rec(3, size_estimate=None),  # partial pass only, no estimate
        }
    )
    agg = NavimowZonesAggregateSensor(coord)
    assert agg.native_value == 2
    attrs = agg.extra_state_attributes
    assert attrs["total_area"] == 228  # only zone 1 contributes
    assert attrs["per_zone"][3]["size_estimate"] is None


def test_aggregate_translation_key_is_set_for_static_name() -> None:
    """Static entity → must carry translation_key so it ships named
    (PR #50 lesson)."""
    coord = _make_coordinator({})
    agg = NavimowZonesAggregateSensor(coord)
    assert agg.translation_key == "zones"


# --------------------------------------------------------------------- #
# 3. Discovery: eager (setup) + lazy (dispatcher)                       #
# --------------------------------------------------------------------- #


def test_wire_zone_discovery_dispatches_on_new_boundary() -> None:
    coord = _make_coordinator({})
    hass = MagicMock()
    config_entry = MagicMock()
    async_add_entities = MagicMock()

    captured_callback: list = []

    def _fake_connect(_hass, _signal, cb):
        captured_callback.append(cb)
        return lambda: None

    with patch(
        "custom_components.navimow.sensor.async_dispatcher_connect",
        side_effect=_fake_connect,
    ):
        _wire_zone_discovery(hass, config_entry, coord, async_add_entities)

    # Signal to fire uses the standard suffix.
    assert async_add_entities.call_count == 0  # nothing added yet

    # PR 4 also connects a forgotten-listener for the internal `known`
    # bookkeeping — first captured callback is discovery, second is forget.
    on_discovery = captured_callback[0]
    on_discovery(3)
    async_add_entities.assert_called_once()
    added = async_add_entities.call_args.args[0]
    # FEAT-08 (#88): the trio grew to a quartet — the runtime-discovered
    # zone gets `last_surface` + `duration` + `last_mowed` + `surface`
    # (the size-estimate entity). Detailed contracts in
    # `test_feat_08_zone_surface_entities.py`.
    assert len(added) == 4


def test_wire_zone_discovery_dedups_known_boundary() -> None:
    """A boundary already in the registry (restored / previously
    discovered) must not be re-added on a signal echo."""
    coord = _make_coordinator({1: _rec(1)})
    hass = MagicMock()
    config_entry = MagicMock()
    async_add_entities = MagicMock()

    captured_callback: list = []

    def _fake_connect(_hass, _signal, cb):
        captured_callback.append(cb)
        return lambda: None

    with patch(
        "custom_components.navimow.sensor.async_dispatcher_connect",
        side_effect=_fake_connect,
    ):
        _wire_zone_discovery(hass, config_entry, coord, async_add_entities)

    on_discovery = captured_callback[0]
    on_discovery(1)  # already known — no-op
    on_discovery(3)  # new — three entities added
    assert async_add_entities.call_count == 1
    on_discovery(3)  # second echo on the same new one — no-op
    assert async_add_entities.call_count == 1


def test_wire_zone_discovery_registers_unload_on_config_entry() -> None:
    """Dispatcher connect must be paired with async_on_unload — a
    dangling listener across integration reload would leak stale
    add-entities calls into the next setup."""
    coord = _make_coordinator({})
    hass = MagicMock()
    config_entry = MagicMock()
    async_add_entities = MagicMock()

    sentinel_unsub = MagicMock(name="unsub")

    with patch(
        "custom_components.navimow.sensor.async_dispatcher_connect",
        return_value=sentinel_unsub,
    ):
        _wire_zone_discovery(hass, config_entry, coord, async_add_entities)

    # Two unsubs registered: SIGNAL_ZONE_DISCOVERED + SIGNAL_ZONE_FORGOTTEN
    # (the latter keeps the local `known` set in sync so a re-discovery
    # after forget re-adds the trio). Both wrapped in async_on_unload.
    assert config_entry.async_on_unload.call_count == 2
    for call in config_entry.async_on_unload.call_args_list:
        assert call.args[0] is sentinel_unsub


def test_wire_zone_discovery_signal_name_uses_device_id() -> None:
    coord = _make_coordinator({})
    hass = MagicMock()
    config_entry = MagicMock()
    async_add_entities = MagicMock()

    with patch("custom_components.navimow.sensor.async_dispatcher_connect") as connect:
        connect.return_value = lambda: None
        _wire_zone_discovery(hass, config_entry, coord, async_add_entities)

    # First connect is discovery; the second (PR 4) is forgotten-listener.
    signal = connect.call_args_list[0].args[1]
    assert signal == f"{SIGNAL_ZONE_DISCOVERED}_{coord.device.id}"


# --------------------------------------------------------------------- #
# 4. End-to-end async_setup_entry (Fable review of PR 3)                #
# --------------------------------------------------------------------- #


async def test_async_setup_entry_eager_creates_aggregate_and_trio_per_zone() -> None:
    """End-to-end: with the registry pre-populated (as it is after PR 2's
    restore), setup adds the static aggregate plus a per-zone trio for
    every restored boundary — no dispatcher signal fires during setup.
    Closes the one untested branch flagged in the review."""
    coord = _make_coordinator(
        {
            1: _rec(1, surface=227.82, size_estimate=227.82),
            3: _rec(3, surface=123.54, size_estimate=123.54),
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
    ) as connect:
        await async_setup_entry(hass, config_entry, async_add_entities)

    # Exactly one async_add_entities call at setup time.
    async_add_entities.assert_called_once()
    added = list(async_add_entities.call_args.args[0])

    # Aggregate is present exactly once — the static, translation-keyed entity.
    aggregates = [e for e in added if isinstance(e, NavimowZonesAggregateSensor)]
    assert len(aggregates) == 1

    # A trio (surface + duration + last_mowed) for each pre-restored boundary.
    zone_entities = [
        e
        for e in added
        if isinstance(
            e,
            (
                NavimowZoneSurfaceSensor,
                NavimowZoneDurationSensor,
                NavimowZoneLastMowedSensor,
            ),
        )
    ]
    assert len(zone_entities) == 6  # 2 boundaries × 3 sensors
    assert {e._boundary_id for e in zone_entities} == {1, 3}
    # And each boundary got exactly one of each type.
    for cls in (
        NavimowZoneSurfaceSensor,
        NavimowZoneDurationSensor,
        NavimowZoneLastMowedSensor,
    ):
        assert {e._boundary_id for e in added if isinstance(e, cls)} == {1, 3}

    # No dispatcher signal fires during setup — PR 2's
    # "restore-does-not-dispatch" contract is respected end-to-end (and
    # `sensor.py`'s import surface only carries `async_dispatcher_connect`
    # and `async_dispatcher_send`; the latter is only fired by the
    # options-flow update listener, not during initial setup).
    # PR 4 registers three connect calls at setup: discovery, forgotten
    # (bookkeeping), forgotten (registry cleanup). All three unsubs
    # piped through async_on_unload alongside the update-listener unsub.
    assert connect.call_count == 3
    assert config_entry.async_on_unload.call_count >= 3
    # Update listener also wired.
    config_entry.add_update_listener.assert_called_once()


async def test_async_setup_entry_with_no_restored_zones_still_adds_aggregate() -> None:
    """Empty registry (fresh install, no history yet) → aggregate ships
    anyway, no per-zone entities, listener connected for future
    discoveries."""
    coord = _make_coordinator({})  # empty registry
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
    ) as connect:
        await async_setup_entry(hass, config_entry, async_add_entities)

    added = list(async_add_entities.call_args.args[0])
    aggregates = [e for e in added if isinstance(e, NavimowZonesAggregateSensor)]
    zone_entities = [
        e
        for e in added
        if isinstance(
            e,
            (
                NavimowZoneSurfaceSensor,
                NavimowZoneDurationSensor,
                NavimowZoneLastMowedSensor,
            ),
        )
    ]
    assert len(aggregates) == 1
    assert zone_entities == []
    # PR 4: three connects at setup (discovery + 2× forgotten wiring).
    assert connect.call_count == 3
