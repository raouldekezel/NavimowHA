"""FEAT-06 — session-scoped runs (#54).

Verifies the post-BUG-10 hotfix design turns the tracker into a
session-scoped state machine — a run maps to a user session
(activation → final dock), and the reopen-on-strict-progress transition
that used to resurrect a closed run is retired.

Test spine (Fable brief 2026-07-05):

- **2026-07-04 replay** (synthetic fixture built from the #52/#56 TSVs
  in `docs/diag/2026-07-04_bug-09_paused-docked-mp-99/` and the
  recorder TSV `docs/diag/2026-07-04_spike-02_run-semantics-task-vs-session/01_afternoon-recorder.sensors.tsv`
  on PR #55) → **two** history rows out, session-scoped areas.
- **No duplicate history rows** after a close + resume.
- **`session_area` = `last_sub − sub₀`** on the emitted `run_finished`
  payload and history entry.
- **Store migration**: a pre-FEAT-06 snapshot restores cleanly, and
  the degraded first close reports `session_area = None` (rather than
  fabricating a value from the firmware's task accumulator).
- **BUG-10 WARN escalation** (Fable suggestion, Raoul-confirmed): the
  throttled WARNING fires exactly once after
  `WK_REGRESSION_STREAK_TO_WARN` consecutive regressions, and a
  non-regressing packet in between resets the streak.
"""

from __future__ import annotations

import logging
from unittest.mock import MagicMock

from custom_components.navimow.location import parse_location_type_2
from custom_components.navimow.run_tracker import (
    EVENT_RUN_FINISHED,
    EVENT_RUN_STARTED,
    RESULT_COMPLETED,
    STATE_COMPLETED,
    STATE_IDLE,
    STATE_RUNNING,
    VS_DOCKED_CHARGING,
    WK_REGRESSION_STREAK_TO_WARN,
    RunTracker,
)


def _feed(tracker: RunTracker, items: list[dict]) -> list:
    events = []
    for item in items:
        parsed = parse_location_type_2(item)
        events.extend(tracker.process_type2(parsed))
    return events


# --------------------------------------------------------------------- #
# 1. 2026-07-04 replay — two sessions, session-scoped areas             #
# --------------------------------------------------------------------- #

# Timestamps below are synthetic epoch-ms values that preserve the
# ordering and cadence of the real day (morning scheduled mow, ~4 h
# dock, afternoon manual RUN, second dock). Values for `sub` / `wk` /
# `mp` mirror the shape recorded in the two committed TSVs:
#
# - `wk` = 946 at morning start (BUG-09 diag TSV 09:30:58 CEST first
#   surface_hebdomadaire = 945.41; rounded to 946 here), climbing to
#   1063 at 10:28 CEST (surface_hebdomadaire = 1063.33). The BUG-09
#   diag reports the *sensor* value which is `wk`, not `sub` — we
#   choose `sub` = 1 → 118 so the session mowed 117 m² of the ~120 m²
#   zone #1 (consistent with cmp climbing to 99.06 %).
# - Afternoon first packet: SPIKE-02 recorder line 14:17:34 shows
#   `progression_du_passage = 65`, `surface_hebdomadaire = 1066.17`,
#   `zone_courante = #3`, `progression_de_la_zone = 1.07`. `sub` is not
#   in the recorder (only surface_hebdomadaire is), so we pick `sub`
#   values that satisfy the invariant `wk − sub = wk₀` (constant
#   across the task series — verified on FEAT-05 committed data):
#   wk=1066.17 with wk₀=945 gives `sub = 121.17`. The morning last
#   packet had `sub=118`, so 121.17 > 118 (strict progress).
# - Afternoon end: SPIKE-02 recorder line 15:11:23 shows
#   `progression_du_passage = 100`, `surface_hebdomadaire = 1189.34`.
#   That gives `sub = 244.34`.

_MORNING_START_MS = 1_000_000_000_000  # arbitrary anchor, ~2001-09-09
_MORNING_END_MS = _MORNING_START_MS + 47 * 60_000  # ~47 min mow
_AFTERNOON_START_MS = _MORNING_START_MS + 4 * 3_600_000 + 30 * 60_000  # +4h30
_AFTERNOON_END_MS = _AFTERNOON_START_MS + 54 * 60_000  # ~54 min mow


def test_2026_07_04_full_day_yields_exactly_two_sessions() -> None:
    tracker = RunTracker()

    # -- Morning session (scheduled, zone #1) ------------------------ #
    morning_open = _feed(
        tracker,
        [
            {
                "type": 2,
                "currentMowBoundary": 1,
                "currentMowProgress": 100,
                "mowingPercentage": 48,
                "subtotalArea": "1.0",
                "mowingWeekArea": "946.0",  # wk₀ = 945
                "mowStartType": 0,
                "time": _MORNING_START_MS,
            },
            {
                "type": 2,
                "currentMowBoundary": 1,
                "currentMowProgress": 9906,
                "mowingPercentage": 99,
                "subtotalArea": "118.0",
                "mowingWeekArea": "1063.0",
                "mowStartType": 0,
                "time": _MORNING_END_MS,
            },
        ],
    )
    assert [e.kind for e in morning_open] == [EVENT_RUN_STARTED]

    # Robot returns to dock → BUG-09 close path (mp ≥ 99 + vs=2).
    morning_close = tracker.process_vehicle_state(VS_DOCKED_CHARGING)
    assert [e.kind for e in morning_close] == [EVENT_RUN_FINISHED]
    morning = morning_close[0].payload
    assert morning["result"] == RESULT_COMPLETED
    # Session-scoped area = last_sub (118) − sub₀ (1) = 117.
    assert morning["session_area"] == 117.0
    # Zone deltas keep absolute `sub` pairs (FEAT-05 semantics
    # unchanged) — the morning covered zone #1 only.
    assert [z["boundary_id"] for z in morning["zones"]] == [1]
    assert morning["zones"][0]["sub_entry"] == 1.0
    assert morning["zones"][0]["sub_exit"] == 118.0

    # Between sessions: robot leaves the dock (RUN pressed) — vs
    # transitions 2 → 4. Without this, the tracker still thinks it is
    # docked charging and the completion criterion would re-fire on
    # any afternoon packet with mp ≥ 99, collapsing the new session.
    from custom_components.navimow.run_tracker import VS_MOWING

    tracker.process_vehicle_state(VS_MOWING)

    # -- Afternoon session (manual RUN, zone #3) --------------------- #
    # Fresh accepted type-2 with strict progress (sub 121.17 > 118)
    # and layer-3-consistent anchor (1066.17 − 121.17 = 945 = morning
    # wk₀). FEAT-06: opens a NEW session, not a reopen.
    afternoon_events = _feed(
        tracker,
        [
            {
                "type": 2,
                "currentMowBoundary": 3,
                "currentMowProgress": 107,  # ~1 % zone_progress
                "mowingPercentage": 65,
                "subtotalArea": "121.17",
                "mowingWeekArea": "1066.17",
                "mowStartType": 1,  # manual
                "time": _AFTERNOON_START_MS,
            },
            {
                "type": 2,
                "currentMowBoundary": 3,
                "currentMowProgress": 10000,
                "mowingPercentage": 100,
                "subtotalArea": "244.34",
                "mowingWeekArea": "1189.34",
                "mowStartType": 1,
                "time": _AFTERNOON_END_MS,
            },
        ],
    )
    assert [e.kind for e in afternoon_events] == [EVENT_RUN_STARTED]
    # start_time of the new session is the AFTERNOON packet's time —
    # not the morning's start_time (this is the Problem A fix).
    assert tracker.current_run["start_time"] == _AFTERNOON_START_MS
    assert tracker.current_run["sub0"] == 121.17
    # mp=65 is task-scoped — non-zero session start is normal per the
    # SPIKE-02 finding, and `run_progress` reads the raw firmware `mp`.
    # This ensures we did NOT renormalise mp to session scope.
    assert tracker.current_run["last_mp"] == 100  # after 2nd packet

    afternoon_close = tracker.process_vehicle_state(VS_DOCKED_CHARGING)
    assert [e.kind for e in afternoon_close] == [EVENT_RUN_FINISHED]
    afternoon = afternoon_close[0].payload
    assert afternoon["result"] == RESULT_COMPLETED
    # Session-scoped area = 244.34 − 121.17 = 123.17 (not 243.34 = the
    # firmware's raw task accumulator, which would double-count the
    # morning's 117 m²).
    assert abs(afternoon["session_area"] - 123.17) < 1e-9
    assert [z["boundary_id"] for z in afternoon["zones"]] == [3]
    # start_time strictly after morning end — no 6-hour aggregate.
    assert afternoon["start_time"] > morning["end_time"]
    assert afternoon["duration_ms"] < 60 * 60_000  # bounded ~54 min


def test_afternoon_new_session_ignores_echo_of_morning_close_packet() -> None:
    """Regression against phantom sessions on the stream tail: after
    the morning close, an echo of the closing packet (same `sub`, only
    `time` fresher) must not spawn a new session.
    """
    tracker = RunTracker()
    _feed(
        tracker,
        [
            {
                "type": 2,
                "currentMowBoundary": 1,
                "mowingPercentage": 99,
                "subtotalArea": "118.0",
                "mowingWeekArea": "1063.0",
                "mowStartType": 0,
                "time": _MORNING_START_MS,
            }
        ],
    )
    tracker.process_vehicle_state(VS_DOCKED_CHARGING)
    assert tracker.state == STATE_COMPLETED

    # Echo — same sub/mp, later time, no strict progress.
    echo = _feed(
        tracker,
        [
            {
                "type": 2,
                "currentMowBoundary": 1,
                "mowingPercentage": 99,
                "subtotalArea": "118.0",
                "mowingWeekArea": "1063.0",
                "mowStartType": 0,
                "time": _MORNING_START_MS + 60_000,
            }
        ],
    )
    assert echo == []
    assert tracker.state == STATE_COMPLETED


# --------------------------------------------------------------------- #
# 2. History fan-out — no duplicate rows (Problem C)                    #
# --------------------------------------------------------------------- #


def _make_coordinator_with_history():
    """Minimal coordinator stub for `_forward_run_events` exercising."""
    from custom_components.navimow.coordinator import NavimowCoordinator

    coord = NavimowCoordinator.__new__(NavimowCoordinator)
    coord.hass = MagicMock()
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


def test_close_plus_new_session_writes_two_history_rows_not_one() -> None:
    """Problem C from #54 dissolves structurally: with FEAT-06 each
    close is its own history row, so a resume after a close never
    overwrites the morning row (as `_reopen_run` used to imply).
    """
    from custom_components.navimow.run_tracker import VS_MOWING

    coord = _make_coordinator_with_history()

    def _feed_vs(vs: int) -> None:
        # Mimic `_handle_location_position`'s vs-change branch —
        # forward tracker events through the same dispatcher the
        # coordinator uses.
        coord._forward_run_events(coord.run_tracker.process_vehicle_state(vs))

    # Morning session — open, then dock to trigger BUG-09 close.
    coord.handle_location_item(
        {
            "type": 2,
            "mowingPercentage": 99,
            "subtotalArea": "118.0",
            "mowingWeekArea": "1063.0",
            "mowStartType": 0,
            "time": _MORNING_START_MS,
        }
    )
    _feed_vs(VS_DOCKED_CHARGING)
    assert len(coord.history) == 1
    morning_start = coord.history[0]["start_time"]

    # Robot leaves the dock (RUN pressed) — vs 2 → 4.
    _feed_vs(VS_MOWING)

    # Afternoon RUN — first packet opens a new session (FEAT-06);
    # sub₀ anchored on it. mp=65 alone doesn't fire completion.
    coord.handle_location_item(
        {
            "type": 2,
            "mowingPercentage": 65,
            "subtotalArea": "121.17",
            "mowingWeekArea": "1066.17",
            "mowStartType": 1,
            "time": _AFTERNOON_START_MS,
        }
    )
    # Later afternoon packet reaches mp=99 while vs=4 (still mowing).
    coord.handle_location_item(
        {
            "type": 2,
            "mowingPercentage": 100,
            "subtotalArea": "244.34",
            "mowingWeekArea": "1189.34",
            "mowStartType": 1,
            "time": _AFTERNOON_END_MS,
        }
    )
    # Dock arrival closes the afternoon session.
    _feed_vs(VS_DOCKED_CHARGING)

    assert len(coord.history) == 2, coord.history
    assert coord.history[0]["start_time"] == morning_start
    assert coord.history[1]["start_time"] == _AFTERNOON_START_MS
    # The two rows are distinct — no duplicate on the same start_time.
    assert coord.history[0]["start_time"] != coord.history[1]["start_time"]
    # Each row carries its own session_area (afternoon session mowed
    # 244.34 − 121.17 = 123.17, not the raw 244.34 accumulator).
    assert abs(coord.history[1]["session_area"] - 123.17) < 1e-9


# --------------------------------------------------------------------- #
# 3. session_area on the run_finished payload                           #
# --------------------------------------------------------------------- #


def test_session_area_equals_last_sub_minus_sub0() -> None:
    tracker = RunTracker()
    _feed(
        tracker,
        [
            {
                "type": 2,
                "mowingPercentage": 40,
                "subtotalArea": "10.0",
                "mowingWeekArea": "10.0",
                "time": 1_000_000_000_000,
            },
            {
                "type": 2,
                "mowingPercentage": 99,
                "subtotalArea": "42.5",
                "mowingWeekArea": "42.5",
                "time": 1_000_000_120_000,
            },
        ],
    )
    events = tracker.process_vehicle_state(VS_DOCKED_CHARGING)
    finished = [e for e in events if e.kind == EVENT_RUN_FINISHED]
    assert len(finished) == 1
    assert finished[0].payload["session_area"] == 32.5  # 42.5 − 10.0


def test_session_area_none_on_pre_feat_06_open_snapshot() -> None:
    """A pre-FEAT-06 snapshot has an open run without `sub₀`. After
    restore, the very next `_close_run` payload reports
    `session_area = None` rather than fabricating a value from the
    firmware's task accumulator (which would over-credit the session).
    The next `_open_run` writes a real `sub₀` and the sensor exits the
    degraded window.
    """
    tracker = RunTracker()
    legacy_snap = {
        "version": 1,
        "state": STATE_RUNNING,
        "vehicle_state": None,
        "current_run": {
            # Pre-FEAT-06 shape — no `sub0` key.
            "start_time": 1_000_000_000_000,
            "mow_start_type": 1,
            "wk0": 0.0,
            "last_time": 1_000_000_060_000,
            "last_sub": 42.5,
            "last_wk": 42.5,
            "last_mp": 99,
            "zones": [],
        },
        "last_accepted_wk": 42.5,
        "last_accepted_time_ms": 1_000_000_060_000,
        "drops": {"layer_3": 0, "pending_reset_holds": 0},
        "counters": {"wk_regressions_observed": 0},
    }
    assert tracker.restore(legacy_snap) is True
    assert tracker.state == STATE_RUNNING
    assert tracker.current_run.get("sub0") is None

    # First close after restore — degraded to `session_area = None`.
    events = tracker.process_vehicle_state(VS_DOCKED_CHARGING)
    finished = [e for e in events if e.kind == EVENT_RUN_FINISHED]
    assert len(finished) == 1
    assert finished[0].payload["session_area"] is None
    assert finished[0].payload["result"] == RESULT_COMPLETED


# --------------------------------------------------------------------- #
# 4. BUG-10 WARN escalation on sustained wk regressions                 #
# --------------------------------------------------------------------- #


def _regress_packet(*, sub: float, wk: float, time_ms: int) -> dict:
    return {
        "type": 2,
        "currentMowBoundary": 1,
        "mowingPercentage": 40,
        "subtotalArea": str(sub),
        "mowingWeekArea": str(wk),
        "mowStartType": 1,
        "time": time_ms,
    }


def _predecessor_packet() -> dict:
    return {
        "type": 2,
        "currentMowBoundary": 1,
        "mowingPercentage": 40,
        "subtotalArea": "50.0",
        "mowingWeekArea": "500.0",  # wk₀ = 450 for the open run
        "mowStartType": 1,
        "time": 1_000_000_000_000,
    }


def test_wk_regression_warn_fires_once_at_threshold(caplog) -> None:
    tracker = RunTracker()
    _feed(tracker, [_predecessor_packet()])
    caplog.clear()

    with caplog.at_level(
        logging.WARNING, logger="custom_components.navimow.run_tracker"
    ):
        # Feed exactly WK_REGRESSION_STREAK_TO_WARN regressing packets.
        # Each has sub advancing (no reset) but wk regressing.
        for i in range(WK_REGRESSION_STREAK_TO_WARN):
            _feed(
                tracker,
                [
                    _regress_packet(
                        sub=50.0 + (i + 1) * 3.0,
                        wk=100.0 - i,  # every one below the cursor
                        time_ms=1_000_000_000_000 + (i + 1) * 60_000,
                    )
                ],
            )

    # HARD-06 (#62): the fixture (advancing `sub` with a regressing
    # `wk`) exercises BOTH observability signals in this state — layer
    # 2 sees the `wk` regressions, and layer 3 sees `|wk − sub − wk₀|`
    # blow up on the same packets. Both WARN fire; this test filters
    # for the wk-regression WARN specifically. The invariant WARN is
    # covered by the mid-run wk-reset test in test_feat_05b_.
    wk_warns = [
        rec
        for rec in caplog.records
        if rec.levelno >= logging.WARNING
        and "consecutive wk regressions" in rec.getMessage()
    ]
    assert len(wk_warns) == 1, [r.getMessage() for r in wk_warns]
    # Counter matches streak (all packets regressed against the cursor).
    assert tracker.counters["wk_regressions_observed"] == WK_REGRESSION_STREAK_TO_WARN


def test_wk_regression_streak_resets_on_non_regressing_packet(caplog) -> None:
    """A single non-regressing packet in the middle of a streak resets
    the counter → the WARN never fires even after
    2 × WK_REGRESSION_STREAK_TO_WARN interleaved regressions.
    """
    tracker = RunTracker()
    _feed(tracker, [_predecessor_packet()])
    caplog.clear()

    with caplog.at_level(
        logging.WARNING, logger="custom_components.navimow.run_tracker"
    ):
        # First half of the streak (below threshold, no WARN).
        for i in range(WK_REGRESSION_STREAK_TO_WARN - 1):
            _feed(
                tracker,
                [
                    _regress_packet(
                        sub=50.0 + (i + 1) * 3.0,
                        wk=100.0 - i,
                        time_ms=1_000_000_000_000 + (i + 1) * 60_000,
                    )
                ],
            )
        # A packet that ADVANCES wk against the last cursor → streak
        # reset (the cursor was updated by acceptance; the last-seen
        # `wk` = 100 - (WK - 2). A wk larger than that resets streak
        # regardless of layer 3 outcome).
        _feed(
            tracker,
            [
                _regress_packet(
                    sub=90.0,
                    wk=1000.0,  # far above the poisoned cursor
                    time_ms=1_000_000_000_000 + 999_000,
                )
            ],
        )
        # Second half of a fresh streak (still below the threshold
        # because we reset). Also, after the wk=1000 acceptance the
        # cursor moved forward — subsequent regressions will trigger
        # streak again.
        for i in range(WK_REGRESSION_STREAK_TO_WARN - 1):
            _feed(
                tracker,
                [
                    _regress_packet(
                        sub=100.0 + (i + 1) * 3.0,
                        wk=500.0 - i,  # below the reset cursor
                        time_ms=1_000_000_100_000 + (i + 1) * 60_000,
                    )
                ],
            )

    # HARD-06 (#62): as in the sibling test, the wk-regressing fixture
    # also drives layer 3's observation. Filter to the wk-regression
    # WARN — that is the one whose streak reset is under test here.
    wk_warns = [
        rec
        for rec in caplog.records
        if rec.levelno >= logging.WARNING
        and "consecutive wk regressions" in rec.getMessage()
    ]
    assert wk_warns == [], [r.getMessage() for r in wk_warns]


def test_wk_regression_warn_not_emitted_on_fresh_session_start(caplog) -> None:
    """A benign fresh-session start (yesterday's cursor → today's
    small `sub` → fresh reset path opens a new run and advances the
    cursor) triggers exactly one regression observation, well below
    the WARN threshold.
    """
    tracker = RunTracker()
    tracker.state = STATE_COMPLETED
    tracker._last_accepted_wk = 1189.34
    tracker._last_accepted_time_ms = 1_000_000_000_000
    tracker.current_run = {
        "start_time": 1_000_000_000_000 - 10_000_000,
        "mow_start_type": 0,
        "wk0": 1000.0,
        "sub0": 0.0,
        "last_time": 1_000_000_000_000,
        "last_sub": 180.0,
        "last_wk": 1189.34,
        "last_mp": 99,
        "zones": [],
    }
    caplog.clear()
    with caplog.at_level(
        logging.WARNING, logger="custom_components.navimow.run_tracker"
    ):
        _feed(
            tracker,
            [
                {
                    "type": 2,
                    "currentMowBoundary": 1,
                    "mowingPercentage": 1,
                    "subtotalArea": "0.4",
                    "mowingWeekArea": "0.4",
                    "mowStartType": 0,
                    "time": 1_000_100_000_000,
                },
                {
                    "type": 2,
                    "currentMowBoundary": 1,
                    "mowingPercentage": 2,
                    "subtotalArea": "2.0",
                    "mowingWeekArea": "2.0",
                    "mowStartType": 0,
                    "time": 1_000_100_060_000,
                },
            ],
        )

    warns = [rec for rec in caplog.records if rec.levelno >= logging.WARNING]
    assert warns == [], [r.getMessage() for r in warns]
    assert tracker.state == STATE_RUNNING
    assert tracker.counters["wk_regressions_observed"] == 1


# --------------------------------------------------------------------- #
# 5. Reopen event retired — no `EVENT_RUN_REOPENED` symbol              #
# --------------------------------------------------------------------- #


def test_event_run_reopened_symbol_removed_from_tracker() -> None:
    """Guards against a future re-introduction of a reopen shape.
    Feature killed by FEAT-06 (#54): a session is a session; a resume
    of a firmware task after a close is a new session, not a
    continuation of the closed one.
    """
    from custom_components.navimow import run_tracker

    assert not hasattr(run_tracker, "EVENT_RUN_REOPENED")
    assert not hasattr(run_tracker, "_event_run_reopened")
    assert not hasattr(run_tracker.RunTracker, "_reopen_run")

    from custom_components.navimow import const

    assert not hasattr(const, "EVENT_RUN_REOPENED")


def test_idle_open_close_still_works_no_regression() -> None:
    """Smoke: a plain IDLE → open → close cycle still emits the two
    events on the classic shape (regression guard against the state
    machine surgery).
    """
    tracker = RunTracker()
    assert tracker.state == STATE_IDLE
    events = _feed(
        tracker,
        [
            {
                "type": 2,
                "currentMowBoundary": 1,
                "mowingPercentage": 100,
                "subtotalArea": "50.0",
                "mowingWeekArea": "50.0",
                "mowStartType": 1,
                "time": 2_000_000_000_000,
            }
        ],
    )
    assert [e.kind for e in events] == [EVENT_RUN_STARTED]
    events = tracker.process_vehicle_state(VS_DOCKED_CHARGING)
    assert [e.kind for e in events] == [EVENT_RUN_FINISHED]
    assert (
        events[0].payload["session_area"] == 0.0
    )  # 50 − 50 (open packet is also close)
    assert tracker.state == STATE_COMPLETED


# --------------------------------------------------------------------- #
# 6. HARD-06 (#62) — FEAT-06 review-fixture 2 m² anchor drift           #
# --------------------------------------------------------------------- #


def test_feat_06_fixture_drift_yields_complete_second_session_no_warn(
    caplog,
) -> None:
    """The FEAT-06 review fixture accident, reproduced.

    A 2 m² anchor drift between the morning close and the afternoon
    open used to make the pre-HARD-06 layer-3 gate silently reject the
    entire afternoon (three `drops["layer_3"]`, no session opened, no
    history row, no WARN — the wk-regression streak does not fire in
    the drift shape). Post-HARD-06 (#62): the afternoon session opens
    cleanly, closes with the correct `session_area`, the deviation
    counter carries a single observation, and no WARN fires.

    Rationale on the WARN silence (Raoul, 2026-07-05, incorporated
    into the HARD-06 requirements): on the post-close new-session
    path AT MOST ONE consultation ever happens against the closed
    run's anchor — the first strict-progress packet is observed
    (deviation counted, streak = 1) and immediately opens the new
    session via `_open_run`, which re-anchors `wk₀` from that same
    packet; the next packet is within tolerance → streak resets to
    zero. Five consecutive checks against a dead anchor are
    structurally impossible on this path. The WARN's semantic is
    "persistent deviation against a LIVE anchor" — that is the mid-run
    wk-reset test's job (see
    `test_mid_run_wk_reset_run_stays_open_deviation_warns_at_five` in
    `test_feat_05b_run_tracker.py`), and that test keeps its
    single-WARN assertion.
    """
    import logging

    from custom_components.navimow.run_tracker import (
        INVARIANT_DEVIATION_STREAK_TO_WARN,
        VS_MOWING,
    )

    tracker = RunTracker()

    # Morning session — establishes wk₀ = 945 on the closed run.
    _feed(
        tracker,
        [
            {
                "type": 2,
                "currentMowBoundary": 1,
                "currentMowProgress": 100,
                "mowingPercentage": 48,
                "subtotalArea": "1.0",
                "mowingWeekArea": "946.0",
                "mowStartType": 0,
                "time": _MORNING_START_MS,
            },
            {
                "type": 2,
                "currentMowBoundary": 1,
                "currentMowProgress": 9906,
                "mowingPercentage": 99,
                "subtotalArea": "118.0",
                "mowingWeekArea": "1063.0",
                "mowStartType": 0,
                "time": _MORNING_END_MS,
            },
        ],
    )
    morning_close = tracker.process_vehicle_state(VS_DOCKED_CHARGING)
    assert [e.kind for e in morning_close] == [EVENT_RUN_FINISHED]
    assert tracker.state == STATE_COMPLETED
    assert tracker.current_run["wk0"] == 945.0

    tracker.process_vehicle_state(VS_MOWING)
    caplog.clear()

    # Afternoon packets — `wk` shifted +2 m² so the closed-run anchor
    # sees a drift of exactly 2 m² on the first strict-progress packet.
    with caplog.at_level(
        logging.WARNING, logger="custom_components.navimow.run_tracker"
    ):
        afternoon_events = _feed(
            tracker,
            [
                {
                    "type": 2,
                    "currentMowBoundary": 3,
                    "currentMowProgress": 107,
                    "mowingPercentage": 65,
                    "subtotalArea": "121.17",
                    "mowingWeekArea": "1068.17",  # drift: wk₀ implied = 947
                    "mowStartType": 1,
                    "time": _AFTERNOON_START_MS,
                },
                {
                    "type": 2,
                    "currentMowBoundary": 3,
                    "currentMowProgress": 10000,
                    "mowingPercentage": 100,
                    "subtotalArea": "244.34",
                    "mowingWeekArea": "1191.34",  # 1191.34 − 244.34 = 947
                    "mowStartType": 1,
                    "time": _AFTERNOON_END_MS,
                },
            ],
        )
        afternoon_close = tracker.process_vehicle_state(VS_DOCKED_CHARGING)

    # Session opens (pre-HARD-06 the first packet would have been
    # silently dropped by layer 3 and no session would ever have
    # started).
    assert [e.kind for e in afternoon_events] == [EVENT_RUN_STARTED]
    # Session closes with the correct `session_area` = 244.34 − 121.17.
    assert [e.kind for e in afternoon_close] == [EVENT_RUN_FINISHED]
    assert abs(afternoon_close[0].payload["session_area"] - 123.17) < 1e-9

    # Exactly one deviation observation — the first strict-progress
    # afternoon packet, measured against the closed-run wk₀ = 945.
    # The second afternoon packet sits under the freshly re-anchored
    # session wk₀ = 947 and is within tolerance, so the streak resets.
    assert tracker.counters["invariant_deviations_observed"] == 1
    # Streak stayed well below the WARN threshold — 5 consecutive
    # deviations against a dead anchor are structurally impossible on
    # the post-close path.
    assert 1 < INVARIANT_DEVIATION_STREAK_TO_WARN
    assert tracker._invariant_deviation_streak == 0

    # No WARN emitted.
    warns = [rec for rec in caplog.records if rec.levelno >= logging.WARNING]
    assert warns == [], [r.getMessage() for r in warns]

    # No layer_3 key in drops after HARD-06.
    assert "layer_3" not in tracker.drops
