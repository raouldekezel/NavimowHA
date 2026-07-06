"""FEAT-04 PR 2 — coordinator wiring for the zone registry.

Every test exercises one seam:

- Instantiation: the coordinator carries a `zone_registry` from
  `__init__`, empty until either restore or ingest fills it.
- Restore: `_async_restore_store` replays the loaded `history` onto the
  registry so PR 3's sensors have data at first render.
- Ingest + dispatch: on `run_finished`, the coordinator folds the run
  and fires `SIGNAL_ZONE_DISCOVERED_<device_id>` once per first-time
  boundary. A dispatch with no listener is a no-op — PR 3 will listen.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from custom_components.navimow.const import SIGNAL_ZONE_DISCOVERED
from custom_components.navimow.run_tracker import EVENT_RUN_FINISHED, Event, RunTracker
from custom_components.navimow.zone_registry import ZoneRegistry


def _make_coordinator():
    """Minimal `__new__`-built coordinator — mirrors the FEAT-05c helper."""
    from custom_components.navimow.coordinator import NavimowCoordinator

    coord = NavimowCoordinator.__new__(NavimowCoordinator)
    coord.hass = MagicMock()
    coord.hass.bus.async_fire = MagicMock()
    coord.hass.async_create_task = MagicMock()
    coord.logger = MagicMock()
    coord.name = "test"
    coord.update_interval = None
    coord.config_entry = MagicMock()
    device = MagicMock()
    device.id = "REDACTED-ROBOT-SERIAL"
    coord.device = device
    coord.run_tracker = RunTracker()
    coord.history = []
    coord.last_finished_run = None
    coord.zone_registry = ZoneRegistry()
    coord._store = None
    coord._last_store_save_monotonic = 0.0
    return coord


def _seg(boundary_id, first_time, last_time, cmp_max, sub_entry, sub_exit):
    return {
        "boundary_id": boundary_id,
        "first_time": first_time,
        "last_time": last_time,
        "cmp_max": cmp_max,
        "sub_entry": sub_entry,
        "sub_exit": sub_exit,
    }


def _run_finished_event(zones, *, result="completed", start=1_000, end=200_000):
    return Event(
        kind=EVENT_RUN_FINISHED,
        payload={
            "result": result,
            "start_time": start,
            "end_time": end,
            "duration_ms": end - start,
            "session_area": None,
            "mow_start_type": 1,
            "zones": zones,
        },
    )


# --------------------------------------------------------------------- #
# 1. Instantiation                                                      #
# --------------------------------------------------------------------- #


def test_coordinator_owns_empty_zone_registry_from_init() -> None:
    coord = _make_coordinator()
    assert isinstance(coord.zone_registry, ZoneRegistry)
    assert coord.zone_registry.zones == {}


# --------------------------------------------------------------------- #
# 2. Restore rebuilds the registry from history                         #
# --------------------------------------------------------------------- #


async def test_restore_rebuilds_registry_from_history() -> None:
    coord = _make_coordinator()
    history = [
        {
            "result": "completed",
            "start_time": 0,
            "end_time": 100_000,
            "zones": [_seg(1, 0, 90_000, 10_000, 0.0, 200.0)],
        },
        {
            "result": "completed",
            "start_time": 200_000,
            "end_time": 300_000,
            "zones": [
                _seg(1, 200_000, 250_000, 10_000, 0.0, 227.0),  # newer for #1
                _seg(3, 250_000, 300_000, 10_000, 227.0, 351.0),
            ],
        },
    ]
    store_payload = {"tracker": None, "cursors": {}, "history": history}

    with patch("custom_components.navimow.coordinator.Store") as store_cls:
        store_instance = MagicMock()
        store_instance.async_load = AsyncMock(return_value=store_payload)
        store_cls.return_value = store_instance
        await coord._async_restore_store()

    assert coord.zone_registry.zones.keys() == {1, 3}
    # last-wins on size_estimate: #1's second (227) beats the first (200).
    assert coord.zone_registry.zones[1].size_estimate_m2 == 227.0
    assert coord.zone_registry.zones[3].size_estimate_m2 == 124.0


async def test_restore_on_empty_store_leaves_registry_empty() -> None:
    coord = _make_coordinator()
    with patch("custom_components.navimow.coordinator.Store") as store_cls:
        store_instance = MagicMock()
        store_instance.async_load = AsyncMock(return_value=None)
        store_cls.return_value = store_instance
        await coord._async_restore_store()
    assert coord.zone_registry.zones == {}


async def test_restore_does_not_dispatch_zone_discovered() -> None:
    """Contract for PR 3: restore is eager (entities created by iterating
    ``registry.zones`` after the rebuild), the discovery signal is
    reserved for runtime ``run_finished`` events. A dispatch during
    restore would cause double entity adds — pinning this here stops a
    future change to ``_async_restore_store`` from silently regressing
    PR 3."""
    coord = _make_coordinator()
    history = [
        {
            "result": "completed",
            "start_time": 0,
            "end_time": 100_000,
            "zones": [_seg(1, 0, 90_000, 10_000, 0.0, 228.0)],
        }
    ]
    with (
        patch("custom_components.navimow.coordinator.Store") as store_cls,
        patch("custom_components.navimow.coordinator.async_dispatcher_send") as disp,
    ):
        store_instance = MagicMock()
        store_instance.async_load = AsyncMock(return_value={"history": history})
        store_cls.return_value = store_instance
        await coord._async_restore_store()
    assert 1 in coord.zone_registry.zones  # rebuild happened
    assert disp.call_count == 0  # but no dispatch


async def test_restore_survives_corrupt_history() -> None:
    """A malformed on-disk entry (here: ``zones`` is a string, not a
    list) must not crash restore. The registry stays empty; future
    ``run_finished`` events will re-populate it as sessions close."""
    coord = _make_coordinator()
    bad_history = [
        {
            "result": "completed",
            "start_time": 0,
            "end_time": 100_000,
            "zones": "not-a-list",  # AttributeError in the rebuild loop
        }
    ]
    with patch("custom_components.navimow.coordinator.Store") as store_cls:
        store_instance = MagicMock()
        store_instance.async_load = AsyncMock(return_value={"history": bad_history})
        store_cls.return_value = store_instance
        await coord._async_restore_store()  # must not raise
    assert coord.zone_registry.zones == {}
    # History field itself is untouched — the corrupt bytes reach the
    # coordinator; only the registry projection is skipped.
    assert coord.history == bad_history


# --------------------------------------------------------------------- #
# 3. Ingest + dispatch on run_finished                                   #
# --------------------------------------------------------------------- #


def test_run_finished_ingests_and_dispatches_new_boundary() -> None:
    coord = _make_coordinator()
    event = _run_finished_event(
        [_seg(1, 0, 90_000, 10_000, 0.0, 228.0)],
    )
    with patch("custom_components.navimow.coordinator.async_dispatcher_send") as disp:
        coord._forward_run_events([event])

    # Registry got the fold.
    assert 1 in coord.zone_registry.zones
    assert coord.zone_registry.zones[1].last_surface_m2 == 228.0
    # Dispatcher got the discovery signal for boundary 1.
    disp.assert_called_once_with(
        coord.hass,
        f"{SIGNAL_ZONE_DISCOVERED}_{coord.device.id}",
        1,
    )


def test_run_finished_dispatches_each_new_boundary_once() -> None:
    """First run introduces #1 and #3 → 2 dispatches; second run touches
    only #1 → no further dispatch; a third run introducing #4 → 1 dispatch
    for #4 only."""
    coord = _make_coordinator()

    with patch("custom_components.navimow.coordinator.async_dispatcher_send") as disp:
        coord._forward_run_events(
            [
                _run_finished_event(
                    [
                        _seg(1, 0, 5_000, 10_000, 0.0, 200.0),
                        _seg(3, 5_000, 10_000, 10_000, 200.0, 320.0),
                    ]
                )
            ]
        )
    dispatched = [call.args[2] for call in disp.call_args_list]
    assert sorted(dispatched) == [1, 3]

    with patch("custom_components.navimow.coordinator.async_dispatcher_send") as disp:
        coord._forward_run_events(
            [_run_finished_event([_seg(1, 10_000, 20_000, 10_000, 0.0, 210.0)])]
        )
    assert disp.call_count == 0  # already known

    with patch("custom_components.navimow.coordinator.async_dispatcher_send") as disp:
        coord._forward_run_events(
            [_run_finished_event([_seg(4, 20_000, 30_000, 10_000, 0.0, 90.0)])]
        )
    disp.assert_called_once_with(
        coord.hass,
        f"{SIGNAL_ZONE_DISCOVERED}_{coord.device.id}",
        4,
    )


def test_run_finished_appends_history_before_ingest_signal_is_stable() -> None:
    """`history` gets the entry before the dispatch fires. Protects the
    invariant PR 3's `async_add_entities` listener will rely on: at the
    time it receives the signal, the coordinator's `zone_registry`
    already reflects the finished run."""
    coord = _make_coordinator()

    seen_registry_state: list[dict] = []

    def _capture(_hass, _signal, boundary_id):
        seen_registry_state.append(
            {
                "boundary_id": boundary_id,
                "known": boundary_id in coord.zone_registry.zones,
                "surface": coord.zone_registry.zones[boundary_id].last_surface_m2,
                "history_len": len(coord.history),
            }
        )

    with patch(
        "custom_components.navimow.coordinator.async_dispatcher_send",
        side_effect=_capture,
    ):
        coord._forward_run_events(
            [_run_finished_event([_seg(1, 0, 90_000, 10_000, 0.0, 228.0)])]
        )

    assert seen_registry_state == [
        {
            "boundary_id": 1,
            "known": True,
            "surface": 228.0,
            "history_len": 1,
        }
    ]


def test_no_events_no_registry_touch() -> None:
    coord = _make_coordinator()
    with patch("custom_components.navimow.coordinator.async_dispatcher_send") as disp:
        coord._forward_run_events([])
    assert coord.zone_registry.zones == {}
    assert disp.call_count == 0


def test_non_run_finished_events_do_not_touch_registry() -> None:
    """`run_started` and any other kind must not feed the registry."""
    coord = _make_coordinator()
    from custom_components.navimow.run_tracker import EVENT_RUN_STARTED

    started = Event(kind=EVENT_RUN_STARTED, payload={"start_time": 42})
    with patch("custom_components.navimow.coordinator.async_dispatcher_send") as disp:
        coord._forward_run_events([started])
    assert coord.zone_registry.zones == {}
    assert disp.call_count == 0
