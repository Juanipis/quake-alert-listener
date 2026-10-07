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

__version__ = "2.0.0"

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
    origin_ts, alert_id = None, None
    meta = _pb_bytes(ev, 1)
    if meta is not None:
        meta_p = parse_protobuf(meta)
        alert_id = _pb_text(meta_p, 3)
        wire, ms = _pb_first(meta_p, 4)
        if wire == 0 and isinstance(ms, int) and 946684800000 <= ms <= 4102444800000:  # 2000..2100
            origin_ts = ms / 1000.0
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
        "origin_ts": origin_ts,
        "alert_id": alert_id,
    }


def aeas_event(ev):
    """Decoded AEAS event -> common event dict for the DetectionDesk."""
    return {
        "source": "Android AEAS (MCS)", "kind": "aeas",
        "event_id": ev.get("alert_id") or _new_event_id("aeas"), "revision": None,
        "origin_ts": ev.get("origin_ts"), "lat": ev.get("lat"), "lon": ev.get("lon"),
        "depth_km": None, "magnitude": ev.get("magnitude"), "magnitude_type": None,
        "region": ev.get("region"), "radius_km": ev.get("radius_km"), "url": None,
        "final": None, "cancelled": False, "training": False,
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
    "sources": {},
    "last_quake": None,
    "ultimo_sismo": None,
    "total_detections": 0
}


def _new_source_state():
    return {"enabled": False, "connected": False, "events_received": 0, "last_event_ts": None,
            "connected_since": None, "reconnects": 0, "errors": 0, "last_error": None}


STATE["sources"] = {k: _new_source_state() for k in ("emsc", "wolfx", "shake")}
STATE["emsc"] = STATE["sources"]["emsc"]   # v1.2 field name, kept for existing clients


def _update_source_state(key, **kwargs):
    with STATE_LOCK:
        STATE["sources"][key].update(kwargs)


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
            "sources": copy.deepcopy(STATE["sources"]),
            "thresholds": copy.deepcopy(STATE.get("thresholds")),
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
_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
WS_MAX_MESSAGE = 1024 * 1024    # whole (reassembled) message; EMSC events are ~1 KB


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
        self._frag_len = 0
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
        # Validate before buffering the payload, so a hostile peer can't make us hold much.
        if masked:
            raise ConnectionError("Server sent a masked WebSocket frame (RFC 6455 violation)")
        if op >= 0x8 and (n > 125 or not fin):
            raise ConnectionError("Invalid WebSocket control frame")
        if n > WS_MAX_MESSAGE:
            raise ConnectionError(f"WebSocket frame too large ({n} bytes)")
        if len(b) < pos + n:
            return None
        data = bytes(b[pos:pos + n])
        del b[:pos + n]
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
                self._frag, self._frag_len = [data], len(data)
            elif op == 0x0 and self._frag:
                self._frag.append(data)
                self._frag_len += len(data)
            else:
                continue
            if self._frag_len > WS_MAX_MESSAGE:
                raise ConnectionError(f"WebSocket message exceeds {WS_MAX_MESSAGE} bytes")
            if fin:
                msg, self._frag, self._frag_len = b"".join(self._frag), [], 0
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


def _parse_time(ts, utc_offset_h=0.0):
    """Timestamp string -> epoch seconds, or None (Python 3.8-safe).

    Accepts ISO 8601 ('2026-01-01T00:00:00.12Z') and the agency formats
    '2026/01/01 09:00:00' or '2026-01-01 08:00:00', which carry no zone and are
    interpreted with `utc_offset_h` (JMA uses +9, CENC +8).
    """
    if not ts:
        return None
    s = str(ts).strip()
    offset = utc_offset_h
    if "T" in s:
        offset = 0.0
        if s.endswith("Z"):
            s = s[:-1]
        elif len(s) > 6 and s[-6] in "+-" and s[-3] == ":":
            sign = 1 if s[-6] == "+" else -1
            offset = sign * (int(s[-5:-3]) + int(s[-2:]) / 60.0)
            s = s[:-6]
        s = s.replace("T", " ")
    s = s.replace("/", "-")
    if "." in s:
        whole, frac = s.split(".", 1)
        s = f"{whole}.{(frac + '000000')[:6]}"
    try:
        d = dt.datetime.strptime(s, "%Y-%m-%d %H:%M:%S.%f" if "." in s else "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    return d.replace(tzinfo=dt.timezone.utc).timestamp() - offset * 3600.0


def _parse_iso_utc(ts):
    return _parse_time(ts, 0.0)


def _iso_utc(epoch):
    if epoch is None:
        return None
    return dt.datetime.fromtimestamp(epoch, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ==============================================================================
# Local impact: estimated intensity at the base station and wave arrival times
# ==============================================================================

P_WAVE_KMS = 6.0          # typical crustal P-wave speed
S_WAVE_KMS = 3.5          # typical crustal S-wave speed (the damaging shaking)
DEFAULT_DEPTH_KM = 10.0   # when a source gives no depth
MAX_EVENT_AGE_S = 15 * 60  # ignore reports about quakes older than this

# Allen, Wald & Worden (2012), "Intensity attenuation for active crustal regions",
# J. Seismology 16:409-433. Hypocentral-distance form, the default IPE in USGS ShakeMap:
#   MMI = c0 + c1*M + c2*ln(sqrt(R^2 + Rm^2)) [+ c4*ln(R/50) for R > 50 km],
#   Rm = m1 + m2*exp(M - 5)
_AWW12 = {"c0": 2.085, "c1": 1.428, "c2": -1.402, "c4": 0.078, "m1": -0.209, "m2": 2.042}

MMI_NAMES = ["I · not felt", "II · weak", "III · weak", "IV · light", "V · moderate",
             "VI · strong", "VII · very strong", "VIII · severe", "IX · violent", "X+ · extreme"]


def estimate_mmi(magnitude, hypo_km):
    """Median Modified Mercalli Intensity expected at `hypo_km` (rough: no site effects)."""
    c = _AWW12
    r = max(float(hypo_km), 0.1)
    rm = c["m1"] + c["m2"] * math.exp(magnitude - 5.0)
    mmi = c["c0"] + c["c1"] * magnitude + c["c2"] * math.log(math.sqrt(r * r + rm * rm))
    if r > 50.0:
        mmi += c["c4"] * math.log(r / 50.0)
    return max(1.0, min(mmi, 12.0))


def mmi_name(mmi):
    # Round the 1-decimal value half-up, so "4.5" reads as V everywhere (not banker's rounding)
    level = int(math.floor(round(mmi, 1) + 0.5))
    return MMI_NAMES[max(1, min(level, 10)) - 1]


def assess_impact(ev, base_lat, base_lon, now=None):
    """Distance, estimated intensity and wave ETAs of event `ev` at the base station."""
    now = time.time() if now is None else now
    epi = haversine_distance(base_lat, base_lon, ev["lat"], ev["lon"])
    depth = ev.get("depth_km")
    depth = DEFAULT_DEPTH_KM if depth is None or depth < 0 else depth
    hypo = math.sqrt(epi * epi + depth * depth)
    mag = ev.get("magnitude")
    mmi = estimate_mmi(mag, hypo) if mag is not None else None
    origin = ev.get("origin_ts")
    p_at = origin + hypo / P_WAVE_KMS if origin is not None else None
    s_at = origin + hypo / S_WAVE_KMS if origin is not None else None
    return {
        "distance_km": round(epi, 1),
        "hypocentral_km": round(hypo, 1),
        "estimated_mmi": round(mmi, 1) if mmi is not None else None,
        "p_wave_arrival_ts": round(p_at, 1) if p_at is not None else None,
        "s_wave_arrival_ts": round(s_at, 1) if s_at is not None else None,
        "s_wave_eta_s": round(s_at - now, 1) if s_at is not None else None,
        "age_s": round(now - origin, 1) if origin is not None else None,
    }


# ==============================================================================
# Detection desk: one policy for every source (AEAS, EEW feeds, EMSC, on-site)
# ==============================================================================
#
# Each source turns what it receives into an "event" dict:
#   source, kind ('aeas' | 'eew' | 'report' | 'onsite'), event_id, revision,
#   origin_ts, lat, lon, depth_km, magnitude, magnitude_type, region, url,
#   final, cancelled, training, agency_intensity, radius_km
# The desk estimates the local impact, picks a level, merges duplicates of the same
# quake across sources, and only notifies again when things get worse.

KIND_STATUS = {"aeas": "early alert", "eew": "early warning", "report": "rapid report",
               "onsite": "on-site trigger"}
LEVEL_RANK = {None: 0, "notice": 1, "alert": 2}


class DetectionDesk:
    def __init__(self, args, record):
        self.args = args
        self.record = record          # callable(payload) -> stores + fires webhook
        self.lock = threading.Lock()
        self.sent = collections.OrderedDict()  # key -> dict(level, mmi, origin_ts, lat, lon)

    # -- policy ---------------------------------------------------------------
    def level_for(self, mmi, ev, impact=None):
        a = self.args
        floor = None
        if ev["kind"] == "aeas":
            radius = ev.get("radius_km")
            mag = ev.get("magnitude") or 0
            if impact and impact["distance_km"] is not None and radius and impact["distance_km"] <= radius and mag >= 4.5:
                floor = "notice"   # Google itself drew this place inside the impact zone
        if mmi is None:
            return floor
        if mmi >= a.alert_mmi:
            return "alert"
        if mmi >= a.notice_mmi:
            return "notice"
        return floor

    def _match(self, ev):
        """Key of an already-notified quake this event belongs to (same or other source)."""
        own = (ev["source"], ev["event_id"])
        if own in self.sent:
            return own
        o = ev.get("origin_ts")
        if o is None:
            return None
        for key, s in reversed(self.sent.items()):
            if s["origin_ts"] is None or s["lat"] is None or abs(s["origin_ts"] - o) > 90:
                continue
            if haversine_distance(s["lat"], s["lon"], ev["lat"], ev["lon"]) <= 150:
                return key
        return None

    # -- entry point ----------------------------------------------------------
    def submit(self, ev, now=None):
        """Evaluate one event. Returns the dispatched payload, or None."""
        a = self.args
        now = time.time() if now is None else now
        if ev.get("training"):
            log(f"{ev['source']}: training message for {ev.get('region') or 'unknown region'}, ignored")
            return None
        if ev["kind"] == "onsite":
            return self._submit_onsite(ev, now)

        mag = ev.get("magnitude")
        if ev.get("lat") is None or ev.get("lon") is None:
            return self._submit_unlocated(ev, mag)
        impact = assess_impact(ev, a.lat, a.lon, now)
        with self.lock:
            key = self._match(ev)
            prev = self.sent.get(key) if key else None

            if ev.get("cancelled"):
                if prev is None:
                    return None
                del self.sent[key]
                payload = self._payload(ev, impact, "cancel")
                log(f"{ev['source']}: warning for {ev.get('region') or 'event'} was CANCELLED")
                self.record(payload)
                return payload

            if impact["age_s"] is not None and impact["age_s"] > MAX_EVENT_AGE_S:
                return None
            if mag is not None and mag < a.min_magnitude:
                return None
            if impact["distance_km"] > a.max_distance_km:
                return None

            level = self.level_for(impact["estimated_mmi"], ev, impact)
            if level is None:
                return None
            if prev is not None:
                worse = (LEVEL_RANK[level] > LEVEL_RANK[prev["level"]] or
                         (impact["estimated_mmi"] or 0) >= (prev["mmi"] or 0) + 1.0)
                if not worse:
                    return None
            self.sent[key or (ev["source"], ev["event_id"])] = {
                "level": level, "mmi": impact["estimated_mmi"], "origin_ts": ev.get("origin_ts"),
                "lat": ev["lat"], "lon": ev["lon"],
            }
            while len(self.sent) > 500:
                self.sent.popitem(last=False)
            payload = self._payload(ev, impact, level)
        self.record(payload)
        return payload

    def _submit_unlocated(self, ev, mag):
        """An alert without coordinates (possible for AEAS): fall back to magnitude only."""
        if mag is None or mag < max(4.0, self.args.min_magnitude):
            return None
        level = "alert" if mag >= 5.0 else "notice"
        key = (ev["source"], ev["event_id"])
        with self.lock:
            if key in self.sent:
                return None
            self.sent[key] = {"level": level, "mmi": None, "origin_ts": None, "lat": None, "lon": None}
        impact = {"distance_km": None, "hypocentral_km": None, "estimated_mmi": None,
                  "p_wave_arrival_ts": None, "s_wave_arrival_ts": None, "s_wave_eta_s": None, "age_s": None}
        payload = self._payload(ev, impact, level)
        self.record(payload)
        return payload

    def _submit_onsite(self, ev, now):
        a = self.args
        level = "alert" if a.shake_alert_counts and ev.get("peak_counts", 0) >= a.shake_alert_counts else "notice"
        impact = {"distance_km": 0.0, "hypocentral_km": None, "estimated_mmi": None,
                  "p_wave_arrival_ts": round(now, 1), "s_wave_arrival_ts": None,
                  "s_wave_eta_s": None, "age_s": None}
        payload = self._payload(ev, impact, level)
        self.record(payload)
        return payload

    # -- payload --------------------------------------------------------------
    def _payload(self, ev, impact, level):
        a = self.args
        mag = ev.get("magnitude")
        mmi = impact["estimated_mmi"]
        region = ev.get("region") or (f"{ev['lat']:.2f}, {ev['lon']:.2f}" if ev.get("lat") is not None else "unknown location")
        if ev["kind"] == "onsite":
            place = f"On-site P-wave trigger at {a.name} ({ev.get('detail', 'STA/LTA')})"
        else:
            bits = [f"M{mag}" if mag is not None else "Earthquake", region]
            if impact["distance_km"] is not None:
                bits.append(f"{impact['distance_km']:g} km from {a.name}")
            if mmi is not None:
                bits.append(f"est. MMI {mmi_name(mmi).split(' · ')[0]}")
            eta = impact["s_wave_eta_s"]
            if eta is not None and eta > 0:
                bits.append(f"S-wave in {int(eta)} s")
            place = " · ".join(bits)
        nivel = {"alert": "alerta", "notice": "aviso", "cancel": "cancelado"}.get(level, level)
        now_str = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        status = "cancelled" if level == "cancel" else KIND_STATUS.get(ev["kind"], "report")
        return {
            "level": level,
            "nivel": nivel,
            "source": ev["source"],
            "kind": ev["kind"],
            "status": status,
            "id": f"{ev['kind']}-{ev['event_id']}",
            "event_id": ev["event_id"],
            "revision": ev.get("revision"),
            "final": ev.get("final"),
            "magnitude": mag,
            "magnitud": mag,
            "magnitude_type": ev.get("magnitude_type"),
            "lat": ev.get("lat"),
            "lon": ev.get("lon"),
            "depth_km": ev.get("depth_km"),
            "radius_km": ev.get("radius_km"),
            "region": ev.get("region"),
            "distance_km": impact["distance_km"],
            "distancia_km": impact["distance_km"],
            "hypocentral_km": impact["hypocentral_km"],
            "estimated_mmi": mmi,
            "mmi_label": mmi_name(mmi) if mmi is not None else None,
            "agency_intensity": ev.get("agency_intensity"),
            "origin_time": _iso_utc(ev.get("origin_ts")),
            "report_delay_s": impact["age_s"],
            "p_wave_arrival_ts": impact["p_wave_arrival_ts"],
            "s_wave_arrival_ts": impact["s_wave_arrival_ts"],
            "s_wave_eta_s": impact["s_wave_eta_s"],
            "place": place,
            "lugar": place,
            "url": ev.get("url"),
            "timestamp": now_str,
            "hora_local": now_str,
        }


def describe_event(ev, impact):
    """One log line for an incoming event (used by all network sources)."""
    mag = f"M{ev['magnitude']}" if ev.get("magnitude") is not None else "M?"
    where = ev.get("region") or f"{ev['lat']:.2f}, {ev['lon']:.2f}"
    depth = f", depth {ev['depth_km']:g} km" if ev.get("depth_km") is not None else ""
    mmi = f" · est. MMI {mmi_name(impact['estimated_mmi']).split(' · ')[0]}" if impact.get("estimated_mmi") else ""
    return f"{mag} {where}{depth}, {impact['distance_km']:.0f} km away{mmi}"


# ==============================================================================
# Push sources over WebSocket: EMSC (reports) and Wolfx (official EEW relays)
# ==============================================================================

SOURCE_PING_EVERY_S = 60
SOURCE_SILENCE_LIMIT_S = 180


class WebSocketSource(threading.Thread):
    """Follows one WebSocket feed with reconnect/backoff; subclasses parse messages."""
    key = "source"
    label = "Source"
    host = ""
    path = "/"

    def __init__(self, args, desk):
        super().__init__(name=f"{self.key}-feed", daemon=True)
        self.args = args
        self.desk = desk

    def events_from(self, text):
        raise NotImplementedError

    def handle(self, text):
        for ev in self.events_from(text) or ():
            with STATE_LOCK:
                st = STATE["sources"][self.key]
                st["events_received"] += 1
                st["last_event_ts"] = time.time()
            impact = assess_impact(ev, self.args.lat, self.args.lon)
            if self.worth_logging(ev, impact):
                log(f"{self.log_prefix(ev)}: {describe_event(ev, impact)}")
            self.desk.submit(ev)

    def worth_logging(self, ev, impact):
        return True

    def log_prefix(self, ev):
        return self.label

    def run(self):
        backoff = BACKOFF_MIN_S
        while not STOP_EVENT.is_set():
            ws, started, reason = None, None, "connection closed"
            try:
                ws = MiniWebSocket(self.host, self.path)
                started = time.monotonic()
                _update_source_state(self.key, connected=True, connected_since=time.time())
                log(f"Connected to {self.label} (wss://{self.host}{self.path})")
                last_ping = time.monotonic()
                while not STOP_EVENT.is_set():
                    text = ws.recv_message()
                    if text is not None:
                        try:
                            self.handle(text)
                        except Exception as e:  # one bad message must not drop the feed
                            log(f"{self.label}: skipped a message that could not be processed: {e}")
                    now = time.monotonic()
                    if now - last_ping >= SOURCE_PING_EVERY_S:
                        ws.ping()
                        last_ping = now
                    if now - ws.last_rx > SOURCE_SILENCE_LIMIT_S:
                        raise ConnectionError(f"feed silent for {SOURCE_SILENCE_LIMIT_S} s")
            except Exception as e:
                reason = str(e) or e.__class__.__name__
                with STATE_LOCK:
                    STATE["sources"][self.key]["errors"] += 1
                    STATE["sources"][self.key]["last_error"] = reason
            finally:
                if ws is not None:
                    ws.close()
                _update_source_state(self.key, connected=False)
            if STOP_EVENT.is_set():
                break
            if started is not None and time.monotonic() - started >= STABLE_SESSION_S:
                backoff = BACKOFF_MIN_S
            delay = round(backoff + random.uniform(0, backoff * 0.25), 1)
            log(f"{self.label}: {reason}. Reconnecting in {delay}s...")
            with STATE_LOCK:
                STATE["sources"][self.key]["reconnects"] += 1
            STOP_EVENT.wait(delay)
            backoff = min(backoff * 2, BACKOFF_MAX_S)


def emsc_event_from_message(text):
    """Flatten one SeismicPortal message into the common event dict, or None."""
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
    unid = str(props.get("unid") or f"{lat:.3f},{lon:.3f},{props.get('time')}")
    return {
        "source": "EMSC",
        "kind": "report",
        "action": str(msg.get("action") or "update"),
        "event_id": unid,
        "unid": unid,
        "revision": props.get("lastupdate"),
        "origin_ts": _parse_iso_utc(props.get("time")),
        "time": props.get("time"),
        "lat": lat,
        "lon": lon,
        "depth_km": depth,
        "magnitude": round(mag, 1),
        "magnitude_type": props.get("magtype"),
        "region": str(props.get("flynn_region") or "").strip().title() or None,
        "authority": props.get("auth"),
        "url": f"https://www.seismicportal.eu/eventdetails.html?unid={urllib.parse.quote(unid)}",
        "final": None,
        "cancelled": False,
        "training": False,
    }


class EMSCSource(WebSocketSource):
    key = "emsc"
    label = "EMSC"
    host = EMSC_HOST
    path = EMSC_PATH

    def events_from(self, text):
        ev = emsc_event_from_message(text)
        return [ev] if ev else []

    def worth_logging(self, ev, impact):
        # The feed is global; only log what could matter here or is big anywhere.
        return (impact["estimated_mmi"] or 0) >= 2.0 or (ev["magnitude"] or 0) >= 5.0

    def log_prefix(self, ev):
        return f"EMSC {ev['action']}"


# Wolfx relays official early warnings as JSON over one WebSocket (wolfx.jp, free for
# non-abusive use). Times are local to the issuing agency.
WOLFX_HOST = "ws-api.wolfx.jp"
WOLFX_PATH = "/all_eew"
WOLFX_FEEDS = {
    #  type        label                 agency UTC offset
    "jma_eew": ("JMA EEW", 9.0),        # Japan Meteorological Agency
    "cenc_eew": ("CENC EEW", 8.0),      # China Earthquake Networks Center
    "sc_eew": ("Sichuan EEW", 8.0),
    "fj_eew": ("Fujian EEW", 8.0),
    "cq_eew": ("Chongqing EEW", 8.0),
}


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) else f


def wolfx_event_from_message(text):
    """Flatten one Wolfx EEW message into the common event dict, or None."""
    try:
        m = json.loads(text)
    except ValueError:
        return None
    if not isinstance(m, dict) or m.get("type") not in WOLFX_FEEDS:
        return None  # heartbeats, earthquake lists, unknown feeds
    label, offset = WOLFX_FEEDS[m["type"]]
    lat, lon = _num(m.get("Latitude")), _num(m.get("Longitude"))
    mag = _num(m.get("Magnitude", m.get("Magunitude")))
    if lat is None or lon is None or not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    serial = m.get("Serial", m.get("ReportNum"))
    event_id = str(m.get("EventID") or m.get("ID") or f"{lat:.2f},{lon:.2f},{m.get('OriginTime')}")
    depth = _num(m.get("Depth"))
    return {
        "source": label,
        "kind": "eew",
        "event_id": f"{m['type']}:{event_id}",
        "revision": serial,
        "origin_ts": _parse_time(m.get("OriginTime"), offset),
        "lat": lat,
        "lon": lon,
        "depth_km": depth,
        "magnitude": round(mag, 1) if mag is not None else None,
        "magnitude_type": None,
        "region": m.get("Hypocenter") or m.get("HypoCenter"),
        "agency_intensity": str(m["MaxIntensity"]) if m.get("MaxIntensity") not in (None, "") else None,
        "url": None,
        "final": bool(m.get("isFinal")) if "isFinal" in m else None,
        "cancelled": bool(m.get("isCancel")),
        "training": bool(m.get("isTraining")),
    }


class WolfxSource(WebSocketSource):
    key = "wolfx"
    label = "Wolfx EEW"
    host = WOLFX_HOST
    path = WOLFX_PATH

    def events_from(self, text):
        ev = wolfx_event_from_message(text)
        return [ev] if ev else []

    def log_prefix(self, ev):
        rev = f" #{ev['revision']}" if ev.get("revision") is not None else ""
        final = " (final)" if ev.get("final") else ""
        return f"{ev['source']}{rev}{final}"


# ==============================================================================
# On-site detection: Raspberry Shake UDP datacast + STA/LTA P-wave trigger
# ==============================================================================
#
# A Raspberry Shake (or anything speaking its datacast format) streams packets like
#   {'EHZ', 1700000000.120, 17, -4, 12, ...}
# to a UDP port. A recursive STA/LTA on the vertical channel catches the P-wave on
# site, which is the only kind of early warning that works where no network issues
# public EEWs. It cannot tell a quake from a slammed door by itself: pick thresholds
# for your floor, and treat a lone trigger as "notice" unless it is strong.

SHAKE_STA_S = 1.0
SHAKE_LTA_S = 30.0
SHAKE_HOLDOFF_S = 30.0


def parse_shake_packet(data):
    """b"{'EHZ', 1700000000.12, 1, 2, 3}" -> ('EHZ', 1700000000.12, [1, 2, 3]) or None."""
    try:
        text = data.decode("ascii", errors="ignore").strip()
    except AttributeError:
        return None
    if not (text.startswith("{") and text.endswith("}")):
        return None
    parts = [p.strip() for p in text[1:-1].split(",")]
    if len(parts) < 3:
        return None
    channel = parts[0].strip("'\" ")
    try:
        t0 = float(parts[1])
        samples = [float(p) for p in parts[2:] if p]
    except ValueError:
        return None
    if not channel or not samples:
        return None
    return channel, t0, samples


class StaLtaDetector:
    """Recursive STA/LTA on a squared, de-meaned signal (Withers et al., 1998)."""

    def __init__(self, on_ratio, off_ratio, sta_s=SHAKE_STA_S, lta_s=SHAKE_LTA_S, holdoff_s=SHAKE_HOLDOFF_S):
        self.on, self.off = on_ratio, off_ratio
        self.sta_s, self.lta_s, self.holdoff_s = sta_s, lta_s, holdoff_s
        self.sta = self.lta = self.mean = 0.0
        self.n = 0
        self.rate = None
        self.triggered = False
        self.last_trigger_t = -1e18
        self.peak = 0.0
        self.max_ratio = 0.0

    def feed(self, t0, samples, rate):
        """Process one packet. Returns a trigger dict when a new trigger starts."""
        self.rate = rate
        a_sta = 1.0 / max(1.0, self.sta_s * rate)
        a_lta = 1.0 / max(1.0, self.lta_s * rate)
        a_mean = 1.0 / max(1.0, 60.0 * rate)
        warm = self.lta_s * rate
        fired = None
        for i, x in enumerate(samples):
            self.n += 1
            if self.n == 1:
                self.mean = x
            self.mean += (x - self.mean) * a_mean
            y = x - self.mean
            cf = y * y
            self.sta += (cf - self.sta) * a_sta
            self.lta += (cf - self.lta) * a_lta
            if self.n < warm or self.lta <= 0:
                continue
            ratio = self.sta / self.lta
            t = t0 + i / rate
            if self.triggered:
                self.peak = max(self.peak, abs(y))
                self.max_ratio = max(self.max_ratio, ratio)
                if ratio < self.off:
                    self.triggered = False
            elif ratio >= self.on and t - self.last_trigger_t >= self.holdoff_s:
                self.triggered = True
                self.last_trigger_t = t
                self.peak, self.max_ratio = abs(y), ratio
                fired = {"t": t, "ratio": round(ratio, 1)}
        return fired


class ShakeSource(threading.Thread):
    key = "shake"
    label = "Raspberry Shake"

    def __init__(self, args, desk):
        super().__init__(name="shake-udp", daemon=True)
        self.args = args
        self.desk = desk
        host, _, port = args.shake_udp.rpartition(":")
        self.bind = (host or "0.0.0.0", int(port))
        self.detector = StaLtaDetector(args.shake_sta_lta_on, args.shake_sta_lta_off)
        self.channel = args.shake_channel
        self.last_t = None
        self.pending = None   # trigger waiting to collect its peak amplitude

    def pick_channel(self, ch):
        if self.channel:
            return ch == self.channel
        # auto: first vertical channel seen (EHZ geophone, ENZ accelerometer, SHZ ...)
        if ch.endswith("Z"):
            self.channel = ch
            log(f"Raspberry Shake: using channel {ch}")
            return True
        return False

    def process(self, data, now=None):
        pkt = parse_shake_packet(data)
        if pkt is None:
            return None
        ch, t0, samples = pkt
        if not self.pick_channel(ch):
            return None
        with STATE_LOCK:
            st = STATE["sources"]["shake"]
            st["connected"] = True
            st["events_received"] += 1
            st["last_event_ts"] = time.time()
        rate = 100.0
        if self.last_t is not None and t0 > self.last_t:
            est = len(samples) / (t0 - self.last_t)
            if 10 <= est <= 1000:
                rate = est
        self.last_t = t0
        fired = self.detector.feed(t0, samples, rate)
        dispatched = None
        if self.pending and (not self.detector.triggered or t0 - self.pending["t"] > 3.0):
            dispatched = self._dispatch(self.pending)
            self.pending = None
        if fired:
            self.pending = fired
        return dispatched

    def _dispatch(self, trig):
        peak = round(self.detector.peak, 1)
        ratio = max(trig["ratio"], round(self.detector.max_ratio, 1))
        log(f"Raspberry Shake: P-wave trigger on {self.channel} (STA/LTA {ratio}, peak {peak:g} counts)")
        ev = {
            "source": "Raspberry Shake", "kind": "onsite",
            "event_id": f"{self.channel}-{int(trig['t'])}", "revision": None,
            "origin_ts": None, "lat": self.args.lat, "lon": self.args.lon, "depth_km": None,
            "magnitude": None, "magnitude_type": None, "region": None, "url": None,
            "final": None, "cancelled": False, "training": False,
            "peak_counts": peak, "detail": f"{self.channel}, STA/LTA {ratio}, peak {peak:g} counts",
        }
        return self.desk.submit(ev)

    def run(self):
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.bind(self.bind)
            sock.settimeout(1.0)
        except OSError as e:
            log(f"Raspberry Shake: cannot listen on UDP {self.bind[0]}:{self.bind[1]}: {e}")
            with STATE_LOCK:
                STATE["sources"]["shake"]["last_error"] = str(e)
            return
        log(f"Raspberry Shake: listening for datacast on UDP {self.bind[0]}:{self.bind[1]}")
        try:
            while not STOP_EVENT.is_set():
                try:
                    data, _ = sock.recvfrom(8192)
                except socket.timeout:
                    with STATE_LOCK:
                        last = STATE["sources"]["shake"]["last_event_ts"]
                        if last and time.time() - last > 30:
                            STATE["sources"]["shake"]["connected"] = False
                    continue
                try:
                    self.process(data)
                except Exception as e:
                    log(f"Raspberry Shake: skipped a packet: {e}")
        finally:
            sock.close()


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
        icon = {"alert": "🚨 ", "cancel": "✖ "}.get(payload["level"], "")
        log(f"{icon}EARTHQUAKE {payload['level'].upper()} ({payload['status']}, {payload['source']}): "
            f"{payload['place']}")
        if args.webhook_url:
            dispatch_webhook_async(payload)

    desk = DetectionDesk(args, record_detection)

    def dispatch_alert(ev):
        desk.submit(aeas_event(ev))

    log(f"Starting Quake MCS Listener v{__version__} at {args.name} ({args.lat}, {args.lon})")
    if not 30 <= args.ping_interval <= 600:
        log(f"Ping interval {args.ping_interval}s is outside 30-600s and will be clamped.")
    log(f"Ping interval: {max(30, min(args.ping_interval, 600))}s | "
        f"Webhook: {redact_url(args.webhook_url) if args.webhook_url else 'Disabled'}"
        f"{' (HMAC signed)' if args.webhook_url and args.webhook_secret else ''}")

    with STATE_LOCK:
        STATE["thresholds"] = {"notice_mmi": args.notice_mmi, "alert_mmi": args.alert_mmi,
                               "min_magnitude": args.min_magnitude, "max_distance_km": args.max_distance_km}
    log(f"Sources: {', '.join(sorted(args.sources))} | notify at est. MMI {args.notice_mmi:g}+, "
        f"alert at MMI {args.alert_mmi:g}+")
    if args.lat == 0.0 and args.lon == 0.0:
        log("Note: base station is at 0.0, 0.0. Set --lat/--lon so intensity and S-wave "
            "countdowns are computed for where you actually are.")
    for key, cls in (("emsc", EMSCSource), ("wolfx", WolfxSource)):
        if key in args.sources:
            _update_source_state(key, enabled=True)
            cls(args, desk).start()
    if args.shake_udp:
        _update_source_state("shake", enabled=True)
        ShakeSource(args, desk).start()

    client = None
    backoff = BACKOFF_MIN_S
    try:
        if "mcs" not in args.sources:
            log("Google MCS source disabled (--sources); following the other feeds only.")
            while not STOP_EVENT.is_set():
                STOP_EVENT.wait(1.0)
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
        preview = DetectionDesk(args, lambda p: None).submit(aeas_event(ev))
        if preview is None:
            log(f"At {args.name} this synthetic event stays below the notification thresholds "
                f"(--notice-mmi {args.notice_mmi:g}); nothing would be sent.")
        else:
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


def _sources(s):
    items = {x.strip().lower() for x in (s or "").split(",") if x.strip()}
    unknown = items - {"mcs", "emsc", "wolfx"}
    if unknown:
        raise argparse.ArgumentTypeError(f"unknown source(s): {', '.join(sorted(unknown))} (use mcs, emsc, wolfx)")
    return items


def _udp_endpoint(s):
    s = (s or "").strip()
    if not s:
        return ""
    host, _, port = s.rpartition(":")
    try:
        if not 1 <= int(port) <= 65535:
            raise ValueError
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid UDP endpoint {s!r} (expected [host:]port, e.g. 8888)")
    return f"{host}:{int(port)}" if host else f":{int(port)}"


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
    parser.add_argument("--sources", type=_sources, default=env("QUAKE_SOURCES", "mcs,emsc,wolfx"), help="Push sources to follow: mcs (Google AEAS, experimental), emsc (worldwide rapid reports), wolfx (official early warnings for Japan and China). Default: mcs,emsc,wolfx")
    parser.add_argument("--no-emsc", action="store_true", default=env("QUAKE_NO_EMSC", "") not in ("", "0", "false"), help="Shortcut to drop emsc from --sources")
    parser.add_argument("--notice-mmi", type=_float_range(1.0, 12.0, "intensity"), default=env("QUAKE_NOTICE_MMI", "3.0"), help="Notify (level 'notice') when the estimated intensity at your base station reaches this MMI (default: 3.0, like Android's 'Be Aware')")
    parser.add_argument("--alert-mmi", type=_float_range(1.0, 12.0, "intensity"), default=env("QUAKE_ALERT_MMI", "5.0"), help="Raise level 'alert' from this estimated MMI (default: 5.0, like Android's 'Take Action')")
    parser.add_argument("--min-magnitude", "--emsc-min-mag", dest="min_magnitude", type=_float_range(0.0, 10.0, "magnitude"), default=env("QUAKE_MIN_MAGNITUDE", env("QUAKE_EMSC_MIN_MAG", "0")), help="Ignore events below this magnitude, whatever their estimated intensity (default: 0)")
    parser.add_argument("--max-distance-km", "--emsc-radius-km", dest="max_distance_km", type=_float_range(1.0, 20040.0, "distance"), default=env("QUAKE_MAX_DISTANCE_KM", env("QUAKE_EMSC_RADIUS_KM", "20040")), help="Ignore events farther than this from the base station (default: no limit)")
    parser.add_argument("--shake-udp", type=_udp_endpoint, default=env("QUAKE_SHAKE_UDP", ""), help="Listen for a Raspberry Shake UDP datacast on [host:]port (e.g. 8888) and run an on-site P-wave trigger")
    parser.add_argument("--shake-channel", type=str, default=env("QUAKE_SHAKE_CHANNEL", ""), help="Shake channel to watch (default: first vertical channel, e.g. EHZ or ENZ)")
    parser.add_argument("--shake-sta-lta-on", type=_float_range(1.5, 100.0, "STA/LTA ratio"), default=env("QUAKE_SHAKE_STA_LTA_ON", "4.0"), help="STA/LTA ratio that starts an on-site trigger (default: 4.0)")
    parser.add_argument("--shake-sta-lta-off", type=_float_range(0.5, 50.0, "STA/LTA ratio"), default=env("QUAKE_SHAKE_STA_LTA_OFF", "1.5"), help="STA/LTA ratio that ends it (default: 1.5)")
    parser.add_argument("--shake-alert-counts", type=_float_range(0.0, 1e12, "counts"), default=env("QUAKE_SHAKE_ALERT_COUNTS", "0"), help="Peak amplitude (raw counts) that turns an on-site trigger into level 'alert' (default: 0 = always 'notice')")
    parser.add_argument("--debug-frames", action="store_true", default=env("QUAKE_DEBUG_FRAMES", "") not in ("", "0", "false"), help="Log a one-line summary of every non-heartbeat MCS frame (for protocol research)")
    parser.add_argument("--test-ping", action="store_true", help="Perform a single diagnostic TLS ping and exit")
    parser.add_argument("--simulate", action="store_true", help="Test internal Protobuf event decoding")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def main():
    global CREDENTIALS_FILE, ALLOWED_ORIGINS, ALLOWED_HOSTS, DEBUG_FRAMES
    args = build_parser().parse_args()
    DEBUG_FRAMES = args.debug_frames
    if args.no_emsc:
        args.sources.discard("emsc")
    if args.alert_mmi < args.notice_mmi:
        build_parser().error("--alert-mmi must be >= --notice-mmi")
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
