"""Test del ponte sensore aria Amazon -> Loxone senza Amazon: valori, pagina per Loxone, accesso."""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "integrazioni" / "amazon-alexa"))

import amazon_loxone as al  # noqa: E402

T0 = datetime(2026, 10, 21, 18, 0, tzinfo=timezone.utc)


def sensor(value, scale=None, error=False, when=None):
    return SimpleNamespace(value=value, scale=scale, error=error, time_of_sample=when)


def device(name, serial, sensors, device_type=al.DEVICE_TYPE_AQM, online=True):
    return SimpleNamespace(account_name=name, serial_number=serial, sensors=sensors, device_type=device_type,
                           online=online, software_version="1")


def aqm_sensors(when=T0, reach="OK"):
    return {"Air Quality": sensor(95, when=when), "PM25": sensor(3, "MicroGramsPerCubicMeter", when=when),
            "PM10": sensor("4.5"), "VOC": sensor(1.0), "CO": sensor(0, "ppm"), "Humidity": sensor(45, "%"),
            "temperature": sensor(21.25, "CELSIUS"), "reachability": sensor(reach, when=when)}


class ValuesTest(unittest.TestCase):
    def test_names_of_the_page(self):
        self.assertEqual(al.sensor_values(aqm_sensors()), {
            "qualita": 95.0, "pm25": 3.0, "pm10": 4.5, "voc": 1.0, "co": 0.0, "umidita": 45.0,
            "temperatura": 21.25})

    def test_errors_and_bad_values_are_left_out(self):
        self.assertEqual(al.sensor_values({
            "PM25": sensor("n/a", error=True), "PM10": sensor("n/a"), "CO": sensor(float("nan")),
            "VOC": sensor(None), "illuminance": sensor(12)}), {})

    def test_fahrenheit_becomes_celsius(self):
        self.assertAlmostEqual(al.sensor_values({"temperature": sensor(68, "FAHRENHEIT")})["temperatura"], 20.0)

    def test_only_air_monitors_in_name_order(self):
        devices = {"b": device("Studio", "B1", aqm_sensors()), "a": device("Camera", "A1", aqm_sensors()),
                   "tv": device("Fire TV", "T1", {"temperature": sensor(20)}, device_type="A2JKHJ0PX4J3L3")}
        readings = al.readings_from_devices(devices, 100.0)
        self.assertEqual([r.name for r in readings], ["Camera", "Studio"])
        self.assertTrue(readings[0].reachable)
        self.assertEqual(readings[0].sampled, T0.timestamp())
        self.assertEqual(readings[0].fetched, 100.0)

    def test_reachability(self):
        r = al.readings_from_devices({"a": device("Aria", "A1", aqm_sensors(reach="UNREACHABLE"))}, 0)[0]
        self.assertFalse(r.reachable)
        r = al.readings_from_devices({"a": device("Aria", "A1", {"PM25": sensor(1)}, online=False)}, 0)[0]
        self.assertFalse(r.reachable)


class PageTest(unittest.TestCase):
    def reading(self, **kw):
        base = dict(name="Aria", serial="A1", reachable=True, values={"qualita": 95.0, "pm25": 3.04, "umidita": 45.4},
                    sampled=1000.0, fetched=1030.0)
        base.update(kw)
        return al.Reading(**base)

    def test_page_for_loxone(self):
        self.assertEqual(al.render(self.reading(), 1060.0, 900), "online=1\nqualita=95\npm25=3.0\numidita=45\neta=60\n")

    def test_old_or_unreachable_values_are_not_online(self):
        self.assertTrue(al.render(self.reading(), 1030.0 + 901, 900).startswith("online=0\n"))
        self.assertTrue(al.render(self.reading(reachable=False), 1031.0, 900).startswith("online=0\n"))
        self.assertEqual(al.render(None, 0, 900), "online=0\n")

    def test_age_without_sample_time(self):
        self.assertTrue(al.render(self.reading(sampled=None), 1100.0, 900).endswith("eta=70\n"))

    def test_pick(self):
        rs = [self.reading(name="Camera", serial="C1"), self.reading(name="Studio", serial="S1")]
        self.assertEqual(al.pick(rs).name, "Camera")
        self.assertEqual(al.pick(rs, "studio").name, "Studio")
        self.assertEqual(al.pick(rs, "s1").name, "Studio")
        self.assertIsNone(al.pick(rs, "cucina"))
        self.assertEqual(al.pick(rs, "studio", index=1).name, "Camera")
        self.assertIsNone(al.pick(rs, index=3))
        self.assertIsNone(al.pick([]))

    def test_allowed(self):
        allow = frozenset({"10.0.0.5", "127.0.0.1"})
        self.assertTrue(al.allowed("10.0.0.5", allow))
        self.assertTrue(al.allowed("::ffff:10.0.0.5", allow))
        self.assertFalse(al.allowed("10.0.0.6", allow))
        self.assertFalse(al.allowed(None, allow))
        self.assertTrue(al.allowed("10.0.0.6", frozenset()))


class SettingsTest(unittest.TestCase):
    def test_defaults_and_limits(self):
        s = al.Settings.from_env({})
        self.assertEqual((s.port, s.interval_s, s.device, s.allow_from), (8095, 300, "", frozenset()))
        self.assertEqual(s.stale_s, 900)
        s = al.Settings.from_env({"HTTP_PORT": "9000", "INTERVAL_S": "10", "ALLOW_FROM": " 10.0.0.5, ,127.0.0.1",
                                  "DEVICE": " Aria ", "STATE_DIR": "/tmp/x"})
        self.assertEqual((s.port, s.interval_s, s.device), (9000, 60, "Aria"))
        self.assertEqual(s.allow_from, frozenset({"10.0.0.5", "127.0.0.1"}))
        self.assertEqual(s.state_dir, Path("/tmp/x"))


class LoginTest(unittest.TestCase):
    def test_save_and_load(self):
        with tempfile.TemporaryDirectory() as d:
            state = Path(d) / "stato"
            self.assertIsNone(al.load_login(state))
            path = al.save_login(state, {"refresh_token": "Atnr|x", "site": "https://www.amazon.it"})
            self.assertEqual(al.load_login(state)["site"], "https://www.amazon.it")
            if os.name == "posix":
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            al.save_login(state, {"site": "senza token"})
            self.assertIsNone(al.load_login(state))



try:  # librerie usate dall'accesso ad Amazon (le installa aioamazondevices)
    import bs4
    from yarl import URL
except ImportError:
    bs4 = None

SIGNIN = """<html><head><title>Accedi</title></head><body>
<noscript><div class="a-alert-content">Serve JavaScript</div></noscript>
<div id="auth-alert-window" class="a-box a-alert"><h4>C'e' stato un problema</h4>
<div class="a-alert-content">Inserisci il nome</div></div>
<div class="a-box a-alert-inline"><div class="a-alert-content">Inserisci la password</div></div>
<div class="a-box aok-hidden"><div class="a-alert-content">Inserisci il tuo indirizzo e-mail</div></div>
{alert}<form name="signIn" method="post" action="https://www.amazon.com/ap/signin">
<input type="hidden" name="appActionToken" value="tok1"><input type="email" name="email">
<input type="password" name="password"><input type="submit" id="signInSubmit"></form></body></html>"""
MFA = """<html><head><title>Verifica in due passaggi</title></head><body><h1>Verifica in due passaggi</h1>
<form id="auth-mfa-form" method="post" action="/ap/signin"><input type="hidden" name="mfa" value="m1">
<input type="tel" id="auth-mfa-otpcode" name="otpCode"><input type="submit" id="auth-signin-button"></form></body></html>"""


@unittest.skipIf(bs4 is None, "manca beautifulsoup4 (si installa con aioamazondevices)")
class LoginFlowTest(unittest.TestCase):
    """Accesso passo per passo, con pagine di Amazon finte."""

    def run_flow(self, pages, codes=("111111",)):
        calls, asked, registered = [], [], []
        replies = iter(pages)

        async def session_request(method, url, input_data=None, **kw):
            calls.append((method, str(url), input_data))
            html, final_url = next(replies)
            return bs4.BeautifulSoup(html, "html.parser"), SimpleNamespace(url=URL(final_url))

        async def register(data):
            registered.append(data)
            return {"refresh_token": "Atnr|x", "customer_info": {"home_region": "EU"}}

        async def nothing():
            return None

        state = SimpleNamespace(login_email="mario@example.com", login_password="segreta", domain="com",
                                retail_site_url=URL("https://www.amazon.it/"), login_stored_data={})
        lg = SimpleNamespace(_session_state_data=state, _create_code_verifier=lambda: b"verifier",
                             _build_client_id=lambda: "cid", _build_oauth_url=lambda v, c: URL("https://www.amazon.com/ap/signin?x=1"),
                             _register_device=register, _domain_refresh_auth_cookies=nothing,
                             _register_device_capabilities=nothing, obtain_account_customer_id=nothing)
        api = SimpleNamespace(login=lg, _http_wrapper=SimpleNamespace(session_request=session_request))
        codes = iter(codes)
        result = asyncio.run(al.login_flow(api, lambda: asked.append(1) or next(codes)))
        return result, calls, asked, registered, state

    def test_code_asked_after_the_password_and_retried(self):
        landing = "https://www.amazon.com/ap/maplanding?openid.oa2.authorization_code=CODE"
        result, calls, asked, registered, state = self.run_flow(
            [(SIGNIN.format(alert=""), "https://www.amazon.com/ap/signin?x=1"),
             (MFA, "https://www.amazon.com/ap/mfa?y=2"), (MFA, "https://www.amazon.com/ap/signin"),
             ("", landing)], codes=("000000", "123456"))
        self.assertEqual([c[0] for c in calls], ["GET", "POST", "POST", "POST"])
        self.assertEqual(calls[1][2], {"appActionToken": "tok1", "email": "mario@example.com", "password": "segreta"})
        self.assertEqual(calls[2][1], "https://www.amazon.com/ap/signin")  # azione relativa del modulo
        self.assertEqual(calls[3][2], {"mfa": "m1", "otpCode": "123456", "mfaSubmit": "Submit", "rememberDevice": "false"})
        self.assertEqual(len(asked), 2)
        self.assertEqual(registered, [{"authorization_code": "CODE", "code_verifier": b"verifier", "domain": "com"}])
        self.assertEqual(result["site"], "https://www.amazon.it")
        self.assertIs(state.login_stored_data, result)

    def test_account_without_two_step_verification(self):
        _, calls, asked, registered, _ = self.run_flow(
            [(SIGNIN.format(alert=""), "https://www.amazon.com/ap/signin?x=1"),
             ("", "https://www.amazon.com/ap/maplanding?openid.oa2.authorization_code=C2")])
        self.assertEqual((len(calls), asked), (2, []))
        self.assertEqual(registered[0]["authorization_code"], "C2")

    def test_unknown_page_is_described_without_secrets(self):
        alert = '<div class="a-alert-content">La password non è corretta</div>'
        with self.assertRaises(al.LoginStopped) as ctx:
            self.run_flow([(SIGNIN.format(alert=""), "https://www.amazon.com/ap/signin?x=1"),
                           (SIGNIN.format(alert=alert), "https://www.amazon.com/ap/signin?segreto=1")])
        msg = str(ctx.exception)
        self.assertIn("pagina /ap/signin", msg)
        self.assertIn("«La password non è corretta»", msg)
        self.assertIn("campi: email, password", msg)
        for hidden in ("Inserisci", "JavaScript", "problema"):  # messaggi che la pagina non mostra
            self.assertNotIn(hidden, msg)
        self.assertNotIn("tok1", msg)
        self.assertNotIn("segreto", msg)


if __name__ == "__main__":
    unittest.main()
