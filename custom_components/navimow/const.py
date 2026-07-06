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

# BUG-01 WARN emits only after this many consecutive ticks with
# is_connected=False. Debounces the ~40 min token-refresh reconnect cycle
# (sub-second in practice, per FEAT-03 diag) below the noise floor while
# preserving the alert on real broker outages (>= 90 s at
# update_interval=30 s docked; up to ~180 s mowing while `is_state_stale`
# catches up). HARD-04.
MQTT_DISCONNECT_TICKS_TO_WARN: Final = 3

# === /location extensions (FEAT-01) ===

# The position sensor throttles dispatcher emissions. The /location type 1
# payload arrives every ~2 s while mowing; only one out of every N is pushed
# to the entity to keep the recorder happy (the sensor is also excluded from
# the recorder in the documented configuration).
POSITION_THROTTLE_SECONDS: Final = 5

# Dispatcher signal to push a new position to `NavimowPositionSensor` (suffixed
# with the device_id).
SIGNAL_POSITION_UPDATE: Final = "navimow_position_update"

# FEAT-04 dispatcher signal fired the first time a `boundary_id` is seen in a
# closed run. The sensor platform (PR 3) subscribes to
# `f"{SIGNAL_ZONE_DISCOVERED}_{device_id}"` and lazy-adds the per-zone entity
# family. PR 2 emits the signal even though no listener exists yet — a
# dispatch with no listener is a documented no-op.
SIGNAL_ZONE_DISCOVERED: Final = "navimow_zone_discovered"

# FEAT-04 PR 4 — options-flow signals. `SIGNAL_ZONE_NAMES_UPDATED` fires when
# the operator changes `options["zones"]`; per-zone entities read the new
# name and call `async_write_ha_state`. `SIGNAL_ZONE_FORGOTTEN` fires when the
# operator forgets a zone; the sensor platform removes the three entities
# from the entity registry and the coordinator drops the record.
SIGNAL_ZONE_NAMES_UPDATED: Final = "navimow_zone_names_updated"
SIGNAL_ZONE_FORGOTTEN: Final = "navimow_zone_forgotten"

# FEAT-04 PR 4 — key of the options subdict holding the operator's zone
# renames. Shape: `{"1": {"name": "Prunier"}, "3": {"name": "Figuier"}}`.
# JSON serialisation forces the boundary id to a string; sensors coerce back
# to int when reading their own record.
OPTIONS_KEY_ZONES: Final = "zones"

# `vehicleState` value that means "docked and charging". The /state MQTT topic
# reports `isDocked` in both idle-on-dock and charging cases; only the /location
# `vehicleState` field distinguishes the two.
VEHICLE_STATE_CHARGING: Final = 2

# FEAT-05 layer-1 guard tolerance for future-stamped packets. The cursor is
# clamped to `now + FUTURE_TIMESTAMP_TOLERANCE_MS` before storage, so a packet
# stamped anomalously far in the future (BUG-08-style content/timestamp
# mismatch, or a robot RTC skewed ahead pre-GPS-fix) cannot poison the guard
# for longer than this window. 5 min covers realistic clock skew while
# keeping the recovery time short.
FUTURE_TIMESTAMP_TOLERANCE_MS: Final = 300_000

# FEAT-05 observability: after this many consecutive drops on a stream, emit
# one WARNING so a poisoned cursor surfaces in the log. Observed pathology
# rate is 1 gross delay per 81 committed packets, so a healthy stream never
# reaches this threshold; a truly poisoned cursor will.
STALE_DROP_STREAK_TO_WARN: Final = 25

# FEAT-05 (c) persistence + history + HA event names.
# Store payload version — bump when the shape stored on disk changes.
STORE_VERSION: Final = 1
# Cap on the persisted list of closed runs. 50 gives ~2 months of history at
# one run/day (the operator's observed cadence) while keeping the payload
# small and cheap to serialise every save.
HISTORY_MAX: Final = 50
# Heartbeat save cadence while a run is RUNNING. Never per-packet — every
# tracker transition already triggers an event-driven save. This backstop
# survives a hard crash between transitions with at most one heartbeat of
# lost progress.
TRACKER_HEARTBEAT_SECONDS: Final = 300
# HA event bus event names surfaced by the tracker. Prefixed with the
# integration domain per HA convention; step (c) fires them from
# `_forward_run_events`.
EVENT_RUN_STARTED: Final = "navimow_run_started"
EVENT_RUN_FINISHED: Final = "navimow_run_finished"


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
