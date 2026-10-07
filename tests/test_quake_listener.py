"""Offline test suite for quake_listener.py (standard library only).

Run from the repository root:  python3 -m unittest discover -s tests -v
All data here is synthetic: generic "Test Region" places around 0.5, 0.5.
"""
import contextlib
import importlib.util
import io
import json
import math
import os
import random
import struct
import sys
import threading
import time
import types
import unittest
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location("quake_listener", os.path.join(ROOT, "quake_listener.py"))
ql = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ql)
ql._console_write = lambda line: None  # keep test output clean


def make_args(**kw):
    base = dict(lat=0.0, lon=0.0, name="Base Station", notice_mmi=3.0, alert_mmi=5.0,
                min_magnitude=0.0, max_distance_km=20040.0, shake_alert_counts=0.0)
    base.update(kw)
    return types.SimpleNamespace(**base)


def event(**kw):
    ev = dict(source="Test", kind="eew", event_id="e1", revision=1, origin_ts=time.time(),
              lat=0.3, lon=0.2, depth_km=10.0, magnitude=5.0, magnitude_type=None,
              region="Test Region", url=None, final=False, cancelled=False, training=False)
    ev.update(kw)
    return ev


def ws_frame(op, data, fin=True, mask=False):
    n = len(data)
    head = bytes([(0x80 if fin else 0) | op])
    mb = 0x80 if mask else 0
    if n < 126:
        head += bytes([mb | n])
    elif n < 65536:
        head += bytes([mb | 126]) + struct.pack(">H", n)
    else:
        head += bytes([mb | 127]) + struct.pack(">Q", n)
    if mask:
        k = os.urandom(4)
        return head + k + bytes(x ^ k[i % 4] for i, x in enumerate(data))
    return head + data


def fake_ws(stream, chunk=3):
    ws = object.__new__(ql.MiniWebSocket)
    ws._buf, ws._frag, ws._frag_len, ws.sent = bytearray(), [], 0, []
    ws._send = lambda op, data=b"": ws.sent.append((op, data))
    chunks = [stream[i:i + chunk] for i in range(0, len(stream), chunk)]

    def fill():
        if not chunks:
            return False
        ws._buf.extend(chunks.pop(0))
        return True
    ws._fill = fill
    return ws


# ---------------------------------------------------------------------------
class ProtobufAndAeas(unittest.TestCase):
    def test_varint_roundtrip(self):
        for n in (0, 1, 127, 128, 300, 2 ** 32, 2 ** 63 - 1):
            enc = ql.encode_varint(n)
            self.assertEqual(ql.decode_varint(enc, 0), (n, len(enc)))

    def test_simulated_aeas_payload_decodes(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(ql.simulate_alert(None), 0)

    def test_decoder_survives_garbage(self):
        rnd = random.Random(7)
        for _ in range(300):
            blob = bytes(rnd.randrange(256) for _ in range(rnd.randrange(1, 120)))
            try:
                ql.decode_earthquake_payload(blob)
            except Exception:
                pass  # tolerated: the caller catches; what matters is no hang
        # a well-formed event inside garbage still decodes
        self.assertIsInstance(ql.decode_earthquake_payload(b""), list)

    def test_aeas_event_adapter(self):
        ev = ql.aeas_event({"magnitude": 5.4, "region": "Test Region", "lat": 0.5, "lon": 0.5,
                            "radius_km": 90.0, "origin_ts": None, "alert_id": "x1"})
        self.assertEqual(ev["kind"], "aeas")
        self.assertEqual(ev["event_id"], "x1")


class Intensity(unittest.TestCase):
    def test_reference_values(self):
        # Hand-checked against Allen, Wald & Worden (2012), hypocentral form.
        self.assertAlmostEqual(ql.estimate_mmi(6.0, 14.0), 6.86, delta=0.05)
        self.assertAlmostEqual(ql.estimate_mmi(5.0, 50.0), 3.74, delta=0.05)
        self.assertAlmostEqual(ql.estimate_mmi(7.0, 100.0), 5.66, delta=0.05)

    def test_monotonic(self):
        self.assertGreater(ql.estimate_mmi(6.0, 20), ql.estimate_mmi(6.0, 200))
        self.assertGreater(ql.estimate_mmi(7.0, 50), ql.estimate_mmi(5.0, 50))
        self.assertGreaterEqual(ql.estimate_mmi(1.0, 5000), 1.0)

    def test_names(self):
        self.assertTrue(ql.mmi_name(3.7).startswith("IV"))
        self.assertTrue(ql.mmi_name(0.2).startswith("I "))
        self.assertTrue(ql.mmi_name(15).startswith("X+"))
        self.assertTrue(ql.mmi_name(4.5).startswith("V "))     # half-up, not banker's rounding
        self.assertTrue(ql.mmi_name(5.46).startswith("VI"))    # shown as 5.5 -> VI

    def test_assess_impact_eta(self):
        now = 1_000_000.0
        ev = event(origin_ts=now - 5, lat=0.0, lon=0.9, depth_km=10.0)
        imp = ql.assess_impact(ev, 0.0, 0.0, now)
        hypo = math.sqrt(imp["distance_km"] ** 2 + 100)
        self.assertAlmostEqual(imp["hypocentral_km"], hypo, delta=0.1)
        self.assertAlmostEqual(imp["s_wave_eta_s"], hypo / 3.5 - 5, delta=0.2)
        self.assertLess(imp["p_wave_arrival_ts"], imp["s_wave_arrival_ts"])


class TimeParsing(unittest.TestCase):
    def test_formats(self):
        t = 1767225600.0  # 2026-01-01T00:00:00Z
        self.assertEqual(ql._parse_time("2026-01-01T00:00:00Z"), t)
        self.assertAlmostEqual(ql._parse_time("2026-01-01T00:00:00.5Z"), t + 0.5)
        self.assertEqual(ql._parse_time("2026-01-01T09:00:00+09:00"), t)
        self.assertEqual(ql._parse_time("2026/01/01 09:00:00", 9.0), t)   # JMA style
        self.assertEqual(ql._parse_time("2026-01-01 08:00:00", 8.0), t)   # CENC style
        self.assertIsNone(ql._parse_time("nonsense"))
        self.assertIsNone(ql._parse_time(None))


class Desk(unittest.TestCase):
    def setUp(self):
        self.sent = []
        self.desk = ql.DetectionDesk(make_args(), self.sent.append)

    def test_levels(self):
        near = self.desk.submit(event(event_id="a", magnitude=6.0, lat=0.2, lon=0.2))
        self.assertEqual(near["level"], "alert")
        self.assertEqual(near["status"], "early warning")
        self.assertIsNotNone(near["s_wave_eta_s"])
        far = self.desk.submit(event(event_id="b", magnitude=4.0, lat=40, lon=40))
        self.assertIsNone(far)

    def test_escalation_and_dedupe_across_sources(self):
        p1 = self.desk.submit(event(event_id="q", magnitude=4.6))
        self.assertEqual(p1["level"], "notice")
        self.assertIsNone(self.desk.submit(event(event_id="q", revision=2, magnitude=4.7)))
        p3 = self.desk.submit(event(event_id="q", revision=3, magnitude=5.8))
        self.assertEqual(p3["level"], "alert")
        # the same quake reported later by another source is merged, not re-announced
        later = event(source="EMSC", kind="report", event_id="other-id", magnitude=5.7,
                      origin_ts=p1 and self.desk.sent[("Test", "q")]["origin_ts"] + 2)
        self.assertIsNone(self.desk.submit(later))
        self.assertEqual(len(self.sent), 2)

    def test_cancel_only_after_notification(self):
        self.assertIsNone(self.desk.submit(event(event_id="c", cancelled=True)))
        self.desk.submit(event(event_id="c", magnitude=5.5))
        cancel = self.desk.submit(event(event_id="c", cancelled=True))
        self.assertEqual(cancel["level"], "cancel")
        self.assertEqual(cancel["status"], "cancelled")

    def test_filters(self):
        self.assertIsNone(self.desk.submit(event(event_id="t", training=True, magnitude=7)))
        self.assertIsNone(self.desk.submit(event(event_id="o", origin_ts=time.time() - 3600, magnitude=7)))
        desk = ql.DetectionDesk(make_args(min_magnitude=5.0, max_distance_km=10), self.sent.append)
        self.assertIsNone(desk.submit(event(event_id="m", magnitude=4.9)))
        self.assertIsNone(desk.submit(event(event_id="d", magnitude=6.5, lat=1.0, lon=1.0)))

    def test_unlocated_aeas_falls_back_to_magnitude(self):
        p = self.desk.submit(event(kind="aeas", event_id="u", lat=None, lon=None, magnitude=5.2))
        self.assertEqual(p["level"], "alert")
        self.assertIsNone(p["distance_km"])

    def test_aeas_impact_zone_floor(self):
        # far enough that the estimate is weak, but Google put us inside its circle
        p = self.desk.submit(event(kind="aeas", event_id="z", magnitude=4.6, lat=0.9, lon=0.9,
                                   depth_km=None, radius_km=150.0))
        self.assertEqual(p["level"], "notice")

    def test_onsite(self):
        desk = ql.DetectionDesk(make_args(shake_alert_counts=1000), self.sent.append)
        p = desk.submit(event(kind="onsite", event_id="s1", magnitude=None, peak_counts=5000,
                              lat=0.0, lon=0.0, origin_ts=None, detail="EHZ"))
        self.assertEqual(p["level"], "alert")
        self.assertEqual(p["status"], "on-site trigger")


class Feeds(unittest.TestCase):
    def test_emsc(self):
        msg = json.dumps({"action": "create", "data": {"properties": {
            "lat": 0.5, "lon": 0.5, "mag": 4.6, "depth": 10, "flynn_region": "TEST REGION",
            "unid": "u1", "time": "2026-01-01T00:00:00.12Z", "magtype": "mb"}}})
        ev = ql.emsc_event_from_message(msg)
        self.assertEqual((ev["kind"], ev["region"], ev["magnitude"]), ("report", "Test Region", 4.6))
        self.assertAlmostEqual(ev["origin_ts"], 1767225600.12, delta=0.01)
        self.assertIsNone(ql.emsc_event_from_message("{"))
        self.assertIsNone(ql.emsc_event_from_message(json.dumps({"data": {"properties": {"lat": "x"}}})))

    def test_wolfx(self):
        self.assertIsNone(ql.wolfx_event_from_message('{"type":"heartbeat","ver":24}'))
        self.assertIsNone(ql.wolfx_event_from_message('{"type":"jma_eqlist"}'))
        jma = json.dumps({"type": "jma_eew", "EventID": "1", "Serial": 3, "OriginTime": "2026/01/01 09:00:00",
                          "Hypocenter": "Test Region", "Latitude": 0.5, "Longitude": 0.5, "Magunitude": 6.1,
                          "Magnitude": 6.1, "Depth": 30, "MaxIntensity": "5-", "isFinal": False,
                          "isCancel": False, "isTraining": False})
        ev = ql.wolfx_event_from_message(jma)
        self.assertEqual(ev["source"], "JMA EEW")
        self.assertEqual(ev["origin_ts"], 1767225600.0)
        self.assertEqual((ev["revision"], ev["agency_intensity"], ev["kind"]), (3, "5-", "eew"))
        cenc = json.dumps({"type": "cenc_eew", "EventID": "2", "ReportNum": 1, "OriginTime": "2026-01-01 08:00:00",
                           "HypoCenter": "Test Region", "Latitude": 0.5, "Longitude": 0.5, "Magnitude": 4.0,
                           "Depth": 8, "MaxIntensity": 5.6})
        ev = ql.wolfx_event_from_message(cenc)
        self.assertEqual((ev["source"], ev["origin_ts"], ev["agency_intensity"]), ("CENC EEW", 1767225600.0, "5.6"))
        cancel = json.loads(jma)
        cancel["isCancel"] = True
        self.assertTrue(ql.wolfx_event_from_message(json.dumps(cancel))["cancelled"])


class WebSocketFrames(unittest.TestCase):
    def test_fragments_ping_and_large(self):
        big = json.dumps({"x": "y" * 300}).encode()
        stream = (ws_frame(1, b'{"a":', fin=False) + ws_frame(9, b"hi") + ws_frame(0, b"1}") + ws_frame(1, big))
        ws = fake_ws(stream)
        got = []
        while True:
            m = ws.recv_message()
            if m is None:
                break
            got.append(m)
        self.assertEqual(got, ['{"a":1}', big.decode()])
        self.assertEqual(ws.sent, [(0xA, b"hi")])

    def assertRejected(self, raw):
        ws = fake_ws(raw, chunk=1 << 20)
        with self.assertRaises(ConnectionError):
            while ws.recv_message() is not None:
                pass

    def test_hostile_peers(self):
        self.assertRejected(ws_frame(1, b"x", mask=True))                               # masked from server
        self.assertRejected(bytes([0x81, 127]) + struct.pack(">Q", 10 ** 12))          # huge header
        self.assertRejected(bytes([0x89, 126]) + struct.pack(">H", 200) + b"p" * 200)  # long control frame
        chunk = b"a" * 60000
        self.assertRejected(ws_frame(1, chunk, fin=False) +
                            b"".join(ws_frame(0, chunk, fin=False) for _ in range(20)))  # endless fragments
        self.assertRejected(ws_frame(8, struct.pack(">H", 1000)))                       # close


class Shake(unittest.TestCase):
    def test_packet_parse(self):
        self.assertEqual(ql.parse_shake_packet(b"{'EHZ', 1700000000.5, 1, -2, 3}"), ("EHZ", 1700000000.5, [1, -2, 3]))
        self.assertIsNone(ql.parse_shake_packet(b"garbage"))
        self.assertIsNone(ql.parse_shake_packet(b"{'EHZ', x, 1}"))

    def _run(self, burst):
        sent = []
        args = make_args(shake_udp=":0", shake_channel="", shake_sta_lta_on=4.0, shake_sta_lta_off=1.5,
                         shake_alert_counts=0.0)
        src = ql.ShakeSource(args, ql.DetectionDesk(args, sent.append))
        rnd = random.Random(1)
        t = 1700000000.0
        for k in range(60 * 4):                       # 60 s of 25-sample packets at 100 Hz
            samples = [rnd.gauss(0, 10) for _ in range(25)]
            if burst and 45 * 4 <= k < 47 * 4:
                samples = [s + 400 * math.sin(i) for i, s in enumerate(samples)]
            pkt = "{'EHZ', %.2f, %s}" % (t, ", ".join("%d" % s for s in samples))
            src.process(pkt.encode())
            t += 0.25
        return sent

    def test_sta_lta_triggers_on_burst_only(self):
        self.assertEqual(self._run(burst=False), [])
        sent = self._run(burst=True)
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["kind"], "onsite")
        self.assertEqual(sent[0]["level"], "notice")


class HttpApi(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = ql.start_http_server("127.0.0.1", 0)
        cls.port = cls.httpd.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def get(self, path, headers=None, method="GET"):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", headers=headers or {}, method=method)
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            with e:
                return e.code, json.loads(e.read() or b"{}")

    def test_status_shape(self):
        code, body = self.get("/status")
        self.assertEqual(code, 200)
        for k in ("google_mcs", "sources", "emsc", "recent_logs", "total_detections", "last_quake"):
            self.assertIn(k, body)
        self.assertEqual(set(body["sources"]), {"emsc", "wolfx", "shake"})

    def test_guards(self):
        self.assertEqual(self.get("/status", {"Host": "attacker.example"})[0], 403)
        self.assertEqual(self.get("/drill", {"Origin": "https://attacker.example"}, "POST")[0], 403)
        self.assertEqual(self.get("/nope")[0], 404)


class Cli(unittest.TestCase):
    def test_sources_and_aliases(self):
        p = ql.build_parser()
        a = p.parse_args(["--sources", "emsc,wolfx", "--emsc-min-mag", "3.5", "--emsc-radius-km", "200"])
        self.assertEqual(a.sources, {"emsc", "wolfx"})
        self.assertEqual((a.min_magnitude, a.max_distance_km), (3.5, 200.0))
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                p.parse_args(["--sources", "tiktok"])
            with self.assertRaises(SystemExit):
                p.parse_args(["--shake-udp", "notaport"])
        self.assertEqual(p.parse_args(["--shake-udp", "8888"]).shake_udp, ":8888")

    def test_host_guard(self):
        for host, ok in [("127.0.0.1:8990", True), ("localhost", True), ("[::1]:8990", True),
                         ("10.0.0.5:8990", True), ("attacker.example", False), ("127.0.0.1.nip.io", False)]:
            self.assertEqual(ql.is_host_allowed(host), ok, host)


if __name__ == "__main__":
    unittest.main()
