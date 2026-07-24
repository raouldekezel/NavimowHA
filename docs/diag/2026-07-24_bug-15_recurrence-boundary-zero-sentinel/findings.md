# BUG-15 recurrence — 2026-07-24 02:43 — a `boundary=0` session-init sentinel opens a nocturnal phantom run

## TL;DR

**BUG-15 (#90) is not fixed.** It was closed on 2026-07-21 as *"Absorbed in
bug-19"*, but it recurred tonight on `raoul.28`: an all-zero
`boundary=0 ∧ mp=0 ∧ cmp=0 ∧ sub=0.0` type-2 opened a phantom run at
**02:43:27 CEST** while the robot was **docked all night**. The run lingered
~10.9 h and closed `interrupted` (area 0, `zones=[]`) at **13:39**, on the
`raoul.28` install/reload.

The BUG-17/BUG-19 vestige guard **cannot** catch it: its drop signature is
the *opposite* shape — the terminal task-end vestige `mp = 100 ∧ cmp ≥ 10000`
(`run_tracker.py` `_gate_run_start_vestige`, line ~1143). BUG-15 and BUG-19
share a **symptom** (a nocturnal phantom run start from a post-close state)
but have **opposite trigger packets**. The absorption was a misclassification.

This is **unrelated to HARD-19 / `raoul.28`** — HARD-19 did not touch this
run-open path. `raoul.28` in fact *closed* the stale phantom on reload.

## Timeline (CEST)

| Time | Version | Event |
|---|---|---|
| 07-23 20:41:51 → 20:48:54 | raoul.27 | real short evening mow (provisional, boundary 1) → `interrupted` → tracker `IDLE` with a seeded post-close reference |
| 07-24 02:43:08 | raoul.27 | a stale type-2 (`action:5`) DROPPED (`time <= last`) |
| **07-24 02:43:27** | **raoul.27** | **phantom `run_started`** on the all-zero sentinel (`mow_start_type=0`, `start_time=1784853807018`) → « en cours » |
| 07-24 02:43 → 13:39 | raoul.27 | robot **docked** (284× `vehicleState:1`); run lingers, `zones` never seeded |
| 07-24 ~13:33 | — | operator installs `raoul.28` (HACS → `Setting up navimow`, reload) |
| **07-24 13:39:05** | **raoul.28** | phantom `run_finished`: `interrupted`, `session_area=0`, `zones=[]`, `duration_ms=39 249 263` (~10.9 h) → « au repos » |
| 07-24 13:55:43 | raoul.28 | a **real** mow starts (`lawn_mower → mowing`, `vehicleState:4`, boundary 1) — **after** the phantom closed → no engulf this time |

## The trigger packet (02:43:27.132, `/location` type-2)

```json
[{"action":-1,"currentMowBoundary":0,"currentMowProgress":0,
  "mapWorkPosition":"FFFFFFFF0000…0000","mowStartType":0,
  "mowingPercentage":0,"mowingWeekArea":"404.3","subtotalArea":"0.0",
  "time":1784853807018,"type":2}]
```

`boundary = 0`, `mp = 0`, `cmp = 0`, `sub = 0.0` — the all-zero **session-init
sentinel** shape (BUG-06), emitted spuriously overnight with no mow following.

## Robot was docked throughout (corroboration)

| Signal | overnight (02:43 → 13:39) |
|---|---|
| `lawn_mower.navimow_i210_lidar_pro` | `docked` (unbroken 02:00 → 13:55) |
| `binary_sensor.…_en_charge` | `off` |
| `sensor.…_batterie` | 100 % |
| `/location` `vehicleState` | `1` (docked idle) ×284 — the 12× `vehicleState:4` all belong to the **13:55** real mow, not the night |
| accepted `boundary` | none (`zones=[]` at close; the only type-2 with a real boundary in the window was the 02:43:08 stale-drop and the 13:56 real mow) |

## Mechanism

1. Pre-state: `STATE_IDLE` with a seeded post-close reference (the 20:41–20:48
   evening run; `last_sub ≈ 44`).
2. The 02:43 sentinel has `sub = 0.0 < prev last_sub` → `is_reset = True`; and
   `0.0 < RESET_SUB_CEILING` → the IDLE `is_reset` branch calls `_open_run`
   and emits `run_started` (`mow_start_type = 0`).
3. `_gate_run_start_vestige` does **not** intervene — its drop predicate is
   `mp == MP_TASK_END (100) ∧ (cmp or 0) >= CMP_ZONE_COMPLETE_THRESHOLD (10000)`.
   The sentinel is `mp = 0 ∧ cmp = 0` → no match → not dropped.
4. `_update_zone` rejects `boundary = 0`, so `zones` stays `[]` — the run is
   open but never seeds a zone. No real mow follows, so it lingers.

## Why "Absorbed in bug-19" was incorrect

| | Trigger packet | Caught by the vestige guard? |
|---|---|---|
| **BUG-17 / BUG-19** (task-**end** vestige) | `mp = 100 ∧ cmp = 10000` | **yes** — that *is* its signature |
| **BUG-15** (task-**start** sentinel) | `boundary = 0 ∧ mp = 0 ∧ cmp = 0 ∧ sub = 0` | **no** |

Same symptom (nocturnal phantom start), **opposite** trigger. The guard that
absorbs BUG-19 keys on `mp = 100`; it structurally cannot match a `mp = 0`
sentinel. Tonight's `run_started` (02:43:27) is the proof on current code.

## Secondary observation — the ~10.9 h lingering

The phantom opened directly at `STATE_RUNNING` while the robot was **already**
`vehicleState = 1`. `process_vehicle_state` only moves `RUNNING → PAUSED_DOCKED`
on a vs *edge into* a dock state; with no vs change, the run stayed `RUNNING`
and the sustained-interrupt timer never armed — so it did not self-close
overnight. It cleared only when the `raoul.28` install reloaded the tracker
(~13:33) and a post-restore tick closed it (`interrupted`) at 13:39. (Hypothesis
from the vs distribution; worth confirming with a targeted restore trace.)

## Fix direction (separate ticket — NOT this diag, NOT HARD-19)

The sentinel is *also* the legitimate BUG-06 session-init (the first packet of
a real mow, followed by a real boundary ~60 s later), so it cannot be
blind-dropped. The discriminator is **physical**: a real session starts
**off-dock** (`DEPARTURE_EVIDENCE = {4, 5}`, the vocabulary HARD-19 §2 just
established), a spurious nocturnal sentinel arrives **docked**. Candidate:
do not `_open_run` from `STATE_IDLE` on an all-zero sentinel
(`boundary = 0 ∧ mp = 0 ∧ sub = 0`) while the robot is docked — and note that
HARD-18 already anchors the real session on the `vs = 4` activation edge, which
makes opening a run from a bare type-2 sentinel largely redundant.

## References

- #90 (BUG-15, closed 2026-07-21 "Absorbed in bug-19") — original 04:09
  phantom, same `boundary=0` mechanism.
- #114 (BUG-19) / #105 (BUG-17) — the terminal-vestige guard (`mp = 100`).
- BUG-06 — the legitimate all-zero session-init sentinel.
- HARD-18 (#117) eager provisional start; HARD-19 (#120) `DEPARTURE_EVIDENCE`.
- Log evidence: `night_phantom.log` (this folder, redacted).
