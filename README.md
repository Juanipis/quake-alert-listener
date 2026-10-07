<p align="center"><img src="docs/logo.svg" width="96" height="96" alt="Quake MCS Listener logo"></p>

# 🌐 Quake MCS Listener

> **Earthquake warnings, straight into your smart home.**  
> One dependency-free Python file that follows official early warnings, a global quake feed, your own seismometer and Google's push channel, and tells Home Assistant how hard it will shake at your place and how many seconds you have.

[![Python 3.8+](https://img.shields.io/badge/python-3.8+-blue.svg)](https://www.python.org/downloads/)
[![Dependencies: 0](https://img.shields.io/badge/dependencies-0-brightgreen.svg)](#)
[![Home Assistant](https://img.shields.io/badge/Home%20Assistant-Compatible-41BDF5.svg)](https://www.home-assistant.io/)
[![tests](https://github.com/Juanipis/quake-alert-listener/actions/workflows/tests.yml/badge.svg)](https://github.com/Juanipis/quake-alert-listener/actions/workflows/tests.yml)
[![Built with AGY, Gemini & Claude](https://img.shields.io/badge/Built%20with-AGY%20%C2%B7%20Gemini%203.8%20Flash%20%C2%B7%20Claude%20Opus%205.5-8A2BE2.svg)](#-build-acknowledgments)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

---

## 🧪 What works today

| Source | What it gives you | Speed | Where | Status |
| :--- | :--- | :--- | :--- | :--- |
| **Official EEW** via [Wolfx](https://wolfx.jp) (JMA, CENC, Sichuan, Fujian, Chongqing) | Early warning with magnitude and location, revised as the quake grows | **Seconds after origin**, often before the S-wave | Japan and mainland China | ✅ Live |
| **On-site sensor** ([Raspberry Shake](https://raspberryshake.org) UDP datacast) | P-wave trigger at your own house (STA/LTA) | **Seconds**, with no network in between | Anywhere you install one | ✅ Tested with synthetic signals |
| **EMSC** [SeismicPortal](https://www.seismicportal.eu/realtime.html) | Every new or revised quake worldwide | **Minutes** (measured ~6–8 min) | Worldwide | ✅ Live |
| **Google MCS** (Android Earthquake Alerts decoder) | Would be the AEAS alert itself | Seconds | Where AEAS runs | ⚠️ Experimental, never observed |

For every event, wherever it comes from, the bridge works out your local impact:
- **Estimated intensity at your base station** (MMI), using Allen, Wald & Worden (2012), the default intensity equation in USGS ShakeMap.
- **A countdown to the S-wave**, the strong shaking.

Notification levels follow Android's own thresholds:
- **`notice`** from MMI 3, like Android's "Be Aware".
- **`alert`** from MMI 5, like Android's "Take Action".

> [!WARNING]
> **About the Google path.** The TLS connection to `mtalk.google.com:5228`, the MCS login, the heartbeats and the protobuf decoder all work. But Google sends Android Earthquake Alerts through Play services, to phones chosen by the location they report, and an anonymous client like this one is not on that list: in our captures it only receives heartbeats. The full analysis is in **[docs/HOW_IT_WORKS.md](docs/HOW_IT_WORKS.md)**. If you can show it delivering (`--debug-frames` logs), please open an issue.

> [!CAUTION]
> This is a hobby project, not a certified warning system. Estimates carry roughly ±1 MMI of uncertainty, sources can be late or silent, and an on-site trigger can be a slammed door. Always keep official alerts enabled on your phone.

---

## ⚡ Highlights

- **Zero External Dependencies:** Built entirely with Python's standard library (`socket`, `ssl`, `struct`, `urllib`). No `pip install`, heavy runtimes, or Android emulators required.
- **Ultra-Lightweight:** Consumes less than **15 MB of RAM** and **0.0% CPU** at idle. Perfect for running 24/7 on a Raspberry Pi, home server, or Docker container.
- **Configurable Keepalive Pings:** Network-friendly ping intervals (`--ping-interval 120`, configurable between 30s and 600s) with exponential backoff on reconnects.
- **Four Sources, One Policy:** official early warnings (Wolfx relay of JMA / CENC), EMSC's worldwide feed, an optional Raspberry Shake on-site trigger, and Google's MCS channel. Duplicates of the same quake across sources are merged; you're only notified again if things get worse, and cancelled warnings are announced as cancelled.
- **Chrome-Accurate MCS Client:** checkin, login, stream acknowledgements, idle replies and the port-443 fallback follow Chromium's own GCM client; identities re-check in every 2 days, and Google's clock is used to keep countdowns honest (see [HOW_IT_WORKS §1–3](docs/HOW_IT_WORKS.md#1-getting-an-identity-checkin)).
- **Local Impact, Not Just Magnitude:** every payload carries `estimated_mmi`, `mmi_label`, `distance_km`, `hypocentral_km` and `s_wave_eta_s`, so automations can say *"Shaking in 18 seconds"*.
- **Home Assistant Native Integration:** Automatically dispatches structured local webhooks to Home Assistant including magnitude, coordinates, epicenter distance in kilometers, and severity level.
- **Interactive Web Demo:** Includes an interactive [GitHub Pages](https://juanipis.github.io/quake-alert-listener/) web app that detects your local coordinates and generates a ready-to-run CLI command for your location.

---

## 🚀 Quickstart

### Option A: Zero-Install 1-Line Bridge (0 Installs, 0 Git Clone)

Run directly in your terminal to launch a temporary local bridge on port 8990. The [Live Web Interface](https://juanipis.github.io/quake-alert-listener/) will automatically detect your machine and connect in real-time:

* **macOS / Linux (Bash/Zsh):**
  ```bash
  curl -sSL https://raw.githubusercontent.com/Juanipis/quake-alert-listener/main/run.sh | bash
  ```

* **Windows (PowerShell):**
  ```powershell
  irm https://raw.githubusercontent.com/Juanipis/quake-alert-listener/main/run.ps1 | iex
  ```

* **Universal Python 1-Liner:**
  ```bash
  curl -sSL https://raw.githubusercontent.com/Juanipis/quake-alert-listener/main/quake_listener.py | python3 -
  ```

* **Docker (Any OS):**
  ```bash
  docker run --rm -it -p 127.0.0.1:8990:8990 python:alpine sh -c "wget -qO- https://raw.githubusercontent.com/Juanipis/quake-alert-listener/main/quake_listener.py | python3 -"
  ```
  Inside a container the REST API listens on `0.0.0.0` automatically so the published port works; `127.0.0.1:` in `-p` keeps it off your LAN.

---

### Option B: Clone & Run (Raspberry Pi & Home Servers 24/7)

```bash
git clone https://github.com/Juanipis/quake-alert-listener.git
cd quake-alert-listener

# Run with your base coordinates (replace the placeholders with your own values)
python3 quake_listener.py --lat <your-lat> --lon <your-lon> --name "Base Station"
```

### 2. Verify Connection in 1 Second

Verify that the TLS tunnel and protocol authentication respond in milliseconds:

```bash
python3 quake_listener.py --test-ping
```

Expected output:
```text
[2026-10-07 10:20:12] Testing TLS connection to mtalk.google.com:5228...
[2026-10-07 10:20:13] Authenticated with mtalk.google.com:5228 (Handshake latency: 358.1 ms)
[2026-10-07 10:20:13] Test completed! Handshake latency: 358.1 ms, heartbeat round-trip: 77.5 ms. Connection verified.
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

**systemd** (a Raspberry Pi without Docker). Use [`deploy/quake-listener.service`](deploy/quake-listener.service), a hardened unit, together with [`deploy/quake-listener.env`](deploy/quake-listener.env). Install steps are in the unit's header.

---

## 🧪 Tests

```bash
python3 -m unittest discover -s tests -v   # offline, standard library only
```

The suite covers the Google MCS protocol (checkin layout, login, stream acks, idle replies, port fallback), protobuf decoding, the intensity model, time zones of each agency, the detection policy (levels, escalation, cross-source dedupe, cancellations), WebSocket framing including hostile peers, the STA/LTA trigger and the HTTP guards. CI runs it on Python 3.8 to 3.13.

---

## ⚙️ Configuration Parameters

| Parameter | Environment Variable | Default | Description |
| :--- | :--- | :---: | :--- |
| `--lat` | `QUAKE_LAT` | `0.0` | Latitude of your base location |
| `--lon` | `QUAKE_LON` | `0.0` | Longitude of your base location |
| `--name` | `QUAKE_NAME` | `Base Station` | Human-readable location name |
| `--ping-interval` | `QUAKE_PING_INTERVAL` | `120` | Interval in seconds between pings (30 to 600s) |
| `--http-port` | `QUAKE_HTTP_PORT` | `8990` | Local HTTP REST server port for Home Assistant |
| `--http-host` | `QUAKE_HTTP_HOST` | `127.0.0.1` | Interface the REST server binds to (`0.0.0.0` automatically inside Docker/Podman). Use `0.0.0.0` to reach it from other machines or Docker networks |
| `--allowed-origins` | `QUAKE_ALLOWED_ORIGINS` | `https://juanipis.github.io` | Comma-separated browser origins allowed to use the REST API (`*` = any). Loopback origins and non-browser clients (curl, Home Assistant) are always allowed |
| `--allowed-hosts` | `QUAKE_ALLOWED_HOSTS` | *None* | Extra host names the REST API answers to. IP addresses, `localhost` and this machine's hostname always work (`*` disables the check) |
| `--no-http` | - | `False` | Disable the local HTTP REST telemetry server |
| `--webhook-url` | `QUAKE_WEBHOOK_URL` | *None* | Destination webhook URL (e.g. Home Assistant), `http://` or `https://` |
| `--webhook-secret` | `QUAKE_WEBHOOK_SECRET` | *None* | Optional secret key for HMAC-SHA256 payload signing (prefer the env var: CLI args are visible in `ps`) |
| `--credentials-file` | `QUAKE_CREDENTIALS_FILE` | `~/.quake_device_credentials.json` | Where the anonymous device identity is stored (written with `0600` permissions) |
| `--locale` / `--timezone` | `QUAKE_LOCALE` / `QUAKE_TIMEZONE` | `en_US` / `UTC` | Accepted for compatibility; since v2.1 nothing is sent (Chrome's checkin doesn't include them) |
| `--sources` | `QUAKE_SOURCES` | `mcs,emsc,wolfx` | Push sources to follow (`--no-emsc` drops `emsc`) |
| `--notice-mmi` | `QUAKE_NOTICE_MMI` | `3.0` | Estimated intensity at your base station that triggers a `notice` |
| `--alert-mmi` | `QUAKE_ALERT_MMI` | `5.0` | Estimated intensity that triggers an `alert` |
| `--min-magnitude` | `QUAKE_MIN_MAGNITUDE` | `0` | Ignore smaller events whatever their estimated intensity (alias `--emsc-min-mag`) |
| `--max-distance-km` | `QUAKE_MAX_DISTANCE_KM` | no limit | Ignore events farther than this (alias `--emsc-radius-km`) |
| `--shake-udp` | `QUAKE_SHAKE_UDP` | *off* | Listen for a Raspberry Shake UDP datacast on `[host:]port`, e.g. `8888` |
| `--shake-channel` | `QUAKE_SHAKE_CHANNEL` | auto | Channel to watch (first vertical one, e.g. `EHZ`/`ENZ`) |
| `--shake-sta-lta-on` / `--shake-sta-lta-off` | `QUAKE_SHAKE_STA_LTA_ON` / `_OFF` | `4.0` / `1.5` | STA/LTA trigger and release ratios |
| `--shake-alert-counts` | `QUAKE_SHAKE_ALERT_COUNTS` | `0` | Peak amplitude (counts) that makes an on-site trigger an `alert` (0 = always `notice`) |
| `--debug-frames` | `QUAKE_DEBUG_FRAMES` | `False` | Log a one-line summary of every non-heartbeat MCS frame (protocol research) |
| `--test-ping` | - | - | Executes a single diagnostic ping and exits |
| `--simulate` | - | - | Tests internal Protobuf event decoding |
| `--version` | - | - | Prints the listener version |

---

## 🏠 Home Assistant Integration

The listener exposes a local REST telemetry API (`http://127.0.0.1:8990/status`) and posts a JSON payload to your webhook whenever an event is worth telling you about. An early warning looks like this (synthetic example):

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
| `early warning` | Official EEW |
| `rapid report` | EMSC, minutes after the quake |
| `on-site trigger` | Your own sensor |
| `early alert` | Google AEAS |
| `cancelled` | A warning you already received was withdrawn |

Every field is documented in [docs/HOW_IT_WORKS.md](docs/HOW_IT_WORKS.md#7-what-reaches-home-assistant).

In the [`homeassistant/`](homeassistant/) directory, you will find ready-to-use snippets:
- `configuration.yaml`: REST sensors for link status, latency, feeds online and the last quake (with intensity and countdown attributes), plus drill/ping commands.
- `automations.yaml`: critical push with an S-wave countdown ("🚨 Shaking in 18 s"), cancellations, reports and drills.
- `dashboard.yaml`: Modern Lovelace dashboard card with real-time status and interactive test buttons.

Webhooks are sent from a background thread (so the MCS connection never stalls) and retried up to 3 times on network errors or HTTP 5xx. With `--webhook-secret`, every request carries an `X-Quake-Signature` header: the hex HMAC-SHA256 of the raw body. `latency_ms` in `/status` and `POST /ping` is the measured heartbeat round-trip to Google's server.

### 🔒 Networking & Security

- **Local by default:** the REST API binds to `127.0.0.1`, so only this machine can reach it. This works as-is when Home Assistant runs on the same host (including Docker with `network_mode: host`).
- **Home Assistant in another container or machine:** start the listener with `--http-host 0.0.0.0` (or set `QUAKE_HTTP_HOST=0.0.0.0`) and point the REST sensors at the host's IP. Only do this on a network you trust: the API has no authentication.
- **Browser access is restricted:** websites can only call the API if their origin is in `--allowed-origins` (the official web app and `localhost` pages are allowed by default). Other sites get no CORS headers and `403` on `POST /drill` and `/ping`, so a random page can't trigger a drill or read your location. Pass `--allowed-origins "*"` to restore the old allow-all behaviour.
- **DNS-rebinding guard:** requests whose `Host` header is a foreign domain name get `403`, so a malicious site can't point its own domain at `127.0.0.1` and read the API same-origin. Reaching the bridge by IP, `localhost` or this machine's hostname works as usual; add other names with `--allowed-hosts`.
- **Credentials:** the anonymous device identity is stored with owner-only permissions (`0600`); existing files are tightened automatically. Webhook URLs are masked in logs and in `/status` because Home Assistant webhook IDs act as passwords.
- **Clean shutdown:** `Ctrl+C`, `SIGTERM` (systemd, `docker stop`) close the MCS connection and the HTTP server cleanly and give in-flight webhooks up to 5 s to finish.

---

## 🤖 Build Acknowledgments

This project was developed collaboratively using:
- **Google Antigravity (AGY)** and **Gemini 3.8 Flash (Thinking High)**: the original MCS client, protobuf decoder and first web app.
- **Claude Opus 5.5** (Anthropic, via Claude Code): the v2 detection pipeline (official EEW feeds, EMSC, Raspberry Shake on-site trigger, intensity estimate and S-wave countdown), the protocol research in `docs/HOW_IT_WORKS.md`, the security hardening, the test suite, and the redesigned web app and logo.

Earthquake data: [EMSC](https://www.seismicportal.eu) (CC BY 4.0) and [Wolfx](https://wolfx.jp), relaying the Japan Meteorological Agency and the China Earthquake Networks Center. Intensity model: Allen, T. I., Wald, D. J. & Worden, C. B. (2012), *Intensity attenuation for active crustal regions*, J. Seismology 16, 409–433.

---

## 📄 Legal Disclaimer

This is an independent, non-profit open-source project created solely for research, public civil protection, and home automation interoperability. It is not affiliated with, endorsed by, or sponsored by Google LLC. Android and Google are registered trademarks of their respective owners.

---

## ⚖️ License

Distributed under the **MIT License**. See [LICENSE](LICENSE) for more information.
