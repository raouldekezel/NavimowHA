"""FEAT-05 step (c) — entity gating, HA event bus fan-out, Store
persistence, and restore-mid-run continuity.

The tracker (b) is the state machine; (c) turns its returned events and
current state into HA-visible surfaces (sensors, events, Store payload).
Every test exercises one seam:

- Entity gating: `run_progress` / `zone_progress` `None` at rest, held
  during `PAUSED_DOCKED`; `run_state` enum reflects tracker + vehicle
  state; `last_run_*` read from `last_finished_run`.
- Event fan-out: each `run_started` / `run_finished` / `run_reopened`
  emitted by the tracker becomes a `navimow_run_*` event on the bus.
- History: capped list of closed runs, in-place trim.
- Store: `snapshot()` → new coordinator → `restore()` → `RUNNING`
  survives with the same accumulator; heartbeat save while `RUNNING`.
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

from custom_components.navimow.const import (
    EVENT_RUN_FINISHED,
    EVENT_RUN_REOPENED,
    EVENT_RUN_STARTED,
    HISTORY_MAX,
    TRACKER_HEARTBEAT_SECONDS,
)
from custom_components.navimow.location import parse_location_type_2
from custom_components.navimow.run_tracker import (
    STATE_COMPLETED,
    STATE_IDLE,
    STATE_INTERRUPTED,
    STATE_PAUSED_DOCKED,
    STATE_RUNNING,
    VS_DOCKED_IDLE,
    VS_MOWING,
    VS_RETURNING,
    RunTracker,
)
from custom_components.navimow.sensor import SENSOR_DESCRIPTIONS


def _desc(key: str):
    return next(d for d in SENSOR_DESCRIPTIONS if d.key == key)


def _make_coordinator():
    """A minimal `__new__`-built coordinator carrying only the FEAT-05
    surface the tests exercise. Kept local to avoid cross-file coupling
    with the older helpers; consolidation into `conftest.py` deferred
    to a housekeeping PR."""
    from custom_components.navimow.coordinator import NavimowCoordinator

    coord = NavimowCoordinator.__new__(NavimowCoordinator)
    coord.hass = MagicMock()
    coord.hass.bus.async_fire = MagicMock()
    coord.hass.async_create_task = MagicMock()
    coord.logger = MagicMock()
    coord.name = "test"
    coord.update_interval = None
    coord.config_entry = MagicMock()
    device = MagicMock()
    device.id = "REDACTED-ROBOT-SERIAL"
    coord.device = device
    coord.position = None
    coord.vehicle_state = None
    coord._last_position_dispatch = 0.0
    coord.stats = None
    coord._last_accepted_time_type1 = None
    coord._last_accepted_time_type2 = None
    coord._type1_drop_streak = 0
    coord._type2_drop_streak = 0
    coord.run_tracker = RunTracker()
    coord.history = []
    coord.last_finished_run = None
    coord._store = None
    coord._last_store_save_monotonic = 0.0
    coord._build_data = MagicMock(return_value={})
    coord.async_set_updated_data = MagicMock()
    return coord


def _feed_type2(coord, item):
    parsed = parse_location_type_2(item)
    coord._forward_run_events(coord.run_tracker.process_type2(parsed))


# --------------------------------------------------------------------- #
# 1. Entity gating — run_progress / zone_progress / run_state           #
# --------------------------------------------------------------------- #


def test_run_progress_is_none_at_idle() -> None:
    coord = _make_coordinator()
    assert _desc("run_progress").value_fn(coord) is None
    assert _desc("zone_progress").value_fn(coord) is None


def test_run_progress_reads_last_mp_while_running() -> None:
    coord = _make_coordinator()
    _feed_type2(
        coord,
        {
            "type": 2,
            "currentMowBoundary": 1,
            "currentMowProgress": 4700,
            "mowingPercentage": 42,
            "subtotalArea": "100.0",
            "mowingWeekArea": "100.0",
            "time": 1_000_000_000_000,
        },
    )
    assert coord.run_tracker.state == STATE_RUNNING
    assert _desc("run_progress").value_fn(coord) == 42
    # cmp_max=4700 → 47.0 %
    assert _desc("zone_progress").value_fn(coord) == 47.0


def test_run_progress_held_during_paused_docked() -> None:
    coord = _make_coordinator()
    _feed_type2(
        coord,
        {
            "type": 2,
            "currentMowBoundary": 1,
            "currentMowProgress": 4700,
            "mowingPercentage": 42,
            "subtotalArea": "100.0",
            "mowingWeekArea": "100.0",
            "time": 1_000_000_000_000,
        },
    )
    coord.run_tracker.process_vehicle_state(VS_DOCKED_IDLE)
    assert coord.run_tracker.state == STATE_PAUSED_DOCKED
    # Values are held (still readable) during pause.
    assert _desc("run_progress").value_fn(coord) == 42
    assert _desc("zone_progress").value_fn(coord) == 47.0


def test_run_progress_drops_to_none_on_completed() -> None:
    coord = _make_coordinator()
    _feed_type2(
        coord,
        {
            "type": 2,
            "currentMowBoundary": 1,
            "currentMowProgress": 10000,
            "mowingPercentage": 100,
            "subtotalArea": "200.0",
            "mowingWeekArea": "200.0",
            "time": 1_000_000_000_000,
        },
    )
    assert coord.run_tracker.state == STATE_COMPLETED
    assert _desc("run_progress").value_fn(coord) is None
    assert _desc("zone_progress").value_fn(coord) is None


# --------------------------------------------------------------------- #
# 2. run_state display                                                  #
# --------------------------------------------------------------------- #


def test_run_state_idle() -> None:
    coord = _make_coordinator()
    assert _desc("run_state").value_fn(coord) == "idle"


def test_run_state_running_vs_returning() -> None:
    coord = _make_coordinator()
    _feed_type2(
        coord,
        {
            "type": 2,
            "mowingPercentage": 40,
            "subtotalArea": "100.0",
            "mowingWeekArea": "100.0",
            "time": 1_000_000_000_000,
        },
    )
    coord.vehicle_state = VS_MOWING
    assert _desc("run_state").value_fn(coord) == "running"
    coord.vehicle_state = VS_RETURNING
    assert _desc("run_state").value_fn(coord) == "returning"


def test_run_state_paused() -> None:
    coord = _make_coordinator()
    _feed_type2(
        coord,
        {
            "type": 2,
            "mowingPercentage": 40,
            "subtotalArea": "100.0",
            "mowingWeekArea": "100.0",
            "time": 1_000_000_000_000,
        },
    )
    coord.run_tracker.process_vehicle_state(VS_DOCKED_IDLE)
    coord.vehicle_state = VS_DOCKED_IDLE
    assert _desc("run_state").value_fn(coord) == "paused"


# --------------------------------------------------------------------- #
# 3. last_run_* sensors                                                 #
# --------------------------------------------------------------------- #


def test_last_run_started_is_none_at_cold_boot() -> None:
    coord = _make_coordinator()
    assert _desc("last_run_started").value_fn(coord) is None


def test_last_run_started_reads_open_run_start_time() -> None:
    coord = _make_coordinator()
    _feed_type2(
        coord,
        {
            "type": 2,
            "mowingPercentage": 40,
            "subtotalArea": "100.0",
            "mowingWeekArea": "100.0",
            "time": 1_000_000_000_000,
        },
    )
    dt = _desc("last_run_started").value_fn(coord)
    assert isinstance(dt, datetime)
    # 1_000_000_000_000 ms = 2001-09-09T01:46:40 UTC.
    assert dt.year == 2001


def test_last_run_duration_and_result_after_close() -> None:
    coord = _make_coordinator()
    _feed_type2(
        coord,
        {
            "type": 2,
            "mowingPercentage": 40,
            "subtotalArea": "100.0",
            "mowingWeekArea": "100.0",
            "mowStartType": 1,
            "time": 1_000_000_000_000,
        },
    )
    # Duration is `None` while the run is still open.
    assert _desc("last_run_duration").value_fn(coord) is None

    _feed_type2(
        coord,
        {
            "type": 2,
            "mowingPercentage": 100,
            "subtotalArea": "200.0",
            "mowingWeekArea": "200.0",
            "mowStartType": 1,
            "time": 1_000_000_060_000,  # +60 s
        },
    )
    assert coord.last_finished_run is not None
    assert _desc("last_run_duration").value_fn(coord) == 60
    assert _desc("last_run_result").value_fn(coord) == "completed"
    attrs = _desc("last_run_result").attrs_fn(coord)
    assert attrs["mow_start_type"] == 1
    assert isinstance(attrs["zones"], list)
    assert attrs["history"] is coord.history


# --------------------------------------------------------------------- #
# 4. Event bus fan-out                                                  #
# --------------------------------------------------------------------- #


def test_run_started_fires_on_bus() -> None:
    coord = _make_coordinator()
    _feed_type2(
        coord,
        {
            "type": 2,
            "mowingPercentage": 40,
            "subtotalArea": "100.0",
            "mowingWeekArea": "100.0",
            "time": 1_000_000_000_000,
        },
    )
    fired = [call.args for call in coord.hass.bus.async_fire.call_args_list]
    assert fired[0][0] == EVENT_RUN_STARTED
    assert fired[0][1]["device_id"] == "REDACTED-ROBOT-SERIAL"


def test_run_finished_fires_on_bus_and_appends_history() -> None:
    coord = _make_coordinator()
    _feed_type2(
        coord,
        {
            "type": 2,
            "mowingPercentage": 40,
            "subtotalArea": "100.0",
            "mowingWeekArea": "100.0",
            "time": 1_000_000_000_000,
        },
    )
    coord.hass.bus.async_fire.reset_mock()

    _feed_type2(
        coord,
        {
            "type": 2,
            "mowingPercentage": 100,
            "subtotalArea": "200.0",
            "mowingWeekArea": "200.0",
            "time": 1_000_000_060_000,
        },
    )
    kinds = [call.args[0] for call in coord.hass.bus.async_fire.call_args_list]
    assert kinds == [EVENT_RUN_FINISHED]
    assert len(coord.history) == 1
    assert coord.last_finished_run is coord.history[-1]


def test_run_reopened_fires_on_bus() -> None:
    coord = _make_coordinator()
    _feed_type2(
        coord,
        {
            "type": 2,
            "mowingPercentage": 100,
            "subtotalArea": "200.0",
            "mowingWeekArea": "200.0",
            "time": 1_000_000_000_000,
        },
    )
    assert coord.run_tracker.state == STATE_COMPLETED
    coord.hass.bus.async_fire.reset_mock()

    _feed_type2(
        coord,
        {
            "type": 2,
            "mowingPercentage": 100,
            "subtotalArea": "205.0",  # strict progress
            "mowingWeekArea": "205.0",
            "time": 1_000_000_060_000,
        },
    )
    kinds = [call.args[0] for call in coord.hass.bus.async_fire.call_args_list]
    # `mp=100` on the reopen packet immediately closes again → we see
    # reopened followed by finished.
    assert kinds == [EVENT_RUN_REOPENED, EVENT_RUN_FINISHED]


# --------------------------------------------------------------------- #
# 5. History cap at HISTORY_MAX                                         #
# --------------------------------------------------------------------- #


def test_history_capped_at_max() -> None:
    coord = _make_coordinator()
    # Prime with HISTORY_MAX + 3 completed runs by manually invoking
    # the tracker's own close helper — spinning HISTORY_MAX real close
    # cycles through the state machine would take ~50 packets per run.
    for i in range(HISTORY_MAX + 3):
        payload = {
            "result": "completed",
            "start_time": i * 1000,
            "end_time": i * 1000 + 60_000,
            "duration_ms": 60_000,
            "mow_start_type": 1,
            "zones": [],
        }
        from custom_components.navimow.run_tracker import EVENT_RUN_FINISHED as K
        from custom_components.navimow.run_tracker import Event

        coord._forward_run_events([Event(kind=K, payload=payload)])
    assert len(coord.history) == HISTORY_MAX
    # FIFO trim keeps the most recent entries.
    assert coord.history[0]["start_time"] == 3 * 1000
    assert coord.history[-1]["start_time"] == (HISTORY_MAX + 2) * 1000


# --------------------------------------------------------------------- #
# 6. Store save + restore round trip                                    #
# --------------------------------------------------------------------- #


def test_store_save_scheduled_on_tracker_events() -> None:
    coord = _make_coordinator()
    coord._store = MagicMock()
    coord._store.async_save = MagicMock()
    coord.hass.async_create_task = MagicMock()

    _feed_type2(
        coord,
        {
            "type": 2,
            "mowingPercentage": 40,
            "subtotalArea": "100.0",
            "mowingWeekArea": "100.0",
            "time": 1_000_000_000_000,
        },
    )
    coord.hass.async_create_task.assert_called()


def test_store_save_noop_when_no_store() -> None:
    coord = _make_coordinator()
    # No _store — should still fire events without crashing.
    _feed_type2(
        coord,
        {
            "type": 2,
            "mowingPercentage": 40,
            "subtotalArea": "100.0",
            "mowingWeekArea": "100.0",
            "time": 1_000_000_000_000,
        },
    )
    coord.hass.async_create_task.assert_not_called()


def test_restore_mid_run_continues_same_run() -> None:
    """Snapshot a `RUNNING` tracker, restore into a fresh one, feed the
    next legitimate packet — it must NOT open a new run.
    """
    src = _make_coordinator()
    _feed_type2(
        src,
        {
            "type": 2,
            "currentMowBoundary": 1,
            "currentMowProgress": 4000,
            "mowingPercentage": 40,
            "subtotalArea": "100.0",
            "mowingWeekArea": "100.0",
            "mowStartType": 1,
            "time": 1_000_000_000_000,
        },
    )
    original_start = src.run_tracker.current_run["start_time"]
    payload = src._build_store_payload()

    dst = _make_coordinator()
    # `restore()` returns True on version match.
    assert dst.run_tracker.restore(payload["tracker"]) is True
    dst._last_accepted_time_type1 = payload["cursors"]["type1"]
    dst._last_accepted_time_type2 = payload["cursors"]["type2"]
    dst.history = list(payload["history"])
    dst.last_finished_run = payload["last_finished_run"]

    # Continuation packet, strict progress, invariant holds.
    _feed_type2(
        dst,
        {
            "type": 2,
            "currentMowBoundary": 1,
            "currentMowProgress": 5000,
            "mowingPercentage": 50,
            "subtotalArea": "110.0",
            "mowingWeekArea": "110.0",
            "mowStartType": 1,
            "time": 1_000_000_060_000,
        },
    )
    assert dst.run_tracker.state == STATE_RUNNING
    assert dst.run_tracker.current_run["start_time"] == original_start
    # No new `run_started` events fired on the destination coordinator.
    kinds = [call.args[0] for call in dst.hass.bus.async_fire.call_args_list]
    assert kinds == []
