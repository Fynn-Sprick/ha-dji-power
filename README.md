# DJI Power Station — Home Assistant Integration

[![HACS Custom](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)
[![GitHub Release](https://img.shields.io/github/v/release/fynnsprick/ha-dji-power)](https://github.com/fynnsprick/ha-dji-power/releases)

Integrates **DJI Power Stations** into Home Assistant via the DJI Home cloud API and MQTT — with live push updates (~1 second latency) and full Energy Dashboard support.

## Supported Devices

| Device | Status |
|--------|--------|
| DJI Power 1000 V2 | ✅ Tested |
| DJI Power 1000 | ⚠️ Untested (likely works) |
| DJI Power 500 | ⚠️ Untested (likely works) |

Please open an issue if you have a different model — feedback welcome.

---

## Features

| Entity | Type | Description |
|--------|------|-------------|
| State of Charge | Sensor | Battery level (%) |
| Input Power | Sensor | Charging power (W) — live |
| Output Power | Sensor | Load power (W) — live |
| Energy In | Sensor | Total energy charged (kWh) — Energy Dashboard |
| Energy Out | Sensor | Total energy discharged (kWh) — Energy Dashboard |
| Battery Temperature | Sensor | Battery temperature (°C) |
| Remaining Time | Sensor | Minutes until empty / full |
| Charge Source | Sensor | `not_charging` / `ac` / `solar` / `car` / `dc` |
| Online | Binary Sensor | Whether device is reachable |
| Charging | Binary Sensor | Whether device is currently charging |

Updates arrive via **MQTT push** (~1 s latency). REST polling every 60 s as fallback.

### Energy Dashboard

The **Energy In** and **Energy Out** sensors let you add the DJI Power Station as a battery storage system in the HA Energy Dashboard:

1. Go to **Settings → Dashboards → Energy → Battery Systems → Add Battery**
2. *Energy charged into battery* → `Energy In`
3. *Energy discharged from battery* → `Energy Out`
4. *Power measurement type* → **Two sensors** → `Input Power` + `Output Power`

---

## Installation

### HACS (recommended)

1. Open HACS → Integrations → ⋮ → **Custom repositories**
2. Add `https://github.com/fynnsprick/ha-dji-power` (category: Integration)
3. Install **DJI Power Station**
4. Restart Home Assistant

### Manual

Copy `custom_components/dji_power/` into your HA `config/custom_components/` folder and restart.

---

## Setup

Go to **Settings → Devices & Services → Add Integration → DJI Power Station**.

You will be asked for an **x-member-token**. See [Getting Your Token](#getting-your-token) below.

---

## Getting Your Token

The DJI Home app uses a proprietary auth flow. You need to extract the `x-member-token` once from a running instance of the DJI Home app using ADB.

### Requirements

- ADB (Android SDK Platform-Tools)
- An Android emulator (**Android 8.1, API 27, `google_apis` image — not `google_play`**) or a rooted Android phone

### Step 1 — Set up an emulator (skip if you have a rooted phone)

1. Install [Android Studio](https://developer.android.com/studio)
2. Create a Virtual Device: **Pixel 2 XL**, system image **Android 8.1 (API 27) — google\_apis / arm64-v8a**, name it `Pixel_2_XL`
3. Start the emulator
4. Install the DJI Home APK:
   ```bash
   adb install path/to/dji-home.apk
   ```
   > The APK is not included here. Download it from a trusted APK mirror or extract it from your own Android device.

### Step 2 — Log into DJI Home

Open the DJI Home app on the emulator/device, log in with your DJI account, and navigate to your Power Station so the app fetches your data.

### Step 3 — Extract the token

```bash
# Become root on the emulator
adb root

# Find the DJI Home process ID
adb shell pidof com.dji.home

# Scan memory for the token (replace <PID> with the output above)
adb shell "grep -oa 'US_[A-Za-z0-9_-]\{80,\}_v2' /proc/<PID>/mem 2>/dev/null | head -1"
```

The token looks like `US_XXXX...XXXX_v2`. Copy it.

### Step 4 — Enter in Home Assistant

Paste the token into the integration setup form in HA.

> **Token lifetime**: The `x-member-token` is long-lived (weeks to months). If the integration stops working, re-extract and update via **Settings → Devices & Services → DJI Power Station → Reconfigure**.

---

## Technical Details

| Item | Value |
|------|-------|
| API Base | `https://home-api-vg.djigate.com` |
| MQTT Broker | `crobot-mqtt-us.djigate.com:8883` (TLS) |
| MQTT Topic | `forward/dy/thing/product/{SN}/property` |
| MQTT Auth | username = `user_uuid`, password = short-lived MQTT token (auto-refreshed) |
| Auth Header | `x-member-token` (long-lived mobile session token) |

---

## Contributing

PRs welcome! Especially for:

- Automated email/password login (Sign-Mc header reverse engineering)
- Additional device models
- EU/Asia region support (`home-api-eu.djigate.com`)
