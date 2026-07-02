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
