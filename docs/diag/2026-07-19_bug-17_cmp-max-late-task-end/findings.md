# BUG-17 diag: `current_zone_progress` sticks at 100 % after a late task-end packet at run start

## TL;DR

Starting a mowing task from HA on a boundary whose previous task closed at `cmp = 10000` causes the firmware to emit — at the moment of the `mowing` state transition — a "task-end" replay packet with `action = -1, mp = 100, cmp = 10000, mowingWeekArea = 0.0, subtotalArea = 0.0`. The tracker accepts this packet as the run's first `type-2`, so `zones[0].cmp_max` is seeded at **10000**. Because `_update_zone` computes `cmp_max = max(cmp_max, incoming_cmp)` (monotonic), every subsequent packet on the same boundary (`cmp = 100, 215, 413, 819, …, 3111`) is silently ignored for the max. `sensor.<slug>_current_zone_progress` renders **100.0 %** for the entire run while `current_run_progress` (sourced from `last_mp`, non-monotonic) tracks correctly.

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

1. `01_manual-start-mowing` — operator triggered a manual mow via HA (`lawn_mower.start_mowing` on `lawn_mower.<slug>`) at 09:31:53 UTC. The lawn-mower entity transitions `docked → mowing` on the same tick. Left the mow running; snapshot at 10:05:00 UTC (33 minutes in, `progression_de_la_tonte = 21`, `progression_de_la_zone` still `100.0`).

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

## Findings

- **The vestige packet's fingerprint is unambiguous** (`01_manual-start-mowing.mqtt.log:1`): `action = -1` (not the `action = 8, subAction = 6` of active-mow packets), `mowingWeekArea = "0.0"` **and** `subtotalArea = "0.0"` (zeroed), `mp = 100`, `cmp = 10000`. The 09:07 Prunier mini-run's closing `cmp_max = 10000` and `last_result = completed` are stored in FEAT-05c `Store` and match the boundary + `cmp` on this packet. This is the same "late task-end replay" firmware mechanism documented in #92 (BUG-16), delivered here on a boundary-change-free `IDLE → RUNNING` opening instead of a `COMPLETED → new task` transition — hence the different pathology (sticky max, not phantom run).
- **The root cause is monotonic `cmp_max`** (`custom_components/navimow/run_tracker.py:1010` in raoul.19): `z["cmp_max"] = max(z.get("cmp_max") or 0, cmp_)`. This is correct for a well-formed run — `cmp` for a boundary is monotonic within a task — but the seed value at zone-open is the incoming `cmp` of the first accepted packet on that boundary. When that first packet is the vestige, the seed is 10000 and no later packet can lower the max.
- **The sensor `current_zone_progress` reads `cmp_max`** (`custom_components/navimow/sensor.py:355` in raoul.19): `value_fn = lambda c: r["zones"][-1]["cmp_max"] / 100.0`. There is no fallback to `parsed.current_mow_progress` and no "invalidate max if stale seed" guard.
- **`current_run_progress` (`last_mp`) is unaffected** because `last_mp` is not maxed — it's overwritten by every accepted packet (`run_tracker.py:986`: `r["last_mp"] = parsed["mowing_percentage"]`). The sensor correctly steps `100 → 0 → 1 → 2 → …` (`01_manual-start-mowing.sensors.tsv`, column `progression_tonte`). This asymmetry between `mp` and `cmp` in the tracker is what makes the bug visible only on `current_zone_progress`.
- **BUG-16 (#92) fix would not have caught this pathology** as-scoped. That guard fires in `STATE_COMPLETED`/`STATE_INTERRUPTED` before `_open_run`. Here the tracker is in `STATE_IDLE` at the moment the vestige arrives (last close was 10 days ago, restored from `Store`), so the packet flows through the standard `IDLE → RUNNING` opening and pollutes the freshly created zone. A BUG-17 fix must gate `_update_zone` (or the zone-open step in `_open_run`) on the same `mp = 100 ∧ cmp = 10000` signature, or equivalently reject `action = -1 ∧ subtotalArea == 0.0` on the run's very first packet.

## Open questions

- **Does the vestige packet always arrive on `IDLE → RUNNING`?** This diag shows one occurrence, on a boundary whose last close was `completed` with `cmp_max = 10000`. Behavior on a boundary whose last close was `interrupted` (partial `cmp_max`) is untested — the vestige may replay the partial value instead, in which case `cmp_max = max(partial, fresh)` may accidentally recover once fresh > partial. A follow-up diag should reproduce on Figuier while it holds an `interrupted` `cmp_max`.
- **Is there a firmware timing window between the `state = mowing` transition and the vestige packet?** Here the two happened within 85 ms (`09:31:53.128` → `09:31:53.213`). If the vestige is always the first `type-2` on the topic after the state transition and always within ≤ N ms, a time-based guard (drop `type-2` within N ms of `state → mowing` when `subtotalArea = 0.0`) is a candidate fix in addition to the payload-shape guard.
- **Are there any other consumers of `cmp_max` besides `current_zone_progress`?** `_maybe_complete_run` uses `zones[-1].cmp_max >= CMP_ZONE_COMPLETE_THRESHOLD` (10000) as the mp=99 fast-path completion condition (BUG-14). On a run poisoned by a `cmp_max = 10000` seed, `mp = 99` + dock arrival would fire the fast-path close after 1-2 packets even though the zone was **not** actually mowed to 100 %. This is a second-order symptom to verify in a follow-up: is the poisoned run misclassified as `completed` on early low-battery dock returns?
- **Should the tracker treat `mowingWeekArea = 0.0 ∧ subtotalArea = 0.0` at run-start as a hard rejection signal?** In a normal run the first accepted `type-2` on the fresh task carries the accumulator values already bumped (`wk = 2.42, sub = 2.47` in this diag, second packet). A packet with both accumulators zero at run-start is almost certainly the vestige.

## Refs

- Fork issue: [#92 BUG-16](https://github.com/raouldekezel/NavimowHA/issues/92) — same firmware mechanism (late task-end replay), different pathology (phantom run in COMPLETED, not sticky max in RUNNING). BUG-17 is the sibling.
- Fork issue: [#89 BUG-14](https://github.com/raouldekezel/NavimowHA/issues/89) — `mp = 99 ∧ cmp_max = 10000` fast-path completion. Interacts with BUG-17 (see Open questions).
- Fork PR (upcoming): PR opening this diag will target `deploy`. A separate PR will land the fix once the guard shape is decided.
- Operator memory (2026-07-09): `navimow_late_mp100_and_cmp_persistent.md` — original documentation of the "late `mp = 100` at task-start" firmware behavior; noted the phantom-run case (→ BUG-16) but not the `cmp_max` seeding case (→ BUG-17).
- Source of truth code inspected:
  - `custom_components/navimow/run_tracker.py:1010` (raoul.19) — `cmp_max = max(...)`.
  - `custom_components/navimow/sensor.py:355` (raoul.19) — `current_zone_progress` value_fn.
