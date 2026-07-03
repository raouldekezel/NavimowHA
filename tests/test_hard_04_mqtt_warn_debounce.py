"""HARD-04 — debounce the MQTT-disconnected WARNING behind a persistence gate.

BUG-01 established an edge-triggered WARN/INFO pair around
`sdk.is_connected` on the coordinator's ~30 s poll tick. It fires as
soon as one tick observes `is_connected=False` while state is stale —
but the SDK's sub-second token-refresh reconnects (~40 min cadence,
per FEAT-03 diag) that happen to span a tick also trip it. Result:
~24 WARN lines per day for no actionable outage.

HARD-04 gates the WARN on a counter of consecutive
`is_connected=False` ticks. WARN fires only when the counter reaches
`MQTT_DISCONNECT_TICKS_TO_WARN` (= 3, i.e. ~90 s of continuous
disconnection at update_interval=30 s). Any tick that observes
`is_connected=True` resets the counter to 0.

These tests exercise the seven scenarios in the design plan; see
issue #36.
"""

from __future__ import annotations

import logging
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

# --------------------------------------------------------------------- #
# constant contract                                                     #
# --------------------------------------------------------------------- #


def test_mqtt_disconnect_ticks_to_warn_is_defined_and_reasonable() -> None:
    """The threshold must exist and land in a sensible band.

    < 2 would defeat the point (a single-tick blip trips WARN); > ~6
    would let a real >2-minute outage sit unlogged.
    """
    from custom_components.navimow import const

    assert hasattr(
        const, "MQTT_DISCONNECT_TICKS_TO_WARN"
    ), "MQTT_DISCONNECT_TICKS_TO_WARN constant is missing — see HARD-04."
    assert 2 <= const.MQTT_DISCONNECT_TICKS_TO_WARN <= 6, (
        f"MQTT_DISCONNECT_TICKS_TO_WARN={const.MQTT_DISCONNECT_TICKS_TO_WARN} "
        "outside the sensible 2..6 band."
    )


# --------------------------------------------------------------------- #
# coordinator harness                                                   #
# --------------------------------------------------------------------- #


def _make_coordinator(*, is_connected: bool, last_mqtt_state: float | None):
    """Mock coordinator sufficient to drive `_async_update_data`.

    Symmetric with the BUG-01/03/04 helpers; kept local so this file
    remains self-contained.
    """
    from custom_components.navimow.coordinator import NavimowCoordinator

    coordinator = NavimowCoordinator.__new__(NavimowCoordinator)
    coordinator.hass = MagicMock()
    coordinator.logger = MagicMock()
    coordinator.name = "test"
    coordinator.update_interval = None
    coordinator.config_entry = MagicMock()

    device = MagicMock()
    device.id = "REDACTED-ROBOT-SERIAL"
    coordinator.device = device

    sdk = MagicMock()
    sdk.get_cached_state.return_value = None
    sdk.get_cached_attributes.return_value = None
    sdk.is_connected = is_connected
    coordinator.sdk = sdk

    api = MagicMock()
    api.async_get_device_status = AsyncMock(return_value=MagicMock(battery=77))
    coordinator.api = api

    coordinator._last_state = None
    coordinator._last_attributes = None
    coordinator._last_mqtt_update = last_mqtt_state
    coordinator._last_mqtt_state_update = last_mqtt_state
    coordinator._last_http_fetch = None
    coordinator._last_data_source = None
    coordinator.oauth_session = None
    coordinator._mqtt_disconnect_warned = False
    coordinator._mqtt_disconnect_ticks = 0

    coordinator._device_status_to_state = MagicMock(return_value=MagicMock(battery=77))
    coordinator._build_data = MagicMock(return_value={"state": "http_fallback_result"})
    coordinator.async_set_updated_data = MagicMock()
    return coordinator


def _disconnect_warnings(caplog) -> list[logging.LogRecord]:
    return [
        r
        for r in caplog.records
        if r.levelno == logging.WARNING and "MQTT appears disconnected" in r.message
    ]


def _reconnect_infos(caplog) -> list[logging.LogRecord]:
    return [
        r
        for r in caplog.records
        if r.levelno == logging.INFO and "MQTT reconnected" in r.message
    ]


# --------------------------------------------------------------------- #
# scenarios 1-7                                                         #
# --------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_scenario_1_below_threshold_no_warn(caplog) -> None:
    """Fewer than N consecutive disconnected ticks must not emit WARN."""
    from custom_components.navimow.const import MQTT_DISCONNECT_TICKS_TO_WARN

    coordinator = _make_coordinator(is_connected=False, last_mqtt_state=None)
    caplog.set_level(logging.WARNING, logger="custom_components.navimow.coordinator")

    for _ in range(MQTT_DISCONNECT_TICKS_TO_WARN - 1):
        await coordinator._async_update_data()

    assert _disconnect_warnings(caplog) == []
    assert coordinator._mqtt_disconnect_warned is False
    assert coordinator._mqtt_disconnect_ticks == MQTT_DISCONNECT_TICKS_TO_WARN - 1


@pytest.mark.asyncio
async def test_scenario_2_threshold_reached_fires_warn(caplog) -> None:
    """The Nth consecutive disconnected tick fires the WARN and arms
    the edge-triggered flag."""
    from custom_components.navimow.const import MQTT_DISCONNECT_TICKS_TO_WARN

    coordinator = _make_coordinator(is_connected=False, last_mqtt_state=None)
    caplog.set_level(logging.WARNING, logger="custom_components.navimow.coordinator")

    for _ in range(MQTT_DISCONNECT_TICKS_TO_WARN):
        await coordinator._async_update_data()

    assert len(_disconnect_warnings(caplog)) == 1
    assert coordinator._mqtt_disconnect_warned is True


@pytest.mark.asyncio
async def test_scenario_3_reconnect_after_warn_fires_info(caplog) -> None:
    """A True tick following a WARN state fires the INFO and clears
    both the flag and the counter."""
    from custom_components.navimow.const import MQTT_DISCONNECT_TICKS_TO_WARN

    coordinator = _make_coordinator(is_connected=False, last_mqtt_state=None)
    caplog.set_level(logging.INFO, logger="custom_components.navimow.coordinator")

    for _ in range(MQTT_DISCONNECT_TICKS_TO_WARN):
        await coordinator._async_update_data()
    assert coordinator._mqtt_disconnect_warned is True

    coordinator.sdk.is_connected = True
    await coordinator._async_update_data()

    assert len(_reconnect_infos(caplog)) == 1
    assert coordinator._mqtt_disconnect_warned is False
    assert coordinator._mqtt_disconnect_ticks == 0


@pytest.mark.asyncio
async def test_scenario_4_routine_reconnect_signature_stays_silent(caplog) -> None:
    """Sub-threshold disconnect / reconnect / sub-threshold / reconnect —
    the routine token-refresh signature. Neither WARN nor INFO must fire.
    """
    from custom_components.navimow.const import MQTT_DISCONNECT_TICKS_TO_WARN

    coordinator = _make_coordinator(is_connected=False, last_mqtt_state=None)
    caplog.set_level(logging.INFO, logger="custom_components.navimow.coordinator")

    sub_threshold = MQTT_DISCONNECT_TICKS_TO_WARN - 1

    # First "reconnect blip".
    for _ in range(sub_threshold):
        coordinator.sdk.is_connected = False
        await coordinator._async_update_data()
    coordinator.sdk.is_connected = True
    await coordinator._async_update_data()
    # Second "reconnect blip", a few minutes later.
    for _ in range(sub_threshold):
        coordinator.sdk.is_connected = False
        await coordinator._async_update_data()
    coordinator.sdk.is_connected = True
    await coordinator._async_update_data()

    assert _disconnect_warnings(caplog) == []
    assert _reconnect_infos(caplog) == []
    assert coordinator._mqtt_disconnect_warned is False


@pytest.mark.asyncio
async def test_scenario_5_warn_is_idempotent_once_armed(caplog) -> None:
    """After WARN has fired, further disconnected ticks must not emit
    a repeat WARN."""
    from custom_components.navimow.const import MQTT_DISCONNECT_TICKS_TO_WARN

    coordinator = _make_coordinator(is_connected=False, last_mqtt_state=None)
    caplog.set_level(logging.WARNING, logger="custom_components.navimow.coordinator")

    for _ in range(MQTT_DISCONNECT_TICKS_TO_WARN + 5):
        await coordinator._async_update_data()

    assert len(_disconnect_warnings(caplog)) == 1
    assert coordinator._mqtt_disconnect_warned is True


@pytest.mark.asyncio
async def test_scenario_6_state_fresh_blocks_warn_even_at_threshold(caplog) -> None:
    """Even after enough consecutive disconnected ticks to trip the
    counter, the WARN must NOT fire if the state is still fresh — the
    existing stale gate is untouched by HARD-04."""
    from custom_components.navimow.const import MQTT_DISCONNECT_TICKS_TO_WARN

    coordinator = _make_coordinator(
        is_connected=False, last_mqtt_state=time.monotonic() - 5
    )
    coordinator._last_state = MagicMock()
    caplog.set_level(logging.WARNING, logger="custom_components.navimow.coordinator")

    for _ in range(MQTT_DISCONNECT_TICKS_TO_WARN + 2):
        await coordinator._async_update_data()

    assert _disconnect_warnings(caplog) == []
    assert coordinator._mqtt_disconnect_warned is False


@pytest.mark.asyncio
async def test_scenario_7_ticks_reach_threshold_before_state_goes_stale(
    caplog,
) -> None:
    """Two-gate interaction (mowing case).

    Simulate a still-fresh MQTT state at the moment the disconnect
    starts. The counter reaches threshold first, but the WARN must
    wait for `is_state_stale` to also become true. This guards the
    compound ~180 s upper bound against silent drift.
    """
    from custom_components.navimow.const import (
        MQTT_DISCONNECT_TICKS_TO_WARN,
        MQTT_STALE_SECONDS,
    )

    # Start with a fresh state push 5 s ago.
    coordinator = _make_coordinator(
        is_connected=False, last_mqtt_state=time.monotonic() - 5
    )
    coordinator._last_state = MagicMock()
    caplog.set_level(logging.WARNING, logger="custom_components.navimow.coordinator")

    # Counter climbs past threshold while state stays fresh — no WARN.
    for _ in range(MQTT_DISCONNECT_TICKS_TO_WARN + 1):
        await coordinator._async_update_data()

    assert _disconnect_warnings(caplog) == []
    assert coordinator._mqtt_disconnect_ticks >= MQTT_DISCONNECT_TICKS_TO_WARN
    assert coordinator._mqtt_disconnect_warned is False

    # Age the state past the stale threshold. Next tick must fire WARN.
    coordinator._last_mqtt_state_update = time.monotonic() - MQTT_STALE_SECONDS - 1
    await coordinator._async_update_data()

    assert len(_disconnect_warnings(caplog)) == 1
    assert coordinator._mqtt_disconnect_warned is True
