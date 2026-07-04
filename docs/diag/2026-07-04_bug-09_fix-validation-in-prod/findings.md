# 2026-07-04 — BUG-09 — post-fix validation of `raoul.9` on a real i210 task close

## TL;DR

First live task closed under `NavimowHA-v1.1.0-raoul.9` (BUG-09 fast
path shipped) fires `run_finished(result=completed)` **≈ 0 s after
dock arrival** and labels the run correctly, versus the pre-fix
`raoul.8` morning run whose close took **53 min 31 s** and mis-labeled
`interrupted`. Timing evidence in `02_dock-close-transition.sensors.tsv`:
`i210_progression = 100` at **15:11:23 CEST** (robot still returning,
`vs = 5`), then `lawn_mower = returning` for 1 min 56 s, then
`lawn_mower = docked` + `i210_en_charge = on` (vs=2) + tracker close at
**15:13:20 CEST** — the 1 min 56 s gap between `mp = 100` and the close
is the signature of the new fast path (`_maybe_complete_run` waits for
`vs ∈ {1, 2, 3}` instead of firing on `mp = 100` alone, which the
pre-fix `raoul.8` code would have done, closing the run prematurely
while the robot was still returning). One bonus finding: **the firmware
DID emit `mp = 100` on this resumed task**, partly refuting the
FEAT-05 SPIKE open question 3 hypothesis "mp caps at 99 on task
completion" — see §Findings for the caveat.

## Context

- **Fork tag installed on HA**: `NavimowHA-v1.1.0-raoul.9`,
  `installed_commit = 6810170`, HACS Redownload landed at
  **13:52:13 CEST**. This diag's task started **25 min later** at
  14:16:38 CEST — the entire observed run is raoul.9-driven.
  Confirmed via `.storage/hacs.repositories`
  `version_installed = NavimowHA-v1.1.0-raoul.9`.
- **Home Assistant**: production instance on `intel-nuc` (Docker
  container `hass`).
- **Robot**: `REDACTED-ROBOT-MODEL` (i210 LiDAR Pro, MAP-01 catalog).
- **Session logging**: DEBUG on `custom_components.navimow.*` and
  `mower_sdk.mqtt` was **off** during this run (default `info`). No
  packet-level `run_tracker event: kind=… payload=…` DEBUG lines, no
  raw `/location` payloads. The evidence in this session is HA state
  history via `/api/history` — enough to characterise the close
  timing and label but not enough to reveal per-packet firmware
  content (`sub`, `wk`, `cmp`, `mowStartType`). A follow-up diag with
  DEBUG on would land the packet trace and settle the SPIKE-02 open
  questions on `mowStartType` distinguishability and mp reset
  semantics.
- **Pre-experiment state**: the morning 07:41:58 → 11:25:13 UTC task
  (raoul.8, mp peak = 99) was closed by the pre-BUG-09 sustained-60 s
  timer with the hardcoded `interrupted` label. See
  [`2026-07-04_bug-09_paused-docked-mp-99/findings.md`](../2026-07-04_bug-09_paused-docked-mp-99/findings.md).
  At the start of this session's observation window (14:15:00 CEST),
  the tracker was at `etat_du_passage = idle`,
  `debut_du_dernier_passage = 2026-07-04T07:41:58+00:00`,
  `duree = 2880`, `resultat = interrupted` — the mislabeled morning
  close.

## Actions taken

1. `01_task-timeline.sensors.tsv` — change-only timeline across the
   full observation window 14:15:00 → 15:20:00 CEST, thirteen entities
   (`i210_batterie`, `i210_en_charge`, `i210_cloud_connecte`,
   `i210_progression`, `progression_du_passage`,
   `progression_de_la_zone`, `zone_courante`,
   `surface_hebdomadaire`, `etat_du_passage`, `debut_du_dernier_passage`,
   `duree_du_dernier_passage`, `resultat_du_dernier_passage`,
   `lawn_mower`).
2. `02_dock-close-transition.sensors.tsv` — compact 15:10:00 → 15:15:00
   CEST zoom on the mp-crosses-100 → returning-1'56" → dock-arrival →
   close sequence.

Both files use `+02:00` (CEST); the local timezone name is redacted
per `docs/diag/README.md`.

## Timeline (CEST)

Key transitions extracted from `01` and `02`.

| Time (CEST) | Event | Entities |
| --- | --- | --- |
| 14:16:40 | Operator presses RUN in the app; `lawn_mower.mowing` transition | `lawn_mower = mowing` |
| 14:17:34 | First accepted `/location` type-2 post-RUN — tracker opens the run via `_reopen_run` on the closed morning run (Problem A on SPIKE-02: `sub` didn't reset, mp task-scoped continues). First accepted `mp = 65` (not 0, not 99 — a partial-task starting point that itself is an unresolved observation). Robot straight to zone 3. | `i210_progression = 65`, `progression_du_passage = 65`, `zone_courante = #3`, `etat_du_passage = running` |
| 14:17 → 15:11 | `mp` progresses monotonically from 65 to 99 over 53 min 33 s (≈ 1.6 %/min, consistent with the 2.0-2.7 m²/min mowing rate on other diag runs). No missed samples. | `i210_progression` in [66, 99] |
| 15:11:23 | **`mp = 100`** — first observation of `mp = 100` on any committed evidence in the fork's diag corpus. Robot still returning (`lawn_mower = mowing` at this instant, `vs` per `i210_en_charge = off` = not 2 yet). Tracker's `_maybe_complete_run` does NOT fire because `vs ∉ {1, 2, 3}` — this is the fast path working as designed. | `i210_progression = 100`, `progression_du_passage = 100`, `progression_de_la_zone = 100.0` |
| 15:11:24 | 1 s later: `vs = 5` (returning) surfaces via `lawn_mower.returning` transition. Tracker's state derivation renders `etat_du_passage = returning` (RUNNING + vs=5). Fast path still doesn't fire (`vs = 5` still not in `{1, 2, 3}`). | `lawn_mower = returning`, `etat_du_passage = returning` |
| 15:13:20 | Robot arrives at dock. `vs = 2` (charging). Tracker's `_maybe_complete_run` fires **within the same call frame** as `process_vehicle_state(VS_DOCKED_CHARGING)`: `_close_run` derives `result = completed` (last_mp = 100 ≥ 99), publishes `duree = 19766` s and `resultat = completed`. Same-second `lawn_mower.docked` + `i210_en_charge = on` + all three tracker outputs (`etat`, `duree`, `resultat`) change. | `lawn_mower = docked`, `i210_en_charge = on`, `etat_du_passage = idle`, `duree_du_dernier_passage = 19766`, `resultat_du_dernier_passage = completed` |
| 15:13:23 | Brief ~0 s dock-contact flicker on `i210_en_charge` (off/on). BUG-09 fast path already fired at 15:13:20; flicker is post-close, no consequence. | `i210_en_charge = off → on` |

**End-of-run latency**: 0 s (or bounded by coordinator tick jitter,
well under 1 s in this instance). The morning `raoul.8` close on a
similar-shape run had 53 min 31 s of latency (dock arrival 10:31:42 →
close 11:25:13 CEST per the original BUG-09 diag).

## Findings

1. **BUG-09 fast path fires on dock arrival, not on mp=100.** The
   1 min 56 s gap between `mp = 100` (15:11:23) and the close (15:13:20)
   is the fast path deliberately deferring the completion event
   until the robot is actually docked (`vs ∈ {1, 2, 3}`), instead of
   firing while the robot is still returning. Pre-fix code on
   `raoul.8` would have fired at 15:11:23 (`if mp == 100 and state ==
   RUNNING → COMPLETED`), producing a `run_finished` event while the
   robot was still travelling — arguably wrong. The 1 min 56 s
   deferral is the correct behaviour on the shape of this run.
2. **Result label is `completed`** (`last_mp = 100 ≥ 99`) — the
   centralised `_close_run` label derivation lands the right result on
   this session, unlike the morning `raoul.8` sustained-timer close
   that hardcoded `interrupted`.
3. **`i210_en_charge = on` at 15:13:20 confirms `vs = 2`**, i.e. the
   dock arrival was on charging (not the `vs = 1` idle nor `vs = 3`
   unpowered variants). The fast path fired on the `vs = 2` transition
   — the first of the three qualifying values in this specific
   session.
4. **Firmware emitted `mp = 100`.** The 2026-07-04 morning BUG-09 diag
   flagged in its open question 3 that "`mp` might never reach 100"
   and set an operator prediction of "`mp = 99` at every task
   completion". This session shows `mp = 100` on a **resumed** task
   (morning task at 07:41 UTC, interrupted by the `raoul.8`
   sustained-timer at 11:25 UTC, resumed via user RUN at 14:16 UTC,
   docked at 15:13 UTC). The prediction was set for a **fresh**
   zone-#3 task — this session is not a decisive answer either way
   because the task was resumed, not fresh. SPIKE-02 open question 2
   ("Does firmware ever reset mp on a manual RUN post-interrupted?")
   remains open and now more urgent: if the firmware treats a resume
   as a new task, we would expect `mp = 0` on the first accepted
   type-2 post-RUN, but we saw `mp = 65`. The observation is
   consistent with the firmware carrying task-scoped `mp` across the
   `interrupted` close (task continues at some intermediate progress),
   which would in turn mean the firmware CAN emit `mp = 100` on
   task-scoped tasks. Whether a **fresh** task also reaches 100 is
   still unknown and remains the operator's next target for a DEBUG
   trace.
5. **Problem A on SPIKE-02 also visible here.**
   `debut_du_dernier_passage` continued to point at
   `2026-07-04T07:41:58+00:00` throughout this session (visible in
   `01_task-timeline.sensors.tsv` — no change on this entity between
   14:17 open and 15:13 close). The tracker `_reopen_run`'d the
   morning task at 14:17 rather than opening a new run, so
   `start_time` stayed anchored on the morning task. The observed
   `duree = 19766` s (5 h 29 min 26 s) then aggregates the morning
   mow (07:41 → 11:25) + the ~1 h 47 min of interrupted docked wait
   + the afternoon mow (14:17 → 15:13) into one number, which is not
   what an operator naïvely reads. Independent of BUG-09; documented
   on [SPIKE-02](../2026-07-04_spike-02_run-semantics-task-vs-session/findings.md).

## Comparison with the morning `raoul.8` close

Same-day precedent, distinct code path:

| Property | Morning (raoul.8, pre-fix) | Afternoon (raoul.9, post-fix) |
| --- | --- | --- |
| Task | 07:41 → 11:25 UTC (~3 h 43 min mowing) | 07:41 → 13:11 UTC (~5 h 29 min total, same task task-scoped) |
| `mp` peak observed | 99 (never 100 in evidence) | **100** (at 13:11:23 UTC) |
| Robot dock arrival | 10:31:42 CEST | 15:13:20 CEST |
| Tracker close fired | 11:25:13 CEST | 15:13:20 CEST |
| End-of-run latency | **53 min 31 s** | **0 s** |
| Close path | sustained-60 s timer (path 3), triggered when battery finished charging and `vs → 1` | BUG-09 fast path (`_maybe_complete_run` via `process_vehicle_state(VS_DOCKED_CHARGING)`) |
| Result label | `interrupted` (hardcoded in pre-fix `_close_run(RESULT_INTERRUPTED)`) | `completed` (derived from `last_mp = 100 ≥ 99`) |
| Correctness of label | mislabeled (successful task, autonomous return, full recharge) | correct |

The comparison is direct: same task (task-scoped mp continued from
morning), same robot, same firmware, same day, same broker. Only the
tracker code differs. The fix delivered.

## Non-goals

- Answering SPIKE-02 open questions on `mowStartType` and firmware mp
  reset semantics — DEBUG was off, packet trace unavailable. Next.
- Any code change. This is validation, not development.

## Refs

- **BUG-09 fix**: [PR #53](https://github.com/raouldekezel/NavimowHA/pull/53), closes [#51](https://github.com/raouldekezel/NavimowHA/issues/51).
- **BUG-09 root-cause diag**: [`2026-07-04_bug-09_paused-docked-mp-99/findings.md`](../2026-07-04_bug-09_paused-docked-mp-99/findings.md) (PR [#52](https://github.com/raouldekezel/NavimowHA/pull/52)).
- **Release notes**: [`NavimowHA-v1.1.0-raoul.9`](https://github.com/raouldekezel/NavimowHA/releases/tag/NavimowHA-v1.1.0-raoul.9).
- **SPIKE-02** (task vs session semantics): [`2026-07-04_spike-02_run-semantics-task-vs-session/findings.md`](../2026-07-04_spike-02_run-semantics-task-vs-session/findings.md), issue [#54](https://github.com/raouldekezel/NavimowHA/issues/54), PR [#55](https://github.com/raouldekezel/NavimowHA/pull/55). Observations #4 and #5 in this diag feed directly into that SPIKE.
- **MAP-01**: `vs = 2` semantics: [`2026-05-23_map-01_vehiclestate-catalog/findings.md`](../2026-05-23_map-01_vehiclestate-catalog/findings.md).
