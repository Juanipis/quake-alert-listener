<p align="center"><img src="docs/logo.svg" width="96" height="96" alt="Quake MCS Listener logo"></p>

# 🌐 Quake MCS Listener

> **Earthquake warnings, straight into your smart home.**
> One dependency-free Python file that follows official early warnings, worldwide and national quake feeds, and your own seismometer, then tells Home Assistant how hard it will shake at your place and how many seconds you have.

[![Python 3.8+](https://img.shields.io/badge/python-3.8+-blue.svg)](https://www.python.org/downloads/)
[![Dependencies: 0](https://img.shields.io/badge/dependencies-0-brightgreen.svg)](#)
[![Home Assistant](https://img.shields.io/badge/Home%20Assistant-Compatible-41BDF5.svg)](https://www.home-assistant.io/)
[![tests](https://github.com/Juanipis/quake-alert-listener/actions/workflows/tests.yml/badge.svg)](https://github.com/Juanipis/quake-alert-listener/actions/workflows/tests.yml)
[![Built with AGY, Gemini & Claude](https://img.shields.io/badge/Built%20with-AGY%20%C2%B7%20Gemini%203.8%20Flash%20%C2%B7%20Claude%20Opus%205.5-8A2BE2.svg)](#-build-acknowledgments)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

---

## 🧪 What works today

| Source | `--sources` | What it gives you | Speed | Where | Status |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **Official EEW** via [Wolfx](https://wolfx.jp) (JMA, CENC, Sichuan, Fujian, Chongqing) | `wolfx` | Early warning with magnitude and location, revised as the quake grows | **Seconds after origin**, often before the S-wave | Japan, mainland China | ✅ Live |
| **EMSC** [SeismicPortal](https://www.seismicportal.eu/realtime.html) | `emsc` | Every new or revised quake, pushed over WebSocket | Minutes (measured ~6–8 min) | Worldwide | ✅ Live |
| **USGS** real-time feed | `usgs` | Every quake of the last hour, polled every 60 s with HTTP 304 caching | Minutes | Worldwide | ✅ Live |
| **SGC** (Servicio Geológico Colombiano) | `sgc` | Colombia's national catalogue, polled every 30 s with HTTP 304 caching | Minutes | Colombia | ✅ Live, opt-in |
| **Your own sensor** ([Raspberry Shake](https://raspberryshake.org) UDP datacast) | `--shake-udp` | On-site P-wave trigger (STA/LTA) | **Seconds**, no network in between | Anywhere you install one | 🧪 Tested with synthetic signals |
| **Android device** via [`android_alert_listener.py`](docs/REDROID_SENTINEL.md) | `POST /android` | Google's Android Earthquake Alert, captured from a real (or containerized) Android | Seconds after Google alerts the device | Where Google runs AEAS | 🧪 Running on one Pi since Oct 2026, no real alert captured yet |
| **Google MCS socket** (`mtalk.google.com:5228`) | `mcs` | Working implementation of Google's push protocol, kept for research | n/a | n/a | 🔬 Connects, but Google has never delivered an alert to it ([why](docs/HOW_IT_WORKS.md#why-the-google-path-is-a-long-shot)) |

Defaults: `mcs,emsc,wolfx,usgs`. In Colombia add `sgc` (the bridge suggests it when your base station is there).

For every event, wherever it comes from, the bridge works out your local impact:
- **Estimated intensity at your base station** (MMI), using Allen, Wald & Worden (2012), the default intensity equation in USGS ShakeMap. Expect about ±1 MMI: local soil isn't modelled.
- **A countdown to the S-wave**, the strong shaking, whenever the source gives an origin time.

Notification levels mirror Android's:
- **`notice`** from estimated MMI 3.0 (`--notice-mmi`), like Android's "Be Aware".
- **`alert`** from estimated MMI 5.0 (`--alert-mmi`), like Android's "Take Action".

The same quake reported by several sources produces **one** notification; you only hear about it again if it gets worse, and a cancelled early warning is announced as cancelled.

> [!CAUTION]
> This is an independent open-source project, not a certified civil-protection warning system. Early warnings in seconds exist only where an agency publishes them (today: Japan and mainland China) or where you run your own sensor; elsewhere you get rapid reports minutes after the quake. Always keep official emergency alerts enabled on your phone.

---

## 🔬 What we learned reverse-engineering Google's alerts

Google's **Android Earthquake Alerts System (AEAS)** is the world's largest earthquake detection network, and it has no public API. We tried to listen to it directly:

1. **The wire protocol works.** `quake_listener.py` implements Google's MCS push protocol (`mtalk.google.com:5228`, TLS, version byte `41`, varint-framed protobufs) field by field against Chromium's open-source GCM client: checkin, login, heartbeats, stream acknowledgements, idle replies and the port-443 fallback. It connects and stays connected.
2. **But a socket receives nothing.** Decompiling Google Play services showed alerts arrive through `GcmReceiverChimeraService` and are decoded into `EAlertUxArgs`. Google only sends them to devices that report their location; a bare socket never matches an alert area.
3. **So we watch a real Android instead.** [`android_alert_listener.py`](android_alert_listener.py) follows a phone over ADB, or a headless Android container (Redroid) pinned to your home with a mock location, confirms the full-screen alert, and posts it to the bridge signed with HMAC-SHA256. It has run 24/7 on a Raspberry Pi 4 since 7 Oct 2026 at about 2 % CPU; it has not seen a real earthquake yet, so it is an extra layer, not the foundation.

The full protocol walkthrough is in [docs/HOW_IT_WORKS.md](docs/HOW_IT_WORKS.md); the container setup in [docs/REDROID_SENTINEL.md](docs/REDROID_SENTINEL.md).

---

## ⚡ Highlights

- **Zero dependencies:** the bridge is one file on Python's standard library (`socket`, `ssl`, `urllib`, `http.server`). No `pip install`. The Android sentinel is a separate, optional add-on.
- **Light:** about 30 MB of RAM with every source on (measured; it peaks briefly while parsing the 600 KB SGC feed) and near-zero CPU at idle. Polled feeds use conditional requests, so an unchanged feed costs an empty `304`.
- **Local impact, not just magnitude:** every payload carries `estimated_mmi`, `mmi_label`, `distance_km`, `hypocentral_km` and `s_wave_eta_s`, so automations can say *"Shaking in 18 seconds"*.
- **One policy for every source:** levels, cross-source de-duplication, escalation and cancellation are decided in one place, so adding a source never means rewriting automations.
- **Home Assistant native:** structured webhooks (optionally HMAC-signed), REST sensors, ready-made automations and a dashboard card.
- **Web console:** the [GitHub Pages app](https://juanipis.github.io/quake-alert-listener/) detects a bridge running on your machine and shows its live feeds; without one it runs a clearly-labelled simulation.

---

## 🚀 Quickstart

### Option A: try it without installing anything

Launch a temporary bridge on port 8990. The [web console](https://juanipis.github.io/quake-alert-listener/) connects to it automatically:

* **macOS / Linux:**
  ```bash
  curl -sSL https://raw.githubusercontent.com/Juanipis/quake-alert-listener/main/run.sh | bash
  ```

* **Windows (PowerShell):**
  ```powershell
  irm https://raw.githubusercontent.com/Juanipis/quake-alert-listener/main/run.ps1 | iex
  ```

* **Any OS with Python 3.8+:**
  ```bash
  curl -sSL https://raw.githubusercontent.com/Juanipis/quake-alert-listener/main/quake_listener.py | python3 - --lat <your-lat> --lon <your-lon>
  ```

* **Docker:**
  ```bash
  docker run --rm -it -p 127.0.0.1:8990:8990 python:alpine sh -c "wget -qO- https://raw.githubusercontent.com/Juanipis/quake-alert-listener/main/quake_listener.py | python3 -"
  ```
  Inside a container the REST API listens on `0.0.0.0` automatically so the published port works; `127.0.0.1:` in `-p` keeps it off your LAN.

The `run.sh` / `run.ps1` launchers estimate your location from your IP address (via ipapi.co) when you don't pass `--lat`/`--lon`.

### Option B: clone and run

```bash
git clone https://github.com/Juanipis/quake-alert-listener.git
cd quake-alert-listener

python3 quake_listener.py --lat <your-lat> --lon <your-lon> --name "Base Station" \
  --webhook-url http://<home-assistant>:8123/api/webhook/quake_alert_local
```

Check that it works without waiting for a quake:

```bash
python3 quake_listener.py --simulate     # decode a synthetic alert and preview the webhook body
curl -X POST http://127.0.0.1:8990/drill # send a level "drill" payload to your webhook
```

---

## 🐳 Run it 24/7

**Docker Compose.** Edit `QUAKE_LAT`, `QUAKE_LON` and `QUAKE_WEBHOOK_URL` in [`docker-compose.yml`](docker-compose.yml), then:

```bash
docker compose up -d --build
```

The image (~55 MB, Alpine):
- runs as an unprivileged user;
- keeps the anonymous device identity in a volume (`/data`, mode `0600`);
- reports healthy while at least one source is connected.

**systemd** (a Raspberry Pi without Docker). Use [`deploy/quake-listener.service`](deploy/quake-listener.service), a hardened unit capped at 64 MB of RAM, together with [`deploy/quake-listener.env`](deploy/quake-listener.env). Install steps are in the unit's header.

---

## 🧪 Tests

```bash
python3 -m unittest discover -s tests -v   # offline, standard library only
```

49 tests cover the Google MCS protocol (checkin layout, login, stream acks, idle replies, port fallback), protobuf decoding, the intensity model, each agency's time format, the USGS/SGC parsers and conditional requests, the detection policy (levels, escalation, cross-source dedupe, cancellations), WebSocket framing including hostile peers, the STA/LTA trigger, and the HTTP guards including `/android` signatures. CI runs them on Python 3.8, 3.10, 3.12 and 3.13.

---

## ⚙️ Configuration

Every option can also be set with its environment variable (handy for Docker and systemd). An empty variable counts as unset.

| Parameter | Environment Variable | Default | Description |
| :--- | :--- | :---: | :--- |
| `--lat` / `--lon` | `QUAKE_LAT` / `QUAKE_LON` | `0.0` | Your base station: intensity and countdowns are computed here |
| `--name` | `QUAKE_NAME` | `Base Station` | Human-readable location name |
| `--sources` | `QUAKE_SOURCES` | `mcs,emsc,wolfx,usgs` | Any of `wolfx`, `emsc`, `usgs`, `sgc`, `mcs` (`--no-emsc` drops `emsc`) |
| `--notice-mmi` | `QUAKE_NOTICE_MMI` | `3.0` | Estimated intensity at your base station that triggers a `notice` |
| `--alert-mmi` | `QUAKE_ALERT_MMI` | `5.0` | Estimated intensity that triggers an `alert` |
| `--min-magnitude` | `QUAKE_MIN_MAGNITUDE` | `0` | Ignore smaller events whatever their estimated intensity (alias `--emsc-min-mag`) |
| `--max-distance-km` | `QUAKE_MAX_DISTANCE_KM` | no limit | Ignore events farther than this (alias `--emsc-radius-km`) |
| `--webhook-url` | `QUAKE_WEBHOOK_URL` | *None* | Where payloads are posted (e.g. Home Assistant), `http://` or `https://` |
| `--webhook-secret` | `QUAKE_WEBHOOK_SECRET` | *None* | HMAC-SHA256 key for signing payloads (prefer the env var: CLI args are visible in `ps`) |
| `--android-secret` | `QUAKE_ANDROID_SECRET` | `--webhook-secret` | HMAC-SHA256 key that `POST /android` requests must be signed with. Without any secret, `/android` only accepts clients on this machine |
| `--http-port` | `QUAKE_HTTP_PORT` | `8990` | Local REST API port |
| `--http-host` | `QUAKE_HTTP_HOST` | `127.0.0.1` | Interface the REST API binds to (`0.0.0.0` automatically inside Docker/Podman) |
| `--allowed-origins` | `QUAKE_ALLOWED_ORIGINS` | `https://juanipis.github.io` | Browser origins allowed to use the REST API (`*` = any). Loopback origins and non-browser clients are always allowed |
| `--allowed-hosts` | `QUAKE_ALLOWED_HOSTS` | *None* | Extra host names the REST API answers to. IPs, `localhost` and this machine's hostname always work (`*` disables the check) |
| `--no-http` | - | `False` | Disable the REST API |
| `--shake-udp` | `QUAKE_SHAKE_UDP` | *off* | Listen for a Raspberry Shake UDP datacast on `[host:]port`, e.g. `8888` |
| `--shake-channel` | `QUAKE_SHAKE_CHANNEL` | auto | Channel to watch (first vertical one, e.g. `EHZ`/`ENZ`) |
| `--shake-sta-lta-on` / `--shake-sta-lta-off` | `QUAKE_SHAKE_STA_LTA_ON` / `_OFF` | `4.0` / `1.5` | STA/LTA trigger and release ratios |
| `--shake-alert-counts` | `QUAKE_SHAKE_ALERT_COUNTS` | `0` | Peak amplitude (counts) that makes an on-site trigger an `alert` (0 = always `notice`) |
| `--ping-interval` | `QUAKE_PING_INTERVAL` | `120` | MCS heartbeat interval in seconds (clamped to 30–600) |
| `--device-type` | `QUAKE_DEVICE_TYPE` | `android` | MCS identity profile: `android` or `chrome` |
| `--locale` / `--timezone` | `QUAKE_LOCALE` / `QUAKE_TIMEZONE` | `en_US` / `UTC` | Announced by the `android` MCS checkin |
| `--credentials-file` | `QUAKE_CREDENTIALS_FILE` | `~/.quake_device_credentials.json` | Where the anonymous MCS identity is stored (mode `0600`) |
| `--debug-frames` | `QUAKE_DEBUG_FRAMES` | `False` | Log every non-heartbeat MCS frame (protocol research) |
| `--test-ping` | - | - | One MCS connection + heartbeat round-trip, then exit |
| `--simulate` | - | - | Decode a synthetic alert and preview the webhook payload |
| `--version` | - | - | Print the version |

---

## 🏠 Home Assistant Integration

The bridge exposes a local REST API (`http://127.0.0.1:8990/status`) and posts a JSON payload to your webhook whenever an event is worth telling you about. An early warning looks like this (synthetic example):

```json
{
  "level": "alert",
  "status": "early warning",
  "source": "JMA EEW",
  "kind": "eew",
  "magnitude": 6.1,
  "region": "Test Region",
  "distance_km": 42.3,
  "depth_km": 30.0,
  "hypocentral_km": 51.9,
  "estimated_mmi": 5.3,
  "mmi_label": "V · moderate",
  "s_wave_eta_s": 9.8,
  "origin_time": "2026-01-01T00:00:00Z",
  "place": "M6.1 · Test Region · 42.3 km from Base Station · est. MMI V · S-wave in 9 s",
  "revision": 3,
  "final": false
}
```

`status` tells you what kind of information it is:

| `status` | Meaning |
| :--- | :--- |
| `early warning` | Official EEW (Wolfx) |
| `rapid report` | EMSC, USGS or SGC, minutes after the quake |
| `on-site trigger` | Your own sensor |
| `early alert` | Google's alert, captured from an Android device |
| `cancelled` | A warning you already received was withdrawn |

Every field is documented in [docs/HOW_IT_WORKS.md](docs/HOW_IT_WORKS.md#7-what-reaches-home-assistant).

In the [`homeassistant/`](homeassistant/) directory you will find ready-to-use snippets:
- `configuration.yaml`: REST sensors for the bridge status, feeds online and the last quake (with intensity and countdown attributes), plus drill/ping commands.
- `automations.yaml`: critical push with an S-wave countdown ("🚨 Shaking in 18 s"), cancellations, reports and drills.
- `dashboard.yaml`: a Lovelace card with live status and test buttons.

Webhooks are sent from a background thread (so no source ever stalls) and retried up to 3 times on network errors or HTTP 5xx. With `--webhook-secret`, every request carries an `X-Quake-Signature` header: the hex HMAC-SHA256 of the raw body.

### 🔒 Networking & Security

- **Local by default:** the REST API binds to `127.0.0.1`, so only this machine can reach it. This works as-is when Home Assistant runs on the same host (including Docker with `network_mode: host`).
- **Home Assistant in another container or machine:** start the bridge with `--http-host 0.0.0.0` and point the REST sensors at the host's IP. Only do this on a network you trust: reading `/status` and triggering drills need no authentication.
- **`POST /android` is authenticated:** with `--android-secret` every request must be HMAC-signed; without a secret, only clients on the same machine are accepted, so nobody on your LAN can make the lights flash red.
- **Browser access is restricted:** websites can only call the API if their origin is in `--allowed-origins` (the official web app and `localhost` pages are allowed by default). Other sites get no CORS headers and `403` on `POST` endpoints, so a random page can't trigger a drill or read your location.
- **DNS-rebinding guard:** requests whose `Host` header is a foreign domain name get `403`. Reaching the bridge by IP, `localhost` or this machine's hostname works as usual; add other names with `--allowed-hosts`.
- **Credentials:** the anonymous MCS identity is stored with owner-only permissions (`0600`). Webhook URLs are masked in logs and in `/status`, because Home Assistant webhook IDs act as passwords.
- **Clean shutdown:** `Ctrl+C` and `SIGTERM` (systemd, `docker stop`) close every connection and give in-flight webhooks up to 5 s to finish.

---

## 🤖 Build Acknowledgments

This project was developed collaboratively using:
- **Google Antigravity (AGY)** and **Gemini 3.8 Flash (Thinking High)**: the original MCS client, protobuf decoder and first web app.
- **Claude Opus 5.5** (Anthropic, via Claude Code): the v2 detection pipeline (official EEW feeds, EMSC, USGS and SGC, Raspberry Shake on-site trigger, intensity estimate and S-wave countdown), the protocol research in `docs/HOW_IT_WORKS.md`, the security hardening, the test suite, and the redesigned web app and logo.

Earthquake data: [Wolfx](https://wolfx.jp), relaying the Japan Meteorological Agency and the China Earthquake Networks Center; [EMSC](https://www.seismicportal.eu) (CC BY 4.0); [USGS](https://earthquake.usgs.gov/earthquakes/feed/) (public domain); [Servicio Geológico Colombiano](https://www.sgc.gov.co). Intensity model: Allen, T. I., Wald, D. J. & Worden, C. B. (2012), *Intensity attenuation for active crustal regions*, J. Seismology 16, 409–433.

---

## 📄 Legal Disclaimer

This is an independent, non-profit open-source project created for research, civil protection and home automation interoperability. It is not affiliated with, endorsed by, or sponsored by Google LLC or any of the agencies above. Android and Google are trademarks of Google LLC.

---

## ⚖️ License

Distributed under the **MIT License**. See [LICENSE](LICENSE) for more information.
