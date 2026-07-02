# FEAT-01 — Phase 1 initial deployment smoke test

## TL;DR

The MQTT WSS topic `/downlink/vehicle/<serial>/realtimeDate/location`
delivers a JSON array of items, of which `type=1` carries the vehicle
pose (postureX/Y/Theta + vehicleState). The upstream SDK does not
subscribe this topic; the override in `_attach_mqtt_debug_hooks`
successfully routes it to the coordinator without patching the pip SDK,
and the position sensor renders end-to-end.

## Context

- Date: 2026-05-23, ~23:00 CEST
- Robot: Segway Navimow i210 LiDAR Pro (`REDACTED-ROBOT-SERIAL`)
- Integration: local build of `NavimowHA v1.1.0 + BUG-01/02/03/04`,
  Phase 1 changes applied (equivalent to this PR).
- HA: 2026.5.x (Docker on intel-nuc)
- Pre-experiment state: robot on the dock, **base disconnected from
  mains** (see BUG-01 session for the context of the unplugged base);
  robot at 36 % battery draining at ~13 W idle.

## Actions taken

1. `01_deployment-smoke-test.mqtt.log` — first observed payloads after
   the Phase 1 code was deployed and the config entry reloaded.

## Timeline

| Local time | Event |
| --- | --- |
| 23:00:04 | First `/location` payload received: `postureTheta=3.045`, `postureX=-0.056`, `postureY=0.09`, `vehicleState=3`. Distance to station 0.12 m. |
| 23:00:04 | Type-3 heartbeat also observed (`{"time":..., "type":3}`) — 33 bytes, no pose, correctly ignored by the router. |
| 23:00:06 | Second heartbeat; no fresh type-1 (robot stationary). |

## Findings

- The WSS broker (`mqtt-fra.navimow.com:443`) accepts the subscription
  on `/location` with `rc=0`. No ACL rejection observed.
- The payload envelope is a JSON array. The SDK's `_on_message` was
  the right hook to override: replacing `mqtt.on_message` intercepts
  the payload after MQTT client decode but before the SDK's dispatch,
  and lets us decode and route type-1 items to the coordinator's
  `handle_location_item`.
- `vehicleState=3` in this session corresponds to "docked, no charge"
  (base was unplugged). Confirms via contrast with `vehicleState=2`
  which is documented as "charging" — the two are distinguishable on
  the /location channel but not on the SDK's /state channel (both
  report `isDocked`).
- Coordinator throttle: `postureX/Y` did not change (robot idle), so
  only one dispatch fired per scan interval — no dispatcher storm.
- `sensor.<slug>_position` populated end-to-end with `state=0.12 m` and
  attributes `x=-0.056`, `y=0.09`, `theta=3.045`, `vehicle_state=3`
  (verified in HA state list, not included in the log slice).

## Open questions

- Type-2 payloads (mowing stats) were not observed here — the robot
  did not mow. FEAT-02 session should exercise the mowing path.
- Type-3 heartbeat cadence: seen twice at ~2 s interval, matches the
  expected cadence during idle. Not surveyed in depth.
- Type-4 taskDelay: not observed in this window.

## Refs

- Local doc: `Home Assistant - Navimow - Journal.md` § 2026-05-23 ~22:40
  "Phase 1 déployée + observation « base débranchée » (vehicleState=3)"
- Follow-up: FEAT-02 (mowing metrics on type-2 payloads).
