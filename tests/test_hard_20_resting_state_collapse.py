"""HARD-20 — collapse the post-close resting states (#122, SPIKE-03 outcome B).

Characterization pins for the 5→3 state collapse. Per the implementation
brief (§3) and Sol's review, these are written **first** and must pass on
the *current* build before the refactor lands — they encode today's
behaviour so the refactor cannot move it (risk R1's method).

They are deliberately **state-string-agnostic**: none asserts
the removed terminal-state constants nor
`STATE_IDLE` (fails pre-refactor). Each pins the *gating trichotomy* and
the *record*, which are invariant across the collapse:

- T1  first boot (no reference)          → ungated open;
- T2  at rest + seeded reference          → echo rejected + counter;
                                            strict-progress → new session;
- T3  at rest + seeded reference, reset    → below ceiling opens,
                                            above ceiling stashes then confirms;
- T4  at rest + empty reference (post-abort, both axes None)
                                          → honest packet conservatively
                                            rejected + counter, then vs=4
                                            self-resolves.

Resting states are reached by **closing a run** (a machine act), never by
injecting a state string — the brief's fixture rule.
"""

from __future__ import annotations

import logging

import pytest

from custom_components.navimow.location import parse_location_type_2
from custom_components.navimow.run_tracker import (
    EVENT_RUN_STARTED,
    INTERRUPT_SUSTAIN_SECONDS,
    SNAPSHOT_VERSION,
    STATE_IDLE,
    VS_DOCKED_CHARGING,
    VS_DOCKED_IDLE,
    VS_MOWING,
    RunTracker,
)

_LOGGER_NAME = "custom_components.navimow.run_tracker"
_T0 = 6_000_000_000_000


class _FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, secs: float) -> None:
        self.now += secs


def _t2_item(*, boundary=1, cmp=104, mp=3, sub=12.24, wk=None, time, action=8) -> dict:
    # Default wk keeps the wk₀ invariant clean (wk − sub constant → 0).
    wk = sub if wk is None else wk
    return {
        "type": 2,
        "currentMowBoundary": boundary,
        "currentMowProgress": cmp,
        "mowingPercentage": mp,
        "subtotalArea": str(sub),
        "mowingWeekArea": str(wk),
        "mowStartType": 1,
        "action": action,
        "time": time,
    }


def _feed_t2(tracker: RunTracker, **kw) -> list:
    return tracker.process_type2(parse_location_type_2(_t2_item(**kw)))


def _rest_with_seeded_reference(tracker: RunTracker) -> None:
    """Open a run, mow two packets, then complete it on a dock arrival.
    Leaves the tracker at rest with a *seeded* `current_run` reference
    (`last_sub = 200`, `last_mp = 100`) — the FEAT-06 post-close reference.
    """
    _feed_t2(tracker, boundary=1, mp=50, cmp=5000, sub=100.0, wk=100.0, time=_T0)
    _feed_t2(
        tracker, boundary=1, mp=100, cmp=6000, sub=200.0, wk=200.0, time=_T0 + 30_000
    )
    tracker.process_vehicle_state(VS_DOCKED_CHARGING)  # mp=100 ∧ docked → completes
    assert tracker.current_run is not None
    assert tracker.current_run["last_sub"] == 200.0


def _rest_with_empty_reference(tracker: RunTracker, clk: _FakeClock) -> None:
    """Press RUN then abort before any type-2 (HARD-18 aborted start).
    Leaves the tracker at rest with an *empty* reference (`last_sub` and
    `last_mp` both `None`).
    """
    tracker.process_vehicle_state(VS_MOWING, time_ms=_T0)
    tracker.process_vehicle_state(VS_DOCKED_IDLE, time_ms=_T0 + 1_000)
    clk.advance(INTERRUPT_SUSTAIN_SECONDS + 1)
    tracker.tick()
    assert tracker.current_run is not None
    assert tracker.current_run["last_sub"] is None
    assert tracker.current_run["last_mp"] is None


# --------------------------------------------------------------------- #
# T1 — first boot: no reference → ungated open                          #
# --------------------------------------------------------------------- #


def test_t1_first_boot_ungated_open() -> None:
    tracker = RunTracker()
    assert tracker.current_run is None
    ev = _feed_t2(tracker, boundary=1, mp=50, cmp=5000, sub=20.0, wk=100.0, time=_T0)
    assert [e.kind for e in ev] == [EVENT_RUN_STARTED]
    assert tracker.current_run is not None
    assert tracker.current_run["last_sub"] == 20.0


# --------------------------------------------------------------------- #
# T2 — at rest + seeded reference: echo rejected, strict-progress opens  #
# --------------------------------------------------------------------- #


def test_t2_resting_seeded_echo_rejected_then_strict_opens(
    caplog: pytest.LogCaptureFixture,
) -> None:
    tracker = RunTracker()
    _rest_with_seeded_reference(tracker)  # last_sub = 200
    ref_before = tracker.current_run
    base = tracker.counters["strict_progress_rejections"]

    # Echo of the close (sub == last_sub, no strict progress) → rejected.
    with caplog.at_level(logging.DEBUG, logger=_LOGGER_NAME):
        echo = _feed_t2(
            tracker,
            boundary=1,
            mp=100,
            cmp=6000,
            sub=200.0,
            wk=200.0,
            time=_T0 + 100_000,
        )
    assert echo == []
    assert tracker.counters["strict_progress_rejections"] == base + 1
    assert tracker.current_run is ref_before  # no new run opened
    assert any("strict progress" in r.message for r in caplog.records)

    # Strict-progress packet (sub > last_sub) → new session opens.
    fresh = _feed_t2(
        tracker, boundary=1, mp=30, cmp=1000, sub=205.0, wk=205.0, time=_T0 + 130_000
    )
    assert [e.kind for e in fresh] == [EVENT_RUN_STARTED]
    assert tracker.current_run is not ref_before
    assert tracker.current_run["last_sub"] == 205.0


# --------------------------------------------------------------------- #
# T3 — at rest + seeded reference, reset: below opens / above stashes    #
# --------------------------------------------------------------------- #


def test_t3_resting_reset_below_ceiling_opens() -> None:
    tracker = RunTracker()
    _rest_with_seeded_reference(tracker)  # last_sub = 200
    # sub = 2.0 < 200 (reset) and < RESET_SUB_CEILING (10) → open, no close.
    ev = _feed_t2(
        tracker, boundary=1, mp=5, cmp=200, sub=2.0, wk=2.0, time=_T0 + 100_000
    )
    assert [e.kind for e in ev] == [EVENT_RUN_STARTED]
    assert tracker.current_run["last_sub"] == 2.0


def test_t3_resting_reset_above_ceiling_stashes_then_confirms() -> None:
    tracker = RunTracker()
    _rest_with_seeded_reference(tracker)  # last_sub = 200
    # sub = 100 < 200 (reset) and > ceiling → stash, no open yet.
    stash = _feed_t2(
        tracker, boundary=1, mp=40, cmp=3000, sub=100.0, wk=150.0, time=_T0 + 100_000
    )
    assert stash == []
    assert tracker._pending_reset is not None

    # Coherent successor (sub > candidate, wk − sub shift within tol) confirms.
    confirm = _feed_t2(
        tracker, boundary=1, mp=45, cmp=3500, sub=105.0, wk=155.0, time=_T0 + 130_000
    )
    assert EVENT_RUN_STARTED in [e.kind for e in confirm]
    assert tracker._pending_reset is None


# --------------------------------------------------------------------- #
# T4 — at rest + empty reference (post-abort): conservative reject       #
# --------------------------------------------------------------------- #


def test_t4_resting_empty_reference_rejects_then_vs4_self_resolves(
    caplog: pytest.LogCaptureFixture,
) -> None:
    clk = _FakeClock()
    tracker = RunTracker(clock=clk)
    _rest_with_empty_reference(tracker, clk)  # both axes None
    base = tracker.counters["strict_progress_rejections"]

    # Honest type-2 with NO preceding vs=4 → conservative reject (both
    # comparison axes absent → `_has_strict_progress` False) + counter.
    with caplog.at_level(logging.DEBUG, logger=_LOGGER_NAME):
        ev = _feed_t2(
            tracker, boundary=1, mp=5, cmp=200, sub=12.0, wk=12.0, time=_T0 + 200_000
        )
    assert ev == []
    assert tracker.counters["strict_progress_rejections"] == base + 1
    assert any("strict progress" in r.message for r in caplog.records)

    # Self-resolution: the next vs=4 opens a provisional run normally.
    started = tracker.process_vehicle_state(VS_MOWING, time_ms=_T0 + 300_000)
    assert [e.kind for e in started] == [EVENT_RUN_STARTED]
    assert tracker.is_provisional is True


# --------------------------------------------------------------------- #
# Migration round-trips — legacy terminal state strings → IDLE           #
# --------------------------------------------------------------------- #


def _legacy_snapshot(state: str) -> dict:
    """A pre-raoul.27 tracker snapshot carrying a removed terminal-state
    string plus a seeded post-close reference and lifetime counters —
    the shape the 07-19 / 07-20 / 07-23 Store dumps persisted."""
    return {
        "version": SNAPSHOT_VERSION,
        "state": state,
        "vehicle_state": VS_DOCKED_CHARGING,
        "current_run": {
            "start_time": _T0,
            "mow_start_type": 0,
            "wk0": 0.0,
            "sub0": 10.0,
            "last_time": _T0 + 60_000,
            "last_sub": 200.0,
            "last_wk": 200.0,
            "last_mp": 100,
            "zones": [
                {
                    "boundary_id": 1,
                    "first_time": _T0,
                    "last_time": _T0 + 60_000,
                    "cmp_max": 10000,
                    "sub_entry": 10.0,
                    "sub_exit": 200.0,
                }
            ],
        },
        "last_accepted_wk": 200.0,
        "last_accepted_time_ms": _T0 + 60_000,
        "drops": {"pending_reset_holds": 1},
        "counters": {
            "wk_regressions_observed": 2,
            "invariant_deviations_observed": 1,
            "strict_progress_rejections": 0,
            "aborted_starts_committed": 1,
        },
    }


# HARD-21 (#123): the HARD-20 raoul.27 dead-vocabulary shim
# (`"completed"`/`"interrupted"` → IDLE) is retired now the on-disk state
# is re-persisted in the current vocabulary (verified 2026-07-23,
# `state="idle"`). A legacy terminal string now falls through the
# robustness catch-all (unknown → IDLE + one WARN), never raising — these
# pins confirm the removal did not break reading a raoul.26-vintage Store.
@pytest.mark.parametrize(
    "legacy", ["completed", "interrupted", "some_removed_future_state"]
)
def test_restore_out_of_vocabulary_state_maps_to_idle_with_warn(
    legacy: str, caplog: pytest.LogCaptureFixture
) -> None:
    tracker = RunTracker()
    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        assert tracker.restore(_legacy_snapshot(legacy)) is True

    # Out-of-vocabulary → IDLE + one WARN, never raises...
    assert tracker.state == STATE_IDLE
    assert any(
        "unknown state" in r.getMessage() and r.levelno == logging.WARNING
        for r in caplog.records
    )
    # ...the seeded reference, counters and drops survive byte-intact...
    assert tracker.current_run["last_sub"] == 200.0
    assert tracker.current_run["zones"][0]["boundary_id"] == 1
    assert tracker.counters["aborted_starts_committed"] == 1
    assert tracker.counters["wk_regressions_observed"] == 2
    assert tracker.drops["pending_reset_holds"] == 1
    # ...and a re-snapshot writes the current vocabulary only.
    assert tracker.snapshot()["state"] == STATE_IDLE

    # The restored reference still keys the post-close gate: an echo
    # (sub == last_sub, no strict progress) is conservatively rejected.
    base = tracker.counters["strict_progress_rejections"]
    echo = _feed_t2(
        tracker, boundary=1, mp=100, cmp=6000, sub=200.0, wk=200.0, time=_T0 + 200_000
    )
    assert echo == []
    assert tracker.counters["strict_progress_rejections"] == base + 1
