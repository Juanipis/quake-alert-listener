# 🚀 Product Hunt Launch Kit • Quake MCS Listener

This document contains everything needed to launch **Quake MCS Listener** on [Product Hunt](https://www.producthunt.com/posts/new) using **Claude Code + Claude in Chrome**, plus frontend design upgrades using **Stitch** and design skills.

---

## 📌 Core Metadata for Product Hunt

| Field | Value |
| :--- | :--- |
| **Name of Product** | `Quake MCS Listener` |
| **Tagline** (max 60 chars) | `Real-time earthquake alerts for Home Assistant (<15MB)` |
| **Links** | **Website:** `https://juanipis.github.io/quake-alert-listener/`<br>**GitHub:** `https://github.com/Juanipis/quake-alert-listener` |
| **Logo / Thumbnail** | `docs/logo.svg` (squircle with a two-channel seismogram: the P-wave arrives before the S-wave, which is the warning window). PNG exports: `docs/icon-180.png`, `docs/og.png` (1200×630) |
| **Topics / Tags** | `Home Automation`, `Developer Tools`, `Open Source`, `IoT`, `Smart Home` |
| **Pricing** | `Free / Open Source (MIT)` |

---

## 📝 Product Description & Pitch (v2)

### Short Description (414 / 500 characters)
> Earthquake warnings, straight into Home Assistant. One pure-Python file (0 deps, <15 MB RAM) follows official early warnings (JMA, CENC via Wolfx), EMSC's worldwide feed, your own Raspberry Shake and Google's push channel, then tells your home how hard it will shake there and how many seconds you have: "Shaking in 18 s". One notification per quake, cancellations included. Docker, HA YAML, live web console, MIT.

### The Problem
Smart-home earthquake integrations usually poll a public API every 30–60 seconds and react to magnitude. With seismic waves, that is often the whole warning window. And a magnitude alone doesn't tell you whether *your* house will shake.

### The Solution
Quake MCS Listener keeps sockets open to the networks that actually issue warnings. For every quake it asks one question: *what does this mean at my base station?* It answers with an estimated intensity (Allen, Wald & Worden 2012, the model behind USGS ShakeMap) and a countdown to the S-wave.

### What works today (honest)
| Source | Speed | Where |
| :--- | :--- | :--- |
| Official EEW via Wolfx (JMA, CENC, Sichuan, Fujian, Chongqing) | seconds | Japan, mainland China |
| Raspberry Shake on-site P-wave trigger (STA/LTA) | seconds | anywhere you install one |
| EMSC SeismicPortal | ~6–8 min | worldwide |
| Google MCS / Android Earthquake Alerts decoder | — | experimental; never observed delivering |

### Key Highlights
- **⚡ Zero dependencies:** Python standard library only, Python 3.8+, under 15 MB of RAM.
- **📍 Local impact:** `estimated_mmi`, `distance_km` and `s_wave_eta_s` in every webhook.
- **🔁 One story per quake:**
  - duplicates across feeds are merged;
  - you are only notified again if the quake gets worse;
  - cancelled warnings are announced as cancelled.
- **🏠 Home Assistant ready:** an automation that pushes "🚨 Shaking in 18 s", REST sensors and a dashboard card.
- **🐳 Runs 24/7:**
  - a ~55 MB Docker image with a healthcheck;
  - a hardened systemd unit for a Raspberry Pi.
- **🧪 Tested:** 26 offline tests (including hostile WebSocket peers), with CI on Python 3.8–3.13.
- **🌐 Web console:**
  - a live seismograph;
  - a packet console;
  - a "What would you feel?" calculator;
  - S-wave countdown drills.

---

## 💬 Maker's First Comment (v2)

```markdown
Hey Product Hunt! 👋

Quake MCS Listener started as a question: can a tiny Python script catch Android's earthquake alerts and wire them into a smart home?

It speaks Google's push protocol fine, but we found that Google sends those alerts only to Play-services phones, chosen by their location. So rather than promise something it can't do, v2 follows the sources that do deliver:

- Official early warnings from Japan's JMA and China's CENC (relayed by Wolfx): seconds after the quake starts.
- Your own Raspberry Shake: an on-site P-wave trigger, anywhere in the world.
- EMSC's worldwide feed: a few minutes later, everywhere.

For every quake the bridge estimates how hard it will shake at *your* house (Allen et al. 2012, the model behind USGS ShakeMap) and counts down to the S-wave. Home Assistant gets "🚨 Shaking in 18 s", not just "M6.1 somewhere".

Zero dependencies, under 15 MB of RAM, Docker or systemd, 26 tests, MIT.

Built with Google Antigravity, Gemini 3.8 Flash and Claude Opus 5.5 (Claude Code).

🌐 Live demo + "What would you feel?" calculator: https://juanipis.github.io/quake-alert-listener/
💻 Code, docs and HA configs: https://github.com/Juanipis/quake-alert-listener

It's a hobby project, not a certified warning system, so keep official alerts on your phone. Feedback, especially from places that shake, is very welcome! 🌍
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
