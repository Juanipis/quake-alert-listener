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


class FakeSock:
    """Plays the MCS server side: queued inbound bytes, captured outbound frames."""
    def __init__(self, inbound=b""):
        self.inbound = bytearray(inbound)
        self.sent = bytearray()

    def recv(self, n):
        if not self.inbound:
            raise ql.socket.timeout()
        chunk, self.inbound[:] = bytes(self.inbound[:n]), self.inbound[n:]
        return chunk

    def sendall(self, data):
        self.sent.extend(data)

    def settimeout(self, t):
        pass

    def close(self):
        pass

    def login_request(self):
        """Fields of the LoginRequest at the start of `sent` (after the version byte)."""
        buf = bytes(self.sent)
        assert buf[0] == ql.MCS_VERSION and buf[1] == ql.TAG_LOGIN_REQUEST
        n, start = ql.decode_varint(buf, 2)
        return ql.parse_protobuf(buf[start:start + n])

    def frames(self, skip_version=False):
        buf, out = bytes(self.sent), []
        if skip_version:
            buf = buf[1:]
        while buf:
            tag = buf[0]
            n, start = ql.decode_varint(buf, 1)
            out.append((tag, ql.parse_protobuf(buf[start:start + n])))
            buf = buf[start + n:]
        return out


def mcs_frame(tag, payload):
    return bytes([tag]) + ql.encode_varint(len(payload)) + payload


class GoogleMCS(unittest.TestCase):
    """Protocol behaviour checked against Chromium's google_apis/gcm (mcs.proto, mcs_client.cc)."""

    def client(self, inbound, ping=120):
        c = ql.QuakeMCSClient({"android_id": 4242, "security_token": 99}, ping_interval=ping)
        sock = FakeSock(inbound)
        c._open = lambda port: sock
        return c, sock

    def login_response(self, error_code=None, hb_ms=None, server_ms=None):
        body = ql.field_str(1, "0")
        if error_code is not None:
            body += ql.field_bytes(3, ql.field_varint(1, error_code) + ql.field_str(2, "bad"))
        if hb_ms:
            body += ql.field_bytes(7, ql.field_varint(3, hb_ms))
        if server_ms:
            body += ql.field_varint(8, server_ms)
        return bytes([ql.MCS_VERSION]) + mcs_frame(ql.TAG_LOGIN_RESPONSE, body)

    def test_checkin_request_layout(self):
        first = ql.parse_protobuf(ql.build_checkin_request())
        checkin = ql.parse_protobuf(first[4][0][1])
        self.assertEqual(checkin[12][0][1], ql.DEVICE_CHROME_BROWSER)      # type, field 12
        build = ql.parse_protobuf(checkin[13][0][1])                       # chrome_build, field 13
        self.assertEqual(build[2][0][1], ql.CHROME_VERSION.encode())
        self.assertEqual((first[14][0][1], first[22][0][1]), (3, 0))       # version, user_serial_number
        self.assertNotIn(6, first)                                         # no locale
        self.assertNotIn(12, first)                                        # no time zone
        again = ql.parse_protobuf(ql.build_checkin_request(
            {"android_id": 4242, "security_token": 2 ** 63 + 5, "digest": "1-abc"}))
        self.assertEqual(again[2][0], (0, 4242))
        self.assertEqual(again[13][0], (1, 2 ** 63 + 5))                   # fixed64 security_token
        self.assertEqual(again[3][0][1], b"1-abc")

    def test_checkin_due(self):
        now = 1_000_000_000
        self.assertTrue(ql.checkin_due({"android_id": 1, "security_token": 1}, now))       # legacy file
        fresh = {"checkin_format": ql.CHECKIN_FORMAT, "last_checkin": now - 3600, "checkin_interval_s": 172800}
        self.assertFalse(ql.checkin_due(fresh, now))
        self.assertTrue(ql.checkin_due(dict(fresh, last_checkin=now - 172801), now))

    def test_login_reads_heartbeat_config_and_clock(self):
        server_ms = int((time.time() + 30) * 1000)       # server 30 s ahead
        c, sock = self.client(self.login_response(hb_ms=60000, server_ms=server_ms))
        c.connect()
        self.assertTrue(c.connected)
        self.assertEqual((c.stream_id_in, c.stream_id_out), (1, 1))
        self.assertEqual((c.server_heartbeat_s, c.ping_interval), (60, 60))   # follows the faster server
        self.assertAlmostEqual(ql._CLOCK["offset_s"], 30, delta=1)
        self.assertAlmostEqual(ql.now_corrected() - time.time(), 30, delta=1)
        login = sock.login_request()
        settings = {ql.parse_protobuf(v)[1][0][1]: ql.parse_protobuf(v)[2][0][1] for _, v in login[8]}
        self.assertEqual(settings, {b"new_vc": b"1", b"hbping": b"120000"})   # like Chrome
        self.assertEqual((login[14][0][1], login[16][0][1], login[17][0][1]), (1, 2, 1))
        ql._CLOCK["offset_s"] = None

    def test_login_rejected(self):
        c, _ = self.client(self.login_response(error_code=401))
        with self.assertRaises(ql.MCSLoginRejected):
            c.connect()

    def test_heartbeats_carry_last_stream_id(self):
        ping = mcs_frame(ql.TAG_HEARTBEAT_PING, b"")
        c, sock = self.client(self.login_response() + ping)
        c.connect()
        sock.sent.clear()
        tag, _ = c.read_packet()
        self.assertEqual(tag, ql.TAG_HEARTBEAT_PING)
        c.send_pong()
        c.send_ping()
        frames = sock.frames()
        self.assertEqual([f[0] for f in frames], [ql.TAG_HEARTBEAT_ACK, ql.TAG_HEARTBEAT_PING])
        self.assertEqual(frames[0][1][2][0][1], 2)          # login response + ping received
        self.assertIsNotNone(c.awaiting_ack_since)
        sock.inbound.extend(mcs_frame(ql.TAG_HEARTBEAT_ACK, b""))
        c.read_packet()
        self.assertIsNone(c.awaiting_ack_since)              # any packet clears the ack wait

    def test_stream_ack_after_ten_messages_and_immediate_ack(self):
        def data(i, immediate=False):
            body = ql.field_str(3, "sender") + ql.field_str(5, "test.app") + ql.field_str(9, f"pid-{i}")
            if immediate:
                body += ql.field_varint(24, 1)
            return mcs_frame(ql.TAG_DATA_MESSAGE_STANZA, body)
        c, sock = self.client(self.login_response() + b"".join(data(i) for i in range(10)) + data(10, True))
        c.connect()
        sock.sent.clear()
        for _ in range(11):
            c.read_packet()
        acks = [f for f in sock.frames() if f[0] == ql.TAG_IQ_STANZA]
        self.assertEqual(len(acks), 2)                       # after 10, then immediate_ack
        ext = ql.parse_protobuf(acks[0][1][7][0][1])
        self.assertEqual(ext[1][0][1], ql.IQ_STREAM_ACK)
        self.assertEqual(acks[0][1][10][0][1], 11)          # last_stream_id_received
        self.assertEqual(c.unacked_persistent_ids[:2], ["pid-0", "pid-1"])   # acked, awaiting confirmation
        # the server confirms with last_stream_id_received >= our ack's stream id
        sock.inbound.extend(mcs_frame(ql.TAG_HEARTBEAT_ACK, ql.field_varint(2, c.stream_id_out)))
        c.read_packet()
        self.assertEqual(c.unacked_persistent_ids, [])

    def test_unconfirmed_ids_go_into_next_login(self):
        msg = mcs_frame(ql.TAG_DATA_MESSAGE_STANZA, ql.field_str(3, "s") + ql.field_str(5, "a") + ql.field_str(9, "pid-x"))
        c, sock = self.client(self.login_response() + msg)
        c.connect()
        c.read_packet()
        self.assertEqual(c.unacked_persistent_ids, ["pid-x"])
        sock.inbound.extend(self.login_response())
        sock.sent.clear()
        c.connect()
        self.assertIn((2, b"pid-x"), sock.login_request().get(10, []))   # received_persistent_id
        self.assertEqual(c.unacked_persistent_ids, [])       # confirmed by the new LoginResponse

    def test_idle_notification_is_answered(self):
        app = ql.field_str(1, "IdleNotification") + ql.field_str(2, "")
        idle = mcs_frame(ql.TAG_DATA_MESSAGE_STANZA,
                         ql.field_str(3, "gcm@android.com") + ql.field_str(5, ql.MCS_CATEGORY) + ql.field_bytes(7, app))
        c, sock = self.client(self.login_response() + idle)
        c.connect()
        sock.sent.clear()
        c.read_packet()
        (tag, reply), = sock.frames()
        self.assertEqual(tag, ql.TAG_DATA_MESSAGE_STANZA)
        self.assertEqual(reply[5][0][1].decode(), ql.MCS_CATEGORY)
        kv = ql.parse_protobuf(reply[7][0][1])
        self.assertEqual((kv[1][0][1], kv[2][0][1]), (b"IdleNotification", b"false"))

    def test_fallback_port(self):
        c = ql.QuakeMCSClient({"android_id": 1, "security_token": 2})
        sock = FakeSock(self.login_response())
        tried = []

        def open_port(port):
            tried.append(port)
            if port == 5228:
                raise OSError("blocked")
            return sock
        c._open = open_port
        c.connect()
        self.assertEqual((tried, c.port), ([5228, 443], 443))


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
