#!/usr/bin/env python3
"""Offline tests for tools/radar_sismos_global.py (the global fleet of Google alert receivers).

Run: python -m unittest discover -s tests   (also works next to a deployed copy of the radar)
"""
import base64
import importlib.util
import json
import os
import struct
import sys
import tempfile
import unittest

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUTA = next(p for p in (os.path.join(RAIZ, "tools", "radar_sismos_global.py"),
                        os.path.join(RAIZ, "radar_sismos_global.py")) if os.path.isfile(p))
spec = importlib.util.spec_from_file_location("radar", RUTA)
radar = importlib.util.module_from_spec(spec)
spec.loader.exec_module(radar)
radar.LANG = "es"      # the expectations below use the Spanish summaries (RADAR_LANG=es)
DATOS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")

fv, fd, ff, fb, fs = radar.field_varint, radar.field_double, radar.field_float, radar.field_bytes, radar.field_str

NODO = {"lat": 7.575, "lon": -80.951, "cell_token": "8fb1f",
        "topics": ["ea.8fb1f", "ea.14cab", "ea.32f91", "ea.9105d"],
        "decoys": ["ea.14cab", "ea.32f91", "ea.9105d"], "decoy_names": {"ea.14cab": "Estambul"}}


def latlng(lat, lon): return fd(1, lat) + fd(2, lon)


def gmta_sintetico(modo=0, tipo=5, con_quake=True, celdas=("8fb1c",)):
    alert_id = fs(1, "pa:224397301") + fb(2, fv(1, 1791573700) + fv(2, 5))
    anillo = fb(1, latlng(7.0, -81.5)) + fb(1, latlng(8.0, -81.5)) + fb(1, latlng(7.5, -80.5))
    circulo = fb(1, latlng(7.6, -81.0)) + fv(2, 45000)
    contorno = fv(1, 1) + fb(2, anillo) + fb(3, circulo)
    region = fb(1, b"".join(fs(1, c) for c in celdas)) + fb(2, fb(1, contorno))
    gmsz = fb(1, alert_id) + fv(2, modo) + fv(3, tipo) + fb(6, region)
    if con_quake:
        gmsr = (ff(1, 5.04) + fb(2, latlng(7.6086, -81.0415)) + fv(3, 25962) +
                fb(4, fv(1, 1791573442)) + fv(5, 3500) + fv(6, 12) + fv(7, 2) + fv(8, 1))
        gmsz += fb(14, fb(1, gmsr))
    return fb(2, gmsz)


def stanza(raw=None, app=(), pid="0:1791573719%abc", origen="/topics/ea.8fb1f"):
    s = fs(2, "msgid") + fs(3, origen) + fs(5, "com.google.android.gms")
    for k, v in app:
        s += fb(7, fs(1, k) + fb(2, v))
    s += fs(9, pid) + fv(17, 600)
    if raw is not None:
        s += fb(21, raw)
    return s


class TestProtobuf(unittest.TestCase):
    def test_estricto_rechaza_basura(self):
        self.assertIsNone(radar.parse_estricto(b"\x0a\x10abc"))   # longitud > datos
        self.assertEqual(radar.parse_estricto(fv(1, 300)), [(1, 0, 300)])

    def test_arbol(self):
        arbol = radar.arbol_protobuf(fb(1, fs(1, "hola") + fv(2, 7)))
        self.assertEqual(arbol, [{"campo": 1, "msg": [{"campo": 1, "str": "hola"}, {"campo": 2, "varint": 7}]}])

    def test_int32_negativo(self):
        self.assertEqual(radar._int32((1 << 64) - 5), -5)


class TestDecodificacion(unittest.TestCase):
    def test_alerta_completa(self):
        st = radar.decodificar_stanza(stanza(gmta_sintetico()))
        self.assertEqual(st["category"], "com.google.android.gms")
        self.assertEqual(st["persistent_id"], "0:1791573719%abc")
        a, = radar.interpretar(st, "Panama - Golfo de Montijo", NODO)
        self.assertEqual(a["event_id"], "pa:224397301")
        self.assertEqual(a["tipo"], "terremoto")
        self.assertEqual(a["modo"], "normal")
        self.assertEqual(a["magnitud"], 5.0)
        self.assertEqual((a["lat"], a["lon"]), (7.6086, -81.0415))
        self.assertEqual(a["profundidad_km"], 26.0)
        self.assertEqual(a["origin_time"], 1791573442)
        self.assertAlmostEqual(a["emitida"], 1791573700.000000005)
        self.assertTrue(a["mostrar_magnitud"])
        self.assertTrue(a["decodificada"])
        self.assertEqual(a["via"], "celda propia")
        self.assertEqual(a["celda_coincidente"], "8fb1f")
        self.assertLess(a["distancia_al_nodo_km"], 20)
        c, = a["region"]["contornos"]
        self.assertEqual(c["nivel"], "MMI5+")
        self.assertEqual(c["vertices"], 3)
        self.assertEqual(c["circulos"][0]["radio_km"], 45.0)
        self.assertNotIn("_anillos", c)
        json.dumps(a)   # serializable

    def test_sin_quake_usa_centro_de_region(self):
        st = radar.decodificar_stanza(stanza(gmta_sintetico(con_quake=False)))
        a, = radar.interpretar(st, "Panama - Golfo de Montijo", NODO)
        self.assertIsNone(a["magnitud"])
        self.assertFalse(a["decodificada"])
        self.assertIsNotNone(a["centro_aprox"])
        self.assertIn("sin magnitud", a["resumen"])
        self.assertEqual(a["campos_presentes"], [1, 2, 3, 6])

    def test_prueba_y_silenciosa(self):
        a, = radar.interpretar(radar.decodificar_stanza(stanza(gmta_sintetico(modo=2))), "x", NODO)
        self.assertTrue(a["is_test"])
        self.assertIn("[prueba]", a["resumen"])
        a, = radar.interpretar(radar.decodificar_stanza(stanza(gmta_sintetico(modo=1))), "x", NODO)
        self.assertEqual(a["modo"], "silencioso")

    def test_senuelo(self):
        st = radar.decodificar_stanza(stanza(gmta_sintetico(celdas=("14caac",))))
        self.assertEqual(radar.interpretar(st, "x", NODO)[0]["celda_coincidente"], "14cab")
        st = radar.decodificar_stanza(stanza(gmta_sintetico(celdas=("14caac",)), origen="google.com"))
        a, = radar.interpretar(st, "Panama - Golfo de Montijo", NODO)
        self.assertEqual(a["via"], "celda señuelo")
        self.assertEqual(a["zona_senuelo"], "Estambul")

    def test_rawdata_en_app_data(self):
        st = radar.decodificar_stanza(stanza(app=[("rawData", gmta_sintetico())]))
        self.assertEqual(len(radar.interpretar(st, "x", NODO)), 1)

    def test_ea_msg(self):
        gcrr = fv(1, 0) + fs(3, "ea.tst.8fb1f") + fb(7, fs(1, "arw:1") + fv(2, 3) + fs(5, "Zona") + fs(6, "8fb1f")) + fv(8, 4)
        txt = base64.urlsafe_b64encode(gcrr).decode().rstrip("=")
        a, = radar.interpretar(radar.decodificar_stanza(stanza(app=[("ea.msg", txt.encode())])), "x", NODO)
        self.assertEqual(a["event_id"], "arw:1")
        self.assertTrue(a["is_test"])
        self.assertEqual(a["region"]["nombre"], "Zona")

    def test_mensaje_no_sismo(self):
        st = radar.decodificar_stanza(stanza(app=[("otra", b"cosa")]))
        self.assertEqual(radar.interpretar(st, "x", NODO), [])


class TestCrisisReal(unittest.TestCase):
    """rawData real recibido el 2026-10-09 por el nodo Guerrero: alerta de crisis "Huracán Simon"."""

    def setUp(self):
        with open(os.path.join(DATOS, "crisis_hurricane_simon_rawdata.b64")) as f:
            self.raw = base64.b64decode(f.read())

    def test_no_es_sismo(self):
        st = radar.decodificar_stanza(stanza(self.raw, app=[("gcms", b"crisisalerts"), ("wake", b"1")], pid="CRISIS",
                                             origen="745476177629"))
        a, = radar.interpretar(st, "Mexico - Guerrero", NODO)
        self.assertEqual(a["event_id"], "cmid:610b3195d52aaa9e")
        self.assertEqual(a["tipo"], "crisis_sos")
        self.assertEqual(a["servicio"], "crisisalerts")
        self.assertEqual(a["titulo"], "Huracán Simon")
        self.assertEqual(a["titulo_en"], "Hurricane Simon")
        self.assertEqual(a["idiomas"], 24)
        self.assertEqual(a["resumen"], "Huracán Simon (SOS_ALERT) — alerta SOS de Google, no sismo")
        self.assertEqual(a["via"], "desconocida")     # ninguna celda de NODO (Panamá) toca el polígono

    def test_english_summary(self):
        st = radar.decodificar_stanza(stanza(self.raw, app=[("gcms", b"crisisalerts")], pid="CRISIS",
                                             origen="745476177629"))
        radar.LANG = "en"
        try:
            a, = radar.interpretar(st, "Mexico - Guerrero", NODO)
        finally:
            radar.LANG = "es"
        self.assertEqual(a["resumen"], "Huracán Simon (SOS_ALERT) — Google SOS alert, not an earthquake")

    def test_poligono_real(self):
        # Valores de referencia: librería oficial s2geometry (22 vértices, caja lat 15.52..22.54, lon -107.92..-98.52)
        nodo = {"lat": 16.8531, "lon": -99.8237, "cell_token": "85ca5", "topics": ["ea.85ca5", "ea.4bc4d"],
                "decoys": ["ea.4bc4d"], "decoy_names": {"ea.4bc4d": "Popayán"}, "decoy_coords": {"ea.4bc4d": [2.44, -76.61]}}
        st = radar.decodificar_stanza(stanza(self.raw, app=[("gcms", b"crisisalerts")], pid="CRISIS"))
        a, = radar.interpretar(st, "Mexico - Guerrero", nodo)
        pol = a["region"]["poligono"]
        self.assertEqual(pol["vertices"], 22)
        caja = pol["caja"]
        self.assertAlmostEqual(caja[0], 15.52, delta=0.01); self.assertAlmostEqual(caja[1], -107.92, delta=0.01)
        self.assertAlmostEqual(caja[2], 22.54, delta=0.01); self.assertAlmostEqual(caja[3], -98.52, delta=0.01)
        self.assertEqual(a["via"], "celda propia")
        self.assertEqual(a["distancia_celda_poligono_km"], 0.0)
        self.assertNotIn("_poligono", a["region"])
        self.assertIn("zona Mexico - Guerrero", a["resumen"])
        self.assertEqual(a["categoria"], "SOS_ALERT")
        self.assertEqual(a["duracion_s"], 259200)
        self.assertIsNone(a["magnitud"])
        self.assertFalse(radar.es_sismo(a))
        self.assertGreater(a["region"]["s2polygon_bytes"], 100)
        self.assertIn("no sismo", a["resumen"])

    def test_huella_no_depende_solo_del_pid(self):
        a = radar.decodificar_stanza(stanza(self.raw, pid="CRISIS"))
        b = radar.decodificar_stanza(stanza(gmta_sintetico(tipo=2, con_quake=False), pid="CRISIS"))
        self.assertNotEqual(radar.huella(a, b""), radar.huella(b, b""))


class TestAlertaPublica(unittest.TestCase):
    def test_flood_watch(self):
        # Misma forma que el pa:224397301 recibido el 2026-10-09 (NOAA, Idaho), armado a mano
        gmsz = (fb(1, fs(1, "pa:224397301") + fb(2, fv(1, 1791573300))) + fv(2, 3) + fv(3, 1) +
                fb(5, fv(1, 1791633600)) + fb(6, fb(3, b"\x04\x13\x01")) +
                fb(7, fs(1, "FLOOD") + fb(2, fv(1, 259200))) + fs(8, "kgmid=/g/11p1qgpjq2&wbsk=1") +
                fb(9, fs(1, "en") + fb(2, fs(1, "Flood Watch") + fs(2, "Idaho") + fs(3, "National Weather Service"))) +
                fb(12, fs(1, "NOAA") + fv(2, 12)))
        st = radar.decodificar_stanza(stanza(fb(2, gmsz), app=[("gcms", b"crisisalerts")], pid="CRISIS"))
        a, = radar.interpretar(st, "Panama - Golfo de Montijo", NODO)
        self.assertEqual(a["tipo"], "alerta_publica")
        self.assertEqual(a["evento"], "FLOOD")
        self.assertEqual((a["titulo"], a["area"], a["emisor"]), ("Flood Watch", "Idaho", "National Weather Service"))
        self.assertEqual(a["categoria"], "NOAA")
        self.assertFalse(radar.es_sismo(a))
        self.assertEqual(a["resumen"], "Flood Watch · Idaho (National Weather Service) — alerta pública de Google, no sismo"
                         " · zona Panama - Golfo de Montijo")   # el `from` del dato de prueba apunta a la celda propia


class TestNovedad(unittest.TestCase):
    def setUp(self):
        radar.TODOS.clear(); radar.ESTADO["ultima_novedad"] = None

    def _alerta(self, clave, titulo, **kw):
        a = dict({"_clave": clave, "event_id": clave, "tipo": "crisis_sos", "titulo": titulo, "timestamp_recepcion": 1}, **kw)
        a["resumen"] = radar.resumen_alerta(a)
        return a

    def _recibir(self, a):
        nov = radar.novedad(radar.TODOS.get(a["_clave"]), a)
        radar.registrar(a)
        if nov: radar.marcar_novedad(a, nov)
        return nov

    def test_reenvio_no_es_novedad(self):
        self.assertEqual(self._recibir(self._alerta("cmid:1", "Huracán Isaias")), "nueva")
        self.assertIsNone(self._recibir(self._alerta("cmid:1", "Huracán Isaias", expira=99)))
        self.assertEqual(self._recibir(self._alerta("cmid:1", "Ciclón Post-Tropical Isaias")), "antes: Huracán Isaias")
        self.assertTrue(radar.ESTADO["ultima_novedad"]["resumen_novedad"].endswith("(antes: Huracán Isaias)"))

    def test_otra_alerta_reenviada_no_mueve_la_novedad(self):
        self._recibir(self._alerta("cmid:1", "Huracán Simon"))
        self._recibir(self._alerta("pa:2", "Flood Watch", tipo="alerta_publica", area="Idaho"))
        self._recibir(self._alerta("cmid:1", "Huracán Simon", expira=5))       # reenvío de Simon
        self.assertEqual(radar.ESTADO["ultima_novedad"]["event_id"], "pa:2")

    def test_mismo_texto_distinta_alerta(self):
        self._recibir(self._alerta("pa:1", "Flood Watch", tipo="alerta_publica", area="Tennessee"))
        self._recibir(self._alerta("pa:2", "Flood Watch", tipo="alerta_publica", area="Tennessee"))
        self.assertTrue(radar.ESTADO["ultima_novedad"]["resumen_novedad"].endswith("· pa:2"))


class TestS2(unittest.TestCase):
    def test_jerarquia(self):
        self.assertTrue(radar.s2_relacionadas("8fb1f", "8fb1c"))     # hija nivel 9
        self.assertTrue(radar.s2_relacionadas("8fb1f", "8fb1"))      # padre nivel 7
        self.assertTrue(radar.s2_relacionadas("8fb1f", "8fb1f"))
        self.assertFalse(radar.s2_relacionadas("8fb1f", "8fb1b"))    # vecina
        self.assertFalse(radar.s2_relacionadas("8fb1f", "zz"))


class NodoFalso(radar.NodoFlota):
    def __init__(self):
        super().__init__("n", {"android_id": 1, "security_token": 2, "topics": []})


class TestTramas(unittest.TestCase):
    def test_trama_partida(self):
        n = NodoFalso()
        payload = stanza(gmta_sintetico())
        trama = bytes([8]) + radar.encode_varint(len(payload)) + payload + bytes([0, 0])
        salida = []
        for i in range(0, len(trama), 7):      # llega en pedazos de 7 bytes
            n.buf += trama[i:i + 7]
            salida += list(n.tramas())
        self.assertEqual(salida, [(8, payload), (0, b"")])
        self.assertEqual(n.stream_in, 2)
        self.assertEqual(n.buf, bytearray())


class TestPersistencia(unittest.TestCase):
    def test_cargar_previas_dedup(self):
        with tempfile.TemporaryDirectory() as d:
            ruta = os.path.join(d, "det.jsonl")
            filas = [{"event_id": "pa:1", "magnitud": None, "nodo_receptor": "A", "timestamp_recepcion": 1},
                     {"event_id": "cmid:2", "magnitud": None, "nodo_receptor": "B", "timestamp_recepcion": 2},
                     {"event_id": "pa:1", "_clave": "pa:1", "magnitud": None, "nodo_receptor": "A",
                      "timestamp_recepcion": 1, "usgs": {"magnitud": 4.8, "lugar": "Tebario"}}]
            with open(ruta, "w") as f:
                f.write("\n".join(json.dumps(x) for x in filas) + "\n")
            orig = radar.DETECCIONES_LOG
            radar.DETECCIONES_LOG = ruta
            try:
                radar.cargar_detecciones_previas()
            finally:
                radar.DETECCIONES_LOG = orig
            self.assertEqual(radar.ESTADO["total_alertas_recibidas"], 2)
            self.assertEqual(radar.ESTADO["total_sismos"], 2)
            self.assertEqual(radar.ESTADO["ultima_alerta"]["event_id"], "cmid:2")
            pa = next(e for e in radar.ESTADO["eventos"] if e["event_id"] == "pa:1")
            self.assertEqual(pa["resumen"], "Posible M4.8 (USGS) · Tebario")
            radar.registrar(dict(radar.TODOS["pa:1"], tipo="alerta_publica", titulo="Flood Watch"))
            self.assertEqual(radar.ESTADO["total_alertas_recibidas"], 2)
            self.assertEqual(radar.ESTADO["total_sismos"], 1)



if __name__ == "__main__":
    unittest.main()


class TestFlotaSenuelos(unittest.TestCase):
    def test_66_senuelos_distintos_y_con_nombre(self):
        flota = {h["nombre"]: {"primary_topic": "ea.x"} for h in radar.HOTSPOTS}
        radar.asignar_senuelos(flota)
        toks = [t for v in flota.values() for t in v["decoys"]]
        self.assertEqual(len(toks), 66)
        self.assertEqual(len(set(toks)), 66)
        med = flota["Colombia - Medellin"]
        self.assertEqual(med["decoy_names"], {"ea.88d9b": "Miami", "ea.8f4c3": "Cancún", "ea.0d605": "Valencia"})
        self.assertEqual(med["topics"][0], "ea.x")

    def test_token_s2(self):
        self.assertEqual(radar.lat_lon_to_s2_cell_token(6.337, -75.558), "8e443")   # Bello = celda de Medellín
        self.assertEqual(radar.lat_lon_to_s2_cell_token(42.9, -113.9), "54ab1")    # Idaho (Flood Watch)
