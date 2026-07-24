"""BUG-15 (#90) — drop the all-zero session-init sentinel.

The firmware emits a zero-payload `type-2` at task start
(`currentMowBoundary=0 ∧ mowingPercentage=0 ∧ currentMowProgress=0 ∧
subtotalArea=0.0 ∧ action=-1`) ~15 s AFTER `vs = 4` (validated 4/4 in the
2026-05 corpus, 3/3 in the post-HARD-18 window). It carries no zone, no
progress, and nothing the physical `vs = 4` edge (HARD-18) does not already
give — so a real session-init always arrives while the tracker is already
RUNNING (the `vs = 4` provisional is open). Left in, the sentinel is pure
liability:

- from `STATE_IDLE` it opens a phantom run (this issue), which then lingers
  (opened RUNNING while the robot is already docked → no `RUNNING →
  PAUSED_DOCKED` edge arms the sustained timer; observed ~10.9 h on
  2026-07-24) and can engulf the next real mow;
- in `STATE_RUNNING` on a seeded run (`last_sub > 0`) its `sub = 0` trips
  `is_reset` → close + reopen, splitting a real run.

Fix: drop the all-zero shape (a sibling of the vestige guard) in all states.
Signature is the all-zero shape, NOT bare `boundary = 0` — a
`boundary = 0 ∧ mp = 100` packet is a task-*end* marker (completion + final
area) and must survive.

Observable-only (tracker API, `caplog`); no source introspection.
"""

from __future__ import annotations

import logging

from custom_components.navimow.location import parse_location_type_2
from custom_components.navimow.run_tracker import (
    EVENT_RUN_FINISHED,
    EVENT_RUN_STARTED,
    STATE_IDLE,
    STATE_RUNNING,
    VS_DOCKED_IDLE,
    VS_MOWING,
    RunTracker,
)

_T0 = 5_000_000_000_000
_LOGGER_NAME = "custom_components.navimow.run_tracker"


def _t2(
    *,
    boundary=1,
    cmp=5000,
    mp=50,
    sub=20.0,
    wk=400.0,
    action=8,
    mst=0,
    time: int,
) -> dict:
    return parse_location_type_2(
        {
            "type": 2,
            "currentMowBoundary": boundary,
            "currentMowProgress": cmp,
            "mowingPercentage": mp,
            "subtotalArea": str(sub),
            "mowingWeekArea": str(wk),
            "action": action,
            "mowStartType": mst,
            "time": time,
        }
    )


_SENTINEL = dict(boundary=0, cmp=0, mp=0, sub=0.0, action=-1, mst=0)


def _idle_with_reference() -> RunTracker:
    """A tracker at rest in IDLE with a seeded post-close reference — the
    dominant nocturnal state (the 2026-07-24 phantom's pre-state)."""
    tr = RunTracker()
    tr.process_vehicle_state(VS_MOWING, time_ms=_T0)
    tr.process_type2(_t2(boundary=1, mp=50, sub=44.0, cmp=5000, time=_T0 + 1_000))
    tr.process_type2(_t2(boundary=1, mp=100, sub=50.0, cmp=10000, time=_T0 + 2_000))
    tr.process_vehicle_state(VS_DOCKED_IDLE, time_ms=_T0 + 3_000)  # completes → IDLE
    assert tr.state == STATE_IDLE
    assert tr.current_run["last_sub"] == 50.0  # seeded reference
    return tr


# ===================================================================== #
# The core fix — the phantom is not opened from IDLE                      #
# ===================================================================== #


def test_sentinel_from_idle_with_reference_opens_nothing() -> None:
    """The 2026-07-24 02:43 phantom, replayed. From IDLE with a seeded
    reference, the all-zero sentinel is dropped — no `run_started`, the
    tracker stays IDLE, the closed reference is untouched."""
    tr = _idle_with_reference()
    ref_before = dict(tr.current_run)

    ev = tr.process_type2(_t2(time=_T0 + 100_000, **_SENTINEL))
    assert ev == []
    assert tr.state == STATE_IDLE
    assert tr.current_run == ref_before  # reference not mutated


def test_sentinel_from_cold_idle_opens_nothing() -> None:
    """Cold boot (IDLE, no reference): the sentinel still opens nothing."""
    tr = RunTracker()
    ev = tr.process_type2(_t2(time=_T0, **_SENTINEL))
    assert ev == []
    assert tr.state == STATE_IDLE
    assert tr.current_run is None


# ===================================================================== #
# What must still work                                                   #
# ===================================================================== #


def test_real_boundary_packet_from_cold_idle_still_opens() -> None:
    """Mid-mow recovery preserved: a real boundary-carrying packet from IDLE
    (cold boot / a type-2 before the first `vs = 4` after a reconnect) still
    opens a run — only the all-zero shape is dropped."""
    tr = RunTracker()
    ev = tr.process_type2(_t2(boundary=1, mp=5, cmp=104, sub=2.49, time=_T0))
    assert [e.kind for e in ev] == [EVENT_RUN_STARTED]
    assert tr.state == STATE_RUNNING
    assert tr.current_run["zones"][0]["boundary_id"] == 1


def test_task_end_boundary_zero_mp_100_is_not_dropped() -> None:
    """`boundary = 0 ∧ mp = 100` is a task-END marker, NOT the all-zero
    sentinel — it must survive: it carries the completion signal (and the
    final area). Fed to an open run it advances `last_mp` to 100."""
    tr = RunTracker()
    tr.process_vehicle_state(VS_MOWING, time_ms=_T0)
    tr.process_type2(_t2(boundary=1, mp=50, sub=44.0, cmp=5000, time=_T0 + 1_000))
    assert tr.current_run["last_mp"] == 50

    # task-end packet: boundary=0 but mp=100 with a real final area.
    tr.process_type2(_t2(boundary=0, mp=100, cmp=0, sub=226.68, time=_T0 + 2_000))
    assert tr.current_run["last_mp"] == 100  # processed, not dropped
    assert tr.current_run["last_sub"] == 226.68


def test_sentinel_in_running_does_not_split_a_seeded_run() -> None:
    """In RUNNING on a seeded run (`last_sub = 44`), the sentinel's `sub = 0`
    used to trip `is_reset` → close + reopen (split). Dropped now: the run is
    untouched, no `run_finished`, same `start_time`."""
    tr = RunTracker()
    tr.process_vehicle_state(VS_MOWING, time_ms=_T0)
    tr.process_type2(_t2(boundary=1, mp=50, sub=44.0, cmp=5000, time=_T0 + 1_000))
    assert tr.state == STATE_RUNNING
    start = tr.current_run["start_time"]

    ev = tr.process_type2(_t2(time=_T0 + 2_000, **_SENTINEL))
    assert [e for e in ev if e.kind in (EVENT_RUN_FINISHED, EVENT_RUN_STARTED)] == []
    assert tr.state == STATE_RUNNING
    assert tr.current_run["start_time"] == start  # same run, not split
    assert tr.current_run["last_sub"] == 44.0  # accumulator not reset to 0


# ===================================================================== #
# Observability                                                          #
# ===================================================================== #


def test_dropped_sentinel_emits_debug(caplog) -> None:
    tr = _idle_with_reference()
    with caplog.at_level(logging.DEBUG, logger=_LOGGER_NAME):
        tr.process_type2(_t2(time=_T0 + 100_000, **_SENTINEL))
    assert any(
        "all-zero session-init sentinel dropped" in r.message for r in caplog.records
    )
