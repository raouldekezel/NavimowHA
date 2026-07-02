"""MQTT credentials resolution + circuit-breaker fallback (BUG-02).

Extracted from `async_setup_entry` so the resolution can be exercised
against real production code with a mocked API + hass, rather than
tautological re-implementations inside a test file.

Two data-shape helpers (pure, no IO):
- `build_credentials_cache(mqtt_info)` — dict written back to `entry.data`
- `mqtt_info_from_cache(entry_data)` — dict rebuilt from the cache

One resolution helper (async, does IO):
- `resolve_mqtt_info(api, hass, entry)` — orchestrates the try/fallback.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant
    from mower_sdk.api import MowerAPI

_LOGGER = logging.getLogger(__name__)

CACHE_KEYS = {
    "mqttHost": "cached_mqtt_host",
    "mqttUrl": "cached_mqtt_url",
    "userName": "cached_mqtt_username",
    "pwdInfo": "cached_mqtt_password",
}


def build_credentials_cache(mqtt_info: dict[str, Any]) -> dict[str, Any]:
    """Return the `cached_mqtt_*` dict to merge into `entry.data`.

    Any missing key in `mqtt_info` cascades to `None` in the cache — the
    fallback path (`mqtt_info_from_cache`) preserves that same shape.
    """
    return {
        cache_key: mqtt_info.get(api_key) for api_key, cache_key in CACHE_KEYS.items()
    }


def mqtt_info_from_cache(entry_data: dict[str, Any]) -> dict[str, Any]:
    """Rebuild an `mqtt_info`-shaped dict from the cached entry data.

    Keys mirror the fields the API returns, so the downstream unpacking
    in `async_setup_entry` is unchanged. Missing keys become `None`.
    """
    return {
        api_key: entry_data.get(cache_key) for api_key, cache_key in CACHE_KEYS.items()
    }


async def resolve_mqtt_info(
    api: MowerAPI,
    hass: HomeAssistant,
    entry: ConfigEntry,
) -> dict[str, Any]:
    """Fetch MQTT credentials, cache them, or fall back to cached ones.

    Success path: call `api.async_get_mqtt_user_info()`, cache the four
    credentials on `entry.data` via `hass.config_entries.async_update_entry`,
    and return the fresh dict.

    Failure path (`MowerAPIError`): log a WARNING (not
    `ConfigEntryNotReady` — a raise here would keep HA in a retry loop
    against a circuit-breaking endpoint, defeating the whole point of
    the fallback), and return an `mqtt_info`-shaped dict rebuilt from
    `entry.data`. First-ever setup during an outage yields all-`None`
    values, which the SDK setup path then falls back to hard-coded
    defaults from `const.py`. Ref segwaynavimow/NavimowHA#50.
    """
    # Deferred import — this module is imported at test-collect time and
    # `mower_sdk` may not be present until the HA harness sets up.
    from mower_sdk.errors import MowerAPIError

    try:
        mqtt_info = await api.async_get_mqtt_user_info()
    except MowerAPIError as err:
        _LOGGER.warning(
            "Failed to get MQTT user info (%s) — falling back to cached/default "
            "MQTT config. Real-time MQTT updates may be unavailable until the "
            "Segway API recovers.",
            err,
        )
        return mqtt_info_from_cache(entry.data)

    cached = build_credentials_cache(mqtt_info)
    hass.config_entries.async_update_entry(entry, data={**entry.data, **cached})
    return mqtt_info
