# FEAT-03 — MQTT reconnect cadence motivates `cloud_connected` binary_sensor

## TL;DR

Two rates coexist. The SDK's token-refresh **interval** is ~56 min
(observed as six back-to-back 56-min cycles on 2026-05-22 afternoon),
but that interval only forces a reconnect when the broker actually
drops the socket — the SDK's `update_credentials` defers the client
rebuild while connected. The **observed disconnect rate** is much
lower: 26 disconnect callbacks over 6.5 days (~4×/day), spread
unevenly across the window. Whichever rate, every one of those
disconnects is invisible to end-user HA entities — the SDK reconnects
itself, so neither `lawn_mower.<slug>` nor `sensor.<slug>_batterie`
changes state across the event. FEAT-03's
`binary_sensor.<slug>_cloud_connected` surfaces this state directly,
enabling operator-side automations of the shape "notify if cloud has
been off for N minutes".

## Context

- Date range: 2026-05-22 09:56 CEST → 2026-05-28 12:24 CEST (156 h)
- Robot: Segway Navimow i210 LiDAR Pro (`REDACTED-ROBOT-SERIAL`)
- Integration: mixed — `NavimowHA v1.1.0` vanilla in the morning of
  2026-05-22, then local build of `NavimowHA v1.1.0 + BUG-01/02/03/04`
  from ~19:00 CEST onwards. The reconnect **cadence itself is
  unaffected by the fix** (it comes from the broker); only the amount
  of stale time between the disconnect and the next successful
  HTTP fallback differs.
- HA: 2026.5.x (Docker on intel-nuc)

## Actions taken

1. `01_reconnect-cycles.mqtt.log` — 6 disconnect/reconnect cycles
   captured in the first 6 h of observation, plus an inline count for
   the full 6.5-day window.

## Timeline (representative sample)

| # | Disconnect | Reconnect | Δ from prior | Note |
| --- | --- | --- | --- | --- |
| 1 | 09:55:57 CEST | 09:55:58 CEST | — | First observed after debug logging enabled |
| 2 | 10:07:34 | 10:07:34 | 11 min | Rapid — see BUG-01 diag for the fallout |
| 3 | 11:03:42 | 11:03:43 | 56 min | ⭐ Nominal cadence |
| 4 | 11:59:53 | 11:59:54 | 56 min | ⭐ |
| 5 | 12:56:04 | — | 56 min | ⭐ |
| 6 | 13:52:23 | — | 56 min | ⭐ |
| 7 | 14:48:42 | — | 56 min | ⭐ |
| 8 | 15:44:57 | — | 56 min | ⭐ |
| … | | | | |
| 26 in total, 2026-05-22 to 2026-05-28 | | | | |

## Findings

- **Token-refresh interval: 56 min.** Reproduced across 6 consecutive
  cycles on 2026-05-22 afternoon — the SDK triggers a credential
  refresh at that interval, which the broker's `rc=7` acknowledges.
  Matches the Journal's narrative note ("~56 min, rc=7 = token MQTT
  expiré, le SDK rebuild les credentials"). Each refresh does NOT
  automatically drop the socket, however — see the observed disconnect
  rate below.
- **Observed disconnect rate: ~4/day.** 26 total across 6.5 days, not
  the ~160 a sustained 56-min cycle would produce. The six back-to-back
  56-min cycles were one afternoon window; the rest of the log is
  quieter. Consistent with `mower_sdk.mqtt.update_credentials`
  deferring the client rebuild while connected — the observed
  disconnects fire only when the broker actually drops the socket.
- **Cross-diag** — the [BUG-05 diag (2026-07-02, #31)](https://github.com/raouldekezel/NavimowHA/pull/31)
  measured a clean 40-min cadence on the same install. The
  disconnect-forcing interval appears to have shortened by ~15 min
  between May and July — worth confirming whether that's a broker-side
  change or an SDK version delta.
- **Reconnect is fast** — median ~1 s from disconnect callback to
  `MQTT ready callback`. Below the coordinator tick (30 s), so the
  entities show no state change.
- **Rapid cascades happen.** Same-log 2026-05-22 21:14 → 21:19 shows
  4 disconnect callbacks in 5 min. These are transient (all recovered
  within seconds) but each opens a small window where the coordinator
  serves stale data.
- **Nothing in the current entity set surfaces this state.** Before
  FEAT-03, an operator wanting to know "is the cloud up right now?"
  had to grep the debug log. Battery/state fields are cached, so their
  freshness is not a proxy for connectivity — a docked robot with a
  cloud outage looks identical to a docked robot with the cloud up
  (state stays `docked`, battery stays on the last-pushed value).
- **FEAT-03's `is_on_fn = coordinator.sdk.is_connected` maps exactly
  to the disconnect callback.** Between callback firings, the SDK's
  own internal state (`is_connected`) tracks the WSS session; the
  binary_sensor thus flips on the same wire events surveyed here.

## Open questions

- **Why 56 min?** Not 60. Suggests a token TTL of 3600 s minus a
  server-side margin. Not needed to justify the entity; noted for a
  future upstream question.
- **Rapid cascade root cause** (21:14 → 21:19): 4 disconnects in 5 min
  did not correlate with any observable operational event. Possibly
  the broker rate-limiting after a client-side hiccup. Deferred.

## Refs

- Local FEAT-03 in PR [#15](https://github.com/raouldekezel/NavimowHA/pull/15).
- Sibling BUG-01 diag [#22](https://github.com/raouldekezel/NavimowHA/pull/22): the same disconnects, seen from the "how long is the fallback throttled" angle.
- Local doc: `Home Assistant - Navimow - Journal.md` § « 2026-05-22 11:03 — Disconnect/reconnect automatique du SDK ».
