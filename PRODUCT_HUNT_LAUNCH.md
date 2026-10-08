# 🚀 Product Hunt Launch Kit • Quake MCS Listener

Everything needed to launch **Quake MCS Listener** on [Product Hunt](https://www.producthunt.com/posts/new). Every claim below matches the README and the code; keep it that way when editing.

---

## 📌 Core Metadata

| Field | Value |
| :--- | :--- |
| **Name** | `Quake MCS Listener` |
| **Tagline** (max 60 chars) | `Earthquake warnings with a countdown, for Home Assistant` |
| **Links** | **Website:** `https://juanipis.github.io/quake-alert-listener/`<br>**GitHub:** `https://github.com/Juanipis/quake-alert-listener` |
| **Logo / Thumbnail** | `docs/logo.svg` (social card: `docs/og.png`) |
| **Topics** | `Home Automation`, `Open Source`, `IoT`, `Smart Home`, `Developer Tools` |
| **Pricing** | `Free / Open Source (MIT)` |

---

## 📝 Description & Pitch

### Short description
> One dependency-free Python file that follows official earthquake early warnings, the EMSC, USGS and SGC feeds and your own seismometer, estimates how hard each quake will shake at your home and how many seconds remain until the strong shaking, and sends it to Home Assistant.

### The problem
Most smart-home earthquake integrations show a magnitude and a distance, minutes after the fact. What you need to automate is different: *will it shake here, how hard, and how long do I have?*

### The solution
Quake MCS Listener turns every report into local impact: an estimated intensity at your base station (the same equation USGS ShakeMap uses) and an S-wave countdown when the source gives an origin time. One policy decides what deserves a notification, merges the same quake from several sources, escalates only if it gets worse, and announces cancelled warnings.

### Key highlights
- **⚡ Zero dependencies:** one file on Python's standard library, Python 3.8+.
- **🪶 Light:** about 30 MB of RAM with every source on; unchanged polled feeds cost an empty HTTP 304.
- **📡 Sources, with honest status:** official early warnings for Japan and China (seconds), EMSC, USGS and Colombia's SGC (minutes), a Raspberry Shake on-site trigger (tested with synthetic signals), and an experimental Android sentinel for Google's alerts (tried in Oct 2026, no real alert captured, paused).
- **🏠 Home Assistant native:** signed webhooks, REST sensors, a countdown automation ("🚨 Shaking in 18 s") and a dashboard card.
- **🚀 Try it in one line:** `run.sh` / `run.ps1` start a temporary bridge that the web console finds by itself. Docker Compose and a hardened systemd unit for 24/7.
- **🔬 Research included:** a working client for Google's MCS push protocol, verified against Chromium, and the write-up of why a bare socket never receives Google's alerts.

---

## 💬 Maker's First Comment

```markdown
Hey Product Hunt! 👋

Quake MCS Listener started as an attempt to tap Google's Android Earthquake Alerts directly from a Raspberry Pi. That part taught us a lot and didn't work the way we hoped, so the project became something more useful: one bridge that takes every earthquake source you can get and tells your smart home what it means *for your house*.

What it does today:
- Follows official early warnings (Japan's JMA and China's CENC, via Wolfx), the EMSC, USGS and Colombian SGC feeds, and a Raspberry Shake if you have one.
- Estimates the intensity at your place and the seconds left until the S-wave, then sends one webhook per quake to Home Assistant.
- Runs as one Python file with zero dependencies, about 30 MB of RAM.

The honest limits:
- Early warnings in seconds only exist where an agency publishes them, or where you run your own sensor. Elsewhere you get reports minutes after the quake: still great for automations and peace of mind, not for ducking under a table.
- Google's alerts: we implemented their MCS push protocol (it connects fine), then found that Google only alerts devices that report a location. We then tried an Android container that watches for alerts; it never caught a real one, and its Play services couldn't be updated, so it's paused. A real phone over USB is the next experiment.

If you live in a seismic zone and run Home Assistant, I'd love you to try it and tell me what you see, especially if the Android sentinel ever catches a real alert.

👉 Web console: https://juanipis.github.io/quake-alert-listener/
👉 Code & Home Assistant configs: https://github.com/Juanipis/quake-alert-listener
```

---

## 🎨 Design notes for `docs/index.html`

- **Look:** dark slate/navy (`#030712`, `#0a0f1d`), subtle cyan borders, bento grid. Accents: cyan `#38bdf8`, green `#22c55e` (live), amber `#eab308` (trial / research), crimson `#ef4444` (alerts).
- **Status labels:** `live` only for sources that deliver today; `ready` for tested-but-optional hardware; `trial` / `research` (amber) for the Google paths.
- **Simulation:** without a bridge, the page plays clearly-labelled simulated traffic; once a bridge answers on `127.0.0.1:8990` it switches to live data.
- **Assets:** `docs/logo.svg` (brand and favicon), `docs/favicon-32.png`, `docs/icon-180.png`, `docs/og.png`.
