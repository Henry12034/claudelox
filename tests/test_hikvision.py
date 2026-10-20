"""Test del ponte Hikvision -> Loxone senza telecamere: configurazione, eventi XML, flusso multipart, regole."""
from __future__ import annotations

import io
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "raspberry"))

import hikvision_loxone as hk  # noqa: E402

CONFIG = """
[miniserver]
host = "192.168.1.10"
user = "raspberry"
password = "x"

[[cameras]]
name = "Esterna"
host = "192.168.1.20"
user = "u"
password = "p"

[[cameras.events]]
type = "linedetection"
region = 1
target = "human"
inputs = ["movimento_ingresso"]

[[cameras.events]]
type = "regionExiting"
inputs = ["uscita_area", "altro"]
cooldown = 0
"""

LINE_EVENT = b"""<?xml version="1.0" encoding="UTF-8"?>
<EventNotificationAlert version="2.0" xmlns="http://www.hikvision.com/ver20/XMLSchema">
<ipAddress>192.168.1.20</ipAddress><channelID>1</channelID><activePostCount>1</activePostCount>
<eventType>linedetection</eventType><eventState>active</eventState>
<DetectionRegionList><DetectionRegionEntry><regionID>1</regionID><detectionTarget>human</detectionTarget>
</DetectionRegionEntry></DetectionRegionList></EventNotificationAlert>"""

HEARTBEAT = (b'<EventNotificationAlert version="2.0" xmlns="http://www.hikvision.com/ver20/XMLSchema">'
             b"<eventType>videoloss</eventType><eventState>inactive</eventState></EventNotificationAlert>")


class FakeLoxone:
    def __init__(self):
        self.calls = []

    def trigger(self, name):
        self.calls.append(name)
        return True


class ConfigTest(unittest.TestCase):
    def test_load(self):
        ms, cams = hk.load_config(CONFIG)
        self.assertEqual(ms["host"], "192.168.1.10")
        self.assertEqual(cams[0].rules[0], hk.Rule("linedetection", ["movimento_ingresso"], 1, "human", 5.0))
        self.assertEqual(cams[0].rules[1].type, "regionexiting")

    def test_missing_fields(self):
        with self.assertRaises(ValueError):
            hk.load_config('[miniserver]\nhost = "x"\n')
        with self.assertRaises(ValueError):
            hk.load_config('[miniserver]\nhost = "x"\nuser = "u"\npassword = "p"\n')


    def test_dump_roundtrip_keeps_rules_and_odd_passwords(self):
        ms, cams = hk.load_config(CONFIG)
        ms["password"] = 'a"b\c	è#'                       # virgolette, barra, tab, accento, cancelletto
        cams[0].password = "x'y = [z]"
        text = hk.dump_config(ms, cams)
        ms2, cams2 = hk.load_config(text)
        self.assertEqual(ms2["password"], ms["password"])
        self.assertEqual(cams2[0].password, cams[0].password)
        self.assertEqual(cams2[0].rules, cams[0].rules)
        self.assertNotIn("command", ms2)                       # i campi vuoti non vengono scritti


class EventTest(unittest.TestCase):
    def test_parse_event_with_namespace(self):
        ev = hk.parse_event(LINE_EVENT)
        self.assertEqual((ev.type, ev.state, ev.regions, ev.target), ("linedetection", "active", {1}, "human"))
        self.assertEqual(hk.parse_event(HEARTBEAT).type, "videoloss")
        self.assertIsNone(hk.parse_event(b"<altro/>"))
        self.assertIsNone(hk.parse_event(b"non xml"))

    def test_rules(self):
        _, cams = hk.load_config(CONFIG)
        line, exiting = cams[0].rules
        ev = hk.parse_event(LINE_EVENT)
        self.assertTrue(hk.matches(line, ev))
        self.assertFalse(hk.matches(line, hk.Event("linedetection", "active", {2}, "human", "1")))    # altra linea
        self.assertFalse(hk.matches(line, hk.Event("linedetection", "active", {1}, "vehicle", "1")))  # veicolo
        self.assertFalse(hk.matches(line, hk.Event("linedetection", "inactive", {1}, "human", "1")))  # fine evento
        self.assertTrue(hk.matches(exiting, hk.Event("regionexiting", "active", set(), None, "1")))

    def test_worker_fires_once_per_cooldown(self):
        _, cams = hk.load_config(CONFIG)
        lox = FakeLoxone()
        w = hk.CameraWorker(cams[0], lox)
        for _ in range(5):                       # la telecamera ripete l'evento ogni secondo
            w.handle(hk.parse_event(LINE_EVENT))
        self.assertEqual(lox.calls, ["movimento_ingresso"])
        w.handle(hk.Event("regionexiting", "active", set(), None, "1"))
        self.assertEqual(lox.calls[1:], ["uscita_area", "altro"])


class MultipartTest(unittest.TestCase):
    def test_parts_with_length_and_jpeg(self):
        jpeg = b"\xff\xd8binary\r\n--boundary fake\r\n\xff\xd9"
        raw = (b"--boundary\r\nContent-Type: application/xml; charset=\"UTF-8\"\r\nContent-Length: %d\r\n\r\n%s\r\n"
               b"--boundary\r\nContent-Type: image/jpeg\r\nContent-Length: %d\r\n\r\n%s\r\n"
               b"--boundary\r\nContent-Type: application/xml\r\n\r\n%s\r\n--boundary\r\n"
               % (len(LINE_EVENT), LINE_EVENT, len(jpeg), jpeg, HEARTBEAT))
        parts = list(hk.iter_parts(io.BufferedReader(io.BytesIO(raw)), b"boundary"))
        self.assertEqual([p[0].split(";")[0] for p in parts], ["application/xml", "image/jpeg", "application/xml"])
        self.assertEqual(hk.parse_event(parts[0][1]).type, "linedetection")
        self.assertEqual(parts[1][1], jpeg)                  # il JPEG non confonde il separatore
        self.assertEqual(hk.parse_event(parts[2][1].strip()).type, "videoloss")


if __name__ == "__main__":
    unittest.main()
