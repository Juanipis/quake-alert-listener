# 🚀 Product Hunt Launch Kit • Quake MCS Listener

This document contains everything needed to launch **Quake MCS Listener** on [Product Hunt](https://www.producthunt.com/posts/new) using **Claude Code + Claude in Chrome**, plus frontend design upgrades using **Stitch** and design skills.

---

## 📌 Core Metadata for Product Hunt

| Field | Value |
| :--- | :--- |
| **Name of Product** | `Quake MCS Listener` |
| **Tagline** (max 60 chars) | `Real-time earthquake alerts for Home Assistant (<15MB)` |
| **Links** | **Website:** `https://juanipis.github.io/quake-alert-listener/`<br>**GitHub:** `https://github.com/Juanipis/quake-alert-listener` |
| **Logo / Thumbnail** | `docs/logo.svg` (High-resolution squircle with seismic radar waveform) |
| **Topics / Tags** | `Home Automation`, `Developer Tools`, `Open Source`, `IoT`, `Smart Home` |
| **Pricing** | `Free / Open Source (MIT)` |

---

## 📝 Product Description & Pitch

### Short Description
> An autonomous, ultra-lightweight standard Python client (<15 MB RAM, 0% CPU, 0 external dependencies) that connects directly to the Android Earthquake Alerts System (AEAS) via Google's MCS push protocol (`mtalk.google.com:5228`) to deliver early earthquake warnings to Home Assistant, local alarms, and smart homes in milliseconds.

### The Problem
Traditional earthquake monitoring integrations for smart homes often rely on polling third-party public APIs (like USGS or EMSC) every 30–60 seconds. For seismic events, seconds matter: by the time an API poll runs, shaking has already arrived.

### The Solution
Google's Android Earthquake Alerts System (AEAS) is the world's largest crowdsourced seismic detection network. Android phones detect tremors and send them to Google's cloud, which immediately broadcasts push notifications through the binary Mobile Connection Server (MCS) protocol over TLS.

**Quake MCS Listener** connects directly to this high-speed push pipeline using pure standard Python—no Android emulators, no heavy runtimes, no dependencies.

### Key Highlights
- **⚡ Zero External Dependencies:** Built 100% on Python's standard library (`socket`, `ssl`, `struct`, `urllib`).
- **🪶 Ultra-Lightweight:** Consumes under 15 MB of RAM and 0% CPU at idle. Perfect for running 24/7 on a Raspberry Pi or home server.
- **🤖 Autonomous Micro-Android Sentinel:** Supports containerized Android 11 (Redroid) running 24/7 on Linux/RPi with system mock location beacons, kernel logcat streaming (<10 ms), and zero false alarm protection.
- **🛡️ Multilayer Redundancy:** Integrates Google AEAS, official national feeds (SGC Colombia), global feeds (USGS, EMSC), and official EEW (Wolfx/JMA), with zero-bandwidth HTTP 304 conditional caching.
- **🏠 Native Home Assistant Integration:** Includes an embedded REST telemetry API (`:8990`), plug-and-play sensors, automations, and Lovelace cards.
- **🚀 1-Line Zero-Install Bridge:** Anyone on macOS/Linux or Windows can run a single command (`run.sh` / `run.ps1`) in a temporary sandbox that instantly links with the web portal.
- **🌐 Interactive Web Demo:** Live map with local alert radii, simulated MCS packets, and Web Audio API emergency sirens.

---

## 💬 Maker's First Comment

```markdown
Hey Product Hunt! 👋

I'm excited to share **Quake MCS Listener**—an open-source project designed to bridge the world's fastest earthquake alert network directly into personal smart homes.

### Why we built this:
In an earthquake, early warning is everything. Existing smart home earthquake integrations rely on polling government APIs every 30 to 60 seconds, which is simply too late. 

The Android Earthquake Alerts System (AEAS) detects tremors via phone accelerometers and broadcasts alerts in seconds over Google's binary Mobile Connection Server (MCS) protocol. We set out to build a lightweight, completely autonomous client that could connect directly to this stream without requiring an Android emulator or heavy runtimes.

### What it does:
- Runs natively on Python standard library with 0 pip packages.
- Uses less than 15 MB of RAM on a Raspberry Pi.
- Exposes a local REST telemetry API on port 8990 and dispatches instant webhooks to Home Assistant to trigger lights, audio sirens, or safety shutoffs.
- Comes with an interactive web portal and 1-line zero-install commands for macOS, Linux, and Windows.

### Community Validation:
The TLS handshake, keepalive pings, and Protobuf event parser are fully verified. Because real earthquakes can't be scheduled on demand, we're inviting developers and smart home enthusiasts—especially in active seismic zones—to test the bridge and share feedback!

Check out the live interactive web demo:
👉 https://juanipis.github.io/quake-alert-listener/

Source code & Home Assistant configs:
👉 https://github.com/Juanipis/quake-alert-listener

I’d love to hear your thoughts, feedback, and ideas! 🌍🔔
```

---

## 🎨 Design & Frontend Upgrades (Stitch Guidelines)

When enhancing `docs/index.html` using Claude's design skills & Stitch:

1. **Aesthetic Direction:**
   - **Dark Tech / Glassmorphic Bento-Grid:** Slate/Navy background (`#030712`, `#0a0f1d`), subtle border glow (`rgba(56, 189, 248, 0.15)`), and blurred glass backdrops.
   - **Accent Palette:** Electric Cyan (`#38bdf8`), Signal Green (`#22c55e`), Emergency Crimson (`#ef4444`), Warning Amber (`#eab308`).

2. **Interactive Elements:**
   - **Seismic Waveform Live Canvas:** A smooth pulsating canvas wave that responds to simulated or real heartbeat packets.
   - **Tactile Sound & Drill Controls:** Audio siren with frequency sweeps (Web Audio API) and TTS voice announcements.
   - **Terminal Stream Enhancements:** Real-time log inspector with syntax highlighting for tags (`IqStanza`, `HeartbeatAck`, `DataMessage`).

3. **Assets:**
   - Use `docs/logo.svg` as the primary brand emblem and favicon.
