#!/usr/bin/env python3
"""radar_sismos_global.py - Autonomous Global Fleet of Google Seismic Radar Receivers.

Maintains a distributed fleet of 22 independent synthetic Google Pixel devices,
each assigned to a high-seismicity region in the world with its own android_id,
security_token, S2 Level 8 geographic cell, and 3 decoy cells, fully emulating
Google Play Services behavior.

Monitored regions:
  - Chile (Santiago, Antofagasta, Coquimbo)
  - Philippines (Davao, Manila)
  - Indonesia (Sumatra, Java, Sulawesi)
  - Mexico (Oaxaca, Guerrero, Chiapas)
  - Turkey (Istanbul, Eastern Anatolia)
  - Greece (Athens, Crete)
  - Peru (Lima, Arequipa)
  - Colombia (Bucaramanga/Los Santos Nest, Medellin)
  - California (Los Angeles / San Andreas Fault)
  - Taiwan (Hualien)
  - Panama (Gulf of Montijo / Chiriqui)

Usage:
  ./radar_sismos_global.py                    # Run continuous radar daemon
  ./radar_sismos_global.py --test-connection  # Test concurrent handshake for all nodes
  ./radar_sismos_global.py --generate-fleet   # Force regenerate synthetic fleet credentials
"""

import argparse
import datetime as dt
import http.server
import json
import math
import os
import random
import select
import socket
import ssl
import struct
import sys
import threading
import time
import urllib.request

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FLEET_FILE = os.path.join(BASE_DIR, "synthetic_fleet.json")
DETECCIONES_LOG = os.path.join(BASE_DIR, "sismos_globales_detectados.jsonl")

HOST = "mtalk.google.com"
PORT = 5228
MCS_VERSION = 41
DEFAULT_HTTP_PORT = 8998

TAG_HEARTBEAT_PING = 0
TAG_HEARTBEAT_ACK = 1
TAG_LOGIN_REQUEST = 2
TAG_LOGIN_RESPONSE = 3
TAG_CLOSE = 4
TAG_IQ_STANZA = 7
TAG_DATA_MESSAGE_STANZA = 8

CHECKIN_URL = "https://android.clients.google.com/checkin"

HOTSPOTS = [
    {"nombre": "Chile - Santiago", "pais": "Chile", "lat": -33.4489, "lon": -70.6693, "locale": "es_CL", "tz": "America/Santiago"},
    {"nombre": "Chile - Antofagasta", "pais": "Chile", "lat": -23.6509, "lon": -70.3975, "locale": "es_CL", "tz": "America/Santiago"},
    {"nombre": "Chile - Coquimbo", "pais": "Chile", "lat": -29.9533, "lon": -71.3436, "locale": "es_CL", "tz": "America/Santiago"},
    {"nombre": "Filipinas - Davao", "pais": "Filipinas", "lat": 7.1907, "lon": 125.4553, "locale": "en_PH", "tz": "Asia/Manila"},
    {"nombre": "Filipinas - Manila", "pais": "Filipinas", "lat": 14.5995, "lon": 120.9842, "locale": "en_PH", "tz": "Asia/Manila"},
    {"nombre": "Indonesia - Sumatra", "pais": "Indonesia", "lat": -0.9471, "lon": 100.4172, "locale": "id_ID", "tz": "Asia/Jakarta"},
    {"nombre": "Indonesia - Java", "pais": "Indonesia", "lat": -7.7956, "lon": 110.3695, "locale": "id_ID", "tz": "Asia/Jakarta"},
    {"nombre": "Indonesia - Sulawesi", "pais": "Indonesia", "lat": -0.9003, "lon": 119.8780, "locale": "id_ID", "tz": "Asia/Makassar"},
    {"nombre": "Mexico - Oaxaca", "pais": "Mexico", "lat": 17.0732, "lon": -96.7266, "locale": "es_MX", "tz": "America/Mexico_City"},
    {"nombre": "Mexico - Guerrero", "pais": "Mexico", "lat": 16.8531, "lon": -99.8237, "locale": "es_MX", "tz": "America/Mexico_City"},
    {"nombre": "Mexico - Chiapas", "pais": "Mexico", "lat": 16.7569, "lon": -93.1292, "locale": "es_MX", "tz": "America/Mexico_City"},
    {"nombre": "Turquia - Estambul", "pais": "Turquia", "lat": 40.9833, "lon": 28.8167, "locale": "tr_TR", "tz": "Europe/Istanbul"},
    {"nombre": "Turquia - Anatolia", "pais": "Turquia", "lat": 37.5858, "lon": 36.9371, "locale": "tr_TR", "tz": "Europe/Istanbul"},
    {"nombre": "Grecia - Atenas", "pais": "Grecia", "lat": 37.9838, "lon": 23.7275, "locale": "el_GR", "tz": "Europe/Athens"},
    {"nombre": "Grecia - Creta", "pais": "Grecia", "lat": 35.2401, "lon": 24.8093, "locale": "el_GR", "tz": "Europe/Athens"},
    {"nombre": "Peru - Lima", "pais": "Peru", "lat": -12.0464, "lon": -77.0428, "locale": "es_PE", "tz": "America/Lima"},
    {"nombre": "Peru - Arequipa", "pais": "Peru", "lat": -16.4090, "lon": -71.5375, "locale": "es_PE", "tz": "America/Lima"},
    {"nombre": "Colombia - Los Santos", "pais": "Colombia", "lat": 6.7766, "lon": -73.1256, "locale": "es_CO", "tz": "America/Bogota"},
    {"nombre": "Colombia - Medellin", "pais": "Colombia", "lat": 6.2442, "lon": -75.5812, "locale": "es_CO", "tz": "America/Bogota"},
    {"nombre": "California - Los Angeles", "pais": "USA", "lat": 34.0522, "lon": -118.2437, "locale": "en_US", "tz": "America/Los_Angeles"},
    {"nombre": "Taiwan - Hualien", "pais": "Taiwan", "lat": 23.9872, "lon": 121.6016, "locale": "zh_TW", "tz": "Asia/Taipei"},
    {"nombre": "Panama - Golfo de Montijo", "pais": "Panama", "lat": 7.575, "lon": -80.951, "locale": "es_PA", "tz": "America/Panama"},
]

ESTADO = {
    "modo": "flota_distribuida",
    "flota_tamano": len(HOTSPOTS),
    "nodos_conectados": 0,
    "porcentaje_conectado": "0%",
    "iniciado_desde": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    "ultimo_latido_global": None,
    "total_alertas_recibidas": 0,
    "ultima_alerta": None,
    "nodos": {}
}


def log(msg):
    sys.stdout.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] [FLOTA-GLOBAL] {msg}\n")
    sys.stdout.flush()


# ==============================================================================
# S2 Geometry Projection (Pure Python, Zero External Dependencies)
# ==============================================================================

_S2_LOOKUP_BITS = 4
_S2_SWAP_MASK = 1
_S2_INVERT_MASK = 2
_S2_LOOKUP_POS = [0] * 1024
_S2_POS_TO_IJ = [[0, 1, 3, 2], [0, 2, 3, 1], [3, 2, 0, 1], [3, 1, 0, 2]]
_S2_POS_TO_ORIENTATION = [_S2_SWAP_MASK, 0, 0, _S2_SWAP_MASK | _S2_INVERT_MASK]


def _init_s2_lookup(level, i, j, orig_orient, pos, orient):
    if level == _S2_LOOKUP_BITS:
        ij = (i << _S2_LOOKUP_BITS) + j
        _S2_LOOKUP_POS[(ij << 2) + orig_orient] = (pos << 2) + orient
    else:
        level += 1
        i <<= 1
        j <<= 1
        pos <<= 2
        r = _S2_POS_TO_IJ[orient]
        for idx in range(4):
            _init_s2_lookup(level, i + (r[idx] >> 1), j + (r[idx] & 1),
                            orig_orient, pos + idx, orient ^ _S2_POS_TO_ORIENTATION[idx])


for _m in (0, _S2_SWAP_MASK, _S2_INVERT_MASK, _S2_SWAP_MASK | _S2_INVERT_MASK):
    _init_s2_lookup(0, 0, 0, _m, 0, _m)


def lat_lon_to_s2_cell_token(lat, lon, level=8):
    lat = max(-90.0, min(90.0, float(lat)))
    lon = ((float(lon) + 180.0) % 360.0) - 180.0
    phi = math.radians(lat)
    theta = math.radians(lon)
    cos_phi = math.cos(phi)
    x = cos_phi * math.cos(theta)
    y = cos_phi * math.sin(theta)
    z = math.sin(phi)

    ax, ay, az = abs(x), abs(y), abs(z)
    if ax > ay:
        face = 0 if ax > az else 2
    else:
        face = 1 if ay > az else 2

    p = [x, y, z]
    if p[face] < 0:
        face += 3

    if face == 0: u, v = y / x, z / x
    elif face == 1: u, v = -x / y, z / y
    elif face == 2: u, v = -x / z, -y / z
    elif face == 3: u, v = z / x, y / x
    elif face == 4: u, v = z / y, -x / y
    else: u, v = -y / z, -x / z

    def uv_to_st(v_val):
        return 0.5 * math.sqrt(1 + 3 * v_val) if v_val >= 0 else 1 - 0.5 * math.sqrt(1 - 3 * v_val)

    max_size = 1 << 30
    def st_to_ij(s):
        return max(0, min(max_size - 1, int(math.floor(max_size * s))))

    i = st_to_ij(uv_to_st(u))
    j = st_to_ij(uv_to_st(v))

    n = face << 60
    bits = face & _S2_SWAP_MASK
    for k in range(7, -1, -1):
        mask = (1 << _S2_LOOKUP_BITS) - 1
        bits += (((i >> (k * _S2_LOOKUP_BITS)) & mask) << (_S2_LOOKUP_BITS + 2))
        bits += (((j >> (k * _S2_LOOKUP_BITS)) & mask) << 2)
        bits = _S2_LOOKUP_POS[bits]
        n |= (bits >> 2) << (k * 2 * _S2_LOOKUP_BITS)
        bits &= (_S2_SWAP_MASK | _S2_INVERT_MASK)

    cell_id = n * 2 + 1
    lsb = 1 << (2 * (30 - level))
    parent_id = (cell_id & -lsb) | lsb
    return format(parent_id, "016x").rstrip("0")


GLOBAL_DECOYS = [
    "afa0b", "a4dcb", "bae69", "1664b", "3b905", "54ab1", "82f9b", "99277",
    "1c73b", "2fd4b", "85c73", "14cab", "9105d", "80c2d", "34689", "32f91"
]


def generate_decoys(primary, count=3):
    cands = [t for t in GLOBAL_DECOYS if t != primary]
    random.seed(primary)
    return random.sample(cands, min(count, len(cands)))


# ==============================================================================
# Protobuf Utilities
# ==============================================================================

def encode_varint(n):
    res = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n: res.append(b | 0x80)
        else: res.append(b); break
    return bytes(res)


def read_varint_from_stream(stream):
    val = 0
    shift = 0
    while True:
        chunk = stream.read(1) if hasattr(stream, "read") else stream.recv(1)
        if not chunk: return None
        b = chunk[0]
        val |= (b & 0x7F) << shift
        if not (b & 0x80): break
        shift += 7
    return val


def field_varint(tag, val): return encode_varint((tag << 3) | 0) + encode_varint(val)
def field_double(tag, val): return encode_varint((tag << 3) | 1) + struct.pack("<d", val)
def field_float(tag, val): return encode_varint((tag << 3) | 5) + struct.pack("<f", val)
def field_bytes(tag, b): return encode_varint((tag << 3) | 2) + encode_varint(len(b)) + b
def field_str(tag, s): return field_bytes(tag, s.encode("utf-8"))


def parse_protobuf(data):
    fields = {}
    p = 0
    length_data = len(data)
    while p < length_data:
        tag_wire = 0
        shift = 0
        while True:
            if p >= length_data: return fields
            b = data[p]
            p += 1
            tag_wire |= (b & 0x7F) << shift
            if not (b & 0x80): break
            shift += 7
        tag = tag_wire >> 3
        wire = tag_wire & 7
        if wire == 0:
            val = 0
            shift = 0
            while True:
                if p >= length_data: return fields
                b = data[p]
                p += 1
                val |= (b & 0x7F) << shift
                if not (b & 0x80): break
                shift += 7
        elif wire == 1:
            if p + 8 > length_data: break
            val = struct.unpack("<Q", data[p:p + 8])[0]
            p += 8
        elif wire == 2:
            length = 0
            shift = 0
            while True:
                if p >= length_data: return fields
                b = data[p]
                p += 1
                length |= (b & 0x7F) << shift
                if not (b & 0x80): break
                shift += 7
            if p + length > length_data: break
            val = data[p:p + length]
            p += length
        elif wire == 5:
            if p + 4 > length_data: break
            val = struct.unpack("<I", data[p:p + 4])[0]
            p += 4
        else:
            break
        fields.setdefault(tag, []).append((wire, val))
    return fields


def distancia_haversine(lat1, lon1, lat2, lon2):
    R = 6371.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2.0)**2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2.0)**2
    return round(2 * R * math.atan2(math.sqrt(a), math.sqrt(1 - a)), 1)


# ==============================================================================
# Synthetic Checkin Registration
# ==============================================================================

def checkin_virtual_device(locale="en_US", timezone="UTC"):
    """Register an anonymous virtual Google Pixel 6 via Checkin API."""
    build = (
        field_str(1, "google/oriole/oriole:14/UP1A.231005.007/10754064:user/release-keys") +
        field_str(2, "oriole") +
        field_str(3, "Google") +
        field_str(4, "Pixel 6") +
        field_varint(5, 34) +
        field_str(6, "android-google") +
        field_str(7, "oriole") +
        field_varint(8, 240913000)
    )
    checkin_info = (
        field_bytes(1, build) +
        field_varint(2, 0) +
        field_varint(12, 1)
    )
    req_body = (
        field_varint(2, 0) +
        field_bytes(4, checkin_info) +
        field_str(6, locale) +
        field_str(12, timezone) +
        field_varint(14, 3) +
        field_varint(22, 0)
    )
    headers = {
        "Content-Type": "application/x-protobuffer",
        "User-Agent": "Android-Checkin/2.0 (oriole UP1A.231005.007); gzip"
    }
    req = urllib.request.Request(CHECKIN_URL, data=req_body, headers=headers)
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = resp.read()
    pb = parse_protobuf(data)
    aid = pb.get(7, [(1, 0)])[0][1]
    tok = pb.get(8, [(1, 0)])[0][1]
    return aid, tok


def ensure_fleet_credentials(force=False):
    """Load or generate synthetic fleet credentials (preserves existing node credentials)."""
    flota = {}
    if not force and os.path.isfile(FLEET_FILE):
        try:
            with open(FLEET_FILE, "r") as f:
                flota = json.load(f)
            if all(s["nombre"] in flota for s in HOTSPOTS):
                return flota
        except Exception:
            flota = {}

    log(f"Configuring synthetic fleet for {len(HOTSPOTS)} seismic hotspots...")
    actualizado = False
    for spot in HOTSPOTS:
        nombre = spot["nombre"]
        if not force and nombre in flota and "android_id" in flota[nombre]:
            continue
        actualizado = True
        cell = lat_lon_to_s2_cell_token(spot["lat"], spot["lon"], 8)
        decoys = [f"ea.{d}" for d in generate_decoys(cell, 3)]
        aid, tok = checkin_virtual_device(spot["locale"], spot["tz"])
        flota[nombre] = {
            "android_id": aid,
            "security_token": tok,
            "device_model": "Pixel 6 (Android 14)",
            "locale": spot["locale"],
            "time_zone": spot["tz"],
            "lat": spot["lat"],
            "lon": spot["lon"],
            "cell_token": cell,
            "primary_topic": f"ea.{cell}",
            "decoys": decoys,
            "topics": [f"ea.{cell}"] + decoys
        }
        log(f"  ✓ {nombre} -> S2: ea.{cell} (AID: {aid})")
        time.sleep(0.3)

    if actualizado or not os.path.isfile(FLEET_FILE):
        os.makedirs(os.path.dirname(FLEET_FILE), exist_ok=True)
        with open(FLEET_FILE, "w") as f:
            json.dump(flota, f, indent=2)
        os.chmod(FLEET_FILE, 0o600)
        log(f"Synthetic fleet saved to {FLEET_FILE} (mode 0600)")
    return flota


# ==============================================================================
# Earthquake Alert Decoder (Play Services gmta layout)
# ==============================================================================

def decodificar_payload_sismo(raw_bytes, nodo_nombre, nodo_info):
    gmta = parse_protobuf(raw_bytes)
    alertas = []
    for _, alert_bytes in gmta.get(2, []):
        gmsz = parse_protobuf(alert_bytes)
        mode = gmsz.get(2, [(0, 0)])[0][1]
        ev_id = "AEAS-Event"
        if 1 in gmsz:
            gmss = parse_protobuf(gmsz[1][0][1])
            ev_id = gmss.get(1, [(2, b"")])[0][1].decode("utf-8", errors="ignore") or ev_id

        mag, epi_lat, epi_lon, depth_m, origin_s = None, None, None, 0, None
        wave_speed, shaking_s = 2500, 10

        if 14 in gmsz:
            gmsq = parse_protobuf(gmsz[14][0][1])
            for _, info_b in gmsq.get(1, []):
                gmsr = parse_protobuf(info_b)
                if 1 in gmsr:
                    raw_mag = gmsr[1][0][1]
                    mag = round(struct.unpack("<f", struct.pack("<I", raw_mag))[0], 1) if isinstance(raw_mag, int) else float(raw_mag)
                if 2 in gmsr:
                    latlng = parse_protobuf(gmsr[2][0][1])
                    if 1 in latlng:
                        raw_lat = latlng[1][0][1]
                        epi_lat = struct.unpack("<d", struct.pack("<Q", raw_lat))[0] if isinstance(raw_lat, int) else raw_lat
                    if 2 in latlng:
                        raw_lon = latlng[2][0][1]
                        epi_lon = struct.unpack("<d", struct.pack("<Q", raw_lon))[0] if isinstance(raw_lon, int) else raw_lon
                if 3 in gmsr: depth_m = gmsr[3][0][1]
                if 4 in gmsr:
                    ts_p = parse_protobuf(gmsr[4][0][1])
                    origin_s = ts_p.get(1, [(0, None)])[0][1]
                if 5 in gmsr: wave_speed = gmsr[5][0][1] or 2500
                if 6 in gmsr: shaking_s = gmsr[6][0][1]

        dist_al_nodo = None
        if epi_lat and epi_lon and nodo_info.get("lat") and nodo_info.get("lon"):
            dist_al_nodo = distancia_haversine(nodo_info["lat"], nodo_info["lon"], epi_lat, epi_lon)

        alerta = {
            "timestamp_recepcion": time.time(),
            "timestamp_iso": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "nodo_receptor": nodo_nombre,
            "event_id": ev_id,
            "is_test": (mode == 2 or "test" in ev_id.lower()),
            "magnitud": mag,
            "lat": epi_lat,
            "lon": epi_lon,
            "profundidad_km": round(depth_m / 1000.0, 1) if depth_m else 0,
            "distancia_al_nodo_km": dist_al_nodo,
            "origin_time": origin_s,
            "wave_speed_mps": wave_speed,
            "shaking_duration_s": shaking_s
        }
        alertas.append(alerta)
    return alertas


# ==============================================================================
# Telemetry HTTP Server (:8998)
# ==============================================================================

class TelemetriaHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path in ("/", "/status", "/json"):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(ESTADO, indent=2, ensure_ascii=False).encode("utf-8"))
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        return


def iniciar_servidor_telemetria(port=DEFAULT_HTTP_PORT):
    try:
        servidor = http.server.ThreadingHTTPServer(("0.0.0.0", port), TelemetriaHandler)
        log(f"Telemetry server running at http://127.0.0.1:{port}/status")
        t = threading.Thread(target=servidor.serve_forever, daemon=True)
        t.start()
    except Exception as e:
        log(f"Error starting telemetry server: {e}")


# ==============================================================================
# Fleet Node Manager
# ==============================================================================

class NodoFlota:
    def __init__(self, nombre, info):
        self.nombre = nombre
        self.info = info
        self.android_id = info["android_id"]
        self.security_token = info["security_token"]
        self.topics = info["topics"]
        self.sock = None
        self.conectado = False
        self.ultimo_ping = 0
        self.ultimo_latido = None
        self.latidos_exitosos = 0
        self.proximo_intento = 0
        self.backoff = 2

    def _login_packet(self):
        setting = field_str(1, "new_vc") + field_str(2, "1")
        dev_id = f"android-{hex(self.android_id)[2:]}"
        login_req = (
            field_str(1, "android-34") +
            field_str(2, "mcs.android.com") +
            field_str(3, str(self.android_id)) +
            field_str(4, str(self.android_id)) +
            field_str(5, str(self.security_token)) +
            field_str(6, dev_id) +
            field_bytes(8, setting) +
            field_varint(14, 1) +
            field_varint(16, 2) +
            field_varint(17, 1)
        )
        for t in self.topics:
            login_req += field_str(29, t)
        return bytes([MCS_VERSION, TAG_LOGIN_REQUEST]) + encode_varint(len(login_req)) + login_req

    def conectar(self):
        try:
            raw = socket.create_connection((HOST, PORT), timeout=10)
            ctx = ssl.create_default_context()
            s = ctx.wrap_socket(raw, server_hostname=HOST)
            s.settimeout(10)
            s.sendall(self._login_packet())
            v = s.recv(1)[0]
            tag = s.recv(1)[0]
            if v != MCS_VERSION or tag != TAG_LOGIN_RESPONSE:
                s.close()
                return False
            resp_len = read_varint_from_stream(s)
            if resp_len: s.recv(resp_len)
            s.setblocking(False)
            self.sock = s
            self.conectado = True
            self.ultimo_ping = time.time()
            self.backoff = 2
            return True
        except Exception as e:
            self.conectado = False
            self.proximo_intento = time.time() + self.backoff
            self.backoff = min(60, self.backoff * 1.5)
            return False

    def enviar_ping(self):
        try:
            self.sock.sendall(bytes([TAG_HEARTBEAT_PING, 0]))
            self.ultimo_ping = time.time()
        except Exception:
            self.desconectar()

    def enviar_pong(self):
        try:
            self.sock.sendall(bytes([TAG_HEARTBEAT_ACK, 0]))
        except Exception:
            pass

    def desconectar(self):
        self.conectado = False
        if self.sock:
            try: self.sock.close()
            except Exception: pass
            self.sock = None
        self.proximo_intento = time.time() + self.backoff


def ejecutar_radar():
    flota_data = ensure_fleet_credentials()
    nodos = [NodoFlota(nom, dat) for nom, dat in flota_data.items()]
    iniciar_servidor_telemetria()

    log(f"Connecting 21 synthetic Pixel nodes to {HOST}:{PORT}...")
    for nodo in nodos:
        nodo.conectar()
        time.sleep(0.1)

    while True:
        ahora = time.time()
        for nodo in nodos:
            if not nodo.conectado and ahora >= nodo.proximo_intento:
                nodo.conectar()

        # Update telemetry
        conectados = sum(1 for n in nodos if n.conectado)
        ESTADO["nodos_conectados"] = conectados
        ESTADO["porcentaje_conectado"] = f"{round((conectados / len(nodos)) * 100)}%"
        ESTADO["nodos"] = {
            n.nombre: {
                "android_id": n.android_id,
                "celda": n.info.get("cell_token"),
                "topico_principal": n.info.get("primary_topic"),
                "conectado": n.conectado,
                "latidos": n.latidos_exitosos,
                "ultimo_latido": n.ultimo_latido
            } for n in nodos
        }

        # Non-blocking multiplexing
        socks = [n.sock for n in nodos if n.conectado and n.sock]
        sock_to_nodo = {n.sock: n for n in nodos if n.conectado and n.sock}

        try:
            rlist, _, _ = select.select(socks, [], [], 2.0)
            for s in rlist:
                nodo = sock_to_nodo.get(s)
                if not nodo: continue
                try:
                    tag_b = s.recv(1)
                    if not tag_b:
                        nodo.desconectar()
                        continue
                    tag = tag_b[0]
                    length = read_varint_from_stream(s) or 0
                    payload = b""
                    if length > 0:
                        s.settimeout(2.0)
                        while len(payload) < length:
                            chunk = s.recv(length - len(payload))
                            if not chunk: break
                            payload += chunk
                        s.setblocking(False)

                    if tag == TAG_HEARTBEAT_PING:
                        nodo.enviar_pong()
                    elif tag == TAG_HEARTBEAT_ACK:
                        nodo.latidos_exitosos += 1
                        nodo.ultimo_latido = time.strftime("%Y-%m-%d %H:%M:%S")
                        ESTADO["ultimo_latido_global"] = nodo.ultimo_latido
                    elif tag == TAG_DATA_MESSAGE_STANZA:
                        alertas = decodificar_payload_sismo(payload, nodo.nombre, nodo.info)
                        for al in alertas:
                            log(f"🚨 SISMIC ALERT RECEIVED BY NODE {nodo.nombre}! M{al['magnitud']} (Event: {al['event_id']})")
                            ESTADO["total_alertas_recibidas"] += 1
                            ESTADO["ultima_alerta"] = al
                            with open(DETECCIONES_LOG, "a") as f_log:
                                f_log.write(json.dumps(al, ensure_ascii=False) + "\n")
                    elif tag == TAG_CLOSE:
                        nodo.desconectar()
                except Exception:
                    nodo.desconectar()
        except Exception:
            pass

        # Send heartbeats every 2 minutes
        for nodo in nodos:
            if nodo.conectado and ahora - nodo.ultimo_ping > 120:
                nodo.enviar_ping()


def main():
    parser = argparse.ArgumentParser(description="Global Seismic Radar Fleet Manager")
    parser.add_argument("--test-connection", action="store_true", help="Test simultaneous connection of all 21 nodes")
    parser.add_argument("--generate-fleet", action="store_true", help="Force regenerate synthetic fleet credentials")
    args = parser.parse_args()

    if args.generate_fleet:
        ensure_fleet_credentials(force=True)
        return 0

    if args.test_connection:
        flota_data = ensure_fleet_credentials()
        nodos = [NodoFlota(nom, dat) for nom, dat in flota_data.items()]
        log(f"Testing connections for {len(nodos)} nodes...")
        success = 0
        for n in nodos:
            if n.conectar():
                success += 1
                log(f"  ✓ {n.nombre} connected (Cell: {n.info['cell_token']})")
                n.desconectar()
            else:
                log(f"  ✗ {n.nombre} failed")
            time.sleep(0.1)
        log(f"Result: {success}/{len(nodos)} nodes connected successfully.")
        return 0 if success == len(nodos) else 1

    ejecutar_radar()
    return 0


if __name__ == "__main__":
    sys.exit(main())
