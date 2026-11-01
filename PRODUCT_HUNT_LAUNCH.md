# 🚀 Product Hunt Launch Kit • Quake MCS Listener

Everything needed to launch **Quake MCS Listener** on [Product Hunt](https://www.producthunt.com/posts/new). Every claim below matches the README, [docs/FIELD_RESULTS.md](docs/FIELD_RESULTS.md) and the code; keep it that way when editing.

**Positioning in one sentence:** an extra layer for your smart home, anywhere in the world. Add it if you have nothing, or use it to complement what you already have. It is never a replacement for official alerts.

---

## 📌 Core metadata

| Field | Value |
| :--- | :--- |
| **Name** | `Quake MCS Listener` |
| **Tagline** (max 60 chars) | `Earthquake alerts with a countdown, for your smart home` |
| Tagline alternatives | `Open-source earthquake early warning for Home Assistant` · `Turn earthquake alerts into smart-home automations` |
| **Links** | **Website:** `https://juanipis.github.io/quake-alert-listener/`<br>**GitHub:** `https://github.com/Juanipis/quake-alert-listener` |
| **Thumbnail** | `docs/logo.svg` (export a 240×240 PNG) |
| **Gallery** | See [Gallery](#%EF%B8%8F-gallery-1270760) below |
| **Topics** | `Home Automation`, `Open Source`, `Internet of Things`, `Developer Tools`, `Safety` (pick what Product Hunt offers) |
| **Pricing** | `Free / Open Source (MIT)` |

---

## 📝 Description (max 260 chars)

> Open-source bridge that turns earthquake alerts into smart-home actions: how hard it will shake at your place and how many seconds you have. Official EEW, USGS, EMSC, your own sensor, and Google's Android alerts, now confirmed with real quakes.

### The problem
Most smart-home quake integrations show a magnitude and a distance, minutes after the fact. To automate something useful you need to know *whether it will shake here, how hard, and how long you have*. Early warnings exist in only a few countries. Google's Android Earthquake Alerts cover far more of the world, but only reach Android phones and have no public API.

### The solution
Quake MCS Listener collects every source you can get and turns each report into local impact: an estimated intensity at your base station (the equation USGS ShakeMap uses) and an S-wave countdown. One policy decides what deserves a notification, merges duplicates across sources, escalates only if things get worse, and sends one signed webhook to Home Assistant.

### What's new: proof with real earthquakes (October 2026)
With no phone involved, the Google channel received **5 real earthquake alerts** from Google's Android Earthquake Alerts System: Colombia, the Philippines and three off Panama. **USGS confirms 4 of them.** Our delay after Google issued each alert was **0.16–0.30 s**. As with every early-warning system there was no warning right at the epicentre, but the outer alert areas had **~20 s (Panama) to ~70 s (Philippines)** before the strong shaking. Full data, method and limits: [docs/FIELD_RESULTS.md](docs/FIELD_RESULTS.md).

### Key highlights
- **🌍 Works anywhere, as a complement:** official early warnings where they exist (Japan, mainland China), EMSC and USGS worldwide, Colombia's SGC, your own Raspberry Shake, and Google's Android alerts over its push channel.
- **📍 Local impact, not just magnitude:** estimated intensity (MMI) at your place and a countdown to the S-wave.
- **🧪 Verified on real data:** 5 real Google alerts decoded, 4 matched to USGS, and one kept as a test fixture. 93 tests, CI on Python 3.8–3.13.
- **🛡️ Safe defaults:** Google test alerts and crisis alerts (hurricanes, floods) are logged but never trigger your earthquake automations. A missing origin time never stretches the countdown.
- **⚡ Zero dependencies:** one Python file on the standard library, about 30 MB of RAM.
- **🏠 Home Assistant native:** signed webhooks, REST sensors, a countdown automation ("🚨 Shaking in 18 s") and a dashboard card.
- **🚀 One-line trial:** `run.sh` / `run.ps1`; Docker Compose or a hardened systemd unit for 24/7.

### Honest limits (say them before anyone asks)
- **Not a certified warning system.** Keep the official alerts on your phone switched on.
- **The Google channel is unofficial.** It was reverse-engineered, and Google can change or close it at any time. That is exactly why it is one source among several.
- **There is a blind zone.** Near the epicentre the strong shaking arrives before any alert can, for every system, including Google's. Deep quakes are harder (see the Santander case in the field results).
- **Five events is a small sample.** It is enough to prove the pipeline works end to end, not to promise performance in a large quake.

---

## 💬 Maker's first comment

```markdown
Hey Product Hunt! 👋

I live in Colombia, where earthquake alerts reach some phones and no smart homes at all. Quake MCS Listener is my attempt to fix that: one small bridge that gathers every earthquake source it can and tells your home what a quake means *for your house*: how hard it will shake, and how many seconds you have.

What it does:
- Follows official early warnings (Japan's JMA and China's CENC via Wolfx), the EMSC, USGS and Colombian SGC feeds, your own Raspberry Shake, and Google's Android Earthquake Alerts over Google's push channel.
- Estimates the intensity at your place and counts down to the S-wave, then sends one webhook per quake to Home Assistant (lights, sirens, announcements, whatever you automate).
- One Python file, zero dependencies, about 30 MB of RAM.

The part I'm proudest of: we reverse-engineered how Android subscribes to Google's alerts (S2 map cells over the MCS push socket). In October 2026 a Raspberry Pi with no phone attached received 5 real earthquake alerts from Google. USGS confirms 4 of them, and they arrived 0.2 s after Google issued them. Some places in the alert area got ~20 to ~70 s before the strong shaking. Everything is published, including where it did NOT give a warning: https://github.com/Juanipis/quake-alert-listener/blob/main/docs/FIELD_RESULTS.md

Please treat it as an extra layer, not your only one: it's unofficial, Google can change the channel, and no system can warn you at the epicentre. Keep your phone's official alerts on.

If you live in a seismic zone and run Home Assistant, I'd love you to try it and tell me what you see. 🙏

👉 Web console: https://juanipis.github.io/quake-alert-listener/
👉 Code & Home Assistant configs: https://github.com/Juanipis/quake-alert-listener
```

---

## 🖼️ Gallery (1270×760)

| # | Image | Status |
| :--- | :--- | :--- |
| 1 | Hero: logo, tagline and the countdown notification ("🚨 Shaking in 18 s") | Adapt `docs/og.png` (currently 1200×630) |
| 2 | **Proof:** map of the 5 alerts with their USGS IDs, and the "0.2 s after Google" figure | To make, from `docs/field-results/2026-10-aeas-earthquakes.json` |
| 3 | **Lead time:** alert polygons for Panama on 31 Oct with the blind zone and the +14/+20 s edge | To make, from the same JSON |
| 4 | Web console (`docs/index.html`) receiving events | Screenshot |
| 5 | Home Assistant: dashboard card + iPhone notification | Screenshot from the HA config in `homeassistant/` |
| 6 | Architecture: sources → DetectionDesk → webhook → Home Assistant | To make (simple diagram) |

Optional: a 30–60 s video or GIF of a drill (`curl -X POST http://127.0.0.1:8990/drill`) reaching Home Assistant and turning the lights on.

---

## ❓ Prepared answers

**Is it legal / does Google allow it?** It is an independent research and interoperability project. It is not affiliated with Google and ships no Google code. It listens on the same push channel Android uses and receives only the alerts Google already broadcasts to an area. Google can change that at any time, which is why it is never the only source.

**Does it replace my phone's alerts?** No. Keep them on. This adds automation: lights, sirens, gas valves, announcements.

**How fast is it?** Our part takes about 0.2 s after Google issues an alert. Google issued its alerts 8 to 26 s after its own origin estimate; measured against the USGS origin, they arrived 10 to 40 s after the quake. How much warning you get depends on your distance to the epicentre: none right above it, tens of seconds farther away.

**What about false alarms?** Google test alerts and crisis alerts are filtered out, and the same quake from several sources produces one notification. One of the five alerts has no catalogue match yet; it is reported as unconfirmed.

**Which countries?** EMSC and USGS cover the whole world. Official early warnings are available for Japan and mainland China. Google's alerts reach the regions where Google runs them (a growing list, Colombia included).

**Do I need Home Assistant?** No. It sends a generic signed webhook and exposes a local REST API; Home Assistant configs are included.

---

## ✅ Launch checklist

- [ ] Gallery images 2, 3 and 6 made; image 1 resized to 1270×760; screenshots 4 and 5 taken.
- [ ] Clean-machine smoke test: `git clone` → `run.sh` → the web console finds the bridge → `python3 quake_listener.py --simulate` decodes → `curl -X POST http://127.0.0.1:8990/drill` reaches a test webhook.
- [ ] GitHub repo description updated to match the positioning (it still describes only the Android AEAS client).
- [ ] Repo topics: add `earthquake-early-warning`, `home-automation`, `open-source`.
- [ ] Launch on a Tuesday to Thursday at **12:01 AM Pacific** (2:01 AM in Colombia); have the first comment ready to post.
- [ ] Block out the launch day to answer every comment, honestly and with links to the field results.
- [ ] Share in r/homeassistant, the Home Assistant community forum and Colombian tech groups after the launch is live (not before).

---

## 🎨 Design notes for `docs/index.html`

- **Look:** dark slate/navy (`#030712`, `#0a0f1d`), subtle cyan borders, bento grid. Accents: cyan `#38bdf8`, green `#22c55e` (live), amber `#eab308` (trial / research), crimson `#ef4444` (alerts).
- **Status labels:** `live` only for sources that deliver today (the Google MCS socket now qualifies); `ready` for tested-but-optional hardware; `trial` / `research` (amber) for experiments such as the Android sentinel.
- **Simulation:** without a bridge, the page plays clearly labelled simulated traffic; once a bridge answers on `127.0.0.1:8990` it switches to live data.
- **Assets:** `docs/logo.svg` (brand and favicon), `docs/favicon-32.png`, `docs/icon-180.png`, `docs/og.png`.
