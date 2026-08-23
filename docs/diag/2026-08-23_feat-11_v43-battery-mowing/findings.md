# FEAT-11 diag #2 — battery updates during a *mowing* run under OS V4.3.0

## TL;DR

**H1 confirmed** — the mowing freeze has the **same root cause as the dock**: under
V4.3.0 the MQTT `/state` topic pushes the *correct, changing* battery frequently,
BUG-08 discards every value (`held_battery` never moves), and the fresh `/state`
starves the HTTP fallback — the only path allowed to write battery. Refinement the
capture adds: `/state` cadence is **battery-movement-gated** (sparse while the level
is flat, frequent the moment it changes), so HTTP runs *only during flat periods* and
is starved through *every* excursion. HTTP is **starved, not stale** — the earlier
"stale-HTTP-endpoint" hypothesis is refuted. Across a mow to app-95 % and a partial
recharge, the wire battery traced a full **100 → 94 → 100** V while HA stayed pinned
at **100** the entire time.

## Question

Since OS **V4.3.0**, HA's battery is frozen both while charging and while mowing
(FEAT-11 #136). The dock/charging mechanism was already captured and confirmed
(below). The **mowing** mechanism was not: on 2026-08-21 the percentage stayed at
100 % for a ~1h46 run then jumped to 20 % at docking. This session captures a live
partial mow + partial recharge to determine exactly what happens to battery updates
while the robot is mowing.

## Context

- **Date:** 2026-08-23, ~19:36–20:08 CEST (+02:00).
- **Robot:** i210 LiDAR Pro (Razibus), OS **V4.3.0**. Pre-run battery 100 %.
- **Integration:** deployed HACS build (manifest `1.1.0`; coordinator predates
  CHORE-03 #134 — comment vintage confirms) **plus this session's 3-line,
  behaviour-neutral instrumentation applied directly to the running
  `coordinator.py`** (see below). Not committed. BUG-08 preserve
  (`replace(state, battery=prev_battery)`) present on both the push and cache paths,
  verified in the running file.
- **HA:** running on intel-nuc (`hass` container), Python 3.14 per #136.
- **Loggers @ DEBUG:** `custom_components.navimow.coordinator` (the four battery
  lines) and `mower_sdk.mqtt` (raw `/state` payload). Set persistently in
  `configuration.yaml`; single `docker restart hass` loaded code + levels at 19:26.
- **Instrumentation under test (logging only):**
  1. `_update_from_state` — new DEBUG `MQTT state applied: … incoming_battery=… held_battery=…`
  2. HTTP success — extended INFO `HTTP fallback succeeded … (MQTT stale): battery=…`
  3. Coordinator tick — extended DEBUG, added `battery=` (held) to the existing
     `source=/mqtt_ts=/mqtt_state_ts=/http_ts=` line.
  The pre-existing `MQTT state received: … state=… battery=…` DEBUG supplied the
  incoming-per-`/state` requirement unchanged.

## Actions taken

1. **`01_state-and-battery-path.mqtt.log`** — the navimow+mower_sdk record-filtered
   slice of `docker logs hass`, further reduced (grep) to the battery path only —
   `realtimeDate/state payload`, `MQTT state received`, `MQTT state applied`,
   `HTTP fallback succeeded|failed`, `Coordinator update`. The ~950 `/location`
   pose/heartbeat lines in the same window are omitted (irrelevant to battery,
   voluminous). ANSI-stripped; **PII-redacted** (robot serial, MQTT userid,
   MQTT client-id).
2. **`02_ha-sensor-poll.sensors.tsv`** — 15 s poll of
   `sensor.<slug>_batterie` / `lawn_mower.<slug>` / `binary_sensor.<slug>_en_charge`
   across the whole window (mow + return + recharge). 84 rows.

## Timeline (CEST)

| Time | Event | Evidence |
| --- | --- | --- |
| 19:26 | HA restart with patched code + DEBUG loggers | first `HTTP fallback … battery=100` |
| ~19:40 | Mow launched (`lawn_mower → mowing`) | TSV |
| 19:37–19:49 | Battery flat at 100: `/state` **sparse**, **HTTP fires ~every 60–90 s**, all return **100** (correct) | 9 HTTP events 19:37→19:49:30 |
| 19:43:15 | First `/state` burst (3×), `battery=100` | log |
| **19:49:32** | **Real battery starts dropping**: `/state` `battery=99`, `MQTT state applied incoming=99 held=100` | log |
| 19:49:32 → 19:58:40 | `/state` turns **frequent** (~30–90 s), incoming **99→98→97→96→95→94**; every one discarded (`held=100`); **HTTP fires 0 times** | 14 applied lines; HTTP gap |
| ~19:56–19:57 | Operator returns mower (app ≈ 95 %); `returning` | TSV |
| 19:58:02 | Docked, `en_charge=on` | TSV |
| 19:58:40 → 20:05:11 | Recharge: `/state` incoming **94→95→96→97→98→99→100**; all discarded (`held=100`) | 7 applied lines |
| **19:49:30 → 20:07:11** | **HTTP fallback blackout — 17 min 41 s, zero fetches** across the entire excursion | HTTP event list |
| 20:07:11 | Battery back at 100 & flat → `/state` sparse again → state stale → HTTP resumes, returns **100** (correct) | last HTTP event |
| whole window | `sensor.<slug>_batterie` = **100** for all 84 rows | TSV (only distinct value) |

## Observations

- **MQTT `/state` incoming battery (wire truth), `held_battery` throughout:**
  discharge `100→99→98→97→96→95→94`, then charge `94→95→96→97→98→99→100`. A clean
  **V, 100→94→100**. `held_battery=100` on **all 23** pushes.
- **HTTP fallback:** 10 events, **all returned `battery=100`**. Nine were 19:37–19:49
  (battery genuinely 100) and one at 20:07 (battery back to 100). **None during the
  excursion** — a 17 m 41 s blackout from 19:49:30 to 20:07:11.
- **Coordinator ticks:** 19 `source=http_fallback` (flat periods) / 29
  `source=mqtt_cache` (movement periods, HTTP starved). `battery=100` on every tick.
- **App ≈ 95 %, MQTT min 94, HA 100** — MQTT and the app agree; HA alone is wrong.

## Findings

- **H1 is the mowing mechanism** (`01_…mqtt.log`, 19:49:32 onward): fresh, correct,
  frequent `/state` battery is received and **discarded by BUG-08**
  (`incoming_battery=99..94 held_battery=100`), while its freshness holds
  `mqtt_state_ts` current and **starves HTTP**. Identical to the confirmed dock
  mechanism.
- **HTTP is starved, not stale** — the discriminator. From 19:49:30 to 20:07:11 the
  HTTP endpoint was *never queried* (17 m 41 s gap), so it could not "return a stale
  value". When it *did* run (flat battery), it returned the correct figure every
  time. The "additional V4.3.0 stale-HTTP-endpoint" branch of the FEAT-11 H2 tail
  does **not** occur.
- **New, load-bearing detail — `/state` cadence is battery-movement-gated.** `/state`
  is sparse while the level is flat (HTTP fires and is correct) and turns frequent the
  moment the level changes (HTTP starved). So the one path permitted to write battery
  is available exactly when the battery is *not* moving and unavailable through every
  transition. HA can therefore only ever latch a value sampled during a flat period —
  here the pre-mow 100, held until the level returned to 100.
- **This fully explains the 2026-08-21 report.** Our short run returned to 100 (== the
  latched value) so there was no visible jump, but scale the excursion to 100→20: the
  wire `/state` traces 100→20 (all discarded), HTTP starved throughout → HA frozen at
  100 for the whole run; at dock a brief flat/stale window lets one HTTP fetch read
  the real ~20 → the 100→20 jump; charging then moves the level again → `/state`
  frequent → discarded → HTTP starved → HA stuck at 20 while really charging. Matches
  #136 exactly.
- **Instrumentation behaviour-neutral, confirmed in vivo.** Battery path, HTTP timing,
  `MQTT_STALE_SECONDS`, freshness logic, tracker and cadence all unchanged; the run,
  return and charge proceeded normally and BUG-08 kept discarding as designed.

## Open questions

- Exact firmware trigger for the `/state` cadence switch — is it strictly a battery-%
  delta, or any changing telemetry? (Observed: cadence rose exactly as the % began to
  move.)
- On a deep run (→ ~20 %), is the single HTTP read that produces the dock "jump"
  reliably present, or can it be missed entirely (HA never leaving 100)?
- Does an over-discharge still ever put a sentinel (`battery=0`) on `/state` under
  V4.3.0 — the original BUG-08 motivation — or is that premise fully gone? (Must be
  checked before weakening the discard; see #45.)

## Conclusion

The mowing symptom and the charging symptom are **one bug, one root cause (H1)**:
V4.3.0 made `/state` battery reliable and frequent-when-moving, so BUG-08's
unconditional discard now throws away the only timely source and simultaneously
starves the HTTP fallback it defers to. Fixing FEAT-11 means restoring a battery
source that is live *while the level moves* — e.g. trust fresh `/state` battery
(guarded against the historic sentinels) or give battery its own max-staleness that
forces HTTP regardless of `/state` freshness (#136 "Suggested direction"). **No fix
implemented here** — this session is diagnosis only, and the over-discharge premise
(open question 3) must be validated first.

## Refs

- FEAT-11 #136 — parent issue (dock mechanism confirmed; this session confirms the
  mowing mechanism and unifies the root cause).
- BUG-08 #45 — the battery-preserve whose premise V4.3.0 inverts.
- SPIKE-04 #135 — V4.3.0 zone-selection / continue-vs-restart (separate).
- Old-firmware mowing contrast: `2026-07-03_bug-07_progression-battery-trace`
  (then: `/state` sparse, HTTP carried battery correctly — pre-inversion).
- Battery-clobber lineage: BUG-04 `2026-05-25_bug-04_battery-flicker`, BUG-05
  `2026-07-02_bug-05_stale-mqtt-replay-at-reconnect`.
