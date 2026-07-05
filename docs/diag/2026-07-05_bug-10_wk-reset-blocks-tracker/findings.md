# 2026-07-05 — BUG-10 — firmware `mowingWeekArea` reset outside ISO-Monday blocks the FEAT-05 tracker

## TL;DR

The morning scheduled mow (2026-07-05, `mowStartType=0`, zone 1)
ran from 09:30 → 11:25 CEST (1 h 55 min), reached `mp = 100` at
11:24:44 CEST, and returned to dock 57 s later at 11:25:41 CEST.
**Every one of the 63+ observed type-2 packets was rejected by
`_passes_layer_2`** because `mowingWeekArea` climbed monotonically
from `91.3 m²` (09:30) to `230.72 m²` (mp=100) — all below
`self._last_accepted_wk = 1189.34 m²` inherited from yesterday's
afternoon task end (2026-07-04 15:11 CEST, `mp = 100`, `completed`).
`_crosses_iso_monday(prev, curr)` returns `False` for the whole mow
(both dates map to the same ISO week: 2026-06-29 Monday → 2026-07-05
Sunday). Consequence: the run is invisible to HA — `etat_du_passage`
stays `idle` from yesterday, `_debut/_duree/_resultat` stay pinned on
yesterday's afternoon close, no `run_finished` event fires, no history
entry lands. The BUG-09 fast path could not fire either: the tracker
is in `STATE_COMPLETED` from yesterday and `_maybe_complete_run`
short-circuits on `state ∉ {RUNNING, PAUSED_DOCKED}`. The `run` is
neither *this* run nor an aggregate — it is silence. Layer 2's
invariant "`mowingWeekArea` never decreases within an ISO week" is
factually wrong: the firmware resets `wk` on task-end (or task-start;
resumed vs fresh remains to disambiguate — see §Findings 6).

## Context

- **Fork tag installed on HA**: `NavimowHA-v1.1.0-raoul.9`.
- **Home Assistant**: production instance on `intel-nuc` (Docker
  container `hass`).
- **Robot**: `REDACTED-ROBOT-MODEL` (i210 LiDAR Pro).
- **Session logging**: DEBUG on `custom_components.navimow.*` and
  `mower_sdk.mqtt` **enabled at 10:07 CEST via `logger.set_level`**
  after the layer-2 rejection was first observed. The first ~30 min
  of the mow (09:30 → 10:00 CEST) predate the DEBUG activation and
  are only visible through HA state history + INFO-level lines
  (HTTP fallback ticks + `Started mowing` at 09:30:35 UTC).
- **Trigger**: scheduled mow (`mowStartType=0` observed on the first
  captured type-2). Operator confirmed no manual RUN — the firmware
  chose the start time from the mowing schedule.
- **Pre-experiment tracker state**: from yesterday's afternoon task
  end (2026-07-04 15:11:23 CEST, `mp=100`, `completed`, dock arrival
  15:13:20 CEST). Store persistence restored `STATE_COMPLETED` on the
  latest HA restart, along with `_last_accepted_wk=1189.34` and
  `_last_accepted_time_ms` in the same task.

## Actions taken

1. `01_mowing-start-layer-2-rejection.mqtt.log` — DEBUG log slice
   from 10:07 to 10:09 CEST (ANSI stripped, serial + MQTT client id
   redacted per `docs/diag/README.md`). Contains 2 rejected type-2
   packets and interleaved type-1 + type-3 payloads. First live
   capture of a `run_tracker: type-2 rejected by layer 2` line — the
   bug's fingerprint.
2. `02_full-run-through-dock.mqtt.log` — full DEBUG log from
   10:07 CEST (DEBUG activation) through 11:26 CEST (dock arrival + a
   few seconds after). 10 369 lines, 126 type-2 payloads (63 unique
   packets — each logged twice via the `paho-mqtt-client-…` and the
   `MainThread` MQTT-message-received handlers), 63 corresponding
   `type-2 rejected by layer 2` lines. Shows the whole rejection
   sweep 91.3 → 230.72 m² and the terminal `mp = 100` packet at
   11:24:44 CEST.
3. `03_sensor-timeline.tsv` — change-only HA state timeline across
   the run window (09:25 → 11:29 CEST, 294 rows). Confirms zero
   updates to the five FEAT-05 tracker-fed sensors
   (`etat_du_passage`, `progression_du_passage`,
   `progression_de_la_zone`, `debut_du_dernier_passage`,
   `duree_du_dernier_passage`, `resultat_du_dernier_passage`) across
   the whole mow. The coordinator-fed sensors (`i210_progression`,
   `zone_courante`, `surface_hebdomadaire`, `i210_en_charge`,
   `lawn_mower`) update normally throughout — because those don't
   pass through the tracker's guards.

## Timeline (CEST)

| Time (CEST) | Event | Evidence |
| --- | --- | --- |
| 09:30:35 | `Started mowing` INFO line + `lawn_mower = mowing` | Docker logs, `03_sensor-timeline.tsv` |
| 09:31:31 | `zone_courante = #1` — coordinator-fed sensor updates from `stats["current_zone"]`, unaffected by layer 2. Shows the first accepted type-2 by the coordinator (but tracker rejected it). | `03_sensor-timeline.tsv` |
| ~09:30 → 10:07 | Type-2 flow: coordinator processes them, tracker rejects them. DEBUG off; the pre-DEBUG rejections are inferred from the persistent `etat_du_passage = idle` state. | `03_sensor-timeline.tsv` |
| 10:07:26 | Operator enables DEBUG via `POST /api/services/logger/set_level` (custom_components.navimow.* + mower_sdk.* → debug) | Session action |
| 10:07:41 | First DEBUG-captured type-2: `wk=91.3, sub=91.3, mp=38, cmp=3809, boundary=1, mowStartType=0, action=5`. Rejected: `run_tracker: type-2 rejected by layer 2 (wk=91.3 last=1189.34 time=1783238861112)` | `01`, `02` |
| 10:08 → 11:24 | 61 more type-2 packets, `wk` climbs monotonically 93.68 → 228.41. Every single one rejected with `last=1189.34` — the cursor never advances. `mp` climbs 39 → 99 over the same window (~1 %/min, consistent with a start from 0). | `02_full-run-through-dock.mqtt.log` |
| 11:24:44 | `mp = 100` reached (`wk=230.72, sub=230.72, cmp=10000`). Rejected same reason. Coordinator-fed `i210_progression` updates to 100; tracker-fed `progression_du_passage` stays `unknown`. | `02`, `03` |
| 11:24:45 | `lawn_mower = returning` (vs=5). Under normal semantics this is where the run would enter its return-to-dock phase. | `03` |
| 11:25:41 | `lawn_mower = docked` + `i210_en_charge = on` (vs=2). BUG-09 fast path would fire here, but `_maybe_complete_run` requires `state ∈ {RUNNING, PAUSED_DOCKED}` — the tracker is still `STATE_COMPLETED` from yesterday, so no close event, no `run_finished`. | `03` |
| 11:25:43 → 44 | Normal dock-contact flicker on `en_charge` (off/on ~1 s). No consequence. | `03` |

The dock-arrival BUG-09 fast path deferral we validated yesterday
(`docs/diag/2026-07-04_bug-09_fix-validation-in-prod/`) works
mechanically the same today — `mp = 100` at 11:24:44 + `vs = 2` at
11:25:41 = 57 s later — but it never triggers, because the tracker
never entered `RUNNING` for this run to begin with.

## Findings

1. **Layer 2 rejects 100 % of packets across the whole 1 h 55 min
   mow.** 63 rejections observed, none accepted. The cursor
   `_last_accepted_wk = 1189.34` never moves. The rejection is not
   transient — it will persist until either the ISO week rolls over
   (Monday 2026-07-06 UTC) or an HA restart clears the cursor.
2. **`sub` and `wk` are exactly equal on every packet of the run**
   (91.3 = 91.3, 93.68 = 93.68, …, 230.72 = 230.72). Both counters
   were reset in lockstep by the firmware. Contrast with yesterday's
   resumed afternoon task where `wk` climbed from the morning
   cumulative while `sub` also climbed on the same offset — SPIKE-02
   findings §Findings 4 already documented the `wk − sub` invariant
   across the resume, which held.
3. **`mp` starts fresh on a scheduled fresh task.** The morning was a
   fresh scheduled mow (`mowStartType = 0`). `mp` climbs ~1 %/min
   from ~0 at 09:30 to 100 at 11:24:44 — consistent with a start
   from zero. Answers **SPIKE-02 open question 2** for the fresh
   case: the firmware DOES reset `mp` on a fresh scheduled task.
   Manual-RUN fresh case still not observed with DEBUG on.
4. **`mowStartType = 0` on all 63 packets** of this scheduled start —
   first robust data point for the `mowStartType` semantics table
   (SPIKE-02 open question 1). Manual-RUN comparison would need a
   captured `mowStartType` from a real user RUN — yesterday's data
   was in HA state history but `mowStartType` is not exposed as a
   sensor, so retroactive recovery from yesterday needs the raw
   /location log which was not captured.
5. **BUG-09 fast path is a no-op when layer 2 has silenced the
   tracker.** The `mp = 100 → returning → docked → charging`
   sequence is exactly the shape the fast path was designed for.
   `_maybe_complete_run` short-circuits on
   `state ∉ {RUNNING, PAUSED_DOCKED}`, and this tracker is
   `STATE_COMPLETED` from yesterday. So even the dock-arrival close
   we validated yesterday cannot save this run — it's structural.
6. **Reset trigger disambiguated: task-end (or dock-arrival), not
   idle-timer.** Between yesterday's close (`wk = 1189.34` at
   15:11 CEST) and today's start (`wk = 91.3` at 09:30 CEST) ~18 h
   30 min of idle time elapsed. That could equally point at a 24-h
   idle reset, a task-end reset, or a scheduled-task-start reset.
   But `wk = 91.3` at 09:30 (mow just started) is the value
   *after* ~37 min of mowing — the operator observed `zone_courante = #1`
   updated at 09:31:31, which is a full minute into the mow. So
   `wk` must have been reset **at or before the first accepted
   packet at 09:31**, not gradually reaccumulated over the idle 18 h.
   Consistent with a task-start reset. A task-end reset would give
   the same observation. A daily-idle reset is compatible too. Not
   fully disambiguated on this one capture; a same-day multi-task
   cycle would answer definitively (rare — requires an app-cancelled
   mid-day task followed by another schedule).
7. **Coordinator-fed sensors update normally throughout.**
   `i210_progression` (from `stats["mowing_percentage"]`
   unconditional), `zone_courante` (from tracker but the
   `_open_run`-independent path), `surface_hebdomadaire` (from
   `stats["mowing_week_area"]`), `i210_en_charge` (from
   `process_vehicle_state`), `lawn_mower` all track the real run.
   From the dashboard's perspective the run is *partially* visible
   — you can see the robot is mowing zone 1, at what percentage, and
   the weekly area is climbing — but the FEAT-05 "run" abstraction
   is entirely absent. The row for today does not exist in
   history / `debut_du_dernier_passage`, and no `run_finished`
   event was ever emitted for today.
8. **Signature line for regression testing.**
   `run_tracker: type-2 rejected by layer 2 (wk=91.3 last=1189.34
   time=…)` at `custom_components.navimow.run_tracker` — a future
   BUG-10 fix should include a unit test that feeds today's
   first-few payloads into a fresh `RunTracker` primed with a
   1189.34 cursor and asserts the fix path (whichever we pick)
   accepts them.

## Fix directions to think about (deferred to a design pass)

None being proposed in this diag. Options being weighed:

1. **Loosen the layer-2 invariant** to accept a `wk` regression when
   the incoming packet exhibits a fresh-task shape:
   `sub < RESET_SUB_CEILING (10 m²)` OR `sub ≈ wk` (both counters
   resetting together strongly suggests a task-end reset).
2. **Anchor `_last_accepted_wk` on `_open_run`** — reset the cursor
   whenever the tracker opens a new run. Chicken-and-egg with the
   current design: opening a new run requires an accepted packet.
   Needs a two-pass or an alternative reset trigger.
3. **Fresh-task detection via `vs` transition** (SPIKE-02 Option 1):
   arm `_pending_new_run` on `vs → 4/5` from `COMPLETED`/`INTERRUPTED`,
   let it re-seat the layer-2 cursor when the next type-2 arrives.
   Would solve BUG-10 and SPIKE-02 Problem A together.
4. **Detect `mowStartType` change** at type-2 acceptance boundary.
   Ties the tracker to firmware idiom; brittle if firmware changes.

Option 3 is currently the most attractive (composes with SPIKE-02).
Blocked on a design pass, which is what SPIKE-02 was for.

## Open questions (residual)

1. **What is `mowStartType` when the operator presses RUN via the
   app?** SPIKE-02 open question 1. Requires a separate manual-RUN
   capture with DEBUG on — this diag captures scheduled only.
2. **Is the `wk` reset triggered by task-end, task-start, or a
   scheduled-start-only hook?** Finding 6 above narrows this to
   "reset happens at or before the fresh task's first packet" but
   does not fully separate task-end from schedule-tick. A same-day
   multi-task cycle would answer.
3. **Would the fix's `sub ≈ wk` fresh-task detector overshoot on a
   legitimate `wk` regression that is not a fresh task?** No such
   scenario has been observed on any of the six mowing runs across
   this fork's diag corpus, but the guard should still be tested
   explicitly.

## Refs

- Issue: BUG-10 ([#58](https://github.com/raouldekezel/NavimowHA/issues/58)).
- Precedent — layer-2 introduction: FEAT-05 (b) PR [#49](https://github.com/raouldekezel/NavimowHA/pull/49).
- SPIKE-02 (task vs session semantics): [#54](https://github.com/raouldekezel/NavimowHA/issues/54), PR [#55](https://github.com/raouldekezel/NavimowHA/pull/55). Option 1 there is a candidate fix for this bug too.
- BUG-09 root-cause diag: `docs/diag/2026-07-04_bug-09_paused-docked-mp-99/findings.md`.
- BUG-09 fix-validation diag: `docs/diag/2026-07-04_bug-09_fix-validation-in-prod/findings.md` — established that yesterday afternoon's task-end wk value (1189.34) is where `_last_accepted_wk` was persisted.
- MAP-01: `vehicleState` catalog (`vs=2` semantics).
