# 2026-07-05 — BUG-10 — firmware `mowingWeekArea` reset outside ISO-Monday blocks the FEAT-05 tracker

> **CAPTURE IN PROGRESS.** This artefact is being seeded during the
> morning scheduled mow (started 09:30 CEST on 2026-07-05). The
> current log covers 09:30 → 10:09 CEST only. Additional artefacts
> (dock-arrival window, full type-2 sequence, sensor timeline) will be
> appended once the mow completes and the DEBUG log is fully harvested.

## TL;DR (provisional)

The morning scheduled mow (2026-07-05, `mowStartType=0`, zone 1)
started producing type-2 payloads at `mowingWeekArea = 91.3 m²`.
`self._last_accepted_wk` in the tracker was `1189.34 m²` from
yesterday's afternoon task end (2026-07-04 15:11 CEST, `mp=100`, task
`completed`). The tracker rejected every incoming type-2 through
`_passes_layer_2` because `91.3 < 1189.34` and
`_crosses_iso_monday(prev, curr)` returned `False` (both timestamps
map to the same ISO week — 2026-06-29 Monday → 2026-07-05 Sunday).
Consequence: the run is invisible to HA — `etat_du_passage` still
reads `idle` from yesterday, all FEAT-05 sensors frozen. Layer 2's
invariant "`mowingWeekArea` never decreases within an ISO week" is
factually wrong: the firmware resets `wk` at some other cadence
(hypothesis: at task-end / task-start).

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
   packets and interleaved type-1 (postureX/Y/vs) + type-3
   (heartbeat) payloads. First live capture of a
   `run_tracker: type-2 rejected by layer 2` line — this bug's
   fingerprint.
2. **(pending)** `02_full-run.mqtt.log` — full DEBUG log slice from
   09:30 CEST through dock arrival. Will show whether every type-2
   through the whole mow gets rejected (expected yes, since
   `_last_accepted_wk` never advances) and will capture the sequence
   around dock arrival to confirm whether the vs → 2 transition
   surfaces on HA at all (it should, from `process_vehicle_state`).
3. **(pending)** `03_sensor-timeline.tsv` — change-only HA state
   timeline across the run window, mirror of the BUG-09 diag's
   format. Expected: `razibus_*` sensors frozen throughout;
   `lawn_mower` / `i210_progression` / `zone_courante` /
   `i210_en_charge` update normally (they don't go through the
   tracker).

## Timeline so far (CEST)

| Time (CEST) | Event | Evidence |
| --- | --- | --- |
| 09:30:35 | `Started mowing` INFO line, `lawn_mower = mowing` transition | Docker logs |
| ~09:30 → 10:07 | Type-2 packets flow to the tracker, all rejected. DEBUG off so per-packet trace unavailable; HA state history confirms `etat_du_passage`, `progression_du_passage`, `debut_du_dernier_passage`, `duree_du_dernier_passage`, `resultat_du_dernier_passage` all keep yesterday's `last_changed` timestamps (2026-07-04). | HA state API |
| 10:07:26 | Operator (via API) enables DEBUG on `custom_components.navimow.*` + `mower_sdk.mqtt` via `logger.set_level` | Session action |
| 10:07:41 | First captured type-2: `wk=91.3, sub=91.3, mp=38, cmp=3809, boundary=1, mowStartType=0`. Rejected: `run_tracker: type-2 rejected by layer 2 (wk=91.3 last=1189.34 time=1783238861112)` | `01_mowing-start-layer-2-rejection.mqtt.log` |
| 10:08:41 | Second captured type-2 60 s later: `wk=93.68, sub=93.68, mp=39, cmp=3910`. Rejected same reason: `wk=93.68 last=1189.34`. | same |
| … | Run continues; capture accumulates | pending |

## Findings so far

1. **Layer 2 rejects 100 % of packets for this run.** Both captured
   type-2 packets show identical rejection with `last=1189.34` — the
   cursor never advances (that would require an accepted packet).
   The rejection is stable, not transient.
2. **`sub` and `wk` are equal on this run's early packets** (91.3 = 91.3, 93.68 = 93.68). Both counters were reset in lockstep by the firmware. Contrast with yesterday's resumed task where `wk` continued from the morning task's cumulative value while `sub` also continued.
3. **`mp` behaves fresh, not resumed.** The morning task was
   scheduled (`mowStartType=0`), independent of yesterday's manual
   RUN. `mp = 38` at 10:07 with mowing started at 09:30 = ~1.06 %/min
   — consistent with a start from 0. This confirms **SPIKE-02 open
   question 2** partially: the firmware DOES reset `mp` on a
   fresh (scheduled) task, but did NOT on yesterday's resumed one.
4. **`mowStartType = 0` on a scheduled start** — first data point
   for the `mowStartType` semantics table. Manual RUN and cold-boot
   comparisons still needed to fully answer SPIKE-02 open question 1.
5. **Signature line `run_tracker: type-2 rejected by layer 2` is
   loud enough to grep** — a future BUG-10 fix should include a
   regression test that feeds these very payloads.

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

## Open questions

1. **Does layer 2 also reject the dock-arrival packets** (the
   transition `vs → 2`)? The answer will land in artefact 02.
2. **Does `wk` in this morning's late packets ever climb back above
   1189.34?** If mowing today accumulates > 1189.34 m² we'd
   eventually pass the invariant and start accepting. Unlikely for a
   single-day mow (yesterday's cumulative was 1189.34 across a full
   task including morning + interrupt + afternoon session).
3. **What is `mowStartType` when the operator presses RUN via the
   app?** SPIKE-02 open question 1. Requires a separate manual-RUN
   capture — this diag captures scheduled only.
4. **Is the `wk` reset tied to task-end, task-start, day-boundary
   (local or UTC), or some longer idle timer?** Requires
   multi-morning captures to disambiguate.

## Refs

- Issue: BUG-10 ([#58](https://github.com/raouldekezel/NavimowHA/issues/58)).
- Precedent — layer-2 introduction: FEAT-05 (b) PR [#49](https://github.com/raouldekezel/NavimowHA/pull/49).
- SPIKE-02 (task vs session semantics): [#54](https://github.com/raouldekezel/NavimowHA/issues/54), PR [#55](https://github.com/raouldekezel/NavimowHA/pull/55). Option 1 there is a candidate fix for this bug too.
- BUG-09 root-cause diag: `docs/diag/2026-07-04_bug-09_paused-docked-mp-99/findings.md`.
- BUG-09 fix-validation diag: `docs/diag/2026-07-04_bug-09_fix-validation-in-prod/findings.md` — established that yesterday afternoon's task-end wk value (1189.34) is where `_last_accepted_wk` was persisted.
- MAP-01: `vehicleState` catalog (`vs=2` semantics).
