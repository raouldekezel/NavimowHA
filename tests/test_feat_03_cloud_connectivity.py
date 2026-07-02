"""FEAT-03 — cloud connectivity binary_sensor.

Exercises the entity description directly against a stub coordinator
whose `sdk.is_connected` we control. No HA setup path — purely tests
the observable value contract.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest


def _make_coordinator(*, is_connected):
    coordinator = MagicMock()
    coordinator.sdk = MagicMock()
    coordinator.sdk.is_connected = is_connected
    return coordinator


def test_binary_sensor_is_on_when_sdk_connected() -> None:
    from custom_components.navimow.binary_sensor import BINARY_SENSOR_DESCRIPTIONS

    desc = next(d for d in BINARY_SENSOR_DESCRIPTIONS if d.key == "cloud_connected")
    coordinator = _make_coordinator(is_connected=True)

    assert desc.is_on_fn(coordinator) is True


def test_binary_sensor_is_off_when_sdk_disconnected() -> None:
    from custom_components.navimow.binary_sensor import BINARY_SENSOR_DESCRIPTIONS

    desc = next(d for d in BINARY_SENSOR_DESCRIPTIONS if d.key == "cloud_connected")
    coordinator = _make_coordinator(is_connected=False)

    assert desc.is_on_fn(coordinator) is False


def test_binary_sensor_is_off_when_sdk_missing_attribute() -> None:
    """Defensive: an older SDK version may not have `is_connected`.
    The description must fall back to False rather than raise.
    """
    from custom_components.navimow.binary_sensor import BINARY_SENSOR_DESCRIPTIONS

    desc = next(d for d in BINARY_SENSOR_DESCRIPTIONS if d.key == "cloud_connected")
    coordinator = MagicMock(spec=[])  # no `sdk` attribute at all
    coordinator.sdk = MagicMock(spec=[])  # no `is_connected` on the sdk

    assert desc.is_on_fn(coordinator) is False


def test_binary_sensor_diagnostic_category_and_connectivity_class() -> None:
    """Contract check — must be diagnostic + connectivity, so it lands
    in the diagnostic tab of the device page and speaks the standard
    HA `on = connected` semantics.
    """
    from homeassistant.components.binary_sensor import BinarySensorDeviceClass
    from homeassistant.helpers.entity import EntityCategory

    from custom_components.navimow.binary_sensor import BINARY_SENSOR_DESCRIPTIONS

    desc = next(d for d in BINARY_SENSOR_DESCRIPTIONS if d.key == "cloud_connected")
    assert desc.entity_category == EntityCategory.DIAGNOSTIC
    assert desc.device_class == BinarySensorDeviceClass.CONNECTIVITY


def test_platform_registered_in_init() -> None:
    """FEAT-03 must add Platform.BINARY_SENSOR to __init__'s PLATFORMS
    list so HA actually loads the new platform on setup.
    """
    from homeassistant.const import Platform

    from custom_components.navimow import PLATFORMS

    assert Platform.BINARY_SENSOR in PLATFORMS
