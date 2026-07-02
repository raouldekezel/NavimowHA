"""Constants for Navimow integration."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "navimow"

# OAuth2 Configuration
# 授权页面 URL（用户登录页面）
# 添加 channel=homeassistant 以便 HA 跳转回登录页时携带渠道信息
OAUTH2_AUTHORIZE: Final = (
    "https://navimow-h5-fra.willand.com/smartHome/login?channel=homeassistant"
)

# Token 交换端点
OAUTH2_TOKEN: Final = "https://navimow-fra.ninebot.com/openapi/oauth/getAccessToken"

# Token 刷新端点
OAUTH2_REFRESH: Final | None = None

# OAuth2 Client 配置
CLIENT_ID: Final = "homeassistant"
CLIENT_SECRET: Final = "57056e15-722e-42be-bbaa-b0cbfb208a52"

# API 配置
API_BASE_URL: Final = "https://navimow-fra.ninebot.com"

# MQTT 配置
# TODO: 需要提供实际的 MQTT broker 地址和端口
MQTT_BROKER: Final = "mqtt.navimow.com"
MQTT_PORT: Final = 1883
MQTT_USERNAME: Final | None = None
MQTT_PASSWORD: Final | None = None

# 更新间隔（秒）
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

# MowerStatus 到 LawnMowerActivity 的映射
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
