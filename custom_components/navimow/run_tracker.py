"""Turn the mower's `/location` packet stream into a timeline of mowing runs.

A *run* is one user session: the operator presses run, the robot mows, and the
session ends when it returns to the dock for good. The firmware itself has no
notion of a session — its unit is the *task*, whose counters outlive any single
outing — so the session timeline is reconstructed here, from two packet
families plus a clock.

The tracker is a three-state machine (IDLE, RUNNING, PAUSED_DOCKED) with three
entry points, all fed by the coordinator:

- `process_type2(parsed)` — a type-2 packet: mowing progress, areas, active
  boundary. This is where a run is seeded, extended, split and completed. Only
  items accepted by the coordinator's `time`-monotonicity guard arrive here.
- `process_vehicle_state(vs)` — a type-1 packet whose `vehicleState` changed:
  where the robot *is*. It opens a run on the activation edge, moves a live run
  in and out of the dock, stamps the arrival that ends it, and can close the
  run itself when the completion rule was already met.
- `tick(now=None)` — a ~30 s heartbeat, so a run left docked still closes when
  no further packet arrives.

Each call returns a list of `Event` records — `run_started`, `run_finished` —
for the caller to dispatch to Home Assistant. Nothing here imports HA, which
keeps the machine unit-testable against recorded packet corpora.

Two design choices shape everything below. Three states leave no resting state
to forget: a close is a transition back to IDLE, and the completed/interrupted
distinction lives only in the close record (`run_finished` payload,
`history[]`), never in the state. And IDLE keeps an OPTIONAL reference to the
last closed run — that reference is what the post-close gating reads to tell a
genuinely new session from an echo of the one that just ended.

Transitions:

    IDLE ─vs=4─▶ RUNNING (provisional) [run_started]
        start_time = the type-1 time, ~1.5 s after the press; anchors stay
        None until a type-2 seeds them.
    RUNNING (provisional) ─first accepted type-2─▶ RUNNING (seeded)
        sub₀ / mow_start_type / wk₀ / zone from that packet; start_time keeps
        the activation anchor; no second run_started.
    RUNNING (provisional) ─dock evidence sustained 60 s─▶
        IDLE [run_finished: interrupted]
        Aborted start: session_area None, zones [], real wander duration.
    IDLE (no reference, or reset sub < ceiling) ─fresh type-2─▶
        RUNNING [run_started]
    IDLE (seeded reference) ─fresh type-2 with strict progress─▶
        NEW run [run_started]   (an echo is refused and counted)
    RUNNING ─vs ∈ DOCK_EVIDENCE─▶ PAUSED_DOCKED
    RUNNING/PAUSED_DOCKED ─vs ∈ {VS_STOPPED, VS_MAPPING}─▶ (inert)
    PAUSED_DOCKED ─fresh type-2 while vs ∈ DEPARTURE_EVIDENCE─▶ RUNNING
        (resume, same run — an intra-run recharge dock does not split it)
    RUNNING/PAUSED_DOCKED ─completion rule ∧ vs ∈ DOCK_EVIDENCE─▶
        IDLE [run_finished: completed]
    RUNNING/PAUSED_DOCKED ─fresh reset (sub < last, sub < ceiling)─▶
        close the open run, then open a new one
    PAUSED_DOCKED ─vs = 1 sustained 60 s─▶ IDLE [run_finished: interrupted]

What blocks a packet, and what only observes it:

- Blocking, upstream in the coordinator: `/location` `time` monotonicity per
  stream. Not visible here — the tracker trusts its input.
- Blocking, here: the strict-progress echo filter (a post-close packet must
  advance `sub`, or `mp` when `sub` is absent), and pending-reset deferral (a
  `sub` regression above `RESET_SUB_CEILING` waits for a coherent successor).
  The deferral compares packet to packet, never packet to stored anchor, so a
  single anomalous packet cannot destroy a live run.
- Observability only, never blocking: `wk` regressions against the last
  accepted cursor, and the `|wk − sub − wk₀|` deviation against the open run's
  `wk₀`. Both are counted, DEBUG-logged, and escalate to one WARN after a
  streak. `wk₀` is anchored once per run and never re-anchored, so a firmware
  `wk` reset stays visible instead of being absorbed.

Firmware facts the machine depends on, none of them derivable from the code:

- `sub` (`subtotalArea`) accumulates across tasks, while `mp` re-bases when a
  task is redefined — a session can legitimately open at `mp = 65`. Run
  identity and per-session area therefore key on `sub`, never on `mp`.
- The weekly counter `wk` can reset mid-run. Nothing here encodes which day
  the firmware's week starts on.
- `vs = 8` is a firmware-reset transient and is ignored.
- `boundary = 0` marks a session-init sentinel: excluded from zone
  accounting, but it still updates run accumulators.
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

# Tracker states (internal, distinct from the display `run_state` the sensor
# layer exposes). Three states leave no resting state to forget: a close is a
# transition to IDLE, and the completed/interrupted label lives in the close
# record. The RESULT_* labels below are that record's vocabulary.
STATE_IDLE = "idle"
STATE_RUNNING = "running"
STATE_PAUSED_DOCKED = "paused_docked"

# vehicleState values (`docs/diag/2026-07-07_map-01_vs-empirical/`).
VS_DOCKED_IDLE = 1
VS_DOCKED_CHARGING = 2
# vs = 3 is a generic stopped state — not mowing, not returning, not charging,
# not mapping — and it is NOT a dock state: it is emitted both off-dock (a user
# pause mid-mow) and at-dock (an idle flip between charging samples, or an
# unpowered base). Because it spans both indistinguishably on the type-1
# channel, it is treated as evidence of nothing. Observed sub-cases:
# `docs/diag/2026-07-07_map-01_vs-empirical/`.
VS_STOPPED = 3
VS_MOWING = 4
VS_RETURNING = 5
# vs = 6 is the firmware's map-consolidation phase, usually post-mow at the
# dock — but mapping is a driving activity and a user-initiated remap runs
# off-dock, so vs = 6 is location-agnostic and inert for dock semantics too.
VS_MAPPING = 6
VS_TRANSIENT = 8  # firmware-reset transient (posture all-zero)

# Evidence-role sets. These are the ONLY two sets the machine may branch on
# for dock semantics, and they are named for their evidentiary role rather
# than their membership: a membership name rots silently when the firmware
# taxonomy moves.
#
# Dock evidence must be physically dock-exclusive — a state the robot can only
# be in while on the base — so an edge into one is a true arrival.
DOCK_EVIDENCE = frozenset({VS_DOCKED_IDLE, VS_DOCKED_CHARGING})
# Departure evidence: physically off the dock and moving. The only signal that
# clears an intermediate dock stamp and re-opens a provisional abort window.
DEPARTURE_EVIDENCE = frozenset({VS_MOWING, VS_RETURNING})
# vs = 3 and vs = 6 belong to NEITHER set: both are location-agnostic, so both
# are inert for dock semantics — they never stamp, clear, arm, disarm, resume
# or close. The completion predicate is exactly DOCK_EVIDENCE, and the
# sustained timer arms on VS_DOCKED_IDLE alone.

# Every steady VS_* constant must belong to exactly one evidentiary group;
# only the out-of-band VS_TRANSIENT is excluded. A test derives the VS_*
# constants by introspection and asserts equality against this set, so a new
# firmware state fails a test instead of falling silently through the machine.
KNOWN_VEHICLE_STATES = (
    DOCK_EVIDENCE | DEPARTURE_EVIDENCE | frozenset({VS_STOPPED, VS_MAPPING})
)

# A firmware plateau at `mp = 99` is indistinguishable from a recharge return
# on `mp` alone: a real day (mow to 99, dock to recharge, resume, finish) was
# closed prematurely at the recharge dock and split into two sessions. Both
# plateaus, 99 and 100, occur in the wild.
MP_COMPLETION_THRESHOLD = 100

# A task whose `mp` plateaus at 99 without ever reaching 100 can still be told
# apart from a recharge return by the zone-scoped `cmp`: 10000 means the
# firmware confirms the active zone is fully mowed. Below these values the run
# keeps holding in PAUSED_DOCKED as a recharge candidate.
MP_PARTIAL_THRESHOLD = 99
CMP_ZONE_COMPLETE_THRESHOLD = 10000
# Residual false positive: on a multizone task where the robot finishes zone A
# (cmp = 10000) and docks to recharge before starting zone B, this closes as
# `completed`. Never observed in the corpus. `session_area` and `zones[]` stay
# correct under either label.

# Protocol facts about the firmware's late task-end replay packet, kept
# separate from the completion *policy* above.
#
# `MP_TASK_END` is the wire value the firmware stamps on the vestige. It
# coincides numerically with `MP_COMPLETION_THRESHOLD` today, but the
# completion threshold is a tunable tracker policy and the wire value is not:
# if the policy moves, the vestige guard must not follow.
#
# `RUN_START_SUB_TOLERANCE` sits between the vestige's zeroed `sub` and the
# ~2.4 m² a legitimate first packet already carries after one type-2 cadence,
# with headroom for a firmware variant emitting a residual float near zero.
MP_TASK_END = 100
RUN_START_SUB_TOLERANCE = 0.5

# Seconds a PAUSED_DOCKED run must stay docked-idle (vs = 1) before it is
# declared interrupted. 60 s ≈ 30 type-1 samples at the 2 s cadence: enough
# debounce for dock-contact transients, still timely for end-of-run reporting.
INTERRUPT_SUSTAIN_SECONDS = 60

# Tolerance on the `wk₀ + sub` invariant, in m².
INVARIANT_TOLERANCE_M2 = 0.5

# A `sub` regression below this ceiling is an immediate reset (a genuine run
# just started); above it, the packet is only a candidate that a coherent
# successor must confirm. 10.0 m² is roughly 4× the largest genuine run-start
# `sub` ever committed (0.39 and 2.6 m² observed).
RESET_SUB_CEILING = 10.0

# Streak thresholds for observability. After this many consecutive
# observations the tracker emits one WARNING, so an operator sees the anomaly
# in real time and not only through the counter. The streak resets on any
# non-observing packet, so routine transitions never reach it; ~2.5 min at the
# 30 s type-2 cadence keeps the WARN actionable.
WK_REGRESSION_STREAK_TO_WARN = 5

# A persistent streak against a live run's `wk₀` (never re-anchored) means the
# firmware reset `wk` mid-run. Accepted packets keep the run alive on `sub`,
# and the sustained timer still closes it via `vs`.
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
        # Injectable monotonic clock, in seconds. Only the sustained-dock
        # timer uses it; firmware times come from the packets and live on
        # another axis, so the two are never compared.
        self._clock: Callable[[], float] = clock or time.monotonic

        self.state: str = STATE_IDLE
        self.vehicle_state: int | None = None

        # The open run, or the most recently closed one; `None` at cold boot
        # only. Kept across a close: the post-close gating compares against
        # its `sub`.
        self.current_run: dict[str, Any] | None = None

        # Cursors read by the wk-regression observer.
        self._last_accepted_wk: float | None = None
        self._last_accepted_time_ms: int | None = None

        # Sustained-dock timer, monotonic seconds. `None` unless we are in
        # docked-idle (vs = 1) under an open run.
        self._interrupt_timer_started_at: float | None = None

        # Candidate reset packet: `sub` below `last_sub` but above
        # `RESET_SUB_CEILING`, so not obviously a genuine run start. Held in
        # memory only — a restart mid-flight costs one packet of
        # re-observation, which beats persisting a transient decision.
        self._pending_reset: dict[str, Any] | None = None

        # `drops` = packets the tracker refused; `counters` = things observed
        # but deliberately not acted on.
        self.drops: dict[str, int] = {
            "pending_reset_holds": 0,
        }
        self.counters: dict[str, int] = {
            "wk_regressions_observed": 0,
            "invariant_deviations_observed": 0,
            # Echo packets refused on the post-close path, and provisional
            # runs closed by the sustained-dock abort (pressed, wandered, sent
            # home without ever mowing).
            "strict_progress_rejections": 0,
            "aborted_starts_committed": 0,
        }
        # Real-time streaks behind the throttled WARNINGs, not ledgers: not
        # snapshotted, so a restart mid-anomaly re-arms the WARN from zero
        # while the persistent counters keep the history.
        self._wk_regression_streak: int = 0
        self._invariant_deviation_streak: int = 0

    # ------------------------------------------------------------- #
    # Public API                                                    #
    # ------------------------------------------------------------- #

    @property
    def is_provisional(self) -> bool:
        """True while the current run is provisional — opened on the vs = 4
        activation edge and not yet seeded by a type-2. Exposed as a property
        so the sensor platform never reaches into `current_run`.
        """
        return bool(
            self.current_run is not None and self.current_run.get("provisional")
        )

    def process_type2(self, parsed: dict[str, Any]) -> list[Event]:
        """Feed a type-2 payload (already through the layer-1 guard).

        `parsed` must be the dict returned by
        `location.parse_location_type_2`.
        """
        events: list[Event] = []

        # Drop the run-start vestige before any transition or write: from every
        # entry state it would otherwise anchor the new run on stale values and
        # poison `zones[0]`. Rationale in `_gate_run_start_vestige`.
        if self._gate_run_start_vestige(parsed):
            return events

        # Drop the all-zero session-init sentinel. The firmware emits a
        # zero-payload type-2 (`boundary = 0 ∧ mp = 0 ∧ cmp = 0 ∧ sub = 0`)
        # ~15 s after vs = 4, carrying nothing the activation edge has not
        # already given. Left in, it opens a phantom run from IDLE that no
        # dock edge can then close, or trips `is_reset` on a seeded run and
        # splits it.
        #
        # The signature is keyed on boundary/mp/sub, which separates it from
        # the `boundary = 0 ∧ mp = 100` task-*end* marker that must NOT drop.
        # `mp` and `sub` are compared explicitly to zero, with no `or 0`
        # coercion: the parser represents a missing field as `None`, and an
        # absent field must not read as a confirmed zero — a sparse or
        # malformed packet fails open onto the normal path.
        boundary = parsed.get("boundary")
        mp = parsed.get("mowing_percentage")
        sub = parsed.get("area_session")
        if boundary in (None, 0) and mp == 0 and sub == 0.0:
            _LOGGER.debug(
                "run_tracker: all-zero session-init sentinel dropped (BUG-15) "
                "(state=%s vs=%s boundary=%s mp=%s cmp=%s sub=%s wk=%s action=%s time=%s)",
                self.state,
                self.vehicle_state,
                boundary,
                mp,
                parsed.get("current_mow_progress"),
                sub,
                parsed.get("area_week"),
                parsed.get("action"),
                parsed.get("time"),
            )
            return events

        # A type-2 arriving while a provisional start is still docked is
        # ignored — neither resume nor seed. Seeding would resume to RUNNING
        # and clear the abort timer, leaving `RUNNING ∧ docked ∧ timer=None`;
        # `tick()` acts only in PAUSED_DOCKED, and the type-1/type-2 cadence
        # skew means no further vs edge is guaranteed, so the run would render
        # `running` indefinitely while docked. Worse, a near-close replay would
        # then let the abort mint a phantom *completed* session. A real
        # dock-poke recovers on the off-dock type-1, and the next type-2 seeds
        # normally.
        if (
            self.is_provisional
            and self.state == STATE_PAUSED_DOCKED
            and self.vehicle_state in DOCK_EVIDENCE
        ):
            _LOGGER.debug(
                "run_tracker: type-2 ignored while provisional start "
                "remains docked "
                "(mp=%s cmp=%s sub=%s wk=%s action=%s boundary=%s time=%s)",
                parsed.get("mowing_percentage"),
                parsed.get("current_mow_progress"),
                parsed.get("area_session"),
                parsed.get("area_week"),
                parsed.get("action"),
                parsed.get("boundary"),
                parsed.get("time"),
            )
            return events

        # A `wk` regression is observed and counted; the packet proceeds.
        self._observe_wk_regression(parsed)

        # Resolve any stashed pending reset first: this packet may confirm or
        # discard it before its own reset semantics are read.
        events.extend(self._resolve_pending_reset(parsed))

        prev_sub = self.current_run["last_sub"] if self.current_run else None
        incoming_sub = parsed.get("area_session")
        incoming_mp = parsed.get("mowing_percentage")

        is_reset = (
            prev_sub is not None
            and incoming_sub is not None
            and incoming_sub < prev_sub
        )

        # Open, close, open a new session, or continue. The `|wk − sub − wk₀|`
        # invariant is observed on continuations and post-close opens, never
        # enforced.
        if self.state == STATE_IDLE:
            # IDLE carries an OPTIONAL last-closed reference, and its contents
            # key the gate: no reference (first boot) → ungated open; seeded
            # reference → post-close gating; empty reference (post-abort, both
            # axes None) → conservative refusal, self-resolving at the next
            # vs = 4.
            if is_reset:
                # Below the ceiling this is a genuine run start; above it, hold
                # it as a candidate that a coherent successor must confirm.
                # Nothing is open to close here.
                if incoming_sub is not None and incoming_sub < RESET_SUB_CEILING:
                    self._open_run(parsed)
                    events.append(self._event_run_started())
                else:
                    self._stash_pending_reset(parsed)
                    return events
            else:
                # A fresh type-2 with strict progress opens a NEW session, not
                # a reopen; an echo (same sub/mp, only `time` fresher) is
                # refused, or a stream tail spawns a phantom session after
                # every close. The `current_run is not None` guard keeps the
                # first-boot path ungated. The refusal is counted and logged so
                # an audit can see whether it ever fires in production.
                if self.current_run is not None and not self._has_strict_progress(
                    parsed
                ):
                    self.counters["strict_progress_rejections"] += 1
                    _LOGGER.debug(
                        "run_tracker: type-2 rejected by strict progress "
                        "(sub=%s last_sub=%s mp=%s last_mp=%s time=%s)",
                        incoming_sub,
                        self.current_run.get("last_sub") if self.current_run else None,
                        incoming_mp,
                        self.current_run.get("last_mp") if self.current_run else None,
                        parsed.get("time"),
                    )
                    return events
                # Observe against the closed run's `wk₀` before `_open_run`
                # re-anchors; a persistent drift here is structurally
                # impossible.
                self._observe_invariant_deviation(parsed)
                self._open_run(parsed)
                events.append(self._event_run_started())
        elif self.state in (STATE_RUNNING, STATE_PAUSED_DOCKED):
            if is_reset:
                # Below the ceiling this is a genuine run start; above it, hold
                # it as a candidate that a coherent successor must confirm —
                # one anomalous packet must not destroy a live run.
                if incoming_sub is not None and incoming_sub < RESET_SUB_CEILING:
                    events.append(self._close_run())
                    self._open_run(parsed)
                    events.append(self._event_run_started())
                else:
                    self._stash_pending_reset(parsed)
                    return events
            else:
                # Continuation: observe the invariant, then accept.
                self._observe_invariant_deviation(parsed)
                # Resume, and drop the dock stamp, ONLY on departure evidence:
                # the robot physically left, so the dock it left was a mid-run
                # recharge and not the session's final one. A type-2 arriving
                # while still docked (a late completing flush, or a stream
                # skew) still updates accumulators and may complete the run,
                # but never resumes and never clears the stamp — the run then
                # ends at the frozen dock arrival. `current_run` is non-None
                # here, since PAUSED_DOCKED implies an open run.
                if (
                    self.state == STATE_PAUSED_DOCKED
                    and self.vehicle_state in DEPARTURE_EVIDENCE
                ):
                    self.state = STATE_RUNNING
                    self._interrupt_timer_started_at = None
                    self.current_run["dock_arrival_time"] = None

        # Seed a provisional run from its first accepted type-2 — the first
        # packet carrying honest task data. `start_time` is deliberately not
        # touched: the run starts when the operator pressed run. Flipping
        # `provisional` off is what makes this block one-shot. The DEBUG line
        # collects the shape of every start-window first packet.
        if self.current_run is not None and self.current_run.get("provisional"):
            r = self.current_run
            r["sub0"] = parsed.get("area_session")
            r["mow_start_type"] = parsed.get("mow_start_type")
            r["provisional"] = False
            _LOGGER.debug(
                "run_tracker: start-window first type-2 "
                "(mp=%s cmp=%s sub=%s wk=%s action=%s boundary=%s time=%s)",
                parsed.get("mowing_percentage"),
                parsed.get("current_mow_progress"),
                parsed.get("area_session"),
                parsed.get("area_week"),
                parsed.get("action"),
                parsed.get("boundary"),
                parsed.get("time"),
            )

        # Bookkeeping on acceptance. `_update_wk0_anchor` owns the "first
        # packet with data" case, which keeps the deviation observer a pure
        # read.
        self._update_wk0_anchor(parsed)
        self._update_accumulators(parsed)
        self._update_zone(parsed)

        # Acceptance advances the cursors read by the wk-regression observer.
        if parsed.get("area_week") is not None:
            self._last_accepted_wk = parsed["area_week"]
        if parsed.get("time") is not None:
            self._last_accepted_time_ms = parsed["time"]

        # Fires when a fresh type-2 pushes `last_mp` over the threshold while
        # the robot is already docked; the dock-then-threshold ordering is
        # handled in `process_vehicle_state`.
        completion = self._maybe_complete_run()
        if completion is not None:
            events.append(completion)

        return events

    def process_vehicle_state(
        self, vs: int, *, time_ms: int | None = None
    ) -> list[Event]:
        """React to a `vehicleState` change (type-1 packet).

        On the `IDLE → vs = 4` edge a **provisional** run opens immediately, so
        the state (and the state sensor) reflects the press ~1.5 s later
        instead of waiting the ~3 min until the firmware's first mowing-task
        type-2. `time_ms` is that type-1's `time` and becomes the run's
        `start_time`; the keyword-only default keeps existing callers valid.

        Otherwise, entries into `DOCK_EVIDENCE` from `RUNNING` move a live run
        into `PAUSED_DOCKED`. Resume of a *seeded* run is driven by a fresh
        type-2 in `process_type2`, not by the vs edge itself: a type-1 briefly
        showing vs = 4 during a dock-poke must not resume a real run. A
        provisional run has no mowing data to hold for, so any sustained dock
        aborts it. `VS_STOPPED` and `VS_MAPPING` are inert and return early.
        """
        events: list[Event] = []

        if vs == VS_TRANSIENT:
            return events

        self.vehicle_state = vs

        # `VS_STOPPED` and `VS_MAPPING` are evidence of nothing: both are
        # location-agnostic, so neither stamps a dock arrival, clears one, arms
        # or disarms the timer, resumes a paused run, or closes. An open run
        # rides through with its timer context intact, so a transient
        # `2 → 3 → 2` dock flip and its `1 → 6 → 1` analogue pass straight
        # through. `vehicle_state` is updated above so the display ladder can
        # still render them; completion cannot fire, its predicate being
        # `DOCK_EVIDENCE`.
        if vs in (VS_STOPPED, VS_MAPPING):
            return events

        # Eager session start. Once `state == RUNNING` this test is
        # structurally false, so a repeated vs = 4 or a 4→5→4 wobble cannot
        # re-open — dedupe for free, no flag.
        if vs == VS_MOWING and self.state == STATE_IDLE:
            self._open_provisional_run(time_ms)
            events.append(self._event_run_started())
            return events

        provisional = self.is_provisional

        if self.state == STATE_RUNNING:
            if vs in DOCK_EVIDENCE:
                # Stamp the dock-arrival edge FIRST, before the arm/complete
                # logic below, so a completion close fired later in this same
                # call reads a stamp that already exists. Race-free by
                # construction: the type-1 that closes IS the type-1 that
                # stamps, and the stamp comes only from this single
                # `/location` stream — the mower entity's docked activity is
                # derived from the separate `/state` stream, which has no write
                # path into the tracker. Frozen through the docked idle↔charge
                # flips (they re-enter via the branch below, which does not
                # stamp) and cleared only on departure evidence. Gated on
                # `time_ms` so a caller without it falls back to the packet
                # cursor in `_close_run`.
                if time_ms is not None:
                    self.current_run["dock_arrival_time"] = time_ms
                # A provisional run has no mowing data to hold for, so any
                # dock entry starts the close countdown. The wander end is
                # stamped on this edge and then frozen: a charge↔idle flip an
                # hour later must not inflate the duration.
                if provisional:
                    if time_ms is not None:
                        self.current_run["last_time"] = time_ms
                    self.state = STATE_PAUSED_DOCKED
                    self._arm_interrupt_timer()
                else:
                    self.state = STATE_PAUSED_DOCKED
                    self._start_interrupt_timer_if_applicable(vs)
            elif provisional and time_ms is not None:
                # Off-dock and still provisional: keep the wander duration live
                # so an eventual abort reports real time.
                self.current_run["last_time"] = time_ms
        elif self.state == STATE_PAUSED_DOCKED:
            if provisional:
                if vs in DOCK_EVIDENCE:
                    # Still docked — keep the countdown armed regardless of
                    # charging, and do NOT refresh `last_time` (frozen at the
                    # dock-entry edge above).
                    self._arm_interrupt_timer()
                else:
                    # Departure evidence: left the dock again before the
                    # debounce fired, so the provisional window re-opens
                    # off-dock. A real type-2 (resume + seed) or a sustained
                    # re-dock (abort) resolves it.
                    self.state = STATE_RUNNING
                    self._interrupt_timer_started_at = None
                    if time_ms is not None:
                        self.current_run["last_time"] = time_ms
                    # That dock was intermediate — drop its arrival stamp.
                    self.current_run["dock_arrival_time"] = None
            else:
                # Charging or a pause resets the timer; docked-idle arms it.
                self._start_interrupt_timer_if_applicable(vs)

        # The run may have crossed the mp threshold before arriving at the
        # dock, and no further type-2 is guaranteed after arrival — so the
        # close fires here as soon as vs enters `DOCK_EVIDENCE`. A provisional
        # run cannot complete (`last_mp is None`).
        completion = self._maybe_complete_run()
        if completion is not None:
            events.append(completion)

        return events

    def tick(self, now: float | None = None) -> list[Event]:
        """Advance the sustained-dock interruption timer.

        Called periodically (~30 s), with two roles. It arms the timer when we
        are `PAUSED_DOCKED` under docked-idle and it is not yet running, so the
        detector survives an HA restart without needing a fresh `vehicleState`
        edge — a `restore()` followed by a tick suffices. And it fires
        `run_finished` once the timer has held for `INTERRUPT_SUSTAIN_SECONDS`
        with nothing resuming the run. The label comes from `last_mp` in
        `_close_run`, so a run that had already completed is not mislabelled
        `interrupted`.
        """
        events: list[Event] = []
        now = self._clock() if now is None else now

        if self.state == STATE_PAUSED_DOCKED:
            # A provisional run (aborted start) fires on ANY sustained dock,
            # charging included, having no mowing data to hold for. A seeded
            # run arms on vs = 1 only, so a mid-run recharge never times out.
            docked = (
                self.vehicle_state in DOCK_EVIDENCE
                if self.is_provisional
                else self.vehicle_state == VS_DOCKED_IDLE
            )
            if docked:
                if self._interrupt_timer_started_at is None:
                    self._interrupt_timer_started_at = now
                elif (
                    now - self._interrupt_timer_started_at
                ) >= INTERRUPT_SUSTAIN_SECONDS:
                    events.append(self._close_run())

        return events

    # ------------------------------------------------------------- #
    # Guards + observability                                        #
    # ------------------------------------------------------------- #

    def _gate_run_start_vestige(self, parsed: dict[str, Any]) -> bool:
        """Drop the late task-end vestige packet; True when dropped.

        On task start the firmware sometimes replays the previous task's
        closing packet as the very first type-2 of the fresh mow. Accepted, it
        anchors the new run on stale values (`start_time`, `sub₀`,
        `mow_start_type`) and seeds `zones[0].cmp_max` at the ceiling, where it
        then sticks for the whole zone.

        Armed by default; dark in one state only — an open run whose first zone
        is already honestly seeded, which is where genuine completion packets
        live. Naming the single dark state rather than enumerating the armed
        ones is deliberate: an enumeration silently misses states, and the
        post-close rest is the operator's dominant entry state.

        Drop signature = `mp = MP_TASK_END ∧ cmp ≥
        CMP_ZONE_COMPLETE_THRESHOLD`. `sub` is deliberately not part of it: the
        vestige carries either a zeroed or a frozen `sub`, so it never carried
        the decision. Safety is categorical rather than empirical — a run
        cannot *open* at `cmp = 10000`, that being a finished-boundary state,
        and a genuine start shows `cmp` climbing from a low value, so the
        conjunction has no legitimate opening packet. `mp` may re-base high on
        a resumed task, which is why `cmp` carries the discrimination. A
        missing field fails the match, so an incomplete packet is never
        dropped.

        A gate, not a predicate: it also DEBUG-logs a near-zero `sub` inside
        the armed window without a full match, to collect evidence on the
        untested interrupted-vestige shape. That line has known false
        positives on genuine low-`sub` starts (0.39 m² observed), separable
        after the fact by the logged `mp` / `cmp`.
        """
        # The asset protected here is the *next* run's `zones[0]` seed and the
        # `_open_run` anchor, which is at risk in every state except an open run
        # whose first zone is already honestly seeded. Name that single dark
        # state; stay armed everywhere else. Post-close, the closed run is
        # still referenced with non-empty `zones`, which is exactly why the
        # dark predicate needs the state test AND seeded zones together.
        mowing_with_zone = (
            self.state in (STATE_RUNNING, STATE_PAUSED_DOCKED)
            and self.current_run is not None
            and bool(self.current_run.get("zones"))
        )
        armed = not mowing_with_zone
        if not armed:
            return False

        mp = parsed.get("mowing_percentage")
        cmp_ = parsed.get("current_mow_progress")
        sub = parsed.get("area_session")

        if mp == MP_TASK_END and (cmp_ or 0) >= CMP_ZONE_COMPLETE_THRESHOLD:
            _LOGGER.debug(
                "run_tracker: type-2 rejected — run-start vestige "
                "(mp=%s cmp=%s sub=%s wk=%s action=%s boundary=%s time=%s)",
                mp,
                cmp_,
                sub,
                parsed.get("area_week"),
                parsed.get("action"),
                parsed.get("boundary"),
                parsed.get("time"),
            )
            return True

        if sub is not None and sub < RUN_START_SUB_TOLERANCE:
            _LOGGER.debug(
                "run_tracker: run-start suspicious shape, not dropped "
                "(mp=%s cmp=%s sub=%s wk=%s action=%s boundary=%s time=%s)",
                mp,
                cmp_,
                sub,
                parsed.get("area_week"),
                parsed.get("action"),
                parsed.get("boundary"),
                parsed.get("time"),
            )
        return False

    def _observe_wk_regression(self, parsed: dict[str, Any]) -> None:
        """Count and log `wk` regressions without dropping the packet.

        A firmware `wk` reset makes every incoming packet regress against the
        cursor, so blocking on this rejects a whole day. Anything that looks
        like a reset goes through the reset / pending-reset machinery instead,
        and no calendar assumption is encoded here.

        Also drives the streak counter behind the one-shot WARNING. The streak
        resets on any non-regressing packet, so a routine fresh-session start
        never trips it.
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
        """Observe `|wk − sub − wk₀|` without blocking the packet.

        Mirrors `_observe_wk_regression`: short-circuit on a missing `wk`,
        `sub` or `wk₀` without touching the streak; beyond
        `INVARIANT_TOLERANCE_M2`, count, DEBUG-log, bump the streak and WARN
        exactly once on reaching the threshold; within tolerance, reset the
        streak.

        The WARN means "persistent deviation against a LIVE anchor". On the
        post-close path the observation fires exactly once, against the closed
        run's `wk₀` before `_open_run` re-anchors, so a streak there is
        structurally impossible. On a mid-run `wk` reset the streak climbs
        against the open run's never-re-anchored anchor while the sustained
        timer still closes the run cleanly via `vs`; `session_area` is
        `sub`-only and unaffected.
        """
        if self.current_run is None:
            return
        wk = parsed.get("area_week")
        sub = parsed.get("area_session")
        if wk is None or sub is None:
            return
        wk0 = self.current_run.get("wk0")
        if wk0 is None:
            # No anchor yet: the acceptance path sets it via
            # `_update_wk0_anchor`.
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
        """Initialise `wk₀` on the first packet carrying both `wk` and `sub`,
        for the case where `_open_run` was fed a packet missing one of them.
        Once set it is stable for the life of the run — a mid-run firmware `wk`
        reset is never re-anchored, so the deviation observer can see it.
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
        """True when the packet strictly advances on the closed run's last
        accepted values. Gates the new-session transition against echoes: an
        identical repeat of the closing packet with only `time` fresher would
        otherwise spawn a phantom session after every close.
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

        Called at the top of every `process_type2` acceptance. Returns the
        events emitted if the candidate is confirmed (close the open run, open
        a new one), or an empty list if it is discarded or nothing is pending.

        The coherence check below compares the candidate to its successor
        (packet-vs-packet), never a packet to a stored run anchor. Its failure
        mode is bounded to discarding the candidate — the next packet becomes
        one — which is what makes it safe to block on.
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

        # `p_sub > c_sub` is strict: with `>=`, a repeat of the same anomalous
        # packet would confirm its own predecessor and destroy the live run.
        # Strictness costs nothing — a genuine successor advances `sub` within
        # one 30–90 s cadence — and a frozen-transit corner heals one packet
        # later.
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

        # Confirmed: close the open run at its own last `time`, then open a new
        # one at the candidate. The current packet then flows through the normal
        # continuation path against the new run.
        if self.state in (STATE_RUNNING, STATE_PAUSED_DOCKED):
            events.append(self._close_run())
        self._open_run(candidate)
        events.append(self._event_run_started())
        # Stamp the cursors from the candidate too, so the wk observer judges
        # the current packet against the candidate's (smaller) `wk`.
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
        # `sub₀` is the session-scoped anchor; per-session area is later
        # `last_sub − sub₀`. Near 0 for a genuine fresh mow; the accumulator's
        # value at the press for a session opened on a continuing firmware task.
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
            # Present-and-None so the stamp is a first-class field.
            "dock_arrival_time": None,
        }
        self.state = STATE_RUNNING
        self._interrupt_timer_started_at = None

    def _open_provisional_run(self, time_ms: int | None) -> None:
        """Open a provisional run on the vs = 4 activation edge, before any
        type-2 has carried mowing data.

        A run is a user session and starts when the operator presses run, while
        the firmware's first mowing-task type-2 lands ~3 min later (dock exit
        plus navigation). The first accepted type-2 seeds the baseline anchors
        and flips `provisional` off; `start_time` keeps the activation anchor.

        Every accumulator anchor is `None`, which downstream relies on:
        `_maybe_complete_run` cannot fire, so a provisional run never closes as
        `completed`; `_close_run` yields the minimal interrupted entry
        (`session_area = None`, `zones = []`, real wander `duration_ms`); and
        `zones == []` keeps the vestige gate armed for the whole window.

        Clearing `_pending_reset` is deliberate: a fresh activation invalidates
        any candidate stashed in the previous run's epoch, which must not
        confirm against this window's seeding packet.
        """
        self.current_run = {
            "start_time": time_ms,
            "mow_start_type": None,
            "wk0": None,
            "sub0": None,
            "last_time": time_ms,
            "last_sub": None,
            "last_wk": None,
            "last_mp": None,
            "zones": [],
            "provisional": True,
            # No dock arrival until it docks; on an aborted start the stamp
            # coincides with `last_time`.
            "dock_arrival_time": None,
        }
        self.state = STATE_RUNNING
        self._interrupt_timer_started_at = None
        self._pending_reset = None

    def _close_run(self) -> Event:
        """Close the currently open run.

        The result label is centralised here so every close path — the fast
        completion criterion, a fresh reset, the sustained timer, a resolved
        pending reset — labels identically: `completed` iff `last_mp ≥
        MP_COMPLETION_THRESHOLD`, or `last_mp ≥ MP_PARTIAL_THRESHOLD` with the
        last zone's `cmp_max` at `CMP_ZONE_COMPLETE_THRESHOLD`.
        """
        assert self.current_run is not None, "close_run without an open run"
        r = self.current_run
        if r.get("provisional"):
            # A provisional run reaching a close was an aborted start: pressed,
            # wandered, sent home without ever producing an accepted type-2.
            # The payload below is already the minimal history entry.
            self.counters["aborted_starts_committed"] += 1
        start = r.get("start_time")
        # A run that closed at a dock ends at the dock's arrival edge, not at
        # its last accepted type-2, so a session's duration is exactly
        # activation to arrival. The end is the stamp itself, with no
        # `max(…, last_time)` floor: a late completing flush is bookkeeping
        # emitted at task teardown, its `time` is emission time rather than
        # session activity, and it must not move the end past the physical
        # arrival. Accepted cosmetic consequence: a zone's last packet time may
        # exceed the run's `end_time` by those seconds. A close with no observed
        # dock carries no stamp and falls back to the packet cursor. `last_time`
        # is deliberately not mutated — the post-close gating baseline reads it.
        last_time = r.get("last_time")
        dock_arrival = r.get("dock_arrival_time")
        end = dock_arrival if dock_arrival is not None else last_time
        duration_ms: int | None = None
        if start is not None and end is not None:
            duration_ms = end - start
        result = RESULT_COMPLETED if self._is_completed() else RESULT_INTERRUPTED
        # Per-session area is `last_sub − sub₀`: the firmware's `subtotalArea`
        # continues across tasks, so raw `last_sub` would over-count a session
        # that resumed a still-running task. `None` when either endpoint is
        # missing.
        last_sub = r.get("last_sub")
        sub0 = r.get("sub0")
        session_area: float | None = None
        if last_sub is not None and sub0 is not None:
            session_area = last_sub - sub0
        # A close transitions the machine to IDLE; completed vs interrupted
        # lives in `result` below, never in a resting state.
        self.state = STATE_IDLE
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
        """Close the run when the completion rule holds with `vehicle_state ∈
        DOCK_EVIDENCE`. Immediate, no debounce. Returns the close event, or
        `None` when neither branch fires.

        Called from `process_type2` after the accumulator update, and from
        `process_vehicle_state` after the vs update, so both orderings —
        threshold-then-dock and dock-then-refresh — are handled.
        """
        if self.state not in (STATE_RUNNING, STATE_PAUSED_DOCKED):
            return None
        if self.vehicle_state not in DOCK_EVIDENCE:
            return None
        if not self._is_completed():
            return None
        return self._close_run()

    def _is_completed(self) -> bool:
        """Whether the current run has reached the completion rule. Shared by
        `_maybe_complete_run` and `_close_run`, so the fast path and the label
        can never disagree.
        """
        if self.current_run is None:
            return False
        last_mp = self.current_run.get("last_mp")
        if last_mp is None:
            return False
        if last_mp >= MP_COMPLETION_THRESHOLD:
            return True
        if last_mp >= MP_PARTIAL_THRESHOLD:
            zones = self.current_run.get("zones") or []
            if zones and (zones[-1].get("cmp_max") or 0) >= CMP_ZONE_COMPLETE_THRESHOLD:
                return True
        return False

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
        """Extend the current zone, or open a new one on a boundary change.

        `boundary = 0` (the session-init sentinel) is excluded from zone
        accounting; the packet still updates run accumulators.
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
            # The outgoing zone's `sub_exit` was updated on the previous
            # accepted packet, so no explicit closure step is needed.
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
        if vs == VS_DOCKED_IDLE:
            if self._interrupt_timer_started_at is None:
                self._interrupt_timer_started_at = self._clock()
        else:
            # Charging, or vs = 4/5 reaching here from a seeded PAUSED_DOCKED:
            # hold the run without a countdown.
            self._interrupt_timer_started_at = None

    def _arm_interrupt_timer(self) -> None:
        """Arm the interruption countdown unconditionally, charging included.

        Used only on the provisional-abort path: a pressed run that returned to
        the dock has no mowing data to hold for. Idempotent, so a charge↔idle
        flip during the debounce keeps the countdown running.
        """
        if self._interrupt_timer_started_at is None:
            self._interrupt_timer_started_at = self._clock()

    # ------------------------------------------------------------- #
    # Persistence                                                   #
    # ------------------------------------------------------------- #

    def snapshot(self) -> dict[str, Any]:
        """Serialize enough state for `restore()` to resume a mid-run tracker
        after an HA restart.

        `current_run` is deep-copied so the result is a true point-in-time
        capture: the caller serialises the payload in an executor, and a shared
        reference would let a packet processed meanwhile mutate the live
        `current_run` cross-thread mid-serialisation.
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
        """Load a previously-taken snapshot. Returns True on acceptance, False
        on a version mismatch (the caller decides whether to drop or upgrade).

        An open run with ``sub₀ = None`` is a live provisional shape and is
        restored faithfully, so the next ``_close_run`` reports
        ``session_area = None`` rather than fabricating a value from the
        firmware's task-scoped accumulator.
        """
        if snap.get("version") != SNAPSHOT_VERSION:
            return False
        state = snap.get("state", STATE_IDLE)
        # Robustness, not migration: an out-of-vocabulary state string, of any
        # vintage, maps to IDLE with one WARN and never raises.
        if state not in (STATE_IDLE, STATE_RUNNING, STATE_PAUSED_DOCKED):
            _LOGGER.warning(
                "run_tracker restore: unknown state %r — mapping to idle", state
            )
            state = STATE_IDLE
        self.state = state
        self.vehicle_state = snap.get("vehicle_state")
        self.current_run = snap.get("current_run")
        self._last_accepted_wk = snap.get("last_accepted_wk")
        self._last_accepted_time_ms = snap.get("last_accepted_time_ms")
        drops = snap.get("drops") or {}
        counters = snap.get("counters") or {}
        # Robustness, not migration: an absent counter or drop key defaults to
        # 0, so a partial or hand-edited snapshot never raises on read.
        self.counters = {
            "wk_regressions_observed": counters.get("wk_regressions_observed", 0),
            "invariant_deviations_observed": counters.get(
                "invariant_deviations_observed", 0
            ),
            "strict_progress_rejections": counters.get("strict_progress_rejections", 0),
            "aborted_starts_committed": counters.get("aborted_starts_committed", 0),
        }
        self.drops = {
            "pending_reset_holds": drops.get("pending_reset_holds", 0),
        }
        self._wk_regression_streak = 0
        self._invariant_deviation_streak = 0
        # `_interrupt_timer_started_at` is monotonic and cannot survive a
        # process restart; `tick()` re-arms it on the first call if the machine
        # is `PAUSED_DOCKED` under docked-idle. A pending reset is intentionally
        # not persisted: at worst a candidate re-confirms one packet later,
        # which beats serialising a transient decision.
        self._interrupt_timer_started_at = None
        self._pending_reset = None
        return True
