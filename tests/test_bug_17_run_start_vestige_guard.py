"""BUG-17 (#105) — reject the run-start "task-end vestige" packet.

Operator-observed live on 2026-07-19: manually starting a mow via HA
on Prunier (whose last close 10 days earlier was `completed` at
`cmp_max = 10000`) caused the firmware to replay the previous task's
closing packet as the very first `type-2` after the `state → mowing`
transition. Signature: `mp = 100 ∧ cmp = 10000 ∧ subtotalArea = 0.0`
(the accumulators were zeroed on this vestige — distinct from BUG-16
whose vestige carried the previous close's `sub`). Left untouched the
packet:

- was what `_open_run` read in the IDLE order — anchoring
  `start_time`, `sub₀`, `mow_start_type` on the vestige's fields;
- seeded `zones[0].cmp_max = 10000` via `_update_zone`, sticking
  `sensor.<slug>_current_zone_progress` at 100 % for the whole
  Prunier segment (~1 h 50 min in the operator's trace);
- flashed `sensor.<slug>_current_run_progress` to 100 for one
  type-2 cadence (overwritten by the next fresh `mp = 0` — the
  visibly buggy but "self-correcting" half of the pathology);
- stamped `zones[0].first_time` at the vestige packet's `time`
  field, silently misdating FEAT-08's `last_complete_pass_at`;
- stamped `zones[0].sub_entry = 0.0`, silently accepted here because
  Prunier was the first-in-run zone.

The fix drops the packet at the top of `process_type2`, inside an
arming window keyed on tracker state and armed until the first
zone-carrying `type-2` is accepted. Two disjuncts:

- **IDLE order** (observed 2026-07-19): tracker still `STATE_IDLE`
  when the vestige arrives — nothing in `process_vehicle_state`
  opens a run in the current tracker, so `vs = 4` on the wire does
  not move the tracker. Without the guard, `_open_run(vestige)`
  would fire on the ungated `STATE_IDLE` branch of `process_type2`.
- **Sentinel order** (BUG-06): the run has been opened by a
  `boundary = 0` session-init sentinel (2026-05-25 / 2026-07-03
  corpus). `zones == []` because `_update_zone` rejects the
  sentinel; a vestige delivered second (before the real boundary
  ~60 s later) must still be dropped. Includes the
  sentinel-then-dock `STATE_PAUSED_DOCKED` variant.

Signature: `mp = MP_TASK_END ∧ cmp ≥ 10000 ∧ area_session < 0.5`.
`wk`, `action`, `boundary` are logged but not gated. `area_session`
is checked explicitly for `None` so an incomplete packet never
matches the drop signature (fail-open).

Raw diag: `docs/diag/2026-07-19_bug-17_cmp-max-late-task-end/`.
"""

from __future__ import annotations

import logging

from custom_components.navimow.location import parse_location_type_2
from custom_components.navimow.run_tracker import (
    CMP_ZONE_COMPLETE_THRESHOLD,
    EVENT_RUN_FINISHED,
    EVENT_RUN_STARTED,
    INTERRUPT_SUSTAIN_SECONDS,
    MP_TASK_END,
    RUN_START_SUB_TOLERANCE,
    STATE_COMPLETED,
    STATE_IDLE,
    STATE_PAUSED_DOCKED,
    STATE_RUNNING,
    VS_DOCKED_CHARGING,
    VS_DOCKED_IDLE,
    VS_MOWING,
    RunTracker,
)


def _process(tracker: RunTracker, item: dict) -> list:
    return tracker.process_type2(parse_location_type_2(item))


def _pkt(
    mp: int,
    sub: float | None,
    *,
    cmp: int = 0,
    wk: float | None = None,
    boundary: int = 1,
    action: int = 8,
    t: int,
) -> dict:
    d: dict = {
        "type": 2,
        "currentMowBoundary": boundary,
        "mowingPercentage": mp,
        "currentMowProgress": cmp,
        "mowingWeekArea": str(
            wk if wk is not None else (sub if sub is not None else 0.0)
        ),
        "action": action,
        "time": t,
    }
    if sub is not None:
        d["subtotalArea"] = str(sub)
    return d


def _sentinel(*, sub: float = 0.1, wk: float | None = None, t: int) -> dict:
    """BUG-06 session-init sentinel: `boundary = 0, mp = 0, cmp = 0,
    action = -1`. `_update_zone` rejects `boundary = 0`, so a
    tracker that accepts this packet ends up `STATE_RUNNING` with
    `zones == []` — the exact arming state of the guard's second
    disjunct.
    """
    return _pkt(
        mp=0,
        sub=sub,
        cmp=0,
        wk=wk if wk is not None else sub,
        boundary=0,
        action=-1,
        t=t,
    )


class _FakeClock:
    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


# --------------------------------------------------------------------- #
# 1. IDLE order (observed 2026-07-19) — vestige lands while STATE_IDLE  #
# --------------------------------------------------------------------- #


def test_idle_order_vestige_dropped_state_stays_idle(caplog) -> None:
    """The 2026-07-19 wire trace in the current tracker. `vs = 4` at
    11:31:53.128 UTC updates `vehicle_state` but does not move the
    tracker's `state` — `process_vehicle_state` does not call
    `_open_run` in this architecture. So at 11:31:53.213 UTC the
    vestige arrives with `state == STATE_IDLE` and `current_run is
    None`. The guard drops it with zero mutation — critically the
    vestige never gets to seed `_open_run`'s `start_time`, `sub₀`,
    `mow_start_type`, or the `_update_zone` first-zone entry.
    """
    tracker = RunTracker()
    assert tracker.state == STATE_IDLE
    assert tracker.current_run is None

    with caplog.at_level(logging.DEBUG, logger="custom_components.navimow.run_tracker"):
        events = _process(
            tracker,
            _pkt(
                mp=100,
                cmp=CMP_ZONE_COMPLETE_THRESHOLD,
                sub=0.0,
                wk=0.0,
                boundary=1,
                action=-1,
                t=1_784_453_512_965,
            ),
        )

    assert events == []
    assert tracker.state == STATE_IDLE
    assert tracker.current_run is None
    assert tracker._last_accepted_wk is None
    assert tracker._last_accepted_time_ms is None

    dbgs = [
        r.getMessage()
        for r in caplog.records
        if r.levelno == logging.DEBUG and "run-start vestige" in r.getMessage()
    ]
    assert len(dbgs) == 1, dbgs


def test_idle_order_next_real_packet_opens_run_cleanly() -> None:
    """After the vestige is dropped in `STATE_IDLE`, the tracker is
    still idle. The next genuine packet must open a fresh run
    normally — no lingering contamination on `start_time`, `sub₀`,
    `mow_start_type`, or `zones[0].cmp_max`.
    """
    tracker = RunTracker()
    _process(
        tracker,
        _pkt(
            mp=100,
            cmp=CMP_ZONE_COMPLETE_THRESHOLD,
            sub=0.0,
            wk=0.0,
            boundary=1,
            action=-1,
            t=1_784_453_512_965,
        ),
    )
    assert tracker.state == STATE_IDLE

    events = _process(
        tracker,
        _pkt(
            mp=0,
            cmp=100,
            sub=2.47,
            wk=2.42,
            boundary=1,
            action=8,
            t=1_784_453_569_397,
        ),
    )
    assert tracker.state == STATE_RUNNING
    assert [e for e in events if e.kind == EVENT_RUN_STARTED]
    r = tracker.current_run
    assert r["start_time"] == 1_784_453_569_397
    assert r["sub0"] == 2.47
    assert r["last_mp"] == 0
    zones = r["zones"]
    assert len(zones) == 1
    assert zones[0]["cmp_max"] == 100
    assert zones[0]["first_time"] == 1_784_453_569_397
    assert zones[0]["sub_entry"] == 2.47


# --------------------------------------------------------------------- #
# 2. Sentinel order (BUG-06) — sentinel opens the run, zones stays []   #
# --------------------------------------------------------------------- #


def test_sentinel_order_vestige_dropped_run_stays_seedless(caplog) -> None:
    """BUG-06 session-init sentinel opens the run through the public
    `process_type2` path. `_update_zone` rejects `boundary = 0`, so
    `state == STATE_RUNNING` with `zones == []` and cursors anchored
    on the sentinel's fields. A vestige-shape packet arriving at the
    second position (before the real boundary ~60 s later) hits the
    second-disjunct arming and is dropped — cursors stay at the
    sentinel's values, no `zones[0]` gets seeded.
    """
    tracker = RunTracker()
    sentinel_events = _process(tracker, _sentinel(sub=0.1, wk=0.1, t=1_000))
    started = [e for e in sentinel_events if e.kind == EVENT_RUN_STARTED]
    assert len(started) == 1
    assert tracker.state == STATE_RUNNING
    assert tracker.current_run["zones"] == []
    # Sentinel anchored the run.
    assert tracker.current_run["start_time"] == 1_000
    assert tracker.current_run["sub0"] == 0.1
    assert tracker.current_run["last_mp"] == 0
    assert tracker.current_run["last_sub"] == 0.1
    assert tracker.current_run["last_wk"] == 0.1

    with caplog.at_level(logging.DEBUG, logger="custom_components.navimow.run_tracker"):
        events = _process(
            tracker,
            _pkt(
                mp=100,
                cmp=CMP_ZONE_COMPLETE_THRESHOLD,
                sub=0.0,
                wk=0.0,
                boundary=1,
                action=-1,
                t=2_000,
            ),
        )

    # Guard fired: no mutation from the vestige.
    assert events == []
    assert tracker.state == STATE_RUNNING
    assert tracker.current_run["zones"] == []
    # Cursors are still the sentinel's — the vestige's `mp = 100`,
    # `sub = 0.0`, `wk = 0.0` were never written.
    assert tracker.current_run["last_mp"] == 0
    assert tracker.current_run["last_sub"] == 0.1
    assert tracker.current_run["last_wk"] == 0.1
    # `start_time` still anchored on the sentinel, not the vestige.
    assert tracker.current_run["start_time"] == 1_000

    dbgs = [
        r.getMessage()
        for r in caplog.records
        if r.levelno == logging.DEBUG and "run-start vestige" in r.getMessage()
    ]
    assert len(dbgs) == 1, dbgs


def test_sentinel_then_early_dock_paused_docked_vestige_dropped(caplog) -> None:
    """Sentinel opens the run, then the robot re-docks before its
    first real boundary — `vs = 2` transitions `STATE_RUNNING →
    STATE_PAUSED_DOCKED` with `zones` still empty. A vestige-shape
    packet at this point must still be dropped (the second disjunct
    includes `STATE_PAUSED_DOCKED`).
    """
    tracker = RunTracker()
    _process(tracker, _sentinel(sub=0.1, t=1_000))
    tracker.process_vehicle_state(VS_DOCKED_CHARGING)
    assert tracker.state == STATE_PAUSED_DOCKED
    assert tracker.current_run["zones"] == []

    with caplog.at_level(logging.DEBUG, logger="custom_components.navimow.run_tracker"):
        events = _process(
            tracker,
            _pkt(
                mp=100,
                cmp=CMP_ZONE_COMPLETE_THRESHOLD,
                sub=0.0,
                wk=0.0,
                boundary=1,
                action=-1,
                t=2_000,
            ),
        )
    assert events == []
    assert tracker.state == STATE_PAUSED_DOCKED
    assert tracker.current_run["zones"] == []
    assert tracker.current_run["last_mp"] == 0
    dbgs = [
        r.getMessage()
        for r in caplog.records
        if r.levelno == logging.DEBUG and "run-start vestige" in r.getMessage()
    ]
    assert len(dbgs) == 1, dbgs


def test_sentinel_then_real_boundary_closes_window(caplog) -> None:
    """The window tracks **zone seeding**, not packet count. Sentinel
    → real boundary packet → `zones[0]` seeded at the real `cmp`;
    the window is now closed. A subsequent vestige-shape packet is
    not intercepted by the BUG-17 guard (arming disqualified by
    `zones != []`) and flows on into the tracker's normal branches
    — what those branches do with it (accept, reset+reopen via
    is_reset, whatever) is orthogonal to this test.

    This is important because a legitimate mid-run `mp = 100 ∧
    cmp = 10000` completion is exactly what BUG-14 / #91's fast-path
    needs to close a real mow — the guard must not stay armed
    forever.
    """
    tracker = RunTracker()
    _process(tracker, _sentinel(sub=0.1, t=1_000))
    # Real boundary lands ~60 s later per BUG-06 corpus.
    _process(
        tracker,
        _pkt(mp=0, cmp=100, sub=2.47, wk=2.42, boundary=1, action=8, t=60_000),
    )
    assert tracker.state == STATE_RUNNING
    zones = tracker.current_run["zones"]
    assert len(zones) == 1
    assert zones[0]["cmp_max"] == 100
    assert zones[0]["boundary_id"] == 1

    # Vestige-shape packet at position 3 on the same boundary — window
    # closed, guard inert. Clear caplog first: the sentinel legitimately
    # emitted a suspicious-shape DEBUG at t=1_000 (accepted noise).
    caplog.clear()
    with caplog.at_level(logging.DEBUG, logger="custom_components.navimow.run_tracker"):
        _process(
            tracker,
            _pkt(
                mp=100,
                cmp=CMP_ZONE_COMPLETE_THRESHOLD,
                sub=0.0,
                wk=0.0,
                boundary=1,
                action=-1,
                t=90_000,
            ),
        )
    # The BUG-17 guard did NOT emit a drop DEBUG — the packet flowed
    # through to downstream branches.
    rejects = [
        r.getMessage()
        for r in caplog.records
        if r.levelno == logging.DEBUG and "run-start vestige" in r.getMessage()
    ]
    assert rejects == []
    # No suspicious-shape DEBUG either — the window closed, so the
    # observability line is out of scope for this packet.
    suspicious = [
        r.getMessage()
        for r in caplog.records
        if r.levelno == logging.DEBUG and "run-start suspicious shape" in r.getMessage()
    ]
    assert suspicious == []


# --------------------------------------------------------------------- #
# 3. Sentinel accepted — mp=0 fails the drop signature                  #
# --------------------------------------------------------------------- #


def test_sentinel_from_idle_accepted_with_suspicious_debug(caplog) -> None:
    """The BUG-06 sentinel itself carries `mp = 0`, so it fails the
    drop signature (which requires `mp = MP_TASK_END`). It must be
    accepted — otherwise the guard would break session opens for
    every real run. Its low `sub` (0.1) triggers the
    suspicious-shape observability DEBUG line, documenting the
    accepted noise: sentinels land there every session, alongside
    genuine low-`sub` first packets (Sunday first-mow, fresh session
    post-reset).
    """
    tracker = RunTracker()
    with caplog.at_level(logging.DEBUG, logger="custom_components.navimow.run_tracker"):
        events = _process(tracker, _sentinel(sub=0.1, t=1_000))
    # Sentinel accepted — run_started fired, state moved to RUNNING.
    started = [e for e in events if e.kind == EVENT_RUN_STARTED]
    assert len(started) == 1
    assert tracker.state == STATE_RUNNING
    assert tracker.current_run["zones"] == []
    # No drop DEBUG.
    rejects = [
        r.getMessage()
        for r in caplog.records
        if r.levelno == logging.DEBUG and "run-start vestige" in r.getMessage()
    ]
    assert rejects == []
    # Suspicious-shape DEBUG fired.
    suspicious = [
        r.getMessage()
        for r in caplog.records
        if r.levelno == logging.DEBUG and "run-start suspicious shape" in r.getMessage()
    ]
    assert len(suspicious) == 1, suspicious


# --------------------------------------------------------------------- #
# 4. Post-close vestige — DROPPED post-BUG-19 (widened arming window)   #
# --------------------------------------------------------------------- #


def test_post_close_vestige_dropped_state_completed(caplog) -> None:
    """BUG-19 (#114) — the operator's dominant real-world case.
    Vestige arrives with the tracker in `STATE_COMPLETED` restored
    from a previous mow's close (Store rehydration on HA restart).
    Pre-BUG-19 the guard was dark here and the packet flowed to the
    post-close `is_reset` branch → `_open_run(vestige)` (this test
    file previously pinned that inverted behaviour).

    Post-BUG-19 the arming predicate flips: dark only when
    `state ∈ {RUNNING, PAUSED_DOCKED} ∧ zones != []`. `STATE_COMPLETED`
    fails that state check → armed → signature matches → dropped.
    No `run_started` event, tracker state unchanged, cursors
    untouched.
    """
    tracker = RunTracker()
    # Complete a genuine mow so the tracker enters STATE_COMPLETED with
    # current_run still referenced (mimics the Store-restored state on
    # the operator's 2026-07-20 event).
    _process(tracker, _pkt(mp=0, cmp=100, sub=2.47, wk=2.42, boundary=1, t=1_000))
    _process(tracker, _pkt(mp=50, cmp=5000, sub=100.0, wk=100.0, boundary=1, t=2_000))
    _process(
        tracker, _pkt(mp=100, cmp=10000, sub=232.89, wk=232.89, boundary=1, t=3_000)
    )
    tracker.process_vehicle_state(VS_DOCKED_CHARGING)
    assert tracker.state == STATE_COMPLETED
    prev_last_sub = tracker.current_run.get("last_sub")
    prev_last_mp = tracker.current_run.get("last_mp")
    prev_zones_len = len(tracker.current_run["zones"])

    with caplog.at_level(logging.DEBUG, logger="custom_components.navimow.run_tracker"):
        events = _process(
            tracker,
            _pkt(
                mp=100,
                cmp=CMP_ZONE_COMPLETE_THRESHOLD,
                sub=0.0,
                wk=0.0,
                boundary=1,
                action=-1,
                t=4_000,
            ),
        )
    # Guard fired: no run_started, no state transition, no mutation
    # of the closed run's cursors.
    assert [e for e in events if e.kind == EVENT_RUN_STARTED] == []
    assert tracker.state == STATE_COMPLETED
    assert tracker.current_run.get("last_sub") == prev_last_sub
    assert tracker.current_run.get("last_mp") == prev_last_mp
    assert len(tracker.current_run["zones"]) == prev_zones_len

    dbgs = [
        r.getMessage()
        for r in caplog.records
        if r.levelno == logging.DEBUG and "run-start vestige" in r.getMessage()
    ]
    assert len(dbgs) == 1, dbgs


def test_post_close_vestige_dropped_state_interrupted(caplog) -> None:
    """The sister case: `STATE_INTERRUPTED` (last mow closed by the
    sustained-timer path, not the fast-path). BUG-19's arming
    predicate treats both post-close states the same — dark only on
    mowing-with-real-zone.
    """
    from custom_components.navimow.run_tracker import (
        INTERRUPT_SUSTAIN_SECONDS,
        STATE_INTERRUPTED,
    )

    clock = _FakeClock()
    tracker = RunTracker(clock=clock)
    # Run a partial mow then let the sustained-timer close it as
    # `interrupted` (mp never reaches completion threshold).
    _process(tracker, _pkt(mp=0, cmp=100, sub=2.47, wk=2.42, boundary=1, t=1_000))
    _process(tracker, _pkt(mp=30, cmp=3000, sub=50.0, wk=50.0, boundary=1, t=2_000))
    tracker.process_vehicle_state(VS_DOCKED_IDLE)
    clock.advance(INTERRUPT_SUSTAIN_SECONDS + 1)
    tracker.tick()
    assert tracker.state == STATE_INTERRUPTED

    with caplog.at_level(logging.DEBUG, logger="custom_components.navimow.run_tracker"):
        events = _process(
            tracker,
            _pkt(
                mp=100,
                cmp=CMP_ZONE_COMPLETE_THRESHOLD,
                sub=0.0,
                wk=0.0,
                boundary=1,
                action=-1,
                t=4_000,
            ),
        )
    assert [e for e in events if e.kind == EVENT_RUN_STARTED] == []
    assert tracker.state == STATE_INTERRUPTED
    dbgs = [
        r.getMessage()
        for r in caplog.records
        if r.levelno == logging.DEBUG and "run-start vestige" in r.getMessage()
    ]
    assert len(dbgs) == 1, dbgs


def test_post_close_genuine_remow_start_accepted() -> None:
    """The critical no-false-positive case: from `STATE_COMPLETED`,
    a **genuine** fresh mow's first real packet (`mp = 0, cmp small,
    sub small`) must be accepted. The arming window is wide here
    (BUG-19), but the drop signature is not — `mp = 0` fails the
    `mp == MP_TASK_END` clause, so the packet flows through to the
    post-close `is_reset` branch as intended, opening a fresh run
    normally.

    This is the packet that would land ~60 s after the (now-dropped)
    vestige on the real 2026-07-20 replay.
    """
    tracker = RunTracker()
    _process(tracker, _pkt(mp=0, cmp=100, sub=2.47, wk=2.42, boundary=1, t=1_000))
    _process(tracker, _pkt(mp=50, cmp=5000, sub=100.0, wk=100.0, boundary=1, t=2_000))
    _process(
        tracker, _pkt(mp=100, cmp=10000, sub=232.89, wk=232.89, boundary=1, t=3_000)
    )
    tracker.process_vehicle_state(VS_DOCKED_CHARGING)
    assert tracker.state == STATE_COMPLETED

    # Fresh mow starts: genuine first real packet.
    events = _process(
        tracker,
        _pkt(mp=0, cmp=104, sub=2.53, wk=360.12, boundary=1, action=8, t=4_000),
    )
    started = [e for e in events if e.kind == EVENT_RUN_STARTED]
    assert len(started) == 1
    assert tracker.state == STATE_RUNNING
    # sub₀ anchored on the real packet, not on any vestige.
    assert tracker.current_run["sub0"] == 2.53
    assert tracker.current_run["last_mp"] == 0
    zones = tracker.current_run["zones"]
    assert len(zones) == 1
    assert zones[0]["cmp_max"] == 104


def test_2026_07_20_replay_end_to_end() -> None:
    """Deterministic replay of the 2026-07-20 wire trace end-to-end.

    Preconditions: tracker restored to `STATE_COMPLETED` from a prior
    close (2026-07-19 completed multi-zone). Vestige arrives on the
    wire immediately after `vs = 4`. Genuine first real packet
    arrives ~56 s later.

    Pre-BUG-19 outcome (raoul.22): vestige poisons `_open_run`,
    `zones[0].cmp_max = 10000` seeded, gauge stuck at 100 %.
    Post-BUG-19 outcome (this test): vestige dropped, real packet
    opens the run cleanly with `zones[0].cmp_max = 104` and gauge
    starts at 1.04 %.
    """
    tracker = RunTracker()
    # Reconstitute a prior 2026-07-19 close.
    _process(tracker, _pkt(mp=0, cmp=100, sub=2.47, wk=2.42, boundary=1, t=1_000))
    _process(
        tracker, _pkt(mp=100, cmp=10000, sub=232.89, wk=232.89, boundary=1, t=2_000)
    )
    tracker.process_vehicle_state(VS_DOCKED_CHARGING)
    assert tracker.state == STATE_COMPLETED

    # 11:21:54.382 — vestige (real wire fields from the diag).
    tracker.process_vehicle_state(VS_MOWING)
    vestige_events = _process(
        tracker,
        _pkt(
            mp=100,
            cmp=CMP_ZONE_COMPLETE_THRESHOLD,
            sub=0.0,
            wk=357.63,
            boundary=1,
            action=-1,
            t=1_784_539_314_431,
        ),
    )
    assert [e for e in vestige_events if e.kind == EVENT_RUN_STARTED] == []
    assert tracker.state == STATE_COMPLETED  # vestige rejected, state unchanged

    # 11:22:50.383 — genuine first real packet (56 s later on the wire).
    real_events = _process(
        tracker,
        _pkt(
            mp=0,
            cmp=104,
            sub=2.53,
            wk=360.12,
            boundary=1,
            action=8,
            t=1_784_539_370_461,
        ),
    )
    started = [e for e in real_events if e.kind == EVENT_RUN_STARTED]
    assert len(started) == 1
    r = tracker.current_run
    assert r["start_time"] == 1_784_539_370_461  # real packet, not vestige
    assert r["sub0"] == 2.53
    assert r["last_mp"] == 0
    zones = r["zones"]
    assert len(zones) == 1
    # The critical assertion: cmp_max reflects the real cmp, not 10000.
    assert zones[0]["cmp_max"] == 104
    assert zones[0]["sub_entry"] == 2.53


# --------------------------------------------------------------------- #
# 5. Sunday first mow — tolerances alone must never trigger the drop    #
# --------------------------------------------------------------------- #


def test_sunday_first_mow_low_sub_but_low_mp_accepted() -> None:
    """First mow of the firmware week (Sunday-start): `mp = 0, cmp =
    30, sub = 0.3, wk = 0.3`. `sub` is under the tolerance but the
    signature fails on `mp` (0 ≠ MP_TASK_END), so the packet must be
    accepted. The guard requires the three conjuncts together; `sub`
    alone never triggers the drop.
    """
    tracker = RunTracker()
    events = _process(
        tracker,
        _pkt(mp=0, cmp=30, sub=0.3, wk=0.3, boundary=1, action=8, t=1_000),
    )
    started = [e for e in events if e.kind == EVENT_RUN_STARTED]
    assert len(started) == 1
    assert tracker.state == STATE_RUNNING
    zones = tracker.current_run["zones"]
    assert len(zones) == 1
    assert zones[0]["cmp_max"] == 30
    assert zones[0]["sub_entry"] == 0.3


# --------------------------------------------------------------------- #
# 6. Incomplete packet (area_session is None) never matches drop        #
# --------------------------------------------------------------------- #


def test_incomplete_packet_missing_sub_accepted_not_dropped() -> None:
    """`area_session is None` (parser fallback on a sparse firmware
    variant) inside the arming window: the guard must fail-open. The
    explicit `is not None` check in the condition means a missing
    accumulator never defaults toward the incriminating zero.
    """
    tracker = RunTracker()
    events = _process(
        tracker,
        _pkt(
            mp=100,
            cmp=CMP_ZONE_COMPLETE_THRESHOLD,
            sub=None,
            wk=0.0,
            boundary=1,
            action=-1,
            t=1_000,
        ),
    )
    started = [e for e in events if e.kind == EVENT_RUN_STARTED]
    assert len(started) == 1
    assert tracker.state == STATE_RUNNING
    assert tracker.current_run["last_mp"] == 100


# --------------------------------------------------------------------- #
# 7. Mid-run genuine completion (zones non-empty) is not gated          #
# --------------------------------------------------------------------- #


def test_mid_run_mp_100_cmp_10000_completion_accepted() -> None:
    """A legitimate mid-run `mp = 100 ∧ cmp = 10000` completion packet
    (final packet of a real mow) must be accepted. `zones` is non-empty
    at this point, so neither arming-window disjunct fires — the guard
    is inert.
    """
    tracker = RunTracker()
    _process(tracker, _pkt(mp=0, cmp=100, sub=2.47, wk=2.42, boundary=1, t=1_000))
    _process(tracker, _pkt(mp=50, cmp=5000, sub=100.0, wk=100.0, boundary=1, t=2_000))
    events = _process(
        tracker,
        _pkt(
            mp=100,
            cmp=CMP_ZONE_COMPLETE_THRESHOLD,
            sub=232.89,
            wk=232.89,
            boundary=1,
            action=8,
            t=3_000,
        ),
    )
    assert tracker.current_run["last_mp"] == 100
    assert tracker.current_run["zones"][-1]["cmp_max"] == CMP_ZONE_COMPLETE_THRESHOLD
    assert [e for e in events if e.kind == EVENT_RUN_FINISHED] == []


def test_mid_run_mp_100_cmp_10000_sub_zero_edge_case_still_accepted() -> None:
    """Belt-and-braces: even if a mid-run packet happened to carry
    `sub = 0.0` (contradiction with the accumulated `last_sub`, but
    possible under a firmware anomaly), the guard must not gate it —
    the arming window is disqualified because `zones` is non-empty.
    """
    tracker = RunTracker()
    _process(tracker, _pkt(mp=0, cmp=100, sub=2.47, wk=2.42, boundary=1, t=1_000))
    _process(
        tracker,
        _pkt(
            mp=100,
            cmp=CMP_ZONE_COMPLETE_THRESHOLD,
            sub=0.0,
            wk=0.0,
            boundary=1,
            action=-1,
            t=2_000,
        ),
    )
    assert tracker.current_run["last_mp"] == 100
    assert tracker.current_run["zones"][-1]["cmp_max"] == CMP_ZONE_COMPLETE_THRESHOLD


# --------------------------------------------------------------------- #
# 8. Session-2 resume packet is not gated (no vestige on _reopen_run)   #
# --------------------------------------------------------------------- #


def test_session_2_resume_packet_not_gated() -> None:
    """Session 2 of the 2026-07-19 trace resumed on Figuier via a
    `_reopen_run` path — the first `type-2` after the second `docked →
    mowing` transition carried `sub = 235.65, mp = 65`, no vestige.
    That packet enters `process_type2` with `current_run` still open
    from the interrupted first session and `zones` non-empty — arming
    window disqualified, guard inert.
    """
    tracker = RunTracker()
    _process(tracker, _pkt(mp=0, cmp=100, sub=2.47, wk=2.42, boundary=1, t=1_000))
    _process(tracker, _pkt(mp=65, cmp=5000, sub=200.0, wk=200.0, boundary=1, t=2_000))
    tracker.process_vehicle_state(VS_DOCKED_IDLE)
    tracker.process_vehicle_state(VS_MOWING)
    events = _process(
        tracker,
        _pkt(
            mp=65,
            cmp=222,
            sub=235.65,
            wk=235.5,
            boundary=3,
            action=8,
            t=3_000,
        ),
    )
    zones = tracker.current_run["zones"]
    assert any(z["boundary_id"] == 3 and z["cmp_max"] == 222 for z in zones)
    assert [
        e for e in events if e.kind in (EVENT_RUN_STARTED, EVENT_RUN_FINISHED)
    ] == []


# --------------------------------------------------------------------- #
# 9. Suspicious partial shape — accepted, DEBUG asserted                #
# --------------------------------------------------------------------- #


def test_suspicious_partial_shape_accepted_with_debug(caplog) -> None:
    """The observability line covers the unobserved `interrupted`
    vestige shape (open question 1): `sub` near zero at run-start
    *without* the full `mp = 100 ∧ cmp = 10000` match. The packet is
    accepted, but a distinct DEBUG line records the shape for later
    analysis.
    """
    tracker = RunTracker()
    with caplog.at_level(logging.DEBUG, logger="custom_components.navimow.run_tracker"):
        events = _process(
            tracker,
            _pkt(
                mp=100,
                cmp=4404,
                sub=0.0,
                wk=0.0,
                boundary=1,
                action=-1,
                t=1_000,
            ),
        )
    started = [e for e in events if e.kind == EVENT_RUN_STARTED]
    assert len(started) == 1
    assert tracker.state == STATE_RUNNING
    assert tracker.current_run["last_mp"] == 100

    suspicious = [
        r.getMessage()
        for r in caplog.records
        if r.levelno == logging.DEBUG and "run-start suspicious shape" in r.getMessage()
    ]
    assert len(suspicious) == 1, suspicious
    rejects = [
        r.getMessage()
        for r in caplog.records
        if r.levelno == logging.DEBUG and "run-start vestige" in r.getMessage()
    ]
    assert rejects == []


# --------------------------------------------------------------------- #
# 10. End-to-end — interrupted run before cmp=10000 → cmp_max is real   #
# --------------------------------------------------------------------- #


def test_interrupted_run_after_dropped_vestige_reports_real_cmp_max() -> None:
    """The BUG-17 blast-radius scenario the diag calls out: a
    vestige-poisoned first-in-run zone that gets interrupted before
    the real `cmp` reaches 10000. Pre-fix the tracker would emit
    `zones[0].cmp_max = 10000` on the close event, silently over-
    stating `Store.last_cmp_max` (which ZoneRegistry.ingest_run reads
    at run close). With the guard in place, `cmp_max` on the emitted
    close event reflects the true observed maximum.

    Setup uses the sentinel-order path (the actual BUG-06 opening
    sequence): sentinel opens the run, vestige is dropped, real
    packets accumulate to `cmp = 3111`, robot docks, sustained-timer
    closes the run as `interrupted`.
    """
    clock = _FakeClock()
    tracker = RunTracker(clock=clock)
    # Sentinel opens the run.
    _process(tracker, _sentinel(sub=0.1, t=1_000))
    # Vestige at position 2 — dropped.
    _process(
        tracker,
        _pkt(
            mp=100,
            cmp=CMP_ZONE_COMPLETE_THRESHOLD,
            sub=0.0,
            wk=0.0,
            boundary=1,
            action=-1,
            t=2_000,
        ),
    )
    # Real mow progresses only partway: cmp climbs to 3111 (mp=20).
    _process(tracker, _pkt(mp=0, cmp=100, sub=2.47, wk=2.42, boundary=1, t=60_000))
    _process(tracker, _pkt(mp=10, cmp=1500, sub=50.0, wk=50.0, boundary=1, t=120_000))
    _process(tracker, _pkt(mp=20, cmp=3111, sub=75.0, wk=75.0, boundary=1, t=180_000))

    # Robot docks — sustained-timer close-out (no cmp completion path).
    tracker.process_vehicle_state(VS_DOCKED_IDLE)
    clock.advance(INTERRUPT_SUSTAIN_SECONDS + 1)
    finish_events = tracker.tick()
    finishes = [e for e in finish_events if e.kind == EVENT_RUN_FINISHED]
    assert len(finishes) == 1
    payload = finishes[0].payload
    assert payload["result"] == "interrupted"
    # Critical assertion: the reported cmp_max is the real observed
    # maximum (3111), not the vestige's 10000.
    zones = payload["zones"]
    assert len(zones) == 1
    assert zones[0]["cmp_max"] == 3111


# --------------------------------------------------------------------- #
# Sanity: tolerance and protocol constants pinned                       #
# --------------------------------------------------------------------- #


def test_tolerance_constant_pinned() -> None:
    """Pin the tolerance constant. 0.5 m² sits between the vestige's
    literal 0.0 and the smallest observed genuine first-packet `sub`
    (2.47 m² on 2026-07-19; 0.39 m² on 2026-05-25 for a fresh session
    post-reset). Drifting upward risks catching a genuine packet;
    drifting downward risks missing a vestige on a firmware variant
    that emits a residual float near zero.
    """
    assert RUN_START_SUB_TOLERANCE == 0.5


def test_mp_task_end_pinned() -> None:
    """`MP_TASK_END` is a wire-protocol fact (the firmware stamps
    this value on the vestige), decoupled from
    `MP_COMPLETION_THRESHOLD` (a tunable tracker policy). Pin it so
    a future re-tune of the completion threshold cannot silently
    drag the vestige signature with it.
    """
    assert MP_TASK_END == 100
