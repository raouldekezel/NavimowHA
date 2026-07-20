# BUG-17 bypassed on `STATE_COMPLETED`-restored tracker at run start

## TL;DR

Manual mow started on Prunier (boundary `1`) with tag `NavimowHA-v1.1.0-raoul.22`
installed via HACS. The firmware emitted the expected task-end vestige
(`action = -1, mp = 100, cmp = 10000, sub = "0.0", wk = "357.63"`) as the
first `type-2` after the `docked → mowing` transition — **identical shape to
the 2026-07-19 diag that motivated BUG-17 (#105)**. The BUG-17 guard did not
fire and the tracker opened the fresh run anchored on the vestige's fields
(`start_time = 1784539314431 ms = 2026-07-20T09:21:54.431Z`,
`sub₀ = 0.0`, `wk₀ = 357.63`, `mow_start_type = 1`), then
`_update_zone` seeded `zones[0].cmp_max = 10000` — the full pathology, exactly
as in raoul.19.

Root cause of the bypass: at the moment the vestige arrived, the tracker
was in **`STATE_INTERRUPTED`/`STATE_COMPLETED` restored from Store**, not
`STATE_IDLE`. The BUG-17 guard's arming window is
deliberately dark in post-close states (per #105 fifth-edit body: "the
post-close zero-`sub` vestige variant is BUG-13 territory (#86),
instrumented via #92's observability hook, out of scope here"). The packet
flowed through the post-close `is_reset` branch (0.0 < prev `last_sub` of
357.78 m², `incoming_sub < RESET_SUB_CEILING = 10.0`) → `_open_run(vestige)`
+ `run_started` emitted, seeding the fresh run entirely on the vestige.

Verified separately: the deployed `_gate_run_start_vestige` in
`/config/custom_components/navimow/run_tracker.py` (mtime 11:19:15 CEST,
pyc mtime 11:20:28, HA process started 11:20:30) is byte-identical to
raoul.22; a Python one-liner run inside the `hass` container with the exact
wire payload confirms the guard **would** drop it if the tracker were
`STATE_IDLE`.

Also verified: **no observability line was emitted on this event**. The
BUG-17 "suspicious shape" DEBUG is only wired inside the arming window
(dark post-close), and #92's promised observability hook does not exist
in the deployed source (issue #92 is still OPEN; no PR has landed for
BUG-16 / BUG-13 instrumentation). The event went silent.

**Blast radius on today's session** (per `03_store_after_event.json` and
`02_run_close.mqtt.log`): run closed as `interrupted`, `session_area = 9.96 m²`
(vs the real ~7-9 m² actually mowed), `zones[0].cmp_max = 10000` written to
`history[]` for boundary 1, `Store.last_cmp_max` overstated (persisted
alongside the previous already-poisoned `2026-07-19` Prunier record). The
FEAT-08 `sensor.<slug>_zone_1_surface.last_complete_pass_at` will be
stamped at the vestige's `time` again on the next ingest for that boundary.

## Question for triage

Is this occurrence:

- **a bug in BUG-17's spec** — the guard should have covered
  `STATE_COMPLETED` / `STATE_INTERRUPTED` in the first place, because
  Store-restore is the dominant real-world path from a previous close to
  the next fresh mow (twice-a-week operator rhythm, HA restarts,
  container restarts, HACS updates all reset the process but preserve the
  Store);
- **a duplicate of BUG-13 (#86)** — the shape (`sub = 0` post-close →
  reset + reopen from the vestige) matches BUG-13's chain, and #105
  explicitly delegated this variant to #86;
- **something else** — a distinct pathology (e.g. the discriminator is
  not `STATE_COMPLETED` vs `IDLE` but the *reset-branch* itself, in
  which case the same shape landing on a genuine live tracker in
  `STATE_INTERRUPTED` from a same-day recharge could exhibit different
  behavior).

## Context

- **Date**: 2026-07-20 (Europe/Brussels, UTC+02:00; times below in local).
- **Robot**: Segway Navimow i210 LiDAR Pro (Prunier zone, boundary `1`).
- **Fork tag installed**: `NavimowHA-v1.1.0-raoul.22` (commit `49e38ef` on
  `deploy`, HACS-Redownloaded at 11:19:15 CEST). Verified byte-identical to
  the tag by direct `grep`/`sed` on the deployed
  `custom_components/navimow/run_tracker.py`.
- **HA version**: 2026.1.3 (Docker container `hass` on `intel-nuc`; Python
  3.13).
- **HA process**: started 11:20:30 (Python `etime = 12:20` at the moment
  of investigation). HA bootstrap complete at 11:21:01; navimow domain
  setup at 11:20:28; MQTT connected 11:20:31.
- **DEBUG on**: `custom_components.navimow: debug`, `mower_sdk: debug`
  (unchanged from raoul.19 diag).
- **Pre-run tracker state**: restored from
  `/config/.storage/navimow.3KAAW2606K1874.run_tracker` at coordinator
  setup. The previous run (2026-07-19, closed as `completed`,
  `zones = [Prunier cmp_max=10000, Figuier cmp_max=10000]`,
  `session_area = 357.78 m²`) sits at `history[-2]` in the Store — see
  `03_store_after_event.json`. That prior close left `state ∈
  {STATE_COMPLETED, STATE_INTERRUPTED}` and `current_run.last_sub = 357.78`
  in the Store, which `restore()` re-hydrated on this restart.
- **Store restore mechanism**: `coordinator.py:183`
  `self.run_tracker.restore(tracker_snap)` called from
  `_async_restore_store` at coordinator init. See also `snapshot()` at
  `coordinator.py:223`, called on every `run_finished` and heartbeats
  during an open run.

## Timeline

All timestamps local (CEST, UTC+02:00). Raw payloads in
`01_vestige_and_first_run.mqtt.log`; Store snapshot in
`03_store_after_event.json`; close event in `02_run_close.mqtt.log`.

| Local (CEST)  | Event                                                                                                                                                     |
| ------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 11:19:15.651  | `custom_components/navimow/run_tracker.py` written to disk by HACS Redownload of raoul.22.                                                                |
| 11:20:28.150  | pyc compiled from raoul.22 source (`__pycache__/run_tracker.cpython-313.pyc`).                                                                            |
| 11:20:30.030  | HA Python process starts (`docker top hass -eo etime` at moment of investigation: 12:20).                                                                 |
| 11:20:38.120  | `homeassistant.components.lawn_mower` setup — navimow entities coming online.                                                                             |
| 11:21:01.768  | `homeassistant.bootstrap: Home Assistant initialized in 37.02s`.                                                                                          |
| 11:21:05.280  | Post-init tick — coordinator `_async_restore_store()` completed by this point; tracker state restored from Store (details in `03_...json`).               |
| 11:21:52.882  | Operator invokes `lawn_mower.start_mowing` on the HA entity (`Started mowing for device 3KAAW2606K1874`).                                                 |
| 11:21:54.223  | First `type-1` after start on `/location`: `vehicleState = 4` (mowing).                                                                                   |
| 11:21:54.244  | `/state` topic reports `state = isRunning, battery = 100`.                                                                                                |
| 11:21:54.382  | **Vestige `type-2` on `/location`**: `action = -1, boundary = 1, cmp = 10000, mp = 100, sub = "0.0", wk = "357.63", time = 1784539314431, mowStartType = 1`. Same shape as raoul.19 2026-07-19 vestige, mid-week (Sunday-week convention: 2026-07-20 is Monday, day 2 of the firmware week — `wk = 357.63` reflects the live week counter). |
| 11:21:54.382  | `run_tracker event: kind=run_started payload={'start_time': 1784539314431, 'mow_start_type': 1}` — the fresh run's `start_time` and `mow_start_type` are anchored on the **vestige's** fields. Same coordinator tick as packet arrival. |
| 11:22:50.383  | Second `type-2`: `action = 8, boundary = 1, cmp = 104, mp = 0, sub = "2.53", wk = "360.12"` — the real first packet of the fresh Prunier task, 56.0 s after the vestige (comparable to the 56.4 s gap in the raoul.19 diag). `cmp_max` stays at 10000 via `max(10000, 104)`. |
| 11:23:40.828  | 3rd `type-2`: `cmp = 213, mp = 1, sub = 5.15`.                                                                                                            |
| 11:24:50.234  | 4th `type-2`: `cmp = 319, mp = 2, sub = 7.71`.                                                                                                            |
| 11:25:25.584  | 5th `type-2`: `cmp = 412, mp = 2, sub = 9.96`. `current_zone_progress` sensor still 100.00 % throughout (`zones[-1].cmp_max = 10000`). |
| ~11:25 – 11:34| Operator observes 100 % / 100 % gauges; ends the run (mechanism not captured — likely dashboard-side pause or direct dock).                                |
| 11:34:54.705  | `run_finished` event: `result = interrupted`, `session_area = 9.96`, `zones = [{boundary_id: 1, cmp_max: 10000, sub_entry: 0.0, sub_exit: 9.96}]`. Written to `history[-1]` in the Store. |

## Findings

- **Guard code deployed correctly.** `/config/custom_components/navimow/run_tracker.py`
  contains `MP_TASK_END = 100`, `RUN_START_SUB_TOLERANCE = 0.5`, and
  `_gate_run_start_vestige` verbatim from the raoul.22 tag. The pyc
  (`__pycache__/run_tracker.cpython-313.pyc`, mtime 11:20:28) was
  compiled from that source. Only one `run_tracker.py` and one
  `__pycache__` exist under `/config` (verified with `find`).
- **Guard code works in isolation.** A live `docker exec hass python3 -c
  ...` importing the deployed module and feeding the exact wire vestige
  parsed via `parse_location_type_2` returns `events == []`,
  `state == STATE_IDLE`, `current_run is None`. The guard's `STATE_IDLE`
  disjunct fires and the drop path executes as designed.
- **The bypass is state-conditional, not code-conditional.** In
  production the tracker was restored to `STATE_INTERRUPTED` or
  `STATE_COMPLETED` (from the previous 2026-07-19 close in the Store)
  before the vestige arrived. The guard's arming disjuncts
  (`STATE_IDLE` or `state ∈ {RUNNING, PAUSED_DOCKED} ∧ zones == []`)
  both evaluate false in post-close states. The packet reached the
  `elif self.state in (STATE_COMPLETED, STATE_INTERRUPTED):` branch of
  `process_type2` (`run_tracker.py:490` in raoul.22), matched
  `is_reset = True` (0.0 < 357.78 = previous `last_sub`) with
  `incoming_sub < RESET_SUB_CEILING (10.0)`, took the immediate-reset
  path (`run_tracker.py:496-497`: `self._open_run(parsed);
  events.append(self._event_run_started())`) and anchored the fresh run
  entirely on the vestige's fields.
- **This is the exact shape #105 explicitly deferred to #86.** Fifth-edit
  #105 body ("Seam coverage map"): "A vestige-shaped packet (`cmp =
  10000 ∧ sub ≈ 0`) arriving **post-close** (`STATE_COMPLETED` /
  `STATE_INTERRUPTED`) is covered by neither this guard (window dark)
  nor BUG-16's (which requires `sub ≈ last_sub`, contradictory with
  `sub = 0`); it flows into the post-close `is_reset` branch — which is
  precisely BUG-13's (#86) 0-second-phantom pathology." The 2026-07-20
  event realises that seam exactly.
- **The 0-second phantom is present but ended up ~13 min because of the
  fresh continuation stream.** BUG-13 as described on #86 is a "0-second
  phantom" (open + same-frame close). Here the vestige opened a run and
  then the *genuine* mow's `type-2` stream extended it (`sub` climbed
  0.0 → 9.96, `mp` climbed 100 → 0 → 1 → 2), so the run stayed open until
  the operator ended it. Effect on labels: the vestige's `start_time` /
  `sub₀` / `mow_start_type` still contaminated the run; the poisoned
  `zones[0].cmp_max = 10000` still stuck; `session_area` reflected
  `last_sub - sub₀ = 9.96 - 0.0 = 9.96` instead of the real ~7 m² mowed.
  This is BUG-13's *mechanism* with a longer wall-clock — worth
  clarifying whether #86's spec covers this "extended phantom" variant
  or is genuinely 0-second-only.
- **`current_run_progress` gauge behaved as raoul.19 predicted, not as
  the `interrupted`-vestige open-question 1 hypothesis.** `mp` flashed
  100 on the vestige, was overwritten to 0 by the second packet 56 s
  later, then climbed monotonically. Sensor path unchanged versus the
  raoul.19 pathology; the raoul.22 guard's suppression of this flash
  (documented as one of the five symptoms) is not visible here because
  the guard was bypassed.
- **`current_zone_progress` gauge stuck at 100.0 % for the whole 13 min
  session.** Identical rendering to raoul.19: `zones[-1].cmp_max / 100`
  = 10000/100 = 100.0. The BUG-17 mechanism reproduced end-to-end on the
  operator's dashboard.
- **No observability line was emitted on this event.** Zero occurrences
  of `run-start vestige`, `run-start suspicious shape`, or any BUG-16
  hook string in the HA log between 11:21:00 and 11:35:00. Confirms
  independently:
  - the BUG-17 guard did not enter (arming window dark);
  - #92's promised BUG-16 / BUG-13 observability hook is not implemented
    in the deployed source (issue #92 OPEN, no PR merged — verified via
    `gh issue view 92` and `gh pr list --search "BUG-16"`).

  The `raoul.22` docstring's phrase "instrumented via #92's observability
  hook" is aspirational, not descriptive of the current state.
- **The BUG-14 fast-path interaction did *not* fire on this run.** With
  `zones[-1].cmp_max = 10000` on the poisoned zone and `mp` climbing
  slowly toward 2, the `mp ≥ 99 ∧ cmp = 10000 ∧ vs ∈ {1,2,3}` condition
  was not met — `mp` never reached 99. Had the operator let the run
  progress further and returned to dock with `mp ≥ 99`, BUG-14 would
  have closed as `completed` on a genuinely partial mow (documented
  latent interaction in #105, not observed live today).
- **Not a fluke of Store timing.** The Store save cadence is on
  `run_finished` and open-run heartbeats (`coordinator.py:603`,
  "heartbeat Store save while a run is open"). Both branches persist
  `state != IDLE`. For an operator whose rhythm is one mow every 2–3
  days with HA left running (or restarted for updates), the tracker
  will be in a `STATE_COMPLETED` / `STATE_INTERRUPTED` state at every
  next-mow start — never `STATE_IDLE`. The 2026-07-19 raoul.19 diag
  had `STATE_IDLE` only because that mow followed a 10-day pause with
  presumably a Store wipe (memory backup rebuild or manual delete —
  operator to confirm on #105 back-reference); routine operation
  will hit the post-close path.

## Open questions

- **Taxonomy**: bug on BUG-17 (missed scope), duplicate of BUG-13
  (covered by #86's chain), or a distinct pathology (extended phantom
  via continuation stream, not 0-second)? See "Question for triage"
  above. Resolution should fold in the review of #86's exact spec —
  is #86 strictly the *0-second same-frame close* case, or is it any
  vestige-poisoned run opened from `STATE_COMPLETED`?
- **Does the `STATE_INTERRUPTED` post-close path expose an even
  narrower window?** #86 documents post-restart MQTT reconnect as its
  trigger. Here HA fully restarted and the tracker restored *state* +
  `current_run` from Store. Is that architecturally the same path as
  #86's "cloud replay reaches process_type2", or a distinct
  Store-restore trigger?
- **BUG-16 (#92) discriminator revisited**: #92's spec says the
  BUG-16 vestige carries `sub ≈ last_sub` (post-close frozen). The
  2026-07-20 vestige carried `sub = 0.0` — the BUG-17 shape, delivered
  in the BUG-16 tracker state. Are BUG-16 and BUG-17 two *sub-shapes*
  of a single firmware behaviour ("late task-end replay after any
  fresh mow start on a `completed` boundary"), with the tracker's
  entering state being the only pathological discriminator? If so,
  the taxonomy is neither BUG-13 (that's the specific
  MQTT-reconnect-cloud-replay trigger) nor BUG-17 (that's the
  `STATE_IDLE` variant of the same firmware behaviour) — it's a third
  variant that shares BUG-16's tracker-side state and BUG-17's
  wire-side shape.
- **Should `restore()` demote closed states to `IDLE` on rehydrate?**
  A close event is terminal; there is no operational reason to preserve
  `state = COMPLETED` across an HA restart. If `restore()` set
  `state = STATE_IDLE, current_run = None` when the persisted state is
  `COMPLETED` or `INTERRUPTED` (keeping `history` for `last_run_zones`
  etc.), the BUG-17 guard would cover today's event as-is. **This is a
  solution direction, not a claim — depending on the triage outcome,
  it might belong to the follow-up issue, to #86, or to the FEAT-05c
  Store spec.** Out of scope for this diag.

## Files in this diag

- `01_vestige_and_first_run.mqtt.log` — HA log between 11:21:50 and 11:25:35
  (start-mow command, `vs = 4` transition, vestige packet, first
  four genuine `type-2` packets). PII redacted (device ID retained as
  it is not sensitive; MQTT credentials elided by HA at the source).
- `02_run_close.mqtt.log` — the `run_finished` event at 11:34:54.
- `03_store_after_event.json` — full `/config/.storage/navimow.3KAAW2606K1874.run_tracker`
  Store contents after the event, showing the two-entry `history[]`
  (2026-07-19 completed multi-zone run + 2026-07-20 interrupted
  vestige-anchored run) and the final tracker state
  (`state = interrupted`, `vehicle_state = 1`).
- `findings.md` — this file.

## Refs

- **#105 BUG-17** — the sibling issue. Guard specified and merged as
  raoul.22 (commit `49e38ef` on `deploy`). Fifth-edit body explicitly
  seam-mapped this occurrence to BUG-13.
- **#86 BUG-13** — the candidate duplicate. "0-second phantom run on
  post-restart MQTT reconnect (cloud replay reaches process_type2)".
  Open at the time of this diag.
- **#92 BUG-16** — the other sibling. Post-close phantom run opened by
  a *frozen-`sub`* late task-end packet. Open at the time of this diag.
  Its promised observability hook (referenced by #105 and raoul.22's
  docstring) is not implemented in the deployed source.
- Fork PR: [#111 BUG-17 fix](https://github.com/raouldekezel/NavimowHA/pull/111) merged 2026-07-19T22:36 UTC.
- Release: [NavimowHA-v1.1.0-raoul.22](https://github.com/raouldekezel/NavimowHA/releases/tag/NavimowHA-v1.1.0-raoul.22) published 2026-07-20T09:17 UTC.
- Source of truth inspected on this branch:
  - `custom_components/navimow/run_tracker.py:442-480` (raoul.22) —
    `process_type2` state-branch dispatch; post-close reset path at
    L490-497.
  - `custom_components/navimow/run_tracker.py:617-728` (raoul.22) —
    `_gate_run_start_vestige` with the two-disjunct arming window.
  - `custom_components/navimow/coordinator.py:166-197` (deploy) —
    `_async_restore_store` invoking `run_tracker.restore(tracker_snap)`.
  - `custom_components/navimow/coordinator.py:217-224` (deploy) —
    `snapshot()` persisted on `run_finished` and heartbeats.
