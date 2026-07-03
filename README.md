## Experimental fork — Segway Navimow i210 LiDAR Pro.

A fork of [`segwaynavimow/NavimowHA`](https://github.com/segwaynavimow/NavimowHA)
dedicated to the Navimow i210 LiDAR Pro.

## What this fork changes (vs. upstream)

- Targets the Navimow i210 LiDAR Pro specifically.
- Adds the live mower position, read from the undocumented `/location` MQTT channel.
- Adds mowing metrics: progression, weekly mowed area and current zone.
- Adds a cloud-connectivity binary sensor that reflects the MQTT link to the Navimow cloud.
- Surfaces a silent MQTT outage within ~90 s and falls back to an HTTP poll, instead of showing stale state for up to an hour.
- Quiets the log: routine ~40-minute token-refresh reconnects no longer spam a WARNING.
- Persists the weekly mowed area across a Home Assistant restart.
- Fixes battery flicker and a spurious "#0" current-zone at the start of each mow.
- Replaces the inherited Chinese user-facing strings and developer comments with English.
- Redacts sensitive tokens and serials from logs.
- Adds a documented library of real-device MQTT traces with their interpretation ([`docs/diag/`](https://github.com/raouldekezel/NavimowHA/tree/deploy/docs/diag)), plus a pytest test suite and CI.
- Miscellaneous issues fixed (see the [Releases](https://github.com/raouldekezel/NavimowHA/releases) page).

# Navimow for Home Assistant

<p align="center">
  <img src="https://fra-navimow-prod.s3.eu-central-1.amazonaws.com/img/navimowhomeassistant.png" width="600">
</p>

Monitor and control Navimow robotic mowers in Home Assistant.

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=segwaynavimow&repository=NavimowHA&category=Integration)

## Features ✨

### Mower Control

Control your mower directly from Home Assistant:

* Start mowing
* Pause mowing
* Resume mowing
* Send mower to dock

### Device Monitoring

Keep track of mower status and health:

* Real-time mower state
* Battery level sensor
* Integration with Home Assistant dashboards

### Real-Time Communication

* **MQTT-based real-time communication**
* Fast state updates and reliable device synchronization

### Native Home Assistant Integration

* Native **`lawn_mower` entity**
* Fully compatible with **Home Assistant automations**
* Device and entity model aligned with HA standards

### Continuous Development

This integration is **under active development**.

**More features are being added all the time**, including additional sensors, diagnostics, and deeper Home Assistant automation support.

## Prerequisites 📋

- **Warning**: Home Assistant minimum version **2026.1.0**
- **Account**: your Navimow account can sign in to the official app (used for authorization)

## Installation 🛠️

This integration is not in the default HACS store. You must add it as a custom repository.

This integration will be installed as a custom repository in HACS:

1. HACS → Integrations → top-right menu → **Custom repositories**
2. Repository: `https://github.com/segwaynavimow/NavimowHA`
3. Category: Integration
4. Search for `Navimow` in HACS and install it
5. Restart Home Assistant
6. Settings → Devices & Services → Add Integration → search `Navimow`

## Usage 🎮

See the [Getting Started](https://github.com/segwaynavimow/NavimowHA/wiki/Getting-Started).

Once the integration is set up, you can control and monitor your Navimow mower using Home Assistant! 🎉

After setup, you should see:

- A `lawn_mower` entity (start/pause/dock/resume)
- A battery `sensor`

## Troubleshooting 🔧

If you encounter any issues with the Navimow integration, please check the Home Assistant logs for error messages. You can also try the following steps:

- Ensure that your mower is connected to your home network and accessible from Home Assistant.
- Restart Home Assistant and check if the issue persists.
- Make sure you are not blocking network access to services in China (if applicable to your environment).
- If you are using DNS filtering/ad-blocking, try disabling it temporarily.

If the problem continues, please file an issue on GitHub and include relevant log snippets:

- `https://github.com/segwaynavimow/NavimowHA/issues`

## Navimow SDK Library 📚

This integration uses `navimow-sdk` to communicate with Navimow mowers. `navimow-sdk` provides the Python API used by this integration (details will be expanded in the SDK documentation).
