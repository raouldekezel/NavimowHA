# BUG-03 — the /attributes topic is empty on i210 (fix is preventive)

## TL;DR

Over 6.5 days of continuous MQTT observation on the i210 LiDAR Pro:
**478 `state` messages, 0 `attributes` messages**. The topic
`/downlink/vehicle/<serial>/realtimeDate/attributes` — which BUG-03's
upstream PR #60 identified as the vector for the "attributes-pollute-
freshness-clock" race — never emits a payload for this model. The
BUG-03 fix (separate `_last_mqtt_state_update` clock) is therefore
**latent-preventive** on the operator's setup; it becomes load-bearing
on any Navimow model whose cloud does push `/attributes` (older Segway
firmware branches, other product lines).

## Context

- Date range: 2026-05-22 08:00 UTC → 2026-05-28 12:24 UTC (156 h)
- Robot: Segway Navimow i210 LiDAR Pro (`REDACTED-ROBOT-SERIAL`)
- Integration: local build of `NavimowHA v1.1.0 + BUG-01 + BUG-02 + BUG-03`
  (patches applied 2026-05-22 ~19:00 CEST)
- HA: 2026.5.x (Docker on intel-nuc)
- Pre-experiment state: normal operation across the window includes
  multiple mowing sessions (see FEAT-02 diag), a mid-window
  over-discharge event (see BUG-04 diag), a base unplug/re-plug cycle,
  and normal charging cycles.

## Actions taken

1. `01_state-messages-boundaries.mqtt.log` — first and last observed
   `MQTT state received` messages with an inline count summary.

## Timeline

| Time                       | Event |
| -------------------------- | --- |
| 2026-05-22 10:00 CEST      | First `state` message captured (post-patch reload). |
| 2026-05-22 10:00 → 2026-05-28 14:24 CEST | 477 further `state` messages (roughly one per minute on average, clustered around real device transitions and 40-min token-refresh reconnects). |
| — (never)                  | Zero `attributes` messages across the full window. |

## Findings

- **`_handle_attributes` is dead code on i210** — it exists in
  `coordinator.py` because `mower_sdk.on_attributes` fires it on any
  payload on the `/attributes` subscription, but that subscription
  never receives a payload on i210. Confirms the local memo note in
  `Home Assistant - Navimow.md` § « Ce qui ne remonte JAMAIS » that
  lists `/realtimeDate/attributes` as empty for this model.
- **The BUG-03 fix is preventive for our install, not observational.**
  With `_last_mqtt_update` (catch-all) never bumped by attribute
  packets, the freshness clock and the state clock evolve identically
  on i210 — the pre-fix behaviour would have been equivalent.
- **The fix is nonetheless useful in this fork**:
  - **Model-portability** — the fork is documented as the "official"
    Segway integration for HA. A user on an S-series or an older i2 AWD
    (which do push `/attributes`) inherits the fix by installing the
    fork.
  - **Contract clarity** — code that reads "state freshness" from a
    field explicitly named `_last_mqtt_state_update` is easier to reason
    about than one that reads it from `_last_mqtt_update` and assumes
    non-i210 semantics.
  - **Guardrail against Segway backfilling `/attributes`** — the roadmap
    comments on upstream issue #11 ("New MQTT status push strategy will
    be released soon") suggest the cloud may start pushing more on
    `/attributes` in the future. The fix ensures the guardrail is
    already in place.

## Open questions

- **Would a real S-series install reproduce the flicker fix effect?**
  Not answerable from our i210 dataset — would need a co-operator with
  an S-series to run a comparable window with the pre-patch and
  post-patch coordinator side by side.
- **Does Segway actually plan a `/attributes` push?** The maintainer
  comment on issue #11 is 2 months old and no release has followed.

## Refs

- Upstream PR [segwaynavimow/NavimowHA#60](https://github.com/segwaynavimow/NavimowHA/pull/60) (partial import, `coordinator.py` slice)
- Local BUG-03 fix in PR [#13](https://github.com/raouldekezel/NavimowHA/pull/13)
- Local doc: `Home Assistant - Navimow.md` § 3.2 « Ce qui ne remonte JAMAIS »
