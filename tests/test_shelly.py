"""Test senza dispositivi: Wi-Fi del telefono via adb e collegamento degli Shelly alla rete di casa."""
from __future__ import annotations

import json
import sys
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from claudelox import shelly, telefono  # noqa: E402

SCAN = """    BSSID              Frequency      RSSI           Age(sec)     SSID                                 Flags
  02:00:00:00:00:01       5180    -47(0:-54/1:-48)     0,726    CasaTest                          [WPA2-PSK-CCMP-128][RSN-PSK+SAE-CCMP-128][ESS][WPS][MFPC]
  02:00:00:00:00:02       5220    -85(0:-88/1:-88)     3,581                                      [WPA2-PSK-CCMP-128][RSN-PSK-CCMP-128][ESS]
  02:00:00:00:00:03       2417    -37(0:-51/1:-37)     5.868    CasaTest                          [WPA2-PSK-CCMP-128][RSN-PSK+SAE-CCMP-128][ESS][WPS][MFPC]
  02:00:00:00:00:04       2447    -74(0:-81/1:-75)     5,254    Rete Vicino 74 EN                 [WPA2-PSK-CCMP-128][ESS][WPS]
  02:00:00:00:00:05       2412    -60(0:-60/1:-61)     1,000    shellycolorbulb-0123456789AB      [ESS]
"""
STATUS = """Wifi is enabled
==== Primary ClientModeManager instance ====
Wifi is connected to "CasaTest"
WifiInfo: SSID: "CasaTest", BSSID: 02:00:00:00:00:01, MAC: 02:00:00:00:00:99, IP: /192.168.10.13, Security type: 4
"""
NETWORKS = """Network Id      SSID                         Security type
0            CasaTest                         wpa2-psk
0            CasaTest                         wpa3-sae^
3            shellycolorbulb-0123456789AB     open
3            shellycolorbulb-0123456789AB     owe^
5            Rete con spazi                   wpa2-psk
"""
PWD = "segreta-di-prova"


class PhoneParsingTest(unittest.TestCase):
    def test_scan(self):
        nets = telefono.parse_scan(SCAN)
        self.assertEqual(len(nets), 5)
        self.assertEqual(nets[0], {"bssid": "02:00:00:00:00:01", "freq": 5180, "rssi": -47, "ssid": "CasaTest",
                                   "flags": "[WPA2-PSK-CCMP-128][RSN-PSK+SAE-CCMP-128][ESS][WPS][MFPC]"})
        self.assertEqual(nets[1]["ssid"], "")                    # rete nascosta
        self.assertEqual(nets[3]["ssid"], "Rete Vicino 74 EN")   # nome con spazi
        self.assertTrue(telefono.is_open(nets[4]))
        self.assertFalse(telefono.is_open(nets[0]))
        self.assertEqual([n["ssid"] for n in nets if shelly.SETUP_SSID_RE.match(n["ssid"])],
                         ["shellycolorbulb-0123456789AB"])

    def test_status_and_networks(self):
        self.assertEqual(telefono.parse_status(STATUS), {"ssid": "CasaTest", "ip": "192.168.10.13"})
        self.assertEqual(telefono.parse_status("Wifi is enabled\nWifi is not connected\n"), {"ssid": None, "ip": None})
        self.assertEqual(telefono.parse_networks(NETWORKS),
                         [(0, "CasaTest"), (3, "shellycolorbulb-0123456789AB"), (5, "Rete con spazi")])

    def test_http_keeps_secrets_off_the_command_line(self):
        calls = []

        class FakePhone(telefono.Phone):
            def __init__(self):
                pass

            def shell(self, command, data=None, timeout=30):
                calls.append((command, data))
                return 0, '{"ok":true}\n200'

        url = f"http://192.168.33.1/settings/sta?key={PWD}"
        code, text = FakePhone().http(url, data='{"pass":"a\\"b"}', content_type="application/json")
        self.assertEqual((code, text), (200, '{"ok":true}'))
        command, data = calls[0]
        self.assertNotIn(PWD, command)
        self.assertIn("--interface wlan0", command)
        self.assertIn("-K -", command)
        cfg = data.decode()
        self.assertIn(f'url = "{url}"', cfg)
        self.assertIn('data-raw = "{\\"pass\\":\\"a\\\\\\"b\\"}"', cfg)   # virgolette e barre protette
        self.assertIn('header = "Content-Type: application/json"', cfg)

    def test_http_without_answer(self):
        class FakePhone(telefono.Phone):
            def __init__(self):
                pass

            def shell(self, command, data=None, timeout=30):
                return 7, "\n000"

        self.assertEqual(FakePhone().http("http://192.168.33.1/shelly"), (0, ""))


class KindTest(unittest.TestCase):
    def test_device_kind(self):
        self.assertEqual(shelly.device_kind({"gen": 1, "model": "SHCB-1"}), "luce")
        self.assertEqual(shelly.device_kind({"gen": 1, "model": "SHMOS-01"}), "sensore")
        self.assertEqual(shelly.device_kind({"gen": 1, "model": "SHPLG-S"}), "presa")
        self.assertEqual(shelly.device_kind({"gen": 2, "model": "SNPL-00112EU", "app": "PlusPlugS"}), "presa")
        self.assertEqual(shelly.device_kind({"gen": 2, "model": "SNSN-0013A", "app": "PlusHT"}), "sensore")

    def test_program_kind(self):
        self.assertEqual(shelly.program_kind(["", "/light/0?brightness=<v>", "settings/?mode=color"]), "luce")
        self.assertEqual(shelly.program_kind(["/status", '"tmp":{"value":\\v', '"lux":{"value":\\v']), "sensore")
        self.assertEqual(shelly.program_kind(["/rpc/Shelly.GetStatus", '"tC":\\v', "/relay/0?turn=on"]), "presa")

    def test_device_info(self):
        info = shelly.device_info("192.168.33.1", {"type": "SHCB-1", "mac": "0123456789ab", "fw": "x"})
        self.assertEqual((info["gen"], info["model"], info["mac"]), (1, "SHCB-1", "0123456789AB"))
        self.assertIsNone(shelly.device_info("192.168.33.1", {"altro": 1}))


class FakeShelly:
    """Shelly gen1 in modalita' AP: risponde a /settings/sta secondo il comportamento scelto."""

    def __init__(self, post_ok=True, drop_post=False, alive_after_drop=True):
        self.post_ok, self.drop_post, self.alive_after_drop = post_ok, drop_post, alive_after_drop
        self.sta = {"enabled": False, "ssid": None, "ipv4_method": "dhcp", "ip": None}
        self.requests = []

    def apply(self, params):
        self.sta.update(enabled=params["enabled"] == "1", ssid=params["ssid"], ipv4_method=params["ipv4_method"],
                        ip=params["ip"])

    def http(self, url, data=None, content_type=None):
        self.requests.append((url, data))
        u = urllib.parse.urlsplit(url)
        if data is not None:
            if self.drop_post:
                self.apply(dict(urllib.parse.parse_qsl(data)))
                return 0, ""
            if self.post_ok:
                self.apply(dict(urllib.parse.parse_qsl(data)))
        elif u.query:
            self.apply(dict(urllib.parse.parse_qsl(u.query)))
        elif self.drop_post and not self.alive_after_drop:
            return 0, ""
        return 200, json.dumps(self.sta)


class ConfigureWifiTest(unittest.TestCase):
    args = ("CasaTest", PWD, "192.168.10.50", "192.168.10.1", "192.168.10.1")

    def test_form(self):
        q = dict(urllib.parse.parse_qsl(shelly.sta_form(*self.args)))
        self.assertEqual(q, {"enabled": "1", "ssid": "CasaTest", "key": PWD, "ipv4_method": "static",
                             "ip": "192.168.10.50", "netmask": "255.255.255.0", "gateway": "192.168.10.1",
                             "dns": "192.168.10.1"})
        sta = json.loads(shelly.sta_rpc(*self.args))["params"]["config"]["sta"]
        self.assertEqual((sta["ssid"], sta["pass"], sta["ipv4mode"], sta["gw"]), ("CasaTest", PWD, "static",
                                                                                 "192.168.10.1"))

    def test_gen1_post(self):
        dev = FakeShelly()
        self.assertEqual(shelly.configure_wifi(dev.http, {"gen": 1}, *self.args), "ok")
        self.assertEqual(len(dev.requests), 1)
        self.assertNotIn(PWD, dev.requests[0][0])   # la password va nel corpo, non nell'indirizzo

    def test_gen1_ignores_post_body(self):
        dev = FakeShelly(post_ok=False)
        self.assertEqual(shelly.configure_wifi(dev.http, {"gen": 1}, *self.args), "ok")
        self.assertEqual(dev.sta["ssid"], "CasaTest")
        self.assertEqual(len(dev.requests), 2)

    def test_gen1_answer_lost(self):
        dev = FakeShelly(drop_post=True)
        self.assertEqual(shelly.configure_wifi(dev.http, {"gen": 1}, *self.args), "ok")   # rilegge e conferma
        gone = FakeShelly(drop_post=True, alive_after_drop=False)
        self.assertEqual(shelly.configure_wifi(gone.http, {"gen": 1}, *self.args), "inviato")

    def test_gen1_refused_without_password_in_message(self):
        def http(url, data=None, content_type=None):
            return 400, f"bad request {PWD}"

        with self.assertRaises(ValueError) as ctx:
            shelly.configure_wifi(http, {"gen": 1}, *self.args)
        self.assertNotIn(PWD, str(ctx.exception))

    def test_gen2(self):
        sent = []

        def ok(url, data=None, content_type=None):
            sent.append((url, data, content_type))
            return 200, '{"id":1,"result":{"restart_required":false}}'

        self.assertEqual(shelly.configure_wifi(ok, {"gen": 2}, *self.args), "ok")
        self.assertEqual((sent[0][0], sent[0][2]), ("http://192.168.33.1/rpc", "application/json"))

        def refused(url, data=None, content_type=None):
            return 500, json.dumps({"id": 1, "error": {"code": -103, "message": f"bad pass {PWD}"}})

        with self.assertRaises(ValueError) as ctx:
            shelly.configure_wifi(refused, {"gen": 2}, *self.args)
        self.assertNotIn(PWD, str(ctx.exception))
        self.assertEqual(shelly.configure_wifi(lambda *a, **k: (0, ""), {"gen": 2}, *self.args), "inviato")


class ShellyIdentityTest(unittest.TestCase):
    def test_only_real_shelly_commands_count(self):
        self.assertTrue(shelly.looks_like_shelly(["/relay/0", "<v>"]))
        self.assertTrue(shelly.looks_like_shelly(["/light/0", "settings/"]))
        self.assertTrue(shelly.looks_like_shelly(["/status", '"lux":{"value":\\v']))
        self.assertTrue(shelly.looks_like_shelly(["/rpc/Switch.GetStatus?id=0", '"apower":\\v']))
        self.assertFalse(shelly.looks_like_shelly(["/solar_api/v1/GetInverterRealtimeData.cgi", '"PAC":\\v']))
        self.assertFalse(shelly.looks_like_shelly(["/status"]))
        self.assertFalse(shelly.looks_like_shelly([]))

    def test_the_setup_network_must_belong_to_the_device(self):
        self.assertTrue(shelly.ssid_matches("shellycolorbulb-0123456789AB", "0123456789AB"))
        self.assertTrue(shelly.ssid_matches("shellyplug-s-6789ab", "0123456789AB"))
        self.assertFalse(shelly.ssid_matches("shellyplus1-FFFFFFFFFFFF", "0123456789AB"))
        self.assertFalse(shelly.ssid_matches("shelly-gratis", "0123456789AB"))
        self.assertFalse(shelly.ssid_matches("shellycolorbulb-0123456789AB", ""))

    def test_blink_uses_the_part_the_device_has(self):
        for app, part in (("DimmerG3", "Light"), ("PlusRGBWPM", "RGBW"), ("Plus1", "Switch")):
            urls: list[str] = []
            with self.subTest(app), mock.patch.object(shelly, "_get_json",
                                                      side_effect=lambda url, timeout=3: urls.append(url) or {}), \
                    mock.patch.object(shelly.time, "sleep"):
                shelly.blink({"gen": 2, "ip": "10.0.0.7", "model": "X", "app": app}, times=1)
                self.assertTrue(urls and all(f"/rpc/{part}." in u for u in urls))


if __name__ == "__main__":
    unittest.main()
