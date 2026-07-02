# BUG-05 — cloud replays stale `/state` payload at every MQTT reconnect

## TL;DR

Every WSS reconnect (~40 min cadence on i210), the Navimow cloud replays
the **last-buffered** `/state` payload as if it were fresh. That
buffered payload can pre-date the current physical state by many
minutes (or hours during an overnight docking window), so HA gets
overwritten with a stale battery / activity / state before an HTTP
fallback recovers ~2 min later. BUG-04 does not cover this because it
guards against `sdk.get_cached_state()` in `_async_update_data`, not
against the `_handle_state` callback path. The payload carries a
firmware-side `timestamp` field (epoch ms) that the SDK already
exposes; a strict-less-than guard in `_handle_state` drops the replay.

## Context

- Date range: 2026-07-02 03:00 CEST → 11:00 CEST (nightly docking +
  morning mowing run)
- Robot: Segway Navimow i210 LiDAR Pro (`REDACTED-ROBOT-SERIAL`)
- Integration: `NavimowHA-v1.1.0-raoul.3` (HACS fresh install of the
  fork's latest tag, active since ~2026-07-02 22:30 CEST — the pattern
  captured here pre-dates the install and reproduces on both the
  operator's earlier local build and on `raoul.3`)
- HA: 2026.1.3 (Docker on intel-nuc)
- Robot pre-experiment state: fully charged (100 %) on dock since
  the previous mowing session; scheduled weekly run started at ~07:40
  CEST.

## Actions taken

1. `01_battery-history-mowing-morning.sensors.tsv` — deduped battery
   sensor history 03:00 → 11:00 CEST (72 rows: overnight dock, morning
   discharge, glitches highlighted below).

## Timeline

Extracted from the TSV.

### Overnight docking (03:00 → 07:20 CEST, robot docked, real battery = 100 %)

| Reconnect glitch | Value dropped | Recovery |
| --- | --- | --- |
| 03:18:18 | `100 → 48` | 03:20:18 → `100` |
| 03:58:19 | `100 → 48` | 04:00:18 → `100` |
| 04:38:18 | `100 → 48` | 04:40:18 → `100` |
| 05:18:19 | `100 → 48` | 05:20:18 → `100` |
| 05:58:19 | `100 → 48` | 05:59:49 → `100` |
| 06:38:19 | `100 → 48` | 06:40:18 → `100` |
| 07:18:19 | `100 → 48` | 07:20:18 → `100` |

The `48` is the last state pushed **hours before** — from a previous
mowing session. It reappears every 40 min because the cloud's
buffered-last-message replay hasn't been refreshed by a real
transition.

### Morning mowing run (07:40 CEST departure)

| Time | Battery | Note |
| --- | --- | --- |
| 07:40:23 | 99 | Departure, real discharge begins |
| 07:41:53 | 97 | |
| 07:43 → 07:57 | 96 → 85 | Monotonic descent (~52 %/h, matches spec) |
| **07:58:19** | **100** | **GLITCH — cloud replays pre-departure docked value** |
| 08:00:04 | 83 | Discharge resumes with the *actually* correct value |
| 08:01 → 08:37 | 82 → 54 | Monotonic descent |
| **08:38:19** | **100** | **GLITCH again, exactly 40 min after the first** |
| 08:40:35 | 51 | Discharge resumes |
| 08:42 → 08:58 | 50 → 37 | |

## Findings

- **The 40 min cadence is exact.** Both overnight and mid-run glitches
  sit on 07:18, 07:58, 08:38 — 40 min apart. This is ~15 min shorter
  than the 56-min token-refresh interval measured on the same install
  in FEAT-03 diag [#23](https://github.com/raouldekezel/NavimowHA/pull/23)
  on 2026-05-22 (six back-to-back 56-min cycles that afternoon). The
  interval appears to have shortened between May and July — probably a
  broker-side change; worth confirming against SDK release notes.
- **The replayed payload is byte-identical** to the last real payload
  the cloud saw before it buffered. Overnight it's the pre-docking
  battery (48 %); mid-run it's the pre-departure docked state (100 %).
  The cloud does not synthesize a fresh reading; it simply retransmits.
- **BUG-04 doesn't help here** — BUG-04 blocks
  `sdk.get_cached_state()` clobbering HTTP in `_async_update_data`.
  The reconnect path goes through the SDK's dispatch callback →
  `_handle_state` → `_update_from_state`, which unconditionally writes
  `_last_state` and pushes it to entities.
- **The payload carries a `timestamp` field** (epoch ms) that is
  exposed as `DeviceStateMessage.timestamp` by `mower_sdk.models`.
  Sample capture from `mower_sdk.mqtt` DEBUG:
  ```
  {"battery":23,"device_id":"...","state":"isDocked","timestamp":1779436831492}
  ```
  Every push has one; the replay carries the timestamp of the
  originally-emitted state (i.e. older than the fresher-value we
  currently hold).
- **Fix — drop-if-older in `_handle_state`.** Compare
  `state.timestamp` to `self._last_state.timestamp`; if strictly less,
  drop with a DEBUG line. Equal / missing timestamps fall through
  (defensive for firmwares that omit the field). The isinstance check
  ensures MagicMock-shaped test states are treated as "no
  timestamp" (fall-through). No mutation of any clock on drop, so the
  BUG-01 staleness logic keeps firing at the right time.

## Open questions

- **Is the replayed payload also acknowledged by the cloud with an ACK
  we could exploit differently?** Not observed in the wire capture. The
  timestamp-diff fix is sufficient in practice.
- **Does the same behaviour affect `/attributes` on other Navimow
  models?** On i210 the `/attributes` topic never fires
  (BUG-03 diag [#21](https://github.com/raouldekezel/NavimowHA/pull/21)),
  so the parallel fix in `_handle_attributes` would be latent-preventive
  only. Deferred.

## Refs

- Issue [BUG-05 #29](https://github.com/raouldekezel/NavimowHA/issues/29).
- Related fixes: BUG-04 (`sdk.get_cached_state` guard, PR [#14](https://github.com/raouldekezel/NavimowHA/pull/14))
  — orthogonal to BUG-05, both are needed.
- FEAT-03 diag [#23](https://github.com/raouldekezel/NavimowHA/pull/23)
  — the 40-min reconnect cadence that BUG-05 mitigates the fallout of.
- Upstream issue [segwaynavimow/NavimowHA#11](https://github.com/segwaynavimow/NavimowHA/issues/11)
  — @stefan73's original diagnosis noting "MQTT state updates … may
  contain an old battery value".
