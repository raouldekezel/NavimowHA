# BUG-01 — silent MQTT gap leaves HA stale for ~1 h under upstream timings

## TL;DR

On 2026-05-22 (morning, **before** the BUG-01 patch was applied at
~19:00 CEST), a routine MQTT disconnect at 10:07:34 CEST left the
coordinator on the last cached `battery=23` payload for **55 minutes**
straight, with `http_ts` frozen at `3722293.595540517` across every
30-s tick. The upstream default `HTTP_FALLBACK_MIN_INTERVAL = 3600`
throttled the fallback poll for a full hour, so HA showed a stale
battery/state until the SDK's own reconnect brought a fresh MQTT push
at 11:03. BUG-01 lowers this to 60 s.

## Context

- Date: 2026-05-22, 10:00–12:00 CEST
- Robot: Segway Navimow i210 LiDAR Pro (`REDACTED-ROBOT-SERIAL`)
- Integration: **upstream `NavimowHA v1.1.0` vanilla** on the local
  install (patches BUG-01/02/03 applied later that day at ~19:00 CEST
  — the morning window pre-dates all fork changes).
- HA: 2026.5.x (Docker on intel-nuc)
- Pre-experiment state: robot docked, on charge. First observation
  window since debug logging was enabled at 09:50.

## Actions taken

1. `01_pre-fix-coordinator-ticks-frozen.mqtt.log` — 131-line log slice
   spanning 10:00:28 → 11:59:54 CEST, one coordinator tick per minute
   plus every state message and reconnect callback.

## Timeline

| Local time | Event |
| --- | --- |
| 10:00:28 | Coordinator's first tick: `source=http_fallback, mqtt_ts=None, http_ts=3721597.268...` |
| 10:00:31 | First MQTT state: `state=docked battery=23`. From here `_last_state.battery = 23`. |
| 10:07:34 | MQTT disconnect callback (`rc=0`). Last HTTP fetch at `http_ts=3722293.595...`. |
| 10:08:04 → 11:03 CEST | **55 min of coordinator ticks with `http_ts=3722293.595540517` frozen.** Every tick logs `source=http_fallback` (from the last successful fetch), but `HTTP_FALLBACK_MIN_INTERVAL=3600` blocks any new poll. `_last_state.battery` stays 23. |
| ~11:03 | SDK reconnect brings fresh MQTT state; `_last_state.battery` updates. |
| 11:55–11:59 | Steady state, `source=mqtt_cache`, `mqtt_ts=3725717...`, `http_ts=3726047...` (last HTTP fetch nearly 5 min old — pre-fix behaviour: only refreshed lazily). |
| 11:59:53 | Second MQTT disconnect + immediate reconnect within 1.2 s (`rc=?` on paho close). |

## Findings

- **The 3600 s HTTP throttle was load-bearing on the observation.**
  During the 55-min gap, the coordinator ticked ~110 times, each time
  logging `source=http_fallback` (a post-hoc annotation from the last
  successful fetch) but never actually calling
  `api.async_get_device_status()` because
  `now - self._last_http_fetch <= HTTP_FALLBACK_MIN_INTERVAL`.
- **The stale value in HA was NOT an artefact of the cache** — the
  MQTT cache remained on the 23 % payload for the entire window;
  reducing MQTT_STALE_SECONDS alone would not have helped because the
  fallback poll was itself blocked. The three tunings ship together
  for a reason: they cover the two bottlenecks (stale detection and
  refresh throttle) plus the keepalive that would have detected the
  half-open TCP earlier.
- **BUG-01 (60 s HTTP throttle) shrinks this specific gap by 60×.**
  Post-patch tuning would have refreshed the state at 10:08:31 +60 s
  and continued every ~60 s until MQTT recovered. Observation window
  gap = 55 min → post-fix expected gap ≤ 90 s.
- **Reconnect cadence observed here (10:07:34 → 11:03:42 → 11:59:53 =
  two consecutive ~56 min windows) matches the "rc=7 token expiry" cadence
  the Journal describes later's later observations**, but the disconnect at
  10:07 came with `rc=0` (client-initiated), suggesting the SDK's own
  credential-refresh loop closed the socket. Motivates FEAT-03
  (`binary_sensor.…_cloud_connected`) so this class of disconnect
  surfaces in HA rather than only in the debug log.

## Open questions

- **Why the 10:07 `rc=0` disconnect?** The Journal at that time notes
  "Le SDK A bien un mécanisme de reconnect" but doesn't isolate why the
  first disconnect at 10:07 fired only 7 min after subscribe. Not
  needed to justify BUG-01 (the throttle behaviour is fundamental).
- **Post-patch validation on the same pattern**: after 19:00 CEST the
  fix was applied and MQTT reconnects continued at ~56-min intervals
  (see later Journal entries). We could measure the actual observed
  gap under the patched timings on the same day's afternoon window —
  deferred (would only reinforce the case).

## Refs

- Upstream PR [segwaynavimow/NavimowHA#48](https://github.com/segwaynavimow/NavimowHA/pull/48) — the timings this diag empirically motivates.
- Local BUG-01 fix in PR [#11](https://github.com/raouldekezel/NavimowHA/pull/11).
- Local doc: `Home Assistant - Navimow - Journal.md` § « 2026-05-22 10:00 → 11:03 — 1h de silence MQTT total ».
