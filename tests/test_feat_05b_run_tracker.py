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
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from custom_components.navimow.location import parse_location_type_2
from custom_components.navimow.run_tracker import (
    EVENT_RUN_FINISHED,
    EVENT_RUN_REOPENED,
    EVENT_RUN_STARTED,
    INTERRUPT_SUSTAIN_SECONDS,
    RESET_SUB_CEILING,
    RESULT_COMPLETED,
    RESULT_INTERRUPTED,
    STATE_COMPLETED,
    STATE_IDLE,
    STATE_INTERRUPTED,
    STATE_PAUSED_DOCKED,
    STATE_RUNNING,
    VS_DOCKED_CHARGING,
    VS_DOCKED_IDLE,
    VS_DOCKED_UNPOWERED,
    VS_MOWING,
    VS_PAUSED,
    VS_TRANSIENT,
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


def _epoch_ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


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
    assert tracker.drops == {"layer_2": 0, "layer_3": 0, "pending_reset_holds": 0}


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
    assert tracker.drops == {"layer_2": 0, "layer_3": 0, "pending_reset_holds": 0}


# --------------------------------------------------------------------- #
# 3. Layer-2 rejects late packet after synthetic live predecessor       #
# --------------------------------------------------------------------- #


def test_synthetic_predecessor_plus_real_late_packet_layer_2_rejects() -> None:
    """The Fable brief's fixture reconstruction: after the tracker has
    accepted a packet stamped at the live findings-timeline (~13:37:25
    UTC, wk ≈ 338), feeding it the *real* 2026-05-25 late packet (fw
    time 12:01:15 UTC, wk 124.15) must be rejected by layer 2 — `wk`
    regresses, and both packets are inside the same ISO week (2026-W22)
    so the Monday exemption does not fire.
    """
    tracker = RunTracker()
    predecessor = {
        "type": 2,
        "currentMowBoundary": 1,
        "currentMowProgress": 4700,
        "mowingPercentage": 42,
        "subtotalArea": "200.0",  # wk₀ = 138 (any consistent value works)
        "mowingWeekArea": "338.0",
        "mowStartType": 1,
        "time": 1779716245448,  # 2026-05-25T13:37:25 UTC
    }
    _feed(tracker, [predecessor])
    assert tracker.state == STATE_RUNNING

    late = {
        "type": 2,
        "currentMowBoundary": 1,
        "currentMowProgress": 16,
        "mowingPercentage": 0,
        "subtotalArea": "0.39",
        "mowingWeekArea": "124.15",
        "mowStartType": 1,
        "time": 1779710475448,  # 2026-05-25T12:01:15 UTC
    }
    events = _feed(tracker, [late])

    assert events == []  # nothing surfaced from a rejected packet
    assert tracker.drops["layer_2"] == 1
    # Cursor untouched by the drop.
    assert tracker.current_run["last_sub"] == 200.0


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

    # Fresh type-2 with sub ≥ last → resume, same run.
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


def test_vs_1_sustained_interrupts_then_reopens_on_continuation() -> None:
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
    assert tracker.state == STATE_INTERRUPTED

    # Fresh type-2 that continues the accumulator (sub ≥ last) → reopen.
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
    assert [e.kind for e in events] == [EVENT_RUN_REOPENED]
    assert tracker.state == STATE_RUNNING
    # Accumulator carries forward: sub advanced 10 → 12, no new run
    # started.
    assert tracker.current_run["last_sub"] == 12.0


# --------------------------------------------------------------------- #
# 6. vs=3 base-loss mid-pause interrupts after 60 s                     #
# --------------------------------------------------------------------- #


def test_vs_3_base_unpowered_interrupts_after_60s() -> None:
    """`vs=3` = docked-but-base-unpowered. The base cannot recharge the
    battery, so this is equivalent to `vs=1` for interruption purposes
    — the BUG-04 pathological case Fable pointed out. The timer must
    arm and fire at the sustained threshold.
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
    tracker.process_vehicle_state(VS_DOCKED_UNPOWERED)
    assert tracker.state == STATE_PAUSED_DOCKED

    clock.advance(INTERRUPT_SUSTAIN_SECONDS + 1)
    events = tracker.tick()
    assert [e.kind for e in events] == [EVENT_RUN_FINISHED]
    assert events[0].payload["result"] == RESULT_INTERRUPTED
    assert tracker.state == STATE_INTERRUPTED


# --------------------------------------------------------------------- #
# 7. vs=6 (explicit user pause) holds indefinitely                      #
# --------------------------------------------------------------------- #


def test_vs_6_paused_holds_docked_indefinitely() -> None:
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
    tracker.process_vehicle_state(VS_PAUSED)
    assert tracker.state == STATE_PAUSED_DOCKED
    # Timer NOT armed — user is in control.
    assert tracker._interrupt_timer_started_at is None

    clock.advance(3600)  # 1 h paused
    assert tracker.tick() == []
    assert tracker.state == STATE_PAUSED_DOCKED

    # Resume via fresh type-2 (sub ≥ last).
    events = _feed(
        tracker,
        [
            {
                "type": 2,
                "mowingPercentage": 6,
                "subtotalArea": "11.0",
                "mowingWeekArea": "11.0",
                "time": 1_000_000_100_000,
            }
        ],
    )
    assert events == []
    assert tracker.state == STATE_RUNNING


# --------------------------------------------------------------------- #
# 8. ISO-Monday rollover — no false rejection                           #
# --------------------------------------------------------------------- #


def test_iso_monday_rollover_exempts_both_wk_layers() -> None:
    tracker = RunTracker()

    sunday = _epoch_ms(datetime(2026, 5, 24, 23, 59, tzinfo=timezone.utc))
    monday = _epoch_ms(datetime(2026, 5, 25, 0, 1, tzinfo=timezone.utc))

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
                "time": sunday,
            }
        ],
    )
    assert tracker.current_run["wk0"] == 50.0

    # After the Monday rollover: wk resets (100 → 5), but the run
    # continues. Layer 2 would normally reject `wk 5 < wk 100`; layer 3
    # would reject `|5 − 52 − 50| = 97`. Both must be exempted, and
    # wk₀ re-anchored from the new packet (5 − 52 = −47).
    events = _feed(
        tracker,
        [
            {
                "type": 2,
                "currentMowBoundary": 1,
                "mowingPercentage": 42,
                "subtotalArea": "52.0",
                "mowingWeekArea": "5.0",
                "mowStartType": 1,
                "time": monday,
            }
        ],
    )
    assert events == []  # no `run_finished`, run still open
    assert tracker.state == STATE_RUNNING
    assert tracker.drops == {"layer_2": 0, "layer_3": 0, "pending_reset_holds": 0}
    assert tracker.current_run["wk0"] == -47.0  # re-anchored


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
# 10. Run completes at mp=100                                           #
# --------------------------------------------------------------------- #


def test_mp_100_closes_run_completed() -> None:
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
    finishes = [e for e in events if e.kind == EVENT_RUN_FINISHED]
    assert len(finishes) == 1
    assert finishes[0].payload["result"] == RESULT_COMPLETED
    assert tracker.state == STATE_COMPLETED


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
    assert restored.drops == {"layer_2": 0, "layer_3": 0, "pending_reset_holds": 0}
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
    must NOT retrigger the `run_reopened → run_finished(completed)`
    cycle. Fable's B1 finding: reopen requires strict `sub > last_sub`
    progress; an echo carries no progress.
    """
    tracker = RunTracker()

    # Complete a run at mp=100.
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
    assert tracker.state == STATE_COMPLETED

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
    assert tracker.state == STATE_COMPLETED


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
    assert tracker.state == STATE_INTERRUPTED

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
    assert tracker.state == STATE_INTERRUPTED


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
    # Layer 3 must NOT have rejected the genuine packet — the phantom
    # anchor is gone, the original wk₀ is intact.
    assert tracker.drops["layer_3"] == 0


# --------------------------------------------------------------------- #
# 17. Layer 3 rejection path (mutation-testing gap flagged on #49)      #
# --------------------------------------------------------------------- #


def test_layer_3_rejects_invariant_violating_continuation() -> None:
    """The replays only *confirm* the invariant, so an accidental
    neutering of `_passes_layer_3` (e.g. `return True` unconditionally)
    would ship green. Feed a mid-run continuation whose `|wk - sub -
    wk₀| = 5 m² > INVARIANT_TOLERANCE_M2` and assert the drop counter,
    the untouched accumulator, and the untouched cursor.

    Concrete values: open at `sub=10, wk=110 → wk₀=100`. Poison packet
    at `sub=20, wk=125` gives `125 - 20 = 105`, off the anchor by 5 m².
    `sub` grows (no reset path), `wk` grows (layer 2 passes), only the
    anchor invariant fails.
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
    baseline_last_sub = tracker.current_run["last_sub"]
    baseline_last_wk = tracker._last_accepted_wk

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
    assert tracker.drops["layer_3"] == 1
    # Rejected packet touches no state.
    assert tracker.current_run["last_sub"] == baseline_last_sub
    assert tracker._last_accepted_wk == baseline_last_wk


def test_layer_3_within_tolerance_still_accepted() -> None:
    """Regression guard on the tolerance itself: a packet exactly on
    the `wk - sub` anchor line (offset 0) is accepted, and one within
    `INVARIANT_TOLERANCE_M2` (say offset 0.3) is also accepted. Makes
    sure the guard does not overshoot into the healthy-data range.
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

    # Offset 0.3 — within tolerance.
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
                "time": 2_000_000_060_000,
            }
        ],
    )

    assert events == []
    assert tracker.drops["layer_3"] == 0
    assert tracker.current_run["last_sub"] == 20.0
