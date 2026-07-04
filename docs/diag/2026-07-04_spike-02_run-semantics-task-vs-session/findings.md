# 2026-07-04 — SPIKE-02 — run semantics: Navimow "task" vs user "session"

## TL;DR

Post-BUG-09 (#53) shipped and post-FEAT-05 (#43) in production, the
tracker's three `last_run_*` sensors do not agree with each other on
what "the last run" refers to, and their subject drifts under the
operator's feet the moment a real interrupt-then-resume cycle happens.
The root cause is a modelling gap the FEAT-05 SPIKE record already
flagged as non-blocking (open question #3): the Navimow firmware
models a `mp`-carrying **task** that persists across `interrupted`
closes; the operator models an activation-to-dock **session**. BUG-09
aligned the *closing* on session-shape (dock arrival) but left the
*opening* on task-shape. This SPIKE captures the observation, disaggregates
the two problems tangled together, restates the semantics of
`interrupted` post-BUG-09, enumerates fix options without committing
to one, and lists the DEBUG capture that would close the design.

## Context

- Fork tag: `NavimowHA-v1.1.0-raoul.8` still installed on HA at the
  time of the observation (raoul.9 published earlier the same day but
  not yet HACS-Redownloaded).
- Home Assistant: production instance on `intel-nuc` (Docker container
  `hass`).
- Robot: `REDACTED-ROBOT-MODEL` (i210 LiDAR Pro).
- Trigger: operator pressed RUN in the mobile app at ~14:17 CEST on
  2026-07-04, after the 07:41 → 11:25 UTC morning run was closed
  `interrupted` (2880 s) by the pre-BUG-09 sustained-timer path.
- Session logging: DEBUG on `custom_components.navimow.*` was **off**.
  Observation is HA state history plus the BUG-09 findings from earlier
  the same day (`docs/diag/2026-07-04_bug-09_paused-docked-mp-99/`),
  not a fresh packet trace.

## Observation

Post-RUN snapshot at ~15:04 CEST (about 47 min into the resumed cycle),
via `/api/states`:

```
sensor.razibus_etat_du_passage           = running
sensor.razibus_progression_du_passage    = 97
sensor.razibus_progression_de_la_zone    = 93.0
sensor.razibus_zone_courante             = #3
sensor.razibus_surface_hebdomadaire      = 1179.84
sensor.razibus_debut_du_dernier_passage  = 2026-07-04T07:41:58+00:00
sensor.razibus_duree_du_dernier_passage  = 2880
sensor.razibus_resultat_du_dernier_passage = interrupted
lawn_mower.navimow_i210_lidar_pro        = mowing
binary_sensor.navimow_i210_lidar_pro_en_charge = off
```

Salient last-changed timestamps:

- `etat_du_passage = running` last_changed **12:17:34 UTC**
  (14:17 CEST) — the transition INTERRUPTED → RUNNING at the moment
  the operator pressed RUN.
- `zone_courante = #3` last_changed **12:17:34 UTC** — the robot went
  straight to zone 3, first accepted type-2 already had
  `currentMowBoundary = 3`.
- `debut_du_dernier_passage = 2026-07-04T07:41:58+00:00` last_changed
  **12:05:14 UTC**. The change at 12:05 is unexplained by the
  observation alone (Home Assistant restart? unrelated `open_run`
  update? subject for a DEBUG capture).
- `duree = 2880`, `resultat = interrupted` last_changed 12:05:14 UTC
  too. These reflect the pre-BUG-09 close of the 07:41 → 11:25 run
  (which reached `mp = 99` but was labelled `interrupted` because the
  sustained-timer path hardcoded that label on raoul.8).

## Two problems tangled together

### Problem A — task vs session mismatch

The Navimow firmware carries `mowingPercentage` as a **task-scoped**
value: it does not reset on a mid-run pause or on user-initiated
RUN after an `interrupted` close. `subtotalArea` similarly climbs
monotonically across pause/resume within the same task. BUG-09's diag
established this from the 2026-07-03 controlled run: `mp` went 0 → 30
on zone #1, was manually docked, and the scheduler continued the same
task on 2026-07-04 with the first packet already at `mp = 48`, ending
around `mp = 99`.

The tracker's `_open_run` fires only on IDLE start or on a
`sub < RESET_SUB_CEILING` (10 m²) fresh reset. Neither path fires on
a user RUN after an `interrupted` close, because the firmware doesn't
reset `sub` — it continues. What fires instead is `_reopen_run`, which
keeps `current_run.start_time` at the *task*'s start (the morning
07:41:58 UTC in this case), not the *session*'s start (14:17 CEST when
the operator pressed RUN).

The operator's mental model is a session: activation → mowing →
(pause / recharge / interrupt) → resume → … → dock arrival. All of
that is one "run". BUG-09 aligned the *closing* on session-shape (dock
arrival marks a run end). The *opening* is still on task-shape (a task
starts when the firmware started counting `mp`).

`_run_progress = 97` on the resumed cycle is not a bug per se — the
firmware really does say `mp = 97` on the first packet of the resume.
It is honest data leaking a modelling choice the operator did not
sign up to.

### Problem B — `last_run_*` sensors don't share a subject

`_last_run_start_dt` in `sensor.py:52-64` prefers `open_run.start_time`
over `last_finished_run.start_time` when an open run exists.
`_last_run_duration` (`sensor.py:202-215`) and `_last_run_result`
(`sensor.py:219-237`) always read `last_finished_run.*`. During an
active run, the three sensors describe *two different runs* — Problem
A makes the "active" one the ongoing task rather than the current
session, but even setting Problem A aside, the split of subject is
independent and visible.

Snapshot above illustrates: `_debut` reads the ongoing task (which
Problem A also mislabels — the sensor's own logic is doing what it
was told), `_duree` and `_resultat` read the previous closed run. A UI
row rendering the three side by side reads as one incoherent record.

Note also: `sensor.py:198-201` claims *"While a run is open we
deliberately show `None` here"* for `last_run_duration`. The lambda
does not do that — it returns the closed run's value regardless. The
comment lies about the code. Design intent might have been `None`
during open, but if so it was never implemented, or the intent was
dropped and the comment not updated.

## Semantics of `interrupted` post-BUG-09

Restated for the record, since Problem A makes the label appear on
runs the operator did not consider interrupted:

- `_close_run` in `run_tracker.py` derives the result label from the
  run's `last_mp`, centralised in one place (BUG-09 finding).
- `completed` ⇔ `last_mp >= MP_COMPLETION_THRESHOLD (99)`.
- `interrupted` ⇔ `last_mp < 99` (or `None`).

Three call sites reach `_close_run`, so three ways a run can be
labelled `interrupted`:

1. **Fresh reset** (RUNNING/PAUSED_DOCKED, incoming `sub < 10 m²`).
   The previous run's `last_mp` was below 99 when the new task
   started counting from zero. Rare — requires a user starting a
   fresh task before completing the previous one.
2. **Sustained-60 s timer** (PAUSED_DOCKED, vs ∈ {1, 3} sustained).
   A mid-run pause below `mp = 99` that never resumed. This is the
   "genuine abandonment" path.
3. **Resolved pending reset**. A candidate reset packet ambiguously
   above 10 m² is confirmed by the successor. The previous run's
   `last_mp` decides the label.

The BUG-09 fast path (`mp >= 99 ∧ vs ∈ {1, 2, 3}`) cannot produce
`interrupted` — by construction `last_mp >= 99` there.

The morning 07:41 → 11:25 run on HA in the snapshot above is labelled
`interrupted` even though `mp = 99` was reached. That is a pre-BUG-09
close (raoul.8 was installed), where the sustained-timer path
hardcoded `interrupted` regardless of `last_mp`. Post-Redownload to
raoul.9 the equivalent close labels `completed`.

## Options to think about

None of these are being proposed for implementation in this SPIKE.
Weighed here for a design pass.

### Option 1 — Detect session start via vs transition

Wire on `process_vehicle_state`: if `state ∈ {COMPLETED, INTERRUPTED}`
and `vs` transitions from `DOCKED_STATES` to `{VS_MOWING, VS_RETURNING}`,
arm a `_pending_new_run` flag. On the next accepted type-2, open a
new run (with the type-2's `time` as `start_time`) instead of
`_reopen_run`. Preserves the `mp` axis as firmware-truthful (still
task-scoped 97 on first packet — no lying) but re-anchors the session
subject on the operator's activation.

Trade-offs:
- Solves Problem A directly for the sensors' subject.
- Does not solve `_run_progress = 97` at session start — that is
  honest firmware data, and hiding it would require Option 3.
- Requires care on transient `vs = 8` (MAP-01 firmware-reset
  transient, already ignored by `process_vehicle_state`) and on
  ping-pong `vs = 4 → 6 → 4` (dock-poke false resume) — the flag
  must debounce.
- Slightly asymmetric wrt the FEAT-05 (b) contract: today the
  tracker's state machine is closed under (type-2, type-1, tick).
  Adding a "start on vs transition, sealed on next type-2" splits
  the acceptance into two-step, which the FEAT-05 SPIKE deliberately
  avoided.

### Option 2 — Split into two entity families

Keep the current `last_run_*` sensors as "last **closed** run"
(drop the `open_run` fallback in `_last_run_start_dt`). Add a
parallel `current_run_started` / `current_run_progress` /
`current_run_zone` set for the ongoing subject (which remains the
task under Problem A, unresolved).

Trade-offs:
- Solves Problem B directly — every sensor is internally consistent.
- Does not solve Problem A. Users still see the ongoing task, not
  the ongoing session.
- Adds three entities and a new "current run" concept in the UI. The
  dashboard's "Tonte en cours" section would show `current_run_*`,
  the "Dernier passage" section would show `last_run_*` — cleaner
  than today's mixed subject.
- Cheapest option code-wise: renames the current confusing pair
  rather than fixing them.

### Option 3 — Renormalise `mp` to be session-scoped

Compute a `mp₀` anchor on `_open_run` and expose
`mp_session = mp - mp₀`. Progression shows a clean 0 → 100 per
session, matching the operator's model.

Trade-offs:
- Solves Problem A visually — the operator sees session progression.
- Requires ceiling logic (a session that starts mid-task at `mp₀ = 68`
  and ends at `mp = 99` shows 31 %, not "done") — either accept that
  or renormalise `[mp₀, 99]` to `[0, 100]`.
- Discards the operator's ability to see how close the *task* is to
  completion.
- Interacts non-trivially with BUG-09 completion criterion — is the
  criterion still `mp >= 99` (firmware value) or `mp_session >= 99`?

### Option 4 — Leave the state machine, rename the sensors

`last_run_started` → `current_or_last_task_started`.
`last_run_duration` → `previous_task_duration`.
`last_run_result` → `previous_task_result`.

Trade-offs:
- Zero code change on the state machine.
- Solves Problem B by making the split explicit in the naming.
- Does not solve Problem A — user still asks "when did *this* run
  start" and gets a task subject.
- Long entity names in French (a `sensor.razibus_debut_de_la_tache_courante_ou_derniere` reads badly).

## Open questions to close before coding

1. **Does `mowStartType` distinguish RUN-after-interrupted from
   schedule-triggered from cold-boot start?** Capture the four raw
   type-2 packets around a real resume-after-interrupted cycle and
   check the `mowStartType` value. If it is `1` on user RUN (any
   RUN, resumption or fresh) and `0` on schedule, we have a signal
   Option 1 could use in addition to the vs transition. If it is
   invariant across resume cycles, we don't.
2. **Does the firmware ever reset `mp` on a user RUN post-interrupted?**
   Or is task-scoped `mp` monotonic across any pause the firmware
   considers a pause? DEBUG capture during an interrupt-then-resume
   cycle would answer.
3. **How should a `vs = 6` (explicit user pause) → app-resume cycle
   be modelled**: same session as one continued after a recharge, or
   different session? Firmware treats both identically. Operator
   intuition may differ.
4. **Is there a downside to always reading `last_finished_run.start_time`
   in `_last_run_start_dt`** (Option 4 minus the rename)? During an
   ongoing run the sensor shows the previous session's start — is
   that a regression from today's behaviour, or is today's behaviour
   already broken so the change is a net improvement?
5. **What is `_debut_du_dernier_passage`'s last_changed at 12:05:14 UTC
   really about?** Between the morning close (11:25 UTC) and the RUN
   press (12:17 UTC) the tracker was in STATE_INTERRUPTED with
   `last_finished_run.start_time = 07:41`. The 12:05 change is 40 min
   before the RUN press — worth chasing (HA restart? integration
   reload? unrelated recorder-side artefact?).

## Follow-up capture to land

Before choosing an option:

- Enable DEBUG on `custom_components.navimow.run_tracker` +
  `coordinator` + `mower_sdk.mqtt`.
- Capture a real interrupt-then-user-RUN cycle. Save the type-1 vs
  timeline plus the type-2 packet sequence across the transition,
  with the `run_tracker event: …` DEBUG lines for the tracker's
  interpretation.
- Answer questions 1 and 2 from the raw packets.
- File a fresh `docs/diag/2026-MM-DD_spike-02b_…/` (or similar) with
  the trace, and cross-link.

## Refs

- **BUG-09** — fix decision + close-path label centralisation:
  [#51](https://github.com/raouldekezel/NavimowHA/issues/51),
  [#53](https://github.com/raouldekezel/NavimowHA/pull/53).
- **BUG-09 diag** — the 2026-07-04 findings that also established
  task-scoped `mp` on the 2026-07-03 controlled run:
  `docs/diag/2026-07-04_bug-09_paused-docked-mp-99/findings.md`
  (PR [#52](https://github.com/raouldekezel/NavimowHA/pull/52)).
- **FEAT-05 SPIKE record** on
  [#43](https://github.com/raouldekezel/NavimowHA/issues/43) — open
  question 3 flagged the exact task-vs-session ambiguity and
  downgraded it as non-blocking. This SPIKE picks it back up.
- **MAP-01** — `vs` catalog (including the transient `vs = 8`):
  `docs/diag/2026-05-23_map-01_vehiclestate-catalog/findings.md`.
- **FEAT-04** — zone registry design
  ([#10](https://github.com/raouldekezel/NavimowHA/issues/10)),
  adjacent to this SPIKE (per-zone metrics also need a stable "run"
  subject to attribute duration/area to).
