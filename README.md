# 🌐 Quake MCS Listener

> **Lightweight, autonomous Python client for the Android Earthquake Alerts System (AEAS)**  
> Direct, low-latency earthquake early warning integration for Home Assistant and local home automation.

[![Python 3.8+](https://img.shields.io/badge/python-3.8+-blue.svg)](https://www.python.org/downloads/)
[![Dependencies: 0](https://img.shields.io/badge/dependencies-0-brightgreen.svg)](#)
[![Home Assistant](https://img.shields.io/badge/Home%20Assistant-Compatible-41BDF5.svg)](https://www.home-assistant.io/)
[![Built with AGY & Gemini 3.8 Flash](https://img.shields.io/badge/Built%20with-AGY%20%26%20Gemini%203.8%20Flash%20(Thinking%20High)-8A2BE2.svg)](#)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

---

## 🧪 Project Status & Community Call

> [!WARNING]
> **Experimental Validation Notice:**  
> We arrived at this lightweight, autonomous implementation using strictly the Python standard library. The direct TLS connection (`mtalk.google.com:5228`), protocol authentication handshake, and Protobuf stanza decoders are fully verified and operational. However, **we are currently awaiting a live, natural earthquake event in our test region to 100% validate in-the-wild dispatch**.  
> 
> If you live in a seismically active region and wish to run the listener to test, monitor, or report observations, **your testing and community feedback are warmly welcomed!**

---

## ⚡ Highlights

- **Zero External Dependencies:** Built entirely with Python's standard library (`socket`, `ssl`, `struct`, `urllib`). No `pip install`, heavy runtimes, or Android emulators required.
- **Ultra-Lightweight:** Consumes less than **15 MB of RAM** and **0.0% CPU** at idle. Perfect for running 24/7 on a Raspberry Pi, home server, or Docker container.
- **Configurable Keepalive Pings:** Network-friendly ping intervals (`--ping-interval 120`, configurable between 30s and 600s) with exponential backoff on reconnects.
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
  docker run --rm -it -p 8990:8990 python:alpine sh -c "wget -qO- https://raw.githubusercontent.com/Juanipis/quake-alert-listener/main/quake_listener.py | python3 -"
  ```

---

### Option B: Clone & Run (Raspberry Pi & Home Servers 24/7)

```bash
git clone https://github.com/Juanipis/quake-alert-listener.git
cd quake-alert-listener

# Run with your base coordinates (example: San Francisco, CA)
python3 quake_listener.py --lat 37.7749 --lon -122.4194 --name "San Francisco, CA"
```

### 2. Verify Connection in 1 Second

Verify that the TLS tunnel and protocol authentication respond in milliseconds:

```bash
python3 quake_listener.py --test-ping
```

Expected output:
```text
[2026-10-07 10:20:12] Testing TLS connection to mtalk.google.com:5228...
[2026-10-07 10:20:13] Authenticated via MCS v41 (Handshake: 34.2 ms).
[2026-10-07 10:20:13] Test completed! Handshake latency: 34.2 ms. Connection verified.
```

---

## ⚙️ Configuration Parameters

| Parameter | Environment Variable | Default | Description |
| :--- | :--- | :---: | :--- |
| `--lat` | `QUAKE_LAT` | `0.0` | Latitude of your base location |
| `--lon` | `QUAKE_LON` | `0.0` | Longitude of your base location |
| `--name` | `QUAKE_NAME` | `Base Station` | Human-readable location name |
| `--ping-interval` | `QUAKE_PING_INTERVAL` | `120` | Interval in seconds between pings (30 to 600s) |
| `--http-port` | `QUAKE_HTTP_PORT` | `8990` | Local HTTP REST server port for Home Assistant |
| `--no-http` | - | `False` | Disable the local HTTP REST telemetry server |
| `--webhook-url` | `QUAKE_WEBHOOK_URL` | *None* | Destination webhook URL (e.g. Home Assistant) |
| `--webhook-secret` | `QUAKE_WEBHOOK_SECRET` | *None* | Optional secret key for HMAC-SHA256 payload signing |
| `--test-ping` | - | - | Executes a single diagnostic ping and exits |
| `--simulate` | - | - | Tests internal Protobuf event decoding |

---

## 🏠 Home Assistant Integration

The listener exposes a local REST telemetry API (`http://127.0.0.1:8990/status`) and dispatches structured JSON payloads to Home Assistant whenever an earthquake alert is received:

```json
{
  "level": "alert",
  "source": "Android AEAS (MCS)",
  "id": "aeas-1791386413",
  "magnitude": 5.4,
  "distance_km": 42.1,
  "lat": 37.77,
  "lon": -122.41,
  "place": "M5.4 at 42.1 km from Base Station (Test Region)",
  "timestamp": "2026-10-07 10:20:13",
  "status": "early alert"
}
```

In the [`homeassistant/`](homeassistant/) directory, you will find ready-to-use snippets:
- `configuration.yaml`: REST sensors to track connection status, latency, pings, and service commands.
- `automations.yaml`: Safe light automation and critical push notification triggers.
- `dashboard.yaml`: Modern Lovelace dashboard card with real-time status and interactive test buttons.

---

## 🤖 Build Acknowledgments

This project was developed collaboratively using:
- **Google Antigravity (AGY)**
- **Gemini 3.8 Flash (Thinking High)**

---

## 📄 Legal Disclaimer

This is an independent, non-profit open-source project created solely for research, public civil protection, and home automation interoperability. It is not affiliated with, endorsed by, or sponsored by Google LLC. Android and Google are registered trademarks of their respective owners.

---

## ⚖️ License

Distributed under the **MIT License**. See [LICENSE](LICENSE) for more information.
