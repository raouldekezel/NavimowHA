"""FEAT-05 step (b) — pure run/zone tracker fed by /location packets.

Design: FEAT-05 SPIKE record + Fable implementation brief on #43.

Zero HA imports so the tracker is unit-testable without spinning up the
core loop, matching the testability pattern of `location.py`. The
coordinator instantiates one tracker per device and forwards:

- `process_type2(parsed)` — for every type-2 item accepted by the
  layer-1 ordering guard (FEAT-05 step (a)).
- `process_vehicle_state(vs, time_ms)` — for every accepted type-1 item
  whose `vehicleState` differs from the previously seen value.
- `tick(now=None)` — periodically (coordinator tick cadence, ~30 s) so
  the sustained-60 s docked-idle interruption detector can fire even
  when no MQTT traffic is arriving.

Every call returns a list of `Event` records the caller can dispatch to
Home Assistant. Step (c) turns those into HA events + entity updates;
step (b) verifies them via `list[Event]` returns in the test suite.

Guard layers (Fable brief 2026-07-03 16:57 UTC):
- Layer 1 lives in the coordinator (`/location` `time` monotonicity per
  stream) and is not seen here — this module trusts its input.
- Layer 2: `mowingWeekArea` is monotonically non-decreasing across the
  ISO week. Rejection is exempted when the payload crosses an ISO
  Monday 00:00 UTC boundary (the counter resets there).
- Layer 3: for an open run, `|wk - sub - wk₀| ≤ 0.5` m², where `wk₀` is
  captured on the first packet of the run. Same ISO-Monday exemption;
  when the exemption fires wk₀ is re-anchored from the new packet.

State machine (converged, authoritative per the brief):

    IDLE ─fresh type-2─▶ RUNNING [run_started]
    RUNNING ─vs ∈ {1,2,3,6} while mp < 100─▶ PAUSED_DOCKED
    PAUSED_DOCKED ─fresh type-2, sub ≥ last─▶ RUNNING   (resume)
    RUNNING ─mp = 100─▶ COMPLETED [run_finished(completed)]
    RUNNING/PAUSED_DOCKED ─fresh reset (sub < last)─▶
        close open run INTERRUPTED [run_finished(interrupted)],
        open new run [run_started]
    PAUSED_DOCKED ─vs ∈ {1,3} sustained 60 s─▶ INTERRUPTED
        [run_finished(interrupted)]
    COMPLETED/INTERRUPTED ─fresh type-2 continuing accumulator
        (sub ≥ last)─▶ reopen same run, RUNNING [run_reopened]
    COMPLETED/INTERRUPTED ─fresh type-2 with reset─▶ open new run

vs=2 (charging) holds PAUSED_DOCKED indefinitely — a recharge pause
never times out. vs=8 (firmware-reset transient, MAP-01) is ignored.
`boundary=0` (BUG-06 sentinel) is excluded from zone accounting but
still updates run accumulators.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

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

# Seconds a PAUSED_DOCKED run must remain in DOCKED_NOT_CHARGING before
# it is declared INTERRUPTED. 60 s ≈ 30 type-1 samples at the 2 s
# cadence — ample debounce for dock-contact transients while keeping
# end-of-run reporting timely.
INTERRUPT_SUSTAIN_SECONDS = 60

# Layer-3 tolerance around the wk₀+sub invariant (Fable brief).
INVARIANT_TOLERANCE_M2 = 0.5

# Event kinds.
EVENT_RUN_STARTED = "run_started"
EVENT_RUN_FINISHED = "run_finished"
EVENT_RUN_REOPENED = "run_reopened"

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
# Helpers                                                               #
# --------------------------------------------------------------------- #


def _iso_week(time_ms: int) -> tuple[int, int]:
    """Return (ISO year, ISO week) of a firmware epoch-ms timestamp."""
    dt = datetime.fromtimestamp(time_ms / 1000, tz=timezone.utc)
    iso = dt.isocalendar()
    return (iso[0], iso[1])


def _crosses_iso_monday(prev_time_ms: int | None, curr_time_ms: int | None) -> bool:
    """True when `curr` and `prev` belong to different ISO weeks (i.e.
    something between them crossed a Monday 00:00 UTC boundary).
    """
    if prev_time_ms is None or curr_time_ms is None:
        return False
    return _iso_week(prev_time_ms) != _iso_week(curr_time_ms)


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

        # Diagnostic counters for the three guard layers (layer 1 is
        # tallied by the coordinator; kept here as `0` for shape
        # symmetry so the restore payload is uniform).
        self.drops: dict[str, int] = {"layer_2": 0, "layer_3": 0}

    # ------------------------------------------------------------- #
    # Public API                                                    #
    # ------------------------------------------------------------- #

    def process_type2(self, parsed: dict[str, Any]) -> list[Event]:
        """Feed a type-2 payload (already through the layer-1 guard).

        `parsed` must be the dict returned by
        `location.parse_location_type_2`.
        """
        events: list[Event] = []

        # Layer 2 — wk monotonicity (ISO-Monday exempt).
        if not self._passes_layer_2(parsed):
            self.drops["layer_2"] += 1
            _LOGGER.debug(
                "run_tracker: type-2 rejected by layer 2 " "(wk=%s last=%s time=%s)",
                parsed.get("area_week"),
                self._last_accepted_wk,
                parsed.get("time"),
            )
            return events

        prev_sub = self.current_run["last_sub"] if self.current_run else None
        incoming_sub = parsed.get("area_session")
        incoming_mp = parsed.get("mowing_percentage")

        # `is_reset` only meaningful when we have a prior sub AND the
        # new sub is numeric. Two-of-three-way None short-circuits into
        # "continuation".
        is_reset = (
            prev_sub is not None
            and incoming_sub is not None
            and incoming_sub < prev_sub
        )

        # State transition — determine whether we open, close, reopen,
        # or continue. Layer 3 only fires on continuations and reopens
        # (where `wk₀` is defined).
        if self.state == STATE_IDLE:
            self._open_run(parsed)
            events.append(self._event_run_started())
        elif self.state in (STATE_RUNNING, STATE_PAUSED_DOCKED):
            if is_reset:
                events.append(self._close_run(RESULT_INTERRUPTED))
                self._open_run(parsed)
                events.append(self._event_run_started())
            else:
                if not self._passes_layer_3(parsed):
                    self.drops["layer_3"] += 1
                    _LOGGER.debug(
                        "run_tracker: type-2 rejected by layer 3 "
                        "(wk=%s sub=%s wk0=%s tol=%s)",
                        parsed.get("area_week"),
                        incoming_sub,
                        self.current_run["wk0"] if self.current_run else None,
                        INVARIANT_TOLERANCE_M2,
                    )
                    return events
                # Resume from a pause when a fresh type-2 continues.
                if self.state == STATE_PAUSED_DOCKED:
                    self.state = STATE_RUNNING
                    self._interrupt_timer_started_at = None
        elif self.state in (STATE_COMPLETED, STATE_INTERRUPTED):
            if is_reset:
                self._open_run(parsed)
                events.append(self._event_run_started())
            else:
                if not self._passes_layer_3(parsed):
                    self.drops["layer_3"] += 1
                    return events
                self._reopen_run()
                events.append(self._event_run_reopened())

        # Bookkeeping that happens on every accepted packet.
        self._update_accumulators(parsed)
        self._update_zone(parsed)

        # Layer-2 acceptance stamps the wk/time cursors.
        if parsed.get("area_week") is not None:
            self._last_accepted_wk = parsed["area_week"]
        if parsed.get("time") is not None:
            self._last_accepted_time_ms = parsed["time"]

        # Terminal transition — mp reaching 100 closes the run.
        if incoming_mp == 100 and self.state == STATE_RUNNING:
            events.append(self._close_run(RESULT_COMPLETED))

        return events

    def process_vehicle_state(self, vs: int, time_ms: int) -> list[Event]:
        """React to a `vehicleState` change (type-1 packet).

        Only state entries into DOCKED_STATES from RUNNING move the
        machine (RUNNING → PAUSED_DOCKED). The reverse — resume — is
        driven by a fresh type-2 in `process_type2`, not by a vs=4/5
        signal, because type-1 briefly showing vs=4 during a dock-poke
        must not falsely "resume" the run.
        """
        events: list[Event] = []

        # Firmware-reset transient — never interpret as a real state.
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

        return events

    def tick(self, now: float | None = None) -> list[Event]:
        """Fire the sustained-docked interruption check.

        The caller is expected to invoke `tick` periodically (coordinator
        cadence ~30 s). Idempotent when there is nothing to close.
        """
        events: list[Event] = []
        now = self._clock() if now is None else now

        if (
            self.state == STATE_PAUSED_DOCKED
            and self.vehicle_state in DOCKED_NOT_CHARGING
            and self._interrupt_timer_started_at is not None
            and (now - self._interrupt_timer_started_at) >= INTERRUPT_SUSTAIN_SECONDS
        ):
            events.append(self._close_run(RESULT_INTERRUPTED))

        return events

    # ------------------------------------------------------------- #
    # Guards                                                        #
    # ------------------------------------------------------------- #

    def _passes_layer_2(self, parsed: dict[str, Any]) -> bool:
        """`mowingWeekArea` never decreases within an ISO week."""
        wk = parsed.get("area_week")
        if wk is None or self._last_accepted_wk is None:
            return True
        if wk >= self._last_accepted_wk:
            return True
        # `wk` regressed — allowed only if we crossed a Monday.
        return _crosses_iso_monday(self._last_accepted_time_ms, parsed.get("time"))

    def _passes_layer_3(self, parsed: dict[str, Any]) -> bool:
        """|wk - sub - wk₀| ≤ 0.5 m² for the currently open run."""
        if self.current_run is None:
            return True
        wk = parsed.get("area_week")
        sub = parsed.get("area_session")
        if wk is None or sub is None:
            return True
        # ISO-Monday exemption: wk itself just reset, so wk₀ has to be
        # re-anchored from this packet. Accept unconditionally on the
        # rollover packet and rebuild the anchor.
        if _crosses_iso_monday(self._last_accepted_time_ms, parsed.get("time")):
            self.current_run["wk0"] = wk - sub
            return True
        wk0 = self.current_run.get("wk0")
        if wk0 is None:
            # No anchor (opening packet came without wk/sub) — accept
            # and let a later packet establish the anchor.
            if wk is not None and sub is not None:
                self.current_run["wk0"] = wk - sub
            return True
        return abs(wk - sub - wk0) <= INVARIANT_TOLERANCE_M2

    # ------------------------------------------------------------- #
    # Run lifecycle                                                 #
    # ------------------------------------------------------------- #

    def _open_run(self, parsed: dict[str, Any]) -> None:
        wk = parsed.get("area_week")
        sub = parsed.get("area_session")
        wk0 = (wk - sub) if (wk is not None and sub is not None) else None
        self.current_run = {
            "start_time": parsed.get("time"),
            "mow_start_type": parsed.get("mow_start_type"),
            "wk0": wk0,
            "last_time": parsed.get("time"),
            "last_sub": sub,
            "last_wk": wk,
            "last_mp": parsed.get("mowing_percentage"),
            "zones": [],
        }
        self.state = STATE_RUNNING
        self._interrupt_timer_started_at = None

    def _close_run(self, result: str) -> Event:
        assert self.current_run is not None, "close_run without an open run"
        r = self.current_run
        start = r.get("start_time")
        end = r.get("last_time")
        duration_ms: int | None = None
        if start is not None and end is not None:
            duration_ms = end - start
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
                "mow_start_type": r.get("mow_start_type"),
                "zones": [dict(z) for z in r.get("zones", [])],
            },
        )

    def _reopen_run(self) -> None:
        """Move a closed run back to RUNNING without altering its
        accumulator. The subsequent `_update_accumulators` call in the
        surrounding `process_type2` frame extends it.
        """
        assert self.current_run is not None, "reopen without a prior run"
        self.state = STATE_RUNNING
        self._interrupt_timer_started_at = None

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

    def _event_run_reopened(self) -> Event:
        assert self.current_run is not None
        r = self.current_run
        return Event(
            kind=EVENT_RUN_REOPENED,
            payload={"start_time": r.get("start_time")},
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
        """
        return {
            "version": SNAPSHOT_VERSION,
            "state": self.state,
            "vehicle_state": self.vehicle_state,
            "current_run": self.current_run,
            "last_accepted_wk": self._last_accepted_wk,
            "last_accepted_time_ms": self._last_accepted_time_ms,
            "drops": dict(self.drops),
        }

    def restore(self, snap: dict[str, Any]) -> bool:
        """Load a previously-taken snapshot. Returns True on acceptance,
        False when the version doesn't match (caller decides whether to
        drop the payload or upgrade it).
        """
        if snap.get("version") != SNAPSHOT_VERSION:
            return False
        self.state = snap.get("state", STATE_IDLE)
        self.vehicle_state = snap.get("vehicle_state")
        self.current_run = snap.get("current_run")
        self._last_accepted_wk = snap.get("last_accepted_wk")
        self._last_accepted_time_ms = snap.get("last_accepted_time_ms")
        drops = snap.get("drops") or {}
        self.drops = {
            "layer_2": drops.get("layer_2", 0),
            "layer_3": drops.get("layer_3", 0),
        }
        # `_interrupt_timer_started_at` is monotonic and can't be
        # restored across a process restart. Leaving it at None means
        # the sustained-docked check re-arms from the first tick after
        # restart, which is the safe default (no false interruption).
        self._interrupt_timer_started_at = None
        return True
