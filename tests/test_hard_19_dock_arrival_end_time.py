"""HARD-19 — a dock-closed run ends at the dock arrival, not the last
type-2 (#120).

HARD-18 (#117) anchored a run's `start_time` on the vs=4 activation edge,
so a session's duration now includes the *outbound* transit. But a
seeded run's `end_time` still came from the last accepted type-2, so the
*return* transit (last mow packet → dock) was excluded — an
outbound-counted / return-dropped asymmetry. HARD-19 stamps the
dock-arrival type-1 `time` on `current_run["dock_arrival_time"]` and
`_close_run` reads `end = max(dock_arrival, last_time)`, making a
session's duration exactly FEAT-06's activation → dock arrival, both
edges type-1-stamped.

Adopted design: issue #120 body + operator clarification (2026-07-23) +
Fable brief v2 / v2.1. The stamp lives in the run (rides the snapshot
deepcopy, no `SNAPSHOT_VERSION` bump), set on the RUNNING → PAUSED_DOCKED
edge, frozen through the docked idle↔charge flips, dropped on resume,
and floored by `last_time` so a session never ends before its last
accepted packet (the one BUG-09 #89 dock-first-then-`mp = 100` shape).

Tests map to the brief's eight families plus the v2.1 ordering pins
O1/O2 (observable behaviour only — the tracker/coordinator API,
`caplog`, snapshot/restore; no source introspection).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from custom_components.navimow.location import parse_location_type_2
from custom_components.navimow.run_tracker import (
    EVENT_RUN_FINISHED,
    EVENT_RUN_STARTED,
    INTERRUPT_SUSTAIN_SECONDS,
    RESULT_COMPLETED,
    RESULT_INTERRUPTED,
    SNAPSHOT_VERSION,
    STATE_IDLE,
    STATE_PAUSED_DOCKED,
    STATE_RUNNING,
    VS_DOCKED_CHARGING,
    VS_DOCKED_IDLE,
    VS_DOCKED_UNPOWERED,
    VS_MAPPING,
    VS_MOWING,
    RunTracker,
)

# Anchor epoch-ms value (arbitrary; only ordering + deltas matter).
_T0 = 5_000_000_000_000
_DEVICE_ID = "REDACTED-ROBOT-SERIAL"


class _FakeClock:
    """Controllable monotonic clock for the sustained-dock timer."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, secs: float) -> None:
        self.now += secs


def _t2_item(
    *,
    boundary: int = 1,
    cmp: int = 5000,
    mp: int = 50,
    sub: float = 20.0,
    wk: float = 357.63,
    time: int,
    mow_start_type: int = 0,
    action: int = 8,
) -> dict:
    """A raw type-2 `/location` item in the on-wire shape."""
    return {
        "type": 2,
        "currentMowBoundary": boundary,
        "currentMowProgress": cmp,
        "mowingPercentage": mp,
        "subtotalArea": str(sub),
        "mowingWeekArea": str(wk),
        "mowStartType": mow_start_type,
        "action": action,
        "time": time,
    }


def _feed_t2(tracker: RunTracker, **kw) -> list:
    return tracker.process_type2(parse_location_type_2(_t2_item(**kw)))


def _seed_running(tracker: RunTracker, *, start: int = _T0) -> None:
    """vs=4 activation → first real type-2 seeds a non-provisional run,
    off-dock (tracker.vehicle_state == 4), zones == [{boundary 1}]."""
    tracker.process_vehicle_state(VS_MOWING, time_ms=start)
    _feed_t2(tracker, mp=50, cmp=5000, sub=20.0, boundary=1, time=start + 1_000)
    assert tracker.is_provisional is False
    assert tracker.state == STATE_RUNNING


# ===================================================================== #
# Family 1 + Pin O1 — fast-path completion ends at the dock arrival       #
# ===================================================================== #


@pytest.mark.parametrize(
    "dock_vs", [VS_DOCKED_IDLE, VS_DOCKED_CHARGING, VS_DOCKED_UNPOWERED]
)
def test_pin_o1_fast_completion_ends_strictly_at_dock_arrival(dock_vs: int) -> None:
    """Pin O1 (type-1 first, the immediate-completion path). After mowing
    to `mp = 100` off-dock, a SINGLE `process_vehicle_state(vs, time_ms=Y)`
    call fires the completed close *within that same call*, with
    `end_time == Y` (strict dock arrival, not the last type-2) — no other
    input delivered. The close cannot fall back to the packet cursor
    because the type-1 that closes is the type-1 that stamps.

    `vs = 6` (isMapping) is excluded from the completion set (BUG-09 #89),
    so it is covered separately below, not here.
    """
    tracker = RunTracker()
    _seed_running(tracker)
    last_packet = _T0 + 2_000
    _feed_t2(tracker, mp=100, cmp=10000, sub=40.0, boundary=1, time=last_packet)
    # Off-dock (vs=4): a completed run does not close while mowing.
    assert tracker.state == STATE_RUNNING

    dock_y = _T0 + 5_000  # dock arrival strictly after the last packet
    closed = tracker.process_vehicle_state(dock_vs, time_ms=dock_y)

    assert [e.kind for e in closed] == [EVENT_RUN_FINISHED]
    p = closed[0].payload
    assert p["result"] == RESULT_COMPLETED
    assert p["end_time"] == dock_y  # strict dock arrival, NOT last_packet
    assert p["start_time"] == _T0
    assert p["duration_ms"] == dock_y - _T0
    assert tracker.state == STATE_IDLE


def test_vs6_stamps_arrival_but_holds_then_vs1_completes_at_the_vs6_edge() -> None:
    """The vs=6 arm of family 1: vs=6 (isMapping) is a DOCKED_STATES member
    so the arrival edge IS stamped, but it is excluded from the completion
    set — the run HOLDS (no close, no timer). A later flip to vs=1 fires
    the completion, and the end is the *first* dock arrival (the vs=6
    edge), frozen through the 6→1 flip, not the vs=1 time.
    """
    tracker = RunTracker()
    _seed_running(tracker)
    _feed_t2(tracker, mp=100, cmp=10000, sub=40.0, boundary=1, time=_T0 + 2_000)

    y6 = _T0 + 5_000
    ev6 = tracker.process_vehicle_state(VS_MAPPING, time_ms=y6)
    assert ev6 == []  # held: vs=6 excluded from completion
    assert tracker.state == STATE_PAUSED_DOCKED
    assert tracker.current_run["dock_arrival_time"] == y6

    ev1 = tracker.process_vehicle_state(VS_DOCKED_IDLE, time_ms=_T0 + 7_000)
    assert [e.kind for e in ev1] == [EVENT_RUN_FINISHED]
    p = ev1[0].payload
    assert p["result"] == RESULT_COMPLETED
    assert p["end_time"] == y6  # frozen at the first (vs=6) dock arrival
    assert p["duration_ms"] == y6 - _T0


# ===================================================================== #
# Family 2 — the arbitration pin: post-arrival flips never move the end   #
# ===================================================================== #


def test_arbitration_pin_charge_idle_flips_do_not_move_end_interrupted() -> None:
    """Dock at Y (vs=1, `mp` below threshold) → charge↔idle flips 10 min
    later → sustained timer fires. The flips reset/re-arm *when* it closes,
    but the end stays the dock arrival Y — not Y+60 s, not the flip time.
    Interrupted label (mp never reached the completion rule).
    """
    clk = _FakeClock()
    tracker = RunTracker(clock=clk)
    _seed_running(tracker)

    dock_y = _T0 + 3_000
    tracker.process_vehicle_state(VS_DOCKED_IDLE, time_ms=dock_y)
    assert tracker.current_run["dock_arrival_time"] == dock_y

    # 10 min later, charge↔idle flips (each carries a fresh, later time_ms
    # but PAUSED_DOCKED re-entries never restamp the arrival edge).
    clk.advance(600)
    tracker.process_vehicle_state(VS_DOCKED_CHARGING, time_ms=dock_y + 600_000)
    tracker.process_vehicle_state(VS_DOCKED_IDLE, time_ms=dock_y + 600_500)
    assert tracker.current_run["dock_arrival_time"] == dock_y  # unmoved

    # Sustained past the debounce from the last idle arm → interrupted close.
    tracker.tick()  # arm
    clk.advance(INTERRUPT_SUSTAIN_SECONDS + 1)
    closed = tracker.tick()
    assert [e.kind for e in closed] == [EVENT_RUN_FINISHED]
    p = closed[0].payload
    assert p["result"] == RESULT_INTERRUPTED
    assert p["end_time"] == dock_y  # dock arrival, not the flip / +60 s
    assert p["duration_ms"] == dock_y - _T0


def test_completed_sustained_close_after_restore_ends_at_dock() -> None:
    """The completed arm of the arbitration pin. A completed run closes on
    the fast path the instant a live vs-edge in {1,2,3} arrives, so the
    sustained-timer *completed* path is only reachable post-restore, where
    loading runs no fast-path re-evaluation. A restored completed-pending
    PAUSED_DOCKED run carrying a dock stamp closes `completed` via the
    sustained timer, at the stamped dock arrival — the label is derived in
    `_close_run`, the end is the frozen stamp.
    """
    dock_y = _T0 + 3_000
    snap = {
        "version": SNAPSHOT_VERSION,
        "state": STATE_PAUSED_DOCKED,
        "vehicle_state": VS_DOCKED_IDLE,
        "current_run": {
            "start_time": _T0,
            "mow_start_type": 0,
            "wk0": 50.0,
            "sub0": 10.0,
            "last_time": _T0 + 1_000,
            "last_sub": 60.0,
            "last_wk": 110.0,
            "last_mp": 100,  # completed
            "zones": [
                {
                    "boundary_id": 1,
                    "first_time": _T0,
                    "last_time": _T0 + 1_000,
                    "cmp_max": 10000,
                    "sub_entry": 10.0,
                    "sub_exit": 60.0,
                }
            ],
            "dock_arrival_time": dock_y,
        },
        "last_accepted_wk": 110.0,
        "last_accepted_time_ms": _T0 + 1_000,
        "drops": {"pending_reset_holds": 0},
        "counters": {},
    }
    clk = _FakeClock()
    tracker = RunTracker(clock=clk)
    assert tracker.restore(snap) is True
    assert tracker.current_run["dock_arrival_time"] == dock_y

    tracker.tick()  # arm
    clk.advance(INTERRUPT_SUSTAIN_SECONDS + 1)
    closed = tracker.tick()
    assert [e.kind for e in closed] == [EVENT_RUN_FINISHED]
    p = closed[0].payload
    assert p["result"] == RESULT_COMPLETED  # label from last_mp, via _close_run
    assert p["end_time"] == dock_y
    assert p["duration_ms"] == dock_y - _T0


# ===================================================================== #
# Family 3 — a mid-run recharge dock is cleared on resume                 #
# ===================================================================== #


def test_mid_run_recharge_stamp_cleared_end_is_final_dock() -> None:
    """Dock T1 (recharge) → the mower leaves and a fresh type-2 resumes the
    mow (clearing the intermediate stamp) → final dock T2 → close. The end
    is T2, never T1: only the dock that actually ends the session leaves
    its stamp standing.
    """
    tracker = RunTracker()
    _seed_running(tracker)

    t1 = _T0 + 2_000
    tracker.process_vehicle_state(VS_DOCKED_CHARGING, time_ms=t1)  # recharge dock
    assert tracker.state == STATE_PAUSED_DOCKED
    assert tracker.current_run["dock_arrival_time"] == t1

    # Robot leaves the dock (vs=4 — a seeded run does not resume on the
    # type-1 alone), then a fresh type-2 resumes and clears the stamp.
    tracker.process_vehicle_state(VS_MOWING, time_ms=_T0 + 2_500)
    _feed_t2(tracker, mp=60, cmp=6000, sub=25.0, boundary=1, time=_T0 + 3_000)
    assert tracker.state == STATE_RUNNING
    assert tracker.current_run["dock_arrival_time"] is None  # T1 cleared on resume

    # Mow to completion, then the FINAL dock.
    _feed_t2(tracker, mp=100, cmp=10000, sub=40.0, boundary=1, time=_T0 + 4_000)
    assert tracker.state == STATE_RUNNING
    t2 = _T0 + 6_000
    closed = tracker.process_vehicle_state(VS_DOCKED_IDLE, time_ms=t2)
    assert [e.kind for e in closed] == [EVENT_RUN_FINISHED]
    p = closed[0].payload
    assert p["result"] == RESULT_COMPLETED
    assert p["end_time"] == t2  # final dock, not T1, not the last packet
    assert p["duration_ms"] == t2 - _T0


# ===================================================================== #
# Family 4 — a reset-driven close with no dock keeps the last-packet end  #
# ===================================================================== #


def test_reset_driven_close_no_dock_keeps_last_packet_end() -> None:
    """A fresh task's first packet (sub regresses below the ceiling) closes
    the open run mid-mow. No dock transition was observed, so the stamp is
    absent and the end falls back to the last accepted packet — unchanged.
    """
    tracker = RunTracker()
    _seed_running(tracker)
    last_packet = _T0 + 2_000
    _feed_t2(tracker, mp=55, cmp=5500, sub=45.0, boundary=1, time=last_packet)
    assert tracker.current_run.get("dock_arrival_time") is None  # never docked

    events = _feed_t2(tracker, mp=5, cmp=200, sub=2.0, boundary=2, time=_T0 + 3_000)
    kinds = [e.kind for e in events]
    assert EVENT_RUN_FINISHED in kinds and EVENT_RUN_STARTED in kinds
    finished = next(e for e in events if e.kind == EVENT_RUN_FINISHED)
    assert finished.payload["end_time"] == last_packet  # last packet, no dock
    assert finished.payload["duration_ms"] == last_packet - _T0


# ===================================================================== #
# Family 5 — the stamp round-trips snapshot/restore; legacy tolerated     #
# ===================================================================== #


def test_stamp_survives_snapshot_and_restore_then_closes_at_dock() -> None:
    """Dock at Y stamps the live run; snapshot → restore into a fresh
    tracker preserves the stamp (it rides the `current_run` deepcopy); a
    post-restore `tick()` sustained-closes at exactly Y. Better than the
    old behaviour: a dock at T before a restart now ends the session at T.
    """
    src = RunTracker()
    _seed_running(src)
    dock_y = _T0 + 3_000
    src.process_vehicle_state(VS_DOCKED_IDLE, time_ms=dock_y)
    assert src.current_run["dock_arrival_time"] == dock_y

    snap = src.snapshot()
    assert snap["current_run"]["dock_arrival_time"] == dock_y  # rides deepcopy

    clk = _FakeClock()
    dst = RunTracker(clock=clk)
    assert dst.restore(snap) is True
    assert dst.current_run["dock_arrival_time"] == dock_y

    dst.tick()  # arm
    clk.advance(INTERRUPT_SUSTAIN_SECONDS + 1)
    closed = dst.tick()
    assert [e.kind for e in closed] == [EVENT_RUN_FINISHED]
    p = closed[0].payload
    assert p["result"] == RESULT_INTERRUPTED  # mp=50 < threshold
    assert p["end_time"] == dock_y  # dock arrival survived the snapshot
    assert p["duration_ms"] == dock_y - _T0


def test_legacy_snapshot_without_key_falls_back_to_last_time_no_raise() -> None:
    """A pre-HARD-19 run dict simply lacks `dock_arrival_time`; `_close_run`
    falls back to the last packet cursor and never raises. No
    `SNAPSHOT_VERSION` bump — absence is tolerated by construction.
    """
    last_packet = _T0 + 100_000
    snap = {
        "version": SNAPSHOT_VERSION,
        "state": STATE_PAUSED_DOCKED,
        "vehicle_state": VS_DOCKED_IDLE,
        "current_run": {
            "start_time": _T0,
            "mow_start_type": 0,
            "wk0": 50.0,
            "sub0": 10.0,
            "last_time": last_packet,
            "last_sub": 60.0,
            "last_wk": 110.0,
            "last_mp": 50,
            "zones": [
                {
                    "boundary_id": 1,
                    "first_time": _T0,
                    "last_time": last_packet,
                    "cmp_max": 5000,
                    "sub_entry": 10.0,
                    "sub_exit": 60.0,
                }
            ],
            # NOTE: no "dock_arrival_time" key — the legacy shape.
        },
        "last_accepted_wk": 110.0,
        "last_accepted_time_ms": last_packet,
        "drops": {"pending_reset_holds": 0},
        "counters": {},
    }
    clk = _FakeClock()
    tracker = RunTracker(clock=clk)
    assert tracker.restore(snap) is True
    assert "dock_arrival_time" not in tracker.current_run

    tracker.tick()  # arm
    clk.advance(INTERRUPT_SUSTAIN_SECONDS + 1)
    closed = tracker.tick()
    assert [e.kind for e in closed] == [EVENT_RUN_FINISHED]
    p = closed[0].payload
    assert p["result"] == RESULT_INTERRUPTED
    assert p["end_time"] == last_packet  # last_time fallback, no raise
    assert p["duration_ms"] == last_packet - _T0


# ===================================================================== #
# Family 6 — BUG-09 dock-first-then-mp=100: the max(stamp, packet) floor  #
# ===================================================================== #


def test_bug09_dock_first_then_mp100_end_is_floored_to_the_packet() -> None:
    """The one shape that exercises the floor. The robot docks at Y with
    `mp < 100` (holds), then the completing type-2 arrives *after* the dock
    arrival. It resumes-and-completes in the same call; its packet time
    postdates the dock, so `end = max(dock, packet) = packet` — a session
    never ends before its last accepted data (§2b floor, #89).
    """
    tracker = RunTracker()
    _seed_running(tracker)

    dock_y = _T0 + 3_000
    tracker.process_vehicle_state(VS_DOCKED_CHARGING, time_ms=dock_y)  # holds, mp<100
    assert tracker.state == STATE_PAUSED_DOCKED
    assert tracker.current_run["dock_arrival_time"] == dock_y

    packet = _T0 + 4_000  # completing type-2 postdates the dock arrival
    events = _feed_t2(tracker, mp=100, cmp=10000, sub=40.0, boundary=1, time=packet)
    assert [e.kind for e in events] == [EVENT_RUN_FINISHED]
    p = events[0].payload
    assert p["result"] == RESULT_COMPLETED
    assert p["end_time"] == packet
    assert p["end_time"] == max(dock_y, packet)  # the floor, made explicit
    assert p["duration_ms"] == packet - _T0


# ===================================================================== #
# Family 7 — non-perturbation                                            #
# ===================================================================== #


def test_provisional_abort_end_unchanged_and_stamp_coincides() -> None:
    """HARD-18's aborted-start entry is untouched: its `last_time` IS the
    dock entry, so the new stamp coincides and `max(stamp, last_time)`
    yields the same end. §1c/§1d mechanics are not refactored onto the new
    field (one change per PR) — the values simply agree.
    """
    clk = _FakeClock()
    tracker = RunTracker(clock=clk)
    tracker.process_vehicle_state(VS_MOWING, time_ms=_T0)  # provisional
    end = _T0 + 90_000
    tracker.process_vehicle_state(VS_DOCKED_IDLE, time_ms=end)  # abort dock
    assert tracker.current_run["dock_arrival_time"] == end
    assert tracker.current_run["last_time"] == end  # coincide

    clk.advance(INTERRUPT_SUSTAIN_SECONDS + 1)
    closed = tracker.tick()
    p = closed[0].payload
    assert p["result"] == RESULT_INTERRUPTED
    assert p["end_time"] == end  # unchanged from HARD-18
    assert p["duration_ms"] == end - _T0
    assert p["session_area"] is None
    assert p["zones"] == []
    assert p["mow_start_type"] is None
    assert tracker.counters["aborted_starts_committed"] == 1


def test_stamp_does_not_perturb_area_zones_or_label() -> None:
    """The stamp moves only `end_time` / `duration_ms`. `session_area`
    (sub-based), `zones`, and the completion *label* are identical whether
    the dock edge carried a `time_ms` (stamped) or not (fallback).
    """

    def _completed_run(dock_time_ms: int | None) -> dict:
        tracker = RunTracker()
        _seed_running(tracker)
        _feed_t2(tracker, mp=100, cmp=10000, sub=40.0, boundary=1, time=_T0 + 2_000)
        closed = tracker.process_vehicle_state(VS_DOCKED_IDLE, time_ms=dock_time_ms)
        assert [e.kind for e in closed] == [EVENT_RUN_FINISHED]
        return closed[0].payload

    dock_y = _T0 + 5_000
    with_stamp = _completed_run(dock_y)
    without_stamp = _completed_run(None)  # legacy caller, no time_ms → no stamp

    # Only the end differs.
    assert with_stamp["end_time"] == dock_y
    assert without_stamp["end_time"] == _T0 + 2_000  # last packet cursor

    # Everything else is byte-identical.
    assert with_stamp["result"] == without_stamp["result"] == RESULT_COMPLETED
    assert with_stamp["session_area"] == without_stamp["session_area"] == 20.0
    assert with_stamp["zones"] == without_stamp["zones"]
    assert with_stamp["mow_start_type"] == without_stamp["mow_start_type"]


def test_last_sub_gating_baseline_unaffected_by_the_stamp() -> None:
    """The stamp does not mutate `last_time`/`last_sub`: the post-close
    gating baseline (`last_sub`) the next-session strict-progress guard
    reads is exactly the last accepted packet's `sub`, not perturbed by the
    dock edge.
    """
    tracker = RunTracker()
    _seed_running(tracker)
    _feed_t2(tracker, mp=100, cmp=10000, sub=40.0, boundary=1, time=_T0 + 2_000)
    tracker.process_vehicle_state(VS_DOCKED_IDLE, time_ms=_T0 + 5_000)  # completes
    assert tracker.state == STATE_IDLE
    # The closed run is kept as the post-close reference; last_sub is the
    # packet cursor, not touched by the dock stamp.
    assert tracker.current_run["last_sub"] == 40.0
    assert tracker.current_run["last_time"] == _T0 + 2_000  # packet, not dock


# ===================================================================== #
# Pin O2 — coordinator-level: the /state topic is not a tracker input     #
# ===================================================================== #


def _make_coordinator():
    """Minimal `__new__`-built coordinator — mirrors the FEAT-05c helper."""
    from custom_components.navimow.coordinator import NavimowCoordinator
    from custom_components.navimow.zone_registry import ZoneRegistry

    coord = NavimowCoordinator.__new__(NavimowCoordinator)
    coord.hass = MagicMock()
    coord.hass.bus.async_fire = MagicMock()
    coord.hass.async_create_task = MagicMock()
    coord.logger = MagicMock()
    coord.name = "test"
    coord.update_interval = None
    coord.config_entry = MagicMock()
    device = MagicMock()
    device.id = _DEVICE_ID
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
    coord.zone_registry = ZoneRegistry()
    coord._store = None
    coord._last_store_save_monotonic = 0.0
    coord._build_data = MagicMock(return_value={})
    coord.async_set_updated_data = MagicMock()
    return coord


def _feed_type2(coord, item) -> None:
    parsed = parse_location_type_2(item)
    coord._forward_run_events(coord.run_tracker.process_type2(parsed))


def test_pin_o2_state_topic_docked_writes_nothing_then_type1_ends_at_dock() -> None:
    """Pin O2 (`/state` first — independence). A `/state`-topic mower
    activity change to `docked`, delivered BEFORE the dock-entry type-1,
    writes nothing into the tracker: no stamp, no state transition, no
    close. Only the `/location` type-1 edge stamps — the subsequent type-1
    then produces the same strict `end_time == Y`. Pins that the stamp's
    source is the type-1 edge and only the type-1 edge, so a future
    refactor that moves it to the entity/`/state` layer fails loudly.
    """
    from mower_sdk.models import DeviceStateMessage

    coord = _make_coordinator()
    t = 1_000_000_000_000
    _feed_type2(
        coord,
        {
            "type": 2,
            "currentMowBoundary": 1,
            "currentMowProgress": 5000,
            "mowingPercentage": 50,
            "subtotalArea": "20.0",
            "mowingWeekArea": "300.0",
            "time": t + 1_000,
        },
    )
    _feed_type2(
        coord,
        {
            "type": 2,
            "currentMowBoundary": 1,
            "currentMowProgress": 10000,
            "mowingPercentage": 100,
            "subtotalArea": "40.0",
            "mowingWeekArea": "320.0",
            "time": t + 2_000,
        },
    )
    coord.vehicle_state = VS_MOWING  # off-dock; the dock is a vs change
    tr = coord.run_tracker
    assert tr.state == STATE_RUNNING
    assert tr.current_run.get("dock_arrival_time") is None

    # (O2) /state → docked FIRST. The /state handler is not a tracker input.
    coord._handle_state(
        DeviceStateMessage(
            device_id=_DEVICE_ID, timestamp=t + 3_000, state="isDocked", battery=80
        )
    )
    assert tr.state == STATE_RUNNING  # no transition
    assert tr.current_run is not None  # not closed
    assert tr.current_run.get("dock_arrival_time") is None  # no stamp from /state

    # THEN the /location type-1 dock edge — the sole stamp source — closes
    # the run at the dock arrival Y (the /state message changed nothing).
    dock_y = t + 5_000
    with patch("custom_components.navimow.coordinator.async_dispatcher_send"):
        coord._handle_location_position(
            {
                "type": 1,
                "postureX": "0.0",
                "postureY": "0.0",
                "vehicleState": VS_DOCKED_IDLE,
                "time": dock_y,
            }
        )
    assert tr.state == STATE_IDLE
    assert coord.last_finished_run is not None
    assert coord.last_finished_run["result"] == RESULT_COMPLETED
    assert coord.last_finished_run["end_time"] == dock_y  # strict dock arrival
    assert coord.last_finished_run["duration_ms"] == dock_y - (t + 1_000)
