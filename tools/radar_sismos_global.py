#!/usr/bin/env python3
"""radar_sismos_global.py - Fleet of Google alert receivers (Android Earthquake Alerts System).

Keeps a fleet of synthetic Android identities connected to Google MCS (mtalk.google.com:5228).
Each node subscribes, like Play Services does, to its own S2 level-8 cell plus 3 decoy cells
(field 29 of the LoginRequest, see docs/HOW_IT_WORKS.md). The 66 decoys are not random: they
sit on places that matter or that issue many alerts (DECOY_PLACES), so a node in Chile also
listens to Bogota, Oklahoma City and Buenos Aires.

What arrives on this channel (observed Oct 2026):
  * earthquake alerts: gmta type 5, EarthquakeWrap (field 14) with magnitude/epicenter/origin;
  * Google crisis alerts in the same gmta format (gcms=crisisalerts, persistent_id "CRISIS",
    re-sent on every login while active): type 1 = public alert (`pa:` ids, e.g. a US National
    Weather Service "Flood Watch") and type 2 = SOS alert (`cmid:` ids, e.g. hurricanes).
    Their area comes as a compressed S2Polygon, decoded here in pure Python.

Files (in RADAR_DATA_DIR, default: this directory):
  radar_payloads.jsonl              EVERY data message, raw (base64) + protobuf tree
  sismos_globales_detectados.jsonl  one line per received alert/version (decoded)
  radar_vistos.json                 fingerprints of processed messages (Google re-sends)

Environment: RADAR_FLEET_FILE (default <data dir>/synthetic_fleet.json), RADAR_DATA_DIR,
RADAR_LANG (en|es, language of the summaries), RADAR_HTTP_PORT (8998, loopback only).

Usage:
  ./radar_sismos_global.py                     Run the fleet (needs the fleet file)
  ./radar_sismos_global.py --generate-fleet    Register missing nodes via Checkin + assign decoys
  ./radar_sismos_global.py --update-decoys     Re-assign the decoys of an existing fleet
  ./radar_sismos_global.py --test-connection   Connect every node once and exit
  ./radar_sismos_global.py --decodificar [F]   Re-interpret saved payloads (radar_payloads.jsonl)
  ./radar_sismos_global.py --reconstruir       Rebuild the detections file from the raw payloads
"""

import base64
import datetime as dt
import hashlib
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
import urllib.parse
import urllib.request
import http.server

BASE_DIR = os.environ.get("RADAR_DATA_DIR") or os.path.dirname(os.path.abspath(__file__))
FLEET_FILE = os.environ.get("RADAR_FLEET_FILE") or os.path.join(BASE_DIR, "synthetic_fleet.json")
DETECCIONES_LOG = os.path.join(BASE_DIR, "sismos_globales_detectados.jsonl")
PAYLOADS_LOG = os.path.join(BASE_DIR, "radar_payloads.jsonl")
VISTOS_FILE = os.path.join(BASE_DIR, "radar_vistos.json")
LANG = os.environ.get("RADAR_LANG", "en")

HOST = "mtalk.google.com"
PORT = 5228
MCS_VERSION = 41
HTTP_STATUS_PORT = int(os.environ.get("RADAR_HTTP_PORT", "8998"))
CHECKIN_URL = "https://android.clients.google.com/checkin"
PING_CADA_S = 150          # our own ping per node
SIN_RESPUESTA_S = 90       # nothing received this long after a ping -> dead connection
TRAMA_MAX = 1 << 20

TAG_HEARTBEAT_PING = 0
TAG_HEARTBEAT_ACK = 1
TAG_LOGIN_REQUEST = 2
TAG_LOGIN_RESPONSE = 3
TAG_CLOSE = 4
TAG_IQ_STANZA = 7
TAG_DATA_MESSAGE_STANZA = 8
IQ_SET = 1
EXT_SELECTIVE_ACK = 12

# Enums of the gmsz/gmsw protos (docs/HOW_IT_WORKS.md)
TIPO_TERREMOTO = 5
TIPOS = {5: "terremoto", 2: "crisis_sos", 1: "alerta_publica"}
MODOS = {0: "normal", 1: "silencioso", 2: "prueba", 3: "normal"}
NIVELES_MMI = {1: "MMI5+", 2: "MMI4", 3: "MMI3"}

ESTADO = {
    "modo": "flota_distribuida",
    "conectado": False,
    "flota_tamano": 22,
    "nodos_conectados": 0,
    "porcentaje_conectado": "0%",
    "iniciado_desde": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    "ultimo_latido_global": None,
    "mensajes_recibidos": 0,
    "total_alertas_recibidas": 0,
    "total_sismos": 0,
    "ultima_alerta": None,
    "ultimo_sismo": None,
    "ultima_novedad": None,     # new alert, or one whose title/zone/type changed (not re-sends)
    "eventos": [],
    "nodos": {}
}
LOCK = threading.RLock()
MAX_EVENTOS = 20
TODOS = {}   # _clave -> latest version of each alert (for the counters)

TEXTOS = {
    "es": {"crisis": "{nombre} — {clase} de Google, no sismo", "publica": "alerta pública", "sos": "alerta SOS",
           "otra": "alerta de crisis", "zona": " · zona {z}", "posible": "Posible M{m:.1f} (USGS) · {l}",
           "sin_mag": "Alerta sin magnitud (tipo sin confirmar) · {l}", "prueba": " [prueba]",
           "silenciosa": " [silenciosa]", "antes": "antes: {x}", "mag_antes": "magnitud antes: {x}",
           "zona_antes": "zona antes: {x}", "cambio": "cambió"},
    "en": {"crisis": "{nombre} — Google {clase}, not an earthquake", "publica": "public alert", "sos": "SOS alert",
           "otra": "crisis alert", "zona": " · zone {z}", "posible": "Possible M{m:.1f} (USGS) · {l}",
           "sin_mag": "Alert without magnitude (type unconfirmed) · {l}", "prueba": " [test]",
           "silenciosa": " [silent]", "antes": "was: {x}", "mag_antes": "magnitude was: {x}",
           "zona_antes": "zone was: {x}", "cambio": "changed"},
}


def T(clave, **kw):
    return TEXTOS.get(LANG, TEXTOS["en"])[clave].format(**kw)


def log(msg):
    sys.stdout.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] [FLOTA-GLOBAL] {msg}\n")
    sys.stdout.flush()


def ahora_iso():
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def anexar_jsonl(ruta, obj):
    fd = os.open(ruta, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


# ==============================================================================
# 1. Protobuf utilities
# ==============================================================================

def encode_varint(n):
    res = bytearray()
    while True:
        b = n & 0x7F; n >>= 7
        if n: res.append(b | 0x80)
        else: res.append(b); break
    return bytes(res)


def leer_varint(buf, p):
    """(value, new_pos), or (None, p) if the varint is incomplete."""
    val = 0; shift = 0
    while p < len(buf):
        b = buf[p]; p += 1
        val |= (b & 0x7F) << shift
        if not (b & 0x80): return val, p
        shift += 7
        if shift > 63: raise ValueError("varint inválido")
    return None, p


def field_varint(tag, val): return encode_varint((tag << 3) | 0) + encode_varint(val)
def field_double(tag, val): return encode_varint((tag << 3) | 1) + struct.pack("<d", val)
def field_float(tag, val): return encode_varint((tag << 3) | 5) + struct.pack("<f", val)
def field_bytes(tag, b): return encode_varint((tag << 3) | 2) + encode_varint(len(b)) + b
def field_str(tag, s): return field_bytes(tag, s.encode("utf-8"))


def parse_estricto(data):
    """[(tag, wire, value)], or None if the bytes are not a complete, valid protobuf."""
    campos = []; p = 0; n = len(data)
    try:
        while p < n:
            tw, p = leer_varint(data, p)
            if tw is None: return None
            tag, wire = tw >> 3, tw & 7
            if tag == 0: return None
            if wire == 0:
                val, p = leer_varint(data, p)
                if val is None: return None
            elif wire == 1:
                if p + 8 > n: return None
                val = struct.unpack("<Q", data[p:p + 8])[0]; p += 8
            elif wire == 2:
                ln, p = leer_varint(data, p)
                if ln is None or p + ln > n: return None
                val = bytes(data[p:p + ln]); p += ln
            elif wire == 5:
                if p + 4 > n: return None
                val = struct.unpack("<I", data[p:p + 4])[0]; p += 4
            else:
                return None
            campos.append((tag, wire, val))
    except ValueError:
        return None
    return campos


def parse_protobuf(data):
    """{tag: [(wire, value), ...]} (empty if not a valid protobuf)."""
    fields = {}
    for tag, wire, val in parse_estricto(data) or []:
        fields.setdefault(tag, []).append((wire, val))
    return fields


def _texto_legible(b):
    try:
        s = b.decode("utf-8")
    except UnicodeDecodeError:
        return None
    return s if s and all(c.isprintable() or c in "\n\t" for c in s) else None


def arbol_protobuf(data, prof=0):
    """`protoc --decode_raw`-style dump as JSON, for reading payloads by hand."""
    campos = parse_estricto(data)
    if campos is None or prof > 12:
        return None
    out = []
    for tag, wire, val in campos:
        if wire == 0:
            out.append({"campo": tag, "varint": val})
        elif wire == 1:
            out.append({"campo": tag, "fixed64": val, "double": struct.unpack("<d", struct.pack("<Q", val))[0]})
        elif wire == 5:
            out.append({"campo": tag, "fixed32": val, "float": struct.unpack("<f", struct.pack("<I", val))[0]})
        else:
            texto = _texto_legible(val)
            sub = arbol_protobuf(val, prof + 1) if val and texto is None else None
            if sub:
                out.append({"campo": tag, "msg": sub})
            elif texto is not None:
                out.append({"campo": tag, "str": texto})
            else:
                out.append({"campo": tag, "hex": val.hex()})
    return out


def _f(campos, tag, default=None):
    v = campos.get(tag)
    return v[0][1] if v else default


def _double(v): return struct.unpack("<d", struct.pack("<Q", v))[0] if isinstance(v, int) else None
def _float(v): return struct.unpack("<f", struct.pack("<I", v))[0] if isinstance(v, int) else None


def _int32(v):
    """Negative int32 values travel as 64-bit varints."""
    if v is None: return None
    return v - (1 << 64) if v >= (1 << 63) else v


def _latlng(b):
    p = parse_protobuf(b)
    lat, lon = _double(_f(p, 1)), _double(_f(p, 2))
    return (round(lat, 4), round(lon, 4)) if lat is not None and lon is not None else None


def _timestamp(b):
    p = parse_protobuf(b)
    s = _f(p, 1)
    return s + (_f(p, 2, 0) or 0) / 1e9 if s is not None else None


def distancia_haversine(lat1, lon1, lat2, lon2):
    R = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2.0)**2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2.0)**2
    return round(2 * R * math.atan2(math.sqrt(a), math.sqrt(1 - a)), 1)


# ------------------------------------------------------------------------------
# S2 cells (hierarchy between tokens only, no geometry)
# ------------------------------------------------------------------------------

def _s2_id(token):
    return int(token.ljust(16, "0")[:16], 16)


def s2_relacionadas(a, b):
    """True if one cell contains the other (S2 cells are nested)."""
    try:
        ia, ib = _s2_id(a), _s2_id(b)
    except ValueError:
        return False
    if not ia or not ib: return False
    la, lb = ia & -ia, ib & -ib
    return ia - (la - 1) <= ib + (lb - 1) and ib - (lb - 1) <= ia + (la - 1)


# ------------------------------------------------------------------------------
# Compressed S2Polygon (S2Polygon::EncodeCompressed, version 4) in pure Python.
# Checked against the official s2geometry library with the real polygons of 2026-10-09.
# ------------------------------------------------------------------------------

def _st_a_uv(s):
    return (4 * s * s - 1) / 3.0 if s >= 0.5 else (1 - 4 * (1 - s) * (1 - s)) / 3.0


def _cara_uv_a_latlng(cara, u, v):
    x, y, z = [(1, u, v), (-u, 1, v), (-u, -v, 1), (-1, -v, -u), (v, -1, -u), (v, u, -1)][cara]
    return math.degrees(math.atan2(z, math.hypot(x, y))), math.degrees(math.atan2(y, x))


def _desintercalar(n):
    a = b = 0
    for i in range(32):
        a |= ((n >> (2 * i)) & 1) << i
        b |= ((n >> (2 * i + 1)) & 1) << i
    return a, b


class _Derivada2:
    """S2 NthDerivativeCoder(2), decoder side (32-bit arithmetic)."""
    def __init__(self):
        self.m = 0; self.mem = [0, 0]

    def decodificar(self, k):
        if self.m < 2: self.m += 1
        for i in range(self.m - 1, -1, -1):
            k = (self.mem[i] + k) & 0xFFFFFFFF
            self.mem[i] = k
        return k - (1 << 32) if k >= (1 << 31) else k


def decodificar_s2polygon(b):
    """List of rings [(lat, lon), ...], or None if this is not the compressed encoding."""
    if not b or b[0] != 4: return None
    nivel, p = b[1], 2
    nloops, p = leer_varint(b, p)
    anillos = []
    for _ in range(nloops):
        n, p = leer_varint(b, p)
        caras = []
        while len(caras) < n:
            v, p = leer_varint(b, p)
            caras += [v % 6] * (v // 6)
        cpi, cqi, pts = _Derivada2(), _Derivada2(), []
        for i in range(n):
            if i == 0:
                nb = (nivel + 7) // 8 * 2
                pi, qi = _desintercalar(int.from_bytes(b[p:p + nb], "little")); p += nb
                pi, qi = cpi.decodificar(pi), cqi.decodificar(qi)
            else:
                v, p = leer_varint(b, p)
                zp, zq = _desintercalar(v)
                pi = cpi.decodificar((zp >> 1) ^ -(zp & 1))
                qi = cqi.decodificar((zq >> 1) ^ -(zq & 1))
            escala = 1 << nivel
            pts.append(_cara_uv_a_latlng(caras[i], _st_a_uv((pi + 0.5) / escala), _st_a_uv((qi + 0.5) / escala)))
        fuera, p = leer_varint(b, p)          # vertices that are not at the centre of their cell
        for _ in range(fuera):
            idx, p = leer_varint(b, p)
            x, y, z = struct.unpack("<ddd", b[p:p + 24]); p += 24
            pts[idx] = (math.degrees(math.atan2(z, math.hypot(x, y))), math.degrees(math.atan2(y, x)))
        props, p = leer_varint(b, p)
        _, p = leer_varint(b, p)              # depth
        if props & 2: p += 33                 # encoded S2LatLngRect
        anillos.append([(round(la, 5), round(lo, 5)) for la, lo in pts])
    return anillos


def _dentro(lat, lon, anillo):
    """Point in polygon (ray casting in lat/lon; alert areas do not cross the poles or the antimeridian)."""
    dentro = False
    for (la1, lo1), (la2, lo2) in zip(anillo, anillo[1:] + anillo[:1]):
        if (la1 > lat) != (la2 > lat) and lon < lo1 + (lat - la1) * (lo2 - lo1) / (la2 - la1):
            dentro = not dentro
    return dentro


def resumen_poligono(anillos):
    pts = [q for a in anillos for q in a]
    if not pts: return None
    return {"caja": [min(q[0] for q in pts), min(q[1] for q in pts), max(q[0] for q in pts), max(q[1] for q in pts)],
            "centro": (round(sum(q[0] for q in pts) / len(pts), 3), round(sum(q[1] for q in pts) / len(pts), 3)),
            "vertices": len(pts), "anillos": len(anillos)}


def celda_mas_cercana(anillos, candidatos):
    """From {token: (lat, lon)}, the subscribed cell inside the polygon, or the closest one."""
    mejor = None
    for tok, (la, lo) in candidatos.items():
        if any(_dentro(la, lo, a) for a in anillos):
            return tok, 0.0
        d = min(distancia_haversine(la, lo, q[0], q[1]) for a in anillos for q in a)
        if mejor is None or d < mejor[1]: mejor = (tok, d)
    return mejor


# ==============================================================================
# Fleet generation (synthetic identities via Checkin, cells and decoys)
# ==============================================================================

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

# Where the 66 decoys go (3 per node, node i gets places i, i+22 and i+44). Real phones pick
# them at random for privacy; most random cells fall in the ocean and never see an alert.
DECOY_PLACES = [
    # Colombia and neighbours
    ("Bogota", 4.711, -74.072),
    ("Cali", 3.4516, -76.532),
    ("Barranquilla", 10.9685, -74.7813),
    ("Cartagena", 10.391, -75.4794),
    ("Santa Marta", 11.2408, -74.199),
    ("San Andres", 12.5847, -81.7006),
    ("Nevado del Ruiz", 4.892, -75.324),
    ("Pasto (Galeras)", 1.2136, -77.2811),
    ("Cucuta", 7.8939, -72.5078),
    ("Popayan", 2.4448, -76.6147),
    ("Villavicencio", 4.142, -73.6266),
    ("Quibdo", 5.6947, -76.6611),
    ("Mocoa", 1.1522, -76.6464),
    ("Bucaramanga", 7.1193, -73.1227),
    ("Monteria", 8.7479, -75.8814),
    ("Quito", -0.1807, -78.4678),
    ("Caracas", 10.4806, -66.9036),
    ("Guayaquil", -2.1709, -79.9224),
    # United States: hurricanes, tornadoes, heat, wildfires, tsunamis
    ("Miami", 25.7617, -80.1918),
    ("Tampa", 27.9506, -82.4572),
    ("New Orleans", 29.9511, -90.0715),
    ("Houston", 29.7604, -95.3698),
    ("Oklahoma City", 35.4676, -97.5164),
    ("Wichita", 37.6872, -97.3301),
    ("Dallas", 32.7767, -96.797),
    ("Nashville", 36.1627, -86.7816),
    ("Charleston", 32.7765, -79.9311),
    ("New York", 40.7128, -74.006),
    ("Phoenix", 33.4484, -112.074),
    ("Sacramento", 38.5816, -121.4944),
    ("Seattle", 47.6062, -122.3321),
    ("Anchorage", 61.2181, -149.9003),
    ("Honolulu", 21.3069, -157.8583),
    ("San Juan (PR)", 18.4655, -66.1057),
    ("Idaho (Boise)", 42.9, -113.9),
    # Caribbean, Central America and Mexico
    ("Havana", 23.1136, -82.3666),
    ("Santo Domingo", 18.4861, -69.9312),
    ("Port-au-Prince", 18.5944, -72.3074),
    ("Guatemala City", 14.6349, -90.5069),
    ("San Salvador", 13.6929, -89.2182),
    ("Cancún", 21.1619, -86.8515),
    ("Mexico City", 19.4326, -99.1332),
    # South America
    ("São Paulo", -23.5505, -46.6333),
    ("Porto Alegre", -30.0346, -51.2177),
    ("Buenos Aires", -34.6037, -58.3816),
    # Asia
    ("Tokyo", 35.6762, 139.6503),
    ("Sendai", 38.2682, 140.8694),
    ("Osaka", 34.6937, 135.5023),
    ("Naha (Okinawa)", 26.2124, 127.6809),
    ("Seoul", 37.5665, 126.978),
    ("Taipei", 25.033, 121.5654),
    ("Hong Kong", 22.3193, 114.1694),
    ("Tacloban", 11.2443, 125.0048),
    ("Hanoi", 21.0278, 105.8342),
    ("Dhaka", 23.8103, 90.4125),
    ("Kolkata", 22.5726, 88.3639),
    ("Mumbai", 19.076, 72.8777),
    ("Chennai", 13.0827, 80.2707),
    ("Jakarta", -6.2088, 106.8456),
    # Oceania and Europe
    ("Auckland", -36.8485, 174.7633),
    ("Brisbane", -27.4698, 153.0251),
    ("Suva (Fiji)", -18.1416, 178.4419),
    ("Valencia", 39.4699, -0.3763),
    ("Bologna", 44.4949, 11.3426),
    ("Naples (Campi Flegrei)", 40.827, 14.139),
    ("Amsterdam", 52.3676, 4.9041),
]

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


def asignar_senuelos(flota):
    """Give every node 3 decoys from DECOY_PLACES, in HOTSPOTS order (names and coordinates kept)."""
    nombres = [h["nombre"] for h in HOTSPOTS if h["nombre"] in flota]
    n = len(nombres)
    if 3 * n > len(DECOY_PLACES):
        raise ValueError(f"{n} nodes need {3 * n} decoys, DECOY_PLACES has {len(DECOY_PLACES)}")
    for i, nombre in enumerate(nombres):
        v = flota[nombre]
        lugares = [DECOY_PLACES[i + k * n] for k in range(3)]
        tokens = [f"ea.{lat_lon_to_s2_cell_token(la, lo, 8)}" for _, la, lo in lugares]
        v["decoys"] = tokens
        v["decoy_names"] = {t: lugar for t, (lugar, _, _) in zip(tokens, lugares)}
        v["decoy_coords"] = {t: [la, lo] for t, (_, la, lo) in zip(tokens, lugares)}
        v["topics"] = [v["primary_topic"]] + tokens
    return flota


def guardar_flota(flota):
    tmp = FLEET_FILE + ".tmp"
    os.makedirs(os.path.dirname(FLEET_FILE) or ".", exist_ok=True)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(flota, f, indent=2, ensure_ascii=False)
    os.replace(tmp, FLEET_FILE)


def generar_flota(force=False):
    """Register (Checkin) the HOTSPOTS that have no identity yet, assign decoys and save (0600)."""
    flota = {}
    if os.path.isfile(FLEET_FILE):
        with open(FLEET_FILE) as f:
            flota = json.load(f)
    nuevos = [h for h in HOTSPOTS if force or "android_id" not in flota.get(h["nombre"], {})]
    log(f"Registering {len(nuevos)} new identities with Google Checkin into {FLEET_FILE} "
        f"({len(HOTSPOTS) - len(nuevos)} already registered)")
    for spot in nuevos:
        nombre = spot["nombre"]
        cell = lat_lon_to_s2_cell_token(spot["lat"], spot["lon"], 8)
        aid, tok = checkin_virtual_device(spot["locale"], spot["tz"])
        flota[nombre] = {"android_id": aid, "security_token": tok, "device_model": "Pixel 6 (Android 14)",
                         "locale": spot["locale"], "time_zone": spot["tz"], "lat": spot["lat"], "lon": spot["lon"],
                         "cell_token": cell, "primary_topic": f"ea.{cell}"}
        log(f"  ✓ {nombre} -> ea.{cell}")
        time.sleep(0.3)
    asignar_senuelos(flota)
    guardar_flota(flota)
    log(f"Fleet saved to {FLEET_FILE} ({len(flota)} nodes, mode 0600)")
    return flota


# ==============================================================================
# 2. Alert decoding (gmta proto, docs/HOW_IT_WORKS.md)
# ==============================================================================

def decodificar_region(b):
    r = parse_protobuf(b)
    celdas = []
    for _, cb in r.get(1, []):
        celdas += [t.decode("ascii", "ignore") for _, t in parse_protobuf(cb).get(1, []) if isinstance(t, bytes)]
    contornos = []
    for _, cvb in r.get(2, []):
        for _, cb in parse_protobuf(cvb).get(1, []):
            c = parse_protobuf(cb)
            anillos = []
            for _, rb in c.get(2, []):
                pts = [_latlng(v) for _, v in parse_protobuf(rb).get(1, [])]
                anillos.append([p for p in pts if p])
            circulos = []
            for _, ccb in c.get(3, []):
                cc = parse_protobuf(ccb)
                centro = _latlng(_f(cc, 1, b"")) if _f(cc, 1) else None
                circulos.append({"centro": centro, "radio_km": round((_int32(_f(cc, 2)) or 0) / 1000.0, 1)})
            nivel = _f(c, 1, 0)
            contornos.append({"nivel": NIVELES_MMI.get(nivel, f"nivel_{nivel}"),
                              "poligonos": len(anillos), "vertices": sum(len(a) for a in anillos),
                              "circulos": circulos, "_anillos": anillos})
    poly = _f(r, 3, b"") or b""
    region = {"celdas": celdas, "contornos": contornos, "s2polygon_bytes": len(poly)}
    if poly:
        try:
            anillos = decodificar_s2polygon(poly)
        except Exception:
            anillos = None
        if anillos:
            region["poligono"] = resumen_poligono(anillos)
            region["_poligono"] = anillos
    return region


def _centro_region(region):
    """Rough centre of the region (when the alert has no epicentre)."""
    pts = []
    for c in region["contornos"]:
        for a in c["_anillos"]: pts += a
        pts += [ci["centro"] for ci in c["circulos"] if ci["centro"]]
    if not pts: return None
    return (round(sum(p[0] for p in pts) / len(pts), 3), round(sum(p[1] for p in pts) / len(pts), 3))


def decodificar_gmta(raw_bytes):
    """Alerts (dicts) in a `rawData` payload (gmta AlertBatch)."""
    alertas = []
    gmta = parse_protobuf(raw_bytes)
    lote = _timestamp(_f(gmta, 1)) if _f(gmta, 1) else None
    for _, alert_bytes in gmta.get(2, []):
        gmsz = parse_protobuf(alert_bytes)
        a = {"event_id": None, "emitida": None, "modo_raw": _f(gmsz, 2, 0), "tipo_raw": _f(gmsz, 3, 0),
             "magnitud": None, "lat": None, "lon": None, "profundidad_km": None, "origin_time": None,
             "wave_speed_mps": None, "shaking_duration_s": None, "fuente_raw": None,
             "mostrar_magnitud": None, "region": None, "campos_presentes": sorted(gmsz)}
        a["modo"] = MODOS.get(a["modo_raw"], f"modo_{a['modo_raw']}")
        a["tipo"] = TIPOS.get(a["tipo_raw"], f"tipo_{a['tipo_raw']}")
        a["lote_enviado"] = lote
        # Fields seen in crisis alerts, 2026-10-09: 5 expires, 7 {event, duration},
        # 8 query (kgmid), 9 texts per language, 12 {source, n}, 13 updated
        if _f(gmsz, 5): a["expira"] = _timestamp(_f(gmsz, 5))
        if _f(gmsz, 7):
            g7 = parse_protobuf(_f(gmsz, 7))
            ev = (_f(g7, 1) or b"").decode("utf-8", "ignore") if isinstance(_f(g7, 1), bytes) else ""
            if ev and ev != a["event_id"]: a["evento"] = ev          # e.g. FLOOD
            d = parse_protobuf(_f(g7, 2, b"") or b"")
            if _f(d, 1) is not None: a["duracion_s"] = _f(d, 1)
        if isinstance(_f(gmsz, 8), bytes): a["consulta"] = _f(gmsz, 8).decode("utf-8", "ignore")
        textos = {}   # language -> {titulo, area, emisor}
        for _, tb in gmsz.get(9, []):
            t = parse_protobuf(tb)
            if not isinstance(_f(t, 1), bytes): continue
            dec = lambda m, k: _f(m, k).decode("utf-8", "ignore") if isinstance(_f(m, k), bytes) else None
            if _f(t, 2):    # public alert: {1 headline, 2 area, 3 sender}
                m = parse_protobuf(_f(t, 2))
                x = {"titulo": dec(m, 1), "area": dec(m, 2), "emisor": dec(m, 3)}
            else:           # SOS: {3: {2 title}}
                x = {"titulo": dec(parse_protobuf(_f(t, 3, b"") or b""), 2)}
            if x["titulo"]: textos[_f(t, 1).decode("ascii", "ignore")] = x
        if textos:
            x = textos.get("es") or textos.get("en") or next(iter(textos.values()))
            a["titulo"] = x["titulo"]
            for k in ("area", "emisor"):
                if x.get(k): a[k] = x[k]
            a["titulo_en"] = (textos.get("en") or {}).get("titulo")
            a["idiomas"] = len(textos)
        if _f(gmsz, 12):
            c = parse_protobuf(_f(gmsz, 12))
            if isinstance(_f(c, 1), bytes): a["categoria"] = _f(c, 1).decode("utf-8", "ignore")
            if _f(c, 2) is not None: a["categoria_n"] = _f(c, 2)
        if _f(gmsz, 13): a["actualizada"] = _timestamp(_f(gmsz, 13))
        if 1 in gmsz:
            gmss = parse_protobuf(_f(gmsz, 1))
            if _f(gmss, 1): a["event_id"] = _f(gmss, 1).decode("utf-8", "ignore")
            if _f(gmss, 2): a["emitida"] = _timestamp(_f(gmss, 2))
        if 14 in gmsz:
            gmsr = parse_protobuf(_f(parse_protobuf(_f(gmsz, 14)), 1, b"") or b"")
            if 1 in gmsr: a["magnitud"] = round(_float(_f(gmsr, 1)), 1)
            if 2 in gmsr:
                ll = _latlng(_f(gmsr, 2))
                if ll: a["lat"], a["lon"] = ll
            if 3 in gmsr: a["profundidad_km"] = round((_int32(_f(gmsr, 3)) or 0) / 1000.0, 1)
            if 4 in gmsr: a["origin_time"] = _timestamp(_f(gmsr, 4))
            if 5 in gmsr: a["wave_speed_mps"] = _int32(_f(gmsr, 5))
            if 6 in gmsr: a["shaking_duration_s"] = _int32(_f(gmsr, 6))
            if 7 in gmsr: a["fuente_raw"] = _f(gmsr, 7)
            if 8 in gmsr: a["mostrar_magnitud"] = _f(gmsr, 8) == 1
        if 6 in gmsz:
            a["region"] = decodificar_region(_f(gmsz, 6))
        alertas.append(a)
    return alertas


def decodificar_ea_msg(texto):
    """Old `ea.msg` format (gcrr, unpadded web-safe base64)."""
    try:
        b = base64.urlsafe_b64decode(texto + "=" * (-len(texto) % 4))
    except Exception:
        return None
    g = parse_protobuf(b)
    al = parse_protobuf(_f(g, 7, b"") or b"")
    return {"modo_raw": _f(g, 1), "topic": (_f(g, 3) or b"").decode("utf-8", "ignore"),
            "formato_raw": _f(g, 8), "id": (_f(al, 1) or b"").decode("utf-8", "ignore"),
            "tipo_raw": _f(al, 2), "emitida": _timestamp(_f(al, 3)) if _f(al, 3) else None,
            "region_nombre": (_f(al, 5) or b"").decode("utf-8", "ignore"),
            "celdas": [t.decode("ascii", "ignore") for _, t in al.get(6, []) if isinstance(t, bytes)]}


def decodificar_stanza(data):
    """MCS DataMessageStanza -> dict with what matters (the raw copy is saved separately)."""
    s = parse_protobuf(data)
    txt = lambda t: (_f(s, t) or b"").decode("utf-8", "ignore") if isinstance(_f(s, t), bytes) else None
    app = {}
    for _, adb in s.get(7, []):
        ad = parse_protobuf(adb)
        k = (_f(ad, 1) or b"").decode("utf-8", "ignore")
        app[k] = _f(ad, 2) or b""
    raw = _f(s, 21)
    if raw is None and "rawData" in app:
        raw = app["rawData"]
    return {"id": txt(2), "from": txt(3), "category": txt(5), "token": txt(6),
            "persistent_id": txt(9), "stream_id": _f(s, 10), "ttl": _f(s, 17), "sent": _f(s, 18),
            "immediate_ack": _f(s, 24), "app_data": app, "raw_data": raw}


def interpretar(st, nodo_nombre, nodo_info):
    """Turn a decoded stanza into alerts ready to store and show."""
    alertas = []
    if st["raw_data"]:
        alertas = decodificar_gmta(st["raw_data"])
        for a in alertas: a["formato"] = "rawData"
    elif "ea.msg" in st["app_data"]:
        v = decodificar_ea_msg(st["app_data"]["ea.msg"].decode("ascii", "ignore"))
        if v:
            alertas = [{"formato": "ea.msg", "event_id": v["id"], "emitida": v["emitida"],
                        "modo_raw": v["modo_raw"], "modo": MODOS.get(v["modo_raw"], "?"),
                        "tipo_raw": v["tipo_raw"], "tipo": f"legado_{v['tipo_raw']}",
                        "region": {"celdas": v["celdas"], "contornos": [], "nombre": v["region_nombre"]},
                        "topic": v["topic"], "magnitud": None, "lat": None, "lon": None}]
    temas = nodo_info.get("topics", [])
    propia = nodo_info.get("cell_token")
    servicio = st["app_data"].get("gcms", b"").decode("utf-8", "ignore") or None
    for a in alertas:
        a["nodo_receptor"] = nodo_nombre
        a["servicio"] = servicio
        a["from"] = st["from"]
        a["persistent_id"] = st["persistent_id"]
        reg = a.get("region") or {}
        celdas = reg.get("celdas", [])
        toks = [t.split(".")[-1] for t in temas]
        coincide = (next((t for t in toks if any(s2_relacionadas(t, c) for c in celdas)), None) or
                    next((t for t in toks if st["from"] and t in st["from"]), None))
        if not coincide and reg.get("_poligono"):
            cand = {propia: (nodo_info.get("lat"), nodo_info.get("lon"))} if nodo_info.get("lat") is not None else {}
            for t, ll in nodo_info.get("decoy_coords", {}).items():
                cand[t.split(".")[-1]] = tuple(ll)
            if cand:
                tok, dist = celda_mas_cercana(reg["_poligono"], cand)
                if dist <= 100:      # the cell (~35 km) has to touch the polygon
                    coincide, a["distancia_celda_poligono_km"] = tok, dist
        a["celda_coincidente"] = coincide
        a["via"] = ("celda propia" if coincide == propia else "celda señuelo") if coincide else "desconocida"
        zona = nodo_info.get("decoy_names", {}).get(f"ea.{coincide}") if coincide and coincide != propia else None
        if zona: a["zona_senuelo"] = zona
        a["zona"] = zona or (nodo_nombre if a["via"] == "celda propia" else None)
        if a.get("lat") is not None:
            centro = (a["lat"], a["lon"])
        elif reg.get("contornos"):
            centro = _centro_region(reg)
        else:
            centro = (reg.get("poligono") or {}).get("centro")
        a["centro_aprox"] = None if a.get("lat") is not None else centro
        a["distancia_al_nodo_km"] = (distancia_haversine(nodo_info["lat"], nodo_info["lon"], *centro)
                                     if centro and nodo_info.get("lat") is not None and a["via"] != "celda señuelo" else None)
        for c in reg.get("contornos", []): c.pop("_anillos", None)
        reg.pop("_poligono", None)
        a["decodificada"] = a.get("magnitud") is not None and a.get("lat") is not None
        a["is_test"] = a.get("modo") == "prueba" or "tst" in (a.get("topic") or "").split(".")
        a["resumen"] = resumen_alerta(a)
    return alertas


def es_sismo(a):
    """Earthquake by type, or an old-format alert with neither type nor title."""
    return a.get("tipo") == "terremoto" or (a.get("tipo") is None and not a.get("titulo"))


def resumen_alerta(a):
    lugar = (a.get("usgs") or {}).get("lugar") or a.get("nodo_receptor") or "?"
    if not es_sismo(a):
        nombre = a.get("titulo") or a.get("evento") or a.get("tipo") or "?"
        if a.get("area"): nombre += f" · {a['area']}"
        if a.get("emisor") or a.get("categoria"): nombre += f" ({a.get('emisor') or a.get('categoria')})"
        clase = T({"alerta_publica": "publica", "crisis_sos": "sos"}.get(a.get("tipo"), "otra"))
        txt = T("crisis", nombre=nombre, clase=clase)
        if a.get("zona") and a.get("zona") != a.get("area"): txt += T("zona", z=a["zona"])
    elif a.get("magnitud") is not None:
        txt = f"M{a['magnitud']:.1f} · {lugar}"
    elif (a.get("usgs") or {}).get("magnitud") is not None:
        txt = T("posible", m=a["usgs"]["magnitud"], l=lugar)
    else:
        txt = T("sin_mag", l=lugar)
    if a.get("is_test"): txt += T("prueba")
    elif a.get("modo") == "silencioso": txt += T("silenciosa")
    return txt


# ------------------------------------------------------------------------------
# USGS cross-check (alerts without epicentre, or to name the place)
# ------------------------------------------------------------------------------

def buscar_usgs(lat, lon, t_ref, radio_km=600, antes_s=900, despues_s=120):
    q = urllib.parse.urlencode({
        "format": "geojson", "latitude": lat, "longitude": lon, "maxradiuskm": radio_km,
        "starttime": dt.datetime.fromtimestamp(t_ref - antes_s, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"),
        "endtime": dt.datetime.fromtimestamp(t_ref + despues_s, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"),
        "orderby": "time"})
    with urllib.request.urlopen(f"https://earthquake.usgs.gov/fdsnws/event/1/query?{q}", timeout=15) as r:
        feats = json.load(r).get("features", [])
    if not feats: return None
    mejor = min(feats, key=lambda f: abs(f["properties"]["time"] / 1000 - t_ref))
    p, c = mejor["properties"], mejor["geometry"]["coordinates"]
    return {"id": mejor["id"], "magnitud": p.get("mag"), "lugar": p.get("place"),
            "hora_utc": dt.datetime.fromtimestamp(p["time"] / 1000, dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            "lat": c[1], "lon": c[0], "profundidad_km": c[2], "candidatos": len(feats)}


def guardar_deteccion(a):
    try:
        anexar_jsonl(DETECCIONES_LOG, a)
    except Exception as e:
        log(f"Could not save detection to disk: {e}")


def enriquecer(a, nodo_info, reintentos=(60, 300, 900)):
    """Look the quake up in USGS (own thread; the catalogue takes minutes). If found, append
    another line with the same `_clave` (the last one wins when loading)."""
    if not es_sismo(a):
        return
    if a.get("lat") is not None:
        lat, lon = a["lat"], a["lon"]
    elif a.get("centro_aprox"):
        lat, lon = a["centro_aprox"]
    elif a.get("via") in ("celda propia", "desconocida"):
        lat, lon = nodo_info.get("lat"), nodo_info.get("lon")   # assumption: near the receiving node
    else:
        return
    if lat is None: return
    t_ref = a.get("origin_time") or a["timestamp_recepcion"]
    usgs = None
    for espera in (0,) + tuple(reintentos):
        time.sleep(espera)
        try:
            usgs = buscar_usgs(lat, lon, t_ref)
        except Exception as e:
            log(f"USGS did not answer ({a.get('event_id')}): {str(e)[:100]}")
            continue
        if usgs: break
    if not usgs:
        log(f"No USGS match for {a.get('event_id')}")
        return
    b = dict(a, usgs=usgs)
    b["resumen"] = resumen_alerta(b)
    guardar_deteccion(b)
    with LOCK:
        if TODOS.get(b["_clave"]) is a:
            registrar(b)
            if (ESTADO["ultima_novedad"] or {}).get("_clave") == b["_clave"]:
                marcar_novedad(b, ESTADO["ultima_novedad"]["novedad"])
    log(f"USGS: {b['resumen']} ({usgs['id']}, {usgs['hora_utc']} UTC)")


# ==============================================================================
# 3. Telemetry HTTP server (:8998, loopback only)
# ==============================================================================

class TelemetriaHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path in ("/", "/status", "/json"):
            with LOCK:
                vista = dict(ESTADO, eventos_resumen=[{
                    "hora": e.get("timestamp_iso"), "resumen": e.get("resumen"), "event_id": e.get("event_id"),
                    "nodo": e.get("nodo_receptor"), "via": e.get("via"), "decodificada": e.get("decodificada"),
                    "tipo": e.get("tipo"), "sismo": es_sismo(e),
                    "usgs": (e.get("usgs") or {}).get("id")} for e in ESTADO["eventos"]])
                cuerpo = json.dumps(vista, indent=2, ensure_ascii=False, default=str)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(cuerpo.encode("utf-8"))
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        return


def iniciar_servidor_telemetria():
    try:
        servidor = http.server.ThreadingHTTPServer(("127.0.0.1", HTTP_STATUS_PORT), TelemetriaHandler)
        log(f"Telemetry at http://127.0.0.1:{HTTP_STATUS_PORT}/status")
        threading.Thread(target=servidor.serve_forever, daemon=True).start()
    except Exception as e:
        log(f"Could not start telemetry server: {e}")


def _firma(a):
    return (a.get("tipo"), a.get("titulo"), a.get("area"), a.get("evento"), a.get("magnitud"))


def novedad(previo, a):
    """None if `a` is a re-send/update without visible changes; otherwise what is new."""
    if previo is None:
        return "nueva"
    if _firma(previo) == _firma(a):
        return None
    antes = previo.get("titulo") or previo.get("evento") or previo.get("tipo")
    ahora = a.get("titulo") or a.get("evento") or a.get("tipo")
    if antes != ahora:
        return T("antes", x=antes)
    if previo.get("magnitud") != a.get("magnitud"):
        return T("mag_antes", x=previo.get("magnitud"))
    return T("zona_antes", x=previo.get("area")) if previo.get("area") != a.get("area") else T("cambio")


def marcar_novedad(a, texto):
    """Set ESTADO["ultima_novedad"] (LOCK held). The text must change for Home Assistant to log it."""
    r = a["resumen"] if texto == "nueva" else f"{a['resumen']} ({texto})"
    previa = ESTADO["ultima_novedad"]
    if previa and previa.get("resumen_novedad") == r and previa.get("_clave") != a["_clave"]:
        r += f" · {a.get('event_id')}"          # two different alerts with the same text
    ESTADO["ultima_novedad"] = dict(a, novedad=texto, resumen_novedad=r)


def registrar(a):
    """Insert/update an alert and recompute counters and the recent list (LOCK held)."""
    TODOS[a["_clave"]] = a
    orden = sorted(TODOS.values(), key=lambda e: e.get("timestamp_recepcion") or 0, reverse=True)
    sismos = [e for e in orden if es_sismo(e)]
    ESTADO["total_alertas_recibidas"] = len(orden)
    ESTADO["total_sismos"] = len(sismos)
    ESTADO["eventos"] = orden[:MAX_EVENTOS]
    ESTADO["ultima_alerta"] = orden[0] if orden else None
    ESTADO["ultimo_sismo"] = sismos[0] if sismos else None


def cargar_detecciones_previas():
    """Restore counters, latest alert and recent events after a restart."""
    if not os.path.isfile(DETECCIONES_LOG): return
    filas = []
    with open(DETECCIONES_LOG) as f:
        for linea in f:
            try: filas.append(json.loads(linea))
            except ValueError: pass
    TODOS.clear()
    with LOCK:
        for a in filas:                  # the last line of each event wins
            a["_clave"] = a.get("event_id")
            a["resumen"] = resumen_alerta(a)
            nov = novedad(TODOS.get(a["_clave"]), a)
            registrar(a)
            if nov: marcar_novedad(a, nov)


# ==============================================================================
# 4. MCS fleet manager
# ==============================================================================

def cargar_vistos():
    try:
        with open(VISTOS_FILE) as f: return list(json.load(f))
    except (OSError, ValueError, TypeError):
        return []


def guardar_vistos(vistos):
    tmp = VISTOS_FILE + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f: json.dump(vistos[-500:], f)
    os.replace(tmp, VISTOS_FILE)


def huella(st, data):
    """Identity of a message, to process it once. persistent_id alone is NOT enough:
    every crisis alert arrives with persistent_id = "CRISIS"."""
    base = st["raw_data"] if st["raw_data"] else data
    return f"{st['persistent_id']}|{st['sent']}|{hashlib.sha256(base).hexdigest()[:24]}"


class NodoFlota:
    def __init__(self, nombre, info):
        self.nombre = nombre
        self.info = info
        self.android_id = info["android_id"]
        self.security_token = info["security_token"]
        self.topics = info["topics"]  # own cell + 3 decoys
        self.sock = None
        self.buf = bytearray()
        self.conectado = False
        self.stream_in = 0            # frames received this session (for last_stream_id_received)
        self.ultimo_ping = 0
        self.ultimo_rx = 0
        self.ultimo_latido = None
        self.latidos = 0
        self.mensajes = 0
        self.reconexiones = 0
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
            field_bytes(8, setting)
        )
        login_req += field_varint(14, 1) + field_varint(16, 2) + field_varint(17, 1)
        for t in self.topics:
            login_req += field_str(29, t)
        return bytes([MCS_VERSION, TAG_LOGIN_REQUEST]) + encode_varint(len(login_req)) + login_req

    def conectar(self):
        try:
            raw = socket.create_connection((HOST, PORT), timeout=10)
            s = ssl.create_default_context().wrap_socket(raw, server_hostname=HOST)
            s.settimeout(10)
            s.sendall(self._login_packet())
            self.sock = s
            self.buf = bytearray()
            limite = time.time() + 15
            while len(self.buf) < 1:
                if not self._recv_bloqueante(limite): raise ConnectionError("sin versión")
            if self.buf[0] != MCS_VERSION: raise ConnectionError(f"versión MCS {self.buf[0]}")
            del self.buf[:1]
            self.stream_in = 0
            tramas = []
            while True:
                tramas += list(self.tramas())
                if any(t == TAG_LOGIN_RESPONSE for t, _ in tramas): break
                if not self._recv_bloqueante(limite): raise ConnectionError("sin LoginResponse")
            resp = parse_protobuf(next(p for t, p in tramas if t == TAG_LOGIN_RESPONSE))
            if 3 in resp:
                raise ConnectionError(f"login rechazado: {arbol_protobuf(_f(resp, 3))}")
            s.setblocking(False)
            self.conectado = True
            self.ultimo_ping = self.ultimo_rx = time.time()
            self.ultimo_latido = ahora_iso()
            self.backoff = 2
            # Frames that came right after the LoginResponse (e.g. pending messages)
            self.pendientes = [(t, p) for t, p in tramas if t != TAG_LOGIN_RESPONSE]
            return True
        except Exception as e:
            log(f"Could not connect {self.nombre}: {e}")
            self.desconectar()
            return False

    def _recv_bloqueante(self, limite):
        if time.time() > limite: return False
        chunk = self.sock.recv(65536)
        if not chunk: return False
        self.buf += chunk
        return True

    def leer_disponible(self):
        """Read everything available (including what TLS already decrypted). False = closed."""
        while True:
            try:
                chunk = self.sock.recv(65536)
            except (ssl.SSLWantReadError, BlockingIOError):
                return True
            if not chunk:
                return False
            self.buf += chunk
            self.ultimo_rx = time.time()

    def tramas(self):
        """Extrae tramas completas (tag, payload) del buffer; deja lo incompleto."""
        while len(self.buf) >= 2:
            tag = self.buf[0]
            ln, p = leer_varint(self.buf, 1)
            if ln is None: return
            if ln > TRAMA_MAX: raise ConnectionError(f"trama de {ln} B")
            if len(self.buf) < p + ln: return
            payload = bytes(self.buf[p:p + ln])
            del self.buf[:p + ln]
            self.stream_in += 1
            yield tag, payload

    def desconectar(self):
        self.conectado = False
        if self.sock:
            try: self.sock.close()
            except Exception: pass
            self.sock = None
        self.buf = bytearray()
        self.proximo_intento = time.time() + self.backoff
        self.backoff = min(self.backoff * 2, 60)

    def _enviar(self, tag, cuerpo):
        if not self.sock: return
        try:
            self.sock.setblocking(True)
            self.sock.settimeout(10)
            self.sock.sendall(bytes([tag]) + encode_varint(len(cuerpo)) + cuerpo)
        except Exception:
            self.desconectar()
        else:
            self.sock.setblocking(False)

    def enviar_pong(self):
        self._enviar(TAG_HEARTBEAT_ACK, field_varint(2, self.stream_in))

    def enviar_ping(self):
        self._enviar(TAG_HEARTBEAT_PING, field_varint(2, self.stream_in))
        self.ultimo_ping = time.time()

    def confirmar(self, persistent_id):
        """SelectiveAck (IqStanza SET, extension 12) so Google does not re-send it."""
        ext = field_varint(1, EXT_SELECTIVE_ACK) + field_bytes(2, field_str(1, persistent_id))
        iq = field_varint(2, IQ_SET) + field_str(3, "") + field_bytes(7, ext) + field_varint(10, self.stream_in)
        self._enviar(TAG_IQ_STANZA, iq)

    def latido(self):
        self.latidos += 1
        self.ultimo_latido = ahora_iso()


class GestorFlota:
    def __init__(self, fleet_data):
        self.nodos = [NodoFlota(nom, info) for nom, info in fleet_data.items()]
        self.mapa_socks = {}
        self.vistos = cargar_vistos()
        ESTADO["flota_tamano"] = len(self.nodos)
        self.actualizar_estado()

    def _conectar(self, nodo):
        if not nodo.conectar(): return False
        self.mapa_socks[nodo.sock] = nodo
        for tag, payload in nodo.pendientes:
            self.manejar_trama(nodo, tag, payload)
        return True

    def conectar_todos(self):
        log(f"Connecting the fleet ({len(self.nodos)} synthetic Pixels)...")
        conectados = 0
        for nodo in self.nodos:
            if self._conectar(nodo):
                conectados += 1
                time.sleep(0.05)
        self.actualizar_estado()
        log(f"✓ Fleet online: {conectados}/{len(self.nodos)} nodes connected.")

    def actualizar_estado(self):
        with LOCK:
            conectados = sum(1 for n in self.nodos if n.conectado)
            ESTADO["conectado"] = (conectados > 0)
            ESTADO["nodos_conectados"] = conectados
            ESTADO["porcentaje_conectado"] = f"{(conectados / len(self.nodos) * 100):.0f}%"
            latidos = [n.ultimo_latido for n in self.nodos if n.ultimo_latido]
            ESTADO["ultimo_latido_global"] = max(latidos) if latidos else None
            for n in self.nodos:
                ESTADO["nodos"][n.nombre] = {
                    "android_id": n.android_id,
                    "celda": n.info.get("cell_token"),
                    "topico_principal": n.info.get("primary_topic"),
                    "senuelos": [f"{n.info.get('decoy_names', {}).get(d, '?')} ({d.split('.')[-1]})"
                                 for d in n.info.get("decoys", [])],
                    "conectado": n.conectado,
                    "latidos": n.latidos,
                    "mensajes": n.mensajes,
                    "reconexiones": n.reconexiones,
                    "ultimo_latido": n.ultimo_latido
                }

    def _caer(self, nodo, motivo):
        if nodo.sock: self.mapa_socks.pop(nodo.sock, None)
        nodo.desconectar()
        log(f"Node down: {nodo.nombre} ({motivo})")
        self.actualizar_estado()

    def manejar_trama(self, nodo, tag, payload):
        if tag == TAG_HEARTBEAT_PING:
            nodo.enviar_pong(); nodo.latido()
        elif tag == TAG_HEARTBEAT_ACK:
            nodo.latido()
        elif tag == TAG_DATA_MESSAGE_STANZA:
            nodo.latido()
            self.procesar_mensaje(payload, nodo)
        elif tag == TAG_CLOSE:
            self._caer(nodo, "Close del servidor")
            return
        self.actualizar_estado()

    def bucle_escucha(self):
        log("Fleet active. Entering the multiplexed event loop...")
        while True:
            try:
                ahora = time.time()
                for nodo in self.nodos:
                    if not nodo.conectado and ahora >= nodo.proximo_intento:
                        if self._conectar(nodo):
                            nodo.reconexiones += 1
                            self.actualizar_estado()
                            log(f"Node reconnected: {nodo.nombre}")
                    elif nodo.conectado:
                        if ahora - nodo.ultimo_ping > PING_CADA_S:
                            nodo.enviar_ping()
                        if nodo.ultimo_rx < nodo.ultimo_ping and ahora - nodo.ultimo_ping > SIN_RESPUESTA_S:
                            self._caer(nodo, "sin respuesta al ping")

                for s, n in list(self.mapa_socks.items()):
                    if n.sock is not s: self.mapa_socks.pop(s, None)   # sockets of old sessions
                lista_socks = [s for s, n in self.mapa_socks.items() if n.conectado]
                if not lista_socks:
                    time.sleep(1)
                    continue

                rlist, _, _ = select.select(lista_socks, [], [], 2.0)
                for s in rlist:
                    nodo = self.mapa_socks.get(s)
                    if not nodo or not nodo.conectado: continue
                    try:
                        vivo = nodo.leer_disponible()
                        for tag, payload in nodo.tramas():
                            self.manejar_trama(nodo, tag, payload)
                            if not nodo.conectado: break
                        if not vivo and nodo.conectado:
                            self._caer(nodo, "socket cerrado")
                    except Exception as e:
                        if nodo.conectado: self._caer(nodo, f"error: {e}")
            except Exception as e:
                log(f"Error in fleet loop: {e}")
                time.sleep(2)

    def procesar_mensaje(self, data, nodo):
        nodo.mensajes += 1
        with LOCK:
            ESTADO["mensajes_recibidos"] += 1
        st = decodificar_stanza(data)
        pid = st["persistent_id"]
        h = huella(st, data)
        repetido = h in self.vistos

        # 1) ALWAYS save the raw message before interpreting anything
        registro = {"timestamp_recepcion": time.time(), "timestamp_iso": ahora_iso(), "nodo": nodo.nombre,
                    "bytes": len(data), "repetido": repetido,
                    "stanza_b64": base64.b64encode(data).decode(),
                    "arbol": arbol_protobuf(data),
                    "raw_data_arbol": arbol_protobuf(st["raw_data"]) if st["raw_data"] else None}
        try:
            anexar_jsonl(PAYLOADS_LOG, registro)
        except Exception as e:
            log(f"Could not save raw payload: {e}")
        if pid:
            nodo.confirmar(pid)
        if repetido:
            log(f"Ignoring re-sent message ({nodo.nombre}, pid={pid})")
            return
        self.vistos.append(h)
        try: guardar_vistos(self.vistos)
        except Exception as e: log(f"Could not save fingerprints: {e}")

        claves = ", ".join(sorted(st["app_data"]))
        log(f"Message for {nodo.nombre}: cat={st['category']} from={st['from']} pid={pid} {len(data)} B "
            f"app_data=[{claves}] rawData={len(st['raw_data']) if st['raw_data'] else 0} B")

        try:
            alertas = interpretar(st, nodo.nombre, nodo.info)
        except Exception as e:
            log(f"Could not interpret the message (raw copy kept in {os.path.basename(PAYLOADS_LOG)}): {e}")
            return
        for a in alertas:
            a["timestamp_recepcion"] = registro["timestamp_recepcion"]
            a["timestamp_iso"] = registro["timestamp_iso"]
            a["_clave"] = a.get("event_id")
            with LOCK:
                previo = TODOS.get(a["_clave"])
                if previo and previo.get("timestamp_recepcion"):   # update: keep the first reception time
                    a["primera_recepcion"] = previo.get("primera_recepcion") or previo.get("timestamp_iso")
                nov = novedad(previo, a)
                registrar(a)
                if nov: marcar_novedad(a, nov)
            if nov:
                log(f"{'🌋' if es_sismo(a) else '⚠️'} ALERT {a['tipo']}/{a['modo']} [{nov}]: {a['resumen']} | "
                    f"id={a.get('event_id')} | via {a['via']} ({a.get('celda_coincidente')}) | "
                    f"distance to node {a.get('distancia_al_nodo_km')} km")
            else:
                log(f"Update without visible changes: {a.get('event_id')} ({a.get('titulo') or a['tipo']})")
            guardar_deteccion(a)
            threading.Thread(target=enriquecer, args=(a, nodo.info), daemon=True).start()


# ==============================================================================
# 5. Re-interpreting saved payloads
# ==============================================================================

def decodificar_archivo(ruta, fleet_data):
    with open(ruta) as f:
        for linea in f:
            r = json.loads(linea)
            data = base64.b64decode(r["stanza_b64"])
            st = decodificar_stanza(data)
            info = fleet_data.get(r["nodo"], {})
            print(f"=== {r['timestamp_iso']} {r['nodo']} ({r['bytes']} B) cat={st['category']} "
                  f"from={st['from']} pid={st['persistent_id']} ttl={st['ttl']}")
            print("  app_data:", {k: (v[:60].decode('utf-8', 'replace') if k != 'rawData' else f'{len(v)} B')
                                  for k, v in st["app_data"].items()})
            for a in interpretar(st, r["nodo"], info):
                print("  alerta:", json.dumps(a, ensure_ascii=False, default=str))
            if st["raw_data"]:
                print("  rawData decode_raw:", json.dumps(arbol_protobuf(st["raw_data"]), ensure_ascii=False))


def reconstruir_detecciones(fleet_data):
    """Rebuild DETECCIONES_LOG from PAYLOADS_LOG with the current decoder. Keeps the first
    reception time and USGS match of old lines, and alerts that have no raw copy."""
    viejas = {}
    if os.path.isfile(DETECCIONES_LOG):
        with open(DETECCIONES_LOG) as f:
            for linea in f:
                try: a = json.loads(linea)
                except ValueError: continue
                v = viejas.setdefault(a.get("event_id"), a)
                if (a.get("timestamp_recepcion") or 0) < (v.get("timestamp_recepcion") or 0): v.update(
                    timestamp_recepcion=a["timestamp_recepcion"], timestamp_iso=a.get("timestamp_iso"))
                if a.get("usgs"): v["usgs"] = a["usgs"]
    nuevas = {}
    with open(PAYLOADS_LOG) as f:
        for linea in f:
            r = json.loads(linea)
            st = decodificar_stanza(base64.b64decode(r["stanza_b64"]))
            for a in interpretar(st, r["nodo"], fleet_data.get(r["nodo"], {})):
                a["_clave"] = a["event_id"]
                a["timestamp_recepcion"], a["timestamp_iso"] = r["timestamp_recepcion"], r["timestamp_iso"]
                v = nuevas.get(a["_clave"]) or viejas.get(a["_clave"])
                if v and (v.get("timestamp_recepcion") or 1e12) < a["timestamp_recepcion"]:
                    a["timestamp_recepcion"], a["timestamp_iso"] = v["timestamp_recepcion"], v["timestamp_iso"]
                if v and v.get("usgs") and es_sismo(a): a["usgs"] = v["usgs"]
                a["resumen"] = resumen_alerta(a)
                nuevas[a["_clave"]] = a
    filas = [v for k, v in viejas.items() if k not in nuevas] + list(nuevas.values())
    filas.sort(key=lambda a: a.get("timestamp_recepcion") or 0)
    tmp = DETECCIONES_LOG + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        for a in filas: f.write(json.dumps(a, ensure_ascii=False) + "\n")
    os.replace(tmp, DETECCIONES_LOG)
    for a in filas: print(f"{a.get('timestamp_iso')}  {a.get('event_id')}  {a.get('resumen')}")


def main():
    if "--generate-fleet" in sys.argv:
        generar_flota(force="--force" in sys.argv)
        return

    if not os.path.isfile(FLEET_FILE):
        # Registering identities talks to Google, so it only happens on an explicit --generate-fleet
        if "--decodificar" in sys.argv or "--reconstruir" in sys.argv:
            fleet_data = {}
        else:
            log(f"No fleet file at {FLEET_FILE}. Create it with: {sys.argv[0]} --generate-fleet")
            sys.exit(1)
    else:
        with open(FLEET_FILE, "r") as f:
            fleet_data = json.load(f)

    if "--update-decoys" in sys.argv:
        guardar_flota(asignar_senuelos(fleet_data))
        for nombre, v in fleet_data.items():
            log(f"  {nombre}: {', '.join(v.get('decoy_names', {}).values())}")
        return

    if "--decodificar" in sys.argv:
        i = sys.argv.index("--decodificar")
        decodificar_archivo(sys.argv[i + 1] if len(sys.argv) > i + 1 else PAYLOADS_LOG, fleet_data)
        return

    if "--reconstruir" in sys.argv:
        reconstruir_detecciones(fleet_data)
        return

    if "--test-connection" in sys.argv:
        gestor = GestorFlota(fleet_data)
        gestor.conectar_todos()
        time.sleep(2)
        for s in list(gestor.mapa_socks.keys()):
            try: s.close()
            except Exception: pass
        log("✓ Fleet test finished.")
        return

    cargar_detecciones_previas()
    iniciar_servidor_telemetria()
    gestor = GestorFlota(fleet_data)
    gestor.conectar_todos()
    gestor.bucle_escucha()


if __name__ == "__main__":
    main()
