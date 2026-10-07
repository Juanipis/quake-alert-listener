# How Quake MCS Listener works

This is a walkthrough of what `quake_listener.py` actually does on the wire, what has been verified, and what has not. It is written for people who want to audit the code, extend it, or try to prove the Google path works.

**Short version.** The bridge follows up to four push sources, works out what each quake means *at your base station*, and tells Home Assistant over a webhook.

| Source | Kind | Typical speed | Status |
| :--- | :--- | :--- | :--- |
| Official EEW via Wolfx (JMA, CENC, Sichuan, Fujian, Chongqing) | `eew` | seconds after origin | live |
| Raspberry Shake UDP datacast + STA/LTA | `onsite` | seconds, on site | tested with synthetic signals |
| EMSC SeismicPortal | `report` | minutes (measured ~6–8 min) | live |
| Google MCS + AEAS decoder | `aeas` | would be seconds | experimental; never observed delivering ([why](#why-the-google-path-is-a-long-shot)) |

```mermaid
flowchart LR
    subgraph Push sources
      W[Wolfx all_eew<br/>JMA · CENC EEW]
      E[EMSC SeismicPortal]
      S[Raspberry Shake<br/>UDP datacast]
      G[Google MCS<br/>mtalk.google.com:5228]
    end
    D[DetectionDesk<br/>intensity · S-wave ETA<br/>levels · dedupe]
    HA[Home Assistant<br/>webhook]
    WEB[Web console<br/>GET /status]
    W --> D
    E --> D
    S -- STA/LTA trigger --> D
    G -- AEAS stanza --> D
    D -- JSON POST, optional HMAC --> HA
    D -- REST :8990 --> WEB
```

Sections 1–4 cover the Google path in depth (it is the most involved protocol). Sections 5–7 cover the other sources and the desk that ties everything together.

---

## 1. Getting an identity: checkin

Before anything can log in to MCS, it needs an `android_id` and a `security_token`. The bridge gets them the same way Chrome's built-in GCM client does.

- **Request:** a protobuf `POST` to `https://android.clients.google.com/checkin`.
- **What it claims to be:** a Chrome build (`chrome_build { platform: 2, version: "120.0.6099.144", channel: 1 }`), with checkin `type: 3`. It also sends the locale (default `en_US`) and timezone (default `UTC`).
- **Response fields used:** field `7` is `android_id` and field `8` is `security_token`, both 64-bit integers.
- **Storage:** they are saved to `~/.quake_device_credentials.json` (or `--credentials-file`) with `0600` permissions and reused on later starts.

> [!IMPORTANT]
> This identity is a **browser-type GCM client**. It is not an Android phone, it has no Google Play services, it reports no location, and it is not registered (`register3`) with any sender or app. Keep that in mind for section 4.

## 2. Logging in to MCS

MCS (Mobile Connection Server) is the long-lived binary protocol behind Android and Chrome push.

**Connection.**
- TLS to `mtalk.google.com:5228`, with certificate verification on.
- The client sends one version byte (`41`), then the `LoginRequest`.

**Framing.** After the version byte, every message is `tag (1 byte) + length (varint) + protobuf payload`.

| Tag | Message | What the bridge does |
|---:|---|---|
| 0 | `HeartbeatPing` | Replies with a `HeartbeatAck` |
| 1 | `HeartbeatAck` | Measures the round-trip of its own ping |
| 2 | `LoginRequest` | Sent once per connection |
| 3 | `LoginResponse` | Checked for an error field; a rejection is reported as `Login rejected by MCS` |
| 4 | `Close` | Reconnects |
| 7 | `IqStanza` | Counted (the server uses these for acks) |
| 8 | `DataMessageStanza` | The only frame that can carry an alert |

**LoginRequest fields.** The login sends:
- `id`: `chrome-120.0.6099.144`
- `domain`: `mcs.android.com`
- `user` and `resource`: the `android_id`
- `auth_token`: the `security_token`
- `device_id`: `android-<hex android_id>`
- the `new_vc` setting
- `received_persistent_id` (repeated) for messages already handled, so Google does not resend them
- `adaptive_heartbeat`, `use_rmq2` and `auth_service`

**Reads.** Reads go through a buffer with a 0.5 s timeout. A frame split across TLS records is reassembled, not treated as an error. Frames over 4 MB and varints over 10 bytes are rejected as corrupt.

## 3. Staying connected

- **Heartbeats.** Every `--ping-interval` seconds (clamped to 30–600, default 120) the client sends a `HeartbeatPing` from the listener thread. `latency_ms` in `/status` is the real ping → ack round-trip. The TLS connect time is reported separately as `handshake_latency_ms`.
- **Dead links.** If nothing arrives for `ping_interval + 60` s, the connection is considered dead and reopened. TCP keepalive is also on.
- **Reconnects.** Backoff runs from 3 s to 60 s with jitter, and resets after a session that lasted at least 60 s.
- **Manual pings.** `POST /ping` only *asks* the listener thread to ping, so the HTTP thread never writes to the TLS socket.

## 4. Data messages and the AEAS decoder

When a `DataMessageStanza` (tag 8) arrives:

1. **De-duplication.** Its `persistent_id` (field 9) is checked against recently seen IDs, so re-deliveries are dropped.
2. **Category check.** If `category` (field 5) is `com.google.android.gms` and `raw_data` (field 21) is present, `raw_data` goes to the earthquake decoder.

**Decoder.** It walks this protobuf tree:

```
payload
└─ 2 (repeated) event
   ├─ 6 geometry
   │  └─ 2 zones
   │     └─ 1 (repeated) zone
   │        └─ 3 circle
   │           ├─ 1 center  { 1 lat (double), 2 lon (double) }
   │           └─ 2 radius_m
   ├─ 7 magnitude  { 2 value | 1 value }   (number or numeric string)
   └─ 8 region name (string)
```

**Where the schema comes from.** The variable names in the code (`jeim`, `jeik`, `jeif`, `jeig`, `jhom`) are obfuscated class names. That suggests the schema was taken from a decompiled Google Play services build.

**What has been tested.** The decoder has only been exercised with synthetic payloads (`--simulate`) and 500 rounds of random input. It has never seen a real alert.

**Origin time and alert ID.** Field `1` of each event carries an alert ID (sub-field `3`) and an origin time in milliseconds (sub-field `4`). They are used for deduplication and for the S-wave countdown.

**Hand-off.** A decoded event goes to the [detection desk](#6-the-detection-desk), like every other source. If Google's own impact circle covers your base station and M ≥ 4.5, the event is at least a `notice`, even if the intensity estimate is low. If an alert ever arrives without coordinates, the desk falls back to magnitude alone: M ≥ 5 is an `alert`, M ≥ 4 a `notice`.

## Why the Google path is a long shot

Google's own descriptions say how AEAS alerts get to people:

- Alerts are delivered by **Google Play services**.
- Phones are chosen by their **coarse location**.
- The phone needs **Earthquake Alerts and location turned on**.

([Google Research blog](https://research.google/blog/android-earthquake-alerts-a-global-system-for-early-warning/); [Science, 2025](https://www.science.org/doi/10.1126/science.ads4779)).

In other words, Google decides on the server which enrolled phones are in the affected area, and pushes to those phones only. A browser-type identity that reports no location and has no app registrations is not in that set.

**What we measured.** The listener ran with `--debug-frames` for about four minutes on 2026-10-07:

```
Authenticated with mtalk.google.com:5228 (Handshake latency: 408.3 ms)
[frame] IqStanza 10B type=1 extension=12      ← a selective ack, nothing else
pings_sent 6 · pings_received 8 · messages_received 0 · latency_ms 91.2
```

The connection is healthy, but the only traffic is protocol housekeeping. Making Google send AEAS alerts would mean posing as a real, location-reporting Android device with Play services. We don't do that: it would mean misrepresenting the device to Google, and it would be fragile anyway.

If you have evidence that a client like this one receives AEAS stanzas, please [open an issue](https://github.com/Juanipis/quake-alert-listener/issues) with a `--debug-frames` log. That is exactly the kind of report this project needs.

## 5. The other sources

### 5a. EMSC (worldwide rapid reports)

[EMSC's SeismicPortal](https://www.seismicportal.eu/realtime.html) pushes every new or revised earthquake worldwide. The data is licensed CC BY 4.0.

**Connection.** The bridge opens `wss://www.seismicportal.eu/standing_order/websocket` with a small built-in WebSocket client (still no dependencies):
- TLS, with the RFC 6455 handshake checked via `Sec-WebSocket-Accept`;
- masked client frames, fragmentation, ping/pong and close all handled;
- reconnection with the same backoff as MCS.

**Messages.** Each message looks like this:

```json
{"action": "create", "data": {"properties": {
  "unid": "…", "time": "…Z", "lat": 0.5, "lon": 0.5, "depth": 10.0,
  "mag": 4.6, "magtype": "mb", "flynn_region": "…", "auth": "…"}}}
```

**What happens to each event.** It goes to the [detection desk](#6-the-detection-desk). The feed also re-sends revisions of quakes from weeks ago, which the desk's 15-minute age limit drops.

**Logging.** EMSC events are logged when their estimated intensity at your base station is at least MMI 2, or when they are M ≥ 5 anywhere. That lets you watch the feed in the web console without flooding it with global micro-quakes.

**What we measured.** In a 2.5-minute sample on 2026-10-07, six real events arrived, between about 6 and 8 minutes after their origin times. That is too late for early warning, but useful to log the event, notify your phone, or check on the house afterwards.

Disable it with `--no-emsc` or `--sources mcs,wolfx`.

### 5b. Official early warnings via Wolfx (Japan, mainland China)

[Wolfx](https://wolfx.jp) is an independent public-interest project. It relays official earthquake early warnings as JSON over WebSocket.

**Connection.** The bridge subscribes to `wss://ws-api.wolfx.jp/all_eew`. It gets a `{"type": "heartbeat"}` every 60 s, plus every EEW as it is issued:

| `type` | Agency | Time zone of `OriginTime` |
|---|---|---|
| `jma_eew` | Japan Meteorological Agency | UTC+9, `YYYY/MM/DD HH:MM:SS` |
| `cenc_eew` | China Earthquake Networks Center | UTC+8, `YYYY-MM-DD HH:MM:SS` |
| `sc_eew`, `fj_eew`, `cq_eew` | Sichuan, Fujian, Chongqing networks | UTC+8 |

**Fields used:**
- `EventID`, plus `Serial` or `ReportNum` (the revision number);
- `Latitude`, `Longitude`, `Depth`;
- `Magnitude` (the misspelled `Magunitude` is also accepted);
- `Hypocenter` or `HypoCenter` (region name);
- `MaxIntensity` (the agency's own scale, passed through as `agency_intensity`);
- `isFinal`, `isCancel`, `isTraining`.

**Behaviour.** An EEW is issued seconds after the quake starts and revised several times as more stations report. The desk notifies on the first revision that reaches your threshold, and again only if a later revision makes it worse. Training messages are ignored, and a cancellation of something you were told about is passed on as `level: "cancel"`.

### 5c. On-site detection with a Raspberry Shake

Where no agency publishes EEW, the only true early warning is detecting the P-wave yourself.

**Input.** A [Raspberry Shake](https://raspberryshake.org) can be configured to send its *UDP datacast* to the machine running the bridge. Each packet is plain text: `{'EHZ', <epoch seconds>, <count>, <count>, …}`. Start the bridge with `--shake-udp 8888`.

**Detection.**
- The bridge watches the first vertical channel (`EHZ`, `ENZ`, …) or the one set with `--shake-channel`.
- It runs a recursive STA/LTA ([Withers et al., 1998](https://doi.org/10.1785/BSSA0880010095)) over the squared, de-meaned signal: 1 s short window, 30 s long window, 30 s warm-up.
- A trigger starts when the ratio reaches `--shake-sta-lta-on` (default 4.0) and ends below `--shake-sta-lta-off` (1.5).
- After a trigger, the next one is suppressed for 30 s.
- The bridge waits up to 3 s to measure the peak amplitude, then reports it.

**Level.** A trigger is a `notice`. It becomes an `alert` only if its peak reaches `--shake-alert-counts`, which you should calibrate for your floor.

**Limits.** A trigger cannot tell a quake from a slammed door, and the bridge has no magnitude for it. What it gives you is "something is shaking the house *now*", a few seconds before the strong part. The trigger is unit-tested with synthetic signals; it has not yet been tuned on a real installation.

## 6. The detection desk

All four sources produce the same *event* shape:
- `source`, `kind`, `event_id`, `revision`;
- `origin_ts`, `lat`, `lon`, `depth_km`;
- `magnitude`, `region`;
- `final`, `cancelled`, `training`.

`DetectionDesk.submit()` applies one policy to all of them.

**1. Local impact.**
- **Distances.** It computes the epicentral distance with the haversine formula, and the hypocentral distance with depth (10 km if unknown).
- **Intensity.** It estimates the median intensity at your base station with **Allen, Wald & Worden (2012)**, *Intensity attenuation for active crustal regions*. This is the hypocentral form and the default intensity prediction equation in USGS ShakeMap:

  ```
  MMI = 2.085 + 1.428·M − 1.402·ln √(R² + Rm²)  [+ 0.078·ln(R/50) if R > 50 km]
  Rm  = −0.209 + 2.042·e^(M−5)
  ```

  Sanity checks: M6 at 14 km ≈ MMI 6.9, M5 at 50 km ≈ 3.7, M7 at 100 km ≈ 5.7. The estimate ignores local soil, so expect about ±1 MMI.
- **Wave arrivals.** P-wave arrival is `origin + R / 6.0 km/s` and S-wave arrival is `origin + R / 3.5 km/s`. `s_wave_eta_s` is the time left until the S-wave when the payload is built.

**2. Filters.** An event is dropped when:
- it is a training message;
- it is older than 15 minutes;
- it is below `--min-magnitude`;
- it is farther than `--max-distance-km`.

**3. Level.**
- `alert` if the estimated MMI is at least `--alert-mmi` (5.0, like Android's "Take Action");
- `notice` if it is at least `--notice-mmi` (3.0, like Android's "Be Aware");
- otherwise nothing is sent.

**4. One quake, one story.** An event matches an already-notified quake if it has the same source and ID, or if it comes from another source with an origin within 90 s and an epicenter within 150 km. A match is announced again only when:
- the level goes up, or
- the estimated MMI rises by at least 1.

So an EEW, its revisions and the EMSC report a few minutes later produce one notification, plus an escalation if the quake turns out bigger. A cancelled EEW that had been announced produces a `cancel` payload.



## 7. What reaches Home Assistant

Every source produces the same JSON. These are the fields (synthetic values):

| Field | Example | Notes |
|---|---|---|
| `level` / `nivel` | `alert` / `alerta` | `notice`·`aviso`, `alert`·`alerta`, `cancel`·`cancelado`, `drill`·`simulacro` |
| `status` | `early warning` | `early warning` (EEW), `rapid report` (EMSC), `on-site trigger`, `early alert` (AEAS), `cancelled` |
| `source`, `kind` | `JMA EEW`, `eew` | `kind` is `eew` · `report` · `onsite` · `aeas` |
| `id`, `event_id`, `revision`, `final` | `eew-jma_eew:…`, `…`, `3`, `false` | Revision / final flags as given by the agency |
| `magnitude` / `magnitud`, `magnitude_type` | `6.1` | |
| `lat`, `lon`, `depth_km`, `region` | `0.5`, `0.5`, `30`, `Test Region` | |
| `distance_km` / `distancia_km`, `hypocentral_km` | `42.3`, `51.9` | From your `--lat/--lon` |
| `estimated_mmi`, `mmi_label` | `5.3`, `V · moderate` | Allen et al. (2012) at your base station |
| `agency_intensity` | `5-` | The agency's own scale (JMA shindo, CENC intensity) |
| `origin_time`, `report_delay_s` | ISO UTC, `4.8` | How late the information reached you |
| `p_wave_arrival_ts`, `s_wave_arrival_ts`, `s_wave_eta_s` | epoch, epoch, `9.8` | Countdown to strong shaking; `null` when unknown |
| `radius_km` | `90` | AEAS impact circle only |
| `place` / `lugar` | `M6.1 · Test Region · 42.3 km from Base Station · est. MMI V · S-wave in 9 s` | Ready to show or speak |
| `url`, `timestamp` / `hora_local` | | Event page (EMSC); local time the payload was built |

**Signing.** With `--webhook-secret`, the body is signed with HMAC-SHA256. The signature is sent in both `X-Quake-Signature` and `X-Sismo-Firma`.

**Delivery.** Webhooks run in a background thread, with three attempts on network errors or 5xx responses. `POST /drill` sends a `level: "drill"` payload synchronously, so you can test automations end to end.

## 8. The local REST API and its security model

| Endpoint | Method | Purpose |
|---|---|---|
| `/status` (also `/`, `/api/status`) | GET | Telemetry for every source (`google_mcs`, `sources.emsc/wolfx/shake`), thresholds, last quake, recent logs |
| `/ping` | GET or POST | Ping MCS through the listener thread; returns `latency_ms` |
| `/drill` (also `/simulacro`) | POST | Send a drill payload to your webhook |

The API defends itself on three levels:

- **Binding.** The server listens on `127.0.0.1` by default, or on `0.0.0.0` automatically inside a container. Use `--http-host 0.0.0.0` to reach it from another machine.
- **Browser origins.** Only the official page and `localhost` pages get CORS headers. Other origins get `403` on `/ping` and `/drill` (`--allowed-origins`). Clients that send no `Origin` header, such as curl or Home Assistant, are not affected.
- **DNS rebinding.** Requests whose `Host` header is a foreign domain name are refused (`--allowed-hosts`). IPs, `localhost` and this machine's hostname always work.

## 9. Research it yourself

```bash
# Watch every non-heartbeat MCS frame (tag, size, category, sender, app_data keys)
python3 quake_listener.py --debug-frames --no-emsc

# Decode a synthetic AEAS payload and preview the webhook body (nothing is sent)
python3 quake_listener.py --simulate

# Follow only the official early warnings and EMSC, with stricter thresholds
python3 quake_listener.py --sources wolfx,emsc --notice-mmi 3.5 --alert-mmi 5.5

# Run the offline test suite
python3 -m unittest discover -s tests -v

# One-shot TLS + login + heartbeat round-trip
python3 quake_listener.py --test-ping
```
