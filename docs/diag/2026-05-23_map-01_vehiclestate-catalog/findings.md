# MAP-01 — `vehicleState` catalog + firmware `isIdel` typo

## TL;DR

The `/location` `vehicleState` field carries **7 distinct values** on
i210 (1–6 as documented in the local MQTT.md reference, plus an
uncatalogued `8` observed twice). The `/state` `state` field emits
`isIdel` — a firmware **typo** for `isIdle` — in every idle payload
(342 occurrences observed, zero `isIdle` correct spellings). The SDK
normalizes both spellings, and consumers should trust the SDK's
`idle` normalization rather than the wire spelling.

## Context

- Date range: 2026-05-22 07:55 UTC → 2026-05-28 12:24 UTC (156 h continuous)
- Robot: Segway Navimow i210 LiDAR Pro (`REDACTED-ROBOT-SERIAL`)
- Integration: `NavimowHA v1.1.0 + BUG-01/02/03/04 + FEAT-01`
- HA: 2026.5.x (Docker on intel-nuc)

## Actions taken

1. `01_vehiclestate-transitions-mowing-cycle.mqtt.log` — sampled
   `/location` payloads spanning a real 2026-05-23 mowing cycle
   (`4→5→2→1`) plus a docked-but-unpowered observation (`3`).
2. `02_vehiclestate-8-uncatalogued.mqtt.log` — the two occurrences of
   the mysterious `vehicleState=8` on 2026-05-26 04:56 UTC (nighttime).
3. `03_isIdel-typo-samples.mqtt.log` — `/state` payloads with the
   `state="isIdel"` typo + the corresponding coordinator log line
   after SDK normalization to `state=idle`.

## Timeline of vehicleState occurrences over 156 h

| `vehicleState` | Count | Interpretation |
| --- | --- | --- |
| `1` | 903 | Docked, battery full, no charge |
| `2` | 205 | Docked, **actively charging** |
| `3` | 171 | Docked, **base unpowered** (no charge current) |
| `4` | **20 438** | Mowing (dominant during runs, per-2-s cadence) |
| `5` | 1 052 | Returning to dock |
| `6` | 10 | Paused |
| `8` | **2** | Uncatalogued — see below |

## Findings

### `vehicleState=8` — uncatalogued state observed only at 04:56 UTC on 2026-05-26

The two `vehicleState=8` payloads share a striking signature (see
`02_vehiclestate-8-uncatalogued.mqtt.log`):

```
postureTheta=0.0, postureX=0.0, postureY=0.0, vehicleState=8
```

All three posture coordinates are exactly `0.0` — a value never
observed on `1`/`2`/`3`/`4`/`5`/`6` — and both messages arrived within
milliseconds of each other in the middle of the night. Strongly
suggests a **firmware reset / uninitialized state** payload emitted
briefly at power-on or after an internal crash. Not correlated with
any operator activity in the Journal. Hypothesis: firmware `poweron`
transient before the LiDAR / SoC settle on a real posture. Not
critical to expose in the entity; worth defending against in the
`is_on_fn` for the charging binary_sensor and in any downstream
consumer of `postureX/Y`.

### `isIdel` firmware typo (342 occurrences, 0 corrections)

Every idle payload in 6.5 days uses `isIdel` (missing the `l`).
`isIdle` (the correct spelling) is **never** emitted by this firmware.
The SDK's `mower_sdk.models` normalizes both spellings to `idle`
before the coordinator sees the payload, so `_handle_state` reads
`state.state == "idle"` in either case. Confirmed by the coordinator
log lines showing `MQTT state received: state=idle` post-normalization.

**Impact for consumers**: never grep `isIdle` in raw logs; use `isIdel`
if you need the wire representation. In code, rely on the SDK enum
(`state == "idle"`) which handles both.

### State frequency skew

`vehicleState=4` (mowing) is 20× more frequent than the next most
common state (`1` at 903 occurrences). This is expected: the /location
channel emits at ~2 s cadence during mowing, so a single 1-hour mow
generates ~1800 payloads while a full day of docking generates ~500.
Consumers should not treat frequency as a proxy for total wall-clock
time in each state.

## Open questions

- **Is `vehicleState=8` reproducible?** Would need a controlled
  power-cycle of the robot. Not attempted here.
- **Do the S-series (X315, i2 AWD) emit `isIdle` correctly, or is the
  typo firmware-wide?** Unknown without a second-model dataset.
- **Are there more states in the firmware's enum?** The SDK's
  `MowerStatus` enum values catalog is worth reading against this
  finding to check for gaps.

## Refs

- Issue [MAP-01 #25](https://github.com/raouldekezel/NavimowHA/issues/25).
- Local doc: `Home Assistant - Navimow - MQTT.md` § 5 `vehicleState` mapping.
- Local doc: `Home Assistant - Navimow - Journal.md` § « Nouveau state
  découvert : isIdel (typo isIdle manqué !) que le SDK normalise vers idle ».
