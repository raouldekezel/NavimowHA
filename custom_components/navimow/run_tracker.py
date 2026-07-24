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

State machine (BUG-09 / FEAT-06 / HARD-18 / HARD-19 revised; HARD-20
(#122) revised — three states, not five).

HARD-20 (SPIKE-03 #115 outcome B): the terminal resting states are gone.
A close is a machine transition to IDLE; the completed/interrupted
distinction lives in the close *record* (`run_finished` payload →
`last_finished_run["result"]`, `history[]`), the only place any consumer
reads it. Invariant: **IDLE = at rest, carrying an OPTIONAL last-closed
`current_run` reference** (kept across a close since FEAT-06) that keys
the post-close gating and holds the records. Three states — IDLE,
RUNNING, PAUSED_DOCKED — leave nothing to enumerate, which is what makes
the BUG-19 forgotten-resting-state class unrepresentable.

    IDLE ─vs=4─▶ RUNNING(provisional) [run_started]   (HARD-18 / #117 —
        a run is a user session and starts at activation; start_time =
        the type-1 time, ~1.5 s after the press. Anchors None until
        seeded.)
    RUNNING(provisional) ─first accepted type-2─▶ RUNNING (seeded)
        (sub₀ / mow_start_type / wk₀ / zone from the packet; start_time
        keeps the activation anchor; no second run_started)
    RUNNING(provisional) ─vs ∈ DOCK_EVIDENCE sustained 60 s─▶
        IDLE [run_finished: interrupted]   (aborted start — minimal
        entry: session_area=None, zones=[], real wander duration. Any
        dock, charging included.)
    IDLE (no reference, or reset sub < ceiling) ─fresh type-2─▶
        RUNNING [run_started]
    IDLE (seeded reference) ─fresh type-2, strict progress─▶ open NEW
        run [run_started]   (FEAT-06 / #54 — not a reopen; an echo, or an
        empty post-abort reference, is conservatively rejected + counted)
    RUNNING ─vs ∈ DOCK_EVIDENCE {1,2}─▶ PAUSED_DOCKED
    RUNNING/PAUSED_DOCKED ─vs ∈ {3,6} (VS_STOPPED / VS_MAPPING)─▶ (inert)
    PAUSED_DOCKED ─fresh type-2 while vs ∈ {4,5} (departure)─▶ RUNNING
        (resume, same run — intra-run recharge dock does not split)
    RUNNING/PAUSED_DOCKED ─(mp ≥ 100 OR (mp ≥ 99 ∧ zones[-1].cmp_max
        ≥ 10000)) ∧ vs ∈ {1,2}─▶ IDLE [run_finished: completed]
    RUNNING/PAUSED_DOCKED ─fresh reset (sub < last, sub < ceiling)─▶
        close open run [run_finished], open new run [run_started]
    PAUSED_DOCKED ─vs = 1 sustained 60 s─▶ IDLE
        [run_finished: interrupted]

HARD-18 (2026-07-21, #117): finishing the FEAT-06 session migration.
`process_vehicle_state` opens a **provisional** run on the vs=4
activation edge so the tracker state (and `sensor.*_etat_de_la_tonte`)
reflects the press within ~1.5 s instead of ~3 min — the delay until
the firmware's first mowing-task type-2. A run's `start_time` is the
activation `time` (the operator's "the run starts when I press run"),
carried from the vs=4 type-1 and NOT overwritten when the first type-2
seeds the run. The provisional run holds no honest mowing data
(`last_mp is None` ⇒ never `completed`; `zones == []` ⇒ the BUG-17/19
vestige gate stays armed for the whole start window). If it docks
before any type-2 seeds it, the sustained-dock timer commits a minimal
`interrupted` history entry (an aborted start is a session too — the
duration includes the real dock-exit / navigation wander).

HARD-19 (2026-07-23, #120): a run's `end_time` is the **dock-arrival**
type-1 `time` — the RUNNING → PAUSED_DOCKED edge of the *final* dock —
for every dock-closed run, mirroring HARD-18's activation-anchored
`start_time`. A session's duration is therefore exactly FEAT-06's
definition, **activation → dock arrival, both edges type-1-stamped**
(HARD-18 #117 / HARD-19 #120), so the *return* transit (last mow packet
→ dock) is now counted, closing the outbound/return asymmetry HARD-18
opened. The stamp lives in `current_run["dock_arrival_time"]`, set on
the dock edge in `process_vehicle_state`, frozen through the docked
idle↔charge flips (charging after arrival never moves the end), and
dropped on resume (an intra-run recharge dock is not the final dock).
`_close_run` reads `end = dock_arrival_time` when the stamp is present —
**strict, no floor** (operator arbitration §1): a late BUG-09 completing
flush (#89) is bookkeeping emitted at task teardown, its `time` is
emission time, not session activity, and never moves the end past the
physical arrival (family 6, inverted). Closes with no observed dock keep
the last-packet fallback. Riding in
the `current_run` deepcopy, the key needs no snapshot-shape change; a
run dict without it (legacy, or a no-dock close) falls back cleanly.
Future runs only; persisted `history[]` is untouched.

BUG-09 (2026-07-04): the completion criterion is
`mp ≥ MP_COMPLETION_THRESHOLD` ∧ `vs ∈ DOCK_EVIDENCE {1, 2}` (the two
dock-exclusive states). HARD-19 §2 (#120): `vs = 3` (VS_STOPPED,
arbitration 3) and `vs = 6` (VS_MAPPING, arbitration 4) are both
**inert** — location-agnostic, evidence of nothing, so neither completes
a run. See `docs/diag/2026-07-07_map-01_vs-empirical/` — the earlier
comment that named `vs = 6` an "explicit user pause" was empirically
wrong. Immediate close, no debounce.
The result label is centralised in `_close_run`: `completed` iff the
last accepted `mp ≥ MP_COMPLETION_THRESHOLD`, else `interrupted` — so
every close path (fast, reset, sustained-timer) labels consistently.

BUG-14 (2026-07-09, #89): threshold raised from 99 to 100 because a
firmware plateau at `mp = 99` is *indistinguishable from a recharge
return* on `mp` alone: on 2026-07-09 a real day was closed prematurely
on the recharge dock at `mp = 99`, splitting a single logical session
in two. Both plateaus (99 and 100) have been observed in the wild
(2026-05-25 & 2026-07-04 afternoon reached 100; 2026-07-04 morning
peaked at 99).

BUG-14 refinement (same day, operator-designed): also close when
`mp ≥ 99 ∧ zones[-1].cmp_max ≥ 10000`. The zone-scoped `cmp` reaches
10000 when the firmware confirms the last active zone is 100 % mowed,
giving an independent completion signal that discriminates a real
finish from a recharge return. On the 2026-07-09 day, the resume
after recharge added `cmp = 10000` on the same last-active boundary,
so the second dock arrival closes the (single, continuous) session as
`completed` rather than leaving it to a `interrupted` sustained-timer
close. Runs whose `mp` and `cmp` both plateau below the thresholds
still close via the sustained-timer path with `interrupted`.

FEAT-06 (2026-07-05, #54): a run maps to a **user session** — an
activation → final dock cycle. Intra-run recharge docks (vs=2 while
`mp < threshold`) do NOT split the session (unchanged). What changes
is the post-close boundary: a fresh accepted type-2 arriving after a
close (HARD-20: at rest in IDLE with a seeded reference) no longer
*reopens* the closed run — it opens a **new run** at that packet's
time, with `sub₀` = that packet's
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
run). New-session transitions from a resting IDLE with a seeded
reference require strict `sub > last_sub` progress (B1 on #49: an echo
packet after a close must not spawn phantom sessions).

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
#
# HARD-20 (#122, SPIKE-03 outcome B): three states, not five. The terminal
# resting states COMPLETED / INTERRUPTED were collapsed into IDLE — a close
# is a machine transition to IDLE, and the completed/interrupted label lives
# in the close *record* (`last_finished_run["result"]`, `history[]`), the
# only place any consumer reads it. IDLE = at rest, carrying an OPTIONAL
# last-closed `current_run` reference that keys the post-close gating and
# holds the records. The RESULT_* labels below are the record vocabulary and
# are untouched.
STATE_IDLE = "idle"
STATE_RUNNING = "running"
STATE_PAUSED_DOCKED = "paused_docked"

# vehicleState values (from MAP-01 diag / #25, corrected on 2026-07-07
# via `docs/diag/2026-07-07_map-01_vs-empirical/`).
VS_DOCKED_IDLE = 1
VS_DOCKED_CHARGING = 2
# vs = 3 is a **generic stopped state** — "not mowing, not returning,
# not charging, not mapping" — and it is NOT a dock state. Observed
# sub-cases (MAP-01, 2026-07-07): (a) a real user pause emitted off-dock
# mid-mow (`/state = isPaused`, posture far from origin), (b) a transient
# at-dock idle flip between charging samples (`/state = isIdel`),
# (c) docked on an unpowered base (`/state = isDocked`). Because it spans
# off-dock and at-dock indistinguishably on the type-1 channel, HARD-19
# §2 (#120) treats vs = 3 as **evidence of nothing**: in neither evidence
# set, it never stamps a dock arrival, clears one, arms or disarms the
# interrupt timer, resumes a paused run, or closes. The historical name
# `VS_DOCKED_UNPOWERED` (which read case (c) as the whole story) is retired
# for `VS_STOPPED`. Dock vs departure discrimination lives in the
# DOCK_EVIDENCE {1,2} / DEPARTURE_EVIDENCE {4,5} sets below.
VS_STOPPED = 3
VS_MOWING = 4
VS_RETURNING = 5
# vs = 6 = firmware map-consolidation phase (isMapping in the `/state`
# channel), typically post-mow at the dock — but mapping is a *driving
# activity*: a user-initiated remap runs off-dock. HARD-19 §2 arbitration 4
# (#120) therefore reclassifies vs = 6 as **location-agnostic and inert for
# dock semantics** (like vs = 3): it is NOT dock evidence and never
# stamps/arms/closes. The earlier label `VS_PAUSED` and its "explicit user
# pause" comment were empirically wrong — real user pause emits vs = 3.
VS_MAPPING = 6
VS_TRANSIENT = 8  # firmware-reset transient (posture all-zero)

# HARD-19 §2 (#120) — evidence-role naming. These are the ONLY two sets
# the machine may branch on for dock semantics. They are named for their
# **evidentiary role**, not their membership arithmetic: a membership name
# (the old `DOCKED_NOT_CHARGING`) rots silently when the taxonomy moves; a
# role name encodes its own admission criterion.
#
# Principle: dock evidence must be a state that is physically
# **dock-exclusive** — one the robot can only be in while on the base.
#
# DOCK_EVIDENCE = arrival. `vs=1` (idle-on-base) and `vs=2` (charging) are
# dock-exclusive; an edge into one is a true dock arrival → stamp +
# PAUSED_DOCKED + completion-eligible.
DOCK_EVIDENCE = frozenset({VS_DOCKED_IDLE, VS_DOCKED_CHARGING})
# DEPARTURE_EVIDENCE = leaving. The robot is physically off the dock and
# moving — the only signal that clears an intermediate dock stamp (a
# mid-run recharge that resumed) and re-opens a provisional abort window.
DEPARTURE_EVIDENCE = frozenset({VS_MOWING, VS_RETURNING})
#
# vs = 3 (VS_STOPPED) and vs = 6 (VS_MAPPING) belong to NEITHER set — both
# are **location-agnostic** (a user pause and a user-initiated remap both
# run off-dock), so they are **inert for dock semantics**: they never
# stamp, clear, arm, disarm, resume, or close (arbitrations 3 & 4). The
# completion predicate is exactly DOCK_EVIDENCE, and the sustained-timer
# arms on the single constant VS_DOCKED_IDLE — the retired sets
# `DOCKED_STATES` / `DOCKED_NOT_CHARGING` / `DOCKED_NOT_USER_PAUSED` are
# gone (the `VS_DOCKED_` prefix is now a true invariant: it survives only
# on the two dock-exclusive states).

# HARD-19 §2 (#120): the complete classification of the *steady*
# vehicleStates — every VS_* constant except the out-of-band firmware-reset
# sentinel VS_TRANSIENT (8) must belong to exactly one evidentiary group.
# The partition pin derives the VS_* constants by module introspection and
# asserts equality against this set, so a future firmware state breaks a
# test unless it is classified here (not a silent hole).
KNOWN_VEHICLE_STATES = (
    DOCK_EVIDENCE | DEPARTURE_EVIDENCE | frozenset({VS_STOPPED, VS_MAPPING})
)

# BUG-14 (2026-07-09, #89): threshold raised from 99 to 100. The
# earlier value (99) was chosen when the only observed firmware plateau
# was 99, but the recharge-at-mp-99 pathology surfaced on 2026-07-09:
# the operator's real day was mow (mp reaches 99) → return dock with
# battery 15 % to recharge → resume and finish → dock again. With
# threshold 99 the first dock arrival closed the run as `completed`
# (false positive: it was a recharge, not a finish), the mini-return
# after recharge opened a new session, and one logical session was
# split into two runs.
MP_COMPLETION_THRESHOLD = 100

# BUG-14 refinement: firmware tasks whose task-scoped `mp` plateaus at
# 99 without ever emitting 100 can still be discriminated from a
# recharge return by the zone-scoped `currentMowProgress` (`cmp`):
# `cmp = 10000` means the firmware confirms the currently-active zone
# is 100 % mowed. Combined with the task-scoped `mp = 99` at dock, that
# is credibly a real finish (task's last zone is done, only mp is
# late-plateauing). Below the threshold — the run keeps holding in
# PAUSED_DOCKED as a recharge candidate.
MP_PARTIAL_THRESHOLD = 99
CMP_ZONE_COMPLETE_THRESHOLD = 10000
# Trade-off: on a multizone task where the robot finishes zone A
# (cmp=10000) and returns to dock to recharge before starting zone B,
# this rule closes as `completed` — the residual false positive. Not
# observed in the corpus (2026-05-25 multizone ran without an inter-
# zone recharge); noted here so a future report can reproduce it.
# `session_area` and `zones[]` remain correct on either label.

# BUG-17 (2026-07-19, #105): protocol facts about the firmware's late
# task-end replay packet — distinct from the *policy* completion
# thresholds above.
#
# `MP_TASK_END` is the wire value the firmware stamps on the vestige.
# It coincides numerically with `MP_COMPLETION_THRESHOLD` today (both
# 100), but they are conceptually independent: the completion
# threshold is a tunable tracker policy that has already moved once
# (99 → 100 in PR #91 / BUG-14) and is still under debate (#89's
# battery-gate alternative). If it moves again the vestige guard must
# not follow — a value of 99 as completion threshold would still see
# a `mp = 100` vestige on the wire. Keeping the two constants
# separate makes the coupling explicit and prevents silent drift.
#
# `RUN_START_SUB_TOLERANCE` is the tolerance for the `area_session`
# (`sub`) check in the vestige signature. The vestige carries
# `subtotalArea = 0.0`; a legitimate first packet of a fresh mow
# already carries at least ~2.4 m² after one type-2 cadence (2.47 m²
# observed on 2026-07-19). 0.5 m² sits below both and above the
# vestige's literal zero, giving headroom for a firmware variant that
# emits a residual float near zero.
MP_TASK_END = 100
RUN_START_SUB_TOLERANCE = 0.5

# Seconds a PAUSED_DOCKED run must remain docked-idle (vs=1) before
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
        # paused / running; set to `_clock()` when we enter docked-idle
        # (`vs = 1`) under an open run.
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
            # HARD-18 (#117): from raoul.26 this counter only ever sees
            # *within-session* deviations. Sessions now open at the vs=4
            # activation edge with `wk0 = None`, so the first packet
            # re-anchors instead of being judged against the previous
            # session's dead anchor — the cross-boundary drift shape that
            # produced the pre-raoul.26 instance baseline. A flat counter
            # post-deploy is therefore expected, not a sign the invariant
            # machinery broke.
            "invariant_deviations_observed": 0,
            # strict-progress rejections on the terminal-state path (no
            # preceding vs=4) — previously a silent `return`.
            # `aborted_starts_committed` counts provisional runs closed by
            # the sustained-dock abort (a pressed run that wandered and was
            # sent home without ever mowing).
            "strict_progress_rejections": 0,
            "aborted_starts_committed": 0,
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

    @property
    def is_provisional(self) -> bool:
        """HARD-18 (#117): True while the current run is a *provisional*
        session — opened at the vs=4 activation edge and not yet seeded
        by a type-2. The state sensor renders this window as "starting";
        the seeding block in `process_type2` flips it off. Exposed as a
        property so the sensor platform never reaches into `current_run`.
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

        # BUG-17 (2026-07-19, #105) + BUG-19 (2026-07-20/21, #114):
        # drop the run-start "task-end vestige" packet before any
        # state transition or write. On task-start the firmware
        # sometimes replays the *previous* task's closing packet as
        # the very first `type-2` on the fresh mow — the ceiling
        # signature `mp = 100 ∧ cmp = 10000` (its `subtotalArea` is
        # zeroed in the BUG-17/BUG-19 shape, frozen at the previous
        # close in the BUG-16 shape — `sub` never discriminates, so
        # the guard no longer tests it). Left untouched:
        # - From `STATE_IDLE`: vestige is what `_open_run` reads;
        #   `start_time`, `sub₀`, `mow_start_type` all anchored on it;
        #   `_update_zone` seeds `zones[0].cmp_max = 10000` (monotonic
        #   max — sensor.<slug>_current_zone_progress sticks at 100 %
        #   for the whole zone).
        # - From post-close (HARD-20: at rest in IDLE with a seeded
        #   reference — the operator's dominant path since Store
        #   persistence landed FEAT-05c 2026-07-04): vestige takes the
        #   `is_reset` branch (0.0 < prev `last_sub`) → `_open_run(vestige)`
        #   ⇒ same anchoring collateral, same `zones[0]` poisoning.
        # - From BUG-06 sentinel-then-vestige (open run, `zones ==
        #   []`): sentinel already anchored the run, only
        #   `_update_zone` and `last_mp` are at risk — still real.
        #
        # BUG-19 step 1 widened the arming window from an enumeration
        # of safe states to "armed unless we're mid-mow with a real
        # zone already seeded" — the enumeration missed post-close.
        # Step 2 simplified the drop signature to the ceiling alone
        # (`mp = 100 ∧ cmp = 10000`, no `sub` term), which absorbs
        # BUG-16's (#92) frozen-`sub` variant into the same guard.
        # See `_gate_run_start_vestige` for the full rationale.
        if self._gate_run_start_vestige(parsed):
            return events

        # BUG-15 (#90): drop the all-zero session-init sentinel. The firmware
        # emits a zero-payload type-2 at task start
        # (`boundary = 0 ∧ mp = 0 ∧ cmp = 0 ∧ sub = 0 ∧ action = -1`) ~15 s
        # AFTER `vs = 4` (validated 4/4 in the 2026-05 corpus, 3/3 in the
        # post-HARD-18 window). It carries no zone, no progress, and nothing
        # the physical `vs = 4` edge (HARD-18) does not already give — a real
        # session-init therefore always arrives while the tracker is already
        # RUNNING (the `vs = 4` provisional is open), so it never reaches the
        # `STATE_IDLE` open. Left in, it is pure liability:
        # - from `STATE_IDLE` it opens a phantom run that then lingers (it
        #   opens RUNNING while the robot is already docked, so no
        #   `RUNNING → PAUSED_DOCKED` edge arms the sustained timer — observed
        #   ~10.9 h on 2026-07-24) and can engulf the next real mow;
        # - in `STATE_RUNNING` on a seeded run (`last_sub > 0`) its `sub = 0`
        #   trips `is_reset` → close + reopen, splitting a real run.
        # Dropping it only ever removes a docked spurious sentinel; the real
        # run seeds on the first boundary-carrying packet a moment later
        # (`sub0` anchors ~2.5 m² later, ~1 %). Signature is the all-zero
        # shape, NOT bare `boundary = 0` — a `boundary = 0 ∧ mp = 100` packet
        # is a task-*end* marker (completion + final area) and must not drop.
        _b = parsed.get("boundary")
        _mp = parsed.get("mowing_percentage")
        _sub = parsed.get("area_session")
        if _b in (None, 0) and (_mp or 0) == 0 and (_sub or 0.0) == 0.0:
            _LOGGER.debug(
                "run_tracker: all-zero session-init sentinel dropped (BUG-15) "
                "(state=%s vs=%s boundary=%s mp=%s cmp=%s sub=%s wk=%s action=%s time=%s)",
                self.state,
                self.vehicle_state,
                _b,
                _mp,
                parsed.get("current_mow_progress"),
                _sub,
                parsed.get("area_week"),
                parsed.get("action"),
                parsed.get("time"),
            )
            return events

        # HARD-18 (#117) — Sol/Fable review 2026-07-23 (blocking): a
        # type-2 that arrives while a provisional start is STILL DOCKED
        # must be ignored — neither resume nor seed. Placed right after
        # the vestige gate (which keeps its own ceiling-replay drop
        # accounting) and before every state transition.
        #
        # Why not seed here: seeding resumes to RUNNING and clears the
        # abort timer, leaving `RUNNING ∧ docked ∧ timer=None`. `tick()`
        # acts only in PAUSED_DOCKED, so with no further vs *edge* to
        # forward (type-1 ~2 s vs type-2 30-90 s cadence skew makes a
        # delayed docked type-2 ordinary) the run would render `running`
        # indefinitely while docked. Worse, if that delayed packet is a
        # near-close replay (`mp ≥ threshold ∧ ceiling cmp`), the later
        # abort `_close_run` would see `_is_completed()` true and mint a
        # phantom *completed* session. Ignoring preserves the arbitrated
        # minimal-abort entry unconditionally; a genuine micro-mow's data
        # is dropped (the DEBUG line keeps the evidence) — accepted per
        # the arbitration. A real dock-poke is recovered by the off-dock
        # type-1 (which returns the provisional run to RUNNING in
        # `process_vehicle_state`); the next type-2 then seeds normally.
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
            # HARD-20 (#122): IDLE = at rest, with an OPTIONAL last-closed
            # `current_run` reference (kept across a close since FEAT-06
            # #54). Its contents key the gate — which is what the old
            # `state ∈ {COMPLETED, INTERRUPTED}` enumeration always meant:
            # no reference (first boot) → ungated open; seeded reference →
            # post-close gating; empty reference (post-abort, both axes
            # None) → conservative reject, self-resolving at the next vs=4.
            if is_reset:
                # Split by ceiling: sub << ceiling → a genuine run-start;
                # sub above → hold as a pending candidate a coherent
                # successor must confirm (BUG-08 mixed-epoch packets must
                # not destroy a live run). Nothing open to close here.
                if incoming_sub is not None and incoming_sub < RESET_SUB_CEILING:
                    self._open_run(parsed)
                    events.append(self._event_run_started())
                else:
                    self._stash_pending_reset(parsed)
                    return events
            else:
                # FEAT-06 (#54): a fresh type-2 with strict progress after a
                # close opens a NEW session (not a reopen); an echo (same
                # sub/mp, only time fresher) is rejected, else a stream tail
                # spawns phantom sessions after every close (B1 on #49).
                # The `current_run is not None` guard makes the first-boot
                # path byte-identical to the pre-HARD-20 IDLE open:
                # current_run None ⇒ prev_sub None ⇒ is_reset False ⇒ here ⇒
                # guard short-circuits ⇒ `_observe_invariant_deviation`
                # no-ops (no wk₀) ⇒ `_open_run` + run_started.
                # HARD-18 (#117): the refusal is counted + DEBUG-logged
                # (was silent) so the audits see whether strict progress
                # ever fires in production.
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
                # HARD-06 (#62): observe the invariant against the closed
                # run's `wk₀` BEFORE `_open_run` re-anchors (a no-op on
                # first boot with no reference); persistent drift here is
                # structurally impossible.
                self._observe_invariant_deviation(parsed)
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
                # HARD-19 §3 (#120): resume from a pause + drop the
                # intermediate dock stamp ONLY on departure evidence — the
                # robot physically left the dock (`vehicle_state ∈ {4, 5}`)
                # and a fresh type-2 is now continuing the mow, so the dock
                # it left was a mid-run recharge, not the session's final
                # dock. Only a subsequent (final) dock's arrival can then end
                # the run. A type-2 that arrives while still docked
                # (`vehicle_state ∉ DEPARTURE_EVIDENCE` — the late BUG-09 completing
                # flush, or a cross-stream skew where the packet beats the
                # off-dock type-1) still updates accumulators and may complete
                # the run below, but it never resumes and never clears the
                # stamp: the run then ends at the frozen dock arrival, not the
                # flush packet (family 6, inverted). `current_run` is non-None
                # here (PAUSED_DOCKED implies an open run).
                if (
                    self.state == STATE_PAUSED_DOCKED
                    and self.vehicle_state in DEPARTURE_EVIDENCE
                ):
                    self.state = STATE_RUNNING
                    self._interrupt_timer_started_at = None
                    self.current_run["dock_arrival_time"] = None

        # HARD-18 (#117): seed a provisional run from its first accepted
        # type-2. The run was opened at the vs=4 activation edge with all
        # baseline anchors `None` (see `_open_provisional_run`); this is
        # the first packet carrying honest task data. `start_time` is
        # deliberately NOT touched — it keeps the activation anchor (the
        # run starts when the operator pressed run). `wk₀` is set below by
        # `_update_wk0_anchor`, `last_*` by `_update_accumulators`, the
        # zone seed by `_update_zone`. Flipping `provisional` off here is
        # what makes this block a one-shot: the next packet finds a
        # non-provisional run. The DEBUG line logs the full shape of every
        # start-window first packet — the passive #105-Q1 evidence
        # collector (below-ceiling replay shapes). A BUG-06 all-zero
        # sentinel seeds `sub0 = 0.0`, exactly as `_open_run(sentinel)`
        # would from IDLE (parity kept, not a regression).
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

        # BUG-09: completion criterion (`mp ≥ 99 ∧ vs ∈ {1,2}`). Fires
        # when a fresh type-2 pushes `last_mp` over the threshold while
        # the robot is already docked (the mp-then-dock path is handled
        # by `process_vehicle_state`).
        completion = self._maybe_complete_run()
        if completion is not None:
            events.append(completion)

        return events

    def process_vehicle_state(
        self, vs: int, *, time_ms: int | None = None
    ) -> list[Event]:
        """React to a `vehicleState` change (type-1 packet).

        HARD-18 (#117): a run is a **user session** and starts at
        activation. On the `{IDLE, COMPLETED, INTERRUPTED} → vs=4`
        transition the tracker opens a **provisional** run immediately,
        so `state` (and the state sensor) reflects the press ~1.5 s later
        instead of ~3 min later — the delay until the firmware's first
        mowing-task type-2 lands. `time_ms` is the type-1 packet's `time`,
        which becomes the run's `start_time`; the coordinator passes it
        from `parsed.get("time")`. The keyword-only default keeps every
        existing caller (and test) valid.

        Otherwise: entries into `DOCK_EVIDENCE` (`{1, 2}`) from
        `RUNNING` move a live run into `PAUSED_DOCKED`. Resume of a
        *seeded* run is driven by a fresh type-2 in `process_type2`
        (gated on departure evidence `vehicle_state ∈ {4, 5}`, HARD-19 §3),
        not by the vs edge itself — a type-1 briefly showing `vs=4` during
        a dock-poke must not by itself "resume" a real run. A *provisional*
        run, having no mowing data to hold for, treats any sustained dock
        as an aborted start (see `tick` and `_close_run`). HARD-19 §2
        (#120): `vs = 3` (VS_STOPPED) is inert here — it returns before any
        transition.
        """
        events: list[Event] = []

        if vs == VS_TRANSIENT:
            return events

        self.vehicle_state = vs

        # HARD-19 §2 (#120): vs = 3 (VS_STOPPED) and vs = 6 (VS_MAPPING) are
        # both **evidence of nothing** (arbitrations 3 & 4) — location-
        # agnostic states in neither DOCK_EVIDENCE nor DEPARTURE_EVIDENCE. A
        # user pause and a user-initiated remap both run off-dock, so
        # neither is a reliable dock signal. They never stamp a dock
        # arrival, clear one, arm or disarm the interrupt timer, resume a
        # paused run, or close: the open run (RUNNING or PAUSED_DOCKED)
        # rides through untouched, its timer context intact, so MAP-01's
        # transient `2 → 3 → 2` dock flip and its `1 → 6 → 1` analogue pass
        # straight through. `vehicle_state` is updated above so the display
        # ladder can render « En pause » on vs = 3 (§5; vs = 6 reads
        # « En cours »); no machine transition occurs, and completion cannot
        # fire (the predicate is DOCK_EVIDENCE = {1, 2}).
        if vs in (VS_STOPPED, VS_MAPPING):
            return events

        # HARD-18: eager session start. HARD-20 (#122): the three terminal
        # origins collapsed to `IDLE`, so this is a single equality test.
        # Once `state == RUNNING` it is structurally false, so a repeated
        # vs=4 or a 4→5→4 wobble cannot re-open (dedupe is free, no flag).
        if vs == VS_MOWING and self.state == STATE_IDLE:
            self._open_provisional_run(time_ms)
            events.append(self._event_run_started())
            return events

        provisional = self.is_provisional

        if self.state == STATE_RUNNING:
            if vs in DOCK_EVIDENCE:
                # HARD-19 (#120): stamp the dock-arrival edge on the current
                # run FIRST — before the arm / complete logic below — so a
                # completion close fired later in this same call (the BUG-09
                # dock-then-`mp = 100` fast path, #89) reads a stamp that
                # already exists. There is no window where the close can
                # precede the stamp because the type-1 that closes IS the
                # type-1 that stamps; the ordering concern raised on #120
                # (a later `/state → docked` losing the race) cannot occur —
                # the stamp comes ONLY from this `/location` type-1 edge, a
                # single stream, so it is race-free by construction. The
                # mower entity's non-docked → docked activity is derived from
                # the SEPARATE `/state` `DeviceStateMessage` stream, which has
                # no write path into the tracker (Sol/Fable review, #126); the
                # stamp needs no equivalence-of-feeds claim (Fable Retraction
                # A) — the tracker owns this edge without importing HA state.
                # Frozen through the docked idle↔charge flips (those re-enter
                # via the PAUSED_DOCKED branch below, which does not stamp)
                # and cleared only on departure evidence (§3: a resume with
                # `vehicle_state ∈ {4, 5}` — a mid-run recharge dock the
                # robot then left is not the session's final dock). vs entry
                # is DOCK_EVIDENCE `{1, 2}` only — vs = 3 and vs = 6 returned
                # inert above and never reach this stamp. Gated on `time_ms`
                # so a legacy
                # caller without it falls back to the last packet cursor in
                # `_close_run`. For a provisional run the stamp coincides
                # with `last_time` (both = this edge's `time_ms`), so the
                # HARD-18 abort mechanics below are untouched.
                if time_ms is not None:
                    self.current_run["dock_arrival_time"] = time_ms
                # HARD-18 abort arming: a provisional run has no mowing
                # data to hold for, so *any* DOCK_EVIDENCE entry (vs=1 idle
                # or vs=2 charging) starts the close countdown. The
                # wander end is stamped here on the RUNNING→PAUSED_DOCKED
                # edge and then frozen — a later charge↔idle flip an hour
                # after the abort must not inflate the duration.
                if provisional:
                    if time_ms is not None:
                        self.current_run["last_time"] = time_ms
                    self.state = STATE_PAUSED_DOCKED
                    self._arm_interrupt_timer()
                else:
                    self.state = STATE_PAUSED_DOCKED
                    self._start_interrupt_timer_if_applicable(vs)
            elif provisional and time_ms is not None:
                # Still off-dock and provisional (vs=4 navigation, or a
                # vs=5 transit on an aborting start): keep the wander
                # duration live so an eventual abort reports real time.
                self.current_run["last_time"] = time_ms
        elif self.state == STATE_PAUSED_DOCKED:
            if provisional:
                if vs in DOCK_EVIDENCE:
                    # Still docked — keep the abort countdown armed
                    # regardless of charging; do NOT refresh `last_time`
                    # (frozen at the dock-entry edge above).
                    self._arm_interrupt_timer()
                else:
                    # Departure evidence (`vs ∈ {4, 5}` — vs = 3 returned
                    # inert above, so this else is reached only on {4, 5}):
                    # left the dock again before the debounce fired, so the
                    # provisional window re-opens off-dock; a real type-2
                    # (resume + seed) or a sustained re-dock (abort) will
                    # resolve it. Disarm and keep the wander duration live.
                    self.state = STATE_RUNNING
                    self._interrupt_timer_started_at = None
                    if time_ms is not None:
                        self.current_run["last_time"] = time_ms
                    # HARD-19 §3 (#120): that dock was intermediate — drop
                    # its arrival stamp (symmetric with the seeded resume).
                    self.current_run["dock_arrival_time"] = None
            else:
                # Charging / explicit pause reset the timer; docked-and-
                # not-charging arms it.
                self._start_interrupt_timer_if_applicable(vs)

        # BUG-09: the run may already have reached the mp threshold
        # before the robot arrived at the dock — process_type2 alone
        # can't fire the close in that ordering because no further
        # type-2 packet is guaranteed after dock arrival. Firing here
        # closes the run as soon as `vs` enters DOCK_EVIDENCE {1, 2}
        # (vs = 3 and vs = 6 already returned inert above). A provisional
        # run cannot
        # complete here (`last_mp is None`).
        completion = self._maybe_complete_run()
        if completion is not None:
            events.append(completion)

        return events

    def tick(self, now: float | None = None) -> list[Event]:
        """Advance the sustained-docked interruption timer.

        Called periodically (coordinator cadence ~30 s). Two roles:
        - Arm the timer if we are `PAUSED_DOCKED` under
          docked-idle (`vs = 1`) and it is not yet running. This makes
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

        if self.state == STATE_PAUSED_DOCKED:
            # HARD-18 (#117): a *provisional* run (aborted start) fires on
            # ANY sustained dock — charging included — because there is no
            # mowing data to hold for. A *seeded* run keeps the original
            # semantics: only vs = 1 (idle) arms, so a mid-run
            # recharge (vs=2) never times out.
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
        """BUG-17 (#105) + BUG-19 (#114): drop the late task-end vestige.

        Name is a gate, not a predicate: on top of returning `True`
        when the packet must be dropped, this method also emits an
        observability DEBUG line on the suspicious-but-not-dropped
        shape (see the "observability" paragraph below). A pure
        `_is_...` name would understate the side effect.

        Semantics (BUG-19 widening, 2026-07-20): **armed by default;
        dark only in one specific state — an open run whose first
        zone is already honestly seeded**. The asset the guard
        protects is the *next* run's `zones[0]` seed plus the
        `_open_run` anchor (`start_time`, `sub₀`, `mow_start_type`);
        that asset is at risk in every state except "mowing with a
        real zone in flight". Naming the single dark state is safer
        than enumerating the armed ones — the raoul.22 enumeration
        (IDLE ∪ "open run with zones == []") missed post-close, which
        turned out to be the operator's dominant real-world entry
        state (HARD-20: Store restore rehydrates a resting IDLE with a
        seeded reference after any prior close; every next mow starts
        from that reference-bearing rest, not from a fresh IDLE).

        Concretely the guard is armed in:

        - `STATE_IDLE` — the observed 2026-07-19 order. Nothing in
          `process_vehicle_state` opens a run in the current tracker,
          so at the moment the vestige arrives on the wire the
          tracker is still `IDLE`; without this guard, the ungated
          `STATE_IDLE` branch of `process_type2` would call
          `_open_run(vestige)`, anchoring `start_time`, `sub₀`, and
          `mow_start_type` on the vestige's fields, then
          `_update_zone` would seed `zones[0].cmp_max` at 10000.
        - Open run (`STATE_RUNNING` / `STATE_PAUSED_DOCKED`) with
          `zones == []` — the **BUG-06 sentinel** order. The
          firmware emits an all-zero session-init `type-2`
          (`boundary = 0 ∧ mp = 0 ∧ cmp = 0 ∧ action = -1`) at run
          start, with a real boundary landing ~60 s later (2026-05-25
          and 2026-07-03 corpus). That sentinel is accepted by
          `process_type2` (accumulators updated, `state → RUNNING`,
          `run_started` emitted), but `_update_zone` rejects
          `boundary = 0` — so `zones` stays empty until the real
          boundary arrives. A vestige delivered at the second packet
          position (before the real boundary) must still be dropped.
          `STATE_PAUSED_DOCKED` covers the sentinel-then-dock variant.
        - **Post-close** (HARD-20: at rest in IDLE with a seeded
          reference) — the operator's dominant path (BUG-19, 2026-07-20
          wire trace). The closed run stays referenced by `current_run`
          with non-empty `zones` from the completed session, but the
          `zones == []` half of the dark predicate filters it out: the
          `zones` filled here belong to the *previous* run, not to a
          new one being seeded. Absent the guard, the vestige takes
          the `is_reset` branch (0.0 < prev `last_sub`, below
          `RESET_SUB_CEILING`) → `_open_run(vestige)` anchors the
          fresh run entirely on the vestige. With the guard, the
          packet is dropped before that machinery ever runs.
        - **Provisional start window** (`STATE_RUNNING` with
          `zones == []`, HARD-18 / #117) — the run opened on the vs=4
          activation edge has an empty zone list until its first type-2
          seeds it, so `mowing_with_zone` is False and the guard stays
          armed for the whole window. A ceiling vestige delivered as the
          first start-window packet is dropped here; the next honest
          packet seeds the run. This is why the eager transition opens a
          run with `zones == []` rather than flipping to a state that
          references the previous run's seeded zones — the latter would
          satisfy the dark predicate and reopen the vestige hole.

        Dark only in:

        - Open run with a real zone in flight. The nullity subtlety
          survives the inversion: post-close, `current_run`
          references the *closed* run with non-empty `zones` — which
          is precisely why the dark predicate requires
          `state ∈ {RUNNING, PAUSED_DOCKED}` **and** seeded zones
          together, not either alone.

        Rejected alternative D (Sol review, #105): an IDLE-only
        window, or a `has_accepted_type2` flag on the open run.
        Both go dark after the BUG-06 sentinel and reopen the
        pathology on the sentinel-first ordering.

        One packet drop suppresses five documented symptoms at once:
        the `current_run_progress` flash, the sticky
        `current_zone_progress`, the poisoned `first_time` (FEAT-08
        `last_complete_pass_at`), the poisoned `sub_entry = 0.0`
        (`size_estimate_m2` in the multi-zone-latent scenario), and
        the over-stated `Store.last_cmp_max` on interrupted runs —
        plus the BUG-14 fast-path interaction (`mp = 99 ∧ cmp_max =
        10000` on a poisoned zone). Post-BUG-19, the same suppression
        set applies to the post-close entry state, where the
        underlying reset-branch chain (BUG-13's steps 2–5) previously
        opened a phantom run.

        The rejection signature is the ceiling alone: `mp =
        MP_TASK_END ∧ cmp ≥ CMP_ZONE_COMPLETE_THRESHOLD`. `sub` is
        deliberately *not* gated (BUG-19 step 2): it never carried
        the drop decision — it only distinguished the two vestige
        sub-shapes (zeroed vs frozen-at-the-previous-close), and both
        are the same phantom. Testing `area_session < 0.5` is
        precisely why raoul.22 caught the zero-`sub` shape (BUG-17)
        yet missed the frozen-`sub` shape (BUG-16, #92); dropping the
        `sub` clause folds #92 into this one guard. Safety is
        categorical, not empirical: a run cannot *open* at `cmp =
        10000` — that is a finished-boundary state; a genuine start
        shows `cmp` climbing from low (`cmp = 104` on the 2026-07-20
        first real packet). `mp` may re-base high on a resumed task
        (SPIKE-02: `mp = 65`), but `cmp` on the active boundary always
        starts low, so the conjunction `mp = 100 ∧ cmp = 10000` has no
        legitimate opening packet. Combined with the dark window (a
        seeded run mid-mow — where genuine completion packets live),
        no honest packet is rejected. `wk` and `action` are logged but
        not gated on: the observed `wk = 0.0` in raoul.19 was a
        calendar artifact of the Sunday-start firmware week — a
        mid-week vestige carries `wk > 0` (`wk = 357.63` on 2026-07-20
        confirms), `action = -1` appears on legitimate mid-run packets
        and its cross-firmware stability is not established. Fail-open
        on `None` survives the simplification: a missing `mp` fails
        `== MP_TASK_END` and a missing `cmp` collapses to `0` via
        `(cmp or 0)`, so an incomplete packet never matches the drop
        signature.

        Also emits an observability DEBUG line for suspicious-but-
        -not-dropped shapes: `area_session` near zero inside the
        armed window without the full `mp = MP_TASK_END ∧ cmp =
        CMP_ZONE_COMPLETE_THRESHOLD` match. Collects evidence for
        the untested `interrupted` vestige shape (open question 1).
        This line does fire on some genuine low-`sub` first packets
        — a fresh session post-reset was observed at `sub = 0.39 m²`
        on 2026-05-25 and a Sunday-first-mow at `sub ≈ 0.3` is
        plausible — so the DEBUG stream carries a few false
        positives per week. Analysis discriminates them via the
        logged `mp` / `cmp` (a genuine low-`sub` start carries
        `mp = 0` and `cmp ≪ 10000`; an `interrupted` vestige would
        carry the partial `cmp_max` from the previous close).
        Accepted noise, not a defect.

        Note: #92 (BUG-16)'s "observability hook" referenced in the
        #105 fifth-edit body is retired, not deferred — the ceiling
        signature *drops* the frozen-`sub` shape rather than merely
        observing it (a drop DEBUG beats an observe DEBUG), so #92 is
        absorbed by this guard rather than shipping separately.
        """
        # BUG-19 (#114): arming inverted. The asset the guard protects
        # is the *next* run's `zones[0]` seed (plus `_open_run` anchor);
        # that asset is at risk in every state except "open run whose
        # first zone is already honestly seeded". Name that single dark
        # state; be armed everywhere else. The prior raoul.22
        # enumeration ({IDLE} ∪ {open run, zones == []}) missed the
        # post-close states where `_open_run` fires via the `is_reset`
        # branch — the operator's dominant real-world path.
        #
        # The nullity subtlety survives inverted: post-close, the closed
        # run stays referenced with non-empty `zones`, which is exactly
        # why the dark predicate requires `state ∈ {RUNNING,
        # PAUSED_DOCKED}` *and* seeded zones together. Post-close
        # (`state ∈ {COMPLETED, INTERRUPTED}`) fails the state check,
        # so `mowing_with_zone` is False, so the guard is armed.
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
            # HARD-19 §3 (#120): a fresh run carries no dock arrival — the
            # key is present-and-None so the stamp is a first-class field
            # (a legacy snapshot without it still falls back via `.get`).
            "dock_arrival_time": None,
        }
        self.state = STATE_RUNNING
        self._interrupt_timer_started_at = None

    def _open_provisional_run(self, time_ms: int | None) -> None:
        """HARD-18 (#117): open a provisional run at the vs=4 activation
        edge, before any type-2 has carried mowing data.

        A run is a user session and starts when the operator presses run.
        The firmware's first mowing-task type-2 lands ~3 min later (dock
        exit + navigation), so anchoring the run on it left every
        run-scoped sensor showing the previous close's values for that
        whole gap. This opens the run immediately with `start_time =`
        the type-1 activation `time`; the first accepted type-2 later
        seeds the baseline anchors (`sub0`, `mow_start_type`, `wk0`, the
        zone) and flips `provisional` off — `start_time` keeps the
        activation anchor.

        Every accumulator anchor is `None` (no honest mowing data yet).
        Consequences relied on downstream:

        - `_maybe_complete_run` cannot fire (`_is_completed` short-
          circuits on `last_mp is None`), so a provisional run never
          closes as `completed`.
        - `_close_run` on an aborted start yields the arbitrated minimal
          interrupted entry (`session_area = None`, `zones = []`,
          `mow_start_type = None`, real wander `duration_ms`).
        - `zones == []` keeps the BUG-17/19 vestige gate armed for the
          whole window (its dark predicate needs seeded zones).

        The explicit `"provisional": True` key rides through
        `snapshot()`/`restore()` via the existing `current_run` deepcopy
        — no snapshot-shape change, no `SNAPSHOT_VERSION` bump. Clearing
        `_pending_reset` here is deliberate: a fresh activation
        invalidates any pre-close reset candidate stashed in the previous
        run's epoch, which must not confirm against this window's seeding
        packet.
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
            # HARD-19 §3 (#120): structural clear — a provisional run has no
            # dock arrival until it docks. For an aborted start the dock
            # entry stamp coincides with `last_time` (§1c/§1d values agree).
            "dock_arrival_time": None,
        }
        self.state = STATE_RUNNING
        self._interrupt_timer_started_at = None
        self._pending_reset = None

    def _close_run(self) -> Event:
        """Close the currently open run. The result label is centralised
        here (BUG-09) so every close path — the fast BUG-09 completion
        criterion, a fresh reset, the sustained-60 s interruption timer,
        a resolved pending reset — labels the same way. The sustained-
        timer path on 2026-07-04 used to hardcode `interrupted` and thus
        mis-labeled a genuinely completed run whose close it caught
        after the battery finished charging.

        BUG-14 label rule: `completed` iff
        `last_mp ≥ MP_COMPLETION_THRESHOLD (100)`, OR
        `last_mp ≥ MP_PARTIAL_THRESHOLD (99) ∧ zones[-1].cmp_max ≥
        CMP_ZONE_COMPLETE_THRESHOLD (10000)`.
        """
        assert self.current_run is not None, "close_run without an open run"
        r = self.current_run
        if r.get("provisional"):
            # HARD-18 (#117): a provisional run reaching a close was an
            # aborted start — pressed, wandered, sent home without ever
            # producing an accepted type-2 (a seeding packet flips
            # `provisional` off, so only never-seeded runs are here).
            # Count it; the payload below is already the arbitrated
            # minimal history entry (`result = interrupted` because
            # `_is_completed()` is False on `last_mp is None`, `zones =
            # []`, `session_area = None`, `mow_start_type = None`, real
            # wander `duration_ms` from the type-1 `last_time`).
            self.counters["aborted_starts_committed"] += 1
        start = r.get("start_time")
        # HARD-19 (#120): a run that closed at a dock ends at the dock's
        # arrival edge, not at its last accepted type-2. `dock_arrival_time`
        # is stamped on the RUNNING → PAUSED_DOCKED transition (see
        # `process_vehicle_state`), frozen through the docked idle↔charge
        # flips and through a late completing flush (§3 clears the stamp
        # only on departure evidence), so the session duration is exactly
        # FEAT-06's activation → dock arrival (symmetric with HARD-18's
        # activation-anchored `start_time`). The end is the stamp itself —
        # strict, no `max(…, last_time)` floor (operator arbitration §1:
        # "use dock_arrival_time as end_time INSTEAD OF the last type-2
        # timestamp"; a late BUG-09 #89 completing flush is bookkeeping
        # emitted at task teardown, its `time` is emission time, not session
        # activity, so it must not move the end past the physical arrival).
        # One accepted cosmetic consequence: a zone's last packet time may
        # exceed the run's `end_time` by the flush seconds. Closes with no
        # observed dock (a reset-driven close mid-mow, a legacy / degraded
        # run without the key) carry no stamp and fall back to the last
        # packet cursor. `last_time` is deliberately NOT mutated — it stays
        # the packet cursor the post-close gating baseline (`last_sub`) and
        # everything downstream read.
        last_time = r.get("last_time")
        dock_arrival = r.get("dock_arrival_time")
        end = dock_arrival if dock_arrival is not None else last_time
        duration_ms: int | None = None
        if start is not None and end is not None:
            duration_ms = end - start
        result = RESULT_COMPLETED if self._is_completed() else RESULT_INTERRUPTED
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
        # HARD-20 (#122): a close transitions the machine to IDLE; the
        # completed/interrupted distinction lives in `result` (the record
        # below), never in resting state. Zero other diff in `_close_run`.
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
        """BUG-09 completion criterion (BUG-14 refined): close on
        `mp ≥ 100`, OR on `mp ≥ 99 ∧ zones[-1].cmp_max ≥ 10000`, with
        `vehicle_state ∈ DOCK_EVIDENCE` (`{1, 2}`) in either
        case. Immediate close with no debounce. Returns the close event,
        or `None` when neither branch fires.

        Called from `process_type2` (after accumulator update, so the
        just-accepted packet's `mp` / `cmp` is visible) and
        `process_vehicle_state` (after the vs update, so a dock arrival
        while `last_mp` / `last_cmp` was already at threshold fires the
        close even before the next type-2). Either ordering — signal-
        crosses-threshold-then-dock, or dock-arrives-then-signal-refresh
        — is handled.
        """
        if self.state not in (STATE_RUNNING, STATE_PAUSED_DOCKED):
            return None
        if self.vehicle_state not in DOCK_EVIDENCE:
            return None
        if not self._is_completed():
            return None
        return self._close_run()

    def _is_completed(self) -> bool:
        """Whether the current run has reached the BUG-14 completion
        rule. Used both by `_maybe_complete_run` (fast path) and
        `_close_run` (label derivation) so the two never disagree.
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
        if vs == VS_DOCKED_IDLE:
            if self._interrupt_timer_started_at is None:
                self._interrupt_timer_started_at = self._clock()
        else:
            # vs=2 (charging) — and vs=4/5 reaching here from a seeded
            # PAUSED_DOCKED — hold the run without a countdown (vs=3/6 are
            # inert and never reach this helper).
            self._interrupt_timer_started_at = None

    def _arm_interrupt_timer(self) -> None:
        """HARD-18 (#117): arm the interruption countdown unconditionally
        (charging included). Used only on the provisional-abort path — a
        pressed run that returned to the dock has no mowing data to hold
        for, so even a vs=2 charging dock must eventually commit the
        aborted-start entry. Idempotent: it never resets an already-armed
        timer, so a charge↔idle flip during the debounce keeps the
        countdown running.
        """
        if self._interrupt_timer_started_at is None:
            self._interrupt_timer_started_at = self._clock()

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

        An open run with ``sub₀ = None`` is a live HARD-18 (#117) shape —
        a provisional run opened at the vs=4 edge has no baseline until
        its first type-2 seeds it — restored faithfully so the next
        ``_close_run`` reports ``session_area = None`` rather than
        fabricating a value from the firmware task-scoped accumulator.
        """
        if snap.get("version") != SNAPSHOT_VERSION:
            return False
        state = snap.get("state", STATE_IDLE)
        # Robustness (NOT a migration): a malformed / unknown state of any
        # vintage maps to IDLE with one WARN, never raising. The trigger is
        # "an out-of-vocabulary string", not "a specific extinct on-disk
        # shape", so this stays — per the HARD-21 (#123) classification
        # rule. The HARD-20 raoul.27 dead-vocabulary shim (`"completed"` /
        # `"interrupted"` → IDLE) was retired here once the on-disk state
        # was verified re-persisted in the current vocabulary (2026-07-23,
        # `state = "idle"`); a legacy string now falls through to this same
        # catch-all — WARN + IDLE, still never raises.
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
        # Robustness (NOT a migration — HARD-21 #123 item 4): an absent
        # counter / drop key defaults to 0 so a partial or hand-edited
        # snapshot never raises on read. The trigger is "a key isn't
        # present", of any vintage — not "an extinct shape on disk" — so
        # this tolerant read stays regardless of the migration sweep.
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
        # `_interrupt_timer_started_at` is monotonic and cannot be
        # restored across a process restart. `tick()` re-arms it on the
        # first call after restart if the machine is `PAUSED_DOCKED`
        # under `vs = 1` (docked-idle, HARD-19 §2) — so the
        # sustained-docked interruption detector survives a restart even if
        # `vehicle_state` is restored from the snapshot rather than
        # re-derived. A restored `PAUSED_DOCKED` under `vs = 3` cannot arm
        # (vs = 3 is not a dock state) — it waits for the next real dock or
        # departure edge. A pending
        # reset is intentionally *not* persisted: worst case a mid-flight
        # candidate re-confirms one packet later after a restart, which
        # is safer than serialising a transient decision.
        self._interrupt_timer_started_at = None
        self._pending_reset = None
        return True
