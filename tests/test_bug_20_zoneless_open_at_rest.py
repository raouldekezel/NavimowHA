"""BUG-20 (#142) — a zoneless type-2 never opens a run from IDLE.

While the mower sits on its dock at night, the firmware emits a task-end
marker that names no zone (`boundary = 0 ∧ mp = 100`, zone progress 0). From
IDLE it used to open a run (`is_reset` on its near-zero `sub`, or strict
progress on a higher one) and, the robot being docked with `mp = 100`, close
it in the same call: a 0-second `completed` history row with `zones = []`
(2026-09-15 03:19, 2026-09-25 04:47, 2026-10-19 06:05 local).

The BUG-15 sentinel drop does not match it (`mp ≠ 0`), nor does the
BUG-17/19 vestige gate (`cmp < 10000`). Inside an open run the marker must
keep flowing — it carries the completion and the final area.

Observable-only (tracker API, `caplog`); no source introspection.
"""

from __future__ import annotations

import copy
import logging

from custom_components.navimow.location import parse_location_type_2
from custom_components.navimow.run_tracker import (
    EVENT_RUN_FINISHED,
    EVENT_RUN_STARTED,
    RESULT_COMPLETED,
    STATE_IDLE,
    STATE_RUNNING,
    VS_DOCKED_CHARGING,
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


def _marker(*, sub: float = 0.0, wk: float = 363.71, time: int) -> dict:
    """The zoneless task-end marker: no boundary, `mp = 100`, no zone
    progress."""
    return _t2(boundary=0, cmp=0, mp=100, sub=sub, wk=wk, action=-1, time=time)


def _docked_after_close() -> RunTracker:
    """A tracker at rest after a completed two-zone mow, docked and
    charged — the 2026-10-18 → 10-19 pre-state."""
    tr = RunTracker()
    tr.process_vehicle_state(VS_MOWING, time_ms=_T0)
    tr.process_type2(
        _t2(boundary=1, mp=40, cmp=5000, sub=120.0, wk=120.0, time=_T0 + 1_000)
    )
    tr.process_type2(
        _t2(boundary=3, mp=100, cmp=10000, sub=363.71, wk=363.71, time=_T0 + 2_000)
    )
    ev = tr.process_vehicle_state(VS_DOCKED_CHARGING, time_ms=_T0 + 3_000)
    assert [e.kind for e in ev] == [EVENT_RUN_FINISHED]
    tr.process_vehicle_state(VS_DOCKED_IDLE, time_ms=_T0 + 4_000)
    assert tr.state == STATE_IDLE
    assert tr.current_run["last_sub"] == 363.71
    return tr


# ===================================================================== #
# The ghost row is not minted                                            #
# ===================================================================== #


def test_night_marker_at_rest_opens_nothing(caplog) -> None:
    """The 2026-10-19 06:05 ghost, replayed: zero `sub` (the `is_reset`
    path). No event, still IDLE, closed reference untouched, one DEBUG line
    carrying the payload."""
    tr = _docked_after_close()
    ref_before = copy.deepcopy(tr.current_run)

    with caplog.at_level(logging.DEBUG, logger=_LOGGER_NAME):
        ev = tr.process_type2(_marker(time=_T0 + 36_000_000))

    assert ev == []
    assert tr.state == STATE_IDLE
    assert tr.current_run == ref_before
    refused = [
        r.getMessage()
        for r in caplog.records
        if "zoneless type-2 refused" in r.getMessage()
    ]
    assert len(refused) == 1
    assert "mp=100" in refused[0] and "boundary=0" in refused[0]


def test_night_marker_with_higher_sub_opens_nothing() -> None:
    """Same marker carrying a `sub` above the closed run's (the
    strict-progress path) is refused too."""
    tr = _docked_after_close()
    ref_before = copy.deepcopy(tr.current_run)

    ev = tr.process_type2(_marker(sub=400.0, wk=400.0, time=_T0 + 36_000_000))
    assert ev == []
    assert tr.state == STATE_IDLE
    assert tr.current_run == ref_before


def test_marker_at_cold_boot_opens_nothing() -> None:
    tr = RunTracker()
    tr.process_vehicle_state(VS_DOCKED_IDLE, time_ms=_T0)
    assert tr.process_type2(_marker(time=_T0 + 1_000)) == []
    assert tr.state == STATE_IDLE
    assert tr.current_run is None


def test_refused_marker_does_not_advance_cursors() -> None:
    """The refusal is inert: the next genuine session is still judged
    against the closed run, not against the marker."""
    tr = _docked_after_close()
    wk_before = tr.snapshot()["last_accepted_wk"]
    time_before = tr.snapshot()["last_accepted_time_ms"]

    tr.process_type2(_marker(wk=0.0, time=_T0 + 36_000_000))
    assert tr.snapshot()["last_accepted_wk"] == wk_before
    assert tr.snapshot()["last_accepted_time_ms"] == time_before


# ===================================================================== #
# What must still work                                                   #
# ===================================================================== #


def test_marker_inside_open_run_still_completes_it() -> None:
    """In an open run the zoneless marker is the completion signal: it
    advances `last_mp` and, docked, closes the run `completed` with its
    zones intact."""
    tr = RunTracker()
    tr.process_vehicle_state(VS_MOWING, time_ms=_T0)
    tr.process_type2(_t2(boundary=1, mp=80, cmp=8000, sub=200.0, time=_T0 + 1_000))
    tr.process_vehicle_state(VS_DOCKED_CHARGING, time_ms=_T0 + 2_000)

    ev = tr.process_type2(_marker(sub=226.68, time=_T0 + 3_000))
    finished = [e for e in ev if e.kind == EVENT_RUN_FINISHED]
    assert len(finished) == 1
    assert finished[0].payload["result"] == RESULT_COMPLETED
    assert [z["boundary_id"] for z in finished[0].payload["zones"]] == [1]
    assert tr.state == STATE_IDLE


def test_missed_activation_edge_opens_on_first_zone_packet() -> None:
    """Recovery path (no `vs = 4` seen, e.g. HA restarted mid-mow): a
    zoneless packet is skipped, the next zone-carrying packet opens the run
    and anchors `start_time`."""
    tr = _docked_after_close()

    assert tr.process_type2(_marker(sub=0.0, time=_T0 + 50_000)) == []
    ev = tr.process_type2(
        _t2(boundary=1, mp=2, cmp=150, sub=2.5, wk=2.5, time=_T0 + 110_000)
    )
    assert [e.kind for e in ev] == [EVENT_RUN_STARTED]
    assert tr.state == STATE_RUNNING
    assert tr.current_run["start_time"] == _T0 + 110_000
    assert [z["boundary_id"] for z in tr.current_run["zones"]] == [1]


def test_absent_boundary_fails_open() -> None:
    """A packet without the boundary field is not read as `boundary = 0`:
    it keeps opening from cold IDLE, like before."""
    tr = RunTracker()
    ev = tr.process_type2(
        parse_location_type_2(
            {
                "type": 2,
                "mowingPercentage": 40,
                "subtotalArea": "10.0",
                "mowingWeekArea": "10.0",
                "time": _T0,
            }
        )
    )
    assert [e.kind for e in ev] == [EVENT_RUN_STARTED]
    assert tr.state == STATE_RUNNING
