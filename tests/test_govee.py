"""Test del ponte Loxone -> Govee senza Bluetooth: pacchetti e traduzione dei comandi di Loxone."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "integrazioni" / "govee-bluetooth"))

import loxone_govee as lg  # noqa: E402


class FrameTest(unittest.TestCase):
    def test_frame_layout_and_checksum(self):
        pkt = lg.frame(0x33, 0x01, [0x01])
        self.assertEqual(len(pkt), 20)
        self.assertEqual(pkt.hex(), "3301" + "01" + "00" * 16 + "33")  # 0x33 ^ 0x01 ^ 0x01 = 0x33
        self.assertEqual(lg.POWER_OFF[-1], 0x33 ^ 0x01)
        self.assertEqual(lg.KEEPALIVE.hex(), "aa01" + "00" * 17 + "ab")

    def test_color_variants(self):
        self.assertEqual(lg.color_frame("02", 255, 0, 16)[:6].hex(), "330502ff0010")
        self.assertEqual(lg.color_frame("0d", 1, 2, 3)[:6].hex(), "33050d010203")
        rgbic = lg.color_frame("15", 1, 2, 3)
        self.assertEqual(rgbic[:7].hex(), "33051501010203")
        self.assertEqual(rgbic[12:14].hex(), "ff7f")

    def test_payload_too_long(self):
        with self.assertRaises(ValueError):
            lg.frame(0x33, 0x05, [0] * 18)


class ParseTest(unittest.TestCase):
    s = lg.Settings(address="AA:BB:CC:DD:EE:FF")

    def kinds(self, msg, s=None):
        return [k for k, _ in lg.parse(msg, s or self.s)]

    def test_loxone_commands(self):
        self.assertEqual(lg.parse("off", self.s), [("power", lg.POWER_OFF)])
        self.assertEqual(lg.parse("accendi led", self.s), [("power", lg.POWER_ON)])
        self.assertEqual(self.kinds("Snowflake"), ["power", "scene"])
        self.assertEqual(lg.parse("Snowflake", self.s)[1][1][:4].hex(), "3305040f")
        self.assertEqual(self.kinds("Movie"), ["power", "scene"])
        self.assertEqual(self.kinds("qualcosa di strano"), [])

    def test_brightness(self):
        self.assertEqual(lg.parse("brightness:50", self.s)[1][1][2], 128)       # 50% di 255
        s100 = lg.Settings(address="x", brightness_max=100)
        self.assertEqual(lg.parse("brightness:50", s100)[1][1][2], 50)
        self.assertEqual(lg.parse("brightness:0", self.s), [("dark", lg.POWER_OFF)])
        self.assertEqual(lg.parse("brightness:off", self.s), [("dark", lg.POWER_OFF)])   # testo di Loxone
        self.assertEqual(lg.parse("brightness:on", self.s), [("power", lg.POWER_ON)])
        self.assertEqual(lg.parse("brightness:150", self.s)[1][1][2], 255)      # oltre il massimo
        self.assertEqual(lg.parse("brightness: 33,4", self.s)[1][1][2], 84)

    def test_color(self):
        ops = lg.parse("color:255-128-0", self.s)
        self.assertEqual(ops[1][1][:6].hex(), "330502ff8000")
        self.assertEqual(lg.parse("color:0-0-0", self.s), [("dark", lg.POWER_OFF)])
        self.assertEqual(lg.parse("color:300-0-0", self.s)[1][1][3], 255)

    def test_settings_from_env(self):
        s = lg.Settings.from_env({"GOVEE_ADDRESS": "aa:bb:cc:dd:ee:01", "GOVEE_SCENES": "movie=0x10, test2=300",
                                  "ALLOW_FROM": "192.168.1.10", "GOVEE_COLOR_MODE": "15"})
        self.assertEqual(s.address, "AA:BB:CC:DD:EE:01")
        self.assertEqual((s.scenes["movie"], s.scenes["test2"]), (16, 300))
        self.assertEqual(lg.parse("test2", s)[1][1][:5].hex(), "3305042c01")       # codice su due byte
        self.assertEqual(s.allow_from, {"192.168.1.10"})
        with self.assertRaises(ValueError):
            lg.Settings.from_env({"GOVEE_COLOR_MODE": "99"})

    def test_split_messages(self):
        self.assertEqual(lg.split_messages(b"off;color:1-2-3\r\n"), ["off", "color:1-2-3"])


class PendingTest(unittest.TestCase):
    def test_last_value_wins_and_off_clears(self):
        p = lg.Pending()
        s = lg.Settings(address="x")
        for v in (10, 20, 30):
            p.add(lg.parse(f"brightness:{v}", s))
        self.assertEqual(list(p.items), ["power", "brightness"])
        self.assertEqual(p.items["brightness"][2], round(30 * 255 / 100))
        p.add(lg.parse("Snowflake", s))
        p.add(lg.parse("color:1-2-3", s))
        self.assertNotIn("scene", p.items)
        p.add(lg.parse("off", s))
        self.assertEqual(p.items, {"power": lg.POWER_OFF})

    def test_loxone_bursts(self):
        """Sequenze reali di Loxone (20/10/2026): passaggio alla scena Neve e spegnimento."""
        s = lg.Settings(address="x")
        for order in (("color:0-0-0", "Snowflake"), ("Snowflake", "color:0-0-0")):
            p = lg.Pending()
            for msg in order:
                p.add(lg.parse(msg, s))
            self.assertEqual(list(p.items), ["power", "scene"], order)    # niente spegni-riaccendi
            self.assertEqual(p.items["power"], lg.POWER_ON)
        p = lg.Pending()
        for msg in ("brightness:50", "Snowflake"):                         # prima si accende, poi il resto
            p.add(lg.parse(msg, s))
        self.assertEqual(list(p.items), ["power", "brightness", "scene"])
        p = lg.Pending()
        for msg in ("brightness:off", "off"):
            p.add(lg.parse(msg, s))
        self.assertEqual(p.items, {"power": lg.POWER_OFF})
        p = lg.Pending()
        p.add(lg.parse("color:0-0-0", s))                                 # da solo spegne
        self.assertEqual(p.items, {"power": lg.POWER_OFF})
        p = lg.Pending()
        p.add(lg.parse("Snowflake", s))
        p.done("power", lg.POWER_ON)                                       # gia' inviato, scena in corso
        p.add(lg.parse("brightness:0", s))
        self.assertEqual(list(p.items), ["scene"])

    def test_done_keeps_newer_value(self):
        p = lg.Pending()
        s = lg.Settings(address="x")
        p.add(lg.parse("brightness:10", s))
        kind, old = list(p.items.items())[1]
        p.add(lg.parse("brightness:90", s))     # arriva mentre il vecchio valore viene inviato
        p.done(kind, old)
        self.assertIn("brightness", p.items)


if __name__ == "__main__":
    unittest.main()
