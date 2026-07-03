"""HARD-02 — `weekly_area` survives an HA restart via RestoreSensor.

The cloud stops publishing /location type-2 while the robot is docked
(FEAT-02 diag: 6.5 days continuous /state, zero type-2 outside
mowing). Without HARD-02, restarting HA between mow sessions wiped the
cumulative `area_week` to `unknown` until the next session. This test
file guards:

1. The `restore` opt-in flag on the description dataclass.
2. Exactly one sensor description sets `restore=True`, and it is
   `weekly_area` (so the flag doesn't silently spread to sensors whose
   session-scoped semantics would be violated by a restored stale
   value).
3. The `NavimowSensor.native_value` fallback path (live value wins,
   restore fallback kicks in only when live returns `None` and the
   description opts in).
4. `async_added_to_hass` seeds `_attr_native_value` from the RestoreSensor
   store when `restore=True`, and does nothing when `restore=False`.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# --------------------------------------------------------------------- #
# opt-in surface                                                        #
# --------------------------------------------------------------------- #


def test_restore_flag_default_is_false() -> None:
    """Every description that does not explicitly opt in must default
    to `restore=False`. Prevents accidental spread."""
    from custom_components.navimow.sensor import NavimowSensorEntityDescription

    desc = NavimowSensorEntityDescription(
        key="probe",
        value_fn=lambda c: None,
    )
    assert desc.restore is False


def test_only_weekly_area_opts_in_to_restore() -> None:
    """Exactly one description has `restore=True`, and it is
    `weekly_area`. Guards the deliberate scope of HARD-02.
    """
    from custom_components.navimow.sensor import SENSOR_DESCRIPTIONS

    opted_in = [d.key for d in SENSOR_DESCRIPTIONS if d.restore]
    assert opted_in == [
        "weekly_area"
    ], f"restore flag has spread beyond weekly_area: {opted_in}"


# --------------------------------------------------------------------- #
# NavimowSensor.native_value fallback                                   #
# --------------------------------------------------------------------- #


def _make_sensor(*, restore: bool, stats):
    """Bare-bones NavimowSensor with only what the fallback path needs."""
    from custom_components.navimow.sensor import (
        NavimowSensor,
        NavimowSensorEntityDescription,
    )

    coordinator = MagicMock()
    coordinator.stats = stats
    coordinator.get_device_state = MagicMock(return_value=None)

    device = MagicMock()
    device.id = "REDACTED-ROBOT-SERIAL"
    device.name = "test-mower"
    device.model = "i210 LiDAR Pro"
    device.firmware_version = None
    device.serial_number = None
    coordinator.device = device

    description = NavimowSensorEntityDescription(
        key="probe_area",
        value_fn=lambda c: (c.stats or {}).get("area_week"),
        restore=restore,
    )
    sensor = NavimowSensor.__new__(NavimowSensor)
    # Skip the full super().__init__ chain — we only exercise `native_value`
    # and `async_added_to_hass` here.
    sensor.entity_description = description
    sensor.coordinator = coordinator
    sensor._attr_native_value = None
    return sensor


def test_native_value_returns_live_when_stats_present() -> None:
    """Live coordinator value wins — always."""
    sensor = _make_sensor(restore=True, stats={"area_week": 55.1})
    assert sensor.native_value == 55.1


def test_native_value_live_refreshes_internal_cache_for_next_restart() -> None:
    """Reading a live value must also refresh `_attr_native_value` so a
    subsequent restart resumes from the freshest value we ever saw, not
    from the (older) restored value.
    """
    sensor = _make_sensor(restore=True, stats={"area_week": 55.1})
    _ = sensor.native_value  # read → refresh cache
    assert sensor._attr_native_value == 55.1


def test_native_value_falls_back_to_restored_when_live_is_none() -> None:
    """The fix: live returns None (no fresh type-2 yet post-restart),
    restore=True, restored snapshot present → returns snapshot."""
    sensor = _make_sensor(restore=True, stats=None)
    sensor._attr_native_value = 42.3  # simulate restore having populated it
    assert sensor.native_value == 42.3


def test_native_value_none_when_no_restore_flag_even_if_snapshot_exists() -> None:
    """`restore=False` disables the fallback even if a snapshot happens
    to be present in `_attr_native_value`. Regression guard against
    silent spread of restoration to session-scoped sensors.
    """
    sensor = _make_sensor(restore=False, stats=None)
    sensor._attr_native_value = 42.3  # would leak if the flag was ignored
    assert sensor.native_value is None


def test_native_value_none_when_restore_flag_but_no_snapshot() -> None:
    """Fresh install / storage cleared: no prior data, live is `None`.
    Must return `None` without crashing.
    """
    sensor = _make_sensor(restore=True, stats=None)
    # _attr_native_value defaults to None in the constructor above.
    assert sensor.native_value is None


# --------------------------------------------------------------------- #
# async_added_to_hass restoration                                        #
# --------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_async_added_seeds_from_last_sensor_data_when_restore_true() -> None:
    """`RestoreSensor.async_get_last_sensor_data` returns the previously
    stored native value; the hook must copy it into `_attr_native_value`
    so the very first `native_value` read after startup returns the
    restored value.
    """
    sensor = _make_sensor(restore=True, stats=None)

    stored = MagicMock()
    stored.native_value = 42.3
    with (
        patch.object(
            sensor,
            "async_get_last_sensor_data",
            AsyncMock(return_value=stored),
            create=True,
        ),
        patch(
            "custom_components.navimow.sensor.CoordinatorEntity.async_added_to_hass",
            AsyncMock(),
        ),
        patch(
            "custom_components.navimow.sensor.RestoreSensor.async_added_to_hass",
            AsyncMock(),
        ),
    ):
        await sensor.async_added_to_hass()

    assert sensor._attr_native_value == 42.3


@pytest.mark.asyncio
async def test_async_added_ignores_stored_data_when_restore_false() -> None:
    """A session-scoped sensor (`restore=False`) must not seed itself
    from any leftover storage. Guards against silent spread.
    """
    sensor = _make_sensor(restore=False, stats=None)

    stored = MagicMock()
    stored.native_value = 42.3
    fetch = AsyncMock(return_value=stored)
    with (
        patch.object(sensor, "async_get_last_sensor_data", fetch, create=True),
        patch(
            "custom_components.navimow.sensor.CoordinatorEntity.async_added_to_hass",
            AsyncMock(),
        ),
        patch(
            "custom_components.navimow.sensor.RestoreSensor.async_added_to_hass",
            AsyncMock(),
        ),
    ):
        await sensor.async_added_to_hass()

    fetch.assert_not_called()
    assert sensor._attr_native_value is None


@pytest.mark.asyncio
async def test_async_added_handles_no_prior_data_gracefully() -> None:
    """`async_get_last_sensor_data` returns `None` on a fresh install.
    Hook must not raise and must leave `_attr_native_value` at `None`.
    """
    sensor = _make_sensor(restore=True, stats=None)

    with (
        patch.object(
            sensor,
            "async_get_last_sensor_data",
            AsyncMock(return_value=None),
            create=True,
        ),
        patch(
            "custom_components.navimow.sensor.CoordinatorEntity.async_added_to_hass",
            AsyncMock(),
        ),
        patch(
            "custom_components.navimow.sensor.RestoreSensor.async_added_to_hass",
            AsyncMock(),
        ),
    ):
        await sensor.async_added_to_hass()

    assert sensor._attr_native_value is None
