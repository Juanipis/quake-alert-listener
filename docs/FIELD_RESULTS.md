# Field results: real earthquake alerts on the wire (October–November 2026)

From 9 October to 3 November 2026, `tools/radar_sismos_global.py` ran on a Raspberry Pi 4 with no phone attached. It received **10 earthquake alerts across 9 earthquakes from Google's Android Earthquake Alerts System (AEAS)**. They came over the same MCS socket (`mtalk.google.com:5228`) and in the same `gmta` format that a real Android device uses. Official catalogues confirm **7 of the 9 earthquakes (8 of the 10 alerts)**: five earthquakes (six alerts) in the USGS catalogue and two in national agency catalogues via EMSC (El Salvador's MARN and Panama's IGC).

These are the first earthquake alerts this project has seen on the wire. Until October 2026 the decoders had only been tested with synthetic payloads and the settings demo.

The data behind this page is in [`field-results/2026-10-aeas-earthquakes.json`](field-results/2026-10-aeas-earthquakes.json).

> [!IMPORTANT]
> This is an unofficial channel, not a certified warning system. Google can change or close it at any time. Use it as **one more input** next to official alerts and catalogues (EEW where it exists, USGS, EMSC, your national agency, your own sensor), never as the only one. Keep the official emergency alerts on your phone switched on.

---

## Summary

| | |
| :--- | :--- |
| Earthquake alerts received | **10 alerts across 9 earthquakes** (all real, `is_test` false) |
| Confirmed by official catalogues | **7 of 9 earthquakes (8 of 10 alerts)**: 5 earthquakes (6 alerts) in USGS + 2 in EMSC (MARN, IGC) |
| Our delay after Google issued the alert | **0.13 to 0.30 s** |
| Google's issue time, counted from Google's own origin estimate | 4.8 to 36.6 s |
| Receipt time, counted from the reference origin | **9.9 to 40.3 s** (14.0 s first packet / 16.2 s M5.5 update in the 3 Nov Colombia M5.4/M5.6 quake) |
| Warning before the strong shaking (S-wave) | **none near the epicentre**; **~10–42 s** across central Colombian cities (3 Nov), **~20 s** in Panama (31 Oct) and **~70 s** in the Philippines (27 Oct) at the outer alert areas |

In the same period the radar also logged public-alert and SOS/crisis-alert records (floods, tornado watches, tropical cyclones; new alerts and real changes). They share the channel and are kept out of the earthquake path. See the README for details.

---

## The ten alerts (nine earthquakes)

All times are UTC. When Google revised a magnitude in flight, the progression is shown.

| Event | Google's alert | Reference catalogue | Our delay | Received after reference origin |
| :--- | :--- | :--- | :--- | :--- |
| **Santander, Colombia**, 2026-10-13 | M4.8, depth 60 km, issued 21:25:01.1 (`8e683`) | [USGS us6000u25i](https://earthquake.usgs.gov/earthquakes/eventpage/us6000u25i): M4.8 mb, **depth 162 km**, origin 21:24:21.0 | 0.21 s | 40.3 s |
| **Mindanao, Philippines**, 2026-10-27 | M5.8, depth 55 km, issued 23:44:13.1 (`32f7b`) | [USGS us7000ttkk](https://earthquake.usgs.gov/earthquakes/eventpage/us7000ttkk): M5.1 mww, depth 58 km, origin 23:43:51.5 | 0.19 s | 21.8 s |
| **Southern Panama**, 2026-10-29 | M4.5, depth 10 km, issued 13:43:15.5 (`8fadf`) | [USGS us7000ttyw](https://earthquake.usgs.gov/earthquakes/eventpage/us7000ttyw): M4.7 mb, depth 10 km, origin 13:42:58.0 | 0.16 s | 17.7 s |
| **South of Panama**, 2026-10-29 | M4.5, depth 8 km, issued 23:57:21.0 (`8fae1`) | No USGS event. EMSC (UNA) lists an M4.0 ~150 km away and 22 s later, probably not the same event. | 0.16 s | 16.3 s *(from Google origin)* |
| **Southern Panama**, 2026-10-31 | M5.0 (later M5.6 → M5.4), depth 10 km, issued 15:50:28.5 (`8fad9`) | [USGS us7000tujf](https://earthquake.usgs.gov/earthquakes/eventpage/us7000tujf): M5.2 mww, depth 10 km, origin 15:50:18.9 | 0.30 s | **9.9 s** |
| **Western El Salvador**, 2026-11-02 | M4.5, depth 10 km, issued 11:57:22.1 (`8f62b`) | EMSC `20261102_0000144` (MARN): M3.9, depth 4.7 km, 3.6 km from Google's epicentre, origin 11:57:00.0 | 0.16 s | 22.3 s *(4.9 s from Google origin)* |
| **Southern Panama**, 2026-11-02 | M4.5, depth 10 km, issued 14:22:41.4 (`8fadf`) | EMSC `20261102_0000183` (IGC): M4.2, depth 9.0 km, 7.8 km from Google's epicentre, origin 14:22:25.0 | 0.15 s | 16.6 s |
| **Tolima, Colombia**, 2026-11-03 *(alert 1)* | **M5.3 → M5.5 → M5.8**, depth 21 km, first packet 09:13:29, M5.5 issued 09:13:31.0 (`8e397`) | [USGS us7000tv4i](https://earthquake.usgs.gov/earthquakes/eventpage/us7000tv4i): **M5.4 mww** (SGC **M5.6**), depth 10 km, origin 09:13:15.0 | **0.13 s** | **14.0 s** *(first packet)* / **16.2 s** *(M5.5)* |
| **Tolima, Colombia**, 2026-11-03 *(alert 2)* | **M5.6**, depth 4 km, issued 09:13:45.0 (`8e491`, same quake) | Same event: [USGS us7000tv4i](https://earthquake.usgs.gov/earthquakes/eventpage/us7000tv4i) (13 km from Google's epicentre) | **0.14 s** | 30.1 s |
| **Southern Panama**, 2026-11-03 | M4.5 → M4.7, depth 9 km, first packet 10:35:38, M4.7 issued 10:35:50.2 (`8fb1d`) | No USGS event. EMSC (UNA) lists an M4.2 ~101 km away and 20 s later, probably not the same event. | 0.15 s | 24.4 s *(first)* / 36.7 s *(M4.7, from Google origin)* |

For the seven confirmed earthquakes, Google's epicentres were **3.6 to 13.1 km** from the catalogue ones, and its magnitudes within ±0.7.

### Shallow vs. deep earthquakes in Colombia (3 Nov vs. 13 Oct)

The two Colombian events illustrate why focal depth matters for phone-based early warning:

1. **Santander, 13 October (intermediate depth, 162 km).** It belongs to the Bucaramanga seismic nest, one of the densest clusters of intermediate-depth quakes in the world. USGS places it at 162 km depth, while Google's first estimate said 60 km. A depth that is too shallow pushes the estimated origin later (Google said 21:24:49.5, USGS 21:24:21.0), so the alert arrived 40.3 s after the real origin—about 2 s after the S-wave.
2. **Tolima, 3 November (shallow crustal, 10 km).** Reported as M5.4 mww by USGS (`us7000tv4i`, 24 km NNW of Rioblanco) and M5.6 by Colombia's SGC (near Chaparral), felt across central and western Colombia. Because the rupture was shallow, Google's origin estimate (`09:13:16.0Z`) was within **1.1 s** of the USGS origin (`09:13:15.0Z`). The radar received the first alert (`8e397`, M5.3) **14.0 s after rupture**, the M5.5 update (`MMI5+`, `MMI4`, `MMI3` contours) at **+16.2 s** (**0.13 s** after Google issued it), the M5.8 revision at **+18.0 s**, and a second alert ID (`8e491`, M5.6) at **+30.1 s**.

---

## Did anyone get a warning before the shaking?

Each alert carries up to three nested polygons for predicted intensities **MMI 5+**, **MMI 4** and **MMI 3**. Comparing the time the alert arrived with the S-wave arrival ($V_s \approx 3.5\text{ km/s}$ for crustal events, $4.3\text{ km/s}$ for the 162 km deep event, counted from the USGS origin):

| Event | Where / Contour | Distance from epicentre | Warning before the S-wave |
| :--- | :--- | :--- | :--- |
| Santander, 13 Oct (162 km deep) | MMI 3 contour | 16–34 km | **none** (about 2 s late) |
| Philippines, 27 Oct (M5.1) | MMI 4 contour | 136–161 km | **+20 to +27 s** |
| | MMI 3 contour | 294–319 km | **+64 to +71 s** |
| Panama, 29 Oct | MMI 3 contour | 23–39 km | none |
| Panama, 29 Oct (unconfirmed) | MMI 5+ / MMI 3 | 2 / 31 km | none |
| Panama, 31 Oct (M5.2) | MMI 5+ contour | 1–21 km | none |
| | MMI 4 contour | 37–57 km | **+1 to +7 s** |
| | MMI 3 contour | 84–103 km | **+14 to +20 s** |
| **Tolima, Colombia, 3 Nov (M5.4 / M5.6)** | Near epicentre (Chaparral / Rioblanco) | < 50 km | **none** (blind zone) |
| *(first alert at +14.0 s, M5.5 at +16.2 s)* | **Ibagué** | ~90 km (S-wave ~26 s) | **+10 to +12 s** |
| | **Armenia / Pereira / Cali** | ~120–145 km (S-wave ~35–42 s) | **+19 to +28 s** |
| | **Bogotá** | ~195 km (S-wave ~56 s) | **+40 to +42 s** |

The pattern is the same for every earthquake early-warning system. Near the epicentre the S-wave arrives before any alert can be issued; this is the *blind zone*. Farther out, the margin grows by roughly 1 second for every 3.5 km. In practice, people tens to hundreds of kilometres away get a useful warning, and people right above the quake do not. Deep events such as the Bucaramanga nest are harder, because Google's first estimate can be late.

**Where the time goes.** Almost all of the delay is Google's detection time, which is the same for every Android phone in the area. Our own delay, from Google issuing the alert to the radar having it decoded, was **0.13 to 0.30 s**.

---

## How it was measured

- **Receiver.** `tools/radar_sismos_global.py` with a fleet of synthetic Android identities subscribed to S2 Level 8 cells in several seismic regions (see [HOW_IT_WORKS.md](HOW_IT_WORKS.md)). Its clock is synchronised by NTP.
- **Times.** *Issued* is the batch timestamp in Google's payload. *Received* is when the radar finished decoding the stanza.
- **Reference catalogues.** USGS FDSN event service and EMSC FDSN event service (which relays national networks such as MARN in El Salvador and IGC in Panama), queried through 3 November 2026.
- **Wave arrivals.** Straight-line distance from the hypocentre at a constant speed: Vs 3.5 km/s for crustal events and 4.3 km/s on average for the 162 km deep event. This is an approximation; treat the lead times as accurate to a few seconds.
- **The listener too.** Raw payloads were also fed through `decode_earthquake_payload()` in `quake_listener.py`, the bridge that users run. It recovered every alert ID, magnitude, epicentre and depth correctly. It also recovers the origin time to the millisecond, which drives the S-wave countdown, and flags Google test alerts so they are never dispatched. The 31 October Panama alert is kept as a test fixture.

## What is deliberately not published

- The fleet's identities, tokens and credentials, and any raw stanza that could contain them.
- Which node received which alert, and the list of extra (decoy) cells each node subscribes to.
- The location of the operator's home.

The published JSON keeps only what anyone can check against public catalogues: Google's event ID, its parameters and alert contours, the receipt time and the derived timings.

## Limitations

- **Sample size.** Nine earthquakes (ten alerts) in three and a half weeks, between M3.9 and M5.6 (Google estimates M4.5 to M5.8). No M6+ earthquake was captured yet.
- **Approximate wave model.** No velocity model and no site effects.
- **Two unconfirmed alerts off Panama.** Google's alerts for 2026-10-29 23:57 UTC and 2026-11-03 10:35 UTC have no USGS match and only distant (~100–150 km, +20–22 s) UNA candidates in EMSC. They may be offshore events that only coastal phones saw, or mislocated alerts.
- **Not every alert is seen.** The radar only hears alerts for the cells it subscribes to, and it hears nothing while its host is down. On 31 October the Raspberry Pi hung for 20 hours.
- **Google may change the channel**, the format or the subscription mechanism without notice.
