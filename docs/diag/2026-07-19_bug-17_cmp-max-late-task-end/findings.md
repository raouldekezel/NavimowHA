# BUG-17 diag: `current_zone_progress` sticks at 100 % after a late task-end packet at run start

## TL;DR

Starting a mowing task from HA on a boundary whose previous task closed at `cmp = 10000` causes the firmware to emit — at the moment of the `mowing` state transition — a "task-end" replay packet with `action = -1, mp = 100, cmp = 10000, mowingWeekArea = 0.0, subtotalArea = 0.0`. The tracker accepts this packet as the run's first `type-2`, so `zones[0].cmp_max` is seeded at **10000**. Because `_update_zone` computes `cmp_max = max(cmp_max, incoming_cmp)` (monotonic), every subsequent packet on the same boundary (`cmp = 100, 215, 413, 819, …, 3111`) is silently ignored for the max. `sensor.<slug>_current_zone_progress` renders **100.0 %** for the entire run while `current_run_progress` (sourced from `last_mp`, non-monotonic) tracks correctly.

**Blast radius (widened on Session 2 close-out inspection)**: beyond the visible `current_zone_progress` symptom, the vestige packet also stamps the poisoned zone's `ZoneRecord.size_estimate_updated_ms` (FEAT-08) at the *vestige's* `time` field (85 ms before the `state → mowing` transition), and pollutes `zones[0].sub_entry` with `0.0`. On today's run those two side effects were masked by coincidence (Prunier legitimately reached 100 % during the real mow, and was the first-in-run zone so `sub_entry = 0.0` matched reality), but on a run where the operator interrupts before the poisoned boundary reaches `cmp = 10000`, or where the poisoned boundary is not the run's first zone, both `Store.last_cmp_max` and `sensor.<slug>_zone_<id>_surface` would be silently wrong at run close.

## User-visible symptoms at run start

Two dashboard gauges both snap to **100 %** on the coordinator tick immediately following the `docked → mowing` transition (218 ms after the state change on this trace):

- `sensor.<slug>_current_run_progress` = **100** (from vestige `mp = 100`).
- `sensor.<slug>_current_zone_progress` = **100.0** (from vestige `cmp = 10000`).

They diverge on the 2nd `type-2` packet (56.4 s later, at 09:32:49 UTC):

- `current_run_progress` **self-corrects to 0** (the code overwrites `last_mp` on every packet, non-monotonic — so the poisoned 100 is simply replaced by the fresh `mp = 0`). The gauge then climbs normally.
- `current_zone_progress` **stays stuck at 100.0** for the full duration of the poisoned zone segment (~1 h 50 min in this run) because `cmp_max` is written with `max(...)`. Only released when a boundary transition creates a fresh `zones.append(...)` seed (11:21:08 UTC here, transition Prunier → Figuier, sensor drops to 1.44).

The asymmetry between "overwrite" and "monotonic max" is what makes the bug half-hidden: the `mp` gauge looks buggy for one minute and then normal — easy to dismiss as a display glitch — while the `cmp` gauge stays visibly wrong for the entire zone. Both come from the same poisoned packet.

## Context

- **Date**: 2026-07-19 (Europe/[REDACTED], UTC+02:00)
- **Robot**: Segway Navimow i210 LiDAR Pro (Prunier zone, boundary `1`)
- **Fork tag installed**: `NavimowHA-v1.1.0-raoul.19` (commit `193afb1`, HACS-installed at `/config/custom_components/navimow/`, files unchanged versus the tag)
- **HA version**: 2026.1.3 (Docker)
- **DEBUG on**: `custom_components.navimow: debug`, `mower_sdk: debug`
- **Pre-experiment state**:
  - Last mow on Prunier: **2026-07-09 10:51:46 UTC**, closed at `mp = 99, cmp = 10000, sub = 231.77` (a "mini-run" that plateaued at 99, mentioned in the operator's memo of the 2026-07-09 session — see also #92 BUG-16).
  - `sensor.<slug>_last_run_result` for Prunier: `completed`; `last_cmp_max` attribute: `10000` (stored via FEAT-05c `Store`, survived HA restart).
  - Robot on dock, `state = docked`, no ongoing run in the tracker (`current_run = None`) at 09:30:00 UTC.

## Actions taken

1. `01_manual-start-mowing` — operator triggered a manual mow via HA (`lawn_mower.start_mowing` on `lawn_mower.<slug>`) at 09:31:53 UTC. The lawn-mower entity transitions `docked → mowing` on the same tick. Session 1 ran Prunier → transitioned to Figuier at 11:21:08 UTC → docked for low-battery recharge at 11:22:54 UTC. Tracker held `current_run` open in `STATE_INTERRUPTED` (`etat_de_la_tonte = paused`).
2. `02_resume-and-close` — robot auto-resumed after ~80 min of charging: undock at 12:42:51 UTC, ran Figuier to completion (`cmp = 10000, mp = 100` at 14:00:01 UTC), returned to dock at 14:00:56 UTC. Tracker fired `_maybe_complete_run` on the fast-path and closed the run in `STATE_COMPLETED`. `ingest_run` wrote the final `ZoneRecord` snapshots for Prunier and Figuier — captured for the FEAT-08 side-effect analysis.

## Timeline

All timestamps UTC. Raw payloads in `01_manual-start-mowing.mqtt.log`, sensor states in `01_manual-start-mowing.sensors.tsv`.

| UTC        | Event                                                                                                                                                                                                                                     |
| ---------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 09:31:53.128 | `lawn_mower.<slug>` transitions `docked → mowing` (`isRunning` on `/state`).                                                                                                                                                              |
| 09:31:53.213 | First `type-2` packet on `/location`: `action=-1, boundary=1, cmp=10000, mp=100, mowingWeekArea="0.0", subtotalArea="0.0"`. **This is the vestige task-end of the 2026-07-09 Prunier mini-run replayed on task-start**. Signature match: `mp=100 ∧ cmp=10000` for the boundary whose last stored run closed `completed` at `cmp_max=10000` 10 days earlier. |
| 09:31:53.218 | `sensor.<slug>_current_run_progress` = **100** (rendered from `last_mp`), `sensor.<slug>_current_zone_progress` = **100.0** (rendered from `zones[0].cmp_max/100`), `sensor.<slug>_current_zone` = `Prunier`.                              |
| 09:32:49.651 | Second `type-2`: `action=8, subAction=6, cmp=100, mp=0, mowingWeekArea="2.42", subtotalArea="2.47"`. This is the real first packet of the fresh Prunier task. `mp` drops **100 → 0** in the sensor at the next coordinator tick.            |
| 09:32:49.653 | `sensor.<slug>_current_run_progress` = **0** ✅ (`last_mp` is non-monotonic — it took the fresh value). `sensor.<slug>_current_zone_progress` = **100.0** ❌ (`cmp_max = max(10000, 100) = 10000` — sticky).                                |
| 09:33:39 – 10:04:57 | 30 further `type-2` packets accepted, `cmp` climbing 215 → 3111. `current_run_progress` steps 0 → 21 in lockstep with `mowingPercentage`. `current_zone_progress` **never budges from 100.0** — 22 updates on `current_run_progress`, 1 on `current_zone_progress` (the poisoned initial value). |
| 10:05:00 (snapshot) | Mow still running. `sensor.<slug>_current_zone_progress.last_changed = 09:31:53.218Z` (stuck 33 minutes and counting).                                                                                                                     |

### Session 1 close-out + Session 2 (from `02_resume-and-close.mqtt.log` / `.sensors.tsv`)

| UTC        | Event                                                                                                                                                                                                                                     |
| ---------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 11:21:08.383 | Boundary transition `1 → 3` (Prunier → Figuier). `_update_zone` takes the `else` branch and calls `zones.append({..., cmp_max: 144, sub_entry: sub_at_transition, ...})`. `zones[-1]` is now the fresh Figuier segment. |
| 11:21:08.543 | `sensor.<slug>_current_zone` = `Figuier`, `sensor.<slug>_current_zone_progress` = **1.44** — unblocked. The Prunier segment stays at `zones[0]` with the poisoned `cmp_max = 10000`, but it is no longer `zones[-1]` and no longer visible to the sensor. |
| 11:22:54 | `lawn_mower.<slug>` docks (battery ~52 %). Tracker holds `current_run` in `STATE_INTERRUPTED`; sensors keep their last values (`Figuier`, `mp = 65`, `cmp = 1.44 %`). |
| 12:42:51 | Auto-resume (`state → mowing`). No fresh `_open_run` — the resume goes through `_reopen_run`. |
| 12:44:26 | **First `type-2` of Session 2**: `action = 8, subAction = 6, boundary = 3, cmp = 222, mp = 65, sub = 235.65, wk = 235.5`. **No vestige packet** — accumulators are non-zero, `mp` continues from 65. This is the diagnostic evidence that the vestige is emitted only on fresh `_open_run` from `STATE_IDLE`, not on `_reopen_run`. |
| 12:44:26 – 14:00:01 | Figuier progresses monotonically `cmp = 222 → 10000`, `mp = 65 → 100` across ~50 `type-2` packets. `sensor.<slug>_current_zone_progress` steps `2.22 → 100.0` cleanly — the second segment behaves correctly because its `zones.append(...)` seed was fresh. |
| 14:00:01 | `mp = 100 ∧ cmp = 10000` — fast-path completion armed. `_maybe_complete_run` schedules the close. |
| 14:00:04 | State `mowing → returning`. |
| 14:00:56 | State `returning → docked`. Tracker enters `STATE_COMPLETED`. `ingest_run` writes `ZoneRecord` snapshots: Prunier `{last_result: completed, last_cmp_max: 10000, size_estimate_m2: 232.89, size_estimate_updated_ms: 1784453512965}`, Figuier `{last_result: completed, last_cmp_max: 10000, size_estimate_m2: 123.12, size_estimate_updated_ms: 1784454068383}`. **Prunier's `size_estimate_updated_ms` is the vestige packet's `time` field**, not the real start of the visit — see side-effect finding below. |
| 14:01+ | Sensors go `unknown`. Post-close inspection reveals `razibus_prunier_surface.last_complete_pass_at = 2026-07-19T09:31:52.965Z`, which is the vestige packet's `time` field (85 ms *before* the state transition). |

## Findings

- **The vestige packet's fingerprint is unambiguous** (`01_manual-start-mowing.mqtt.log:1`): `action = -1` (not the `action = 8, subAction = 6` of active-mow packets), `mowingWeekArea = "0.0"` **and** `subtotalArea = "0.0"` (zeroed), `mp = 100`, `cmp = 10000`. The 09:07 Prunier mini-run's closing `cmp_max = 10000` and `last_result = completed` are stored in FEAT-05c `Store` and match the boundary + `cmp` on this packet. This is the same "late task-end replay" firmware mechanism documented in #92 (BUG-16), delivered here on a boundary-change-free `IDLE → RUNNING` opening instead of a `COMPLETED → new task` transition — hence the different pathology (sticky max, not phantom run).
- **The root cause is monotonic `cmp_max`** (`custom_components/navimow/run_tracker.py:1010` in raoul.19): `z["cmp_max"] = max(z.get("cmp_max") or 0, cmp_)`. This is correct for a well-formed run — `cmp` for a boundary is monotonic within a task — but the seed value at zone-open is the incoming `cmp` of the first accepted packet on that boundary. When that first packet is the vestige, the seed is 10000 and no later packet can lower the max.
- **The sensor `current_zone_progress` reads `cmp_max`** (`custom_components/navimow/sensor.py:355` in raoul.19): `value_fn = lambda c: r["zones"][-1]["cmp_max"] / 100.0`. There is no fallback to `parsed.current_mow_progress` and no "invalidate max if stale seed" guard.
- **`current_run_progress` (`last_mp`) is unaffected** because `last_mp` is not maxed — it's overwritten by every accepted packet (`run_tracker.py:986`: `r["last_mp"] = parsed["mowing_percentage"]`). The sensor correctly steps `100 → 0 → 1 → 2 → …` (`01_manual-start-mowing.sensors.tsv`, column `progression_tonte`). This asymmetry between `mp` and `cmp` in the tracker is what makes the bug visible only on `current_zone_progress`.
- **BUG-16 (#92) fix would not have caught this pathology** as-scoped. That guard fires in `STATE_COMPLETED`/`STATE_INTERRUPTED` before `_open_run`. Here the tracker is in `STATE_IDLE` at the moment the vestige arrives (last close was 10 days ago, restored from `Store`), so the packet flows through the standard `IDLE → RUNNING` opening and pollutes the freshly created zone. A BUG-17 fix must gate `_update_zone` (or the zone-open step in `_open_run`) on the same `mp = 100 ∧ cmp = 10000` signature, or equivalently reject `action = -1 ∧ subtotalArea == 0.0` on the run's very first packet.

### Session 2 findings (resume behavior + second-order symptom)

- **The vestige does not re-fire on `_reopen_run`** (`02_resume-and-close.mqtt.log:1`, at 12:44:26 UTC). The first `type-2` after `docked → mowing` on Session 2 is a normal active-mow packet (`action = 8, subAction = 6, sub = 235.65, wk = 235.5, mp = 65, cmp = 222`) — no `sub = 0`, no `mp = 100`, no `cmp = 10000`. This validates the scoping of fix A: the payload-signature guard, gated by `not self.current_run.get("zones")`, has no false-positive path on resume traces.
- **The Figuier segment (`zones[1]`) behaves correctly** because its `zones.append(...)` was called by the boundary-transition path at 11:21:08 UTC (`_update_zone` else-branch) with the real transition packet's `cmp = 144`. `cmp_max` seed is honest, and `sensor.<slug>_current_zone_progress` steps `1.44 → 100.0` cleanly across the two sessions. The bug's blast radius is contained to `zones[0]`.
- **Second-order symptom on FEAT-08 `NavimowZoneAreaSensor`**: the `ingest_run` step at run close (14:00:56 UTC) writes `ZoneRecord.size_estimate_updated_ms` for each visited zone from the corresponding `zones[i].first_time`. For `zones[0]` in a vestige-poisoned run, `first_time = vestige_packet.time = 1784453512965 ms` = `2026-07-19T09:31:52.965Z`. Post-close inspection of `sensor.<slug>_zone_1_surface` (Prunier) confirms:
  ```
  state = 233 m²
  attributes.zone_name = "Prunier"
  attributes.area_precise = 232.89
  attributes.last_complete_pass_at = "2026-07-19T09:31:52.965Z"   ← vestige packet time, not real visit start
  ```
  The `last_complete_pass_at` timestamp is 85 ms *before* the `state → mowing` transition and 56.5 s before the first real active-mow packet on Prunier. FEAT-08's contract (per PR #104 and HARD-12 alignment) is "start of the visit"; it silently becomes "start of the vestige" here.
- **Area estimate is accidentally correct on today's run, but the mechanism is fragile**. `zones[0].sub_entry` was seeded with `0.0` from the vestige's `subtotalArea = "0.0"`. `size_estimate_m2 = sub_exit - sub_entry = 232.89 - 0.0 = 232.89 ≈ 233 m²`, which happens to match Prunier's real surface. This is correct **only because** Prunier was the first-in-run zone and the run legitimately started from `sub = 0`. On a schedule where the vestige-poisoned re-mow of an already-completed boundary happens at zones[1] or later (`sub_entry` would legitimately be non-zero), the vestige's `0.0` would produce `size_estimate_m2 ≈ full run's sub_exit` — badly overstated.
- **`Store.last_cmp_max` is also accidentally correct** on today's run because Prunier was legitimately mowed to `cmp = 10000` during the real Session 1 mow. Had the operator interrupted the run before Prunier reached 10000 (low battery early, obstacle, manual dock), `ingest_run` would still have written `last_cmp_max = 10000` from the poisoned `zones[0].cmp_max`, silently over-stating the persisted state and (via BUG-14's fast-path condition) potentially misclassifying the run's `last_result`.

## Open questions

- **Does the vestige packet always arrive on `IDLE → RUNNING`?** This diag shows one occurrence, on a boundary whose last close was `completed` with `cmp_max = 10000`. Behavior on a boundary whose last close was `interrupted` (partial `cmp_max`) is untested — the vestige may replay the partial value instead, in which case `cmp_max = max(partial, fresh)` may accidentally recover once fresh > partial. A follow-up diag should reproduce on Figuier while it holds an `interrupted` `cmp_max`. *(Session 2 answered this partially for the `_reopen_run` case — no vestige — but a fresh `_open_run` on an `interrupted` boundary is still untested.)*
- **Is there a firmware timing window between the `state = mowing` transition and the vestige packet?** Here the two happened within 85 ms (`09:31:53.128` → `09:31:53.213`). If the vestige is always the first `type-2` on the topic after the state transition and always within ≤ N ms, a time-based guard (drop `type-2` within N ms of `state → mowing` when `subtotalArea = 0.0`) is a candidate fix in addition to the payload-shape guard.
- ✅ **Are there other consumers of `cmp_max` (and `first_time`, `sub_entry`) besides `current_zone_progress`?** Answered on Session 2 close-out. Yes: (a) `_maybe_complete_run` uses `zones[-1].cmp_max >= CMP_ZONE_COMPLETE_THRESHOLD` for the mp≥99 fast-path (BUG-14 / #89) — on a poisoned first-in-run boundary this would produce a wrong `completed` classification on an early dock. (b) `ingest_run` writes `ZoneRecord.size_estimate_updated_ms` from `zones[i].first_time` — on a poisoned zone this is stamped with the vestige packet's `time` field, silently misdating `sensor.<slug>_zone_<id>_surface.last_complete_pass_at`. (c) `size_estimate_m2` uses `sub_exit - sub_entry`; on a poisoned first-in-run zone, `sub_entry = 0.0` from the vestige — correct by coincidence, wrong if the poisoned zone is not first-in-run. (d) `Store.last_cmp_max` is written from `zones[i].cmp_max` — on a poisoned first-in-run zone that gets interrupted before reaching a real `cmp = 10000`, the persisted state is silently over-stated.
- **Should the tracker treat `mowingWeekArea = 0.0 ∧ subtotalArea = 0.0` at run-start as a hard rejection signal?** In a normal run the first accepted `type-2` on the fresh task carries the accumulator values already bumped (`wk = 2.42, sub = 2.47` in this diag, second packet). A packet with both accumulators zero at run-start is almost certainly the vestige. Session 2's resume packet at 12:44:26 (accumulators `sub = 235.65, wk = 235.5`) confirms the discriminator is safe on `_reopen_run` too.
- **What is the vestige's shape on an `interrupted` re-mow?** Session 2 does not answer this because Session 2 went through `_reopen_run` (which does not carry a vestige at all). A follow-up diag should stop-mow-dock a partial Figuier, wait past the 40-min MQTT reconnect window, then manually `start_mowing` again from `STATE_IDLE` on Figuier to observe the vestige's `cmp`, `mp`, `sub`, `wk` on a boundary with a partial `Store.last_cmp_max`.

## Refs

- Fork issue: [#92 BUG-16](https://github.com/raouldekezel/NavimowHA/issues/92) — same firmware mechanism (late task-end replay), different pathology (phantom run in COMPLETED, not sticky max in RUNNING). BUG-17 is the sibling.
- Fork issue: [#89 BUG-14](https://github.com/raouldekezel/NavimowHA/issues/89) — `mp = 99 ∧ cmp_max = 10000` fast-path completion. Interacts with BUG-17 (see Open questions).
- Fork PR (upcoming): PR opening this diag will target `deploy`. A separate PR will land the fix once the guard shape is decided.
- Operator memory (2026-07-09): `navimow_late_mp100_and_cmp_persistent.md` — original documentation of the "late `mp = 100` at task-start" firmware behavior; noted the phantom-run case (→ BUG-16) but not the `cmp_max` seeding case (→ BUG-17).
- Source of truth code inspected:
  - `custom_components/navimow/run_tracker.py:1010` (raoul.19) — `cmp_max = max(...)`.
  - `custom_components/navimow/run_tracker.py:1020-1030` (raoul.19) — `zones.append(...)` seeds `cmp_max`, `first_time`, `sub_entry` from the incoming packet.
  - `custom_components/navimow/sensor.py:355` (raoul.19) — `current_zone_progress` value_fn (`zones[-1].cmp_max / 100`).
  - `custom_components/navimow/zone_registry.py` (raoul.19, FEAT-08 PR #104) — `ingest_run` writes `ZoneRecord.size_estimate_updated_ms` from `zones[i].first_time`, feeds `sensor.<slug>_zone_<id>_surface.last_complete_pass_at`.
