"""Веб-сторінка: стан для браузера, зміни маяків, HTTP і HTTPS."""
import json
import os
import shutil
import socket
import ssl
import tempfile
import types
import unittest
import urllib.error
import urllib.request

from esp32_locator.config import load_config
from esp32_locator.geo import GeoFrame
from esp32_locator.readings import Readings
from esp32_locator.web import EditError, WebState, addresses, ensure_certificate, start_server

KYIV = GeoFrame(50.4501, 30.5234, 90)


def write_cfg(path, **cfg):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f)
    return load_config(path)


def fake_loc(cfg, pos=(3.0, 4.0), heard=3, rms=0.4):
    rows = [(t, {"rssi": -60.0, "snr": 7.5, "n": 6, "pl": 74.0}, 5.0) for t in cfg["transmitters"]]
    return types.SimpleNamespace(pos=pos, rms=rms, heard=heard, rows=rows, rejected=set(),
                                 unknown=[("DDDDDDDDDDDD", {"rssi": -70.0}, 9.0)])


class WebStateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "config.json")
        self.readings = Readings(3.0, keep_recent=50)
        self.source = types.SimpleNamespace(status="Підключено: /dev/serial0")

    def local(self, **web):
        cfg = write_cfg(self.path, area={"width": 10, "height": 8}, web=web,
                        transmitters=[{"name": "A", "mac": "A" * 12, "x": 0, "y": 0},
                                      {"name": "B", "mac": "B" * 12, "x": 10, "y": 0}])
        return cfg, WebState(cfg, self.path, self.readings, self.source, pin=cfg["web"]["pin"])

    def field(self):
        txs = [{"name": n, "mac": n * 12, "lat": KYIV.to_latlon(x, y)[0], "lon": KYIV.to_latlon(x, y)[1]}
               for n, x, y in (("A", 0, 0), ("B", 30, 0), ("C", 15, 25))]
        cfg = write_cfg(self.path, mode="field", transmitters=txs)
        return cfg, WebState(cfg, self.path, self.readings, self.source)

    def state(self, ws):
        return json.loads(ws.state_json())

    def test_state_in_local_mode(self):
        cfg, ws = self.local()
        self.assertEqual(self.state(ws), {})                       # до першого перерахунку
        self.readings.add("A" * 12, -60.0, 7.5, 14.0)
        ws.on_update(fake_loc(cfg))
        s = self.state(ws)
        self.assertEqual((s["mode"], s["editable"], s["pin_required"], s["heard"]), ("local", True, False, 3))
        self.assertEqual(s["receiver"], {"x": 3.0, "y": 4.0, "latlon": None, "rms": 0.4, "fix": True})
        self.assertEqual([b["name"] for b in s["beacons"]], ["A", "B"])
        self.assertIsNone(s["beacons"][0]["latlon"])
        self.assertEqual(s["unknown"][0]["mac"], "DDDDDDDDDDDD")
        self.assertEqual(s["trail"], [[3.0, 4.0]])
        self.assertEqual(s["status"], "Підключено: /dev/serial0")
        self.assertEqual(len(s["log"]), 1)

    def test_no_fix_does_not_extend_trail(self):
        cfg, ws = self.local()
        ws.on_update(fake_loc(cfg, heard=2))
        s = self.state(ws)
        self.assertFalse(s["receiver"]["fix"])
        self.assertEqual(s["trail"], [])

    def test_state_in_field_mode_has_lat_lon(self):
        cfg, ws = self.field()
        ws.on_update(fake_loc(cfg, pos=(cfg["transmitters"][0]["x"], cfg["transmitters"][0]["y"])))
        s = self.state(ws)
        self.assertEqual(s["mode"], "field")
        lat, lon = s["receiver"]["latlon"]
        self.assertAlmostEqual(lat, cfg["transmitters"][0]["lat"], places=6)
        self.assertAlmostEqual(lon, cfg["transmitters"][0]["lon"], places=6)
        self.assertEqual(s["beacons"][1]["latlon"], [cfg["transmitters"][1]["lat"], cfg["transmitters"][1]["lon"]])

    def test_local_edits(self):
        cfg, ws = self.local()
        res = ws.save_beacon({"mac": "c3:c3:c3:c3:c3:c3", "x": "4.5", "y": 7})
        self.assertTrue(res["ok"])
        self.assertEqual(cfg["transmitters"][-1], {"name": "C", "mac": "C3C3C3C3C3C3", "x": 4.5, "y": 7.0})
        ws.save_beacon({"mac": "A" * 12, "x": 1, "y": 2, "name": "Ріг"})
        self.assertEqual(load_config(self.path)["transmitters"][0], {"name": "Ріг", "mac": "A" * 12, "x": 1.0, "y": 2.0})
        for bad, hint in (({"mac": "A" * 12, "x": 11, "y": 1}, "поза зоною"), ({"mac": "A" * 12, "x": "ab", "y": 1}, "числами"),
                          ({"mac": "A" * 12, "x": "nan", "y": 1}, "скінченними"), ({"mac": "123", "x": 1, "y": 1}, "ID"),
                          ({"mac": "A" * 12}, "числами")):
            with self.assertRaises(EditError, msg=bad) as ctx:
                ws.save_beacon(bad)
            self.assertIn(hint, str(ctx.exception))
        ws.delete_beacon({"mac": "B" * 12})
        self.assertEqual([t["mac"] for t in load_config(self.path)["transmitters"]], ["A" * 12, "C3C3C3C3C3C3"])
        with self.assertRaises(EditError):
            ws.delete_beacon({"mac": "B" * 12})

    def test_field_edit_recomputes_grid_and_resets_position(self):
        cfg, ws = self.field()
        lat, lon = KYIV.to_latlon(80, 0)                           # новий маяк на схід: мапа ширшає,
        res = ws.save_beacon({"mac": "E" * 12, "lat": lat, "lon": lon})   # а кут (0, 0) лишається на місці
        self.assertTrue(res["ok"])
        self.assertIsNone(res["warning"])
        self.assertEqual(cfg["area"]["width"], 100)               # 80 м + по 10 м відступу
        loc = fake_loc(cfg)
        ws.on_update(loc)
        self.assertEqual(loc.pos, (3.0, 4.0))                      # сітка не зсунулась — положення не скидається
        with open(self.path, encoding="utf-8") as f:
            raw = json.load(f)
        self.assertEqual(set(raw["transmitters"][-1]), {"name", "mac", "lat", "lon"})
        self.assertNotIn("geo", raw)
        lat, lon = KYIV.to_latlon(-20, -5)                         # маяк на південний захід: кут (0, 0) зсувається
        ws.save_beacon({"mac": "0" * 12, "lat": lat, "lon": lon})
        self.assertEqual(cfg["area"], {"width": 120, "height": 50})
        ws.on_update(loc)
        self.assertIsNone(loc.pos)                                 # старе згладжене положення — в іншій сітці
        with self.assertRaises(EditError) as ctx:
            ws.save_beacon({"mac": "E" * 12, "lat": 95, "lon": 30})
        self.assertIn("поза межами", str(ctx.exception))
        far = ws.save_beacon({"mac": "F" * 12, "lat": lon, "lon": lat})     # переплутано широту й довготу
        self.assertIn("переплутано", far["warning"])

    def test_pin_and_read_only(self):
        cfg, ws = self.local(pin="4321")
        with self.assertRaises(PermissionError):
            ws.save_beacon({"mac": "A" * 12, "x": 1, "y": 1})
        with self.assertRaises(PermissionError):
            ws.save_beacon({"mac": "A" * 12, "x": 1, "y": 1, "pin": "0000"})
        self.assertTrue(ws.save_beacon({"mac": "A" * 12, "x": 1, "y": 1, "pin": "4321"})["ok"])
        viewer = WebState(cfg, None, self.readings, self.source)
        with self.assertRaises(EditError):
            viewer.save_beacon({"mac": "A" * 12, "x": 1, "y": 1})


class HttpTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        path = os.path.join(self.tmp.name, "config.json")
        self.cfg = write_cfg(path, area={"width": 10, "height": 8}, web={"pin": "4321"},
                             transmitters=[{"name": "A", "mac": "A" * 12, "x": 1, "y": 1}])
        self.ws = WebState(self.cfg, path, Readings(3.0, keep_recent=10), types.SimpleNamespace(status="ok"), pin="4321")
        self.ws.on_update(fake_loc(self.cfg))

    def serve(self, tls=None):
        server = start_server(self.ws, 0, host="127.0.0.1", tls=tls)
        self.addCleanup(server.server_close)            # прибирання — у зворотному порядку: спершу shutdown
        self.addCleanup(server.shutdown)
        return f"{'https' if tls else 'http'}://127.0.0.1:{server.server_address[1]}"

    def request(self, url, data=None, raw=None, context=None):
        body = raw if raw is not None else (json.dumps(data).encode() if data is not None else None)
        req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=5, context=context) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def test_pages_and_api(self):
        base = self.serve()
        code, body = self.request(base + "/")
        self.assertEqual(code, 200)
        self.assertIn("<title>LoRa-локатор</title>", body.decode())
        code, body = self.request(base + "/api/state")
        self.assertEqual((code, json.loads(body)["beacons"][0]["mac"]), (200, "A" * 12))
        self.assertEqual(self.request(base + "/../config.json")[0], 404)
        self.assertEqual(self.request(base + "/api/nothing", {"a": 1})[0], 404)

    def test_post_validation_and_pin(self):
        base = self.serve()
        url = base + "/api/beacons"
        self.assertEqual(self.request(url, {"mac": "A" * 12, "x": 2, "y": 2})[0], 403)
        self.assertEqual(self.request(url, raw=b"not json")[0], 400)
        self.assertEqual(self.request(url, raw=b"[1, 2]")[0], 400)
        self.assertEqual(self.request(url, raw=b"x" * 5000)[0], 400)            # завеликий запит
        code, body = self.request(url, {"mac": "A" * 12, "x": 20, "y": 2, "pin": "4321"})
        self.assertEqual(code, 400)
        self.assertIn("поза зоною", json.loads(body)["error"])
        code, body = self.request(url, {"mac": "A" * 12, "x": 2, "y": 3, "pin": "4321"})
        self.assertEqual((code, json.loads(body)["ok"]), (200, True))
        self.assertEqual((self.cfg["transmitters"][0]["x"], self.cfg["transmitters"][0]["y"]), (2.0, 3.0))

    @unittest.skipUnless(shutil.which("openssl"), "потрібен openssl")
    def test_https_with_self_signed_certificate(self):
        cert, key = ensure_certificate(os.path.join(self.tmp.name, "certs"))
        self.assertEqual(ensure_certificate(os.path.join(self.tmp.name, "certs")), (cert, key))   # не перестворюється
        self.assertEqual(os.stat(key).st_mode & 0o777, 0o600)
        base = self.serve(tls=(cert, key))
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)              # як браузер після «Все одно перейти»:
        ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE  # шифрування є, довіри до сертифіката немає
        idle = socket.create_connection(("127.0.0.1", int(base.rsplit(":", 1)[1])))   # підключився й мовчить
        self.addCleanup(idle.close)
        code, body = self.request(base + "/api/state", context=ctx)       # сервер однаково відповідає іншим
        self.assertEqual(code, 200)
        with self.assertRaises(OSError):                           # звичайним HTTP цей порт не відповідає
            urllib.request.urlopen(base.replace("https", "http") + "/api/state", timeout=5)
        self.assertEqual(self.request(base + "/", context=ctx)[0], 200)    # і після цього працює

    def test_addresses(self):
        self.assertTrue(any(a.endswith(".local") for a in addresses()))

    def test_hotspot_address_comes_first(self):
        from unittest import mock
        with mock.patch("esp32_locator.web._interface_ips", return_value=["192.168.1.5", "10.42.0.1"]):
            out = addresses()
        self.assertEqual(out[0], "10.42.0.1")
        self.assertTrue(out[1].endswith(".local"))
        self.assertIn("192.168.1.5", out)


if __name__ == "__main__":
    unittest.main()
