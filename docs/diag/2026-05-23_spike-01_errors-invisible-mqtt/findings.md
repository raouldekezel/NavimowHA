# SPIKE-01 — operational errors on i210 are invisible on MQTT

## TL;DR

Two independent operational incidents on 2026-05-22 → 2026-05-23
produced **zero MQTT payloads** despite requiring either manual
intervention (tangled net) or a 13× longer than normal path (deliberate
obstacle). The i210 firmware does not surface surmountable navigation
difficulties on the `/state`, `/event`, or `/attributes` channels; a
future HA "stuck detection" needs a client-side heuristic on top of
`sensor.<slug>_position` + `vehicleState` — the firmware won't do it
for us.

## Context

- Date: 2026-05-22 13:30 CEST (tangled net) + 2026-05-23 14:31 CEST (obstacle test)
- Robot: Segway Navimow i210 LiDAR Pro (`REDACTED-ROBOT-SERIAL`)
- Integration: `NavimowHA v1.1.0 + BUG-01/02/03 + FEAT-01` (patches applied
  2026-05-22 19:00 CEST; obstacle test 2026-05-23 is post-patch, tangled-net
  incident 2026-05-22 13:30 is pre-patch — the MQTT-silence observation
  survives either way).
- HA: 2026.5.x (Docker on intel-nuc)

## Actions taken

1. `01_tangled-net-3h47-gap.mqtt.log` — proof-by-absence: state messages
   before 12:00 UTC and reconnect callbacks throughout the 3 h 49 min
   window covering the tangled-net incident. Zero `state` messages
   captured on the wire between 12:00 and 15:49.
2. `02_brol-terrasse-vehicleState-5.mqtt.log` — proof-by-presence:
   during the deliberate obstacle test the /location channel keeps
   emitting `vehicleState=5` (returning) payloads at the normal ~2 s
   cadence, but with no error/state transition to indicate difficulty.

## Timeline

### Incident 1 — tangled net (2026-05-22, pre-patch)

| Local time | Event |
| --- | --- |
| 12:00:32 CEST | Last `MQTT state received: state=docked battery=68` payload — robot had just docked normally. |
| ~13:30       | Operator observes robot physically caught in a garden net, mid-run. Manual intervention required to free it. |
| 12:56, 13:52, 14:48, 15:44 CEST | 4 reconnect callbacks fire (nominal 56-min cadence) but each reconnect brings **no new state payload**. |
| 15:49:26     | First fresh `MQTT state received` after the incident — robot back on dock post-manual-recovery. |
| **Gap: 3 h 49 min** with **zero `state` payloads**, zero `event`, zero `attributes`, zero notification. |

### Incident 2 — deliberate obstacle (2026-05-23 14:31, post-patch, FEAT-01 running)

| Local time | Event |
| --- | --- |
| 14:31:55 CEST | Robot enters `vehicleState=5 returning`, `battery=48`. Normal path to dock ~1 min. |
| 14:31 → 14:45 | Continuous `/location` type-1 payloads at ~2 s cadence. `postureTheta` pivots erratically (searching for a passage), `postureX/Y` explore the negative X quadrant (not the direct dock line). |
| 14:45:24     | Robot re-docks: `vehicleState=2, battery=41` — 13 min 30 s of detour, no error emitted. |

## Findings

- **Neither incident produced any \`/state\`, \`/event\`, or \`/attributes\`
  payload attributable to the problem.** In both, the /location
  channel kept telemetry flowing (per-2-s posture); in neither did the
  firmware raise an alarm.
- **The tangled-net case was a total-blocking event** (required manual
  freeing). Yet the firmware still did not publish an `isStuck` /
  `isLifted` / `Error` payload. Possibly because manual intervention
  freed the robot before the firmware's own timeout, but even so:
  we cannot rely on such a payload as a canary.
- **The obstacle test showed the firmware perseveres silently** — the
  same `vehicleState=5` continuously for 13 min 30 s of no-progress-
  toward-dock. From HA's point of view, the robot was "returning
  normally"; from reality, it was struggling.
- **Implication: any "stuck detection" HA automation must be a
  client-side heuristic**, not a firmware-triggered event. FEAT-01
  now surfaces the required inputs (`x`/`y` from position sensor,
  `vehicleState` attribute) so a template sensor like
  `binary_sensor.<slug>_probably_stuck` could fire on "\`vehicleState=5\`
  for >5 min without distance-to-dock trending toward zero". Not
  implemented in this fork — filed as **SPIKE-01** so a future FEAT
  can pick it up.

## Open questions

- **Does the firmware emit ANYTHING on a true give-up-and-park state?**
  Would need to be captured on a hard-fault incident (deep stuck, blade
  jam, empty battery mid-run). Not reproduced here. The Journal notes
  that the operator's Segway app receives the notifications, so the
  path exists — probably FCM/APNS out-of-band rather than MQTT.
- **Is the /location \`action\` field a reliable stuck signal?** Values
  observed: `-1` (idle), `5` (mowing?), `8` (?). During the obstacle
  test, `action` was not systematically tracked. Deferred.

## Refs

- Issue [SPIKE-01 #24](https://github.com/raouldekezel/NavimowHA/issues/24).
- Local doc: `Home Assistant - Navimow - Journal.md` § « 2026-05-22 ~13:30 — Incident filet »
  and § « Test obstacle (brol terrasse) 2026-05-23 14:31-14:45 — comment remontent les erreurs ? ».
- Related: BUG-01 diag [#22](https://github.com/raouldekezel/NavimowHA/pull/22) covers the
  tangled-net gap from the "throttle" angle; this session covers the
  same gap from the "no error emitted" angle.
