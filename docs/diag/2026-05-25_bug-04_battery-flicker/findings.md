# BUG-04 — battery flicker under stuck-MQTT + fresh-HTTP race

## TL;DR

The SDK's cached MQTT state (`sdk.get_cached_state()`) is re-applied on
every coordinator tick, so a stuck MQTT payload (`battery=0` after
firmware over-discharge recovery) clobbers the fresher HTTP fallback
reading (`battery=100`) every ~30 s. Result: HA UI oscillates.

## Context

- Date: 2026-05-25
- Robot: Segway Navimow i210 LiDAR Pro (`REDACTED-ROBOT-SERIAL`)
- Integration: local patched build of `NavimowHA v1.1.0` (pre-fork),
  living in `raouldekezel/home-assistant` at `data/config/custom_components/navimow/`
- HA: 2026.5.x (Docker on intel-nuc)
- Pre-experiment state: the robot spent the night 2026-05-23 → 2026-05-24
  on an **unplugged** base and drained to 0 % (~13 W idle consumption
  measured, 8 h 08 discharge from 100 % to 0 %). The base was then
  re-plugged 2026-05-24 morning. By 2026-05-25 mid-afternoon the pack
  was physically at 100 % (confirmed via the Navimow app and via a
  direct `POST /openapi/smarthome/getVehicleStatus` call returning
  `capacityRemaining: [{PERCENTAGE, 100}]`, `descriptiveCapacityRemaining: "FULL"`).
  But the `/state` MQTT payload still carried `battery: 0`.

## Actions taken

1. `01_coordinator-ticks.mqtt.log` — 5-minute slice of coordinator debug
   logs at 15:00–15:05 CEST, showing the tick-by-tick source
   attribution, MQTT vs HTTP timestamps.

## Timeline

| Local time | Event |
| --- | --- |
| 2026-05-24 02:19 | Robot batteries hit 0 %, over-discharge event. Base still unplugged. |
| 2026-05-24 morning | Operator plugs the base back in; robot starts charging. |
| 2026-05-24 13:20 | First MQTT `/state` reported `battery: 0` while a direct HTTP `getVehicleStatus` returned `100`. |
| 2026-05-25 15:00:29 | HA `sensor.…_batterie` displayed value flickering between the two sources every tick. |

## Findings

- **The MQTT payload was durably stuck at 0** — the SDK cache retained
  the last MQTT push indefinitely, and the cloud only pushes on state
  transitions. With the robot in `charging`/`docked` stable state, no
  new MQTT `state` push arrived for hours.
- **HTTP fallback fetched the correct value every ~30 s** — visible in
  `01_coordinator-ticks.mqtt.log` where `http_ts` advances every tick
  (`3999068 → 3999128 → 3999190 → 3999254 → 3999334 → …`) while
  `mqtt_state_ts` stays frozen at `3998732.670363963`.
- **The coordinator re-applies the SDK cache unconditionally**:
  `_async_update_data()` calls `sdk.get_cached_state()` at the top of
  every tick and assigns `self._last_state = cached_state`, overwriting
  whatever the HTTP fallback had just written. The `source` attribute
  in the log shows `http_fallback` because it was set after the cache
  application, but the state message object with `battery=0` had
  already replaced the HTTP truth in memory.
- **Consequence in HA**: `lawn_mower.<slug>` battery attribute flips
  between `0 %` (mqtt_cache re-applied at start of tick) and `100 %`
  (HTTP fallback overrides at end of tick if `is_state_stale`).
- **Not observable in `_last_data_source`**: the debug string shows
  `http_fallback` after each HTTP call succeeds, but the field is a
  post-hoc annotation — the state message that actually reaches HA is
  whichever was last assigned to `self._last_state`.

## Fix

Skip the SDK cache re-application when the last HTTP fetch is newer
than the last observed MQTT state push:

```python
http_is_newer = (
    self._last_http_fetch is not None
    and (
        self._last_mqtt_state_update is None
        or self._last_http_fetch > self._last_mqtt_state_update
    )
)
if not http_is_newer:
    self._last_state = cached_state
    self._last_data_source = "mqtt_cache"
```

The freshness guard uses monotonic timestamps that the coordinator
already tracks (`_last_http_fetch`, `_last_mqtt_state_update` from
BUG-03), so it has no dependency on payload-embedded server timestamps
(which the SDK does not expose per-message).

## Open questions

- **When does the MQTT battery recover?** Empirically, a new `state`
  push arrives when the robot transitions (undocks for a mowing cycle,
  goes into paused/returning). Whether the cloud eventually pushes a
  refresh purely on-schedule remains unconfirmed.
- **Should we apply the same guard to attributes?** Attributes carry
  progression/zone/week area — none of which flicker in the same way
  as the battery today. Skipping the guard for attributes keeps them
  as fresh as possible. Deferred.

## Refs

- Upstream issue [segwaynavimow/NavimowHA#11](https://github.com/segwaynavimow/NavimowHA/issues/11)
  — `@stefan73` diagnosed the race in prose, no code provided.
- Related upstream issues on similar symptom:
  [#44](https://github.com/segwaynavimow/NavimowHA/issues/44),
  [#67](https://github.com/segwaynavimow/NavimowHA/issues/67).
- Local doc: `Home Assistant - Navimow - Journal.md` § 2026-05-24
  après-midi "MQTT renvoie 0 après l'over-discharge (cause non établie)".
