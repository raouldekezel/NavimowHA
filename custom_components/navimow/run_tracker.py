"""FEAT-05 step (b) — pure run/zone tracker fed by /location packets.

Design: FEAT-05 SPIKE record + Fable implementation brief on #43.

Zero HA imports so the tracker is unit-testable without spinning up the
core loop, matching the testability pattern of `location.py`. The
coordinator instantiates one tracker per device and forwards:

- `process_type2(parsed)` — for every type-2 item accepted by the
  layer-1 ordering guard (FEAT-05 step (a)).
- `process_vehicle_state(vs)` — for every accepted type-1 item whose
  `vehicleState` differs from the previously seen value.
- `tick(now=None)` — periodically (coordinator tick cadence, ~30 s) so
  the sustained-60 s docked-idle interruption detector can fire even
  when no MQTT traffic is arriving.

Every call returns a list of `Event` records the caller can dispatch to
Home Assistant. Step (c) turns those into HA events + entity updates;
step (b) verifies them via `list[Event]` returns in the test suite.

Guard layers (post-HARD-06 truth, #62 — the last blocking content guard
was demoted to observability after a packet-economics audit found zero
true positives in production and two demonstrated silent-massive-
rejection failures):

- **Layer 1 — blocking, in the coordinator.** `/location` `time`
  monotonicity per stream (FEAT-05 step (a) / #48). Evidence-backed:
  caught the 1 h 38 late-arriving packet on the committed corpus.
  Not visible from this module — the tracker trusts its input.
- **Strict-progress echo filter — blocking, here.** On post-close
  transitions from `COMPLETED` / `INTERRUPTED`, a fresh packet must
  strictly advance `sub` (or, if unavailable, `mp`) over the closed
  run's last accepted values. Evidence-backed: caught the event-spam
  adversarial B1 on #49. Worst case: session start delayed by the
  transit-length echo tail.
- **Pending-reset deferral — blocking, here.** A `sub` regression above
  `RESET_SUB_CEILING` is not accepted immediately: the packet is
  stashed and only promoted retroactively when the next packet confirms
  it coherently on `time`, `sub`, and the `wk − sub` shift.
  Evidence-backed: neutralised the mixed-epoch poison adversarials B2
  on #49. Worst case: 30–90 s detection latency; the check is
  packet-vs-packet (never packet-vs-anchor) so a single anomalous
  packet cannot destroy a live run.
- **Observability — never blocking.**
  * `mowingWeekArea` regressions against the last accepted cursor:
    logged at DEBUG, counted in `counters["wk_regressions_observed"]`,
    with a one-shot WARN at `WK_REGRESSION_STREAK_TO_WARN` consecutive
    observations. Layer 2 was demoted on BUG-10 / #58 (evidence audit:
    zero true positives, one catastrophic all-day false positive on a
    Sunday firmware `wk` reset).
  * Invariant deviation `|wk − sub − wk₀|` against the open run's
    `wk₀`: logged at DEBUG, counted in
    `counters["invariant_deviations_observed"]`, with a one-shot WARN
    at `INVARIANT_DEVIATION_STREAK_TO_WARN` consecutive observations.
    Layer 3 was demoted on HARD-06 / #62 (evidence audit: zero true
    positives in production, two silent-massive-rejection failures —
    the BUG-10 mid-run `wk` reset and the FEAT-06 review-fixture 2 m²
    anchor drift that dropped an entire afternoon without a WARN).
    `wk₀` stays anchored once per run as the pure reference; no
    re-anchoring machinery. Runs and per-session areas are
    `sub`-based and unaffected by a `wk` reset.

The invariant streak WARN fires only against a LIVE run's stable
`wk₀`; on the post-close new-session path it is measured against the
closed run's `wk₀` for exactly the first packet before `_open_run`
re-anchors, so persistent drift there is structurally impossible.

No calendar assumption survives contact with evidence: nothing here
encodes what week (ISO Monday, Sunday, etc.) the firmware follows.

State machine (converged, authoritative per the brief; BUG-09 revised;
FEAT-06 revised — session-scoped runs):

    IDLE ─fresh type-2─▶ RUNNING [run_started]
    RUNNING ─vs ∈ {1,2,3,6}─▶ PAUSED_DOCKED
    PAUSED_DOCKED ─fresh type-2, strict progress─▶ RUNNING   (resume,
        same run — intra-run recharge dock does not split)
    RUNNING/PAUSED_DOCKED ─mp ≥ MP_COMPLETION_THRESHOLD (99)
        ∧ vs ∈ {1,2,3}─▶ COMPLETED [run_finished]
    RUNNING/PAUSED_DOCKED ─fresh reset (sub < last, sub < ceiling)─▶
        close open run [run_finished], open new run [run_started]
    PAUSED_DOCKED ─vs ∈ {1,3} sustained 60 s─▶ INTERRUPTED
        [run_finished]
    COMPLETED/INTERRUPTED ─fresh type-2 with strict progress─▶
        open NEW run [run_started] (FEAT-06 / #54)
    COMPLETED/INTERRUPTED ─fresh reset (sub < ceiling)─▶ open new run

BUG-09 (2026-07-04): the observed i210 firmware never emits `mp = 100`
— tasks terminate at `mp = 99`. The completion criterion is therefore
`mp ≥ MP_COMPLETION_THRESHOLD` (99) ∧ `vs ∈ {1, 2, 3}` (docked idle /
charging / unpowered; user pause `vs = 6` excluded so a manual pause
still holds the run even at `mp = 99`). Immediate close, no debounce.
The result label is centralised in `_close_run`: `completed` iff the
last accepted `mp ≥ MP_COMPLETION_THRESHOLD`, else `interrupted` — so
every close path (fast, reset, sustained-timer) labels consistently.

FEAT-06 (2026-07-05, #54): a run maps to a **user session** — an
activation → final dock cycle. Intra-run recharge docks (vs=2 while
`mp < threshold`) do NOT split the session (unchanged). What changes
is the post-close boundary: a fresh accepted type-2 arriving after a
`COMPLETED` / `INTERRUPTED` no longer *reopens* the closed run — it
opens a **new run** at that packet's time, with `sub₀` = that packet's
`sub`. Per-session area is then `sub − sub₀` (per-zone deltas already
use absolute `sub` pairs and are unaffected). The `run_reopened` event
is retired.

Task vs session scoping — the fact this design turns on (recorder TSV
`docs/diag/2026-07-04_spike-02_run-semantics-task-vs-session/` from
PR #55, first afternoon packet at `mp=65` while the morning had just
completed at `mp=99`): the firmware's `mp` re-bases on a fresh task
definition (freshly-mowed zones are credited — non-zero session starts
are normal), while `sub` (`subtotalArea`) keeps accumulating across
tasks. `wk − sub` therefore remains an invariant across a task series;
layer 3 keeps guarding continuation shape. `run_progress` documented
as *task* progress on the sensor side; run identity keys on `sub`.

A `sub` regression *above* `RESET_SUB_CEILING` is not accepted as an
immediate reset — the packet is stashed as a *pending* reset and only
promoted retroactively when the next accepted packet confirms it
coherently (B2 on #49: one anomalous packet must not destroy a live
run). New-session transitions from COMPLETED / INTERRUPTED require
strict `sub > last_sub` progress (B1 on #49: an echo packet after a
close must not spawn phantom sessions).

vs=2 (charging) still holds PAUSED_DOCKED when the run has not reached
the completion threshold — a mid-run recharge pause below `mp = 99`
never times out. vs=8 (firmware-reset transient, MAP-01) is ignored.
`boundary=0` (BUG-06 sentinel) is excluded from zone accounting but
still updates run accumulators.
"""

from __future__ import annotations

import copy
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

_LOGGER = logging.getLogger(__name__)

# --------------------------------------------------------------------- #
# Constants                                                             #
# --------------------------------------------------------------------- #

# Tracker states (internal, distinct from the display `run_state`
# enum step (c) will expose).
STATE_IDLE = "idle"
STATE_RUNNING = "running"
STATE_PAUSED_DOCKED = "paused_docked"
STATE_COMPLETED = "completed"
STATE_INTERRUPTED = "interrupted"

# vehicleState values (from MAP-01 diag / #25).
VS_DOCKED_IDLE = 1
VS_DOCKED_CHARGING = 2
VS_DOCKED_UNPOWERED = 3
VS_MOWING = 4
VS_RETURNING = 5
VS_PAUSED = 6
VS_TRANSIENT = 8  # firmware-reset transient (posture all-zero)

# Any docked variant that puts an open run on hold.
DOCKED_STATES = frozenset(
    {VS_DOCKED_IDLE, VS_DOCKED_CHARGING, VS_DOCKED_UNPOWERED, VS_PAUSED}
)
# Docked-and-not-charging: signal that a recharge is not imminent.
# vs=2 (charging) → resume coming; vs=6 (explicit pause) → user in
# control, no timeout; vs ∈ {1, 3} → terminal for the open run once
# sustained.
DOCKED_NOT_CHARGING = frozenset({VS_DOCKED_IDLE, VS_DOCKED_UNPOWERED})

# BUG-09: docked variants that qualify for the mp-completion criterion.
# vs = 6 (explicit user pause) is excluded so a manual pause at mp = 99
# still holds the run — the user is in control and may resume.
DOCKED_NOT_USER_PAUSED = frozenset(
    {VS_DOCKED_IDLE, VS_DOCKED_CHARGING, VS_DOCKED_UNPOWERED}
)

# BUG-09: mp threshold that marks a task-scoped run as complete. The
# observed i210 firmware never emits `mp = 100` — tasks terminate at
# `mp = 99` (2026-07-04 diag: real run peaked at 99, robot returned to
# dock, no further mp progression). Set to 99 to catch normal
# completions while remaining strict enough that a mid-run recharge
# pause at `mp < 99` still holds PAUSED_DOCKED.
MP_COMPLETION_THRESHOLD = 99

# Seconds a PAUSED_DOCKED run must remain in DOCKED_NOT_CHARGING before
# it is declared INTERRUPTED. 60 s ≈ 30 type-1 samples at the 2 s
# cadence — ample debounce for dock-contact transients while keeping
# end-of-run reporting timely.
INTERRUPT_SUSTAIN_SECONDS = 60

# Layer-3 tolerance around the wk₀+sub invariant (Fable brief).
INVARIANT_TOLERANCE_M2 = 0.5

# Sub ceiling below which a `sub` regression is treated as an *immediate*
# reset (a genuine run just started). Above the ceiling, the packet is
# treated as a candidate for a *pending* reset that a coherent successor
# must confirm — otherwise it is discarded as a content anomaly. 10.0 m²
# gives roughly 4× headroom over every genuine run-start `sub` ever
# committed (0.39 m² on 2026-05-25, 2.6 m² on 2026-07-03).
RESET_SUB_CEILING = 10.0

# Observability streak thresholds. After this many consecutive
# observations the tracker emits one WARNING so an operator sees the
# anomaly in real time rather than only through the counter attribute.
# Streak resets on any non-observing packet — routine transitions (fresh
# session start on a new week, first packet of a new session on the
# post-close path) never reach the threshold. Chosen small enough
# (~2.5 min at the 30 s type-2 cadence) that the WARN lands while the
# anomaly is still actionable.
WK_REGRESSION_STREAK_TO_WARN = 5

# HARD-06 (#62): the invariant `|wk − sub − wk₀|` is observability, not
# a blocking guard. A persistent streak against a LIVE run's stable
# `wk₀` (never re-anchored) means the firmware reset `wk` mid-run; the
# accepted packets keep the run alive on `sub`, and the sustained-timer
# closes it via `vs` as usual.
INVARIANT_DEVIATION_STREAK_TO_WARN = 5

# Event kinds.
EVENT_RUN_STARTED = "run_started"
EVENT_RUN_FINISHED = "run_finished"

# Run result values (payload of run_finished events).
RESULT_COMPLETED = "completed"
RESULT_INTERRUPTED = "interrupted"

# Snapshot format version — bump when the shape of `snapshot()` changes
# so `restore()` can refuse an incompatible older payload rather than
# silently loading a corrupted state.
SNAPSHOT_VERSION = 1


# --------------------------------------------------------------------- #
# Event type                                                            #
# --------------------------------------------------------------------- #


@dataclass(frozen=True)
class Event:
    """A single change the tracker wants surfaced.

    `kind` selects the event type (`EVENT_RUN_STARTED` etc.); `payload`
    is opaque per-kind data the coordinator/entity layer will consume.
    """

    kind: str
    payload: dict[str, Any] = field(default_factory=dict)


# --------------------------------------------------------------------- #
# Tracker                                                               #
# --------------------------------------------------------------------- #


class RunTracker:
    """Turn a stream of `/location` type-2 and type-1 payloads into a
    run/zone timeline plus HA-agnostic events. See module docstring for
    the state machine and guard layers.
    """

    def __init__(self, *, clock: Callable[[], float] | None = None) -> None:
        # Injectable monotonic clock (seconds). Only used for the
        # sustained-60 s interruption timer — never for firmware time
        # comparisons (those come from the packets themselves and live
        # on a different axis).
        self._clock: Callable[[], float] = clock or time.monotonic

        self.state: str = STATE_IDLE
        self.vehicle_state: int | None = None

        # Open OR most-recently-closed run; `None` at cold boot only.
        # Kept across a close so a reopen can compare `sub` values.
        self.current_run: dict[str, Any] | None = None

        # Layer-2 anchor: last accepted /location type-2 `wk`/`time`.
        self._last_accepted_wk: float | None = None
        self._last_accepted_time_ms: int | None = None

        # Interruption timer, monotonic seconds. `None` while charging /
        # paused / running; set to `_clock()` when we enter
        # DOCKED_NOT_CHARGING under an open run.
        self._interrupt_timer_started_at: float | None = None

        # Pending-reset candidate: a packet with `sub` below the previous
        # `last_sub` but *above* RESET_SUB_CEILING (so not obviously a
        # genuine run start). Held in-memory only — a mid-run HA restart
        # would forget it, at the cost of one packet of re-observation
        # latency, which is defensible (the alternative is serialising a
        # transient decision across restarts). Confirmed retroactively by
        # the next coherent packet; discarded otherwise.
        self._pending_reset: dict[str, Any] | None = None

        # Diagnostic counters. `drops` = packets the tracker refused
        # (held as a pending reset — the only remaining refusal path in
        # the module); `counters` = events observed but *not* acted on.
        # `wk_regressions_observed` replaced the old `drops["layer_2"]`
        # on BUG-10 / #58, `invariant_deviations_observed` replaced
        # `drops["layer_3"]` on HARD-06 / #62 — both are now
        # observability signals with streak-WARN escalation.
        self.drops: dict[str, int] = {
            "pending_reset_holds": 0,
        }
        self.counters: dict[str, int] = {
            "wk_regressions_observed": 0,
            "invariant_deviations_observed": 0,
        }
        # Consecutive observation streaks feeding the throttled WARNINGs
        # in `_observe_wk_regression` / `_observe_invariant_deviation`.
        # In-memory only — not snapshotted, so a mid-anomaly restart
        # re-arms the WARN from zero (the persistent counters still tell
        # the operator that observations happened; the streaks are
        # real-time signals, not ledgers).
        self._wk_regression_streak: int = 0
        self._invariant_deviation_streak: int = 0

    # ------------------------------------------------------------- #
    # Public API                                                    #
    # ------------------------------------------------------------- #

    def process_type2(self, parsed: dict[str, Any]) -> list[Event]:
        """Feed a type-2 payload (already through the layer-1 guard).

        `parsed` must be the dict returned by
        `location.parse_location_type_2`.
        """
        events: list[Event] = []

        # BUG-10 (2026-07-05, #58): layer 2 is now observability. A `wk`
        # regression is logged at DEBUG and counted, but the packet
        # proceeds through the rest of the machine. Rationale in the
        # module docstring; the evidence audit is on issue #58.
        self._observe_wk_regression(parsed)

        # Resolve any previously-stashed pending reset first — the
        # current packet may confirm or discard it before we look at
        # its own reset semantics.
        events.extend(self._resolve_pending_reset(parsed))

        prev_sub = self.current_run["last_sub"] if self.current_run else None
        incoming_sub = parsed.get("area_session")
        incoming_mp = parsed.get("mowing_percentage")

        is_reset = (
            prev_sub is not None
            and incoming_sub is not None
            and incoming_sub < prev_sub
        )

        # State transition — determine whether we open, close, open a
        # new session, or continue. The invariant `|wk − sub − wk₀|` is
        # observed (not enforced, HARD-06 / #62) on continuations and
        # post-close new-session opens; every packet proceeds.
        if self.state == STATE_IDLE:
            self._open_run(parsed)
            events.append(self._event_run_started())
        elif self.state in (STATE_RUNNING, STATE_PAUSED_DOCKED):
            if is_reset:
                # Split by ceiling: sub << ceiling → obviously a genuine
                # run-start; sub above → hold as a pending candidate that
                # a coherent successor must confirm (BUG-08-style
                # mixed-epoch packets must not destroy a live run).
                if incoming_sub is not None and incoming_sub < RESET_SUB_CEILING:
                    events.append(self._close_run())
                    self._open_run(parsed)
                    events.append(self._event_run_started())
                else:
                    self._stash_pending_reset(parsed)
                    return events
            else:
                # Continuation — observe the invariant against the open
                # run's `wk₀`, then accept. A persistent deviation streak
                # WARNs at HARD-06 / #62 threshold; it never blocks.
                self._observe_invariant_deviation(parsed)
                # Resume from a pause when a fresh type-2 continues.
                if self.state == STATE_PAUSED_DOCKED:
                    self.state = STATE_RUNNING
                    self._interrupt_timer_started_at = None
        elif self.state in (STATE_COMPLETED, STATE_INTERRUPTED):
            if is_reset:
                if incoming_sub is not None and incoming_sub < RESET_SUB_CEILING:
                    self._open_run(parsed)
                    events.append(self._event_run_started())
                else:
                    self._stash_pending_reset(parsed)
                    return events
            else:
                # FEAT-06 (#54): a fresh accepted type-2 with strict
                # progress after a close no longer *reopens* the closed
                # run — it opens a **new session** anchored at this
                # packet's time and `sub`. Strict progress rejects echo
                # packets (identical `sub` / `mp` with only `time`
                # fresher) — otherwise a stream tail would spawn phantom
                # sessions after every close (B1 on #49 generalised).
                if not self._has_strict_progress(parsed):
                    return events
                # HARD-06 (#62): observe the invariant against the
                # CLOSED run's `wk₀` BEFORE `_open_run` re-anchors,
                # so the observability reference is the anchor the
                # packet was expected to share — the one whose 2 m²
                # drift silently killed an afternoon in the review
                # fixture. Post `_open_run`, subsequent packets sit
                # under the fresh anchor and are within tolerance;
                # persistent drift here is structurally impossible.
                self._observe_invariant_deviation(parsed)
                self._open_run(parsed)
                events.append(self._event_run_started())

        # Bookkeeping on acceptance. `_update_wk0_anchor` handles the
        # "first packet with data" case; keeping the mutation out of
        # `_observe_invariant_deviation` keeps that observer a pure
        # read.
        self._update_wk0_anchor(parsed)
        self._update_accumulators(parsed)
        self._update_zone(parsed)

        # Acceptance stamps the wk/time cursors used by the
        # wk-regression observer.
        if parsed.get("area_week") is not None:
            self._last_accepted_wk = parsed["area_week"]
        if parsed.get("time") is not None:
            self._last_accepted_time_ms = parsed["time"]

        # BUG-09: completion criterion (`mp ≥ 99 ∧ vs ∈ {1,2,3}`). Fires
        # when a fresh type-2 pushes `last_mp` over the threshold while
        # the robot is already docked (the mp-then-dock path is handled
        # by `process_vehicle_state`).
        completion = self._maybe_complete_run()
        if completion is not None:
            events.append(completion)

        return events

    def process_vehicle_state(self, vs: int) -> list[Event]:
        """React to a `vehicleState` change (type-1 packet).

        Only entries into `DOCKED_STATES` from `RUNNING` move the machine
        (`RUNNING → PAUSED_DOCKED`). Resume is driven by a fresh type-2
        in `process_type2`, not by a vs=4/5 signal — type-1 briefly
        showing `vs=4` during a dock-poke must not falsely "resume" the
        run.
        """
        events: list[Event] = []

        if vs == VS_TRANSIENT:
            return events

        self.vehicle_state = vs

        if self.state == STATE_RUNNING:
            if vs in DOCKED_STATES:
                self.state = STATE_PAUSED_DOCKED
                self._start_interrupt_timer_if_applicable(vs)
        elif self.state == STATE_PAUSED_DOCKED:
            # Charging / explicit pause reset the timer; docked-and-not-
            # -charging arms it.
            self._start_interrupt_timer_if_applicable(vs)

        # BUG-09: the run may already have reached the mp threshold
        # before the robot arrived at the dock — process_type2 alone
        # can't fire the close in that ordering because no further
        # type-2 packet is guaranteed after dock arrival. Firing here
        # closes the run as soon as `vs` enters {1, 2, 3}.
        completion = self._maybe_complete_run()
        if completion is not None:
            events.append(completion)

        return events

    def tick(self, now: float | None = None) -> list[Event]:
        """Advance the sustained-docked interruption timer.

        Called periodically (coordinator cadence ~30 s). Two roles:
        - Arm the timer if we are `PAUSED_DOCKED` under
          `DOCKED_NOT_CHARGING` and it is not yet running. This makes
          the interruption detector survive an HA restart *without*
          needing a fresh `vehicleState` change to re-arm — a `restore()`
          followed by a tick suffices.
        - Fire `run_finished` once the timer has been armed for at
          least `INTERRUPT_SUSTAIN_SECONDS` and nothing has resumed the
          run. The result label is derived from `last_mp` in
          `_close_run`, so a sustained-timer close after the robot
          completed (mp ≥ 99) is labelled `completed`, not
          `interrupted` (BUG-09 label consistency).
        """
        events: list[Event] = []
        now = self._clock() if now is None else now

        if (
            self.state == STATE_PAUSED_DOCKED
            and self.vehicle_state in DOCKED_NOT_CHARGING
        ):
            if self._interrupt_timer_started_at is None:
                self._interrupt_timer_started_at = now
            elif (now - self._interrupt_timer_started_at) >= INTERRUPT_SUSTAIN_SECONDS:
                events.append(self._close_run())

        return events

    # ------------------------------------------------------------- #
    # Guards + observability                                        #
    # ------------------------------------------------------------- #

    def _observe_wk_regression(self, parsed: dict[str, Any]) -> None:
        """Count and log wk regressions without dropping the packet.

        Replaces the old blocking `_passes_layer_2` (BUG-10 / #58): a
        Sunday firmware `wk` reset made every incoming packet regress
        against the cursor and the guard rejected the whole day. No
        calendar assumption survives here — anything that looks like a
        firmware reset (or a genuine content anomaly for that matter)
        goes through the reset / pending-reset / layer-3 machinery
        instead.

        Also drives a streak counter and one-shot WARNING: after
        `WK_REGRESSION_STREAK_TO_WARN` consecutive regressions the
        tracker emits a single WARNING so the operator sees the anomaly
        in real time rather than only through the counter attribute.
        Streak resets on any non-regressing packet, so a routine
        fresh-session start (one regression, cursor advances via the
        reset path, next packet already ahead of the cursor) never
        trips it.
        """
        wk = parsed.get("area_week")
        if wk is None or self._last_accepted_wk is None:
            return
        if wk >= self._last_accepted_wk:
            self._wk_regression_streak = 0
            return
        self.counters["wk_regressions_observed"] += 1
        self._wk_regression_streak += 1
        _LOGGER.debug(
            "run_tracker: wk regression observed (wk=%s last=%s time=%s "
            "streak=%d) — accepting, layer 2 is observability only "
            "(BUG-10 / #58)",
            wk,
            self._last_accepted_wk,
            parsed.get("time"),
            self._wk_regression_streak,
        )
        if self._wk_regression_streak == WK_REGRESSION_STREAK_TO_WARN:
            _LOGGER.warning(
                "run_tracker: %d consecutive wk regressions observed "
                "against cursor=%.2f (state=%s). Packets are accepted "
                "— wk checks are observability only (BUG-10 #58, "
                "HARD-06 #62). A persistent streak means the firmware "
                "reset its weekly counter mid-run; run identity and "
                "session_area are sub-based and unaffected. Total in "
                "counters['wk_regressions_observed'].",
                self._wk_regression_streak,
                self._last_accepted_wk,
                self.state,
            )

    def _observe_invariant_deviation(self, parsed: dict[str, Any]) -> None:
        """Observe `|wk − sub − wk₀|` without blocking the packet
        (HARD-06 / #62).

        Was `_passes_layer_3`, a blocking predicate whose audit on #62
        showed zero true positives in production and two silent-massive-
        rejection failures (BUG-10 mid-run wk reset; FEAT-06 review-
        fixture 2 m² anchor drift). Every packet the tracker now drops
        is dropped by an evidence-backed guard (layer 1, strict
        progress, pending-reset) — this one only observes.

        Semantics mirror `_observe_wk_regression`:
        - Short-circuit on missing `wk` / `sub` / `wk₀` without touching
          the streak (nothing to compare, nothing to reset).
        - When the deviation exceeds `INVARIANT_TOLERANCE_M2`, increment
          `counters["invariant_deviations_observed"]`, DEBUG-log the
          values, bump the streak, WARN exactly once when the streak
          reaches `INVARIANT_DEVIATION_STREAK_TO_WARN` (streak-equality
          gate — the same throttle as the wk-regression WARN).
        - When the deviation is within tolerance, reset the streak.

        The WARN's semantic is "persistent deviation against a LIVE
        anchor". On the post-close new-session path the observation
        fires exactly once against the closed run's `wk₀` before
        `_open_run` re-anchors — a streak there is structurally
        impossible. On a mid-run `wk` reset the streak climbs against
        the open run's never-re-anchored anchor and the WARN lands
        while the sustained-timer still closes the run cleanly via
        `vs`; `session_area` is `sub`-only and unaffected.
        """
        if self.current_run is None:
            return
        wk = parsed.get("area_week")
        sub = parsed.get("area_session")
        if wk is None or sub is None:
            return
        wk0 = self.current_run.get("wk0")
        if wk0 is None:
            # No anchor yet — nothing to compare against; the acceptance
            # path will set it via `_update_wk0_anchor`.
            return
        deviation = abs(wk - sub - wk0)
        if deviation <= INVARIANT_TOLERANCE_M2:
            self._invariant_deviation_streak = 0
            return
        self.counters["invariant_deviations_observed"] += 1
        self._invariant_deviation_streak += 1
        _LOGGER.debug(
            "run_tracker: invariant deviation observed "
            "(wk=%s sub=%s wk0=%s deviation=%.3f tol=%s time=%s state=%s "
            "streak=%d) — accepting, layer 3 is observability only "
            "(HARD-06 / #62)",
            wk,
            sub,
            wk0,
            deviation,
            INVARIANT_TOLERANCE_M2,
            parsed.get("time"),
            self.state,
            self._invariant_deviation_streak,
        )
        if self._invariant_deviation_streak == INVARIANT_DEVIATION_STREAK_TO_WARN:
            _LOGGER.warning(
                "run_tracker: %d consecutive invariant deviations "
                "observed against wk0=%.2f (state=%s). Packets are "
                "accepted — the invariant is observability only "
                "(HARD-06 #62). A persistent streak against a live "
                "anchor means the firmware reset wk mid-run or the "
                "map was edited during a task; run identity and "
                "session_area are sub-based and unaffected. Total in "
                "counters['invariant_deviations_observed'].",
                self._invariant_deviation_streak,
                wk0,
                self.state,
            )

    def _update_wk0_anchor(self, parsed: dict[str, Any]) -> None:
        """Initialise `wk₀` on the first packet with (`wk`, `sub`) after
        `_open_run` — for the case where `_open_run` was fed a packet
        without both fields. Once set, `wk₀` is stable for the life of
        the run; a firmware `wk` reset mid-run is never re-anchored so
        the deviation observer can see it. `wk₀` is purely the
        observability reference now (HARD-06 / #62); nothing blocks on
        it.
        """
        if self.current_run is None:
            return
        wk = parsed.get("area_week")
        sub = parsed.get("area_session")
        if wk is None or sub is None:
            return
        if self.current_run.get("wk0") is None:
            self.current_run["wk0"] = wk - sub

    # ------------------------------------------------------------- #
    # Reset semantics (immediate / pending / echo)                  #
    # ------------------------------------------------------------- #

    def _has_strict_progress(self, parsed: dict[str, Any]) -> bool:
        """True when the incoming packet shows strict progress over the
        closed run's last accepted values. Used to gate the FEAT-06
        new-session transition against echo packets — an identical
        repeat of the closing packet with only `time` fresher would
        otherwise spawn a phantom session after every close (B1 on #49
        generalised).
        """
        if self.current_run is None:
            return True
        last_sub = self.current_run.get("last_sub")
        incoming_sub = parsed.get("area_session")
        if incoming_sub is not None and last_sub is not None:
            return incoming_sub > last_sub
        last_mp = self.current_run.get("last_mp")
        incoming_mp = parsed.get("mowing_percentage")
        if incoming_mp is not None and last_mp is not None:
            return incoming_mp > last_mp
        # Neither axis available → conservative default (no reopen).
        return False

    def _stash_pending_reset(self, parsed: dict[str, Any]) -> None:
        """Hold a candidate reset packet until a coherent successor
        confirms it. Discards any prior stash — the newer candidate
        supersedes.
        """
        self._pending_reset = dict(parsed)
        self.drops["pending_reset_holds"] += 1
        _LOGGER.debug(
            "run_tracker: pending reset stashed (sub=%s wk=%s time=%s)",
            parsed.get("area_session"),
            parsed.get("area_week"),
            parsed.get("time"),
        )

    def _resolve_pending_reset(self, parsed: dict[str, Any]) -> list[Event]:
        """Decide the fate of a previously stashed pending reset.

        Called at the top of every `process_type2` acceptance. Returns
        the events emitted if the pending is confirmed (close old run +
        open new run), or an empty list if it is discarded or if there
        is nothing pending.

        HARD-06 (#62) explicitly leaves this internal coherence check
        untouched. It compares the candidate packet to its successor
        (packet-vs-packet), NOT a packet to a stored run anchor
        (packet-vs-anchor, which was layer 3's shape). Its failure
        mode is bounded to discarding the candidate — the next packet
        becomes one — and it is the evidence-backed mechanism that
        neutralised the #49 mixed-epoch poison adversarials. The
        `abs((p_wk - p_sub) - (c_wk - c_sub)) <= INVARIANT_TOLERANCE_M2`
        term below is that packet-vs-packet check; do not conflate
        with the demoted layer 3.
        """
        events: list[Event] = []
        candidate = self._pending_reset
        if candidate is None:
            return events

        # Coherence requires: strictly later `time`, no `sub` regression
        # against the candidate, and a layer-3-tolerated shift on the
        # candidate's implied anchor.
        c_time = candidate.get("time")
        c_sub = candidate.get("area_session")
        c_wk = candidate.get("area_week")
        p_time = parsed.get("time")
        p_sub = parsed.get("area_session")
        p_wk = parsed.get("area_week")

        # `p_sub > c_sub` (STRICT — Fable review 2 on #49): allowing
        # `>=` lets a repeat of the same poison packet confirm its own
        # predecessor, destroying the live run. Strictness costs
        # nothing on the observed data (any genuine mowing successor
        # advances `sub` in a single 30-90 s cadence at 2.0-2.7 m²/min);
        # a frozen-transit corner heals in transit duration + 1 packet.
        coherent = (
            c_time is not None
            and c_sub is not None
            and c_wk is not None
            and p_time is not None
            and p_sub is not None
            and p_wk is not None
            and p_time > c_time
            and p_sub > c_sub
            and abs((p_wk - p_sub) - (c_wk - c_sub)) <= INVARIANT_TOLERANCE_M2
        )

        self._pending_reset = None

        if not coherent:
            _LOGGER.debug(
                "run_tracker: pending reset discarded (candidate sub=%s wk=%s "
                "time=%s vs incoming sub=%s wk=%s time=%s)",
                c_sub,
                c_wk,
                c_time,
                p_sub,
                p_wk,
                p_time,
            )
            return events

        # Confirmed — close the open run (if any) at its own
        # accumulator's last `time` (label derived from that run's
        # `last_mp` in `_close_run`), then open a new run at the
        # candidate. The current packet then flows through the normal
        # continuation path against the new run.
        if self.state in (STATE_RUNNING, STATE_PAUSED_DOCKED):
            events.append(self._close_run())
        self._open_run(candidate)
        events.append(self._event_run_started())
        # Stamp cursors from the candidate too, so `_observe_wk_regression`
        # against the current packet sees the candidate's `wk` (which
        # was smaller) as the anchor.
        if candidate.get("area_week") is not None:
            self._last_accepted_wk = candidate["area_week"]
        if candidate.get("time") is not None:
            self._last_accepted_time_ms = candidate["time"]
        return events

    # ------------------------------------------------------------- #
    # Run lifecycle                                                 #
    # ------------------------------------------------------------- #

    def _open_run(self, parsed: dict[str, Any]) -> None:
        wk = parsed.get("area_week")
        sub = parsed.get("area_session")
        wk0 = (wk - sub) if (wk is not None and sub is not None) else None
        # FEAT-06: `sub₀` is the session-scoped anchor — the incoming
        # packet's `sub` at the moment the run opens. Per-session area
        # is later `last_sub − sub₀`. For a genuine fresh mow it is
        # near 0; for a session opened post-close on a continuing
        # firmware task it is the accumulator's value at RUN press.
        self.current_run = {
            "start_time": parsed.get("time"),
            "mow_start_type": parsed.get("mow_start_type"),
            "wk0": wk0,
            "sub0": sub,
            "last_time": parsed.get("time"),
            "last_sub": sub,
            "last_wk": wk,
            "last_mp": parsed.get("mowing_percentage"),
            "zones": [],
        }
        self.state = STATE_RUNNING
        self._interrupt_timer_started_at = None

    def _close_run(self) -> Event:
        """Close the currently open run. The result label is derived
        from the last accepted `mowing_percentage`: `completed` iff
        `last_mp >= MP_COMPLETION_THRESHOLD`, else `interrupted`.

        Centralising the decision here (BUG-09) means every close path
        — the fast BUG-09 completion criterion, a fresh reset, the
        sustained-60 s interruption timer, a resolved pending reset —
        labels the same way. The sustained-timer path on 2026-07-04
        used to hardcode `interrupted` and thus mis-labeled a genuinely
        completed run whose close it caught after the battery finished
        charging.
        """
        assert self.current_run is not None, "close_run without an open run"
        r = self.current_run
        start = r.get("start_time")
        end = r.get("last_time")
        duration_ms: int | None = None
        if start is not None and end is not None:
            duration_ms = end - start
        last_mp = r.get("last_mp")
        result = (
            RESULT_COMPLETED
            if last_mp is not None and last_mp >= MP_COMPLETION_THRESHOLD
            else RESULT_INTERRUPTED
        )
        # FEAT-06 (#54): per-session area = last_sub − sub₀. Reflects
        # what the *session* mowed; the firmware's `subtotalArea`
        # continues across tasks, so raw `last_sub` on its own would
        # over-count when a session resumes a still-running firmware
        # task. `None` when either endpoint is missing (e.g. an
        # old-snapshot run restored without `sub₀` — see `restore`).
        last_sub = r.get("last_sub")
        sub0 = r.get("sub0")
        session_area: float | None = None
        if last_sub is not None and sub0 is not None:
            session_area = last_sub - sub0
        self.state = (
            STATE_COMPLETED if result == RESULT_COMPLETED else STATE_INTERRUPTED
        )
        self._interrupt_timer_started_at = None
        return Event(
            kind=EVENT_RUN_FINISHED,
            payload={
                "result": result,
                "start_time": start,
                "end_time": end,
                "duration_ms": duration_ms,
                "session_area": session_area,
                "mow_start_type": r.get("mow_start_type"),
                "zones": [dict(z) for z in r.get("zones", [])],
            },
        )

    def _maybe_complete_run(self) -> Event | None:
        """BUG-09 completion criterion: `last_mp ≥ MP_COMPLETION_THRESHOLD`
        (99) ∧ `vehicle_state ∈ DOCKED_NOT_USER_PAUSED` (`{1, 2, 3}`).
        Immediate close with no debounce. Returns the close event, or
        `None` when the criterion is not met.

        Called from `process_type2` (after accumulator update, so the
        just-accepted packet's `mp` is visible) and `process_vehicle_state`
        (after the vs update, so a dock arrival while `last_mp` was
        already ≥ threshold fires the close even before the next type-2).
        Either ordering — mp-crosses-threshold-then-dock, or
        dock-arrives-then-mp-refresh — is handled.
        """
        if self.state not in (STATE_RUNNING, STATE_PAUSED_DOCKED):
            return None
        if self.current_run is None:
            return None
        last_mp = self.current_run.get("last_mp")
        if last_mp is None or last_mp < MP_COMPLETION_THRESHOLD:
            return None
        if self.vehicle_state not in DOCKED_NOT_USER_PAUSED:
            return None
        return self._close_run()

    def _event_run_started(self) -> Event:
        assert self.current_run is not None
        r = self.current_run
        return Event(
            kind=EVENT_RUN_STARTED,
            payload={
                "start_time": r.get("start_time"),
                "mow_start_type": r.get("mow_start_type"),
            },
        )

    # ------------------------------------------------------------- #
    # Accumulator / zone bookkeeping                                #
    # ------------------------------------------------------------- #

    def _update_accumulators(self, parsed: dict[str, Any]) -> None:
        r = self.current_run
        if r is None:
            return
        if parsed.get("time") is not None:
            r["last_time"] = parsed["time"]
        if parsed.get("area_session") is not None:
            r["last_sub"] = parsed["area_session"]
        if parsed.get("area_week") is not None:
            r["last_wk"] = parsed["area_week"]
        if parsed.get("mowing_percentage") is not None:
            r["last_mp"] = parsed["mowing_percentage"]

    def _update_zone(self, parsed: dict[str, Any]) -> None:
        """Extend the current zone or open a new one on boundary change.

        `boundary=0` (BUG-06 session-init sentinel) is excluded from
        zone accounting — the packet still updates run accumulators,
        just not the zone list.
        """
        if self.current_run is None:
            return
        b = parsed.get("boundary")
        sub = parsed.get("area_session")
        cmp_ = parsed.get("current_mow_progress")
        t = parsed.get("time")
        if b is None or b == 0:
            return
        zones = self.current_run["zones"]
        if zones and zones[-1]["boundary_id"] == b:
            z = zones[-1]
            if t is not None:
                z["last_time"] = t
            if cmp_ is not None:
                z["cmp_max"] = max(z.get("cmp_max") or 0, cmp_)
            if sub is not None:
                z["sub_exit"] = sub
        else:
            # New zone — the outgoing zone's `sub_exit` was updated on
            # the previous accepted packet, so no explicit closure step
            # is needed here.
            zones.append(
                {
                    "boundary_id": b,
                    "first_time": t,
                    "last_time": t,
                    "cmp_max": cmp_ if cmp_ is not None else 0,
                    "sub_entry": sub,
                    "sub_exit": sub,
                }
            )

    # ------------------------------------------------------------- #
    # Sustained-interrupt bookkeeping                               #
    # ------------------------------------------------------------- #

    def _start_interrupt_timer_if_applicable(self, vs: int) -> None:
        if vs in DOCKED_NOT_CHARGING:
            if self._interrupt_timer_started_at is None:
                self._interrupt_timer_started_at = self._clock()
        else:
            # vs=2 (charging) or vs=6 (paused) explicitly holds the run
            # without a countdown.
            self._interrupt_timer_started_at = None

    # ------------------------------------------------------------- #
    # Persistence                                                   #
    # ------------------------------------------------------------- #

    def snapshot(self) -> dict[str, Any]:
        """Serialize enough state for `restore()` to resume a mid-run
        tracker after an HA restart. Consumed by step (c) `Store`.

        `current_run` is `deepcopy`'d so the returned dict is a true
        point-in-time capture. Step (c) schedules the `Store.async_save`
        fire-and-forget and serialises the payload in an executor; a
        shared reference would let a packet processed between
        scheduling and the dump mutate the live `current_run` cross-
        thread mid-serialisation.
        """
        return {
            "version": SNAPSHOT_VERSION,
            "state": self.state,
            "vehicle_state": self.vehicle_state,
            "current_run": copy.deepcopy(self.current_run),
            "last_accepted_wk": self._last_accepted_wk,
            "last_accepted_time_ms": self._last_accepted_time_ms,
            "drops": dict(self.drops),
            "counters": dict(self.counters),
        }

    def restore(self, snap: dict[str, Any]) -> bool:
        """Load a previously-taken snapshot. Returns True on acceptance,
        False when the version doesn't match (caller decides whether to
        drop the payload or upgrade it).

        A pre-FEAT-06 snapshot may carry an open run without ``sub₀``;
        that is left as-is so the next ``_close_run`` reports
        ``session_area = None`` rather than fabricating a value from
        the firmware task-scoped accumulator (the FEAT-06 bug).
        """
        if snap.get("version") != SNAPSHOT_VERSION:
            return False
        self.state = snap.get("state", STATE_IDLE)
        self.vehicle_state = snap.get("vehicle_state")
        self.current_run = snap.get("current_run")
        self._last_accepted_wk = snap.get("last_accepted_wk")
        self._last_accepted_time_ms = snap.get("last_accepted_time_ms")
        drops = snap.get("drops") or {}
        counters = snap.get("counters") or {}
        # HARD-14: pre-BUG-10 / pre-HARD-06 migrations retired. Snapshots
        # written on those builds have long since been re-persisted in
        # the current shape; the two observability counters simply
        # default to 0 when absent (cosmetic reset at worst).
        self.counters = {
            "wk_regressions_observed": counters.get("wk_regressions_observed", 0),
            "invariant_deviations_observed": counters.get(
                "invariant_deviations_observed", 0
            ),
        }
        self.drops = {
            "pending_reset_holds": drops.get("pending_reset_holds", 0),
        }
        self._wk_regression_streak = 0
        self._invariant_deviation_streak = 0
        # `_interrupt_timer_started_at` is monotonic and cannot be
        # restored across a process restart. `tick()` re-arms it on the
        # first call after restart if the machine is `PAUSED_DOCKED`
        # under `vs ∈ {1, 3}` — so the sustained-docked interruption
        # detector survives a restart even if `vehicle_state` is
        # restored from the snapshot rather than re-derived. A pending
        # reset is intentionally *not* persisted: worst case a mid-flight
        # candidate re-confirms one packet later after a restart, which
        # is safer than serialising a transient decision.
        self._interrupt_timer_started_at = None
        self._pending_reset = None
        return True
