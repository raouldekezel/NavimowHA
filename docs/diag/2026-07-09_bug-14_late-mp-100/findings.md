# 2026-07-09 — BUG-14 refined, late ``mp = 100`` delivery, ``cmp`` zone-persistent

## Question

Two live-DEBUG questions surfaced during the operator's 2026-07-09 mowing day, running raoul.15 (pre-PR #91):

1. Why did the tracker split a single logical session into two ``completed`` runs when the robot returned to dock at ``mp = 99`` for recharge?
2. When the operator triggered a manual Figuier task 3 h 20 after the mini-run, what did the firmware ship on the wire, and what did the tracker do with it?

## Answer (TL;DR)

1. **BUG-09's threshold at 99** (see #89) closed the mini-run on the recharge dock, exactly as reported by the operator. **PR #91** raises the threshold to 100 and adds a refined branch ``mp ≥ 99 ∧ zones[-1].cmp_max ≥ 10000`` that recovers the ``completed`` label when the last active zone is 100 % mowed.
2. Live DEBUG capture reveals two firmware behaviours that were unknown or partially wrong in the previous doc:
   - **``mp = 100`` can be delivered several hours late**, at the moment the firmware transitions to a new task. On the observed day: mini-run closed with ``mp = 99`` at 12:51 CEST, then a packet with ``boundary = 1, cmp = 10000, mp = 100, sub = 231.77`` arrived at ``16:11:44 CEST`` (+3 h 20), one minute before the first real Figuier packet. This validates PR #91's ``mp ≥ 99 ∧ cmp = 10000`` refinement: waiting for ``mp = 100`` alone would have mis-labelled the mini-run as ``interrupted`` for over 3 h before the correct ``completed`` verdict landed.
   - **``currentMowProgress`` is zone-persistent across sessions**, contradicting the previous doc's "reset at each zone change". On resume of a partially-mowed Figuier zone (interrupted 07/07 at ~35 %), the first type-2 packet showed ``cmp = 4404`` (44.04 %), not 0. The firmware credits prior progress.

## Timeline (heures CEST, live DEBUG log)

The morning half of this day (recharge-splits-session pathology) is reported in #89. This diag focuses on the afternoon.

### Mini-run close (13:00 CEST, task Prunier)

The mini-run at 12:51:46 CEST is the last packet of the Prunier task before the tracker closes via BUG-09 fast path (mp=99, cmp=10000, vs=2). Storage after close:

```
tracker state = COMPLETED
last_finished_run.zones = [{boundary_id: 1, cmp_max: 10000, sub_entry: 231.77, sub_exit: 231.77}]
last_finished_run.result = completed
_last_accepted_time_type2 = 1783594306436  # 12:51:46 CEST firmware time
```

### Manual Figuier task start (16:11 CEST)

Operator triggers a manual RUN on Figuier at ~16:11 CEST. Wire capture (packets abridged to the fields that matter):

```
16:11:44  boundary=1  cmp=10000  mp=100  sub=231.77   wk=869.88   mst=1  action=5
16:13:08  boundary=3  cmp=4404   mp=80   sub=286.87   wk=870.39   mst=1  action=5
16:13:48  boundary=3  cmp=4518   mp=80   sub=288.09   wk=871.61   mst=1  action=-1
16:14:38  boundary=3  cmp=4627   mp=81   sub=289.47   wk=872.98   mst=1  action=-1
16:15:19  boundary=3  cmp=4704   mp=81   sub=290.37   wk=873.89   mst=1  action=-1
16:15:56  boundary=3  cmp=4805   mp=81   sub=291.52   wk=875.03   mst=1  action=5
16:16:59  boundary=3  cmp=4908   mp=82   sub=293.03   wk=876.55   mst=1  action=-1
```

Three observations pop out.

### Observation A — the 16:11:44 packet is a late task-end for Prunier

Its ``boundary`` (1), ``cmp`` (10000), ``sub`` (231.77 — identical to the mini-run's ``sub_exit``) and ``mp`` (100 — first time the firmware has ever shown 100 for this Prunier task) make it structurally a **task-end delivery for the Prunier task, shipped just before the Figuier task-start**. It is not a real mowing sample.

Under the pre-PR #91 code (raoul.15), the tracker was in ``STATE_COMPLETED``, and ``process_type2`` evaluated:

- ``is_reset``: ``incoming_sub=231.77 < last_sub=231.77`` is False.
- ``_has_strict_progress``: ``incoming_mp=100 > last_mp=99`` is True.

So ``_open_run`` fires with ``start_time = 16:11:44 CEST``, ``sub₀ = 231.77``, ``zones = [{boundary_id: 1, ...}]``. At 16:13:08 the boundary changes to 3 and ``_update_zone`` appends a second segment. The eventual run close will report:

- ``start_time = 16:11:44`` — should be 16:13:08.
- ``zones = [1, 3]`` — should be ``[3]``.

That is a **phantom-open** issue distinct from BUG-15 (whose trigger is ``boundary = 0`` in ``STATE_INTERRUPTED``). Filed as **BUG-16** (#92).

### Observation B — ``mp = 100`` is not reliable in real time

The 3 h 20 gap between the mini-run's 12:51 close and the ``mp = 100`` packet at 16:11 is the entire reason the ``MP_COMPLETION_THRESHOLD = 100 alone`` design would fail: PR #91's fast path would never fire on the mini-run, and the sustained-timer would eventually close it with label ``interrupted`` (mp = 99 at close, ``last_mp < threshold``).

**PR #91's refined branch** ``mp ≥ 99 ∧ zones[-1].cmp_max ≥ 10000`` catches this immediately at the 12:52 dock arrival, labelling ``completed``. The refined rule is not a nicety — it is structurally required by the firmware's late-delivery behaviour.

The ``time`` field on the 16:11:44 packet is the firmware epoch of generation (matches the ``sub = 231.77`` snapshot), not of delivery — so a client-side "is this late?" filter cannot rely on ``time`` alone. It would need signature matching on ``(boundary, sub, mp, cmp)`` against the last closed run's payload. BUG-16's fix A implements exactly that.

### Observation C — ``cmp`` is zone-persistent across sessions

The Figuier zone was mowed to ~35 % on 2026-07-07 and interrupted (``sensor.razibus_zone_3 = 43`` m² of the ~123 m² zone). On the resume today, the first Figuier packet at 16:13:08 shows ``cmp = 4404`` (44.04 %), not 0.

The previous doc's line "reset at each zone change" is only true **within a single run**. Across sessions, the firmware credits prior progress into ``cmp``. To formalise:

- ``cmp`` resets to 0 only when the firmware starts a zone from scratch (no prior credit, e.g. after a full 10000 completion followed by a fresh task on the same zone).
- ``cmp`` resumes at the previous value when a session picks up a previously-interrupted zone.
- ``cmp = 10000`` is an absolute state (zone 100 %) — this is the invariant PR #91's refined rule relies on, and it is unaffected by the persistence behaviour.

## Impact on PR #91's refinement

Both findings **strengthen** the PR #91 design:

- Finding B (late ``mp = 100``) is the operational justification for waiting on ``cmp = 10000`` instead of ``mp = 100`` alone.
- Finding C (``cmp`` zone-persistent) means the ``cmp = 10000`` signal is a genuine "this zone is done" statement — not a per-session counter that could accidentally hit 10000 without the zone being fully mowed.

The residual trade-off documented on the PR body (``mp = 99`` tasks whose ``cmp`` never reaches 10000 close as ``interrupted``) is unchanged.

## Follow-up

- **BUG-16 (#92)** — filter late task-end packets before ``_open_run`` on the post-close new-session path. Signature: ``sub == last_closed_sub ∧ mp = 100 ∧ cmp = 10000 ∧ boundary ∈ last_closed_boundaries``.
- **Doc MQTT §5 update** — the two ``⚠️ CORRIGÉ 2026-07-09`` notes on ``currentMowProgress`` and ``mowingPercentage`` in ``it-documentation/Home Assistant - Navimow - MQTT.md``.
- **BUG-15 (#90)** — the same 2026-07-09 day exposes a distinct phantom-open pattern from ``boundary = 0`` in ``STATE_INTERRUPTED``. Independent from BUG-16, will need its own fix.

## Environment

- Firmware: Segway Navimow i210 LiDAR Pro, serial ``3KAAW2606K1874``, region FRA.
- Integration: ``raouldekezel/NavimowHA`` prerelease ``raoul.15`` on Home Assistant 2026.1.3 (Docker on intel-nuc).
- DEBUG re-enabled at 15:59 CEST 2026-07-09 via ``configuration.yaml`` (persistent) + ``logger.set_level`` (immediate).
- Storage tracker snapshot as of 12:52 CEST (after the mini-run close, before the manual Figuier task) captured under this issue's evidence trail.
