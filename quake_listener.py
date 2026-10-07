#!/usr/bin/env python3
"""quake_listener.py
Lightweight, autonomous client (<15 MB RAM, 0 external dependencies) for receiving
real-time earthquake alerts from the Android Earthquake Alerts System (AEAS) via MCS (mtalk:5228).

Designed for seamless integration with Home Assistant and local home automation systems.
Built with Google Antigravity (AGY) & Gemini 3.8 Flash (Thinking High).

Usage:
  python3 quake_listener.py --lat 37.7749 --lon -122.4194 --name "San Francisco, CA"
  python3 quake_listener.py --ping-interval 120 --webhook-url http://127.0.0.1:8123/api/webhook/quake
  python3 quake_listener.py --test-ping
  python3 quake_listener.py --simulate
"""

import argparse
import datetime as dt
import hashlib
import hmac
import http.server
import json
import math
import os
import select
import socket
import ssl
import struct
import sys
import threading
import time
import urllib.request

HOST_MCS = "mtalk.google.com"
PORT_MCS = 5228
MCS_VERSION = 41

TAG_HEARTBEAT_PING = 0
TAG_HEARTBEAT_ACK = 1
TAG_LOGIN_REQUEST = 2
TAG_LOGIN_RESPONSE = 3
TAG_CLOSE = 4
TAG_IQ_STANZA = 7
TAG_DATA_MESSAGE_STANZA = 8

CREDENTIALS_FILE = os.path.join(os.path.expanduser("~"), ".quake_device_credentials.json")
RECENT_LOGS = []


def log(msg):
    t_full = time.strftime('%Y-%m-%d %H:%M:%S')
    sys.stdout.write(f"[{t_full}] {msg}\n")
    sys.stdout.flush()
    RECENT_LOGS.append({"time": time.strftime('%H:%M:%S'), "msg": msg})
    if len(RECENT_LOGS) > 30:
        RECENT_LOGS.pop(0)


# ==============================================================================
# Network & Protobuf Low-Level Helpers (Pure Python)
# ==============================================================================

def encode_varint(n):
    if n < 0:
        n &= (1 << 64) - 1
    res = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            res.append(b | 0x80)
        else:
            res.append(b)
            break
    return bytes(res)


def read_varint_from_stream(stream):
    val = 0
    shift = 0
    while True:
        chunk = stream.read(1) if hasattr(stream, "read") else stream.recv(1)
        if not chunk:
            return None
        b = chunk[0]
        val |= (b & 0x7F) << shift
        if not (b & 0x80):
            break
        shift += 7
    return val


def field_varint(tag, val):
    return encode_varint((tag << 3) | 0) + encode_varint(val)


def field_bytes(tag, val):
    return encode_varint((tag << 3) | 2) + encode_varint(len(val)) + val


def field_str(tag, s):
    return field_bytes(tag, s.encode("utf-8"))


def parse_protobuf(data):
    fields = {}
    p = 0
    length_data = len(data)
    while p < length_data:
        key = 0
        shift = 0
        while True:
            if p >= length_data:
                return fields
            b = data[p]
            p += 1
            key |= (b & 0x7F) << shift
            if not (b & 0x80):
                break
            shift += 7
        tag = key >> 3
        wire = key & 0x07

        if wire == 0:
            val = 0
            shift = 0
            while True:
                if p >= length_data:
                    return fields
                b = data[p]
                p += 1
                val |= (b & 0x7F) << shift
                if not (b & 0x80):
                    break
                shift += 7
        elif wire == 1:
            if p + 8 > length_data:
                break
            val = struct.unpack("<Q", data[p:p + 8])[0]
            p += 8
        elif wire == 2:
            l = 0
            shift = 0
            while True:
                if p >= length_data:
                    return fields
                b = data[p]
                p += 1
                l |= (b & 0x7F) << shift
                if not (b & 0x80):
                    break
                shift += 7
            if p + l > length_data:
                break
            val = data[p:p + l]
            p += l
        elif wire == 5:
            if p + 4 > length_data:
                break
            val = struct.unpack("<I", data[p:p + 4])[0]
            p += 4
        else:
            break
        fields.setdefault(tag, []).append((wire, val))
    return fields


def haversine_distance(lat1, lon1, lat2, lon2):
    R = 6371.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2.0)**2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2.0)**2
    return round(2 * R * math.atan2(math.sqrt(a), math.sqrt(1 - a)), 1)


# ==============================================================================
# Anonymous Device Hardware Registration
# ==============================================================================

def register_device(locale="es_CO", tz="America/Bogota"):
    log(f"Registering anonymous hardware identity (locale={locale}, tz={tz})...")
    chrome_build = field_varint(1, 2) + field_str(2, "120.0.6099.144") + field_varint(3, 1)
    checkin_proto = field_varint(1, 3) + field_bytes(2, chrome_build)
    body = (
        field_bytes(4, checkin_proto) +
        field_str(6, locale) +
        field_str(12, tz) +
        field_varint(14, 3)
    )

    req = urllib.request.Request(
        "https://android.clients.google.com/checkin",
        data=body,
        headers={"Content-Type": "application/x-protobuf"},
        method="POST"
    )

    with urllib.request.urlopen(req, timeout=12) as resp:
        if resp.status != 200:
            raise RuntimeError(f"Checkin registration failed with HTTP code {resp.status}")
        data = resp.read()

    parsed = parse_protobuf(data)
    android_id = parsed.get(7, [(1, None)])[0][1]
    security_token = parsed.get(8, [(1, None)])[0][1]

    if not (android_id and security_token):
        raise RuntimeError("Registration response did not return valid hardware credentials.")

    creds = {
        "android_id": android_id,
        "security_token": security_token,
        "created_at": time.time(),
        "locale": locale,
        "time_zone": tz
    }
    with open(CREDENTIALS_FILE, "w") as f:
        json.dump(creds, f, indent=2)
    log(f"Device registered successfully (id: {android_id})")
    return creds


def get_credentials(locale="es_CO", tz="America/Bogota"):
    if os.path.isfile(CREDENTIALS_FILE):
        try:
            with open(CREDENTIALS_FILE, "r") as f:
                creds = json.load(f)
                if "android_id" in creds and "security_token" in creds:
                    return creds
        except Exception:
            pass
    return register_device(locale, tz)


# ==============================================================================
# Earthquake Alert Protobuf Decoder (AEAS)
# ==============================================================================

def decode_earthquake_payload(raw_bytes):
    jeim = parse_protobuf(raw_bytes)
    events = []
    for _, ev_bytes in jeim.get(2, []):
        ev = parse_protobuf(ev_bytes)
        mag = None
        if 7 in ev:
            mag_p = parse_protobuf(ev[7][0][1])
            if 2 in mag_p:
                val = mag_p[2][0][1]
                mag = round(struct.unpack("<f", struct.pack("<I", val))[0] if isinstance(val, int) else val, 1)
            elif 1 in mag_p:
                try:
                    mag = float(mag_p[1][0][1].decode("latin1", errors="ignore"))
                except Exception:
                    pass

        region = ev[8][0][1].decode("utf-8", errors="ignore") if 8 in ev else "Region"
        epicenter_lat, epicenter_lon, radius_km = None, None, None

        if 6 in ev:
            jeif = parse_protobuf(ev[6][0][1])
            if 2 in jeif:
                jeig = parse_protobuf(jeif[2][0][1])
                for _, zone_b in jeig.get(1, []):
                    zone = parse_protobuf(zone_b)
                    for _, circ_b in zone.get(3, []):
                        circ = parse_protobuf(circ_b)
                        if 2 in circ:
                            radius_km = round(circ[2][0][1] / 1000.0, 1)
                        if 1 in circ:
                            jhom = parse_protobuf(circ[1][0][1])
                            if 1 in jhom:
                                raw_lat = jhom[1][0][1]
                                epicenter_lat = struct.unpack("<d", struct.pack("<Q", raw_lat))[0] if isinstance(raw_lat, int) else raw_lat
                            if 2 in jhom:
                                raw_lon = jhom[2][0][1]
                                epicenter_lon = struct.unpack("<d", struct.pack("<Q", raw_lon))[0] if isinstance(raw_lon, int) else raw_lon

        events.append({
            "magnitude": mag,
            "region": region,
            "lat": epicenter_lat,
            "lon": epicenter_lon,
            "radius_km": radius_km,
        })
    return events


# ==============================================================================
# TLS Socket Client for Google MCS (mtalk:5228)
# ==============================================================================

class QuakeMCSClient:
    def __init__(self, creds, ping_interval=120):
        self.android_id = creds["android_id"]
        self.security_token = creds["security_token"]
        self.ping_interval = max(30, min(ping_interval, 600))
        self.sock = None
        self.last_ping = time.time()
        self.pings_sent = 0
        self.pings_received = 0
        self.messages_received = 0
        self.last_packet_ts = None
        self.latency_ms = None

    def _login_packet(self):
        setting = field_str(1, "new_vc") + field_str(2, "1")
        login_req = (
            field_str(1, "chrome-120.0.6099.144") +
            field_str(2, "mcs.android.com") +
            field_str(3, str(self.android_id)) +
            field_str(4, str(self.android_id)) +
            field_str(5, str(self.security_token)) +
            field_str(6, f"android-{hex(self.android_id)[2:]}") +
            field_bytes(8, setting) +
            field_varint(14, 1) +
            field_varint(16, 2) +
            field_varint(17, 1)
        )
        return bytes([MCS_VERSION, TAG_LOGIN_REQUEST]) + encode_varint(len(login_req)) + login_req

    def connect(self):
        t0 = time.time()
        raw_sock = socket.create_connection((HOST_MCS, PORT_MCS), timeout=15)
        ctx = ssl.create_default_context()
        self.sock = ctx.wrap_socket(raw_sock, server_hostname=HOST_MCS)
        self.sock.settimeout(15)

        self.sock.sendall(self._login_packet())

        v_byte = self.sock.recv(1)[0]
        t_byte = self.sock.recv(1)[0]
        if v_byte != MCS_VERSION or t_byte != TAG_LOGIN_RESPONSE:
            raise ConnectionError(f"Handshake failed: version={v_byte}, tag={t_byte}")

        length = read_varint_from_stream(self.sock)
        _ = self._recv_exact(length) if length > 0 else b""
        self.latency_ms = round((time.time() - t0) * 1000, 1)
        self.sock.setblocking(False)
        self.last_ping = time.time()
        self.last_packet_ts = time.time()
        self.pings_received += 1
        log(f"Authenticated with {HOST_MCS}:{PORT_MCS} (Handshake latency: {self.latency_ms} ms)")

    def _recv_exact(self, n):
        buf = bytearray()
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionResetError("Socket closed by remote peer.")
            buf.extend(chunk)
        return bytes(buf)

    def send_ping(self):
        self.sock.sendall(bytes([TAG_HEARTBEAT_PING, 0]))
        self.last_ping = time.time()
        self.pings_sent += 1

    def send_pong(self):
        self.sock.sendall(bytes([TAG_HEARTBEAT_ACK, 0]))


# ==============================================================================
# HTTP REST Telemetry Server (Home Assistant & Local Automation)
# ==============================================================================

GLOBAL_CLIENT = None
GLOBAL_ARGS = None

STATE = {
    "start_time": time.time(),
    "source": "Android Earthquake Alerts System (AEAS / MCS)",
    "location": {
        "name": "Base Station",
        "lat": 0.0,
        "lon": 0.0
    },
    "google_mcs": {
        "connected": False,
        "conectado": False,
        "android_id": None,
        "pings_sent": 0,
        "pings_received": 0,
        "messages_received": 0,
        "latency_ms": None,
        "last_packet_ts": None,
        "errors": 0,
        "last_error": None
    },
    "last_quake": None,
    "ultimo_sismo": None,
    "total_detections": 0
}


def dispatch_webhook(payload, url=None, secret=None):
    target_url = url or (GLOBAL_ARGS.webhook_url if GLOBAL_ARGS else None)
    target_secret = secret or (GLOBAL_ARGS.webhook_secret if GLOBAL_ARGS else None)
    if not target_url:
        return None
    try:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if target_secret:
            sig = hmac.new(target_secret.encode(), body, hashlib.sha256).hexdigest()
            headers["X-Quake-Signature"] = sig
            headers["X-Sismo-Firma"] = sig

        req = urllib.request.Request(target_url, data=body, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=8) as r:
            log(f"Webhook dispatched successfully (HTTP {r.status}) to {target_url}")
            return r.status
    except Exception as e:
        log(f"Error dispatching webhook: {e}")
        return None


class QuakeHTTPHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        # Silence default access logging to avoid terminal spam
        pass

    def send_cors_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Requested-With, Origin, Accept, X-Quake-Signature, X-Sismo-Firma")
        self.send_header("Access-Control-Allow-Private-Network", "true")

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_cors_headers()
        self.end_headers()

    def do_GET(self):
        clean_path = self.path.split("?")[0]
        if clean_path in ("/", "/index.html"):
            # If docs/index.html exists locally, serve the full dashboard
            candidates = [
                os.path.join(os.path.dirname(os.path.abspath(__file__)), "docs", "index.html"),
                os.path.join(os.getcwd(), "docs", "index.html")
            ]
            for c in candidates:
                if os.path.isfile(c):
                    try:
                        with open(c, "rb") as f:
                            html_bytes = f.read()
                        self.send_response(200)
                        self.send_cors_headers()
                        self.send_header("Content-Type", "text/html; charset=utf-8")
                        self.end_headers()
                        self.wfile.write(html_bytes)
                        return
                    except Exception:
                        pass

        if clean_path in ("/", "/status", "/api/status"):
            self.send_response(200)
            self.send_cors_headers()
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            uptime_s = round(time.time() - STATE["start_time"], 1)
            resp = {
                "status": "online" if STATE["google_mcs"]["connected"] else "reconnecting",
                "uptime_s": uptime_s,
                "source": STATE["source"],
                "fuente_principal": STATE["source"],
                "location": STATE["location"],
                "ubicacion": {"ciudad": STATE["location"]["name"], "lat": STATE["location"]["lat"], "lon": STATE["location"]["lon"]},
                "google_mcs": STATE["google_mcs"],
                "last_quake": STATE["last_quake"],
                "ultimo_sismo": STATE["ultimo_sismo"],
                "total_detections": STATE["total_detections"],
                "recent_logs": list(RECENT_LOGS)
            }
            self.wfile.write(json.dumps(resp, ensure_ascii=False).encode("utf-8"))

        elif clean_path == "/ping":
            lat = None
            if GLOBAL_CLIENT and GLOBAL_CLIENT.sock:
                try:
                    t0 = time.time()
                    GLOBAL_CLIENT.send_ping()
                    STATE["google_mcs"]["pings_sent"] += 1
                    lat = round((time.time() - t0) * 1000, 1)
                    STATE["google_mcs"]["latency_ms"] = lat
                except Exception as e:
                    lat = str(e)

            self.send_response(200)
            self.send_cors_headers()
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(json.dumps({"ok": True, "latency_ms": lat}).encode("utf-8"))

        else:
            self.send_response(404)
            self.send_cors_headers()
            self.end_headers()

    def do_POST(self):
        clean_path = self.path.split("?")[0]
        if clean_path in ("/drill", "/simulacro"):
            log("📣 Safety drill triggered via HTTP REST API")
            drill_payload = {
                "level": "drill",
                "nivel": "simulacro",
                "source": "Safety Drill Simulation",
                "id": f"drill-{int(time.time())}",
                "magnitude": 5.0,
                "magnitud": 5.0,
                "place": f"Safety Drill Simulation at {STATE['location']['name']}",
                "lugar": f"Simulacro de Seguridad en {STATE['location']['name']}",
                "timestamp": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "hora_local": dt.datetime.now().strftime("%H:%M:%S")
            }
            status = dispatch_webhook(drill_payload)
            self.send_response(200)
            self.send_cors_headers()
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(json.dumps({
                "ok": True,
                "message": "Safety drill dispatched",
                "webhook_status": status
            }).encode("utf-8"))

        elif clean_path == "/ping":
            lat = None
            if GLOBAL_CLIENT and GLOBAL_CLIENT.sock:
                try:
                    t0 = time.time()
                    GLOBAL_CLIENT.send_ping()
                    STATE["google_mcs"]["pings_sent"] += 1
                    lat = round((time.time() - t0) * 1000, 1)
                    STATE["google_mcs"]["latency_ms"] = lat
                except Exception as e:
                    lat = str(e)

            self.send_response(200)
            self.send_cors_headers()
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(json.dumps({"ok": True, "latency_ms": lat}).encode("utf-8"))

        else:
            self.send_response(404)
            self.send_cors_headers()
            self.end_headers()


def start_http_server(host, port):
    server = http.server.ThreadingHTTPServer((host, port), QuakeHTTPHandler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return server


# ==============================================================================
# Main Listener Loop & Event Dispatcher
# ==============================================================================

def run_listener(args):
    global GLOBAL_CLIENT, GLOBAL_ARGS
    GLOBAL_ARGS = args

    STATE["location"]["name"] = args.name
    STATE["location"]["lat"] = args.lat
    STATE["location"]["lon"] = args.lon

    if not args.no_http:
        try:
            start_http_server("0.0.0.0", args.http_port)
            log(f"HTTP REST telemetry server active on http://0.0.0.0:{args.http_port}/status")
        except Exception as e:
            log(f"Warning: Could not start HTTP server on port {args.http_port}: {e}")

    creds = get_credentials(args.locale, args.timezone)
    STATE["google_mcs"]["android_id"] = str(creds.get("android_id"))

    client = QuakeMCSClient(creds, ping_interval=args.ping_interval)
    GLOBAL_CLIENT = client

    def dispatch_alert(ev):
        mag = ev.get("magnitude") or 4.0
        lat = ev.get("lat")
        lon = ev.get("lon")
        region = ev.get("region") or args.name

        dist = haversine_distance(args.lat, args.lon, lat, lon) if (lat and lon) else None
        radius = ev.get("radius_km") or 150.0
        level = "alert" if (dist and dist <= radius and mag >= 4.5) else ("alert" if mag >= 5.0 else "notice")
        nivel_es = "alerta" if level == "alert" else "aviso"

        place_desc = f"M{mag} at {dist} km from {args.name} ({region})" if dist else f"M{mag} in {region}"
        now_str = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        payload = {
            "level": level,
            "nivel": nivel_es,
            "source": "Android AEAS (MCS)",
            "id": f"aeas-{int(time.time())}",
            "magnitude": mag,
            "magnitud": mag,
            "distance_km": dist,
            "distancia_km": dist,
            "lat": lat,
            "lon": lon,
            "place": place_desc,
            "lugar": place_desc,
            "timestamp": now_str,
            "hora_local": now_str,
            "status": "early alert"
        }

        STATE["last_quake"] = payload
        STATE["ultimo_sismo"] = payload
        STATE["total_detections"] += 1

        log(f"🚨 EARTHQUAKE ALERT DETECTED! {place_desc} (Level: {level})")
        dispatch_webhook(payload)

    log(f"Starting Quake MCS Listener at {args.name} ({args.lat}, {args.lon})")
    log(f"Ping interval: {client.ping_interval}s | Webhook: {args.webhook_url or 'Disabled'}")

    backoff = 3
    while True:
        try:
            client.connect()
            STATE["google_mcs"]["connected"] = True
            STATE["google_mcs"]["conectado"] = True
            STATE["google_mcs"]["latency_ms"] = client.latency_ms
            backoff = 3

            while True:
                rlist, _, _ = select.select([client.sock], [], [], 3.0)
                if rlist:
                    tag_chunk = client.sock.recv(1)
                    if not tag_chunk:
                        break
                    tag = tag_chunk[0]
                    l = read_varint_from_stream(client.sock)
                    if l is None:
                        break
                    payload = client._recv_exact(l) if l > 0 else b""
                    client.last_packet_ts = time.time()
                    STATE["google_mcs"]["last_packet_ts"] = client.last_packet_ts

                    if tag == TAG_HEARTBEAT_PING:
                        client.send_pong()
                        client.pings_received += 1
                        STATE["google_mcs"]["pings_received"] = client.pings_received
                    elif tag == TAG_HEARTBEAT_ACK or tag == TAG_IQ_STANZA:
                        client.pings_received += 1
                        STATE["google_mcs"]["pings_received"] = client.pings_received
                    elif tag == TAG_DATA_MESSAGE_STANZA:
                        client.messages_received += 1
                        STATE["google_mcs"]["messages_received"] = client.messages_received
                        stanza = parse_protobuf(payload)
                        cat = stanza.get(5, [(2, b"")])[0][1].decode('latin1', errors='ignore')
                        raw = stanza.get(21, [(2, None)])[0][1]
                        if raw and (cat == "com.google.android.gms"):
                            for ev in decode_earthquake_payload(raw):
                                dispatch_alert(ev)
                    elif tag == TAG_CLOSE:
                        log("Server sent Close command. Reconnecting...")
                        break

                if time.time() - client.last_ping > client.ping_interval:
                    client.send_ping()
                    STATE["google_mcs"]["pings_sent"] = client.pings_sent

        except KeyboardInterrupt:
            log("Listener stopped by user.")
            break
        except Exception as e:
            STATE["google_mcs"]["connected"] = False
            STATE["google_mcs"]["conectado"] = False
            STATE["google_mcs"]["errors"] += 1
            STATE["google_mcs"]["last_error"] = str(e)
            log(f"Socket exception: {e}. Reconnecting in {backoff}s...")
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)


def simulate_alert():
    log("Running synthetic event decoding test...")
    jhom = field_varint(1, int(6.40 * 1e7)) + field_varint(2, int(-75.50 * 1e7))
    circle = field_bytes(1, jhom) + field_varint(2, 90000)
    impact_zone = field_varint(1, 2) + field_bytes(3, circle)
    geom_jeif = field_bytes(2, field_bytes(1, impact_zone))
    mag_jeie = field_str(1, "5.4")
    event_jeik = (
        field_bytes(1, field_str(3, "synthetic") + field_varint(4, int(time.time() * 1000))) +
        field_varint(2, 1) +
        field_varint(3, 2) +
        field_bytes(6, geom_jeif) +
        field_bytes(7, mag_jeie) +
        field_str(8, "Test Region")
    )
    payload = field_varint(1, int(time.time() * 1000)) + field_bytes(2, event_jeik)

    events = decode_earthquake_payload(payload)
    ev = events[0]
    log(f"Synthetic test successful: Magnitude={ev['magnitude']}, Region={ev['region']}")
    print(json.dumps(ev, indent=2))


def test_ping():
    log("Testing TLS connection to mtalk.google.com:5228...")
    creds = get_credentials()
    client = QuakeMCSClient(creds)
    client.connect()
    client.send_ping()
    time.sleep(0.5)
    log(f"Test completed! Handshake latency: {client.latency_ms} ms. Connection verified.")
    client.sock.close()


def main():
    parser = argparse.ArgumentParser(
        description="Quake MCS Listener - Lightweight Android Earthquake Alerts System (AEAS) Client"
    )
    parser.add_argument("--lat", type=float, default=float(os.environ.get("QUAKE_LAT", "0.0")), help="Base monitoring latitude")
    parser.add_argument("--lon", type=float, default=float(os.environ.get("QUAKE_LON", "0.0")), help="Base monitoring longitude")
    parser.add_argument("--name", type=str, default=os.environ.get("QUAKE_NAME", "Base Station"), help="Human-readable location name")
    parser.add_argument("--ping-interval", type=int, default=int(os.environ.get("QUAKE_PING_INTERVAL", "120")), help="Keepalive ping interval in seconds (30-600)")
    parser.add_argument("--http-port", type=int, default=int(os.environ.get("QUAKE_HTTP_PORT", "8990")), help="Local HTTP REST server port for Home Assistant (default: 8990)")
    parser.add_argument("--no-http", action="store_true", help="Disable the local HTTP REST telemetry server")
    parser.add_argument("--webhook-url", type=str, default=os.environ.get("QUAKE_WEBHOOK_URL", ""), help="Webhook destination URL (e.g. Home Assistant)")
    parser.add_argument("--webhook-secret", type=str, default=os.environ.get("QUAKE_WEBHOOK_SECRET", ""), help="Optional HMAC-SHA256 secret for payload signing")
    parser.add_argument("--locale", type=str, default="en_US", help="Locale for device registration")
    parser.add_argument("--timezone", type=str, default="UTC", help="Timezone for device registration")
    parser.add_argument("--test-ping", action="store_true", help="Perform a single diagnostic TLS ping and exit")
    parser.add_argument("--simulate", action="store_true", help="Test internal Protobuf event decoding")

    args = parser.parse_args()

    if args.test_ping:
        test_ping()
    elif args.simulate:
        simulate_alert()
    else:
        run_listener(args)


if __name__ == "__main__":
    main()

