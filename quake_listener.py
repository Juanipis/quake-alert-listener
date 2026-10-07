#!/usr/bin/env python3
"""quake_listener.py
Lightweight, autonomous client (<15 MB RAM, 0 external dependencies) for receiving
real-time earthquake alerts from the Android Earthquake Alerts System (AEAS) via MCS (mtalk:5228).

Designed for seamless integration with Home Assistant and local home automation systems.
Built with Google Antigravity (AGY) & Gemini 3.8 Flash (Thinking High).

Usage:
  python3 quake_listener.py --lat <your-lat> --lon <your-lon> --name "Base Station"
  python3 quake_listener.py --ping-interval 120 --webhook-url http://127.0.0.1:8123/api/webhook/quake
  python3 quake_listener.py --test-ping
  python3 quake_listener.py --simulate
"""

import argparse
import base64
import collections
import copy
import datetime as dt
import hashlib
import hmac
import http.server
import ipaddress
import json
import math
import os
import random
import signal
import socket
import ssl
import struct
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

if sys.version_info < (3, 8):  # pragma: no cover - guard for very old interpreters
    sys.stderr.write("quake_listener.py requires Python 3.8 or newer.\n")
    sys.exit(1)

__version__ = "1.2.0"

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

# Hard upper bound for a single MCS frame; protects against a corrupt length prefix
# making us try to buffer gigabytes.
MAX_PACKET_SIZE = 4 * 1024 * 1024
# How long a blocking read waits before the loop gets a chance to send pings,
# serve manual ping requests and notice a shutdown request.
READ_TIMEOUT_S = 0.5
CONNECT_TIMEOUT_S = 15
BACKOFF_MIN_S = 3
BACKOFF_MAX_S = 60
# A session that lived at least this long counts as healthy and resets the backoff.
STABLE_SESSION_S = 60

DEFAULT_CREDENTIALS_FILE = os.path.join(os.path.expanduser("~"), ".quake_device_credentials.json")
CREDENTIALS_FILE = DEFAULT_CREDENTIALS_FILE

# Browser origins allowed to use the REST API (CORS). Requests without an Origin
# header (curl, Home Assistant rest/rest_command) are always accepted. Loopback
# origins (http://127.0.0.1:*, http://localhost:*, http://[::1]:*) are always accepted.
DEFAULT_ALLOWED_ORIGINS = "https://juanipis.github.io"
ALLOWED_ORIGINS = set(DEFAULT_ALLOWED_ORIGINS.split(","))

# Host names the REST API answers to (DNS-rebinding guard). IP literals, "localhost"
# and this machine's own host name are always accepted; anything else needs
# --allowed-hosts / QUAKE_ALLOWED_HOSTS ("*" disables the check).
ALLOWED_HOSTS = set()

RECENT_LOGS = collections.deque(maxlen=30)
_LOG_LOCK = threading.Lock()

# Set when the process should shut down (Ctrl+C / SIGTERM).
STOP_EVENT = threading.Event()

# --debug-frames: log every non-heartbeat MCS frame (tag, size, category, sender, keys).
DEBUG_FRAMES = False
TAG_NAMES = {0: "HeartbeatPing", 1: "HeartbeatAck", 2: "LoginRequest", 3: "LoginResponse",
             4: "Close", 7: "IqStanza", 8: "DataMessageStanza"}


class _ShutdownRequested(BaseException):
    """Raised from the SIGTERM handler; BaseException so `except Exception` won't swallow it."""


def _console_write(line):
    stream = sys.stdout
    try:
        stream.write(line)
        stream.flush()
    except UnicodeEncodeError:
        # Legacy Windows code pages / redirected output can't encode emoji.
        enc = getattr(stream, "encoding", None) or "ascii"
        try:
            stream.write(line.encode(enc, errors="replace").decode(enc, errors="replace"))
            stream.flush()
        except Exception:
            pass
    except (OSError, ValueError):
        # stdout closed or detached (e.g. running as a background service).
        pass


def log(msg):
    now = time.localtime()
    line = "[%s] %s\n" % (time.strftime('%Y-%m-%d %H:%M:%S', now), msg)
    with _LOG_LOCK:
        _console_write(line)
        RECENT_LOGS.append({"time": time.strftime('%H:%M:%S', now), "msg": msg})


def redact_url(url):
    """Hide the path/query of a URL (Home Assistant webhook IDs are secrets) for logging."""
    if not url:
        return url
    try:
        parts = urllib.parse.urlsplit(url)
        host = parts.hostname or ""
        if ":" in host:
            host = "[%s]" % host
        if parts.port:
            host = "%s:%d" % (host, parts.port)
        suffix = "/***" if (parts.path not in ("", "/") or parts.query) else "/"
        return "%s://%s%s" % (parts.scheme, host, suffix)
    except Exception:
        return "<webhook>"


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


def decode_varint(data, pos=0):
    """Decode a varint from `data` at `pos`.

    Returns (value, new_pos), or None if the buffer ends before the varint does.
    Raises ValueError for varints longer than 10 bytes (corrupt stream).
    """
    val = 0
    shift = 0
    length = len(data)
    for _ in range(10):
        if pos >= length:
            return None
        b = data[pos]
        pos += 1
        val |= (b & 0x7F) << shift
        if not (b & 0x80):
            return val, pos
        shift += 7
    raise ValueError("Malformed varint (more than 10 bytes)")


def field_varint(tag, val):
    return encode_varint((tag << 3) | 0) + encode_varint(val)


def field_bytes(tag, val):
    return encode_varint((tag << 3) | 2) + encode_varint(len(val)) + val


def field_str(tag, s):
    return field_bytes(tag, s.encode("utf-8"))


def field_double(tag, val):
    return encode_varint((tag << 3) | 1) + struct.pack("<d", val)


def parse_protobuf(data):
    """Schema-less protobuf parser: {tag: [(wire_type, value), ...]}.

    Truncated or unsupported input stops parsing and returns what was decoded so far.
    """
    fields = {}
    p = 0
    length_data = len(data)
    try:
        while p < length_data:
            res = decode_varint(data, p)
            if res is None:
                break
            key, p = res
            tag = key >> 3
            wire = key & 0x07

            if wire == 0:
                res = decode_varint(data, p)
                if res is None:
                    break
                val, p = res
            elif wire == 1:
                if p + 8 > length_data:
                    break
                val = struct.unpack("<Q", data[p:p + 8])[0]
                p += 8
            elif wire == 2:
                res = decode_varint(data, p)
                if res is None:
                    break
                l, p = res
                if p + l > length_data:
                    break
                val = bytes(data[p:p + l])
                p += l
            elif wire == 5:
                if p + 4 > length_data:
                    break
                val = struct.unpack("<I", data[p:p + 4])[0]
                p += 4
            else:
                break
            fields.setdefault(tag, []).append((wire, val))
    except ValueError:
        pass
    return fields


def _pb_first(fields, tag):
    """(wire, value) of the first occurrence of `tag`, or (None, None)."""
    vals = fields.get(tag)
    return vals[0] if vals else (None, None)


def _pb_bytes(fields, tag):
    wire, val = _pb_first(fields, tag)
    return val if wire == 2 else None


def _pb_text(fields, tag, default=None, encoding="utf-8"):
    raw = _pb_bytes(fields, tag)
    return raw.decode(encoding, errors="ignore") if raw is not None else default


def _pb_number(wire, val):
    """Interpret a scalar field as a float according to its wire type."""
    if wire == 1:   # fixed64 -> double
        return struct.unpack("<d", struct.pack("<Q", val))[0]
    if wire == 5:   # fixed32 -> float
        return struct.unpack("<f", struct.pack("<I", val))[0]
    if wire == 0:   # varint -> (possibly negative) integer
        return float(val - (1 << 64) if val >= (1 << 63) else val)
    if wire == 2:   # numeric string
        try:
            return float(val.decode("latin1", errors="ignore"))
        except ValueError:
            return None
    return None


def _pb_coordinate(wire, val, limit):
    num = _pb_number(wire, val)
    if num is None or math.isnan(num) or math.isinf(num):
        return None
    if wire == 0 and abs(num) > limit:
        num /= 1e7  # integer coordinates are conventionally degrees * 1e7 (E7)
    return num if abs(num) <= limit else None


def haversine_distance(lat1, lon1, lat2, lon2):
    R = 6371.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2.0)**2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2.0)**2
    a = min(1.0, max(0.0, a))  # float rounding can push `a` just outside [0, 1]
    return round(2 * R * math.atan2(math.sqrt(a), math.sqrt(1 - a)), 1)


# ==============================================================================
# Anonymous Device Hardware Registration
# ==============================================================================

def save_credentials(creds, path=None):
    """Atomically write credentials readable only by the current user (0600)."""
    path = path or CREDENTIALS_FILE
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    tmp = "%s.tmp-%d" % (path, os.getpid())
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(creds, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def register_device(locale="en_US", tz="UTC"):
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
    android_id = _pb_first(parsed, 7)[1]
    security_token = _pb_first(parsed, 8)[1]

    if not (isinstance(android_id, int) and isinstance(security_token, int) and android_id and security_token):
        raise RuntimeError("Registration response did not return valid hardware credentials.")

    creds = {
        "android_id": android_id,
        "security_token": security_token,
        "created_at": time.time(),
        "locale": locale,
        "time_zone": tz
    }
    try:
        save_credentials(creds)
        log(f"Device registered successfully (id: {android_id}); credentials saved to {CREDENTIALS_FILE}")
    except OSError as e:
        log(f"Device registered (id: {android_id}) but credentials could not be saved to "
            f"{CREDENTIALS_FILE}: {e}. A new identity will be registered on next start.")
    return creds


def _valid_credentials(creds):
    try:
        return (isinstance(creds, dict) and int(creds["android_id"]) > 0
                and int(creds["security_token"]) > 0)
    except (KeyError, TypeError, ValueError):
        return False


def get_credentials(locale="en_US", tz="UTC"):
    path = CREDENTIALS_FILE
    if os.path.isfile(path):
        try:
            with open(path, "r") as f:
                creds = json.load(f)
            if _valid_credentials(creds):
                creds["android_id"] = int(creds["android_id"])
                creds["security_token"] = int(creds["security_token"])
                if os.name == "posix":
                    try:
                        if os.stat(path).st_mode & 0o077:
                            os.chmod(path, 0o600)
                            log(f"Restricted permissions of {path} to owner-only (0600).")
                    except OSError:
                        pass
                return creds
            log(f"Credentials file {path} is incomplete; registering a new identity.")
        except (OSError, ValueError) as e:
            log(f"Could not read credentials file {path} ({e}); registering a new identity.")
    return register_device(locale, tz)


# ==============================================================================
# Earthquake Alert Protobuf Decoder (AEAS)
# ==============================================================================

def _decode_event(ev):
    mag = None
    mag_raw = _pb_bytes(ev, 7)
    if mag_raw is not None:
        mag_p = parse_protobuf(mag_raw)
        if 2 in mag_p:
            num = _pb_number(*mag_p[2][0])
            if num is not None and not math.isnan(num):
                mag = round(num, 1)
        elif 1 in mag_p:
            num = _pb_number(*mag_p[1][0])
            if num is not None and not math.isnan(num):
                mag = round(num, 1)

    region = _pb_text(ev, 8, default="Region")
    epicenter_lat, epicenter_lon, radius_km = None, None, None

    geom = _pb_bytes(ev, 6)
    if geom is not None:
        jeif = parse_protobuf(geom)
        jeig_raw = _pb_bytes(jeif, 2)
        if jeig_raw is not None:
            jeig = parse_protobuf(jeig_raw)
            for wire, zone_b in jeig.get(1, []):
                if wire != 2:
                    continue
                zone = parse_protobuf(zone_b)
                for cwire, circ_b in zone.get(3, []):
                    if cwire != 2:
                        continue
                    circ = parse_protobuf(circ_b)
                    if 2 in circ:
                        radius_m = _pb_number(*circ[2][0])
                        if radius_m is not None and radius_m >= 0:
                            radius_km = round(radius_m / 1000.0, 1)
                    center = _pb_bytes(circ, 1)
                    if center is not None:
                        jhom = parse_protobuf(center)
                        if 1 in jhom:
                            epicenter_lat = _pb_coordinate(*jhom[1][0], limit=90.0)
                        if 2 in jhom:
                            epicenter_lon = _pb_coordinate(*jhom[2][0], limit=180.0)

    return {
        "magnitude": mag,
        "region": region,
        "lat": epicenter_lat,
        "lon": epicenter_lon,
        "radius_km": radius_km,
    }


def decode_earthquake_payload(raw_bytes):
    jeim = parse_protobuf(raw_bytes)
    events = []
    for wire, ev_bytes in jeim.get(2, []):
        if wire != 2:
            continue
        try:
            events.append(_decode_event(parse_protobuf(ev_bytes)))
        except Exception as e:  # one malformed event must not drop the others
            log(f"Skipping undecodable event ({len(ev_bytes)} bytes): {e}")
    return events


_LAST_EVENT_ID = {"base": None, "n": 0}
_EVENT_ID_LOCK = threading.Lock()


def _new_event_id(prefix):
    """`<prefix>-<unix seconds>`, with a `-N` suffix if several events share a second."""
    with _EVENT_ID_LOCK:
        base = f"{prefix}-{int(time.time())}"
        if base == _LAST_EVENT_ID["base"]:
            _LAST_EVENT_ID["n"] += 1
            return f"{base}-{_LAST_EVENT_ID['n']}"
        _LAST_EVENT_ID["base"], _LAST_EVENT_ID["n"] = base, 1
        return base


def build_alert_payload(ev, base_name, base_lat, base_lon):
    """Turn a decoded AEAS event into the webhook / REST payload."""
    mag = ev.get("magnitude")
    if mag is None:
        mag = 4.0
    lat = ev.get("lat")
    lon = ev.get("lon")
    region = ev.get("region") or base_name

    dist = haversine_distance(base_lat, base_lon, lat, lon) if (lat is not None and lon is not None) else None
    radius = ev.get("radius_km") or 150.0
    if dist is not None and dist <= radius and mag >= 4.5:
        level = "alert"
    elif mag >= 5.0:
        level = "alert"
    else:
        level = "notice"
    nivel_es = "alerta" if level == "alert" else "aviso"

    place_desc = f"M{mag} at {dist} km from {base_name} ({region})" if dist is not None else f"M{mag} in {region}"
    now_str = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    return {
        "level": level,
        "nivel": nivel_es,
        "source": "Android AEAS (MCS)",
        "id": _new_event_id("aeas"),
        "magnitude": mag,
        "magnitud": mag,
        "distance_km": dist,
        "distancia_km": dist,
        "lat": lat,
        "lon": lon,
        "radius_km": ev.get("radius_km"),
        "place": place_desc,
        "lugar": place_desc,
        "timestamp": now_str,
        "hora_local": now_str,
        "status": "early alert"
    }


# ==============================================================================
# TLS Socket Client for Google MCS (mtalk:5228)
# ==============================================================================

class QuakeMCSClient:
    def __init__(self, creds, ping_interval=120):
        self.android_id = int(creds["android_id"])
        self.security_token = int(creds["security_token"])
        self.ping_interval = max(30, min(int(ping_interval), 600))
        self.sock = None
        self.connected = False
        self.last_ping = time.time()
        self.pings_sent = 0
        self.pings_received = 0
        self.messages_received = 0
        self.last_packet_ts = None
        self.latency_ms = None            # last heartbeat round-trip time
        self.handshake_latency_ms = None  # TCP + TLS + login time of the last connect
        self._buf = bytearray()
        self._last_rx_mono = time.monotonic()
        # Persistent IDs of data messages received since the last login; sent back in the
        # next LoginRequest (received_persistent_id) so the server stops re-delivering them.
        self.unacked_persistent_ids = []
        self._seen_persistent_ids = collections.OrderedDict()
        # Heartbeat bookkeeping (shared with the HTTP thread through _cond).
        self._cond = threading.Condition()
        self._session = 0
        self._session_pings = 0
        self._session_acks = 0
        self._ping_sent_at = collections.deque(maxlen=32)
        self._manual_ping_requested = False
        self._manual_ping_seq = None

    # ------------------------------------------------------------------ framing
    def _login_packet(self):
        setting = field_str(1, "new_vc") + field_str(2, "1")
        login_req = (
            field_str(1, "chrome-120.0.6099.144") +
            field_str(2, "mcs.android.com") +
            field_str(3, str(self.android_id)) +
            field_str(4, str(self.android_id)) +
            field_str(5, str(self.security_token)) +
            field_str(6, f"android-{self.android_id:x}") +
            field_bytes(8, setting) +
            b"".join(field_str(10, pid) for pid in self.unacked_persistent_ids) +
            field_varint(14, 1) +
            field_varint(16, 2) +
            field_varint(17, 1)
        )
        return bytes([MCS_VERSION, TAG_LOGIN_REQUEST]) + encode_varint(len(login_req)) + login_req

    def _fill(self):
        """Read more bytes into the buffer. False on read timeout; raises on EOF."""
        try:
            chunk = self.sock.recv(16384)
        except (socket.timeout, ssl.SSLWantReadError):
            return False
        if not chunk:
            raise ConnectionResetError("Socket closed by remote peer.")
        self._buf.extend(chunk)
        self._last_rx_mono = time.monotonic()
        return True

    def _parse_frame(self):
        """Pop one complete (tag, payload) frame from the buffer, or None if incomplete."""
        if not self._buf:
            return None
        res = decode_varint(self._buf, 1)
        if res is None:
            return None
        length, start = res
        if length > MAX_PACKET_SIZE:
            raise ConnectionError(f"MCS frame too large ({length} bytes); stream is corrupt.")
        end = start + length
        if len(self._buf) < end:
            return None
        tag = self._buf[0]
        payload = bytes(self._buf[start:end])
        del self._buf[:end]
        return tag, payload

    def read_packet(self):
        """Return the next (tag, payload), or None if nothing complete arrived within READ_TIMEOUT_S.

        Partial frames stay buffered across calls, so TLS records split anywhere are handled.
        """
        while True:
            frame = self._parse_frame()
            if frame is not None:
                return frame
            if not self._fill():
                return None

    def _recv_exact(self, n):
        """Blocking read of exactly n bytes (uses buffered data first)."""
        while len(self._buf) < n:
            if not self._fill():
                raise socket.timeout("Timed out waiting for data from MCS.")
        data = bytes(self._buf[:n])
        del self._buf[:n]
        return data

    # ------------------------------------------------------------- connection
    def connect(self):
        self.close()
        t0 = time.monotonic()
        raw_sock = socket.create_connection((HOST_MCS, PORT_MCS), timeout=CONNECT_TIMEOUT_S)
        try:
            raw_sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            ctx = ssl.create_default_context()
            self.sock = ctx.wrap_socket(raw_sock, server_hostname=HOST_MCS)
        except BaseException:
            raw_sock.close()
            raise
        self._buf = bytearray()
        self.sock.settimeout(CONNECT_TIMEOUT_S)

        sent_ids = list(self.unacked_persistent_ids)
        self.sock.sendall(self._login_packet())

        v_byte = self._recv_exact(1)[0]
        if v_byte != MCS_VERSION:
            raise ConnectionError(f"Handshake failed: unexpected MCS version {v_byte}")
        frame = None
        while frame is None:
            frame = self._parse_frame()
            if frame is None and not self._fill():
                raise socket.timeout("Timed out waiting for MCS login response.")
        tag, payload = frame
        if tag != TAG_LOGIN_RESPONSE:
            raise ConnectionError(f"Handshake failed: version={v_byte}, tag={tag}")
        resp = parse_protobuf(payload)
        err_raw = _pb_bytes(resp, 3)
        if err_raw is not None:
            err = parse_protobuf(err_raw)
            code = _pb_first(err, 1)[1]
            msg = _pb_text(err, 2, default="")
            raise ConnectionError(f"Login rejected by MCS (code {code}: {msg or 'no message'})")

        # Server has now acknowledged the persistent IDs we reported.
        self.unacked_persistent_ids = [p for p in self.unacked_persistent_ids if p not in sent_ids]

        self.handshake_latency_ms = round((time.monotonic() - t0) * 1000, 1)
        self.latency_ms = self.handshake_latency_ms
        self.sock.settimeout(READ_TIMEOUT_S)
        now = time.time()
        self.last_ping = now
        self.last_packet_ts = now
        self._last_rx_mono = time.monotonic()
        self.pings_received += 1
        with self._cond:
            self._session += 1
            self._session_pings = 0
            self._session_acks = 0
            self._ping_sent_at.clear()
            self.connected = True
            self._cond.notify_all()
        log(f"Authenticated with {HOST_MCS}:{PORT_MCS} (Handshake latency: {self.handshake_latency_ms} ms)")

    def close(self):
        with self._cond:
            self.connected = False
            self._manual_ping_requested = False
            self._cond.notify_all()
        sock, self.sock = self.sock, None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass

    def seconds_since_rx(self):
        return time.monotonic() - self._last_rx_mono

    # ------------------------------------------------------------- heartbeats
    def send_ping(self):
        """Send a HeartbeatPing. Must only be called from the thread that owns the socket."""
        self.sock.sendall(bytes([TAG_HEARTBEAT_PING, 0]))
        self.last_ping = time.time()
        with self._cond:
            self.pings_sent += 1
            self._session_pings += 1
            self._ping_sent_at.append(time.monotonic())
            return self._session_pings

    def send_pong(self):
        self.sock.sendall(bytes([TAG_HEARTBEAT_ACK, 0]))

    def on_heartbeat_ack(self):
        """Record a HeartbeatAck; returns the measured round-trip time in ms (or None)."""
        with self._cond:
            self._session_acks += 1
            rtt = None
            if self._ping_sent_at:
                rtt = round((time.monotonic() - self._ping_sent_at.popleft()) * 1000, 1)
                self.latency_ms = rtt
            self._cond.notify_all()
            return rtt

    def service_ping(self):
        """Send a ping if one is due or was requested over HTTP (socket-owner thread only)."""
        with self._cond:
            manual = self._manual_ping_requested
        if not manual and time.time() - self.last_ping < self.ping_interval:
            return False
        seq = self.send_ping()
        if manual:
            with self._cond:
                self._manual_ping_requested = False
                self._manual_ping_seq = (self._session, seq)
                self._cond.notify_all()
        return True

    def request_ping(self, timeout=5.0):
        """Called from other threads: ask the listener loop to ping and wait for the ack.

        Returns the round-trip time in ms, or None if not connected / no ack in time.
        The socket is only ever written by the listener thread (SSL objects are not
        safe for concurrent use).
        """
        deadline = time.monotonic() + timeout
        with self._cond:
            if not self.connected:
                return None
            session = self._session
            self._manual_ping_seq = None
            self._manual_ping_requested = True
            while True:
                seq = self._manual_ping_seq
                if seq is not None and seq[0] == session and self._session_acks >= seq[1]:
                    return self.latency_ms
                if not self.connected or self._session != session:
                    return None
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._cond.wait(remaining)

    # ----------------------------------------------------------- data messages
    def register_persistent_id(self, pid):
        """Track a received persistent_id. Returns False if it is a duplicate delivery."""
        if not pid:
            return True
        if pid in self._seen_persistent_ids:
            return False
        self._seen_persistent_ids[pid] = True
        while len(self._seen_persistent_ids) > 512:
            self._seen_persistent_ids.popitem(last=False)
        self.unacked_persistent_ids.append(pid)
        del self.unacked_persistent_ids[:-100]
        return True


# ==============================================================================
# HTTP REST Telemetry Server (Home Assistant & Local Automation)
# ==============================================================================

GLOBAL_CLIENT = None
GLOBAL_ARGS = None

STATE_LOCK = threading.RLock()
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
        "handshake_latency_ms": None,
        "last_packet_ts": None,
        "connected_since": None,
        "reconnects": 0,
        "errors": 0,
        "last_error": None
    },
    "emsc": {
        "enabled": False,
        "connected": False,
        "events_received": 0,
        "last_event_ts": None,
        "connected_since": None,
        "reconnects": 0,
        "errors": 0,
        "last_error": None
    },
    "last_quake": None,
    "ultimo_sismo": None,
    "total_detections": 0
}


def _update_emsc_state(**kwargs):
    with STATE_LOCK:
        STATE["emsc"].update(kwargs)


def _update_mcs_state(**kwargs):
    with STATE_LOCK:
        STATE["google_mcs"].update(kwargs)


def _set_connected(flag):
    with STATE_LOCK:
        mcs = STATE["google_mcs"]
        mcs["connected"] = flag
        mcs["conectado"] = flag
        mcs["connected_since"] = time.time() if flag else None


def _sync_client_counters(client):
    _update_mcs_state(
        pings_sent=client.pings_sent,
        pings_received=client.pings_received,
        messages_received=client.messages_received,
        latency_ms=client.latency_ms,
        handshake_latency_ms=client.handshake_latency_ms,
        last_packet_ts=client.last_packet_ts,
    )


def status_snapshot():
    with STATE_LOCK:
        loc = dict(STATE["location"])
        mcs = copy.deepcopy(STATE["google_mcs"])
        resp = {
            "status": "online" if mcs["connected"] else "reconnecting",
            "version": __version__,
            "uptime_s": round(time.time() - STATE["start_time"], 1),
            "source": STATE["source"],
            "fuente_principal": STATE["source"],
            "location": loc,
            "ubicacion": {"ciudad": loc["name"], "lat": loc["lat"], "lon": loc["lon"]},
            "google_mcs": mcs,
            "emsc": copy.deepcopy(STATE["emsc"]),
            "last_quake": copy.deepcopy(STATE["last_quake"]),
            "ultimo_sismo": copy.deepcopy(STATE["ultimo_sismo"]),
            "total_detections": STATE["total_detections"],
        }
    with _LOG_LOCK:
        resp["recent_logs"] = list(RECENT_LOGS)
    return resp


def dispatch_webhook(payload, url=None, secret=None, attempts=1, timeout=8):
    """POST `payload` as JSON. Returns the final HTTP status, or None if no response.

    Network errors and 5xx responses are retried (`attempts` total) with a short backoff.
    """
    target_url = url or (GLOBAL_ARGS.webhook_url if GLOBAL_ARGS else None)
    target_secret = secret or (GLOBAL_ARGS.webhook_secret if GLOBAL_ARGS else None)
    if not target_url:
        return None
    shown_url = redact_url(target_url)
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json", "User-Agent": f"quake-alert-listener/{__version__}"}
    if target_secret:
        sig = hmac.new(target_secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
        headers["X-Quake-Signature"] = sig
        headers["X-Sismo-Firma"] = sig

    status = None
    delays = [2, 5, 10]
    for attempt in range(1, max(1, attempts) + 1):
        try:
            req = urllib.request.Request(target_url, data=body, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=timeout) as r:
                log(f"Webhook dispatched successfully (HTTP {r.status}) to {shown_url}")
                return r.status
        except urllib.error.HTTPError as e:
            status = e.code
            log(f"Webhook rejected with HTTP {e.code} by {shown_url} (attempt {attempt}/{attempts})")
            if e.code < 500:
                return status
        except Exception as e:
            log(f"Error dispatching webhook to {shown_url} (attempt {attempt}/{attempts}): {e}")
        if attempt < attempts and not STOP_EVENT.is_set():
            STOP_EVENT.wait(delays[min(attempt - 1, len(delays) - 1)])
    return status


_WEBHOOK_THREADS = set()
_WEBHOOK_THREADS_LOCK = threading.Lock()


def dispatch_webhook_async(payload, attempts=3):
    """Send a webhook from a background thread so the MCS loop never blocks on HTTP."""
    def runner():
        try:
            dispatch_webhook(payload, attempts=attempts)
        finally:
            with _WEBHOOK_THREADS_LOCK:
                _WEBHOOK_THREADS.discard(threading.current_thread())

    t = threading.Thread(target=runner, name="webhook", daemon=True)
    with _WEBHOOK_THREADS_LOCK:
        _WEBHOOK_THREADS.add(t)
    t.start()
    return t


def wait_for_webhooks(timeout):
    deadline = time.monotonic() + timeout
    with _WEBHOOK_THREADS_LOCK:
        threads = list(_WEBHOOK_THREADS)
    for t in threads:
        t.join(max(0.0, deadline - time.monotonic()))


# ==============================================================================
# EMSC SeismicPortal real-time feed (public WebSocket push, data CC BY 4.0)
# ==============================================================================
# Unlike AEAS, this needs no device enrollment: EMSC pushes every new or updated
# event worldwide, usually a few minutes after the origin time. It is a rapid
# report, not an early warning, but it is a source that reliably delivers.

EMSC_HOST = "www.seismicportal.eu"
EMSC_PATH = "/standing_order/websocket"
EMSC_MAX_EVENT_AGE_S = 15 * 60   # the feed also re-sends updates of old events
EMSC_PING_EVERY_S = 60
EMSC_SILENCE_LIMIT_S = 180
_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class MiniWebSocket:
    """Just enough RFC 6455 to follow a text feed over TLS: handshake, frames,
    fragmentation, ping/pong and close. Client frames are masked as required."""

    def __init__(self, host, path, timeout=CONNECT_TIMEOUT_S):
        raw = socket.create_connection((host, 443), timeout=timeout)
        try:
            raw.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            self.sock = ssl.create_default_context().wrap_socket(raw, server_hostname=host)
        except BaseException:
            raw.close()
            raise
        self._buf = bytearray()
        self._frag = []
        self.last_rx = time.monotonic()
        try:
            key = base64.b64encode(os.urandom(16)).decode()
            self.sock.sendall((
                f"GET {path} HTTP/1.1\r\nHost: {host}\r\nUpgrade: websocket\r\n"
                f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
                f"Sec-WebSocket-Version: 13\r\nUser-Agent: QuakeListener/{__version__}\r\n\r\n"
            ).encode())
            lines = self._read_head().split("\r\n")
            status = lines[0].split()
            if len(status) < 2 or status[1] != "101":
                raise ConnectionError(f"WebSocket upgrade refused: {lines[0][:80]}")
            expected = base64.b64encode(hashlib.sha1((key + _WS_GUID).encode()).digest()).decode()
            accept = next((l.split(":", 1)[1].strip() for l in lines[1:]
                           if l.lower().startswith("sec-websocket-accept:")), None)
            if accept != expected:
                raise ConnectionError("WebSocket handshake failed (bad Sec-WebSocket-Accept)")
            self.sock.settimeout(1.0)
        except BaseException:
            self.close()
            raise

    def _fill(self):
        try:
            chunk = self.sock.recv(65536)
        except (socket.timeout, ssl.SSLWantReadError):
            return False
        if not chunk:
            raise ConnectionResetError("WebSocket closed by remote peer.")
        self._buf.extend(chunk)
        self.last_rx = time.monotonic()
        return True

    def _read_head(self):
        while b"\r\n\r\n" not in self._buf:
            if len(self._buf) > 16384:
                raise ConnectionError("WebSocket handshake response too large")
            if not self._fill():
                raise socket.timeout("Timed out waiting for WebSocket handshake")
        end = self._buf.index(b"\r\n\r\n")
        head = bytes(self._buf[:end]).decode("latin1")
        del self._buf[:end + 4]
        return head

    def _parse_frame(self):
        """Pop one complete frame as (fin, opcode, payload), or None if incomplete."""
        b = self._buf
        if len(b) < 2:
            return None
        fin, op = bool(b[0] & 0x80), b[0] & 0x0F
        masked, n = b[1] & 0x80, b[1] & 0x7F
        pos = 2
        if n == 126:
            if len(b) < 4:
                return None
            n, pos = struct.unpack(">H", bytes(b[2:4]))[0], 4
        elif n == 127:
            if len(b) < 10:
                return None
            n, pos = struct.unpack(">Q", bytes(b[2:10]))[0], 10
        if n > MAX_PACKET_SIZE:
            raise ConnectionError(f"WebSocket frame too large ({n} bytes)")
        mask = None
        if masked:
            if len(b) < pos + 4:
                return None
            mask, pos = bytes(b[pos:pos + 4]), pos + 4
        if len(b) < pos + n:
            return None
        data = bytes(b[pos:pos + n])
        del b[:pos + n]
        if mask:
            data = bytes(x ^ mask[i % 4] for i, x in enumerate(data))
        return fin, op, data

    def _send(self, op, data=b""):
        mask = os.urandom(4)
        n = len(data)
        if n < 126:
            head = bytes([0x80 | op, 0x80 | n])
        elif n < 65536:
            head = bytes([0x80 | op, 0x80 | 126]) + struct.pack(">H", n)
        else:
            head = bytes([0x80 | op, 0x80 | 127]) + struct.pack(">Q", n)
        self.sock.sendall(head + mask + bytes(x ^ mask[i % 4] for i, x in enumerate(data)))

    def recv_message(self):
        """Next complete text/binary message as str, or None if nothing arrived for ~1 s."""
        while True:
            frame = self._parse_frame()
            if frame is None:
                if not self._fill():
                    return None
                continue
            fin, op, data = frame
            if op == 0x9:            # ping -> pong
                self._send(0xA, data)
                continue
            if op == 0xA:            # pong
                continue
            if op == 0x8:            # close
                code = struct.unpack(">H", data[:2])[0] if len(data) >= 2 else None
                raise ConnectionResetError(f"Server closed the WebSocket (code {code})")
            if op in (0x1, 0x2):
                self._frag = [data]
            elif op == 0x0 and self._frag:
                self._frag.append(data)
            else:
                continue
            if fin:
                msg, self._frag = b"".join(self._frag), []
                return msg.decode("utf-8", errors="replace")

    def ping(self):
        self._send(0x9, b"quake")

    def close(self):
        sock, self.sock = getattr(self, "sock", None), None
        if sock is None:
            return
        try:
            sock.settimeout(1.0)
            mask = os.urandom(4)
            sock.sendall(bytes([0x88, 0x82]) + mask + bytes(x ^ mask[i % 4] for i, x in enumerate(b"\x03\xe8")))
        except (OSError, ValueError):
            pass
        try:
            sock.close()
        except OSError:
            pass


def _parse_iso_utc(ts):
    """'2026-01-01T00:00:00.123Z' -> epoch seconds (Python 3.8-safe), or None."""
    if not ts:
        return None
    s = str(ts).strip()
    if s.endswith("Z"):
        s = s[:-1]
    s = s.split("+")[0]
    if "." in s:
        whole, frac = s.split(".", 1)
        s = f"{whole}.{(frac + '000000')[:6]}"
    try:
        d = dt.datetime.strptime(s, "%Y-%m-%dT%H:%M:%S.%f" if "." in s else "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return None
    return d.replace(tzinfo=dt.timezone.utc).timestamp()


def emsc_event_from_message(text):
    """Flatten one SeismicPortal message, or None if it is not a usable event."""
    try:
        msg = json.loads(text)
    except ValueError:
        return None
    if not isinstance(msg, dict):
        return None
    props = (msg.get("data") or {}).get("properties") or {}
    try:
        lat, lon, mag = float(props["lat"]), float(props["lon"]), float(props["mag"])
    except (KeyError, TypeError, ValueError):
        return None
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0) or math.isnan(mag):
        return None
    try:
        depth = round(float(props.get("depth")), 1)
    except (TypeError, ValueError):
        depth = None
    region = str(props.get("flynn_region") or "").strip().title() or None
    return {
        "action": str(msg.get("action") or "update"),
        "unid": str(props.get("unid") or f"{lat:.3f},{lon:.3f},{props.get('time')}"),
        "magnitude": round(mag, 1),
        "magtype": props.get("magtype"),
        "lat": lat,
        "lon": lon,
        "depth_km": depth,
        "region": region,
        "time": props.get("time"),
        "authority": props.get("auth"),
    }


def build_emsc_payload(ev, dist_km, age_s, base_name):
    """Webhook / REST payload for an EMSC event (same core fields as AEAS alerts)."""
    mag = ev["magnitude"]
    level = "alert" if mag >= 4.5 else "notice"
    region = ev["region"] or f"{ev['lat']:.2f}, {ev['lon']:.2f}"
    place = f"M{mag} {region}, {dist_km} km from {base_name}"
    now_str = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return {
        "level": level,
        "nivel": "alerta" if level == "alert" else "aviso",
        "source": "EMSC SeismicPortal",
        "id": f"emsc-{ev['unid']}",
        "magnitude": mag,
        "magnitud": mag,
        "magnitude_type": ev.get("magtype"),
        "distance_km": dist_km,
        "distancia_km": dist_km,
        "lat": ev["lat"],
        "lon": ev["lon"],
        "depth_km": ev.get("depth_km"),
        "radius_km": None,
        "region": ev["region"],
        "place": place,
        "lugar": place,
        "event_time": ev.get("time"),
        "report_delay_s": round(age_s) if age_s is not None else None,
        "timestamp": now_str,
        "hora_local": now_str,
        "status": "rapid report",
        "url": f"https://www.seismicportal.eu/eventdetails.html?unid={urllib.parse.quote(ev['unid'])}",
    }


class EMSCFeed(threading.Thread):
    """Follows the EMSC WebSocket and reports events near the base station."""

    def __init__(self, args, on_detection):
        super().__init__(name="emsc-feed", daemon=True)
        self.args = args
        self.on_detection = on_detection
        self.dispatched = collections.OrderedDict()  # unid -> magnitude already reported

    def handle(self, text):
        ev = emsc_event_from_message(text)
        if ev is None:
            return
        a = self.args
        with STATE_LOCK:
            STATE["emsc"]["events_received"] += 1
            STATE["emsc"]["last_event_ts"] = time.time()
        dist = round(haversine_distance(a.lat, a.lon, ev["lat"], ev["lon"]), 1)
        t = _parse_iso_utc(ev["time"])
        age = time.time() - t if t is not None else None
        near = dist <= a.emsc_radius_km
        if near or ev["magnitude"] >= 5.0:
            where = ev["region"] or f"{ev['lat']:.2f}, {ev['lon']:.2f}"
            log(f"EMSC {ev['action']}: M{ev['magnitude']} {where}, depth {ev['depth_km']} km, "
                f"{dist:.0f} km from {a.name}")
        if not near or ev["magnitude"] < a.emsc_min_mag:
            return
        if age is not None and age > EMSC_MAX_EVENT_AGE_S:
            log(f"EMSC: update of an event from {int(age // 60)} min ago, not dispatched")
            return
        prev = self.dispatched.get(ev["unid"])
        if prev is not None and ev["magnitude"] < prev + 0.5:
            return  # already reported; only re-report a big magnitude revision
        self.dispatched[ev["unid"]] = ev["magnitude"]
        while len(self.dispatched) > 500:
            self.dispatched.popitem(last=False)
        self.on_detection(build_emsc_payload(ev, dist, age, a.name))

    def run(self):
        backoff = BACKOFF_MIN_S
        while not STOP_EVENT.is_set():
            ws, started, reason = None, None, "connection closed"
            try:
                ws = MiniWebSocket(EMSC_HOST, EMSC_PATH)
                started = time.monotonic()
                _update_emsc_state(connected=True, connected_since=time.time())
                log(f"Connected to EMSC real-time feed (wss://{EMSC_HOST}{EMSC_PATH})")
                last_ping = time.monotonic()
                while not STOP_EVENT.is_set():
                    text = ws.recv_message()
                    if text is not None:
                        try:
                            self.handle(text)
                        except Exception as e:  # one bad event must not drop the feed
                            log(f"EMSC: skipped an event that could not be processed: {e}")
                    now = time.monotonic()
                    if now - last_ping >= EMSC_PING_EVERY_S:
                        ws.ping()
                        last_ping = now
                    if now - ws.last_rx > EMSC_SILENCE_LIMIT_S:
                        raise ConnectionError(f"EMSC feed silent for {EMSC_SILENCE_LIMIT_S} s")
            except Exception as e:
                reason = str(e) or e.__class__.__name__
                with STATE_LOCK:
                    STATE["emsc"]["errors"] += 1
                    STATE["emsc"]["last_error"] = reason
            finally:
                if ws is not None:
                    ws.close()
                _update_emsc_state(connected=False)
            if STOP_EVENT.is_set():
                break
            if started is not None and time.monotonic() - started >= STABLE_SESSION_S:
                backoff = BACKOFF_MIN_S
            delay = round(backoff + random.uniform(0, backoff * 0.25), 1)
            log(f"EMSC feed: {reason}. Reconnecting in {delay}s...")
            with STATE_LOCK:
                STATE["emsc"]["reconnects"] += 1
            STOP_EVENT.wait(delay)
            backoff = min(backoff * 2, BACKOFF_MAX_S)


def _is_loopback_origin(origin):
    try:
        parts = urllib.parse.urlsplit(origin)
    except ValueError:
        return False
    return parts.scheme in ("http", "https") and parts.hostname in ("127.0.0.1", "localhost", "::1")


def is_origin_allowed(origin):
    if not origin:
        return True  # non-browser client (curl, Home Assistant)
    if "*" in ALLOWED_ORIGINS:
        return True
    return origin.rstrip("/") in ALLOWED_ORIGINS or _is_loopback_origin(origin)


def _builtin_hosts():
    names = {"localhost", "host.docker.internal"}
    try:
        h = socket.gethostname().strip().lower().rstrip(".")
        if h:
            short = h.split(".")[0]
            names.update({h, short, short + ".local"})
    except OSError:
        pass
    return names


_BUILTIN_HOSTS = _builtin_hosts()


def is_host_allowed(host_header):
    """Reject requests addressed to a foreign domain name.

    A DNS-rebinding page (attacker.example resolving to 127.0.0.1) talks to the
    bridge same-origin, so it sends no Origin header on GETs; but its Host header
    still carries the attacker's domain. Real clients use an IP, localhost or
    this machine's name.
    """
    if not host_header or "*" in ALLOWED_HOSTS:
        return True  # HTTP/1.0 clients may omit Host
    try:
        hostname = urllib.parse.urlsplit("//" + host_header.strip()).hostname
    except ValueError:
        return False
    if not hostname:
        return False
    hostname = hostname.lower().rstrip(".")
    try:
        ipaddress.ip_address(hostname)
        return True
    except ValueError:
        pass
    return hostname in _BUILTIN_HOSTS or hostname.endswith(".localhost") or hostname in ALLOWED_HOSTS


def _script_dir():
    path = globals().get("__file__")
    if path and os.path.isfile(path):
        return os.path.dirname(os.path.abspath(path))
    return None  # e.g. `curl ... | python3 -`


class QuakeHTTPHandler(http.server.BaseHTTPRequestHandler):
    server_version = f"QuakeListener/{__version__}"
    timeout = 15  # drop clients that stall mid-request

    def log_message(self, format, *args):
        # Silence default access logging to avoid terminal spam
        pass

    def send_cors_headers(self):
        origin = self.headers.get("Origin")
        if "*" in ALLOWED_ORIGINS:
            self.send_header("Access-Control-Allow-Origin", "*")
        elif origin and is_origin_allowed(origin):
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Requested-With, Origin, Accept, X-Quake-Signature, X-Sismo-Firma")
        self.send_header("Access-Control-Allow-Private-Network", "true")
        self.send_header("Access-Control-Max-Age", "600")

    def _send_json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_cors_headers()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _discard_body(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if 0 < length <= 64 * 1024:
            self.rfile.read(length)

    def _host_ok(self):
        if is_host_allowed(self.headers.get("Host")):
            return True
        self._send_json(403, {"ok": False, "error": "host not allowed"})
        return False

    def _origin_ok(self):
        if is_origin_allowed(self.headers.get("Origin")):
            return True
        self._send_json(403, {"ok": False, "error": "origin not allowed"})
        return False

    def _clean_path(self):
        path = urllib.parse.urlsplit(self.path).path or "/"
        if len(path) > 1:
            path = path.rstrip("/")
        return path

    def _handle_ping(self):
        client = GLOBAL_CLIENT
        lat, error = None, None
        if client is None or not client.connected:
            error = "not connected to MCS"
        else:
            lat = client.request_ping(timeout=5.0)
            if lat is None:
                error = "no heartbeat ack within 5 s"
        self._send_json(200, {"ok": True, "acked": lat is not None, "latency_ms": lat, "error": error})

    def do_OPTIONS(self):
        if not self._host_ok():
            return
        self.send_response(204)
        self.send_cors_headers()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        if not self._host_ok():
            return
        clean_path = self._clean_path()
        if clean_path in ("/", "/index.html"):
            # If docs/index.html exists locally, serve the full dashboard
            candidates = [os.path.join(os.getcwd(), "docs", "index.html")]
            script_dir = _script_dir()
            if script_dir:
                candidates.insert(0, os.path.join(script_dir, "docs", "index.html"))
            for c in candidates:
                if os.path.isfile(c):
                    try:
                        with open(c, "rb") as f:
                            html_bytes = f.read()
                        self.send_response(200)
                        self.send_cors_headers()
                        self.send_header("Content-Type", "text/html; charset=utf-8")
                        self.send_header("Content-Length", str(len(html_bytes)))
                        self.end_headers()
                        self.wfile.write(html_bytes)
                        return
                    except OSError:
                        pass

        if clean_path in ("/", "/status", "/api/status"):
            self._send_json(200, status_snapshot())
        elif clean_path == "/ping":
            if self._origin_ok():
                self._handle_ping()
        else:
            self._send_json(404, {"ok": False, "error": "not found"})

    def do_POST(self):
        self._discard_body()
        if not self._host_ok():
            log(f"Rejected {self.command} for foreign Host {self.headers.get('Host')!r}")
            return
        clean_path = self._clean_path()
        if clean_path not in ("/drill", "/simulacro", "/ping"):
            self._send_json(404, {"ok": False, "error": "not found"})
            return
        if not self._origin_ok():
            log(f"Rejected POST {clean_path} from disallowed origin {self.headers.get('Origin')!r}")
            return

        if clean_path in ("/drill", "/simulacro"):
            log("📣 Safety drill triggered via HTTP REST API")
            with STATE_LOCK:
                name = STATE["location"]["name"]
            now = dt.datetime.now()
            drill_payload = {
                "level": "drill",
                "nivel": "simulacro",
                "source": "Safety Drill Simulation",
                "id": _new_event_id("drill"),
                "magnitude": 5.0,
                "magnitud": 5.0,
                "place": f"Safety Drill Simulation at {name}",
                "lugar": f"Simulacro de Seguridad en {name}",
                "timestamp": now.strftime("%Y-%m-%d %H:%M:%S"),
                "hora_local": now.strftime("%H:%M:%S")
            }
            # Synchronous (single attempt, short timeout) so the caller gets the status.
            status = dispatch_webhook(drill_payload, attempts=1, timeout=6)
            self._send_json(200, {
                "ok": True,
                "message": "Safety drill dispatched",
                "webhook_status": status
            })
        else:
            self._handle_ping()


class QuakeHTTPServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    # On Windows SO_REUSEADDR lets a second process bind the same port silently.
    allow_reuse_address = os.name != "nt"

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionError, socket.timeout)):
            return  # client went away (e.g. browser aborted a fetch); not worth a traceback
        log(f"HTTP handler error for {client_address[0]}: {exc!r}")


def start_http_server(host, port):
    server_cls = QuakeHTTPServer
    if ":" in host:
        server_cls = type("QuakeHTTPServer6", (QuakeHTTPServer,), {"address_family": socket.AF_INET6})
    server = server_cls((host, port), QuakeHTTPHandler)
    t = threading.Thread(target=server.serve_forever, name="http", daemon=True)
    t.start()
    return server


def _running_in_container():
    return os.path.exists("/.dockerenv") or os.path.exists("/run/.containerenv")


def default_http_host():
    env = os.environ.get("QUAKE_HTTP_HOST")
    if env:
        return env
    # Inside a container the API must listen on all interfaces for `-p 8990:8990` to work;
    # on a normal host it stays private to this machine by default.
    return "0.0.0.0" if _running_in_container() else "127.0.0.1"


# ==============================================================================
# Main Listener Loop & Event Dispatcher
# ==============================================================================

def _install_signal_handlers():
    def on_term(signum, frame):
        STOP_EVENT.set()
        raise _ShutdownRequested()

    # SIGINT already raises KeyboardInterrupt; SIGTERM (systemd, `docker stop`) gets the same
    # graceful path. SIGHUP is left alone so `nohup` keeps working.
    sig = getattr(signal, "SIGTERM", None)
    if sig is not None:
        try:
            signal.signal(sig, on_term)
        except (ValueError, OSError):
            pass  # not in main thread / unsupported on this platform


def describe_frame(tag, payload):
    """One-line summary of an MCS frame for --debug-frames (no payload bodies)."""
    name = TAG_NAMES.get(tag, f"tag{tag}")
    parts = [f"{name} {len(payload)}B"]
    try:
        f = parse_protobuf(payload)
        if tag == TAG_DATA_MESSAGE_STANZA:
            # DataMessageStanza: 3 from, 4 to, 5 category, 7 app_data{1 key, 2 value}, 9 persistent_id
            for label, num in (("from", 3), ("category", 5), ("pid", 9)):
                v = _pb_text(f, num, default=None, encoding="latin1")
                if v:
                    parts.append(f"{label}={v[:60]}")
            keys = [_pb_text(parse_protobuf(b), 1, default="?") for w, b in f.get(7, []) if w == 2]
            if keys:
                parts.append(f"app_data={keys[:12]}")
            raw = _pb_bytes(f, 21)
            if raw:
                parts.append(f"raw_data={len(raw)}B")
        elif tag == TAG_IQ_STANZA:
            # IqStanza: 2 type, 3 id, 7 extension{1 id, 2 data}
            iq_type = _pb_first(f, 2)[1] if 2 in f else None
            ext = _pb_bytes(f, 7)
            ext_id = _pb_first(parse_protobuf(ext), 1)[1] if ext else None
            parts.append(f"type={iq_type} extension={ext_id}")
        elif tag == TAG_CLOSE:
            parts.append("server close")
    except Exception as e:  # diagnostics must never break the session
        parts.append(f"(unparsed: {e})")
    return " ".join(parts)


def _handle_data_message(client, payload, dispatch_alert):
    client.messages_received += 1
    stanza = parse_protobuf(payload)
    cat = _pb_text(stanza, 5, default="", encoding="latin1")
    pid = _pb_text(stanza, 9, default="")
    raw = _pb_bytes(stanza, 21)
    if not client.register_persistent_id(pid):
        log(f"Ignoring re-delivered message {pid}")
        return
    if raw and cat == "com.google.android.gms":
        events = decode_earthquake_payload(raw)
        if not events:
            log(f"Data message from {cat} contained no decodable earthquake events ({len(raw)} bytes)")
        for ev in events:
            dispatch_alert(ev)


def _run_session(client, dispatch_alert):
    """Process packets until the server closes the stream or shutdown is requested."""
    stale_after = client.ping_interval + 60
    while not STOP_EVENT.is_set():
        frame = client.read_packet()
        if frame is not None:
            tag, payload = frame
            client.last_packet_ts = time.time()
            if DEBUG_FRAMES and tag not in (TAG_HEARTBEAT_PING, TAG_HEARTBEAT_ACK):
                log(f"[frame] {describe_frame(tag, payload)}")

            if tag == TAG_HEARTBEAT_PING:
                client.send_pong()
                client.pings_received += 1
            elif tag == TAG_HEARTBEAT_ACK:
                client.pings_received += 1
                client.on_heartbeat_ack()
            elif tag == TAG_IQ_STANZA:
                client.pings_received += 1
            elif tag == TAG_DATA_MESSAGE_STANZA:
                _handle_data_message(client, payload, dispatch_alert)
            elif tag == TAG_CLOSE:
                _sync_client_counters(client)
                return "Server sent Close command"
            _sync_client_counters(client)

        if client.service_ping():
            _sync_client_counters(client)

        idle = client.seconds_since_rx()
        if idle > stale_after:
            raise ConnectionError(f"No data from server for {int(idle)} s; connection presumed dead")
    return None


def run_listener(args):
    global GLOBAL_CLIENT, GLOBAL_ARGS
    GLOBAL_ARGS = args

    with STATE_LOCK:
        STATE["location"]["name"] = args.name
        STATE["location"]["lat"] = args.lat
        STATE["location"]["lon"] = args.lon

    _install_signal_handlers()

    httpd = None
    if not args.no_http:
        try:
            httpd = start_http_server(args.http_host, args.http_port)
            shown_host = "127.0.0.1" if args.http_host in ("0.0.0.0", "::") else args.http_host
            if ":" in shown_host:
                shown_host = "[%s]" % shown_host
            log(f"HTTP REST telemetry server active on http://{shown_host}:{args.http_port}/status "
                f"(bound to {args.http_host})")
            if args.http_host not in ("127.0.0.1", "localhost", "::1"):
                log("Note: the REST API is reachable from other machines on this network "
                    "(use --http-host 127.0.0.1 to keep it local).")
        except OSError as e:
            log(f"Warning: Could not start HTTP server on {args.http_host}:{args.http_port}: {e}")

    def record_detection(payload):
        with STATE_LOCK:
            STATE["last_quake"] = payload
            STATE["ultimo_sismo"] = payload
            STATE["total_detections"] += 1
        log(f"🚨 EARTHQUAKE ALERT DETECTED! {payload['place']} "
            f"(Level: {payload['level']}, source: {payload['source']})")
        if args.webhook_url:
            dispatch_webhook_async(payload)

    def dispatch_alert(ev):
        record_detection(build_alert_payload(ev, args.name, args.lat, args.lon))

    log(f"Starting Quake MCS Listener v{__version__} at {args.name} ({args.lat}, {args.lon})")
    if not 30 <= args.ping_interval <= 600:
        log(f"Ping interval {args.ping_interval}s is outside 30-600s and will be clamped.")
    log(f"Ping interval: {max(30, min(args.ping_interval, 600))}s | "
        f"Webhook: {redact_url(args.webhook_url) if args.webhook_url else 'Disabled'}"
        f"{' (HMAC signed)' if args.webhook_url and args.webhook_secret else ''}")

    if not args.no_emsc:
        _update_emsc_state(enabled=True)
        log(f"EMSC source on: events of M{args.emsc_min_mag}+ within {args.emsc_radius_km:g} km "
            f"trigger the webhook (rapid reports, not early warnings)")
        if args.lat == 0.0 and args.lon == 0.0:
            log("Note: base station is at 0.0, 0.0. Set --lat/--lon so nearby EMSC events count.")
        EMSCFeed(args, record_detection).start()

    client = None
    backoff = BACKOFF_MIN_S
    try:
        while not STOP_EVENT.is_set():
            session_start = None
            reason = None
            try:
                if client is None:
                    creds = get_credentials(args.locale, args.timezone)
                    client = QuakeMCSClient(creds, ping_interval=args.ping_interval)
                    GLOBAL_CLIENT = client
                    _update_mcs_state(android_id=str(client.android_id))
                client.connect()
                session_start = time.monotonic()
                _set_connected(True)
                _sync_client_counters(client)
                reason = _run_session(client, dispatch_alert)
            except Exception as e:
                reason = str(e) or e.__class__.__name__
                with STATE_LOCK:
                    STATE["google_mcs"]["errors"] += 1
                    STATE["google_mcs"]["last_error"] = reason
            finally:
                if client is not None:
                    client.close()
                _set_connected(False)

            if STOP_EVENT.is_set():
                break
            if session_start is not None and time.monotonic() - session_start >= STABLE_SESSION_S:
                backoff = BACKOFF_MIN_S
            delay = round(backoff + random.uniform(0, backoff * 0.25), 1)
            log(f"{reason or 'Connection closed'}. Reconnecting in {delay}s...")
            with STATE_LOCK:
                STATE["google_mcs"]["reconnects"] += 1
            STOP_EVENT.wait(delay)
            backoff = min(backoff * 2, BACKOFF_MAX_S)
    except (KeyboardInterrupt, _ShutdownRequested):
        pass
    finally:
        STOP_EVENT.set()
        log("Listener stopping...")
        if client is not None:
            client.close()
        _set_connected(False)
        if httpd is not None:
            httpd.shutdown()
            httpd.server_close()
        wait_for_webhooks(5.0)
        log("Listener stopped.")


def simulate_alert(args=None):
    log("Running synthetic event decoding test...")
    # Synthetic epicenter close to the default base station (0.0, 0.0); not a real place.
    jhom = field_double(1, 0.5) + field_double(2, 0.5)
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
    if not events:
        log("Synthetic test FAILED: no events decoded.")
        return 1
    ev = events[0]
    ok = (ev["magnitude"] == 5.4 and ev["region"] == "Test Region" and ev["lat"] == 0.5
          and ev["lon"] == 0.5 and ev["radius_km"] == 90.0)
    log(f"Synthetic test {'successful' if ok else 'FAILED'}: Magnitude={ev['magnitude']}, "
        f"Region={ev['region']}, Epicenter=({ev['lat']}, {ev['lon']}), Radius={ev['radius_km']} km")
    print(json.dumps(ev, indent=2))
    if args is not None:
        preview = build_alert_payload(ev, args.name, args.lat, args.lon)
        log("Webhook payload that would be sent (not dispatched):")
        print(json.dumps(preview, indent=2, ensure_ascii=False))
    return 0 if ok else 1


def test_ping(args=None):
    log(f"Testing TLS connection to {HOST_MCS}:{PORT_MCS}...")
    locale = args.locale if args else "en_US"
    tz = args.timezone if args else "UTC"
    client = None
    try:
        creds = get_credentials(locale, tz)
        client = QuakeMCSClient(creds)
        client.connect()
        client.send_ping()
        rtt = None
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and rtt is None:
            frame = client.read_packet()
            if frame is not None and frame[0] == TAG_HEARTBEAT_ACK:
                rtt = client.on_heartbeat_ack()
        if rtt is None:
            log(f"Connected (handshake {client.handshake_latency_ms} ms) but no heartbeat ack within 5 s.")
            return 1
        log(f"Test completed! Handshake latency: {client.handshake_latency_ms} ms, "
            f"heartbeat round-trip: {rtt} ms. Connection verified.")
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception as e:
        log(f"Test FAILED: {e}")
        return 1
    finally:
        if client is not None:
            client.close()


# ------------------------------------------------------------------- CLI types

def _float_range(lo, hi, what):
    def conv(s):
        try:
            v = float(s)
        except (TypeError, ValueError):
            raise argparse.ArgumentTypeError(f"invalid {what}: {s!r}")
        if math.isnan(v) or not lo <= v <= hi:
            raise argparse.ArgumentTypeError(f"{what} must be between {lo} and {hi}, got {s}")
        return v
    return conv


def _int_range(lo, hi, what):
    def conv(s):
        try:
            v = int(s)
        except (TypeError, ValueError):
            raise argparse.ArgumentTypeError(f"invalid {what}: {s!r}")
        if not lo <= v <= hi:
            raise argparse.ArgumentTypeError(f"{what} must be between {lo} and {hi}, got {s}")
        return v
    return conv


def _webhook_url(s):
    s = (s or "").strip()
    if not s:
        return ""
    parts = urllib.parse.urlsplit(s)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise argparse.ArgumentTypeError("webhook URL must be an http:// or https:// URL")
    return s


def _origins(s):
    items = [o.strip().rstrip("/") for o in (s or "").split(",") if o.strip()]
    for o in items:
        if o != "*" and urllib.parse.urlsplit(o).scheme not in ("http", "https"):
            raise argparse.ArgumentTypeError(f"invalid origin {o!r} (expected e.g. https://example.org or *)")
    return set(items)


def _hosts(s):
    return {h.strip().lower().rstrip(".") for h in (s or "").split(",") if h.strip()}


def build_parser():
    env = os.environ.get
    parser = argparse.ArgumentParser(
        prog="quake_listener.py",
        description="Quake MCS Listener - Lightweight Android Earthquake Alerts System (AEAS) Client"
    )
    # String defaults are converted (and validated) by `type`, so bad env values give a clean error.
    parser.add_argument("--lat", type=_float_range(-90.0, 90.0, "latitude"), default=env("QUAKE_LAT", "0.0"), help="Base monitoring latitude")
    parser.add_argument("--lon", type=_float_range(-180.0, 180.0, "longitude"), default=env("QUAKE_LON", "0.0"), help="Base monitoring longitude")
    parser.add_argument("--name", type=str, default=env("QUAKE_NAME", "Base Station"), help="Human-readable location name")
    parser.add_argument("--ping-interval", type=_int_range(1, 86400, "ping interval"), default=env("QUAKE_PING_INTERVAL", "120"), help="Keepalive ping interval in seconds (clamped to 30-600)")
    parser.add_argument("--http-port", type=_int_range(1, 65535, "port"), default=env("QUAKE_HTTP_PORT", "8990"), help="Local HTTP REST server port for Home Assistant (default: 8990)")
    parser.add_argument("--http-host", type=str, default=default_http_host(), help="Interface for the REST server (default: 127.0.0.1; 0.0.0.0 inside containers). Use 0.0.0.0 to expose it to your LAN / Docker networks")
    parser.add_argument("--allowed-origins", type=_origins, default=env("QUAKE_ALLOWED_ORIGINS", DEFAULT_ALLOWED_ORIGINS), help="Comma-separated browser origins allowed to use the REST API, or * for any (loopback origins and non-browser clients are always allowed)")
    parser.add_argument("--allowed-hosts", type=_hosts, default=env("QUAKE_ALLOWED_HOSTS", ""), help="Extra comma-separated host names the REST API answers to, e.g. a DNS name for this machine (IPs, localhost and this machine's hostname always work; * disables the check)")
    parser.add_argument("--no-http", action="store_true", help="Disable the local HTTP REST telemetry server")
    parser.add_argument("--webhook-url", type=_webhook_url, default=env("QUAKE_WEBHOOK_URL", ""), help="Webhook destination URL (e.g. Home Assistant)")
    parser.add_argument("--webhook-secret", type=str, default=env("QUAKE_WEBHOOK_SECRET", ""), help="Optional HMAC-SHA256 secret for payload signing (prefer the env var: CLI args are visible in `ps`)")
    parser.add_argument("--credentials-file", type=str, default=env("QUAKE_CREDENTIALS_FILE", DEFAULT_CREDENTIALS_FILE), help="Where the anonymous device identity is stored (default: ~/.quake_device_credentials.json)")
    parser.add_argument("--locale", type=str, default=env("QUAKE_LOCALE", "en_US"), help="Locale for device registration")
    parser.add_argument("--timezone", type=str, default=env("QUAKE_TIMEZONE", "UTC"), help="Timezone for device registration")
    parser.add_argument("--no-emsc", action="store_true", default=env("QUAKE_NO_EMSC", "") not in ("", "0", "false"), help="Disable the EMSC SeismicPortal real-time feed (secondary, non-AEAS source)")
    parser.add_argument("--emsc-min-mag", type=_float_range(0.0, 10.0, "magnitude"), default=env("QUAKE_EMSC_MIN_MAG", "4.0"), help="Minimum magnitude for EMSC events to trigger the webhook (default: 4.0)")
    parser.add_argument("--emsc-radius-km", type=_float_range(1.0, 20040.0, "radius"), default=env("QUAKE_EMSC_RADIUS_KM", "300"), help="Only EMSC events within this distance of the base station trigger the webhook (default: 300)")
    parser.add_argument("--debug-frames", action="store_true", default=env("QUAKE_DEBUG_FRAMES", "") not in ("", "0", "false"), help="Log a one-line summary of every non-heartbeat MCS frame (for protocol research)")
    parser.add_argument("--test-ping", action="store_true", help="Perform a single diagnostic TLS ping and exit")
    parser.add_argument("--simulate", action="store_true", help="Test internal Protobuf event decoding")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def main():
    global CREDENTIALS_FILE, ALLOWED_ORIGINS, ALLOWED_HOSTS, DEBUG_FRAMES
    args = build_parser().parse_args()
    DEBUG_FRAMES = args.debug_frames
    CREDENTIALS_FILE = os.path.abspath(os.path.expanduser(args.credentials_file))
    ALLOWED_ORIGINS = args.allowed_origins
    ALLOWED_HOSTS = args.allowed_hosts

    if args.test_ping:
        return test_ping(args)
    if args.simulate:
        return simulate_alert(args)
    run_listener(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
