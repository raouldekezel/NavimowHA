"""BUG-09 — mp-completion criterion on dock arrival.

Scenario (2026-07-04 diag): the FEAT-05 tracker previously required an
exact `mp = 100 ∧ state = RUNNING` to fire `run_finished`, so a
successful run whose robot returned to dock on `vs = 2` (charging) was
parked in `PAUSED_DOCKED` indefinitely and the sustained-60 s
interruption path was blocked by `vs = 2 ∉ {1, 3}`. When it eventually
did fire (89 s after the battery finished charging, ~53 min after
dock), the hardcoded `interrupted` label mis-labeled a completed run.

BUG-09 fix: completion criterion becomes `mp ≥ MP_COMPLETION_THRESHOLD
∧ vs ∈ DOCKED_NOT_USER_PAUSED ({1, 2, 3})`, evaluated from both
`process_type2` and `process_vehicle_state` (so either ordering
closes). The result label is derived in `_close_run` from `last_mp`,
so the sustained-timer fallback path also labels consistently.

BUG-14 revision (2026-07-09, #89): threshold raised from 99 to 100.
A `mp = 99` plateau is indistinguishable from a run returning to dock
to recharge and finish later — closing on 99 misfires on every
low-battery recharge. The `mp = 99` cases in this file assert the
NEW behaviour (mp = 99 does NOT close on dock), and the completing
cases run at mp = 100. Regression cases specific to BUG-14 (recharge
scenario, single-session preservation) live in
`test_bug_14_no_close_on_recharge.py`.

`vs = 6` (isMapping post-mow) is deliberately excluded — even at
`mp = 100` the run must not auto-close during map consolidation.
`vs = 4` (mowing) and `vs = 5` (returning) also don't qualify: the
robot must actually reach the dock.
"""

from __future__ import annotations

from custom_components.navimow.location import parse_location_type_2
from custom_components.navimow.run_tracker import (
    CMP_ZONE_COMPLETE_THRESHOLD,
    DOCKED_NOT_USER_PAUSED,
    EVENT_RUN_FINISHED,
    EVENT_RUN_STARTED,
    INTERRUPT_SUSTAIN_SECONDS,
    MP_COMPLETION_THRESHOLD,
    MP_PARTIAL_THRESHOLD,
    RESULT_COMPLETED,
    RESULT_INTERRUPTED,
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


class FakeClock:
    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _process(tracker: RunTracker, item: dict) -> list:
    return tracker.process_type2(parse_location_type_2(item))


def _open_at(
    tracker: RunTracker,
    mp: int,
    *,
    sub: str = "100.0",
    cmp: int = 0,
    t: int = 1_000_000_000_000,
) -> list:
    """Open a fresh run at the given mp, with matching sub and wk."""
    return _process(
        tracker,
        {
            "type": 2,
            "currentMowBoundary": 1,
            "mowingPercentage": mp,
            "currentMowProgress": cmp,
            "subtotalArea": sub,
            "mowingWeekArea": sub,
            "mowStartType": 1,
            "time": t,
        },
    )


# --------------------------------------------------------------------- #
# 1. Threshold constant + docked-set are what the fix declares          #
# --------------------------------------------------------------------- #


def test_constants_reflect_design_decision() -> None:
    assert MP_COMPLETION_THRESHOLD == 100
    assert MP_PARTIAL_THRESHOLD == 99
    assert CMP_ZONE_COMPLETE_THRESHOLD == 10000
    assert DOCKED_NOT_USER_PAUSED == frozenset(
        {VS_DOCKED_IDLE, VS_DOCKED_CHARGING, VS_DOCKED_UNPOWERED}
    )


# --------------------------------------------------------------------- #
# 2. mp=100 then dock → immediate close, label completed                #
# --------------------------------------------------------------------- #


def test_mp_100_then_dock_arrival_closes_completed() -> None:
    """The BUG-09 fast path — mp reaches the completion threshold, then
    the robot reaches dock on vs=2. The close must fire on the vs
    event, not later — no waiting for the sustained-60 s timer.
    """
    tracker = RunTracker()
    events = _open_at(tracker, mp=100)
    # Only run_started so far — mp alone doesn't close.
    assert [e.kind for e in events] == [EVENT_RUN_STARTED]
    assert tracker.state == STATE_RUNNING

    # Dock arrival on vs=2 (charging) — immediate close.
    events = tracker.process_vehicle_state(VS_DOCKED_CHARGING)
    finishes = [e for e in events if e.kind == EVENT_RUN_FINISHED]
    assert len(finishes) == 1
    assert finishes[0].payload["result"] == RESULT_COMPLETED
    assert tracker.state == STATE_IDLE


def test_dock_first_then_mp_100_closes_completed() -> None:
    """The other ordering — robot docks while mp is still climbing,
    then the last type-2 packet brings mp over the threshold. Fresh
    packet acceptance path fires the close.
    """
    tracker = RunTracker()
    _open_at(tracker, mp=50)
    tracker.process_vehicle_state(VS_DOCKED_CHARGING)
    assert tracker.state == STATE_PAUSED_DOCKED

    # A fresh type-2 with strict progress bumps mp to 100 — reopens the
    # RUNNING state (resume from pause) and immediately closes.
    events = _process(
        tracker,
        {
            "type": 2,
            "currentMowBoundary": 1,
            "mowingPercentage": 100,
            "subtotalArea": "200.0",
            "mowingWeekArea": "200.0",
            "mowStartType": 1,
            "time": 1_000_000_060_000,
        },
    )
    finishes = [e for e in events if e.kind == EVENT_RUN_FINISHED]
    assert len(finishes) == 1
    assert finishes[0].payload["result"] == RESULT_COMPLETED
    assert tracker.state == STATE_IDLE


# --------------------------------------------------------------------- #
# 3. All three docked-not-user-paused vs values complete                #
# --------------------------------------------------------------------- #


def test_dock_arrival_on_vs_1_completes() -> None:
    """`vs = 1` (docked idle, e.g. battery already full) qualifies."""
    tracker = RunTracker()
    _open_at(tracker, mp=100)
    events = tracker.process_vehicle_state(VS_DOCKED_IDLE)
    finishes = [e for e in events if e.kind == EVENT_RUN_FINISHED]
    assert len(finishes) == 1
    assert finishes[0].payload["result"] == RESULT_COMPLETED


def test_dock_arrival_on_vs_3_completes() -> None:
    """`vs = 3` (docked, dock unpowered) qualifies too — the robot is
    on the dock even if the dock has no power. `last_mp` is the
    ground truth for the label.
    """
    tracker = RunTracker()
    _open_at(tracker, mp=100)
    events = tracker.process_vehicle_state(VS_DOCKED_UNPOWERED)
    finishes = [e for e in events if e.kind == EVENT_RUN_FINISHED]
    assert len(finishes) == 1
    assert finishes[0].payload["result"] == RESULT_COMPLETED


# --------------------------------------------------------------------- #
# 4. vs=6 (isMapping) is excluded — hold semantics preserved            #
# --------------------------------------------------------------------- #


def test_mapping_holds_even_at_mp_100() -> None:
    """`vs = 6` is a deliberate exclusion: firmware post-mow map
    consolidation (isMapping). Even at `mp = 100` the run must not
    auto-close — the mapping phase is at-dock and immobile, closing
    through it would race the consolidation. The run parks in
    PAUSED_DOCKED with no close event.

    (Historical note: the earlier test/label named vs = 6 an "explicit
    user pause" — empirically wrong per the 2026-07-07 diag. The
    behavioural invariant "vs = 6 holds the run" is unchanged; only
    the reason is corrected.)
    """
    tracker = RunTracker()
    _open_at(tracker, mp=100)
    events = tracker.process_vehicle_state(VS_MAPPING)
    assert [e for e in events if e.kind == EVENT_RUN_FINISHED] == []
    assert tracker.state == STATE_PAUSED_DOCKED


# --------------------------------------------------------------------- #
# 5. Mid-run pause below threshold still holds                          #
# --------------------------------------------------------------------- #


def test_dock_with_low_mp_holds_paused_docked() -> None:
    """A legitimate recharge pause mid-run (mp far below threshold)
    must still park in PAUSED_DOCKED — this is the whole reason vs=2
    doesn't arm the sustained-timer. Regression guard: BUG-09's fast
    path must not swallow the mid-run pause case.
    """
    tracker = RunTracker()
    _open_at(tracker, mp=42)
    events = tracker.process_vehicle_state(VS_DOCKED_CHARGING)
    assert [e for e in events if e.kind == EVENT_RUN_FINISHED] == []
    assert tracker.state == STATE_PAUSED_DOCKED


# --------------------------------------------------------------------- #
# 6. Threshold exactness                                                #
# --------------------------------------------------------------------- #


def test_mp_99_without_cmp_10000_holds_paused_docked() -> None:
    """mp = 99 alone (without the zone-complete cmp signal) is not
    enough to fire the completion path (BUG-14). The robot might be
    returning for a recharge — the sustained-timer path decides
    later. Complementary of `test_mp_99_plus_cmp_10000_closes_completed`.
    """
    tracker = RunTracker()
    _open_at(tracker, mp=99, cmp=9500)  # zone not yet complete
    events = tracker.process_vehicle_state(VS_DOCKED_CHARGING)
    assert [e for e in events if e.kind == EVENT_RUN_FINISHED] == []
    assert tracker.state == STATE_PAUSED_DOCKED


def test_mp_99_plus_cmp_10000_closes_completed() -> None:
    """BUG-14 refinement: `mp = 99 ∧ zones[-1].cmp_max = 10000` fires
    the completion path with label `completed`. The zone-scoped cmp
    signal discriminates a real finish (last active zone 100 % mowed)
    from a recharge return (zone not yet finished).

    This is the exact rule that recovers a `completed` label on the
    2026-07-09 mini-run of the operator's day, whose firmware plateau
    at `mp = 99` combined with `cmp = 10000` proves the Prunier zone
    was actually finished. See `test_bug_14_no_close_on_recharge.py`
    for the full-day timeline.
    """
    tracker = RunTracker()
    _open_at(tracker, mp=99, cmp=10000)
    events = tracker.process_vehicle_state(VS_DOCKED_CHARGING)
    finishes = [e for e in events if e.kind == EVENT_RUN_FINISHED]
    assert len(finishes) == 1
    assert finishes[0].payload["result"] == RESULT_COMPLETED
    assert tracker.state == STATE_IDLE


def test_mp_100_is_the_first_completing_value() -> None:
    """mp = 100 is the lowest (and only) value that triggers completion
    under the BUG-14 rule.
    """
    tracker = RunTracker()
    _open_at(tracker, mp=100)
    events = tracker.process_vehicle_state(VS_DOCKED_CHARGING)
    finishes = [e for e in events if e.kind == EVENT_RUN_FINISHED]
    assert len(finishes) == 1
    assert finishes[0].payload["result"] == RESULT_COMPLETED


# --------------------------------------------------------------------- #
# 7. mp = 100 while still mowing/returning does NOT close               #
# --------------------------------------------------------------------- #


def test_mp_100_while_mowing_does_not_close() -> None:
    """Even mp=100 doesn't close while the robot is still mowing (vs=4).
    The completion criterion requires docked. A `run_finished` event
    should never fire while the robot is still moving on the lawn.
    """
    tracker = RunTracker()
    tracker.process_vehicle_state(VS_MOWING)
    events = _open_at(tracker, mp=100)
    assert [e for e in events if e.kind == EVENT_RUN_FINISHED] == []
    assert tracker.state == STATE_RUNNING


def test_mp_100_while_returning_does_not_close() -> None:
    """vs=5 (returning) is not a completion state — the robot is on
    its way back but not yet on the dock. Wait for vs=1/2/3.
    """
    tracker = RunTracker()
    tracker.process_vehicle_state(VS_MOWING)
    _open_at(tracker, mp=100)
    events = tracker.process_vehicle_state(VS_RETURNING)
    assert [e for e in events if e.kind == EVENT_RUN_FINISHED] == []
    assert tracker.state == STATE_RUNNING


# --------------------------------------------------------------------- #
# 8. Sustained-timer fallback labels correctly                          #
# --------------------------------------------------------------------- #


def test_fast_path_preempts_sustained_timer_when_user_unpauses() -> None:
    """Sequence: mp reaches 100, firmware enters post-mow mapping
    (vs=6, hold), then transitions to vs=1 once consolidation
    finishes. The vs=6 → vs=1 transition itself qualifies for the
    BUG-09 fast path — no need to wait for the sustained-60 s timer.
    Documents the composition of the two rules (isMapping hold + fast
    completion).
    """
    tracker = RunTracker()
    _open_at(tracker, mp=100)
    tracker.process_vehicle_state(VS_MAPPING)  # hold semantics
    assert tracker.state == STATE_PAUSED_DOCKED

    events = tracker.process_vehicle_state(VS_DOCKED_IDLE)
    finishes = [e for e in events if e.kind == EVENT_RUN_FINISHED]
    assert len(finishes) == 1
    assert finishes[0].payload["result"] == RESULT_COMPLETED
    assert tracker.state == STATE_IDLE


def test_sustained_timer_labels_completed_via_restore_race() -> None:
    """The one reachable sustained-timer + `completed` combination:
    a snapshot taken while `PAUSED_DOCKED + vs=6 + mp=100`, mutated to
    `vs=1` in transit (as if the vs change happened right around the
    HA restart and only the post-change vs was persisted), then
    restored. `tick()` re-arms the sustained-60 s timer (`restore()`
    intentionally clears the monotonic timestamp) and, when it fires,
    labels the close `completed` because `last_mp = 100`.

    Guards against a regression where someone hardcodes `interrupted`
    back into the sustained-timer path.
    """
    source = RunTracker()
    _open_at(source, mp=100)
    source.process_vehicle_state(VS_MAPPING)
    snap = source.snapshot()
    # Simulate a vs=1 landing between the last save and the restore
    # (the fast path never got called live because we crashed just
    # before it could).
    snap["vehicle_state"] = VS_DOCKED_IDLE

    clock = FakeClock()
    restored = RunTracker(clock=clock)
    assert restored.restore(snap) is True
    assert restored.state == STATE_PAUSED_DOCKED

    # First tick arms the timer; second tick past the window fires.
    assert restored.tick() == []
    clock.advance(INTERRUPT_SUSTAIN_SECONDS + 1)
    events = restored.tick()
    finishes = [e for e in events if e.kind == EVENT_RUN_FINISHED]
    assert len(finishes) == 1
    assert finishes[0].payload["result"] == RESULT_COMPLETED
    assert restored.state == STATE_IDLE


def test_sustained_timer_still_labels_interrupted_below_threshold() -> None:
    """The sustained-timer path fires with `interrupted` when the run
    never reached the completion threshold — the original design's
    intent for a genuinely-abandoned mid-run.
    """
    clock = FakeClock()
    tracker = RunTracker(clock=clock)
    _open_at(tracker, mp=42)
    tracker.process_vehicle_state(VS_DOCKED_IDLE)
    # vs=1 with mp < threshold: fast path doesn't fire — state is PAUSED_DOCKED.
    assert tracker.state == STATE_PAUSED_DOCKED
    # Wait past the sustained window.
    clock.advance(INTERRUPT_SUSTAIN_SECONDS + 1)
    events = tracker.tick()
    finishes = [e for e in events if e.kind == EVENT_RUN_FINISHED]
    assert len(finishes) == 1
    assert finishes[0].payload["result"] == RESULT_INTERRUPTED
    assert tracker.state == STATE_IDLE


def test_sustained_timer_labels_interrupted_on_mp_99_below_cmp() -> None:
    """BUG-14 residual trade-off: firmware tasks that plateau at
    `mp = 99` AND never bring the last zone to `cmp = 10000` close via
    the sustained-timer path when the robot settles on `vs = 1` after
    the recharge finishes. The label is `interrupted`. The 2026-07-04
    morning is the canonical case — `mp = 99` peak, `cmp_max` peaked at
    9906 (see BUG-09 diag), so neither BUG-14 branch fires. Operator
    accepted this on issue #89.
    """
    clock = FakeClock()
    tracker = RunTracker(clock=clock)
    _open_at(tracker, mp=99, cmp=9906)  # never reaches 10000
    tracker.process_vehicle_state(VS_DOCKED_IDLE)  # post-charge idle
    assert tracker.state == STATE_PAUSED_DOCKED
    clock.advance(INTERRUPT_SUSTAIN_SECONDS + 1)
    events = tracker.tick()
    finishes = [e for e in events if e.kind == EVENT_RUN_FINISHED]
    assert len(finishes) == 1
    assert finishes[0].payload["result"] == RESULT_INTERRUPTED
    assert tracker.state == STATE_IDLE


def test_sustained_timer_labels_completed_on_mp_99_cmp_10000() -> None:
    """Label consistency across close paths (BUG-09 centralisation):
    if the sustained-timer catches a close that also satisfies the
    BUG-14 refined rule (`mp = 99 ∧ cmp = 10000`), the label is
    `completed`, not `interrupted`. Guards against a regression where
    only the fast path (`_maybe_complete_run`) considers the cmp
    signal.
    """
    clock = FakeClock()
    tracker = RunTracker(clock=clock)
    _open_at(tracker, mp=99, cmp=10000)
    # Skip the fast path by going through the sustained-timer route:
    # start on vs = 6 (mapping — hold semantics, no fast fire), then
    # transition to vs = 1 after the caller misses the vs → 1 timing.
    tracker.process_vehicle_state(VS_MAPPING)
    assert tracker.state == STATE_PAUSED_DOCKED
    # A restart-race snapshot with vs already flipped to 1 in the
    # snapshot but no fast fire live — same shape as the
    # `via_restore_race` sustained test above.
    snap = tracker.snapshot()
    snap["vehicle_state"] = VS_DOCKED_IDLE
    restored = RunTracker(clock=clock)
    assert restored.restore(snap) is True
    assert restored.tick() == []
    clock.advance(INTERRUPT_SUSTAIN_SECONDS + 1)
    events = restored.tick()
    finishes = [e for e in events if e.kind == EVENT_RUN_FINISHED]
    assert len(finishes) == 1
    assert finishes[0].payload["result"] == RESULT_COMPLETED
    assert restored.state == STATE_IDLE


# --------------------------------------------------------------------- #
# 9. Reset labels prior run consistently                                #
# --------------------------------------------------------------------- #


def test_reset_labels_prior_run_completed_when_mp_at_threshold() -> None:
    """A fresh sub reset closes the previous run. Under BUG-09 the
    label is derived from that run's `last_mp` — so a reset after a
    prior run reached `mp = 100` labels the prior run `completed`,
    not `interrupted`. Handles the corner case where a new run starts
    before the tracker had a chance to fire the completion event.
    """
    tracker = RunTracker()
    _open_at(tracker, mp=100, sub="200.0", t=1_000_000_000_000)
    # Fresh sub reset (sub << ceiling) — closes the prior run and
    # opens a new one. The prior run's last_mp = 100 → completed.
    events = _process(
        tracker,
        {
            "type": 2,
            "currentMowBoundary": 3,
            "mowingPercentage": 0,
            "subtotalArea": "0.5",  # << RESET_SUB_CEILING
            "mowingWeekArea": "200.5",
            "mowStartType": 1,
            "time": 1_000_000_060_000,
        },
    )
    finishes = [e for e in events if e.kind == EVENT_RUN_FINISHED]
    started = [e for e in events if e.kind == EVENT_RUN_STARTED]
    assert len(finishes) == 1
    assert finishes[0].payload["result"] == RESULT_COMPLETED
    assert len(started) == 1
    assert tracker.state == STATE_RUNNING


def test_reset_labels_prior_run_interrupted_when_mp_below_threshold() -> None:
    """The complement — a reset after a prior run with `mp = 30`
    (typical abandoned run) labels the prior `interrupted`.
    """
    tracker = RunTracker()
    _open_at(tracker, mp=30, sub="60.0", t=1_000_000_000_000)
    events = _process(
        tracker,
        {
            "type": 2,
            "currentMowBoundary": 3,
            "mowingPercentage": 0,
            "subtotalArea": "0.5",
            "mowingWeekArea": "60.5",
            "mowStartType": 1,
            "time": 1_000_000_060_000,
        },
    )
    finishes = [e for e in events if e.kind == EVENT_RUN_FINISHED]
    assert len(finishes) == 1
    assert finishes[0].payload["result"] == RESULT_INTERRUPTED


# --------------------------------------------------------------------- #
# 10. Immediate close — no debounce                                     #
# --------------------------------------------------------------------- #


def test_immediate_close_does_not_wait_for_a_tick() -> None:
    """No `tick()` call is needed for the BUG-09 completion — the
    close is synchronous with the qualifying `process_vehicle_state`
    or `process_type2` call. The 2026-07-04 diag showed a 53 min gap
    between dock arrival and event fire; the fix must close within
    the same call frame.
    """
    tracker = RunTracker()
    _open_at(tracker, mp=100)
    # Deliberately do NOT call tick(). The vs change alone must close.
    events = tracker.process_vehicle_state(VS_DOCKED_CHARGING)
    assert any(e.kind == EVENT_RUN_FINISHED for e in events)


# --------------------------------------------------------------------- #
# 11. Snapshot / restore preserves the closed state                     #
# --------------------------------------------------------------------- #


def test_snapshot_after_bug_09_close_survives_restore() -> None:
    """A run closed via the BUG-09 fast path must snapshot as COMPLETED
    and restore as COMPLETED — the label is embedded in `state`, not
    re-derived at restore. Follow-up type-2 packets with equal `mp`
    must not re-open (echo guard from #49 B1) — regression against a
    naïve `_maybe_complete_run` on the restore path.
    """
    source = RunTracker()
    _open_at(source, mp=100)
    source.process_vehicle_state(VS_DOCKED_CHARGING)
    assert source.state == STATE_IDLE

    snap = source.snapshot()
    restored = RunTracker()
    assert restored.restore(snap) is True
    assert restored.state == STATE_IDLE

    # An echo packet (same mp, same sub, only time fresher) must NOT
    # reopen.
    events = _process(
        restored,
        {
            "type": 2,
            "currentMowBoundary": 1,
            "mowingPercentage": 100,
            "subtotalArea": "100.0",
            "mowingWeekArea": "100.0",
            "mowStartType": 1,
            "time": 1_000_000_060_000,
        },
    )
    assert events == []
    assert restored.state == STATE_IDLE
