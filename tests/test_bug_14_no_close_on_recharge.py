"""BUG-14 (#89) — a mid-run recharge dock at `mp = 99` must not close.

Operator-observed on 2026-07-09: a single logical mowing session
(mow → return to dock for recharge at 15 % battery → recharge to 95 %
→ resume → finish) was split into two `completed` runs because
BUG-09's completion criterion fired on `mp = 99 ∧ vs = 2` when the
robot docked to recharge.

Root cause: the firmware's `mp` field is task-scoped (SPIKE-02) and
plateaus at 99 for some tasks; a robot returning to dock at `mp = 99`
with a low battery is indistinguishable from one returning at `mp = 99`
because the task is finished — on the `mp` signal alone.

Fix in two moves:

1. Raise `MP_COMPLETION_THRESHOLD` from 99 to 100 so the fast path
   never fires on `mp = 99` alone.
2. Add a refined branch: close on `mp ≥ 99 ∧ zones[-1].cmp_max ≥
   10000`. The zone-scoped `cmp` reaching 10000 is an independent
   firmware confirmation that the last active zone is 100 % mowed,
   which distinguishes a real finish from a recharge return.

The tests below verify the *positive* behaviour: a single logical
session is preserved through an intra-run recharge cycle, closed as
`completed` when the zone-completion signal arrives. Residual mis-
labels (`interrupted` on `mp = 99` tasks that never reach `cmp = 10000`)
are covered in `test_bug_09_mp_completion_on_dock.py`.
"""

from __future__ import annotations

from custom_components.navimow.location import parse_location_type_2
from custom_components.navimow.run_tracker import (
    EVENT_RUN_FINISHED,
    EVENT_RUN_STARTED,
    STATE_IDLE,
    STATE_PAUSED_DOCKED,
    STATE_RUNNING,
    VS_DOCKED_CHARGING,
    VS_MOWING,
    VS_RETURNING,
    RunTracker,
)


def _process(tracker: RunTracker, item: dict) -> list:
    return tracker.process_type2(parse_location_type_2(item))


def _pkt(
    mp: int,
    sub: float,
    *,
    cmp: int = 0,
    wk: float | None = None,
    boundary: int = 1,
    t: int,
    mst: int = 0,
) -> dict:
    return {
        "type": 2,
        "currentMowBoundary": boundary,
        "mowingPercentage": mp,
        "currentMowProgress": cmp,
        "subtotalArea": str(sub),
        "mowingWeekArea": str(wk if wk is not None else sub),
        "mowStartType": mst,
        "time": t,
    }


# --------------------------------------------------------------------- #
# 1. Baseline pathology — mp=99 + recharge dock must NOT close          #
# --------------------------------------------------------------------- #


def test_mp_99_recharge_dock_holds_paused_docked() -> None:
    """The trigger of the 2026-07-09 pathology. The BUG-09 predicate
    `mp = 99 ∧ vs = 2` fired a `completed` close pre-BUG-14; the run
    was actually a recharge return. `cmp` below 10000 is the
    discriminator: the zone is not yet finished, so this is not a
    real completion.
    """
    tracker = RunTracker()
    _process(tracker, _pkt(mp=99, sub=200.0, cmp=9901, t=1_000))
    events = tracker.process_vehicle_state(VS_DOCKED_CHARGING)
    assert [e for e in events if e.kind == EVENT_RUN_FINISHED] == []
    assert tracker.state == STATE_PAUSED_DOCKED


# --------------------------------------------------------------------- #
# 2. Full 2026-07-09 timeline — one session, not two                    #
# --------------------------------------------------------------------- #


def test_2026_07_09_day_closes_completed_via_cmp_10000() -> None:
    """Reproduces the operator's 2026-07-09 timeline literally:

    - Big morning run on Prunier (boundary 1): mp climbs to 99, cmp
      peaks at 9901 (99.01 % of the zone) — battery drops to 15 %,
      robot returns to dock to recharge.
    - Recharge for ~1 h 25 (vs=2 charging).
    - Robot leaves dock at 12:50 CEST, resumes mowing (same run: fresh
      type-2 with strict progress on `sub`).
    - Mini-run type-2 packet at 12:51:46 UTC: `boundary=1, mp=99,
      cmp=10000, sub=231.77` — the firmware confirms Prunier is now
      100 % mowed. `mp` stays at 99 (task-scoped; per SPIKE-02 it does
      not always emit 100 even on completion).
    - Robot returns to dock at 12:52 CEST → BUG-14 refined rule fires
      on `mp = 99 ∧ zones[-1].cmp_max = 10000 ∧ vs = 2` → CLOSE
      COMPLETED.

    Invariants:
    - Exactly one run_started + one run_finished — the whole day is one
      session, not two.
    - Result label is `completed` (BUG-14 refinement), not
      `interrupted`.
    - `session_area` covers the whole day: sub grows from `sub₀`
      (arbitrary mid-mow anchor) to 231.77 on the last packet.
    """
    tracker = RunTracker()

    all_events: list = []
    # -- Morning session: mow progresses to mp=99, cmp=9901 (99.01 %) -- #
    all_events += _process(tracker, _pkt(mp=50, sub=100.0, cmp=5000, t=1_000))
    all_events += _process(tracker, _pkt(mp=99, sub=230.2, cmp=9901, t=2_000))
    assert tracker.state == STATE_RUNNING

    # -- Recharge return: vs=5 → vs=2 (battery ~15 %) -------------------- #
    all_events += tracker.process_vehicle_state(VS_RETURNING)
    all_events += tracker.process_vehicle_state(VS_DOCKED_CHARGING)
    assert tracker.state == STATE_PAUSED_DOCKED, "mp=99 + cmp<10000 must hold"
    assert [e for e in all_events if e.kind == EVENT_RUN_FINISHED] == []

    # -- Robot leaves dock, resumes mowing (same run continues) --------- #
    all_events += tracker.process_vehicle_state(VS_MOWING)
    # The mini-run packet: `sub` advances (strict progress → resume),
    # cmp finally reaches 10000, mp stays at 99.
    all_events += _process(tracker, _pkt(mp=99, sub=231.77, cmp=10000, t=3_000))
    assert tracker.state == STATE_RUNNING, "recharge resume must not spawn a new run"
    # zones[-1].cmp_max was updated in place: same boundary as before.
    assert tracker.current_run["zones"][-1]["cmp_max"] == 10000

    # -- Final dock arrival at 12:52 CEST ------------------------------- #
    all_events += tracker.process_vehicle_state(VS_RETURNING)
    all_events += tracker.process_vehicle_state(VS_DOCKED_CHARGING)
    assert tracker.state == STATE_IDLE, "BUG-14 cmp rule must fire"

    # Exactly one open and one close over the full cycle.
    started = [e for e in all_events if e.kind == EVENT_RUN_STARTED]
    finished = [e for e in all_events if e.kind == EVENT_RUN_FINISHED]
    assert len(started) == 1
    assert len(finished) == 1
    assert finished[0].payload["result"] == "completed"
    # Session area covers the whole day (231.77 − 100.0).
    assert abs(finished[0].payload["session_area"] - 131.77) < 1e-9


def test_full_recharge_cycle_with_mp_100_finish_closes_completed() -> None:
    """The alternate happy path: some firmware tasks do emit `mp = 100`
    on the resumed segment (2026-05-25 and 2026-07-04 afternoon are
    the two documented occurrences). The BUG-14 fast path fires on
    the `mp = 100 ∧ vs ∈ DOCK_EVIDENCE {1,2}` branch — cmp not even required.
    Same invariant: one session, one close, `completed`.
    """
    tracker = RunTracker()

    all_events: list = []
    all_events += _process(tracker, _pkt(mp=50, sub=100.0, cmp=5000, t=1_000))
    all_events += _process(tracker, _pkt(mp=99, sub=200.0, cmp=9800, t=2_000))
    all_events += tracker.process_vehicle_state(VS_RETURNING)
    all_events += tracker.process_vehicle_state(VS_DOCKED_CHARGING)
    assert tracker.state == STATE_PAUSED_DOCKED

    all_events += tracker.process_vehicle_state(VS_MOWING)
    all_events += _process(tracker, _pkt(mp=99, sub=205.0, cmp=9820, t=3_000))
    all_events += _process(tracker, _pkt(mp=100, sub=232.0, cmp=10000, t=4_000))
    assert tracker.state == STATE_RUNNING

    all_events += tracker.process_vehicle_state(VS_RETURNING)
    all_events += tracker.process_vehicle_state(VS_DOCKED_CHARGING)
    assert tracker.state == STATE_IDLE

    started = [e for e in all_events if e.kind == EVENT_RUN_STARTED]
    finished = [e for e in all_events if e.kind == EVENT_RUN_FINISHED]
    assert len(started) == 1
    assert len(finished) == 1
    assert finished[0].payload["result"] == "completed"
    assert finished[0].payload["session_area"] == 132.0


# --------------------------------------------------------------------- #
# 3. Fresh type-2 during the pause is a resume, not a new session       #
# --------------------------------------------------------------------- #


def test_fresh_type2_at_mp_99_during_recharge_pause_resumes_run() -> None:
    """A type-2 arriving while `PAUSED_DOCKED + vs=2` at `mp = 99`
    must be treated as a continuation of the open run (RUNNING resume),
    not as a post-close new session. The only reason the run could
    have been closed by that point is a pre-BUG-14 fast path — the
    regression is exactly what this test guards against.
    """
    tracker = RunTracker()
    _process(tracker, _pkt(mp=99, sub=200.0, t=1_000))
    tracker.process_vehicle_state(VS_DOCKED_CHARGING)
    assert tracker.state == STATE_PAUSED_DOCKED

    # The robot leaves the dock (departure evidence vs=4, HARD-19 §3 #120),
    # then a follow-up type-2 with strict progress on `sub` (the first
    # packet emitted after leaving the dock) resumes the same run.
    tracker.process_vehicle_state(VS_MOWING)
    events = _process(tracker, _pkt(mp=99, sub=200.5, t=2_000))
    assert tracker.state == STATE_RUNNING
    # No close event fired anywhere in this continuation.
    assert [e for e in events if e.kind == EVENT_RUN_FINISHED] == []


# --------------------------------------------------------------------- #
# 4. Multiple recharge cycles are all held together                     #
# --------------------------------------------------------------------- #


def test_two_recharge_cycles_still_one_session() -> None:
    """A very-long-grass day where the tondeuse has to recharge twice
    at `mp = 99` before finally reaching `mp = 100`. Session must
    still be one run, closed once at the end.
    """
    tracker = RunTracker()
    all_events: list = []
    all_events += _process(tracker, _pkt(mp=50, sub=100.0, t=1_000))

    # Cycle A: mp → 99, dock charging, resume.
    all_events += _process(tracker, _pkt(mp=99, sub=200.0, t=2_000))
    all_events += tracker.process_vehicle_state(VS_DOCKED_CHARGING)
    all_events += tracker.process_vehicle_state(VS_MOWING)
    all_events += _process(tracker, _pkt(mp=99, sub=205.0, t=3_000))
    assert tracker.state == STATE_RUNNING

    # Cycle B: mp still 99 later, dock charging again, resume.
    all_events += _process(tracker, _pkt(mp=99, sub=220.0, t=4_000))
    all_events += tracker.process_vehicle_state(VS_DOCKED_CHARGING)
    all_events += tracker.process_vehicle_state(VS_MOWING)
    all_events += _process(tracker, _pkt(mp=99, sub=225.0, t=5_000))
    assert tracker.state == STATE_RUNNING

    # Finally reaches mp = 100, docks, closes.
    all_events += _process(tracker, _pkt(mp=100, sub=240.0, t=6_000))
    all_events += tracker.process_vehicle_state(VS_DOCKED_CHARGING)
    assert tracker.state == STATE_IDLE

    started = [e for e in all_events if e.kind == EVENT_RUN_STARTED]
    finished = [e for e in all_events if e.kind == EVENT_RUN_FINISHED]
    assert len(started) == 1
    assert len(finished) == 1
    assert finished[0].payload["result"] == "completed"
