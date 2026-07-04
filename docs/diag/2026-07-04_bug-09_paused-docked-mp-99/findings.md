# 2026-07-04 — BUG-09 — run parked in `paused_docked` because `mp` peaked at 99 and the dock landed on `vs=2`

## TL;DR

On a real 2026-07-04 run (`REDACTED-ROBOT-MODEL`, scheduler-driven per the operator — `mowStartType` was not captured, DEBUG off; see Open questions — single-zone #1), `mowingPercentage` peaked at **99** and stopped advancing; `currentMowProgress` reached **10000** (zone complete); the robot returned to dock at 10:31:42 CEST on `vs=2` (charging). During the charge no exit path could fire — `mp=100` (path 1) never occurred, no fresh reset (path 2) opened a new run, and `vs=2` blocks the sustained-`vs ∈ {1, 3}` timer (path 3) — so the tracker sat in `PAUSED_DOCKED` for the full 52 min charge. Once the firmware left `vs=2` (11:23:44), path 3 fired and the run closed at **11:25:13**, with a retroactively **correct duration** (2880 s, to the second) but the **wrong result label** (`interrupted` on a successful run — the non-`mp=100` close paths hardcode it) and a **53 min 31 s** end-of-run reporting latency. BUG-09's real defect is the missing completion label plus the latency, **not a deadlock** — see §"Post-charge close".

## Context

- **Fork tag**: `NavimowHA-v1.1.0-raoul.8` (installed `docker exec hass cat /config/custom_components/navimow/manifest.json` reports `version=1.1.0`; `HACS → update.navimow_update_2 → installed_version = NavimowHA-v1.1.0-raoul.8`).
- **Home Assistant**: production instance on `intel-nuc` (Docker container `hass`).
- **Robot**: `REDACTED-ROBOT-MODEL` (i210 LiDAR Pro, MAP-01 catalog).
- **Session logging**: DEBUG on `custom_components.navimow.*` and `mower_sdk.mqtt` was **off** during this run (default `info`). Raw `/location` payloads and `run_tracker event: …` lines are therefore not in `docker logs hass` for today. The evidence in this session is HA state history via `/api/history` — a change-only timeline of the relevant entities, which is sufficient to characterise BUG-09 but does **not** carry firmware `time` / `wk` / `sub` values per accepted packet. A follow-up diag would enable the DEBUG logger before the next run and land the packet trace.
- **Pre-experiment state**: yesterday's raoul.7 build was in place until ~09:40 CEST when the operator upgraded to raoul.8 via HACS. The upgrade landed mid-way through a first mowing session (see timeline below) — the tracker started from a cold `IDLE` at 09:40:26 while the robot was already mowing; the first accepted type-2 opened the run at `mp=59`.

## Actions taken

1. `01_run-day-timeline.sensors.tsv` — full change-only timeline of nine relevant entities from 09:00 to 11:00 CEST, exported from `GET /api/history/period/…` and deduped per entity.
2. `02_dock-transition.sensors.tsv` — compact 10:20-10:35 window around the mp-peaks-at-99 → dock-arrives-on-`vs=2` transition.
3. `03_charge-complete-tracker-close.sensors.tsv` — 11:00-11:35 window covering the end of charging, the `vs=2 → vs=1` transition when the battery filled, and the tracker's eventual sustained-`vs ∈ {1, 3}` interruption 89 s later.

All three files use `+02:00` (CEST); the local timezone name is redacted per `docs/diag/README.md`.

## Timeline (CEST)

Key transitions extracted from `02_dock-transition.sensors.tsv`. Fable's implementation brief spec + PR #49 semantics for context.

| Time (CEST) | Event | Entities |
| --- | --- | --- |
| 09:30:12 | Physical run begins (pre-upgrade; the tracker only sees it from 09:41:58 — see §"Mid-run install side effect") | `lawn_mower = mowing` |
| 09:40:26 | Operator upgrades to raoul.8; integration reloads, tracker cold-boots to `IDLE` | tracker-fed sensors → `unknown` |
| 09:41:58 | First accepted type-2 after upgrade — tracker opens the run mid-mow | `progression_du_passage = 59`, `progression_de_la_zone = 59.11`, `etat_du_passage = running` |
| 10:28:06 | `mp = 99`, `cmp ≈ 9906` (`progression_de_la_zone = 99.06`) | `progression_du_passage = 99` |
| 10:29:58 | Zone complete: `cmp = 10000`, `mp` **stops** at 99 | `progression_de_la_zone = 100.0` |
| 10:29:59 | Robot begins return: `vehicleState = 5` | `etat_du_passage = returning`, `lawn_mower = returning` |
| 10:31:42 | Robot arrives at dock, first `vs=2` push | `lawn_mower = docked`, `en_charge = on`, `etat_du_passage = paused` |
| 10:31:45 | ~1 s dock-contact flicker: `en_charge = off` (i.e. `vs ≠ 2`) — too brief to arm the sustained timer even if `vs ∈ {1, 3}` | `en_charge = off` |
| 10:31:46 | `vs = 2` again; back to `PAUSED_DOCKED` under charging | `en_charge = on` |
| 10:33 → 11:15 | Battery climbs 59 → 100 monotonically | `batterie` monotonic; no other transitions on tracker sensors |
| **11:15:59** | Battery reaches **100 %** — full-charge threshold | `batterie = 100` |
| **11:23:44** | `en_charge = off` — first `vs ≠ 2` since dock (**52 min 02 s** after dock arrival). Firmware has transitioned to `vs=1` (docked, full, no charge) as documented in MAP-01. | `en_charge = off` |
| **11:25:13** | Tracker fires `run_finished(interrupted)` **89 s** after `vs=1` was first observed — matches `INTERRUPT_SUSTAIN_SECONDS = 60` + one coordinator tick (~30 s) exactly. | `etat_du_passage = idle`, `last_run_duration = 2880`, `last_run_result = interrupted` |

At diagnosis time (~50 min after dock, still charging), the live snapshot was:

```
sensor.<slug>_progression                = 99            (FEAT-02 old)
sensor.<slug>_run_progress               = 99            (tracker.current_run.last_mp)
sensor.<slug>_zone_progress              = 100.0         (tracker.current_run.zones[-1].cmp_max/100)
sensor.<slug>_run_state                  = paused        (tracker.state == PAUSED_DOCKED)
sensor.<slug>_last_run_started           = 2026-07-04T07:41:58+00:00
sensor.<slug>_last_run_duration          = unknown       ← expected ~50 min
sensor.<slug>_last_run_result            = unknown       ← expected completed
lawn_mower.<slug>                        = docked
binary_sensor.<slug>_en_charge           = on            (vs=2)
sensor.<slug>_batterie                   = 78
```

The operator's custom `sensor.razibus_historique` — reading via HA recorder from the FEAT-02 `sensor.<slug>_zone_courante` state changes, driven by a `docked ← *` transition automation — *did* record the session as `04/07 09:30`. So end-of-run is detectable by an out-of-tracker heuristic; the tracker just doesn't detect it.

## Post-charge close — the state machine works but labels the result wrong

Once the battery reached 100 % at 11:15:59 CEST the firmware transitioned out of `vs=2` (charging complete → `vs=1` docked idle full). This was the *only* moment in the whole 3 h 43 min episode where the sustained-`vs ∈ {1, 3}` path 3 could arm, and it did:

- `11:23:44` — `en_charge = off` (`vs=1`).
- `11:25:13` — tracker close: `etat_du_passage = idle`, `last_run_duration = 2880` s (= 48 min), `last_run_result = interrupted`.

The 89 s gap (60 s sustained + one coordinator tick) matches the design exactly, so **`INTERRUPT_SUSTAIN_SECONDS = 60` behaves correctly** — the mechanism is not broken, its precondition is just unreachable during charging.

Two consequences worth calling out for the fix design:

1. **Total end-of-run latency, dock arrival to tracker close, was 53 min 31 s** (10:31:42 → 11:25:13). At 1-2 runs per day this is not a UX disaster, but it means every successful run's HA event lands roughly an hour after the fact; automations keyed on `navimow_run_finished` will see the event roughly when the operator has moved on. Fix A (`mp ≥ ceiling + cmp = 10000 + docked → COMPLETED`) would compress this to a few seconds; Fix B (`mowingWeekArea` stagnation N minutes) would compress it to N minutes.
2. **The result label is `interrupted`, but this was a successful run** — the zone reached 100 %, the robot returned autonomously (not sent back by the app), and the battery recharged in full. Fix design should not just close the run at the right time, it should also close it with `completed` when the shape says so (`mp ≥ ceiling` and `cmp_max = 10000`) and reserve `interrupted` for the genuine "abandoned mid-run" case (`mp < ceiling` at close time). The current close-via-sustained-timer path emits `interrupted` unconditionally.

Fixing (1) automatically fixes (2) if the fast path (Fix A) picks up the completion at zone finish rather than at charge finish. If only Fix B lands, it needs a result-label branch (`completed` when `mp ≥ ceiling`, `interrupted` otherwise).

## Mid-run install side effect — start time and duration are wrong

Independent of BUG-09 itself, this session surfaces a separate quirk worth calling out. The HACS upgrade to raoul.8 landed at 09:40:26 CEST while the robot was already mowing (the run started at **09:30:12 CEST**, `lawn_mower = mowing` transition in `01_run-day-timeline.sensors.tsv`). The tracker cold-booted to `IDLE`, saw the first accepted type-2 packet at 09:41:58 CEST (with `mp = 59`), and opened the run from there. Consequences:

- `sensor.<slug>_last_run_started = 2026-07-04T07:41:58+00:00` — **11 min 46 s later** than the real start.
- `sensor.<slug>_last_run_duration = 2880` s (48 min) — the real elapsed mowing time was roughly **60 min** (09:30:12 → 10:29:58 zone-complete), so about **12 min short**.
- `sensor.<slug>_last_run_result = interrupted` (per §"Post-charge close" above — orthogonal issue).

By design this can only happen on a *first* install (no prior tracker Store payload to restore) or on an integration reload triggered while a `raoul.<N>` build without persistence is in place. The FEAT-05 (c) Store persistence (PR #50) *is* meant to prevent this on subsequent restarts — the snapshot carries `current_run.start_time` across process boundaries. Verified separately in `test_restore_mid_run_continues_same_run` in `test_feat_05c_entities_events_store.py`.

**Not proposing a fix here** — this is the honest floor of the mid-install experience, and Store persistence catches every subsequent restart. The alternative (retroactively backdate the run start using the FEAT-02 `lawn_mower.mowing` transition timestamp on cold boot) would tangle the tracker with recorder history and is out of scope for BUG-09. Worth documenting in the release notes for future raoul.<N> upgrades so operators aren't surprised.

## Findings

1. **`mp` peaked at 99, never 100** — Fable's SPIKE open question #3 answered negatively. On this run, and consistently over the ~40 min visible in the timeline, the last observed `mp` was 99 (row `10:28:06\ti210_progression\t99` in `01_run-day-timeline`) and it did not budge before the return. **Verified against the FEAT-02 `sensor.<slug>_progression` history**: 0 records with state `100` in the 09:00-11:00 window; distinct observed values are 48, 49, …, 99 (peak 99). Because `sensor.<slug>_progression` reads `coordinator.stats["mowing_percentage"]` *unconditionally* after layer-1 acceptance (see `custom_components/navimow/coordinator.py::_handle_location_stats` and the FEAT-02 sensor description), an `mp = 100` packet silently rejected by tracker layer 2 or 3 would still have shown up here. It didn't. The firmware genuinely never emitted `mp = 100` on this run.
2. **Zone complete = `cmp = 10000` — `mp = 99` at that same moment** (both entities update within 2 seconds of each other, 10:29:58 vs. 10:28:06). So `zone_progress` reaching 100.0 *is* a reliable "zone is done" signal on this run; `mp` reaching 100 is not.
3. **Dock arrival lands directly on `vs=2`**. There is a ~1 s window at 10:31:45 where `en_charge = off` (so `vs ≠ 2`), followed immediately by `en_charge = on` at 10:31:46. The sustained-60 s timer wouldn't have fired even without our design — 1 s is well below the debounce.
4. **Battery evidence rules out "mid-run recharge pause"** — the battery climbed monotonically from 58 % at dock arrival (10:31:29) to 82 % at 10:57:59 — end of the `01` window; the diagnosis snapshot (78 %) was taken at ~10:54 *within* that climb — and on to 100 % at 11:15:59 (`03` file). The robot never left the dock: this is a *terminal* dock, not a recharge pause.
5. **Path 1 (`mp=100 → COMPLETED`) unreachable on this class of run**. Path 2 (fresh reset) needs another run to start. Path 3 (`vs ∈ {1, 3}` sustained 60 s) is design-blocked by `vs=2` *during charging*; it fires correctly once charging completes (see post-charge section below), but with a 53-minute latency and the wrong result label (`interrupted` on a completed run).
6. **The old FEAT-02 sensors are not affected in a way the operator would notice** — `sensor.<slug>_progression` shows `99` (the true last MQTT value) and `sensor.<slug>_zone_courante` shows `#1` (the true last boundary). The old system's end-of-run detection is done by the operator's custom automation on the `lawn_mower.docked` transition, which is fine; it just doesn't populate the new `last_run_*` family.

## Open questions

1. **What is `mp`'s scale, and does it ever reach 100?** *Corrected record*: an earlier draft of this section claimed the 2026-05-25 multizone run "ended at `mp = 100` on the last committed packet". That claim was checked against the raw log (`2026-05-25_feat-02_multizone-run/01_multizone-run-type-2-payloads.mqtt.log`) and is **false**: the committed log peaks at **`mp = 68`**, with zone 3 barely started (`cmp = 1312`) on its final packet — the FEAT-05 (b) replay test (`test_replay_2026_05_25_two_runs`) independently asserts that run is still RUNNING at end-of-log. **`mp = 100` has never been observed in any committed evidence.** The operator's task model (established in the 2026-07-04 review session) replaces the earlier map-completion hypothesis: **`mp` is task-scoped and persists across interruptions** — the 2026-07-03 controlled run took `mp` 0 → 30 on zone #1 and was manually docked; the scheduler *continued* the same task on 2026-07-04 (the run's first packet, 46 s in at 09:30:58, already carried `mp = 48`; ~1 h to finish where a fresh zone-#1 task takes ~1 h 30). The 30 → 48 inter-session gap is consistent with an autonomous post-charge resume later on 2026-07-03 (verifiable in the app history). **Operator prediction: `mp = 99` at every normal task completion.** Test: the next scheduled run is a fresh zone-#3 task — expect `mp` 0 → 99, with DEBUG capturing the packet trace.
2. **Would a `mowingWeekArea` stagnation heuristic misfire on the observed dock-contact flicker?** The 1 s `en_charge = off` window is a transient at dock-touchdown, not a genuine vs change. A stagnation-based end-of-run detector wouldn't be triggered by the flicker (since `mowingWeekArea` was already frozen), but any *event-driven* detector keyed on `vs` transitions would need debounce beyond the 60 s sustained window we already have.
3. **`vs = 6` (explicit user pause) vs. `vs = 2` at dock** — the tracker treats both as PAUSED_DOCKED with no timer. If the operator explicitly pauses via the app on the way back and the robot happens to reach dock while paused, we'd want the same "recharge-pause" hold. So a fix keyed strictly on `vs = 2` might be safer than one keyed on "docked charging". *(Resolved by the decided fix on #51: the completion criterion uses `vs ∈ {1, 2, 3}` with `vs = 6` excluded, preserving the user-pause hold.)*

## Follow-up trace to land

Enable DEBUG on `custom_components.navimow.run_tracker` + `custom_components.navimow.coordinator` + `mower_sdk.mqtt` before the next mowing session and capture:
- The full sequence of `run_tracker event: kind=… payload=…` DEBUG lines (open, close, reopen).
- The raw `/location` type-2 payloads with `mp`, `sub`, `wk`, `cmp`, `time` from run start through 15+ min after dock.
- Any `run_tracker: type-2 rejected by layer …` DEBUG entries (would confirm or disprove a silent layer-3 rejection near the mp-99 plateau).

The fix design has since been decided on BUG-09 (#51, Fable comment of 2026-07-04, operator decision): completion = `mp ≥ MP_COMPLETION_THRESHOLD` (99, `const.py` constant) ∧ `vs ∈ {1, 2, 3}`, **immediate close, no debounce**, label decision centralised in `_close_run` (`completed` iff last accepted `mp ≥` threshold). The earlier Fix A / Fix B candidates are superseded — `cmp` measures zone completion, not task completion, and was rejected as the label discriminator during review. The trace's purpose is therefore **validation, not selection**: confirm the operator's `mp = 99` plateau prediction on a fresh zone-#3 task, and confirm no silent layer-2/3 rejections near the plateau.

## Refs

- Issue: [BUG-09 (#51)](https://github.com/raouldekezel/NavimowHA/issues/51).
- FEAT-05 SPIKE record on [#43](https://github.com/raouldekezel/NavimowHA/issues/43) — Fable's open question 3 flagged this class of ambiguity as non-blocking, expecting a "timeout or retroactive close" fallback. The fallback exists and fired once charging ended (retroactive close, correct duration); what it lacked was a completion label — the actual BUG-09 defect.
- MAP-01 diag [`2026-05-23_map-01_vehiclestate-catalog`](../2026-05-23_map-01_vehiclestate-catalog/findings.md) — `vs=2` (charging) semantics.
- PR [#49](https://github.com/raouldekezel/NavimowHA/pull/49) — introduced `INTERRUPT_SUSTAIN_SECONDS = 60` and `DOCKED_NOT_CHARGING = {1, 3}`.
- PR [#50](https://github.com/raouldekezel/NavimowHA/pull/50) — introduced `sensor.<slug>_run_state`, `_last_run_started`, `_last_run_duration`, `_last_run_result` and their gating.
