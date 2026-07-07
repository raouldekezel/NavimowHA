"""FEAT-02 — mowing metrics via /location type 2.

Extends FEAT-01's parser and coordinator to handle mowing stats
(mowingPercentage, currentMowProgress, subtotalArea, mowingWeekArea,
currentMowBoundary). Adds three sensors: progression, weekly_area,
current_zone.
"""

from __future__ import annotations

from unittest.mock import MagicMock

# --------------------------------------------------------------------- #
# 1. parser                                                             #
# --------------------------------------------------------------------- #


def test_parse_location_type_2_full_payload() -> None:
    from custom_components.navimow.location import parse_location_type_2

    parsed = parse_location_type_2(
        {
            "type": 2,
            "action": -1,
            "currentMowBoundary": 3,
            "currentMowProgress": 4321,
            "mowingPercentage": 62,
            "subtotalArea": "180.5",
            "mowingWeekArea": "1234.75",
        }
    )
    # Per-field asserts (not exact dict equality) so FEAT-05 shape
    # extensions (`time`, `mow_start_type`, `sub_action`) do not force
    # this earlier test to churn — those fields have their own guards
    # in test_feat_05a_location_ordering_guard.py.
    assert parsed is not None
    assert parsed["mowing_percentage"] == 62
    assert parsed["current_mow_progress"] == 4321
    assert parsed["area_session"] == 180.5
    assert parsed["area_week"] == 1234.75
    assert parsed["boundary"] == 3
    assert parsed["action"] == -1


def test_parse_location_type_2_sparse_payload() -> None:
    """Sparse type-2 packets (only mowingWeekArea, or only boundary) were
    NOT observed in the operator's 2026-05-25 multizone run (diag #20),
    but the parser is defensively tolerant should another Navimow
    firmware emit them: fill omitted fields with None instead of
    dropping the item.
    """
    from custom_components.navimow.location import parse_location_type_2

    parsed = parse_location_type_2({"type": 2, "mowingWeekArea": "42.0"})
    assert parsed is not None
    assert parsed["area_week"] == 42.0
    assert parsed["mowing_percentage"] is None
    assert parsed["boundary"] is None


def test_parse_location_type_2_non_dict_returns_none() -> None:
    from custom_components.navimow.location import parse_location_type_2

    assert parse_location_type_2([1, 2, 3]) is None  # type: ignore[arg-type]
    assert parse_location_type_2(None) is None  # type: ignore[arg-type]


# --------------------------------------------------------------------- #
# 2. coordinator wiring                                                 #
# --------------------------------------------------------------------- #


def _make_coordinator():
    from custom_components.navimow.coordinator import NavimowCoordinator

    coordinator = NavimowCoordinator.__new__(NavimowCoordinator)
    coordinator.hass = MagicMock()
    coordinator.logger = MagicMock()
    coordinator.name = "test"
    coordinator.update_interval = None
    coordinator.config_entry = MagicMock()

    device = MagicMock()
    device.id = "REDACTED-ROBOT-SERIAL"
    coordinator.device = device

    coordinator.position = None
    coordinator.vehicle_state = None
    coordinator._last_position_dispatch = 0.0
    coordinator.stats = None
    # FEAT-05 layer-1 guard cursors + drop streaks, per-stream — set
    # here so tests that use `__new__` (skipping `__init__`) can invoke
    # the /location handlers.
    coordinator._last_accepted_time_type1 = None
    coordinator._last_accepted_time_type2 = None
    coordinator._type1_drop_streak = 0
    coordinator._type2_drop_streak = 0
    # FEAT-05 (b): run tracker (idle, emits nothing until fed).
    from custom_components.navimow.run_tracker import RunTracker

    coordinator.run_tracker = RunTracker()

    # FEAT-05 (c) persistence + history attributes.

    coordinator.history = []

    coordinator.last_finished_run = None

    # FEAT-04 PR 2: fed by _forward_run_events on run_finished.
    from custom_components.navimow.zone_registry import ZoneRegistry

    coordinator.zone_registry = ZoneRegistry()

    coordinator._store = None

    coordinator._last_store_save_monotonic = 0.0
    coordinator._build_data = MagicMock(return_value={})
    coordinator.async_set_updated_data = MagicMock()
    return coordinator


def test_handle_location_item_type_2_populates_stats() -> None:
    coordinator = _make_coordinator()

    coordinator.handle_location_item(
        {
            "type": 2,
            "mowingPercentage": 40,
            "subtotalArea": "80.0",
            "mowingWeekArea": "500.5",
            "currentMowBoundary": 1,
        }
    )

    assert coordinator.stats is not None
    assert coordinator.stats["mowing_percentage"] == 40
    assert coordinator.stats["area_session"] == 80.0
    assert coordinator.stats["area_week"] == 500.5
    assert coordinator.stats["boundary"] == 1
    coordinator.async_set_updated_data.assert_called_once()


def test_stats_are_preserved_across_ticks_when_no_type_2() -> None:
    """The /location channel stops emitting type 2 while the robot is
    docked. The coordinator must keep the last observed values so HA
    does not flip to 'unknown' when a mowing session ends.
    """
    coordinator = _make_coordinator()

    # First type-2 during mowing.
    coordinator.handle_location_item(
        {"type": 2, "mowingPercentage": 100, "mowingWeekArea": "780.0"}
    )
    assert coordinator.stats["mowing_percentage"] == 100

    # A subsequent type-1 (docked) must not clear the stats.
    coordinator.handle_location_item(
        {"type": 1, "postureX": "0.02", "postureY": "-0.01", "vehicleState": 2}
    )
    assert coordinator.stats["mowing_percentage"] == 100
    assert coordinator.stats["area_week"] == 780.0


# --------------------------------------------------------------------- #
# 3. sensor descriptions                                                #
# --------------------------------------------------------------------- #


def _find_sensor(key):
    from custom_components.navimow.sensor import SENSOR_DESCRIPTIONS

    return next(d for d in SENSOR_DESCRIPTIONS if d.key == key)


def test_progression_sensor_retired_by_hard_14() -> None:
    """HARD-14: ``sensor.<slug>_progression`` was retired. Its
    responsibilities are covered by ``run_progress`` (task progress,
    ``None`` at rest — tracker-driven) and ``zone_progress`` (per-zone
    ``cmp_max`` / 100, ``None`` at rest). The parsers still extract
    ``mowing_percentage`` / ``current_mow_progress`` / ``area_session``
    / ``action`` for the tracker; only the entity is gone.
    """
    with __import__("pytest").raises(StopIteration):
        _find_sensor("progression")


def test_weekly_area_sensor_reads_area_week() -> None:
    # HARD-11: ceil to the next m² for parity with the FEAT-04 rounding
    # convention. Precise float is not exposed as an attribute today —
    # add one if a card needs it.
    desc = _find_sensor("weekly_area")
    coordinator = MagicMock()
    coordinator.stats = {"area_week": 620.75}

    assert desc.value_fn(coordinator) == 621  # ceil(620.75)


def test_current_zone_sensor_renders_hash_prefixed_when_unmapped() -> None:
    """Boundary id is not sequential (1 = zone 1, 2 = tunnel/transit, 3 =
    zone 2 on the operator's install). Rendered as ``#<id>`` when no
    rename is set on the boundary — HARD-11 pins that fallback path.

    BUG-12: source is the tracker (not ``coordinator.stats``); the
    stats fallback was retired.
    """
    from custom_components.navimow.run_tracker import STATE_RUNNING

    desc = _find_sensor("current_zone")
    coordinator = MagicMock()
    coordinator.run_tracker = MagicMock()
    coordinator.run_tracker.state = STATE_RUNNING
    coordinator.run_tracker.current_run = {"zones": [{"boundary_id": 3}]}
    coordinator.stats = None
    # Clear the stashed config entry — MagicMock would otherwise yield a
    # truthy fake for `getattr(c, "config_entry", None)`, and the helper
    # would follow its options path.
    del coordinator.config_entry

    assert desc.value_fn(coordinator) == "#3"
    attrs = desc.attrs_fn(coordinator)
    assert attrs["boundary_id"] == 3


def test_sensors_return_none_when_stats_empty() -> None:
    """Before the first /location type-2 arrival, coordinator.stats is
    None. Sensors must return None (HA renders "unknown") rather than
    crash.

    HARD-14: ``progression`` was retired — check ``weekly_area`` and
    ``current_zone`` only. ``current_zone`` also needs an idle
    ``run_tracker`` to produce ``None`` (BUG-12).
    """
    from custom_components.navimow.run_tracker import STATE_IDLE

    coordinator = MagicMock()
    coordinator.stats = None
    coordinator.run_tracker = MagicMock()
    coordinator.run_tracker.state = STATE_IDLE
    coordinator.run_tracker.current_run = None

    for key in ("weekly_area", "current_zone"):
        desc = _find_sensor(key)
        assert desc.value_fn(coordinator) is None
