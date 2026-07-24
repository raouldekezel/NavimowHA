"""FEAT-05 step (b) — RunTracker state machine + layers 2-3 guards.

The tracker is a pure module (zero HA imports); every test drives it
directly. Fixtures come from the committed diag logs where possible —
those two runs cover almost every real-world path (start, boundary
change, invariant holds, no drops); the remaining paths that are not in
the corpus (mid-run pause/resume, sustained interruption, ISO-Monday
rollover, vs=8 firmware transient) are synthesised as spelt out in the
Fable brief on #43.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from custom_components.navimow.location import parse_location_type_2
from custom_components.navimow.run_tracker import (
    EVENT_RUN_FINISHED,
    EVENT_RUN_STARTED,
    INTERRUPT_SUSTAIN_SECONDS,
    INVARIANT_DEVIATION_STREAK_TO_WARN,
    RESET_SUB_CEILING,
    RESULT_COMPLETED,
    RESULT_INTERRUPTED,
    STATE_IDLE,
    STATE_PAUSED_DOCKED,
    STATE_RUNNING,
    VS_DOCKED_CHARGING,
    VS_DOCKED_IDLE,
    VS_MAPPING,
    VS_MOWING,
    VS_STOPPED,
    VS_TRANSIENT,
    WK_REGRESSION_STREAK_TO_WARN,
    RunTracker,
)

REPO_ROOT = Path(__file__).parent.parent
FIXTURE_2026_05_25 = (
    REPO_ROOT
    / "docs/diag/2026-05-25_feat-02_multizone-run"
    / "01_multizone-run-type-2-payloads.mqtt.log"
)
FIXTURE_2026_07_03 = (
    REPO_ROOT
    / "docs/diag/2026-07-03_bug-07_progression-battery-trace"
    / "04_location-type2.mqtt.log"
)


def _load_type_2_items(path: Path) -> list[dict]:
    """Extract type-2 payload items from a `/location` mqtt.log slice.

    Filters to `paho-mqtt` lines so a log that also contains a
    `MainThread` duplicate (2026-07-03) yields one item per packet.
    """
    pattern = re.compile(r"payload=(\[.*\])$")
    items: list[dict] = []
    for line in path.read_text().splitlines():
        if "paho-mqtt" not in line:
            continue
        match = pattern.search(line.strip())
        if not match:
            continue
        arr = json.loads(match.group(1))
        for item in arr:
            if item.get("type") == 2:
                items.append(item)
    return items


class FakeClock:
    """Manually-advanced monotonic clock for the sustained-60 s tests."""

    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _feed(tracker: RunTracker, items: list[dict]) -> list:
    events = []
    for item in items:
        parsed = parse_location_type_2(item)
        events.extend(tracker.process_type2(parsed))
    return events


# --------------------------------------------------------------------- #
# 1. Full replay — 2026-05-25 log (two runs, mst=0 → mst=1)             #
# --------------------------------------------------------------------- #


def test_full_replay_2026_05_25_yields_two_runs_with_zone_crossing() -> None:
    """The committed log's 34 packets tell a two-run story:

    - Morning run (`mst=0`): a `boundary=0` sentinel + 17 zone-3
      packets ending at `sub=21.76` (the log truncates well before
      that run's real end).
    - Afternoon run (`mst=1`): starts on the late-delivered `boundary=1`
      packet (`sub=0.39`, subtotal reset — closes the morning run
      INTERRUPTED), continues through the boundary=1 → boundary=3
      crossing (`sub 227.82` → `229.11`, no reset), and ends the log
      still RUNNING at `sub=245.87`.
    """
    tracker = RunTracker()
    items = _load_type_2_items(FIXTURE_2026_05_25)
    assert len(items) == 34, len(items)

    events = _feed(tracker, items)

    starts = [e for e in events if e.kind == EVENT_RUN_STARTED]
    finishes = [e for e in events if e.kind == EVENT_RUN_FINISHED]
    assert len(starts) == 2, [e.payload for e in events]
    assert len(finishes) == 1
    # Morning run: opened on the sentinel, closed as interrupted at the
    # reset packet, `mst=0`, visited exactly zone 3.
    assert starts[0].payload["mow_start_type"] == 0
    assert finishes[0].payload["result"] == RESULT_INTERRUPTED
    assert finishes[0].payload["mow_start_type"] == 0
    morning_zones = finishes[0].payload["zones"]
    assert [z["boundary_id"] for z in morning_zones] == [3]
    assert morning_zones[0]["sub_entry"] == 1.67
    assert morning_zones[0]["sub_exit"] == 21.76
    # Afternoon run: still RUNNING, visited zone 1 then zone 3 (a real
    # in-run crossing — the `cmp` reset from 9901 → 0 at packet 21).
    assert starts[1].payload["mow_start_type"] == 1
    assert tracker.state == STATE_RUNNING
    afternoon_zones = tracker.current_run["zones"]
    assert [z["boundary_id"] for z in afternoon_zones] == [1, 3]
    assert afternoon_zones[0]["sub_entry"] == 0.39
    assert afternoon_zones[0]["sub_exit"] == 227.82
    assert afternoon_zones[1]["sub_entry"] == 229.11
    assert tracker.current_run["last_sub"] == 245.87
    # No layer-2 or layer-3 drops on this well-formed corpus.
    assert tracker.drops == {"pending_reset_holds": 0}


# --------------------------------------------------------------------- #
# 2. Full replay — 2026-07-03 log (single run, single zone)             #
# --------------------------------------------------------------------- #


def test_full_replay_2026_07_03_yields_one_running_run() -> None:
    """The 2026-07-03 trace stops before the operator's manual dock,
    so the replay ends with the run still RUNNING. Nothing to close
    from packet content alone — interruption comes from a subsequent
    vs=1 sustained 60 s, exercised separately below.
    """
    tracker = RunTracker()
    items = _load_type_2_items(FIXTURE_2026_07_03)
    assert len(items) == 47, len(items)

    events = _feed(tracker, items)

    starts = [e for e in events if e.kind == EVENT_RUN_STARTED]
    finishes = [e for e in events if e.kind == EVENT_RUN_FINISHED]
    assert len(starts) == 1
    assert len(finishes) == 0
    assert starts[0].payload["mow_start_type"] == 1
    assert tracker.state == STATE_RUNNING
    zones = tracker.current_run["zones"]
    assert [z["boundary_id"] for z in zones] == [1]
    assert zones[0]["sub_entry"] == 2.6
    assert zones[0]["sub_exit"] == 109.78
    assert tracker.drops == {"pending_reset_holds": 0}


# --------------------------------------------------------------------- #
# 3. BUG-10 (2026-07-05 / #58) — layer 2 is observability, not blocking #
# --------------------------------------------------------------------- #


def test_wk_regression_counts_and_logs_but_does_not_drop() -> None:
    """After BUG-10, a `wk` regression against the cursor is logged at
    DEBUG and counted in `counters["wk_regressions_observed"]` but the
    packet flows through the rest of the state machine.
    """
    tracker = RunTracker()
    # Predecessor: sub=200, wk=338, wk₀=138.
    _feed(
        tracker,
        [
            {
                "type": 2,
                "currentMowBoundary": 1,
                "mowingPercentage": 42,
                "subtotalArea": "200.0",
                "mowingWeekArea": "338.0",
                "mowStartType": 1,
                "time": 1_000_000_000_000,
            }
        ],
    )
    baseline_wk0 = tracker.current_run["wk0"]
    assert baseline_wk0 == 138.0

    # A continuation packet with a wk regression but sub still advancing:
    # `sub=201` (progress), `wk=337.5` (regression of 0.5). Not a reset;
    # layer 3 still passes (|337.5 - 201 - 138| = 1.5 > 0.5 — actually
    # fails), so wire the anchor to make layer 3 pass.
    events = _feed(
        tracker,
        [
            {
                "type": 2,
                "currentMowBoundary": 1,
                "mowingPercentage": 43,
                "subtotalArea": "200.0",  # equal — not is_reset
                "mowingWeekArea": "337.9",  # regression by 0.1, layer 3 offset 0.1
                "mowStartType": 1,
                "time": 1_000_000_060_000,
            }
        ],
    )
    assert events == []
    assert tracker.state == STATE_RUNNING
    # Counter increments; no packet dropped.
    assert tracker.counters["wk_regressions_observed"] == 1
    assert tracker.drops == {"pending_reset_holds": 0}


def test_bug_10_sunday_wk_reset_run_opens_via_fresh_reset_path() -> None:
    """The 2026-07-05 scenario (issue #58) with the layer-2 guard gone.

    Close-state cursors at `wk = 1189.34` from yesterday's completed
    run; today's Sunday session starts with `sub ≈ 0` climbing from a
    small value. Layer 2 no longer drops those packets — the first
    fresh-reset packet (`sub < RESET_SUB_CEILING`) opens a new run via
    the normal reset path, subsequent packets continue it, the
    regression counter increments exactly once (on the first Sunday
    packet), and no packet is dropped.
    """
    tracker = RunTracker()

    # Simulate yesterday's completed run leaving the cursor at 1189.34.
    tracker.state = STATE_IDLE
    tracker._last_accepted_wk = 1189.34
    tracker._last_accepted_time_ms = 1_000_000_000_000
    tracker.current_run = {
        "start_time": 1_000_000_000_000 - 10_000_000,
        "mow_start_type": 0,
        "wk0": 1000.0,
        "last_time": 1_000_000_000_000,
        "last_sub": 180.0,
        "last_wk": 1189.34,
        "last_mp": 99,
        "zones": [],
    }

    # Today (Sunday morning): first three packets climbing from ~0 —
    # the shape the tracker would have seen if DEBUG had been on before
    # the run started.
    events = _feed(
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
            {
                "type": 2,
                "currentMowBoundary": 1,
                "mowingPercentage": 38,
                "subtotalArea": "91.3",
                "mowingWeekArea": "91.3",
                "mowStartType": 0,
                "time": 1_000_100_120_000,
            },
        ],
    )

    # Exactly one `run_started` (fresh reset path from a resting IDLE with a reference);
    # no `run_finished` (nothing was open to close on the state side).
    kinds = [e.kind for e in events]
    assert kinds == [EVENT_RUN_STARTED], kinds
    assert tracker.state == STATE_RUNNING
    # Counter fired once — the first Sunday packet's wk (0.4) regressed
    # against the cursor (1189.34). Subsequent packets advance the new
    # cursor so no further regressions.
    assert tracker.counters["wk_regressions_observed"] == 1
    # Zero packets dropped.
    assert tracker.drops == {"pending_reset_holds": 0}
    # New run properly anchored on the fresh Sunday morning.
    assert tracker.current_run["last_sub"] == 91.3
    assert tracker.current_run["last_mp"] == 38
    # wk₀ anchored on the first accepted Sunday packet: 0.4 − 0.4 = 0.
    assert tracker.current_run["wk0"] == 0.0


# --------------------------------------------------------------------- #
# 4. Pause/resume bracket — vs=2 dock mid-run, type-2 resumes           #
# --------------------------------------------------------------------- #


def test_synthetic_vs_2_pause_and_resume_bracket() -> None:
    """Not directly committed anywhere. Simulate: run open → vs=2
    (charging) → PAUSED_DOCKED; then a fresh type-2 with `sub ≥ last`
    → RUNNING (same run), no `run_finished` or `run_reopened` in
    between.
    """
    clock = FakeClock()
    tracker = RunTracker(clock=clock)

    _feed(
        tracker,
        [
            {
                "type": 2,
                "currentMowBoundary": 1,
                "currentMowProgress": 500,
                "mowingPercentage": 5,
                "subtotalArea": "10.0",
                "mowingWeekArea": "10.0",
                "mowStartType": 1,
                "time": 1_000_000_000_000,
            }
        ],
    )
    assert tracker.state == STATE_RUNNING

    # Enter dock while charging — timer must NOT arm (recharge coming).
    tracker.process_vehicle_state(VS_DOCKED_CHARGING)
    assert tracker.state == STATE_PAUSED_DOCKED
    assert tracker._interrupt_timer_started_at is None
    # Advance clock past 60 s to prove no timer fires.
    clock.advance(120)
    assert tracker.tick() == []
    assert tracker.state == STATE_PAUSED_DOCKED

    # Resume: the robot physically leaves the dock (departure evidence
    # vs=4, HARD-19 §3 #120), then a fresh type-2 with sub ≥ last continues
    # the same run.
    tracker.process_vehicle_state(VS_MOWING)
    events = _feed(
        tracker,
        [
            {
                "type": 2,
                "currentMowBoundary": 1,
                "currentMowProgress": 2000,
                "mowingPercentage": 20,
                "subtotalArea": "20.0",
                "mowingWeekArea": "20.0",
                "mowStartType": 1,
                "time": 1_000_000_120_000,
            }
        ],
    )
    assert events == []
    assert tracker.state == STATE_RUNNING


# --------------------------------------------------------------------- #
# 5. False interruption + reopen                                        #
# --------------------------------------------------------------------- #


def test_vs_1_sustained_interrupts_then_new_session_on_continuation() -> None:
    clock = FakeClock()
    tracker = RunTracker(clock=clock)

    _feed(
        tracker,
        [
            {
                "type": 2,
                "currentMowBoundary": 1,
                "mowingPercentage": 5,
                "subtotalArea": "10.0",
                "mowingWeekArea": "10.0",
                "time": 1_000_000_000_000,
            }
        ],
    )
    # Dock to vs=1 → arm the timer.
    tracker.process_vehicle_state(VS_DOCKED_IDLE)
    assert tracker.state == STATE_PAUSED_DOCKED
    assert tracker._interrupt_timer_started_at == 0.0

    # tick at 59 s — still holding.
    clock.advance(59)
    assert tracker.tick() == []
    # tick at 65 s — sustained-60 s check fires.
    clock.advance(6)
    events = tracker.tick()
    assert [e.kind for e in events] == [EVENT_RUN_FINISHED]
    assert events[0].payload["result"] == RESULT_INTERRUPTED
    assert tracker.state == STATE_IDLE

    # FEAT-06 (#54): a fresh accepted type-2 after a close opens a
    # NEW session (not a reopen). start_time = this packet's time,
    # sub₀ = this packet's sub. Layer 3 still gates continuity.
    events = _feed(
        tracker,
        [
            {
                "type": 2,
                "currentMowBoundary": 1,
                "mowingPercentage": 6,
                "subtotalArea": "12.0",
                "mowingWeekArea": "12.0",
                "time": 1_000_000_200_000,
            }
        ],
    )
    assert [e.kind for e in events] == [EVENT_RUN_STARTED]
    assert tracker.state == STATE_RUNNING
    assert tracker.current_run["start_time"] == 1_000_000_200_000
    assert tracker.current_run["sub0"] == 12.0
    assert tracker.current_run["last_sub"] == 12.0


# --------------------------------------------------------------------- #
# 6. vs=3 (VS_STOPPED) is inert — never interrupts (HARD-19 §2, #120)    #
# --------------------------------------------------------------------- #


def test_vs_3_stopped_is_inert_never_interrupts() -> None:
    """HARD-19 §2 (#120) inverts the old `vs=3` sustained-interrupt path.
    MAP-01 (2026-07-07) established `vs=3` (VS_STOPPED) as a generic
    stopped state a real user pause off-dock emits, so it is no longer
    treated as docked: it does not move the open run to PAUSED_DOCKED and
    does not arm the sustained-interrupt timer. The run stays RUNNING and
    open indefinitely on `vs=3` alone — a later real dock or departure
    resolves it.
    """
    clock = FakeClock()
    tracker = RunTracker(clock=clock)

    _feed(
        tracker,
        [
            {
                "type": 2,
                "mowingPercentage": 5,
                "subtotalArea": "10.0",
                "mowingWeekArea": "10.0",
                "time": 1_000_000_000_000,
            }
        ],
    )
    tracker.process_vehicle_state(VS_STOPPED)
    assert tracker.state == STATE_RUNNING  # inert — not PAUSED_DOCKED

    clock.advance(INTERRUPT_SUSTAIN_SECONDS + 1)
    assert tracker.tick() == []  # no timer armed → no close
    assert tracker.state == STATE_RUNNING


# --------------------------------------------------------------------- #
# 7. vs=6 (VS_MAPPING) is inert — never holds, never times out          #
# --------------------------------------------------------------------- #


def test_vs_6_mapping_is_inert_run_stays_running() -> None:
    """HARD-19 §2 arbitration 4 (#120): vs=6 (VS_MAPPING) is inert —
    location-agnostic (a user-initiated remap runs off-dock), evidence of
    nothing. Feeding it to an open run neither moves it to PAUSED_DOCKED nor
    arms the sustained timer; the run stays RUNNING indefinitely on vs=6
    alone. (Before arbitration 4, vs=6 "held" the run in PAUSED_DOCKED.)
    """
    clock = FakeClock()
    tracker = RunTracker(clock=clock)

    _feed(
        tracker,
        [
            {
                "type": 2,
                "mowingPercentage": 5,
                "subtotalArea": "10.0",
                "mowingWeekArea": "10.0",
                "time": 1_000_000_000_000,
            }
        ],
    )
    tracker.process_vehicle_state(VS_MAPPING)
    assert tracker.state == STATE_RUNNING  # inert — not PAUSED_DOCKED
    assert tracker._interrupt_timer_started_at is None

    clock.advance(3600)  # 1 h — no timer armed, so nothing fires
    assert tracker.tick() == []
    assert tracker.state == STATE_RUNNING


# --------------------------------------------------------------------- #
# 8. HARD-06 (#62) — mid-run wk reset stays alive, deviation WARN at 5  #
# --------------------------------------------------------------------- #


def test_mid_run_wk_reset_run_stays_open_deviation_warns_at_five(caplog) -> None:
    """Post-HARD-06 (#62): a firmware `wk` reset while a run is open is
    now OBSERVED, not blocked. The invariant deviation counter climbs,
    the streak WARN fires exactly once at
    `INVARIANT_DEVIATION_STREAK_TO_WARN` consecutive observations
    against the LIVE anchor, the accumulator keeps advancing on `sub`,
    the sustained-timer closes the run normally via `vs`, and
    `session_area` (`last_sub − sub₀`) is correct because it is
    `sub`-only — unaffected by the `wk` collapse.

    Pre-HARD-06 this scenario was the bounded-tail residual (layer 3
    rejected every post-reset packet, `session_area` was still correct
    at close but the counter was silent). #62 turned the counter into
    the visible signal and removed the residual entirely.
    """
    clock = FakeClock()
    tracker = RunTracker(clock=clock)

    # Open a healthy run: sub=50, wk=100, wk₀=50.
    _feed(
        tracker,
        [
            {
                "type": 2,
                "currentMowBoundary": 1,
                "mowingPercentage": 40,
                "subtotalArea": "50.0",
                "mowingWeekArea": "100.0",
                "mowStartType": 1,
                "time": 1_000_000_000_000,
            }
        ],
    )
    assert tracker.current_run["wk0"] == 50.0
    assert tracker.current_run["sub0"] == 50.0
    caplog.clear()

    # Feed exactly INVARIANT_DEVIATION_STREAK_TO_WARN consecutive
    # post-reset packets: `sub` advances by 3 each 60 s (normal run
    # cadence), `wk` climbs from a small value after the firmware reset
    # (so each deviation |wk_i − sub_i − 50| stays roughly at 97, well
    # above tolerance). Each packet is observed, counter climbs, streak
    # climbs, and the WARN fires exactly at the fifth.
    with caplog.at_level(
        logging.WARNING, logger="custom_components.navimow.run_tracker"
    ):
        for i in range(INVARIANT_DEVIATION_STREAK_TO_WARN):
            _feed(
                tracker,
                [
                    {
                        "type": 2,
                        "currentMowBoundary": 1,
                        "mowingPercentage": 42 + i,
                        "subtotalArea": str(52.0 + i * 3.0),
                        "mowingWeekArea": str(5.0 + i * 3.0),
                        "mowStartType": 1,
                        "time": 1_000_000_060_000 + i * 60_000,
                    }
                ],
            )

    # Counter and streak reached the threshold; accumulator advanced;
    # `wk₀` still anchored at the run's original value.
    assert tracker.state == STATE_RUNNING
    assert (
        tracker.counters["invariant_deviations_observed"]
        == INVARIANT_DEVIATION_STREAK_TO_WARN
    )
    assert tracker._invariant_deviation_streak == INVARIANT_DEVIATION_STREAK_TO_WARN
    assert (
        tracker.current_run["last_sub"]
        == 52.0 + (INVARIANT_DEVIATION_STREAK_TO_WARN - 1) * 3.0
    )
    assert tracker.current_run["wk0"] == 50.0
    # `wk` regressed once on the first collapsed packet (100 → 5), then
    # subsequent packets advanced against a cursor that already sits at
    # the collapsed value — no further wk regressions, and one
    # observation short of any WARN there.
    assert tracker.counters["wk_regressions_observed"] == 1

    # Exactly one WARNING emitted — the deviation streak WARN. The
    # wk-regression path is well below its own threshold (streak = 1).
    warns = [rec for rec in caplog.records if rec.levelno >= logging.WARNING]
    assert len(warns) == 1, [r.getMessage() for r in warns]
    assert "invariant deviations" in warns[0].getMessage()

    # No layer_3 key in drops after HARD-06.
    assert "layer_3" not in tracker.drops
    assert tracker.drops == {"pending_reset_holds": 0}

    # Dock on vs=1 → sustained-60 s timer closes the run cleanly.
    # The last accepted `mp` climbed to 42 + 4 = 46 (< threshold), so
    # the close is labelled INTERRUPTED.
    tracker.process_vehicle_state(VS_DOCKED_IDLE)
    clock.advance(INTERRUPT_SUSTAIN_SECONDS + 1)
    close_events = tracker.tick()
    assert [e.kind for e in close_events] == [EVENT_RUN_FINISHED]
    payload = close_events[0].payload
    assert payload["result"] == RESULT_INTERRUPTED
    # `session_area` is `sub`-only (last_sub − sub₀) — the `wk` reset
    # never touched it. sub₀ = 50, last_sub = 52 + (5 − 1) × 3 = 64.
    assert payload["session_area"] == 14.0
    assert tracker.state == STATE_IDLE

    # Next session (fresh reset below the ceiling) opens cleanly with
    # its own anchor.
    new_events = _feed(
        tracker,
        [
            {
                "type": 2,
                "currentMowBoundary": 1,
                "mowingPercentage": 1,
                "subtotalArea": "0.5",
                "mowingWeekArea": "0.5",
                "mowStartType": 1,
                "time": 1_000_000_060_000
                + INVARIANT_DEVIATION_STREAK_TO_WARN * 60_000
                + 120_000,
            }
        ],
    )
    assert [e.kind for e in new_events] == [EVENT_RUN_STARTED]
    assert tracker.state == STATE_RUNNING
    assert tracker.current_run["wk0"] == 0.0


# --------------------------------------------------------------------- #
# 9. vs=8 transient + boundary=0 sentinel                               #
# --------------------------------------------------------------------- #


def test_vs_8_transient_is_ignored() -> None:
    tracker = RunTracker()
    _feed(
        tracker,
        [
            {
                "type": 2,
                "mowingPercentage": 5,
                "subtotalArea": "10.0",
                "mowingWeekArea": "10.0",
                "time": 1_000_000_000_000,
            }
        ],
    )
    tracker.process_vehicle_state(VS_MOWING)
    assert tracker.state == STATE_RUNNING
    assert tracker.vehicle_state == VS_MOWING

    # vs=8 must not disturb the tracker.
    events = tracker.process_vehicle_state(VS_TRANSIENT)
    assert events == []
    assert tracker.state == STATE_RUNNING
    assert tracker.vehicle_state == VS_MOWING  # unchanged


def test_boundary_zero_updates_run_but_not_zones() -> None:
    tracker = RunTracker()
    _feed(
        tracker,
        [
            {
                "type": 2,
                "currentMowBoundary": 0,  # BUG-06 sentinel
                "currentMowProgress": 0,
                "mowingPercentage": 0,
                "subtotalArea": "0.0",
                "mowingWeekArea": "0.0",
                "mowStartType": 0,
                "time": 1_000_000_000_000,
            },
            {
                "type": 2,
                "currentMowBoundary": 3,
                "currentMowProgress": 500,
                "mowingPercentage": 2,
                "subtotalArea": "5.0",
                "mowingWeekArea": "5.0",
                "mowStartType": 0,
                "time": 1_000_000_060_000,
            },
        ],
    )
    # Run opened on the sentinel and continues under boundary 3.
    assert tracker.state == STATE_RUNNING
    zones = tracker.current_run["zones"]
    assert [z["boundary_id"] for z in zones] == [3]
    assert zones[0]["sub_entry"] == 5.0


# --------------------------------------------------------------------- #
# 10. Run completes when mp ≥ threshold and the robot is docked         #
# --------------------------------------------------------------------- #


def test_mp_100_plus_dock_closes_run_completed() -> None:
    """BUG-09 revised the completion criterion: `mp ≥ 99` alone is not
    enough; the robot must also be docked (`vs ∈ {1, 2, 3}`). A run
    that reaches `mp = 100` mid-mow stays open until the robot docks.
    """
    tracker = RunTracker()
    events = _feed(
        tracker,
        [
            {
                "type": 2,
                "currentMowBoundary": 1,
                "mowingPercentage": 90,
                "subtotalArea": "180.0",
                "mowingWeekArea": "180.0",
                "mowStartType": 1,
                "time": 1_000_000_000_000,
            },
            {
                "type": 2,
                "currentMowBoundary": 1,
                "mowingPercentage": 100,
                "subtotalArea": "200.0",
                "mowingWeekArea": "200.0",
                "mowStartType": 1,
                "time": 1_000_000_060_000,
            },
        ],
    )
    # mp=100 with no vs update — the run must still be RUNNING.
    assert [e for e in events if e.kind == EVENT_RUN_FINISHED] == []
    assert tracker.state == STATE_RUNNING

    # Dock arrival on charging (vs=2) triggers the completion close.
    events.extend(tracker.process_vehicle_state(VS_DOCKED_CHARGING))
    finishes = [e for e in events if e.kind == EVENT_RUN_FINISHED]
    assert len(finishes) == 1
    assert finishes[0].payload["result"] == RESULT_COMPLETED
    assert tracker.state == STATE_IDLE


# --------------------------------------------------------------------- #
# 11. Snapshot / restore round-trip                                     #
# --------------------------------------------------------------------- #


def test_snapshot_restore_round_trip() -> None:
    source = RunTracker()
    _feed(
        source,
        [
            {
                "type": 2,
                "currentMowBoundary": 1,
                "mowingPercentage": 5,
                "subtotalArea": "10.0",
                "mowingWeekArea": "10.0",
                "mowStartType": 1,
                "time": 1_000_000_000_000,
            }
        ],
    )
    snap = source.snapshot()

    restored = RunTracker()
    assert restored.restore(snap) is True
    assert restored.state == STATE_RUNNING
    assert restored.current_run["last_sub"] == 10.0
    assert restored.drops == {"pending_reset_holds": 0}
    assert restored.counters == {
        "wk_regressions_observed": 0,
        "invariant_deviations_observed": 0,
        # HARD-18 (#117): two additional observability counters,
        # defaulted to 0 by `restore()` when absent from the snapshot.
        "strict_progress_rejections": 0,
        "aborted_starts_committed": 0,
    }
    # Feed a fresh continuation packet — invariant still holds against
    # the restored wk₀.
    events = _feed(
        restored,
        [
            {
                "type": 2,
                "currentMowBoundary": 1,
                "mowingPercentage": 10,
                "subtotalArea": "20.0",
                "mowingWeekArea": "20.0",
                "mowStartType": 1,
                "time": 1_000_000_060_000,
            }
        ],
    )
    assert events == []  # no new events, continuation
    assert restored.current_run["last_sub"] == 20.0


def test_restore_rejects_wrong_version() -> None:
    tracker = RunTracker()
    assert tracker.restore({"version": 99}) is False


# --------------------------------------------------------------------- #
# 12. Coordinator wiring — smoke                                        #
# --------------------------------------------------------------------- #


def test_coordinator_instantiates_run_tracker() -> None:
    """Regression guard — the coordinator's __init__ wires up a
    RunTracker and forwards the accepted /location packets to it. The
    coordinator itself is exercised in the FEAT-01/02/05a suites; this
    check just verifies the seam exists so a future refactor can't
    silently drop it.
    """
    from unittest.mock import MagicMock

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
    coordinator._last_accepted_time_type1 = None
    coordinator._last_accepted_time_type2 = None
    coordinator._type1_drop_streak = 0
    coordinator._type2_drop_streak = 0
    coordinator._build_data = MagicMock(return_value={})
    coordinator.async_set_updated_data = MagicMock()
    coordinator.run_tracker = RunTracker()

    # FEAT-05 (c) persistence + history attributes.

    coordinator.history = []

    coordinator.last_finished_run = None

    coordinator._store = None

    coordinator._last_store_save_monotonic = 0.0

    coordinator.handle_location_item(
        {
            "type": 2,
            "currentMowBoundary": 1,
            "mowingPercentage": 5,
            "subtotalArea": "10.0",
            "mowingWeekArea": "10.0",
            "mowStartType": 1,
            "time": 1_000_000_000_000,
        }
    )
    assert coordinator.run_tracker.state == STATE_RUNNING


# --------------------------------------------------------------------- #
# 13. B1 (#49 review) — trailing echo packets don't loop reopen/close   #
# --------------------------------------------------------------------- #


def test_completed_run_ignores_trailing_echo_packets() -> None:
    """After a run closes at `mp=100`, further packets with identical
    content but only `time` advancing (a plausible stream-tail residue)
    must NOT spawn a phantom new session (FEAT-06 / #54) — the same
    `_has_strict_progress` gate that used to guard the reopen path now
    guards new-session opens: an echo carries no progress on `sub`, so
    it stays inert.
    """
    tracker = RunTracker()

    # Open a run at mp=100 and dock — BUG-09 close path.
    _feed(
        tracker,
        [
            {
                "type": 2,
                "currentMowBoundary": 1,
                "mowingPercentage": 100,
                "subtotalArea": "200.0",
                "mowingWeekArea": "200.0",
                "mowStartType": 1,
                "time": 1_000_000_000_000,
            }
        ],
    )
    tracker.process_vehicle_state(VS_DOCKED_CHARGING)
    assert tracker.state == STATE_IDLE

    # Three echo packets — same sub/mp, only time advancing.
    echo_events = _feed(
        tracker,
        [
            {
                "type": 2,
                "currentMowBoundary": 1,
                "mowingPercentage": 100,
                "subtotalArea": "200.0",
                "mowingWeekArea": "200.0",
                "mowStartType": 1,
                "time": 1_000_000_030_000 + i * 30_000,
            }
            for i in range(3)
        ],
    )
    assert echo_events == []
    assert tracker.state == STATE_IDLE


def test_interrupted_run_reopens_only_on_strict_progress() -> None:
    """Mirror of the above on the INTERRUPTED path — an echo after a
    sustained-60 s close must not reopen.
    """
    clock = FakeClock()
    tracker = RunTracker(clock=clock)
    _feed(
        tracker,
        [
            {
                "type": 2,
                "currentMowBoundary": 1,
                "mowingPercentage": 5,
                "subtotalArea": "10.0",
                "mowingWeekArea": "10.0",
                "mowStartType": 1,
                "time": 1_000_000_000_000,
            }
        ],
    )
    tracker.process_vehicle_state(VS_DOCKED_IDLE)
    clock.advance(INTERRUPT_SUSTAIN_SECONDS + 1)
    tracker.tick()  # fires INTERRUPTED
    assert tracker.state == STATE_IDLE

    # Echo of the closing packet — same sub/mp, later time. Must NOT
    # reopen.
    events = _feed(
        tracker,
        [
            {
                "type": 2,
                "currentMowBoundary": 1,
                "mowingPercentage": 5,
                "subtotalArea": "10.0",
                "mowingWeekArea": "10.0",
                "mowStartType": 1,
                "time": 1_000_000_200_000,
            }
        ],
    )
    assert events == []
    assert tracker.state == STATE_IDLE


# --------------------------------------------------------------------- #
# 14. B2 (#49 review) — mixed-epoch packet is held pending, not immediate#
# --------------------------------------------------------------------- #


def test_mixed_epoch_packet_does_not_destroy_open_run() -> None:
    """Fable's B2 scenario reproduced: mid-run at `sub=200`, feed one
    packet with fresh `time`, `wk` equal to the last accepted (layer 2
    passes) and stale `sub=150`. The old immediate-reset path would
    close the genuine run and open a phantom; the pending-reset
    mechanism holds the candidate instead — the anomalous packet
    itself never surfaces as a run boundary.
    """
    tracker = RunTracker()

    # Build a healthy run up to sub=200.
    _feed(
        tracker,
        [
            {
                "type": 2,
                "currentMowBoundary": 1,
                "mowingPercentage": 40,
                "subtotalArea": "200.0",
                "mowingWeekArea": "300.0",  # wk₀ = 100
                "mowStartType": 1,
                "time": 2_000_000_000_000,
            }
        ],
    )
    original_start = tracker.current_run["start_time"]

    # Anomalous packet: fresh time, wk unchanged (layer 2 passes), sub
    # regresses well above the ceiling.
    events = _feed(
        tracker,
        [
            {
                "type": 2,
                "currentMowBoundary": 1,
                "mowingPercentage": 40,
                "subtotalArea": "150.0",
                "mowingWeekArea": "300.0",
                "mowStartType": 1,
                "time": 2_000_000_360_000,
            }
        ],
    )
    assert events == []
    assert tracker.state == STATE_RUNNING
    assert tracker.current_run["start_time"] == original_start
    assert tracker.current_run["last_sub"] == 200.0  # unchanged
    assert tracker.drops["pending_reset_holds"] == 1


def test_pending_reset_confirmed_by_coherent_successor() -> None:
    """A pending reset above `RESET_SUB_CEILING` is promoted to a real
    reset iff the next accepted packet coherently continues the
    candidate: strictly later `time`, `sub >` candidate (strict, so an
    identical repeat cannot confirm — Fable review 2 on #49), and the
    candidate's implied anchor (`wk - sub`) matches within the layer-3
    tolerance. Emits `run_finished(interrupted)` + `run_started`
    retroactively, with the new run anchored at the candidate.
    """
    tracker = RunTracker()

    # Healthy run up to sub=200 (wk₀ = 100).
    _feed(
        tracker,
        [
            {
                "type": 2,
                "currentMowBoundary": 1,
                "mowingPercentage": 40,
                "subtotalArea": "200.0",
                "mowingWeekArea": "300.0",
                "mowStartType": 0,
                "time": 2_000_000_000_000,
            }
        ],
    )

    # Candidate (pending): sub=50, wk=550 → implied anchor 500.
    _feed(
        tracker,
        [
            {
                "type": 2,
                "currentMowBoundary": 3,
                "mowingPercentage": 10,
                "subtotalArea": "50.0",
                "mowingWeekArea": "550.0",
                "mowStartType": 1,
                "time": 2_000_000_120_000,
            }
        ],
    )
    assert tracker.state == STATE_RUNNING  # held

    # Confirmation: sub=52 (≥ 50), wk=552 → 552-52=500 = candidate anchor.
    events = _feed(
        tracker,
        [
            {
                "type": 2,
                "currentMowBoundary": 3,
                "mowingPercentage": 11,
                "subtotalArea": "52.0",
                "mowingWeekArea": "552.0",
                "mowStartType": 1,
                "time": 2_000_000_240_000,
            }
        ],
    )
    kinds = [e.kind for e in events]
    assert kinds == [EVENT_RUN_FINISHED, EVENT_RUN_STARTED]
    assert events[0].payload["result"] == RESULT_INTERRUPTED
    # New run anchored at the candidate.
    assert tracker.current_run["start_time"] == 2_000_000_120_000
    assert tracker.current_run["mow_start_type"] == 1
    assert tracker.current_run["wk0"] == 500.0


def test_pending_reset_discarded_when_successor_incoherent() -> None:
    """A pending reset that no coherent successor confirms is
    dropped silently — the open run is preserved. The successor packet
    is then processed as a continuation of the original run.
    """
    tracker = RunTracker()
    _feed(
        tracker,
        [
            {
                "type": 2,
                "currentMowBoundary": 1,
                "mowingPercentage": 40,
                "subtotalArea": "200.0",
                "mowingWeekArea": "300.0",  # wk₀=100
                "mowStartType": 0,
                "time": 2_000_000_000_000,
            }
        ],
    )
    original_start = tracker.current_run["start_time"]

    # Pending candidate: sub=50, wk=550.
    _feed(
        tracker,
        [
            {
                "type": 2,
                "mowingPercentage": 10,
                "subtotalArea": "50.0",
                "mowingWeekArea": "550.0",
                "time": 2_000_000_120_000,
            }
        ],
    )
    assert tracker._pending_reset is not None

    # Incoherent successor: implied anchor 100 ≠ candidate's 500.
    # (Sub advances vs original run, wk advances too — a fresh normal
    # packet on the OLD run.)
    events = _feed(
        tracker,
        [
            {
                "type": 2,
                "mowingPercentage": 41,
                "subtotalArea": "205.0",
                "mowingWeekArea": "305.0",  # 305-205=100 (old anchor)
                "time": 2_000_000_240_000,
            }
        ],
    )
    # Candidate discarded; no events emitted for the pending; old run
    # continues.
    assert events == []
    assert tracker._pending_reset is None
    assert tracker.state == STATE_RUNNING
    assert tracker.current_run["start_time"] == original_start
    assert tracker.current_run["last_sub"] == 205.0


def test_small_sub_regression_is_still_immediate_reset() -> None:
    """A genuine run start with `sub < RESET_SUB_CEILING` (e.g. 0.39 on
    the 2026-05-25 afternoon run) is *not* stashed — the ceiling only
    catches unusually large regressions.
    """
    tracker = RunTracker()
    _feed(
        tracker,
        [
            {
                "type": 2,
                "mowingPercentage": 40,
                "subtotalArea": "200.0",
                "mowingWeekArea": "300.0",
                "time": 2_000_000_000_000,
            }
        ],
    )
    events = _feed(
        tracker,
        [
            {
                "type": 2,
                "mowingPercentage": 0,
                "subtotalArea": "0.39",  # well below RESET_SUB_CEILING
                "mowingWeekArea": "300.39",
                "mowStartType": 1,
                "time": 2_000_000_120_000,
            }
        ],
    )
    kinds = [e.kind for e in events]
    assert kinds == [EVENT_RUN_FINISHED, EVENT_RUN_STARTED]
    assert tracker.drops["pending_reset_holds"] == 0
    # Assert we honoured the ceiling explicitly (guards against a future
    # change accidentally raising the threshold above the observed
    # values).
    assert 0.39 < RESET_SUB_CEILING


# --------------------------------------------------------------------- #
# 15. Minor 1 (#49 review) — tick() arms the interrupt timer            #
# --------------------------------------------------------------------- #


def test_tick_arms_interrupt_timer_when_paused_docked() -> None:
    """A restored tracker (or one whose `_interrupt_timer_started_at`
    was reset by any means) must re-arm the sustained-docked check
    from the first `tick()` call — no fresh `process_vehicle_state`
    required. Otherwise a mid-run HA restart could park an INTERRUPTED
    run in PAUSED_DOCKED forever.
    """
    clock = FakeClock()
    tracker = RunTracker(clock=clock)
    _feed(
        tracker,
        [
            {
                "type": 2,
                "mowingPercentage": 5,
                "subtotalArea": "10.0",
                "mowingWeekArea": "10.0",
                "time": 1_000_000_000_000,
            }
        ],
    )
    tracker.process_vehicle_state(VS_DOCKED_IDLE)
    assert tracker._interrupt_timer_started_at == 0.0
    # Simulate a restart: state stays PAUSED_DOCKED but the timer is
    # gone (as `restore()` sets it to None).
    tracker._interrupt_timer_started_at = None

    # First tick re-arms.
    clock.advance(10)
    events = tracker.tick()
    assert events == []
    assert tracker._interrupt_timer_started_at == 10.0

    # 60 s later, tick fires the interruption.
    clock.advance(INTERRUPT_SUSTAIN_SECONDS + 1)
    events = tracker.tick()
    assert [e.kind for e in events] == [EVENT_RUN_FINISHED]
    assert events[0].payload["result"] == RESULT_INTERRUPTED


# --------------------------------------------------------------------- #
# 16. Pending reset — identical repeat cannot confirm (strict `>`)      #
# --------------------------------------------------------------------- #


def test_identical_repeat_pending_packets_never_confirm() -> None:
    """Fable review 2 on #49: two mixed-epoch packets with identical
    content and successive fresh timestamps used to confirm each other
    under `p_sub >= c_sub`, destroying the live run. With strict
    `p_sub > c_sub` the second poison merely supersedes the stash, the
    run stays intact, and the next genuine packet discards the
    candidate via the anchor check.

    Values lifted from Fable's reproduction: mid-run at `sub=200,
    wk=500 → wk₀=300`, two poisons at `sub=150, wk=500`
    (implied anchor 350), then a genuine `sub=205, wk=505`
    (anchor 300 — original run continues).
    """
    tracker = RunTracker()

    # Healthy run to sub=200, wk₀ = 300.
    _feed(
        tracker,
        [
            {
                "type": 2,
                "currentMowBoundary": 1,
                "mowingPercentage": 40,
                "subtotalArea": "200.0",
                "mowingWeekArea": "500.0",
                "mowStartType": 1,
                "time": 2_000_000_000_000,
            }
        ],
    )
    original_start = tracker.current_run["start_time"]
    assert tracker.current_run["wk0"] == 300.0

    # Two identical poisons with successive `time`.
    poison_a = {
        "type": 2,
        "currentMowBoundary": 1,
        "mowingPercentage": 40,
        "subtotalArea": "150.0",
        "mowingWeekArea": "500.0",
        "mowStartType": 1,
        "time": 2_000_000_120_000,
    }
    poison_b = dict(poison_a, time=2_000_000_240_000)
    events_ab = _feed(tracker, [poison_a, poison_b])
    assert events_ab == []
    assert tracker.state == STATE_RUNNING
    assert tracker.current_run["start_time"] == original_start
    assert tracker.current_run["last_sub"] == 200.0
    # Both stashed (the second superseded the first).
    assert tracker.drops["pending_reset_holds"] == 2

    # Genuine packet from the real stream — discards the candidate,
    # continues the original run without incident.
    events_g = _feed(
        tracker,
        [
            {
                "type": 2,
                "currentMowBoundary": 1,
                "mowingPercentage": 41,
                "subtotalArea": "205.0",
                "mowingWeekArea": "505.0",  # 505 - 205 = 300 = wk₀
                "mowStartType": 1,
                "time": 2_000_000_360_000,
            }
        ],
    )
    assert events_g == []
    assert tracker._pending_reset is None
    assert tracker.state == STATE_RUNNING
    assert tracker.current_run["start_time"] == original_start
    assert tracker.current_run["last_sub"] == 205.0
    # The genuine packet aligns with the original wk₀ (505 − 205 = 300):
    # no invariant deviation is observed. HARD-06 (#62) removed layer 3
    # blocking but the check would have been within tolerance anyway.
    assert tracker.counters["invariant_deviations_observed"] == 0


# --------------------------------------------------------------------- #
# 17. Invariant observation path (HARD-06 / #62 — was layer 3 blocking) #
# --------------------------------------------------------------------- #


def test_invariant_violating_continuation_observed_but_accepted() -> None:
    """HARD-06 (#62) demoted `|wk - sub - wk₀| ≤ INVARIANT_TOLERANCE_M2`
    from blocking guard to observability. A continuation whose deviation
    exceeds the tolerance is now ACCEPTED — the accumulator updates and
    the cursor advances — while the counter increments as the only
    trace.

    Concrete values: open at `sub=10, wk=110 → wk₀=100`. Continuation
    at `sub=20, wk=125` gives `125 - 20 = 105`, off the anchor by 5 m².
    `sub` grows (no reset path), `wk` grows (no wk regression), only the
    anchor invariant deviates.
    """
    tracker = RunTracker()

    _feed(
        tracker,
        [
            {
                "type": 2,
                "currentMowBoundary": 1,
                "mowingPercentage": 5,
                "subtotalArea": "10.0",
                "mowingWeekArea": "110.0",
                "mowStartType": 1,
                "time": 2_000_000_000_000,
            }
        ],
    )
    assert tracker.current_run["wk0"] == 100.0

    events = _feed(
        tracker,
        [
            {
                "type": 2,
                "currentMowBoundary": 1,
                "mowingPercentage": 8,
                "subtotalArea": "20.0",
                "mowingWeekArea": "125.0",  # 125 - 20 = 105, offset 5
                "mowStartType": 1,
                "time": 2_000_000_060_000,
            }
        ],
    )

    assert events == []
    assert tracker.state == STATE_RUNNING
    # Deviation observed, packet accepted: accumulator advances and the
    # cursor follows the wk value.
    assert tracker.counters["invariant_deviations_observed"] == 1
    assert tracker.current_run["last_sub"] == 20.0
    assert tracker._last_accepted_wk == 125.0
    # wk₀ stays anchored once per run (HARD-06 §5): the observability
    # reference is untouched even when the deviation is large.
    assert tracker.current_run["wk0"] == 100.0
    # No layer_3 key in drops after HARD-06.
    assert "layer_3" not in tracker.drops


def test_within_tolerance_continuation_resets_deviation_streak() -> None:
    """After a deviation observation the streak is armed at 1. A
    within-tolerance continuation resets it to 0, ready for the next
    live-anchor drift. Guards the streak-reset semantics of the WARN
    throttle.
    """
    tracker = RunTracker()

    _feed(
        tracker,
        [
            {
                "type": 2,
                "currentMowBoundary": 1,
                "mowingPercentage": 5,
                "subtotalArea": "10.0",
                "mowingWeekArea": "110.0",  # wk₀ = 100
                "mowStartType": 1,
                "time": 2_000_000_000_000,
            }
        ],
    )

    # Deviating packet — streak climbs to 1, counter to 1.
    _feed(
        tracker,
        [
            {
                "type": 2,
                "currentMowBoundary": 1,
                "mowingPercentage": 6,
                "subtotalArea": "15.0",
                "mowingWeekArea": "120.0",  # 120 - 15 = 105, offset 5
                "mowStartType": 1,
                "time": 2_000_000_060_000,
            }
        ],
    )
    assert tracker.counters["invariant_deviations_observed"] == 1
    assert tracker._invariant_deviation_streak == 1

    # Within-tolerance packet — offset 0.3.
    events = _feed(
        tracker,
        [
            {
                "type": 2,
                "currentMowBoundary": 1,
                "mowingPercentage": 8,
                "subtotalArea": "20.0",
                "mowingWeekArea": "120.3",  # 120.3 - 20 = 100.3, offset 0.3
                "mowStartType": 1,
                "time": 2_000_000_120_000,
            }
        ],
    )

    assert events == []
    assert tracker._invariant_deviation_streak == 0
    # Counter is a persistent ledger — the reset touches the streak,
    # not the total.
    assert tracker.counters["invariant_deviations_observed"] == 1
    assert tracker.current_run["last_sub"] == 20.0


# --------------------------------------------------------------------- #
# 18. snapshot() is a point-in-time capture (Opus review 3 on #50)      #
# --------------------------------------------------------------------- #


def test_snapshot_is_isolated_from_subsequent_mutation() -> None:
    """`snapshot()` must return a dict that cannot be mutated by a later
    packet arriving on the HA loop while `Store.async_save` is still
    serialising the payload in an executor. Deep-copy on `current_run`
    guarantees the property; reverting the copy makes this test fail.
    """
    tracker = RunTracker()
    _feed(
        tracker,
        [
            {
                "type": 2,
                "currentMowBoundary": 1,
                "currentMowProgress": 5000,
                "mowingPercentage": 50,
                "subtotalArea": "100.0",
                "mowingWeekArea": "100.0",
                "mowStartType": 1,
                "time": 1_000_000_000_000,
            }
        ],
    )

    snap = tracker.snapshot()
    snap_last_sub = snap["current_run"]["last_sub"]
    snap_zone_count = len(snap["current_run"]["zones"])

    # A later packet mutates the tracker's live current_run in place.
    _feed(
        tracker,
        [
            {
                "type": 2,
                "currentMowBoundary": 3,  # boundary change → new zone
                "currentMowProgress": 500,
                "mowingPercentage": 60,
                "subtotalArea": "120.0",
                "mowingWeekArea": "120.0",
                "mowStartType": 1,
                "time": 1_000_000_060_000,
            }
        ],
    )
    assert tracker.current_run["last_sub"] == 120.0
    assert len(tracker.current_run["zones"]) > snap_zone_count

    # The snapshot must NOT reflect the mutation.
    assert snap["current_run"]["last_sub"] == snap_last_sub
    assert len(snap["current_run"]["zones"]) == snap_zone_count


# --------------------------------------------------------------------- #
# 18. HARD-06 (#62) — benign paths never consult a deviation            #
# --------------------------------------------------------------------- #


def test_benign_paths_do_not_consult_invariant_deviation() -> None:
    """The invariant observer only fires on continuations
    (`STATE_RUNNING` / `STATE_PAUSED_DOCKED`, `is_reset=False`) and on
    post-close new-session opens (at rest in IDLE with a seeded reference,
    strict progress). Every other path — fresh IDLE open, fresh reset
    below the ceiling, pending-reset stash — must not touch the counter,
    so the operator's signal stays specific to actual anchor-drift
    events.
    """
    tracker = RunTracker()

    # IDLE → open. First packet: no prior wk₀; the observer is not
    # consulted at all on this path.
    _feed(
        tracker,
        [
            {
                "type": 2,
                "currentMowBoundary": 1,
                "mowingPercentage": 5,
                "subtotalArea": "10.0",
                "mowingWeekArea": "10.0",
                "mowStartType": 1,
                "time": 1_000_000_000_000,
            }
        ],
    )
    assert tracker.state == STATE_RUNNING
    assert tracker.counters["invariant_deviations_observed"] == 0

    # Fresh reset from RUNNING (sub crashes below `RESET_SUB_CEILING`).
    # The observer is not consulted — the packet takes the reset path.
    events = _feed(
        tracker,
        [
            {
                "type": 2,
                "currentMowBoundary": 1,
                "mowingPercentage": 1,
                "subtotalArea": "0.4",
                "mowingWeekArea": "0.4",
                "mowStartType": 1,
                "time": 1_000_000_060_000,
            }
        ],
    )
    kinds = [e.kind for e in events]
    assert kinds == [EVENT_RUN_FINISHED, EVENT_RUN_STARTED]
    assert tracker.counters["invariant_deviations_observed"] == 0

    # Close via BUG-09 (mp ≥ threshold, vs docked), then a fresh reset
    # from COMPLETED (sub below ceiling) — again the observer stays
    # silent. BUG-14 (#89): threshold is 100, not 99.
    _feed(
        tracker,
        [
            {
                "type": 2,
                "currentMowBoundary": 1,
                "mowingPercentage": 100,
                "subtotalArea": "50.0",
                "mowingWeekArea": "50.0",
                "mowStartType": 1,
                "time": 1_000_000_120_000,
            }
        ],
    )
    tracker.process_vehicle_state(VS_DOCKED_CHARGING)
    assert tracker.state == STATE_IDLE
    assert tracker.counters["invariant_deviations_observed"] == 0

    # Robot leaves the dock (RUN pressed). HARD-18 (#117): vs=4 from a
    # terminal state opens a provisional run and fires run_started here,
    # before any type-2.
    open_events = tracker.process_vehicle_state(VS_MOWING)
    assert [e.kind for e in open_events] == [EVENT_RUN_STARTED]
    # The first type-2 seeds the provisional run (continuation — no
    # second run_started). The invariant observer short-circuits on the
    # still-unseeded wk₀, so it is not consulted on this benign start
    # path (the property this test locks).
    events = _feed(
        tracker,
        [
            {
                "type": 2,
                "currentMowBoundary": 1,
                "mowingPercentage": 1,
                "subtotalArea": "0.5",
                "mowingWeekArea": "0.5",
                "mowStartType": 1,
                "time": 1_000_000_180_000,
            }
        ],
    )
    assert events == []
    assert tracker.counters["invariant_deviations_observed"] == 0


# --------------------------------------------------------------------- #
# 19. HARD-06 (#62) — restore() migrates pre-HARD-06 drops["layer_3"]   #
# --------------------------------------------------------------------- #


def test_restore_of_pre_hard_06_snapshot_defaults_dropped_migration_to_zero() -> None:
    """HARD-14: the pre-BUG-10 / pre-HARD-06 restore migrations were
    retired. A snapshot carrying the legacy ``drops["layer_2"]`` /
    ``drops["layer_3"]`` shape is still accepted (shape-tolerant), but
    those retired keys are no longer folded into the counters — the
    counter values simply default to 0 (cosmetic reset at worst).

    ``pending_reset_holds`` remains a live counter and IS still
    restored from ``drops``.
    """
    tracker = RunTracker()
    legacy_snap = {
        "version": 1,
        "state": STATE_RUNNING,
        "vehicle_state": None,
        "current_run": {
            "start_time": 1_000_000_000_000,
            "mow_start_type": 1,
            "wk0": 100.0,
            "sub0": 10.0,
            "last_time": 1_000_000_060_000,
            "last_sub": 42.5,
            "last_wk": 142.5,
            "last_mp": 40,
            "zones": [],
        },
        "last_accepted_wk": 142.5,
        "last_accepted_time_ms": 1_000_000_060_000,
        # Pre-HARD-06 shape — retired keys still present on disk, but
        # they are NOT migrated any more (HARD-14).
        "drops": {"layer_2": 4, "layer_3": 7, "pending_reset_holds": 3},
        "counters": {},
    }
    assert tracker.restore(legacy_snap) is True
    # HARD-14: retired-key drops are ignored; counters default to 0.
    assert tracker.counters["invariant_deviations_observed"] == 0
    assert tracker.counters["wk_regressions_observed"] == 0
    # `pending_reset_holds` is still restored from `drops` (live counter).
    assert tracker.drops == {"pending_reset_holds": 3}
    # Streak is in-memory only, always re-armed at zero on restore.
    assert tracker._invariant_deviation_streak == 0


def test_restore_of_post_hard_06_snapshot_is_shape_tolerant() -> None:
    """A snapshot taken AFTER HARD-06 has no `drops["layer_3"]` and
    carries `counters["invariant_deviations_observed"]` directly. The
    restore path must accept it as-is, without treating the missing
    `layer_3` key as a zero-count migration overriding the counter.
    """
    tracker = RunTracker()
    modern_snap = {
        "version": 1,
        "state": STATE_RUNNING,
        "vehicle_state": None,
        "current_run": {
            "start_time": 1_000_000_000_000,
            "mow_start_type": 1,
            "wk0": 100.0,
            "sub0": 10.0,
            "last_time": 1_000_000_060_000,
            "last_sub": 42.5,
            "last_wk": 142.5,
            "last_mp": 40,
            "zones": [],
        },
        "last_accepted_wk": 142.5,
        "last_accepted_time_ms": 1_000_000_060_000,
        "drops": {"pending_reset_holds": 4},
        "counters": {
            "wk_regressions_observed": 1,
            "invariant_deviations_observed": 5,
        },
    }
    assert tracker.restore(modern_snap) is True
    assert tracker.counters["invariant_deviations_observed"] == 5
    assert tracker.counters["wk_regressions_observed"] == 1
    assert tracker.drops == {"pending_reset_holds": 4}
