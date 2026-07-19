"""BUG-17 (#105) — reject the run-start "task-end vestige" packet.

Operator-observed live on 2026-07-19: manually starting a mow via HA
on Prunier (whose last close 10 days earlier was `completed` at
`cmp_max = 10000`) caused the firmware to replay the previous task's
closing packet as the very first `type-2` after the `state → mowing`
transition. Signature: `mp = 100 ∧ cmp = 10000 ∧ subtotalArea = 0.0`
(the accumulators were zeroed on this vestige — distinct from BUG-16
whose vestige carried the previous close's `sub`). Left untouched the
packet:

- flashed `sensor.<slug>_current_run_progress` to 100 for one type-2
  cadence (overwritten by the next fresh `mp = 0` — the visibly
  buggy but "self-correcting" half of the pathology),
- seeded `zones[0].cmp_max = 10000`, sticking
  `sensor.<slug>_current_zone_progress` at 100 % for the whole Prunier
  segment (~1 h 50 min in the operator's trace) — the visibly buggy
  and monotonically-stuck half,
- stamped `zones[0].first_time` at the vestige packet's `time` field,
  silently misdating FEAT-08's `last_complete_pass_at`,
- stamped `zones[0].sub_entry = 0.0`, silently accepted here because
  Prunier was the first-in-run zone and legitimately opens at
  `sub ≈ 0` — but wrong-by-collateral on a run where the poisoned
  boundary is not first.

The fix drops the packet at the top of `process_type2`, inside a
two-disjunct arming window (state observer opened the run before the
packet arrived, or is still IDLE), before any state transition or
write. Signature: `mp = 100 ∧ cmp ≥ 10000 ∧ area_session < 0.5`. The
`wk` field is deliberately *not* part of the signature (observed 0.0
on 2026-07-19 was a calendar artifact — Sunday, day one of the
Sunday-start firmware week), and neither is `action` (`= -1` appears
on legitimate mid-run packets in the same trace).

Raw diag: `docs/diag/2026-07-19_bug-17_cmp-max-late-task-end/`.
"""

from __future__ import annotations

import logging

from custom_components.navimow.location import parse_location_type_2
from custom_components.navimow.run_tracker import (
    CMP_ZONE_COMPLETE_THRESHOLD,
    EVENT_RUN_FINISHED,
    EVENT_RUN_STARTED,
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
        "mowingWeekArea": str(wk if wk is not None else (sub if sub is not None else 0.0)),
        "action": action,
        "time": t,
    }
    if sub is not None:
        d["subtotalArea"] = str(sub)
    return d


# --------------------------------------------------------------------- #
# 1. Observed order — state observer opens the run, then vestige lands  #
# --------------------------------------------------------------------- #


def test_observed_order_vestige_dropped_run_stays_seedless(caplog) -> None:
    """The 2026-07-19 firmware ordering. `_open_run` has already run
    (mowing-state transition arrived first, 85 ms ahead of the packet),
    so `current_run` exists with `zones == []` when the vestige is
    fed. The guard drops the packet before any accumulator/cursor/zone
    update.

    Assertions cover the full no-mutation contract:
    - no events emitted from the packet (the `run_started` already
      emitted by `_open_run` on the state transition is legitimate and
      not part of this call's returns),
    - `zones` still empty (no `_update_zone` ran),
    - `last_mp` untouched (no `_update_accumulators` ran — this is
      what suppresses the 56-s `current_run_progress` flash),
    - `last_wk`/`last_sub` cursors likewise untouched,
    - DEBUG line asserted via `caplog`.
    """
    tracker = RunTracker()
    # Simulate the state observer's `_open_run` firing on the mowing
    # transition first, per the observed ordering. The internal helper
    # is the same one `process_type2` would call from `STATE_IDLE` — we
    # invoke it directly rather than fake a state transition.
    seed_time = 1_784_453_512_800
    tracker._open_run(
        {
            "time": seed_time,
            "area_session": None,
            "area_week": None,
            "mowing_percentage": None,
            "mow_start_type": 1,
        }
    )
    assert tracker.state == STATE_RUNNING
    assert tracker.current_run["zones"] == []
    assert tracker.current_run.get("last_mp") is None

    with caplog.at_level(
        logging.DEBUG, logger="custom_components.navimow.run_tracker"
    ):
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
    assert tracker.state == STATE_RUNNING
    assert tracker.current_run is not None
    assert tracker.current_run["zones"] == []
    assert tracker.current_run.get("last_mp") is None
    assert tracker.current_run.get("last_sub") is None
    assert tracker.current_run.get("last_wk") is None
    # start_time still the state-observer's seed, not the vestige packet.
    assert tracker.current_run["start_time"] == seed_time

    dbgs = [
        r.getMessage()
        for r in caplog.records
        if r.levelno == logging.DEBUG and "run-start vestige" in r.getMessage()
    ]
    assert len(dbgs) == 1, dbgs


# --------------------------------------------------------------------- #
# 2. Inverted order — vestige lands while still STATE_IDLE              #
# --------------------------------------------------------------------- #


def test_inverted_order_vestige_dropped_state_stays_idle(caplog) -> None:
    """The unobserved but possible ordering: vestige packet is
    delivered before the mowing-state transition. Tracker is still
    `STATE_IDLE`, `current_run is None`. The guard drops the packet
    with zero mutation — no `_open_run` fires, no phantom run is
    created, `state` stays `STATE_IDLE`.
    """
    tracker = RunTracker()
    assert tracker.state == STATE_IDLE
    assert tracker.current_run is None

    with caplog.at_level(
        logging.DEBUG, logger="custom_components.navimow.run_tracker"
    ):
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


# --------------------------------------------------------------------- #
# 2b. Early-hold arming — STATE_PAUSED_DOCKED with zones == []          #
# --------------------------------------------------------------------- #


def test_early_hold_paused_docked_zero_zones_vestige_dropped(caplog) -> None:
    """Edge but reachable: the state observer opens the run on the
    `docked → mowing` transition, but the robot re-docks before its
    first `type-2` — `_open_run` fired (`state == STATE_RUNNING`,
    `zones == []`), then `process_vehicle_state(VS_DOCKED_*)` moves
    the tracker to `STATE_PAUSED_DOCKED` still with `zones == []`. The
    second disjunct explicitly includes `STATE_PAUSED_DOCKED` so the
    vestige is still dropped in this early-hold window.
    """
    tracker = RunTracker()
    tracker._open_run(
        {
            "time": 900,
            "area_session": None,
            "area_week": None,
            "mowing_percentage": None,
            "mow_start_type": 1,
        }
    )
    tracker.process_vehicle_state(VS_DOCKED_CHARGING)
    assert tracker.state == STATE_PAUSED_DOCKED
    assert tracker.current_run["zones"] == []

    with caplog.at_level(
        logging.DEBUG, logger="custom_components.navimow.run_tracker"
    ):
        events = _process(
            tracker,
            _pkt(
                mp=100,
                cmp=CMP_ZONE_COMPLETE_THRESHOLD,
                sub=0.0,
                wk=0.0,
                boundary=1,
                action=-1,
                t=1_000,
            ),
        )
    assert events == []
    assert tracker.state == STATE_PAUSED_DOCKED
    assert tracker.current_run["zones"] == []
    assert tracker.current_run.get("last_mp") is None
    dbgs = [
        r.getMessage()
        for r in caplog.records
        if r.levelno == logging.DEBUG and "run-start vestige" in r.getMessage()
    ]
    assert len(dbgs) == 1, dbgs


# --------------------------------------------------------------------- #
# 3. Next real packet seeds cleanly after a dropped vestige             #
# --------------------------------------------------------------------- #


def test_real_packet_after_dropped_vestige_seeds_zone_at_real_cmp() -> None:
    """The Prunier trace continues: 56 s after the vestige, the fresh
    task's first genuine packet arrives (`mp=0, cmp=100, sub=2.47`).
    `zones[0].cmp_max` must be seeded at 100, not 10000.
    """
    tracker = RunTracker()
    tracker._open_run(
        {
            "time": 1_784_453_512_800,
            "area_session": None,
            "area_week": None,
            "mowing_percentage": None,
            "mow_start_type": 1,
        }
    )
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
    _process(
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
    zones = tracker.current_run["zones"]
    assert len(zones) == 1
    assert zones[0]["cmp_max"] == 100
    assert zones[0]["first_time"] == 1_784_453_569_397
    assert zones[0]["sub_entry"] == 2.47
    assert tracker.current_run["last_mp"] == 0


def test_inverted_order_next_real_packet_opens_run_from_idle() -> None:
    """After the vestige is dropped in the inverted ordering, the
    tracker is still `STATE_IDLE`. The next genuine packet must open a
    fresh run normally — no lingering state contamination.
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
    started = [e for e in events if e.kind == EVENT_RUN_STARTED]
    assert len(started) == 1
    zones = tracker.current_run["zones"]
    assert len(zones) == 1
    assert zones[0]["cmp_max"] == 100


# --------------------------------------------------------------------- #
# 4. Sunday first-mow — tolerances alone must never trigger the drop    #
# --------------------------------------------------------------------- #


def test_sunday_first_mow_low_sub_but_low_mp_accepted() -> None:
    """First mow of the firmware week (Sunday-start): `mp = 0, cmp =
    30, sub = 0.3, wk = 0.3`. `sub` is under the tolerance but the
    signature fails on `mp` (0 ≠ 100), so the packet must be accepted.
    The guard requires the three conjuncts together; `sub` alone never
    triggers the drop.
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
# 5. Incomplete packet (area_session is None) never matches drop        #
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
# 5b. Window boundary — post-close zero-sub packet not caught here      #
# --------------------------------------------------------------------- #


def test_post_close_vestige_shape_not_caught_by_this_guard() -> None:
    """The revised arming window (2026-07-19 fourth body edit) keys on
    tracker state, not `current_run` nullity: `current_run` remains
    referenced post-close (BUG-16's guard reads `last_sub` from it in
    `STATE_COMPLETED`). A vestige-shape packet (`mp = 100 ∧ cmp =
    10000 ∧ sub = 0`) arriving in `STATE_COMPLETED` is BUG-13
    territory (#86) — the zero-`sub` variant — and must flow through
    to the post-close branches unchanged. This test pins that the
    BUG-17 guard is intentionally dark post-close.

    Observable: the packet reaches the post-close `is_reset` branch
    (0 < last_sub → reset; 0 < RESET_SUB_CEILING → immediate re-open),
    so a fresh `run_started` fires and the tracker moves back to
    `STATE_RUNNING` — precisely the BUG-13 pathology, kept out of
    scope of BUG-17 by decision.
    """
    tracker = RunTracker()
    # Complete a genuine mow so the tracker enters STATE_COMPLETED with
    # current_run still referenced.
    _process(tracker, _pkt(mp=0, cmp=100, sub=2.47, wk=2.42, boundary=1, t=1_000))
    _process(tracker, _pkt(mp=50, cmp=5000, sub=100.0, wk=100.0, boundary=1, t=2_000))
    _process(tracker, _pkt(mp=100, cmp=10000, sub=232.89, wk=232.89, boundary=1, t=3_000))
    tracker.process_vehicle_state(VS_DOCKED_CHARGING)
    assert tracker.state == STATE_COMPLETED
    assert tracker.current_run is not None
    assert tracker.current_run.get("last_sub") == 232.89

    # Move the robot back off the dock — otherwise BUG-14's fast-path
    # re-fires on the fresh post-reset run and re-closes it (mp=100
    # ∧ vs∈{1,2,3}). That interaction is real but orthogonal to what
    # this test pins: the BUG-17 guard's inertness post-close.
    tracker.process_vehicle_state(VS_MOWING)

    # Zero-sub vestige-shape packet post-close — BUG-17 guard must not
    # catch this. The post-close `is_reset` branch handles it (BUG-13
    # territory).
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
    # Not dropped: state transitioned out of STATE_COMPLETED via the
    # post-close reset branch → new run_started fired.
    started = [e for e in events if e.kind == EVENT_RUN_STARTED]
    assert len(started) == 1
    assert tracker.state == STATE_RUNNING
    # The fresh run's sub₀ is the vestige packet's sub (0.0) — this is
    # exactly BUG-13's phantom-open pathology, deliberately left to
    # #86's watch.
    assert tracker.current_run["sub0"] == 0.0


# --------------------------------------------------------------------- #
# 6. Mid-run genuine completion (zones non-empty) is not gated          #
# --------------------------------------------------------------------- #


def test_mid_run_mp_100_cmp_10000_completion_accepted() -> None:
    """A legitimate mid-run `mp = 100 ∧ cmp = 10000` completion packet
    (final packet of a real mow) must be accepted. `zones` is non-empty
    at this point, so neither arming-window disjunct fires — the guard
    is inert.
    """
    tracker = RunTracker()
    # Fresh mow ramps up.
    _process(tracker, _pkt(mp=0, cmp=100, sub=2.47, wk=2.42, boundary=1, t=1_000))
    _process(tracker, _pkt(mp=50, cmp=5000, sub=100.0, wk=100.0, boundary=1, t=2_000))
    # Genuine completion — mp=100, cmp=10000, sub is *not* zero.
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
    # Packet accepted → last_mp advanced, zone cmp_max at 10000.
    assert tracker.current_run["last_mp"] == 100
    assert tracker.current_run["zones"][-1]["cmp_max"] == CMP_ZONE_COMPLETE_THRESHOLD
    # No premature `run_finished` here — the fast path fires on dock.
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
    # Accepted — mp advanced, cmp_max hit 10000, `last_sub` regressed to 0.0.
    assert tracker.current_run["last_mp"] == 100
    assert tracker.current_run["zones"][-1]["cmp_max"] == CMP_ZONE_COMPLETE_THRESHOLD


# --------------------------------------------------------------------- #
# 7. Session-2 resume packet is not gated (no vestige on _reopen_run)   #
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
    # Session 1 progresses on Prunier.
    _process(tracker, _pkt(mp=0, cmp=100, sub=2.47, wk=2.42, boundary=1, t=1_000))
    _process(tracker, _pkt(mp=65, cmp=5000, sub=200.0, wk=200.0, boundary=1, t=2_000))
    # Robot docks for low-battery recharge (interrupt).
    tracker.process_vehicle_state(VS_DOCKED_IDLE)
    # Robot leaves the dock, resumes mowing.
    tracker.process_vehicle_state(VS_MOWING)
    # First real Session-2 packet (from Figuier at cmp=222, no vestige).
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
    # Accepted — Figuier zone opened at cmp=222 (real seed).
    zones = tracker.current_run["zones"]
    assert any(z["boundary_id"] == 3 and z["cmp_max"] == 222 for z in zones)
    # No spurious run_started/run_finished emitted here.
    assert [e for e in events if e.kind in (EVENT_RUN_STARTED, EVENT_RUN_FINISHED)] == []


# --------------------------------------------------------------------- #
# 8. Suspicious partial shape — accepted, DEBUG asserted                #
# --------------------------------------------------------------------- #


def test_suspicious_partial_shape_accepted_with_debug(caplog) -> None:
    """The observability line covers the unobserved `interrupted`
    vestige shape (open question 1): `sub` near zero at run-start
    *without* the full `mp = 100 ∧ cmp = 10000` match. The packet is
    accepted, but a distinct DEBUG line records the shape for later
    analysis. Zero drop risk (the guard rejection condition is not
    met).
    """
    tracker = RunTracker()
    with caplog.at_level(
        logging.DEBUG, logger="custom_components.navimow.run_tracker"
    ):
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
    # Accepted: run_started emitted, run opened, last_mp advanced.
    started = [e for e in events if e.kind == EVENT_RUN_STARTED]
    assert len(started) == 1
    assert tracker.state == STATE_RUNNING
    assert tracker.current_run["last_mp"] == 100

    suspicious = [
        r.getMessage()
        for r in caplog.records
        if r.levelno == logging.DEBUG
        and "run-start suspicious shape" in r.getMessage()
    ]
    assert len(suspicious) == 1, suspicious
    # And *no* vestige-rejection DEBUG line — the packet was accepted.
    rejects = [
        r.getMessage()
        for r in caplog.records
        if r.levelno == logging.DEBUG and "run-start vestige" in r.getMessage()
    ]
    assert rejects == []


# --------------------------------------------------------------------- #
# 9. End-to-end — interrupted run before cmp=10000 → cmp_max is real    #
# --------------------------------------------------------------------- #


class _FakeClock:
    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_interrupted_run_after_dropped_vestige_reports_real_cmp_max() -> None:
    """The BUG-17 blast-radius scenario the diag calls out: a
    vestige-poisoned first-in-run zone that gets interrupted before
    the real `cmp` reaches 10000. Pre-fix the tracker would emit
    `zones[0].cmp_max = 10000` on the close event, silently over-
    stating `Store.last_cmp_max` (which ZoneRegistry.ingest_run reads
    at run close). With the guard in place, `cmp_max` on the emitted
    close event reflects the true observed maximum.
    """
    from custom_components.navimow.run_tracker import INTERRUPT_SUSTAIN_SECONDS

    clock = _FakeClock()
    tracker = RunTracker(clock=clock)
    tracker._open_run(
        {
            "time": 900,
            "area_session": None,
            "area_week": None,
            "mowing_percentage": None,
            "mow_start_type": 1,
        }
    )
    # Vestige — dropped.
    _process(
        tracker,
        _pkt(
            mp=100,
            cmp=CMP_ZONE_COMPLETE_THRESHOLD,
            sub=0.0,
            wk=0.0,
            boundary=1,
            action=-1,
            t=1_000,
        ),
    )
    # Real mow progresses only partway: cmp climbs to 3111 (mp=20).
    _process(tracker, _pkt(mp=0, cmp=100, sub=2.47, wk=2.42, boundary=1, t=2_000))
    _process(tracker, _pkt(mp=10, cmp=1500, sub=50.0, wk=50.0, boundary=1, t=3_000))
    _process(tracker, _pkt(mp=20, cmp=3111, sub=75.0, wk=75.0, boundary=1, t=4_000))

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
# Sanity: tolerance constant is small enough not to swallow real runs   #
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
