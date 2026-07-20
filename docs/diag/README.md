# Diagnostic experiments

Raw artifacts of timed diagnostic runs against a real Segway Navimow
robot lawn mower (typically i210 LiDAR Pro). Each session is
self-contained and immutable once merged; later sessions supersede
rather than rewrite. Not installed by HACS.

## Sessions

One row per experiment session. Each session lands as its own PR that
either drives a feature design or validates it.

| Date       | Bug | Question | Answer (TL;DR) | Link |
| ---------- | --- | -------- | -------------- | ---- |
| 2026-05-22 | [BUG-01](https://github.com/raouldekezel/NavimowHA/issues/3) | With upstream v1.1.0 timings (`HTTP_FALLBACK_MIN_INTERVAL=3600`), how long does HA actually stay stale after a routine MQTT disconnect? | 55 minutes measured in vivo (10:07:34 → 11:03 CEST): `http_ts=3722293.595...` frozen across ~110 coordinator ticks because the fallback poll is throttled to 1 h. Reducing to 60 s (BUG-01) shrinks the same gap ~60×. | [2026-05-22_bug-01_silent-mqtt-stale-pre-fix](2026-05-22_bug-01_silent-mqtt-stale-pre-fix/findings.md) |
| 2026-05-22 | [FEAT-03](https://github.com/raouldekezel/NavimowHA/issues/9) | How often does the Navimow MQTT cloud disconnect, and is that state observable from any HA entity today? | 26 disconnect callbacks over 6.5 days — nominal ~56 min cadence (broker-forced, rc=7 token expiry) + occasional rapid cascades. All invisible to lawn_mower/sensor entities today because SDK reconnect is fast and cached fields don't proxy connectivity. `binary_sensor.<slug>_cloud_connected` (FEAT-03) surfaces the state directly. | [2026-05-22_feat-03_cloud-reconnect-pattern](2026-05-22_feat-03_cloud-reconnect-pattern/findings.md) |
| 2026-05-23 | [FEAT-01](https://github.com/raouldekezel/NavimowHA/issues/7) | Does the WSS broker accept a subscription on the SDK-ignored `/downlink/vehicle/<id>/realtimeDate/location` topic, and does the `mqtt.on_message` override route it end-to-end to a working `sensor.<slug>_position`? | Yes. Subscription returns `rc=0`; type-1 payloads (postureX/Y/Theta + vehicleState) reach the coordinator via the override; the position sensor renders state and attributes end-to-end. Type-3 heartbeats are correctly ignored. | [2026-05-23_feat-01_phase1-deployment](2026-05-23_feat-01_phase1-deployment/findings.md) |
| 2026-05-23 | [SPIKE-01](https://github.com/raouldekezel/NavimowHA/issues/24) | Do operational errors (obstacle, tangled net) surface on the /state, /event, or /attributes MQTT channels? | No. Two incidents — a tangled net requiring manual intervention (2026-05-22 13:30, 3 h 47 min gap in state messages) and a deliberate obstacle test (2026-05-23 14:31, 13 min 30 s of silent detour) — produced zero error payloads. Firmware only signals via out-of-band FCM/APNS to the mobile app. Any HA "stuck detection" must be a client-side heuristic on FEAT-01's position + vehicleState. | [2026-05-23_spike-01_errors-invisible-mqtt](2026-05-23_spike-01_errors-invisible-mqtt/findings.md) |
| 2026-05-23 | [MAP-01](https://github.com/raouldekezel/NavimowHA/issues/25) | What are the observed values of `/location` `vehicleState` and `/state` `state` fields on i210, and how does the SDK handle them? | 7 distinct `vehicleState` values observed over 156 h continuous: 1-6 documented + an **uncatalogued `8` observed twice on 2026-05-26 04:56 UTC** with `postureX/Y/Theta` all `0.0` (firmware reset/power-on transient hypothesis). `/state` uses the firmware **typo `isIdel`** (342 occurrences, 0 correct `isIdle` spellings); SDK's `mower_sdk.models` normalizes both to `idle`. | [2026-05-23_map-01_vehiclestate-catalog](2026-05-23_map-01_vehiclestate-catalog/findings.md) |
| 2026-05-25 | [BUG-03](https://github.com/raouldekezel/NavimowHA/issues/5) | Does the `_handle_attributes` clock pollution that BUG-03 addresses reproduce on i210? | No — the `/attributes` topic is empty on i210 (0 messages in 6.5 days vs 478 `state` messages). The fix is latent-preventive for this model but load-bearing on other Navimow models whose cloud does push `/attributes`. Kept for model-portability, contract clarity, and future-proofing against a potential Segway roadmap push. | [2026-05-25_bug-03_attrs-topic-empty](2026-05-25_bug-03_attrs-topic-empty/findings.md) |
| 2026-05-25 | [BUG-04](https://github.com/raouldekezel/NavimowHA/issues/6) | Why does `sensor.<slug>_batterie` flicker between `100 %` (HTTP truth) and `0 %` (post-over-discharge MQTT lie) every ~30 s once the robot has once bottomed out to 0 %? | The SDK's `get_cached_state()` retains the last MQTT push indefinitely. The coordinator re-applies it at the start of every tick, clobbering any fresher HTTP fallback state written at the end of the previous tick. Fix: skip the cache re-application when `_last_http_fetch > _last_mqtt_state_update`. | [2026-05-25_bug-04_battery-flicker](2026-05-25_bug-04_battery-flicker/findings.md) |
| 2026-05-25 | [FEAT-02](https://github.com/raouldekezel/NavimowHA/issues/8) | On a real multizone run, which /location type-2 field represents the run's progression vs the current zone's, and is `subtotalArea` per-zone or cumulative? | `mowingPercentage` = run progression (monotonic 39 → 100). `currentMowProgress / 100` = current-zone progression (reset at boundary crossing 60 → 20 %). `subtotalArea` is CUMULATIVE across the whole run; per-zone area must be computed as the delta between boundary changes. Boundary ids are creation-order (1 = zone 1, 2 = tunnel/transit, 3 = zone 2) not sequential physical order. A transient "idle" type-2 packet during a mid-run dock-and-charge must be filtered by downstream consumers. | [2026-05-25_feat-02_multizone-run](2026-05-25_feat-02_multizone-run/findings.md) |
| 2026-07-02 | [BUG-05](https://github.com/raouldekezel/NavimowHA/issues/29) | Why does the battery still glitch to `100` (mid-mow) or `48` (overnight dock) every ~40 min despite BUG-04's fresh-cache guard being live? | The cloud replays the last-buffered `/state` payload verbatim at every SDK reconnect (40-min cadence, matching FEAT-03 diag #23). BUG-04 guards `sdk.get_cached_state()` in the poll path only — the reconnect goes through `_handle_state` and overwrites `_last_state` with the stale payload. The payload carries a firmware `timestamp` (epoch ms) exposed by the SDK; a strict-less-than guard in `_handle_state` drops the replay. | [2026-07-02_bug-05_stale-mqtt-replay-at-reconnect](2026-07-02_bug-05_stale-mqtt-replay-at-reconnect/findings.md) |
| 2026-07-03 | [BUG-07](https://github.com/raouldekezel/NavimowHA/issues/44) / [BUG-08](https://github.com/raouldekezel/NavimowHA/issues/45) | Why does `progression` show `100` for ~1 min at every `start_mowing` on raoul.6, and why do battery hiccups still happen despite BUG-05's timestamp guard? | Two independent regressions. **BUG-07**: `_handle_state` does not touch `coordinator.stats`; the first `/location` type-2 arrives 66 s after the `mowing` transition, so the previous session's cached values (`mowing_percentage=100`, `current_mow_progress=10000`) render verbatim during that window — and the fresh type-2 lands with `boundary=1`, so BUG-06's truthy filter does not intercept it. **BUG-08**: the cloud pushes `/state` payloads with **stale battery content** but **fresh (larger) firmware timestamps**; BUG-05's strict-less-than guard accepts them and the sensor flips backward until the next HTTP-fallback tick (~60-90 s). 2/2 hiccups in 65 min. | [2026-07-03_bug-07_progression-battery-trace](2026-07-03_bug-07_progression-battery-trace/findings.md) |
| 2026-07-04 | [BUG-09](https://github.com/raouldekezel/NavimowHA/issues/51) | On the first live raoul.8 run, why is the tracker parked in `paused_docked` ~50 min after the robot returned to dock, and what happens when charging finishes? | `mp` peaked at **99** (never 100), robot docked directly on `vs=2` (charging) — path 1 (`mp=100`) and path 3 (`vs ∈ {1, 3}` sustained 60 s) are both unreachable during charging. Once the battery reached 100 % at 11:15:59 CEST the firmware transitioned to `vs=1`, and the tracker closed the run 89 s later (60 s sustained + 30 s coordinator tick — mechanism works). But total end-of-run latency was **53 min 31 s** and the result label was `interrupted` on a manifestly *completed* run (zone 100 %, autonomous return, full recharge). Fix design open: fast path (`mp ≥ ceiling` + `cmp_max = 10000` + docked → `COMPLETED`) or backstop (`mowingWeekArea` stagnation). | [2026-07-04_bug-09_paused-docked-mp-99](2026-07-04_bug-09_paused-docked-mp-99/findings.md) |
| 2026-07-04 | [BUG-09](https://github.com/raouldekezel/NavimowHA/issues/51) | Post-fix validation on `raoul.9`: does the fast path close a real dock arrival at the right latency and label? | Yes. First raoul.9-driven task closed **0 s after dock arrival** at 15:13:20 CEST (vs 53 min 31 s pre-fix on the morning `raoul.8` close), labelled `completed` (vs mislabeled `interrupted` pre-fix). Timing signature: `mp = 100` at 15:11:23 CEST while robot still returning (`vs = 5`), tracker held; 1 min 56 s later at 15:13:20 CEST robot arrived on dock (`vs = 2`), `_maybe_complete_run` fired in the same call frame as `process_vehicle_state(VS_DOCKED_CHARGING)`. Bonus finding: firmware **DID emit `mp = 100`** on this resumed task — partly refuting the FEAT-05 SPIKE open question 3 hypothesis "mp caps at 99". SPIKE-02 open questions 2 & 5 fed by this diag. | [2026-07-04_bug-09_fix-validation-in-prod](2026-07-04_bug-09_fix-validation-in-prod/findings.md) |
| 2026-07-04 | [SPIKE-02](https://github.com/raouldekezel/NavimowHA/issues/54) | Post-BUG-09, why do the three `last_run_*` sensors disagree on which run they describe, and why does pressing RUN after `interrupted` not appear to start a new run? | Two independent problems tangled together. **(A)** Navimow firmware models `mp`-carrying **tasks** persisting across `interrupted` closes; the tracker's `_open_run` only fires on IDLE or `sub < RESET_SUB_CEILING`, so a user RUN post-`interrupted` triggers `_reopen_run` (`start_time` stays at the task-start, `mp` displays firmware-truthful 97 on the resume). **(B)** `_last_run_start_dt` prefers `open_run.start_time` when a run is open, but `_last_run_duration`/`_last_run_result` always read `last_finished_run.*` — during an ongoing cycle the three sensors describe two different runs. BUG-09 aligned the *closing* on session-shape (dock arrival); opening is still on task-shape. Options weighed (none proposed): detect `vs → 4/5` from `COMPLETED`/`INTERRUPTED` as session start, split into `current_run_*` / `last_run_*` families, renormalise `mp` session-scoped, or accept-and-rename. Blocked on DEBUG capture of a real interrupt-then-user-RUN cycle to answer `mowStartType` distinguishability and whether firmware ever resets `mp` on a manual RUN. | [2026-07-04_spike-02_run-semantics-task-vs-session](2026-07-04_spike-02_run-semantics-task-vs-session/findings.md) |
| 2026-07-05 | [BUG-10](https://github.com/raouldekezel/NavimowHA/issues/58) | During the morning scheduled mow, does the tracker process the fresh type-2 packets — or does something silently block it? | Blocked. Layer 2 rejects 100 % of the incoming type-2 packets because `mowingWeekArea = 91.3 m²` on today's fresh task is less than `_last_accepted_wk = 1189.34 m²` from yesterday's afternoon task end, and `_crosses_iso_monday` returns False (2026-07-04 Saturday and 2026-07-05 Sunday map to the same ISO week 27). The invariant "`mowingWeekArea` never decreases within an ISO week" is factually wrong — the firmware resets `wk` on task-end (or task-start, or a similar cadence, TBD). Consequence: all `razibus_*` sensors frozen on yesterday's afternoon close values; the run is invisible to HA. Fingerprint DEBUG line: `run_tracker: type-2 rejected by layer 2 (wk=91.3 last=1189.34 …)`. Bonus SPIKE-02 answers: `mp` DOES reset on a fresh scheduled task (contrast: resume from `interrupted` kept `mp=97`); `mowStartType=0` observed on this scheduled start. | [2026-07-05_bug-10_wk-reset-blocks-tracker](2026-07-05_bug-10_wk-reset-blocks-tracker/findings.md) |
| 2026-07-07 | [MAP-01](https://github.com/raouldekezel/NavimowHA/issues/25) | Operator-scripted lifecycle (start → user pause off-dock → resume → dock) — what `vehicleState` does a real user pause actually emit, and does `vs = 6` mean "user pause" as the current catalog claims? | No. A real user pause emits `vs = 3` + `/state = isPaused` off-dock (14:46:39 UTC). `vs = 6` is not user-facing at all — it is the post-mow map-consolidation phase (`isMapping`), corroborated by the operator's 2026-05-23 correlation. `vs = 3` is a **catch-all** for "not mowing / returning / charging / mapping" with three observed sub-cases (user pause off-dock, transient at-dock idle flip, docked-unpowered). Rename `VS_PAUSED = 6 → VS_MAPPING = 6`; keep `VS_DOCKED_UNPOWERED = 3` symbol with a broader comment. Latent BUG-09 concern noted (`DOCKED_NOT_USER_PAUSED = {1, 2, 3}` would over-trigger completion on a user pause at `mp = 99` off-dock); not fixed here. `vs = 6` itself was not captured today (battery full, no `mp = 99` phase). | [2026-07-07_map-01_vs-empirical](2026-07-07_map-01_vs-empirical/findings.md) |
| 2026-07-09 | [BUG-14](https://github.com/raouldekezel/NavimowHA/issues/89) / [BUG-16](https://github.com/raouldekezel/NavimowHA/issues/92) | On a real recharge-mid-run day, does the `mp = 99` plateau ever bump to `mp = 100`, and if so when? What does the firmware ship on the wire when the operator triggers the next task? | Yes but LATE. The mini-run at 12:51 CEST closed with `mp = 99, cmp = 10000`. The firmware did NOT emit `mp = 100` in real time. At 16:11:44 CEST — **+3 h 20** — a packet with `boundary = 1, cmp = 10000, mp = 100, sub = 231.77` (same `sub` as the mini-run close) arrived immediately before the first real Figuier packet at 16:13:08. This is a task-end delivery shipped at task-start. Two consequences: (1) PR #91's refined rule `mp ≥ 99 ∧ cmp = 10000` is structurally required — waiting for `mp = 100` alone would mis-label the mini-run as `interrupted` for hours; (2) the late packet in `STATE_COMPLETED` opens a phantom run (start_time wrong, zones = [1, 3] instead of [3]) — filed as BUG-16 #92. Bonus finding: **`cmp` is zone-persistent across sessions**, contradicting the previous doc — the resumed Figuier packet at 16:13:08 shows `cmp = 4404` (credit for 07/07's partial mow), not 0. | [2026-07-09_bug-14_late-mp-100](2026-07-09_bug-14_late-mp-100/findings.md) |
| 2026-07-19 | [BUG-17](https://github.com/raouldekezel/NavimowHA/issues/105) | Why does `sensor.<slug>_current_zone_progress` render `100.0 %` for the entire run when the operator starts a fresh mow on a boundary whose previous task closed at `cmp_max = 10000`? | Firmware replays a **late task-end vestige packet** (`action = -1, mp = 100, cmp = 10000, mowingWeekArea = 0.0, subtotalArea = 0.0`) 85 ms after the `docked → mowing` state transition. The tracker accepts it as the run's first `type-2`, seeding `zones[0].cmp_max = 10000`. `_update_zone` maxes `cmp_max` monotonically (`run_tracker.py:1010`), so every subsequent packet (`cmp = 100, 215, 413, …, 3111`) is silently clamped. Blast radius (from Session 2 close-out): also poisons `zones[0].first_time` (used by FEAT-08 `ZoneRecord.size_estimate_updated_ms` → `last_complete_pass_at` misdated) and `zones[0].sub_entry = 0.0` (correct by coincidence when first-in-run, wrong otherwise). Vestige does NOT re-fire on `_reopen_run` (validated Session 2). Same firmware mechanism as #92 BUG-16 but different pathology and different tracker state at packet arrival. | [2026-07-19_bug-17_cmp-max-late-task-end](2026-07-19_bug-17_cmp-max-late-task-end/findings.md) |
| 2026-07-20 | BUG-17 / BUG-13 / new — pending triage | Post-raoul.22 (BUG-17 fix live) validation mow — does the guard suppress the vestige on the operator's actual daily rhythm? | No. Wire-shape identical to the 2026-07-19 vestige (`action = -1, mp = 100, cmp = 10000, sub = "0.0", wk = "357.63"` mid-week Monday). BUG-17 guard bypassed because the tracker was restored to `STATE_INTERRUPTED`/`STATE_COMPLETED` from Store on HA restart (previous 2026-07-19 close persisted; `coordinator.py:183` `run_tracker.restore(tracker_snap)`), not `STATE_IDLE` — arming window dark by design (#105 fifth-edit seam map delegates post-close to #86). Packet flowed to the post-close `is_reset` branch → `_open_run(vestige)` → run anchored on vestige's `start_time`/`sub₀`/`mow_start_type`, `zones[0].cmp_max = 10000` poisoned exactly as raoul.19. Also confirmed: **no observability line was emitted** — #92's promised BUG-16/BUG-13 hook is not implemented in the deployed source. Triage question filed. | [2026-07-20_bug-17_bypass-in-state-completed](2026-07-20_bug-17_bypass-in-state-completed/findings.md) |

## Layout

```
docs/diag/
├── README.md                                # this file (= the index)
└── YYYY-MM-DD_<bug-id>_<short-topic>/
    ├── findings.md
    ├── NN_<action>.mqtt.log                 # raw HA log slice, ANSI stripped
    └── NN_<action>.sensors.tsv              # periodic HA entity poll
```

- Subdirectory name uses an action-anchored topic (e.g.
  `2026-05-25_bug-04_battery-flicker`), never a finding-anchored one.
- `NN_` numeric prefix gives chronological order; the slug describes the
  **action taken**, never the **outcome** (findings get revised; actions
  don't).
- Two file flavours per action: `.mqtt.log` for raw log lines,
  `.sensors.tsv` for periodic polls of Navimow entities.

## findings.md

Required structure, in this order:

1. **TL;DR** — one sentence stating the answer to the session's question.
2. **Context** — date, robot model (`i210 LiDAR Pro`, etc.), **fork tag or
   commit SHA** of the integration running during the experiment (e.g.
   `NavimowHA-v1.1.0-raoul.1` or `0d5d63e`), HA version, relevant
   pre-experiment state.
3. **Actions taken** — numbered list matching the `NN_…` prefixes.
4. **Timeline** — key timestamps with what happened at each. Evidence
   layer; survives even if the conclusions are later revised.
5. **Findings** — bullet list. Each conclusion cites a specific line or
   timestamp from the included files.
6. **Open questions** — what the next session would need to answer.
7. **Refs** — issues, PRs, previous or follow-up sessions.

## .sensors.tsv format

First non-comment line is a tab-separated header. The very first line is a
header comment carrying the polling interval and timezone offset. Example:

```
# interval=10s tz=+02:00
timestamp	battery	activity	zone	position_x	position_y
14:32:10	87	mowing	3	4.21	-3.15
```

The cadence and offset comment is mandatory: these files are routinely
pasted detached from `findings.md` into upstream issues, and bare TSV with
neither column names nor sampling rate is unreadable evidence.

## PII to redact

| Real value                                                            | Redacted form                |
| --------------------------------------------------------------------- | ---------------------------- |
| Robot serial (`3KAAW...`, 14 chars)                                   | `REDACTED-ROBOT-SERIAL`      |
| MQTT userid (numeric, e.g. `7091984`)                                 | `REDACTED-MQTT-USERID`       |
| Wi-Fi SSID                                                            | `REDACTED-WIFI-SSID`         |
| Wi-Fi BSSID / MAC addresses                                           | `REDACTED-MAC`               |
| Timezone **name** (e.g. `Europe/Brussels`)                            | `Europe/[REDACTED]`          |
| MQTT client id (`web_<userid>_<random>`)                              | `REDACTED-MQTT-CLIENT-ID`    |
| MQTT dynamic password (`pwdInfo`)                                     | `REDACTED-MQTT-PASSWORD`     |
| OAuth Bearer / access / refresh tokens (`eyJ…` or hex)                | `REDACTED-OAUTH-TOKEN`       |
| Account / device UUIDs                                                | `REDACTED-UUID-<purpose>`    |
| Email address                                                         | `REDACTED-EMAIL`             |
| Navimow client_secret (`57056e15-…`)                                  | `REDACTED-CLIENT-SECRET`     |

**Keep** numeric UTC offsets (`+02:00` is shared by ~40 countries, not
PII), timestamps, log levels, thread names, module names, generic robot
model identifiers (`i210 LiDAR Pro`, ...), firmware version strings, and
MQTT topic paths (`/downlink/vehicle/<REDACTED>/realtimeDate/state`) once
the serial inside is redacted — they carry no PII and are necessary for
analysis.

When in doubt, redact.

## Drift-proof index

The `## Sessions` table above is the index. To prevent it from drifting
out of sync with the actual subdirectory list, `scripts/check_diag_index.py`
fails if any `docs/diag/<date>_<topic>/` subdirectory is missing from the
table, or if any table row points to a non-existent directory.

This is enforced by the **Check diag index** GitHub Actions workflow
(`.github/workflows/check-diag-index.yaml`), which runs on every push and
pull request that touches `docs/diag/` or the check script itself. A PR
that adds a session without updating the table (or vice versa) fails CI.

Run it locally first to fail fast:

```
python3 scripts/check_diag_index.py
```

The session PR is expected to add one row to the table **in the same
commit** that adds the subdirectory.

## Adding a new session

1. Branch from `deploy`: `git checkout -b patches/docs-diag-<bug-id>-<date>`.
2. Run the experiment; redact PII on the copies in `/tmp` first.
3. Create `docs/diag/<date>_<bug-id>_<topic>/` and populate.
4. Write `findings.md` last, in front of the raw files.
5. Add the row to the `## Sessions` table here.
6. Run `python3 scripts/check_diag_index.py`.
7. Open one PR per session targeting `deploy`.
