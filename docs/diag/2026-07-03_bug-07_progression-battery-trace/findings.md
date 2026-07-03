# BUG-07 — progression trompeur at session start; battery hiccups from stale-content /state pushes

## TL;DR

The `progression` sensor renders the previous session's cached value (`mowing_percentage=100`, `current_mow_progress=10000`) for the first **66 s** of every fresh mow — no `/location` type-2 payload arrives before then. Independently, `battery` flips to stale-content values twice in a 65-min trace (once during mowing, once while charging) because the cloud replays old `/state` payloads with **fresh timestamps**, defeating the BUG-05 strict-less-than guard.

## Context

- Date: 2026-07-03
- Fork tag: `NavimowHA-v1.1.0-raoul.6` on `deploy` @ `f48a99a`
- Robot model: i210 LiDAR Pro (device name "Razibus")
- HA version: 2026.1.3, container `hass` on intel-nuc
- Pre-experiment state: robot docked, batterie=100 %, cache stale from a 44-s test mow ~1 h earlier (`stats.mowing_percentage=100`, `stats.current_mow_progress=10000`, `stats.boundary=1`, `stats.area_session=0.0`, `stats.action=5`).
- Logger levels set to DEBUG on `custom_components.navimow.*` and `mower_sdk.mqtt` via `logger.set_level` for the duration of the trace, restored to INFO/WARNING afterwards.

## Actions taken

1. **01_states.sensors.tsv** — background sampler polled the HA REST API every 15 s (274 samples), recording `lawn_mower` state, `batterie`, `progression`, `zone_courante`, and the `current_mow_progress` / `surface_session` / `action` / `boundary_id` attributes.
2. **03_state-payloads.mqtt.log** — raw MQTT `/downlink/vehicle/<REDACTED>/realtimeDate/state` payloads captured over the trace window (7 firmware pushes).
3. **04_location-type2.mqtt.log** — raw MQTT `/location` payloads with `"type":2` (mowing stats), 94 entries.
4. **05_handle-state.mqtt.log** — coordinator-level `_handle_state received: …` DEBUG lines correlated with the raw pushes; 0 lines carrying `MQTT state DROPPED as stale` (i.e. BUG-05 guard did not fire once).

## Timeline (all UTC unless noted)

| Time | Event |
| ---- | ----- |
| 11:37:29 | Trace start, sampler running; robot docked; cache stale from a prior 44-s test mow |
| 11:39:33 | Orchestrator issues `lawn_mower.start_mowing` |
| 11:39:34.510 | Coordinator `_handle_state`: `state=mowing battery=100` (fresh timestamp `ts=1783078774523`) |
| 11:39:41 | Sampler observes `lawn_mower=mowing`, progression=**100**, zone=#1 (all still cache) |
| **11:40:40.505** | **First fresh `/location` type-2 arrives**: `{boundary:1, mowingPercentage:0, currentMowProgress:107, action:8}` — sensor drops to progression=0 in the next sampler tick |
| 11:58:24.549 | `/state` push with `battery=100` (`ts=1783079903857`) → `_handle_state` accepts; sensor jumps 94 → **100** |
| ~12:00:02 | HTTP fallback corrects sensor to `battery=92` |
| 12:04:33 | Orchestrator `lawn_mower.dock` — **ignored**, robot keeps mowing |
| 12:19:35 | Orchestrator second `start_mowing` — **ignored**, robot still mowing |
| 12:29:35 | Orchestrator second `dock` — accepted |
| 12:29:41 | Sampler observes `returning` |
| 12:31:27 | Sampler observes `docked`, batterie=68 % |
| 12:38:24.793 | `/state` push with `battery=68` (`ts=1783082303951`) → `_handle_state` accepts; sensor jumps 72 → **68** |
| ~12:39:59 | HTTP fallback corrects sensor to `battery=74` |
| 12:44:35 | Trace end; final state: `docked`, batterie=77 %, progression=**30** (frozen post-session), zone=#1 |

## Findings

### 1. Progression trompeur = 66 s window on every start (structural)

- `lawn_mower` transitions to `mowing` at `11:39:34.510` UTC. `_handle_state` fires but only writes `_last_state.state` and `_last_state.battery`; the `/location` type-2 topic that feeds `coordinator.stats` is **not** touched by this callback.
- The first fresh type-2 arrives at `11:40:40.505` UTC — **66 s later**. In the meantime `coordinator.stats` still holds the previous session's terminal payload:

```
timestamp   lawn_mower  progression   current_mow_progress  action  boundary_id
11:39:41    mowing      100           10000                 5       1
11:39:56    mowing      100           10000                 5       1
11:40:11    mowing      100           10000                 5       1
11:40:27    mowing      100           10000                 5       1
11:40:42    mowing      0             107                   8       1     ← fresh type-2
11:40:57    mowing      0             107                   8       1
```

- `mowing_percentage=100` and `current_mow_progress=10000` (=100.00 %) are BOTH the previous session's end-state, so **swapping the `progression` value_fn semantic from `mowing_percentage` to `current_mow_progress/100` (option A of [HARD-05](#41)) would not remove the trompeur** — the cached "100" reads identically under either metric.
- The first type-2 payload lands with `boundary=1` (Zone prunier), **not** the `boundary=0` session-init sentinel documented in the 2026-05-25 diag and fixed by BUG-06 (#39). So the truthy filter on `boundary=0` does **not** intercept this case either.
- The only fix that eliminates the trompeur is to **invalidate `coordinator.stats` (or at least its session-scoped fields) at the `docked → mowing` transition**. The 66-s window is then rendered as `unknown` (dashboard shows 0 via the `_or_zero` template) until the first fresh type-2 arrives.

### 2. Battery hiccups from stale-content `/state` pushes (BUG-05 gap)

Two hiccups reproduced during a single 65-min trace:

| Sensor time | Push time (payload) | Sensor jump | Payload timestamp | Delta vs prev push |
| ----------- | ------------------- | ----------- | ----------------- | ------------------ |
| 11:58:32 UTC | 11:58:24.549 UTC | 94 → **100** → 92 | `ts=1783079903857` | +18 min 50 s |
| 12:38:29 UTC | 12:38:24.793 UTC | 72 → **68** → 74 | `ts=1783082303951` | +6 min 59 s |

Both payloads carry firmware timestamps **larger** than the previously accepted `_last_state.timestamp`. The BUG-05 guard is `new_ts < prev_ts` (strict), so both slip through into the `_last_state` overwrite path — the sensor renders the stale battery until the next HTTP fallback tick (~60-90 s later) reads the real value from the REST API and re-writes `_last_state`.

The 7 raw `/state` payloads captured over the trace window (`03_state-payloads.mqtt.log`) show the cloud alternating between the current firmware state and this stale-content replay. `MQTT state DROPPED as stale` fires **zero times** across the trace (`05_handle-state.mqtt.log`).

The reproduction pattern matches the FEAT-03 / BUG-05 documented ~40-min WSS reconnect cadence but is **not** the "same timestamp" case that BUG-05 originally addressed — the cloud is forwarding the stale payload with a **fresh** timestamp, defeating a monotonic timestamp check by itself.

### 3. Peripheral observations

- **`lawn_mower.dock` service is not authoritative** during an active mow. The 12:04:33 UTC dock call was ignored; only the 12:29:35 UTC dock call (after the robot had chosen to keep going for ~50 min) took effect. Not the target of this ticket, but worth noting for any future scripted session control.
- **The 2026-07-03 07:30 UTC `boundary=0` session-init sentinel did not reproduce today.** Every fresh type-2 in this trace started with `boundary=1`. Consistent with FEAT-02 diag observation that the "init all-zero" payload is emitted rarely (once on that morning, once on 2026-05-25 diag).
- **`current_mow_progress` grew coherently** once the fresh type-2 stream started: `107 → 213 → 308 → 404 → 504 → …` in `~30-50 s` intervals; boundary stayed at 1 the whole session, no crossing.

## Proposed fix

### BUG-07a — session reset on `docked → mowing`

In `coordinator._handle_state` (or a state-transition observer), detect the `mowing` transition (previous `_last_state.state != mowing` and new state == `mowing`) and clear the session-scoped fields:

```python
self.stats = None                    # progression / current_zone / area_session
self.position = None                 # FEAT-01, dispatcher will re-populate
self.vehicle_state = None            # binary_sensor charging
```

`area_week` **must not** be cleared — it is a cumulative counter and HARD-02's `RestoreSensor` persistence relies on `_attr_native_value` continuing to reflect the last observed value until a fresh type-2 pushes a new `mowingWeekArea`.

Sensor behaviour post-fix during the 66-s trompeur window:

- `progression` returns `None` → HA `unknown` → dashboard `_or_zero` template shows **0**.
- `current_zone` returns `None` → `unknown` → history renderer skips (via existing `zones_during` handling).
- `charging` binary_sensor returns `None` (`vehicle_state = None`).
- `weekly_area` unchanged (RestoreSensor keeps the last observed value).

### BUG-07b — content-aware `_handle_state` gate

The timestamp-only BUG-05 guard misses stale-content payloads with fresh timestamps. Add a magnitude+direction sanity gate:

- If the payload's battery differs from `_last_state.battery` by more than `BATTERY_STEP_MAX_PCT` (e.g. 5 %) **and** the direction contradicts the current mower state (mowing → battery rising; charging/docked → battery falling), drop it as a probable replay.
- Log the drop under a distinct message (`MQTT state DROPPED as content-stale`) so we can count and monitor.

This is orthogonal to BUG-05: the timestamp check stays (covers the older-timestamp replay path) and the content check adds coverage for the fresh-timestamp replay path.

## Open questions

- Does the cloud replay `/state` on **every** WSS reconnect, or only on certain triggers (token refresh, keepalive miss)? A longer trace (2-3 h with several reconnect cycles) would tell whether the ~30-min cadence observed today is stable.
- Is there a firmware field on the payload that discriminates "current" vs "replay"? Grep the raw payloads for `mowStartType`, `subAction`, or any other tag that changes between the fresh push and the stale-content one.
- The `lawn_mower.dock` command being ignored mid-run — is that a Segway API limitation, an integration mapping bug, or a mode-specific policy? Out of scope for BUG-07, worth a separate SPIKE if operational scripts need reliable interruption.

## Refs

- [BUG-05 diag `2026-07-02_bug-05_stale-mqtt-replay-at-reconnect`](../2026-07-02_bug-05_stale-mqtt-replay-at-reconnect/findings.md) — original timestamp-based drop guard.
- [FEAT-02 diag `2026-05-25_feat-02_multizone-run`](../2026-05-25_feat-02_multizone-run/findings.md) — type-2 payload catalog.
- [FEAT-03 diag `2026-05-22_feat-03_cloud-reconnect-pattern`](../2026-05-22_feat-03_cloud-reconnect-pattern/findings.md) — ~40-min WSS reconnect cadence.
- [BUG-06 (#39)](https://github.com/raouldekezel/NavimowHA/issues/39) — `boundary=0` sentinel gate on `current_zone` (not applicable here, cache had `boundary=1`).
- [HARD-05 (#41)](https://github.com/raouldekezel/NavimowHA/issues/41) — semantics debate on `progression` (subsumed by BUG-07a; option A/B/C becomes cosmetic once the reset is in place).
- [HARD-02 (#33, PR #42)](https://github.com/raouldekezel/NavimowHA/pull/42) — `RestoreSensor` on `weekly_area` (must be preserved by the reset).
