"""Constants for Navimow integration."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "navimow"

# OAuth2 Configuration
# Authorization page URL (user login page)
# Add channel=homeassistant so the channel info is carried when HA redirects back to the login page
OAUTH2_AUTHORIZE: Final = (
    "https://navimow-h5-fra.willand.com/smartHome/login?channel=homeassistant"
)

# Token exchange endpoint
OAUTH2_TOKEN: Final = "https://navimow-fra.ninebot.com/openapi/oauth/getAccessToken"

# Token refresh endpoint
OAUTH2_REFRESH: Final | None = None

# OAuth2 client configuration
CLIENT_ID: Final = "homeassistant"
CLIENT_SECRET: Final = "57056e15-722e-42be-bbaa-b0cbfb208a52"

# API configuration
API_BASE_URL: Final = "https://navimow-fra.ninebot.com"

# MQTT configuration
# TODO: provide the actual MQTT broker address and port
MQTT_BROKER: Final = "mqtt.navimow.com"
MQTT_PORT: Final = 1883
MQTT_USERNAME: Final | None = None
MQTT_PASSWORD: Final | None = None

# Update interval (seconds)
UPDATE_INTERVAL: Final = 30

# MQTT staleness threshold (seconds). Beyond this without an MQTT push, the
# coordinator falls back to an HTTP poll. Reduced from 300 to 90 so silent
# MQTT outages surface in HA within ~90 s instead of ~5 min (BUG-01).
MQTT_STALE_SECONDS: Final = 90

# HTTP fallback minimum interval (seconds) — throttles the fallback poll to
# avoid hammering the cloud when MQTT is intermittently degraded. Reduced from
# 3600 (1 h) to 60 so a stuck HA entity recovers in ~1 min instead of ~1 h
# (BUG-01).
HTTP_FALLBACK_MIN_INTERVAL: Final = 60

# MQTT protocol-layer keepalive (seconds), used to detect half-open connections
# faster than the cloud's own 1-hour connection drop. Reduced from an implicit
# 2400 to 120 (BUG-01).
MQTT_KEEPALIVE_SECONDS: Final = 120

# === /location extensions (FEAT-01) ===

# The position sensor throttles dispatcher emissions. The /location type 1
# payload arrives every ~2 s while mowing; only one out of every N is pushed
# to the entity to keep the recorder happy (the sensor is also excluded from
# the recorder in the documented configuration).
POSITION_THROTTLE_SECONDS: Final = 5

# Dispatcher signal to push a new position to `NavimowPositionSensor` (suffixed
# with the device_id).
SIGNAL_POSITION_UPDATE: Final = "navimow_position_update"

# `vehicleState` value that means "docked and charging". The /state MQTT topic
# reports `isDocked` in both idle-on-dock and charging cases; only the /location
# `vehicleState` field distinguishes the two.
VEHICLE_STATE_CHARGING: Final = 2


# Mapping from MowerStatus to LawnMowerActivity
MOWER_STATUS_TO_ACTIVITY = {
    "idle": "docked",
    "mowing": "mowing",
    "paused": "paused",
    "docked": "docked",
    "charging": "docked",
    "returning": "returning",
    "error": "error",
    "unknown": "error",
}
