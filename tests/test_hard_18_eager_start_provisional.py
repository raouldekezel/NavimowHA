"""HARD-18 — eager session start via a provisional run (#117).

The tracker used to open a run only from the first mowing-task type-2,
which the firmware emits ~3 min into a mow (dock exit + navigation). For
that whole gap `sensor.*_current_run_state` (`_etat_de_la_tonte`) held
the previous close's label even though the operator had just pressed run.

Settled design (issue body, Variant O; implementation brief Fable
2026-07-21): on the `{IDLE, COMPLETED, INTERRUPTED} → vs=4` edge the
tracker opens a **provisional** run immediately — `state = RUNNING`, all
baseline anchors `None`, `zones == []`, `start_time` = the type-1
activation `time`, an explicit `provisional` flag. The first accepted
type-2 seeds the anchors and flips `provisional` off (`start_time`
keeps the activation anchor). If the robot docks before any type-2
seeds it, the sustained-dock timer commits a minimal `interrupted`
history entry (an aborted start is a session too).

Tests map to the brief's §5 plan (observable behaviour only — the
tracker API, `caplog`, snapshot/restore; no source introspection).
"""

from __future__ import annotations

import logging

import pytest

from custom_components.navimow.location import parse_location_type_2
from custom_components.navimow.run_tracker import (
    EVENT_RUN_FINISHED,
    EVENT_RUN_STARTED,
    INTERRUPT_SUSTAIN_SECONDS,
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
    VS_RETURNING,
    RunTracker,
)

_LOGGER_NAME = "custom_components.navimow.run_tracker"

# Anchor epoch-ms values (arbitrary; only ordering + deltas matter).
_T0 = 5_000_000_000_000


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
    cmp: int = 104,
    mp: int = 3,
    sub: float = 12.24,
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


def _restore_terminal(
    tracker: RunTracker,
    state: str,
    *,
    last_sub: float = 200.0,
    last_mp: int = 100,
) -> None:
    """Put the tracker into a Store-restored terminal state whose closed
    run still populates `current_run` with non-empty zones — the
    operator's dominant real-world entry state (issue body).
    """
    snap = {
        "version": SNAPSHOT_VERSION,
        "state": state,
        "vehicle_state": VS_DOCKED_CHARGING,
        "current_run": {
            "start_time": _T0 - 3_600_000,
            "mow_start_type": 0,
            "wk0": 50.0,
            "sub0": 10.0,
            "last_time": _T0 - 600_000,
            "last_sub": last_sub,
            "last_wk": 50.0 + last_sub,
            "last_mp": last_mp,
            "zones": [
                {
                    "boundary_id": 1,
                    "first_time": _T0 - 3_600_000,
                    "last_time": _T0 - 600_000,
                    "cmp_max": 10000,
                    "sub_entry": 10.0,
                    "sub_exit": last_sub,
                }
            ],
        },
        "last_accepted_wk": 50.0 + last_sub,
        "last_accepted_time_ms": _T0 - 600_000,
        "drops": {"pending_reset_holds": 0},
        "counters": {},
    }
    # HARD-20 (#122): legacy terminal state strings migrate to IDLE on
    # restore; the seeded reference (current_run) survives and keys the
    # post-close gating exactly as before.
    assert tracker.restore(snap) is True
    assert tracker.state == STATE_IDLE


# --------------------------------------------------------------------- #
# 1. vs=4 from every terminal/idle origin opens one provisional run     #
# --------------------------------------------------------------------- #


@pytest.mark.parametrize("origin", [STATE_IDLE, "completed", "interrupted"])
def test_vs4_opens_provisional_run_from_any_origin(origin: str) -> None:
    # HARD-20 (#122): the three resting origins collapsed to IDLE. Fresh
    # IDLE (no reference) and the two legacy terminal snapshots (migrated
    # to IDLE + a seeded reference on restore) must all open a provisional
    # run identically — SPIKE-03 #115's equivalence, now structural.
    tracker = RunTracker()
    if origin == STATE_IDLE:
        assert tracker.state == STATE_IDLE
    else:
        _restore_terminal(tracker, origin)

    events = tracker.process_vehicle_state(VS_MOWING, time_ms=_T0)

    # Exactly one run_started, anchored on the activation time; no
    # mow_start_type is known yet.
    assert [e.kind for e in events] == [EVENT_RUN_STARTED]
    assert events[0].payload["start_time"] == _T0
    assert events[0].payload["mow_start_type"] is None
    # Tracker-level truth: RUNNING + provisional, the closed run (if any)
    # discarded, no zones seeded from the previous session.
    assert tracker.state == STATE_RUNNING
    assert tracker.is_provisional is True
    assert tracker.current_run["start_time"] == _T0
    assert tracker.current_run["zones"] == []
    assert tracker.current_run["last_mp"] is None


def test_app_or_schedule_start_is_indistinguishable_at_tracker() -> None:
    """Sol's test 8: a start from the Navimow app / a schedule reaches
    the tracker as the same vs=4 edge — there is no initiator signal on
    type-1, so behaviour is identical to an HA-initiated start. The
    mow_start_type distinction only lands later, on the seeding type-2.
    """
    a, b = RunTracker(), RunTracker()
    ea = a.process_vehicle_state(VS_MOWING, time_ms=_T0)
    eb = b.process_vehicle_state(VS_MOWING, time_ms=_T0)
    assert [e.kind for e in ea] == [e.kind for e in eb] == [EVENT_RUN_STARTED]
    assert a.is_provisional and b.is_provisional


# --------------------------------------------------------------------- #
# 2. Dedup — repeated vs=4 / 4→5→4 wobble does not re-fire run_started   #
# --------------------------------------------------------------------- #


def test_repeated_vs4_and_wobble_do_not_refire() -> None:
    tracker = RunTracker()
    first = tracker.process_vehicle_state(VS_MOWING, time_ms=_T0)
    assert [e.kind for e in first] == [EVENT_RUN_STARTED]

    # 4 → 5 (transit) → 4 (navigation): neither re-opens.
    e_returning = tracker.process_vehicle_state(VS_RETURNING, time_ms=_T0 + 1_000)
    e_mowing = tracker.process_vehicle_state(VS_MOWING, time_ms=_T0 + 2_000)
    assert e_returning == []
    assert e_mowing == []
    assert tracker.state == STATE_RUNNING
    assert tracker.is_provisional is True
    # last_time tracks the wander while RUNNING.
    assert tracker.current_run["last_time"] == _T0 + 2_000


# --------------------------------------------------------------------- #
# 3. Aborted start — dock without any type-2 commits a minimal entry     #
# --------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "dock_vs",
    [VS_DOCKED_IDLE, VS_DOCKED_CHARGING, VS_DOCKED_UNPOWERED, VS_MAPPING],
)
def test_aborted_start_commits_minimal_interrupted_entry(dock_vs: int) -> None:
    clk = _FakeClock()
    tracker = RunTracker(clock=clk)

    tracker.process_vehicle_state(VS_MOWING, time_ms=_T0)
    clk.advance(5)

    end = _T0 + 90_000  # 90 s of dock-exit wander before returning
    dock = tracker.process_vehicle_state(dock_vs, time_ms=end)
    # Dock entry alone fires nothing (charging included — vs=2 arms too).
    assert dock == []
    assert tracker.state == STATE_PAUSED_DOCKED

    # Not yet sustained.
    clk.advance(INTERRUPT_SUSTAIN_SECONDS - 1)
    assert tracker.tick() == []

    # Sustained past the debounce → aborted-start close.
    clk.advance(2)
    closed = tracker.tick()
    assert [e.kind for e in closed] == [EVENT_RUN_FINISHED]
    payload = closed[0].payload
    assert payload["result"] == RESULT_INTERRUPTED
    assert payload["start_time"] == _T0
    assert payload["end_time"] == end
    assert payload["duration_ms"] == end - _T0  # real wander duration
    assert payload["zones"] == []
    assert payload["session_area"] is None
    assert payload["mow_start_type"] is None
    assert tracker.state == STATE_IDLE
    assert tracker.counters["aborted_starts_committed"] == 1


def test_seeded_run_recharge_does_not_abort() -> None:
    """Contrast to test 3: once a run is seeded, a vs=2 charging dock
    keeps the original semantics — a mid-run recharge never times out
    (only the provisional path arms on charging).
    """
    clk = _FakeClock()
    tracker = RunTracker(clock=clk)
    tracker.process_vehicle_state(VS_MOWING, time_ms=_T0)
    _feed_t2(tracker, mp=50, cmp=5000, sub=20.0, time=_T0 + 1_000)  # seeds
    assert tracker.is_provisional is False

    tracker.process_vehicle_state(VS_DOCKED_CHARGING, time_ms=_T0 + 2_000)
    assert tracker.state == STATE_PAUSED_DOCKED
    clk.advance(INTERRUPT_SUSTAIN_SECONDS + 5)
    assert tracker.tick() == []  # charging → no interruption close
    assert tracker.state == STATE_PAUSED_DOCKED


# --------------------------------------------------------------------- #
# 4. Ceiling vestige as first window packet is dropped; next seeds       #
# --------------------------------------------------------------------- #


def test_ceiling_vestige_dropped_during_window_then_next_seeds() -> None:
    tracker = RunTracker()
    tracker.process_vehicle_state(VS_MOWING, time_ms=_T0)

    # Late task-end vestige (mp=100 ∧ cmp=10000): armed window (zones==[])
    # drops it, run stays provisional.
    vestige = _feed_t2(tracker, mp=100, cmp=10000, sub=0.0, time=_T0 + 1_000)
    assert vestige == []
    assert tracker.is_provisional is True
    assert tracker.current_run["zones"] == []

    # Next honest packet seeds normally, no second run_started.
    seeded = _feed_t2(tracker, mp=3, cmp=104, sub=12.24, time=_T0 + 2_000)
    assert seeded == []
    assert tracker.is_provisional is False
    assert tracker.current_run["sub0"] == 12.24
    assert tracker.current_run["start_time"] == _T0  # kept the vs=4 anchor


# --------------------------------------------------------------------- #
# 5. Below-ceiling replay as first window packet — accepted + logged     #
# --------------------------------------------------------------------- #


def test_below_ceiling_replay_accepted_and_debug_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    tracker = RunTracker()
    tracker.process_vehicle_state(VS_MOWING, time_ms=_T0)

    with caplog.at_level(logging.DEBUG, logger=_LOGGER_NAME):
        # frozen-ish sub, mp ≤ 99, partial cmp — the untested #105-Q1
        # shape. Variant O accepts it by default (no gating vs the
        # previous run inside the window) and logs the full shape.
        ev = _feed_t2(tracker, mp=80, cmp=5000, sub=150.0, time=_T0 + 1_000)

    assert ev == []
    assert tracker.is_provisional is False
    assert tracker.current_run["sub0"] == 150.0
    assert any("start-window first type-2" in r.message for r in caplog.records)


# --------------------------------------------------------------------- #
# 6. Honest seeding — anchors from the packet, start_time from vs=4      #
# --------------------------------------------------------------------- #


def test_honest_seeding_keeps_activation_start_time() -> None:
    tracker = RunTracker()
    started = tracker.process_vehicle_state(VS_MOWING, time_ms=_T0)
    assert [e.kind for e in started] == [EVENT_RUN_STARTED]

    seeded = _feed_t2(
        tracker,
        boundary=1,
        mp=3,
        cmp=104,
        sub=12.24,
        wk=357.63,
        mow_start_type=1,
        time=_T0 + 1_500,
    )
    # Continuation, not a fresh open — no second run_started.
    assert seeded == []
    r = tracker.current_run
    assert r["provisional"] is False
    assert r["start_time"] == _T0  # unchanged (activation anchor)
    assert r["sub0"] == 12.24
    assert r["mow_start_type"] == 1
    assert r["wk0"] == pytest.approx(357.63 - 12.24)
    assert r["last_sub"] == 12.24
    assert r["last_mp"] == 3
    assert r["zones"] and r["zones"][0]["boundary_id"] == 1


def test_bug06_sentinel_first_packet_seeds_sub0_zero() -> None:
    """Explicitly-accepted parity (brief §1f): a BUG-06 all-zero
    sentinel as the first window packet seeds `sub0 = 0.0`, exactly as
    `_open_run(sentinel)` would from IDLE. Not a regression.
    """
    tracker = RunTracker()
    tracker.process_vehicle_state(VS_MOWING, time_ms=_T0)
    _feed_t2(tracker, boundary=0, mp=0, cmp=0, sub=0.0, time=_T0 + 1_000)
    assert tracker.is_provisional is False
    assert tracker.current_run["sub0"] == 0.0
    # boundary=0 is filtered from zone accounting (BUG-06).
    assert tracker.current_run["zones"] == []


# --------------------------------------------------------------------- #
# 7. No completion can fire while provisional                            #
# --------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "dock_vs", [VS_DOCKED_IDLE, VS_DOCKED_CHARGING, VS_DOCKED_UNPOWERED]
)
def test_no_completion_while_provisional(dock_vs: int) -> None:
    tracker = RunTracker()
    tracker.process_vehicle_state(VS_MOWING, time_ms=_T0)
    # A dock while provisional never yields run_finished synchronously
    # (last_mp is None → _is_completed False), only arms the abort timer.
    ev = tracker.process_vehicle_state(dock_vs, time_ms=_T0 + 1_000)
    assert ev == []
    assert tracker.state == STATE_PAUSED_DOCKED
    assert tracker.is_provisional is True


# --------------------------------------------------------------------- #
# 8. Snapshot mid-window → restore resolves both ways                    #
# --------------------------------------------------------------------- #


def test_snapshot_midwindow_restores_provisional_then_seeds() -> None:
    src = RunTracker()
    src.process_vehicle_state(VS_MOWING, time_ms=_T0)
    snap = src.snapshot()

    dst = RunTracker()
    assert dst.restore(snap) is True
    assert dst.is_provisional is True
    assert dst.current_run["start_time"] == _T0

    ev = _feed_t2(dst, mp=3, cmp=104, sub=12.24, time=_T0 + 2_000)
    assert ev == []
    assert dst.is_provisional is False
    assert dst.current_run["start_time"] == _T0


def test_snapshot_midwindow_restores_provisional_then_aborts() -> None:
    src = RunTracker()
    src.process_vehicle_state(VS_MOWING, time_ms=_T0)
    snap = src.snapshot()

    clk = _FakeClock()
    dst = RunTracker(clock=clk)
    assert dst.restore(snap) is True
    assert dst.is_provisional is True

    dst.process_vehicle_state(VS_DOCKED_IDLE, time_ms=_T0 + 30_000)
    clk.advance(INTERRUPT_SUSTAIN_SECONDS + 1)
    closed = dst.tick()
    assert [e.kind for e in closed] == [EVENT_RUN_FINISHED]
    assert closed[0].payload["result"] == RESULT_INTERRUPTED
    assert closed[0].payload["duration_ms"] == 30_000
    assert dst.counters["aborted_starts_committed"] == 1


# --------------------------------------------------------------------- #
# 9. Terminal path without vs=4 — strict-progress refusal now counted    #
# --------------------------------------------------------------------- #


def test_terminal_strict_progress_rejection_counted_and_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    tracker = RunTracker()
    _restore_terminal(tracker, "completed", last_sub=200.0, last_mp=50)

    with caplog.at_level(logging.DEBUG, logger=_LOGGER_NAME):
        # Echo of the close (sub == last_sub, not a vestige: mp≠100) —
        # no strict progress → rejected, no run opened, now counted.
        ev = _feed_t2(
            tracker, boundary=1, mp=50, cmp=5000, sub=200.0, wk=250.0, time=_T0
        )

    assert ev == []
    assert tracker.state == STATE_IDLE  # unchanged, no new run
    assert tracker.counters["strict_progress_rejections"] == 1
    assert any("rejected by strict progress" in r.message for r in caplog.records)


# --------------------------------------------------------------------- #
# 10. PAUSED_DOCKED-provisional — Sol/Fable blocking-fix behaviour        #
# --------------------------------------------------------------------- #


def test_paused_provisional_type2_while_docked_is_ignored(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Sol/Fable review 2026-07-23 (blocking): a delayed type-2 that
    arrives while a provisional start is STILL docked must be IGNORED —
    neither resume nor seed. Seeding would strand the run at
    `RUNNING ∧ docked ∧ timer=None`, which `tick()` (PAUSED_DOCKED-only)
    could never close. The run stays provisional + PAUSED_DOCKED, the
    abort timer keeps running, and the sustained-dock close still fires
    the arbitrated minimal interrupted entry.
    """
    clk = _FakeClock()
    tracker = RunTracker(clock=clk)
    tracker.process_vehicle_state(VS_MOWING, time_ms=_T0)
    tracker.process_vehicle_state(VS_DOCKED_IDLE, time_ms=_T0 + 1_000)
    assert tracker.state == STATE_PAUSED_DOCKED
    assert tracker.is_provisional is True

    # Delayed type-2 while still docked → ignored + DEBUG-logged.
    with caplog.at_level(logging.DEBUG, logger=_LOGGER_NAME):
        ev = _feed_t2(tracker, mp=3, cmp=104, sub=12.24, time=_T0 + 2_000)
    assert ev == []
    assert tracker.state == STATE_PAUSED_DOCKED
    assert tracker.is_provisional is True  # not seeded
    assert tracker.current_run["sub0"] is None
    assert any(
        "ignored while provisional start remains docked" in r.message
        for r in caplog.records
    )

    # The abort timer was untouched — a sustained dock still closes it.
    clk.advance(INTERRUPT_SUSTAIN_SECONDS + 1)
    closed = tracker.tick()
    assert [e.kind for e in closed] == [EVENT_RUN_FINISHED]
    assert closed[0].payload["result"] == RESULT_INTERRUPTED
    assert tracker.counters["aborted_starts_committed"] == 1


def test_paused_provisional_offdock_type1_then_type2_seeds() -> None:
    """Sol/Fable review 2026-07-23: the dock-poke recovery leg. A
    provisional run that pokes the dock then leaves again (off-dock
    type-1) returns to RUNNING and disarms — the state must never read
    'paused' while the robot is off-dock — and the *next* type-2 then
    seeds normally.
    """
    clk = _FakeClock()
    tracker = RunTracker(clock=clk)
    tracker.process_vehicle_state(VS_MOWING, time_ms=_T0)
    tracker.process_vehicle_state(VS_DOCKED_IDLE, time_ms=_T0 + 1_000)
    assert tracker.state == STATE_PAUSED_DOCKED

    # Off-dock again before the debounce → back to RUNNING, disarmed.
    ev = tracker.process_vehicle_state(VS_MOWING, time_ms=_T0 + 2_000)
    assert ev == []
    assert tracker.state == STATE_RUNNING
    assert tracker.is_provisional is True
    # A sustained interval off-dock does not commit an abort.
    clk.advance(INTERRUPT_SUSTAIN_SECONDS + 5)
    assert tracker.tick() == []
    assert tracker.state == STATE_RUNNING

    # The next type-2 (now off-dock) seeds normally.
    seed = _feed_t2(tracker, mp=3, cmp=104, sub=12.24, time=_T0 + 3_000)
    assert seed == []
    assert tracker.state == STATE_RUNNING
    assert tracker.is_provisional is False
    assert tracker.current_run["sub0"] == 12.24
    assert tracker.current_run["start_time"] == _T0


# --------------------------------------------------------------------- #
# 11. Backward compatibility — vs without time_ms still legal            #
# --------------------------------------------------------------------- #


def test_process_vehicle_state_without_time_ms_is_legal() -> None:
    clk = _FakeClock()
    tracker = RunTracker(clock=clk)

    started = tracker.process_vehicle_state(VS_MOWING)  # no time_ms
    assert [e.kind for e in started] == [EVENT_RUN_STARTED]
    assert tracker.is_provisional is True
    assert tracker.current_run["start_time"] is None

    tracker.process_vehicle_state(VS_DOCKED_IDLE)  # no time_ms
    clk.advance(INTERRUPT_SUSTAIN_SECONDS + 1)
    closed = tracker.tick()
    assert [e.kind for e in closed] == [EVENT_RUN_FINISHED]
    # Degraded, not crashed — duration is None when either endpoint is None.
    assert closed[0].payload["duration_ms"] is None
    assert closed[0].payload["result"] == RESULT_INTERRUPTED
