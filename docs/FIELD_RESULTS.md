# Field results: first real earthquake alerts (October 2026)

From 9 October to 1 November 2026, `tools/radar_sismos_global.py` ran on a Raspberry Pi 4 with no phone attached. It received **five earthquake alerts from Google's Android Earthquake Alerts System (AEAS)**. They came over the same MCS socket (`mtalk.google.com:5228`) and in the same `gmta` format that a real Android device uses. The USGS catalogue confirms four of the five events.

These are the first earthquake alerts this project has seen on the wire. Until now the decoders had only been tested with synthetic payloads and the settings demo.

The data behind this page is in [`field-results/2026-10-aeas-earthquakes.json`](field-results/2026-10-aeas-earthquakes.json).

> [!IMPORTANT]
> This is an unofficial channel, not a certified warning system. Google can change or close it at any time. Use it as **one more input** next to official alerts and catalogues (EEW where it exists, USGS, EMSC, your national agency, your own sensor), never as the only one. Keep the official emergency alerts on your phone switched on.

---

## Summary

| | |
| :--- | :--- |
| Earthquake alerts received | **5** (all real, `is_test` false) |
| Confirmed by USGS | **4** (the fifth has no clear USGS or EMSC match) |
| Our delay after Google issued the alert | **0.16 to 0.30 s** |
| Google's issue time, counted from Google's own origin estimate | 8 to 26 s |
| Receipt time, counted from the USGS origin | 10 to 40 s |
| Warning before the strong shaking (S-wave) | **none near the epicentre**; up to **~20 s** in Panama and **~70 s** in the Philippines at the outer edge of the alert area |

In the same period the radar also logged 123 public-alert records and 88 SOS or crisis-alert records (floods, hurricanes; new alerts and real changes). They share the channel and are kept out of the earthquake path. See the README for details.

---

## The five events

All times are UTC. Google's values are taken from the first alert issued for each event. Google later sent updates for some of them.

| Event | Google's alert | Reference catalogue | Received after the reference origin |
| :--- | :--- | :--- | :--- |
| **Santander, Colombia**, 2026-10-13 | M4.8, depth 60 km, issued 21:25:01.1 | [USGS us6000u25i](https://earthquake.usgs.gov/earthquakes/eventpage/us6000u25i): M4.8 mb, **depth 162 km**, origin 21:24:21 | 40.3 s |
| **Mindanao, Philippines**, 2026-10-27 | M5.8, depth 55 km, issued 23:44:13 | [USGS us7000ttkk](https://earthquake.usgs.gov/earthquakes/eventpage/us7000ttkk): M5.1 mww, depth 58 km, origin 23:43:51 | 21.8 s |
| **Southern Panama**, 2026-10-29 | M4.5, depth 10 km, issued 13:43:15 | [USGS us7000ttyw](https://earthquake.usgs.gov/earthquakes/eventpage/us7000ttyw): M4.7 mb, depth 10 km, origin 13:42:57 | 17.7 s |
| **South of Panama**, 2026-10-29 | M4.5, depth 8 km, issued 23:57:20 | No USGS event. EMSC lists an M4.0 about 150 km away and 22 s later, which is probably not the same event. | 16.3 s (counted from Google's origin) |
| **Southern Panama**, 2026-10-31 | M5.0, depth 10 km, issued 15:50:28 | [USGS us7000tujf](https://earthquake.usgs.gov/earthquakes/eventpage/us7000tujf): M5.2 mww, depth 10 km, origin 15:50:18 | 9.9 s |

Google's epicentres were 8 to 13 km from the USGS ones, and its magnitudes within ±0.7.

**The Santander event is the odd one.** It belongs to the Bucaramanga seismic nest, one of the densest clusters of intermediate-depth quakes in the world. USGS places it at 162 km depth, while Google's first estimate said 60 km. A depth that is too shallow pushes the estimated origin later (Google said 21:24:49.5, USGS 21:24:21.0), so the alert looks fast against Google's own clock and slow against the real one.

---

## Did anyone get a warning before the shaking?

Each alert carries its alert area: up to three nested polygons for predicted intensities **MMI 5+**, **MMI 4** and **MMI 3**. The JSON file has every vertex. For the nearest and farthest vertex of each polygon we compared the time the alert arrived with the time the S-wave (the strong shaking) would arrive there:

| Event | Contour | Distance from epicentre | Warning before the S-wave |
| :--- | :--- | :--- | :--- |
| Santander | MMI 3 | 16–34 km | **none** (about 2 s late) |
| Philippines | MMI 4 | 136–161 km | **+20 to +27 s** |
| | MMI 3 | 294–319 km | **+64 to +71 s** |
| Panama, 29 Oct | MMI 3 | 23–39 km | none |
| Panama, 29 Oct (unconfirmed) | MMI 5+ / MMI 3 | 2 / 31 km | none |
| Panama, 31 Oct | MMI 5+ | 1–21 km | none |
| | MMI 4 | 37–57 km | **+1 to +7 s** |
| | MMI 3 | 84–103 km | **+14 to +20 s** |

The pattern is the same for every earthquake early-warning system. Near the epicentre the S-wave arrives before any alert can be issued; this is the *blind zone*. Farther out, the margin grows by roughly 1 second for every 3.5 km. In practice, people tens to hundreds of kilometres away get a useful warning, and people right above the quake do not. Deep events such as the Bucaramanga nest are harder, because Google's first estimate can be late.

**Where the time goes.** Almost all of the delay is Google's detection time, which is the same for every Android phone in the area. Our own delay, from Google issuing the alert to the radar having it decoded, was **0.16 to 0.30 s**.

---

## How it was measured

- **Receiver.** `tools/radar_sismos_global.py` with a fleet of synthetic Android identities subscribed to S2 Level 8 cells in several seismic regions (see [HOW_IT_WORKS.md](HOW_IT_WORKS.md)). Its clock is synchronised by NTP.
- **Times.** *Issued* is the batch timestamp in Google's payload. *Received* is when the radar finished decoding the stanza.
- **Reference catalogue.** USGS FDSN event service, queried on 1 November 2026. EMSC was used for the event USGS does not list.
- **Wave arrivals.** Straight-line distance from the hypocentre at a constant speed: Vs 3.5 km/s for crustal events and 4.3 km/s on average for the 162 km deep event. This is an approximation; treat the lead times as accurate to a few seconds.
- **The listener too.** The same five raw payloads were also fed through `decode_earthquake_payload()` in `quake_listener.py`, the bridge that users run. It recovered every alert ID, magnitude, epicentre and depth correctly. It also recovers the origin time to the millisecond, which drives the S-wave countdown, and flags Google test alerts so they are never dispatched. The Panama alert is kept as a test fixture.

## What is deliberately not published

- The fleet's identities, tokens and credentials, and any raw stanza that could contain them.
- Which node received which alert, and the list of extra (decoy) cells each node subscribes to.
- The location of the operator's home.

The published JSON keeps only what anyone can check against public catalogues: Google's event ID, its parameters and alert polygons, the receipt time and the derived timings.

## Limitations

- **Small sample.** Five events in three weeks, all between M4.5 and M5.8. No large earthquake was captured.
- **Approximate wave model.** No velocity model and no site effects.
- **One unconfirmed event.** Google's alert for 2026-10-29 23:57 UTC has no clear catalogue match. It may be a small real event that was mislocated, or a false alert.
- **Not every alert is seen.** The radar only hears alerts for the cells it subscribes to, and it hears nothing while its host is down. On 31 October the Raspberry Pi hung for 20 hours.
- **Google may change the channel**, the format or the subscription mechanism without notice.
