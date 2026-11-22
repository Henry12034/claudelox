"""Test del ponte Loxone -> Govee senza Bluetooth: pacchetti e traduzione dei comandi di Loxone."""
from __future__ import annotations

import base64
import contextlib
import io
import json
import logging
import os
import random
import re
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

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
                                  "ALLOW_FROM": "10.0.0.2", "GOVEE_COLOR_MODE": "15"})
        self.assertEqual(s.address, "AA:BB:CC:DD:EE:01")
        self.assertEqual((s.scenes["movie"], s.scenes["test2"]), (16, 300))
        self.assertEqual(lg.parse("test2", s)[1][1][:5].hex(), "3305042c01")       # codice su due byte
        self.assertEqual(s.allow_from, {"10.0.0.2"})
        with self.assertRaises(ValueError):
            lg.Settings.from_env({"GOVEE_COLOR_MODE": "99"})

    def test_split_messages(self):
        self.assertEqual(lg.split_messages(b"off;color:1-2-3\r\n"), ["off", "color:1-2-3"])


class ColorParsingTest(unittest.TestCase):
    """Il colore si legge in tempo lineare, con le stesse risposte dell'espressione regolare di prima."""
    OLD = re.compile(r"colou?r\s*:\s*(\d+)\s*[-,;/ ]\s*(\d+)\s*[-,;/ ]\s*(\d+)")   # lenta con tanti spazi
    s = lg.Settings(address="AA:BB:CC:DD:EE:FF")

    def expected(self, msg: str) -> list:
        m = self.OLD.fullmatch(msg.strip().lower())
        if not m:
            return []
        r, g, b = (min(255, int(x)) for x in m.groups())
        if r == g == b == 0:
            return [("dark", lg.POWER_OFF)]
        return [("power", lg.POWER_ON), ("color", lg.color_frame("02", r, g, b))]

    def test_same_answers_as_the_old_expression(self):
        samples = ["color:1-2-3", "Colour: 1 , 2 ; 3", "COLOR:255/128/0", "color:1 2 3", "color:1  2\t 3",
                   "color:1\t2\t3", "color:1--2-3", "color:-1-2-3", "color:1-2-3-", "color:1-2", "color:1-2-3-4",
                   "color:1.5-2-3", "color:300-0-0", "color : 7-8-9", "color:", "color:a-b-c", "color:1 - 2 - 3",
                   "color:1 -- 2 - 3", "colr:1-2-3", "color:0-0-0", "color:0 0 0", "color:00-000-0", "color:1 ,2, 3",
                   "color:1,,2,3", "color:1\xa02\xa03", "color:1 \xa0 2-3", "color:٣-٢-١",
                   "color:1\n-2-3", "color:1-2-3 x", "  color:9-9-9  ", "color:1;2;3", "color:1/2 3"]
        rng = random.Random(8)
        samples += ["color:" + "".join(rng.choice("0159-,;/ \t.x") for _ in range(rng.randint(0, 10)))
                    for _ in range(2000)]
        numbers = ["0", "7", "255", "300", "015", "", "x", "1.5"]
        gaps = ["-", ",", ";", "/", " ", "  ", " - ", " ,\t", "\t", " \t", "--", ", ,", "", "\t-\t"]
        samples += ["color:" + rng.choice(numbers) + "".join(rng.choice(gaps) + rng.choice(numbers)
                                                             for _ in range(rng.choice((1, 2, 2, 2, 3))))
                    for _ in range(2000)]
        for msg in samples:
            self.assertEqual(lg.parse(msg, self.s), self.expected(msg), repr(msg))
        self.assertGreater(sum(1 for msg in samples if self.expected(msg)), 100)       # la prova non e' vuota

    def test_long_runs_of_spaces_do_not_block(self):
        """Con l'espressione di prima 1,6 KB di spazi fermavano il ponte per secondi, 60 KB per giorni."""
        start = time.perf_counter()
        self.assertIsNone(lg.color_values("1" + " " * 60000 + "x"))
        self.assertEqual(lg.color_values("1" + " " * 30000 + "2" + " " * 30000 + "- 3"), (1, 2, 3))
        self.assertEqual(lg.parse("color:1" + " " * 240 + "x", self.s), [])
        self.assertEqual(lg.parse("color:1" + " " * 60000 + "2 3", self.s), [])        # troppo lungo: non si legge
        self.assertEqual(lg.parse("scena:" + " " * 60000 + "aurora", self.s), [])
        self.assertLess(time.perf_counter() - start, 2.0)


class UdpTest(unittest.TestCase):
    """Pacchetti UDP in arrivo, senza rete: chi puo' comandare e quanto puo' essere lungo un pacchetto."""
    sender = ("10.0.0.9", 40000)

    def listener(self, **settings):
        got = []
        udp = lg.LoxoneUdp(lg.Settings(address="AA:BB:CC:DD:EE:FF", **settings), [SimpleNamespace(submit=got.append)])
        return udp, got

    def test_long_datagrams_are_ignored_and_reported_once(self):
        udp, got = self.listener()
        with self.assertLogs("loxone-govee", level="DEBUG") as logs:
            udp.datagram_received(b"color:1" + b" " * 1600 + b"x", self.sender)
            udp.datagram_received(b"off;" * 100, self.sender)
            udp.datagram_received(b"off", self.sender)
        self.assertEqual(got, [[("power", lg.POWER_OFF)]])                  # passa solo il comando normale
        self.assertEqual([r.levelname for r in logs.records][:2], ["WARNING", "DEBUG"])   # un avviso solo
        self.assertIn("1608 byte da 10.0.0.9", logs.output[0])
        self.assertNotIn("color", logs.output[0])                          # il contenuto non finisce nel log
        self.assertLess(len(logs.output[0]), 120)
        udp.datagram_received(b"off;" * (lg.MAX_DATAGRAM // 4), self.sender)   # 256 byte esatti: accettato
        self.assertEqual(len(got), 1 + lg.MAX_DATAGRAM // 4)

    def test_allow_from(self):
        udp, got = self.listener(allow_from={"10.0.0.2"})
        with self.assertLogs("loxone-govee", level="WARNING") as logs:
            udp.datagram_received(b"off", self.sender)
        self.assertEqual(got, [])
        self.assertIn("non in ALLOW_FROM", logs.output[0])
        udp.datagram_received(b"off", ("10.0.0.2", 40000))
        self.assertEqual(got, [[("power", lg.POWER_OFF)]])

    def test_open_allow_from_is_said_at_startup(self):
        with self.assertLogs("loxone-govee", level="WARNING") as logs:
            lg.warn_if_open(lg.Settings(address="AA:BB:CC:DD:EE:FF"))
        self.assertIn("ALLOW_FROM e' vuoto", logs.output[0])
        self.assertIn("chiunque", logs.output[0])
        self.assertIn("/etc/default/loxone-govee", logs.output[0])          # dice anche come restringerlo
        with self.assertNoLogs("loxone-govee", level="WARNING"):
            lg.warn_if_open(lg.Settings(address="AA:BB:CC:DD:EE:FF", allow_from={"10.0.0.2"}))


class StartupTest(unittest.TestCase):
    def main(self, argv, address):
        """Esegue main con l'ambiente indicato, senza Bluetooth: (codice di uscita o None, errori nel log, run)."""
        env = {"GOVEE_ADDRESS": address, "GOVEE_SCENE_FILE": "", "ALLOW_FROM": "", "GOVEE_SCENES": "",
               "GOVEE_COLOR_MODE": "02", "GOVEE_BRIGHTNESS_MAX": "255", "UDP_PORT": "55000"}
        records: list[logging.LogRecord] = []
        handler = logging.Handler()
        handler.emit = records.append
        code = None
        with mock.patch.dict(os.environ, env), mock.patch.object(lg.logging, "basicConfig"), \
                mock.patch.object(lg, "run_bridge", new=mock.Mock()), \
                mock.patch.object(lg, "direct", new=mock.Mock()), \
                mock.patch.object(lg.asyncio, "run") as run, \
                mock.patch.object(lg.log, "handlers", [handler]), mock.patch.object(lg.log, "propagate", False):
            try:
                lg.main(argv)
            except SystemExit as e:
                code = e.code
        return code, [r.getMessage() for r in records if r.levelno >= logging.ERROR], run

    def test_empty_address_exits_with_78_and_a_clear_message(self):
        for argv in (["run"], [], ["query"], ["prova", "off"]):
            code, errors, run = self.main(argv, " ")
            self.assertEqual(code, lg.EXIT_CONFIG, argv)
            self.assertEqual(code, 78)
            run.assert_not_called()                            # niente porta UDP, niente Bluetooth
            self.assertIn("GOVEE_ADDRESS e' vuoto", errors[0])
            self.assertIn("/etc/default/loxone-govee", errors[0])
        unit = (Path(lg.__file__).parent / "loxone-govee.service").read_text(encoding="utf-8")
        self.assertIn(f"RestartPreventExitStatus={lg.EXIT_CONFIG}\n", unit)    # systemd non lo riavvia a vuoto

    def test_with_an_address_the_bridge_starts(self):
        code, errors, run = self.main(["run"], "AA:BB:CC:DD:EE:FF")
        self.assertEqual((code, errors), (None, []))
        run.assert_called_once()


LIBRARY ={"message": "success", "status": 200, "data": {"categories": [
    {"categoryName": "Natural", "scenes": [
        {"sceneName": "Sunrise", "lightEffects": [{"sceneType": 0, "cmdVersion": 0, "sceneCode": 0, "scenceParam": ""}]},
        {"sceneName": "Aurora", "lightEffects": [
            {"scenceName": "A", "sceneType": 2, "cmdVersion": 0, "sceneCode": 215,
             "scenceParam": base64.b64encode(bytes(range(70))).decode()},
            {"scenceName": "B", "sceneType": 2, "cmdVersion": 1, "sceneCode": 2289, "scenceParam": "AAEC"}]},
        {"sceneName": "Solo nuovi", "lightEffects": [{"sceneType": 2, "cmdVersion": 1, "sceneCode": 9, "scenceParam": "AA=="}]},
        {"sceneName": "Musica", "lightEffects": [{"sceneType": 1, "cmdVersion": 0, "sceneCode": 3, "scenceParam": ""}]},
        {"sceneName": "Snow flake", "lightEffects": [{"sceneType": 0, "cmdVersion": 0, "sceneCode": 15,
                                                        "scenceParam": ""}]}]},
    {"categoryName": "Life", "scenes": [
        {"sceneName": "Aurora", "lightEffects": [{"sceneType": 0, "cmdVersion": 0, "sceneCode": 1, "scenceParam": ""}]}]}]}}


class SceneCatalogTest(unittest.TestCase):
    def test_multi_packet_layout(self):
        short = lg.multi_packet(0xA3, 0x02, bytes([9, 8, 7]))
        self.assertEqual(len(short), 2)                                   # sempre un pacchetto finale FF
        self.assertEqual(short[0][:8].hex(), "a3000102" + "02" + "090807")
        self.assertEqual(short[1][:3].hex(), "a3ff00")
        data = bytes(range(70))                                           # 14 + 17 + 17 + 17 + 5
        pkts = lg.multi_packet(0xA3, 0x02, data)
        self.assertEqual([p[1] for p in pkts], [0x00, 1, 2, 3, 0xFF])
        self.assertEqual(pkts[0][3], 5)                                   # numero totale di pacchetti
        joined = pkts[0][5:19] + b"".join(p[2:19] for p in pkts[1:-1]) + pkts[-1][2:7]
        self.assertEqual(joined, data)
        for p in pkts:
            self.assertEqual(len(p), 20)
            x = 0
            for b in p[:19]:
                x ^= b
            self.assertEqual(p[19], x)
        exact = lg.multi_packet(0xA3, 0x02, bytes(14 + 17))               # resto pieno: finisce nel pacchetto FF
        self.assertEqual([p[1] for p in exact], [0x00, 0xFF])

    def test_scene_frames(self):
        classic = lg.scene_frames(lg.Scene("Sunrise", 0))
        self.assertEqual(len(classic), 1)
        self.assertEqual(classic[0][:5].hex(), "3305040000")
        animated = lg.scene_frames(lg.Scene("Aurora", 215, bytes(range(70))))
        self.assertEqual(len(animated), 6)
        self.assertEqual(animated[0][:2].hex(), "a300")
        self.assertEqual(animated[-1][:5].hex(), "330504d700")
        self.assertEqual(lg.scene_frames(lg.Scene("x", 2289))[0][:5].hex(), "330504f108")

    def test_build_and_load_catalog(self):
        cat = lg.build_catalog(LIBRARY, "H6126")
        names = [x["nome"] for x in cat["scene"]]
        self.assertEqual(names, ["Sunrise", "Aurora", "Snow flake"])    # niente musica, niente solo-cmdVersion 1,
        self.assertEqual(cat["scene"][1]["codice"], 215)                 # il primo "Aurora" vince
        self.assertEqual(lg.build_catalog(LIBRARY, "H6126", 1)["scene"][0]["codice"], 2289)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "scene.json"
            path.write_text(json.dumps(cat), encoding="utf-8")
            loaded = lg.load_catalog(path)
            self.assertEqual(loaded["aurora"].param, bytes(range(70)))
            self.assertEqual(loaded["snowflake"].code, 15)
            s = lg.Settings.from_env({"GOVEE_ADDRESS": "x", "GOVEE_SCENE_FILE": str(path)})
        self.assertEqual(len(s.catalog), 3)
        ops = lg.parse("scena: aurora", s)
        self.assertEqual([k for k, _ in ops], ["power", "scene"])
        self.assertEqual(len(ops[1][1]), 6)
        self.assertEqual(lg.parse("Aurora", s)[1][1][-1][:4].hex(), "330504d7")
        self.assertEqual(lg.parse("Snowflake", s)[1][1][:4].hex(), "3305040f")   # scena classica: un pacchetto
        self.assertEqual(lg.parse("scena:inesistente", s), [])
        with self.assertRaises(ValueError):
            lg.build_catalog({"data": {"categories": []}}, "H0000")

    def test_broken_catalog_is_a_warning_not_a_crash(self):
        """Prima un catalogo rovinato o troncato faceva cadere ogni comando, anche quello che lo rifa'."""
        whole = json.dumps(lg.build_catalog(LIBRARY, "H6126"))
        broken = [whole[:len(whole) // 2], "", "[1, 2]", '{"scene": 7}', '{"scene": [{"nome": "Senza codice"}]}',
                  '{"scene": [{"nome": "Dati rovinati", "codice": 1, "dati": "A"}]}', b"\xff\xfe\x00"]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "scene.json"
            for content in broken:
                path.write_bytes(content if isinstance(content, bytes) else content.encode("utf-8"))
                with self.assertLogs("loxone-govee", level="WARNING") as logs:
                    s = lg.Settings.from_env({"GOVEE_ADDRESS": "x", "GOVEE_SCENE_FILE": str(path)})
                self.assertEqual(s.catalog, {}, content)
                self.assertIn("non leggibile", logs.output[0])
                self.assertEqual([k for k, _ in lg.parse("Snowflake", s)], ["power", "scene"])   # le classiche restano
            missing = str(Path(tmp) / "manca.json")
            with self.assertLogs("loxone-govee", level="WARNING") as logs:                 # chiesto ma assente
                self.assertEqual(lg.Settings.from_env({"GOVEE_SCENE_FILE": missing}).catalog, {})
            self.assertIn("non trovato", logs.output[0])
            with mock.patch.object(lg, "SCENE_FILE", missing), self.assertNoLogs("loxone-govee", level="WARNING"):
                self.assertEqual(lg.Settings.from_env({}).catalog, {})    # mai creato: e' normale, niente avviso
            with mock.patch.object(lg, "SCENE_FILE", tmp), self.assertLogs("loxone-govee", level="WARNING"):
                self.assertEqual(lg.Settings.from_env({}).catalog, {})    # una cartella al posto del file

    def test_catalog_is_written_whole_or_not_at_all(self):
        cat = lg.build_catalog(LIBRARY, "H6126")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "nuova" / "scene.json"
            lg.save_catalog(path, cat)                                     # crea anche la cartella
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), cat)
            self.assertEqual(os.listdir(path.parent), ["scene.json"])      # nessun file temporaneo rimasto
            for failing in ("dumps", "replace"):                           # scrittura o scambio che cadono a meta'
                target = lg.json if failing == "dumps" else lg.os
                with mock.patch.object(target, failing, side_effect=OSError("disco pieno")):
                    with self.assertRaises(OSError):
                        lg.save_catalog(path, {"scene": []})
                self.assertEqual(len(lg.load_catalog(path)), 3)            # il catalogo di prima resta intero

    def test_catalogo_command_replaces_a_broken_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            library = Path(tmp) / "libreria.json"
            library.write_text(json.dumps(LIBRARY), encoding="utf-8")
            path = Path(tmp) / "scene.json"
            path.write_text('{"scene": [', encoding="utf-8")               # rimasto a meta' (es. corrente mancata)
            out = io.StringIO()
            with mock.patch.dict(os.environ, {"GOVEE_SCENE_FILE": str(path), "GOVEE_ADDRESS": ""}), \
                    mock.patch.object(lg.logging, "basicConfig"), contextlib.redirect_stdout(out), \
                    self.assertLogs("loxone-govee", level="WARNING"):
                lg.main(["catalogo", "H6126", "--da-file", str(library)])
            self.assertEqual(len(lg.load_catalog(path)), 3)
            self.assertIn("3 scene per H6126", out.getvalue())


class LevelsTest(unittest.TestCase):
    s = lg.Settings(address="x", brightness_max=100, color_mode="15")

    def test_pale_colors_become_full_color_with_less_brightness(self):
        lv = lg.Levels()
        ops = dict(lg.parse("brightness:50", self.s, lv))
        self.assertEqual(ops["brightness"][2], 50)
        ops = lg.parse("color:76-0-0", self.s, lv)                         # rosso al 30% da Loxone
        self.assertEqual([k for k, _ in ops], ["power", "color", "brightness"])
        self.assertEqual(ops[1][1][4:7].hex(), "ff0000")                  # colore pieno
        self.assertEqual(ops[2][1][2], 15)                                # 50% x 30%
        self.assertEqual(dict(lg.parse("brightness:80", self.s, lv))["brightness"][2], 24)
        self.assertEqual(dict(lg.parse("color:255-128-0", self.s, lv))["brightness"][2], 80)
        ops = lg.parse("color:20-10-0", self.s, lv)
        self.assertEqual(ops[1][1][4:7].hex(), "ff8000")
        self.assertEqual(ops[2][1][2], 6)                                 # 80% x 20/255
        ops = lg.parse("Snowflake", self.s, lv)                           # scena: di nuovo piena intensita'
        self.assertEqual(ops[-1][1][2], 80)

    def test_without_loxone_brightness_and_dark(self):
        lv = lg.Levels()
        self.assertEqual(lg.parse("color:0-0-128", self.s, lv)[2][1][2], 50)   # nessuna luminosita': conta 100
        self.assertEqual(lg.parse("brightness:off", self.s, lv), [("dark", lg.POWER_OFF)])
        self.assertEqual(lv.level, 0)
        self.assertEqual(lg.parse("color:0-0-0", self.s, lv), [("dark", lg.POWER_OFF)])
        self.assertEqual(lv.factor, 128 / 255)                            # il nero non cambia l'intensita'
        self.assertEqual(lg.parse("brightness:1", self.s, lv)[1][1][2], 1)       # mai sotto 1 se accesa


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

    def together(self, *messages) -> dict:
        """Comandi arrivati insieme, come nel ponte (un solo Levels): cosa resta da mandare alla striscia."""
        s = lg.Settings(address="x", brightness_max=100, color_mode="15")
        s.catalog["aurora"] = lg.Scene("Aurora", 215, bytes(20))
        levels, p = lg.Levels(), lg.Pending()
        for msg in messages:
            p.add(lg.parse(msg, s, levels))
        return p.items

    def test_mood_changes_keep_brightness_and_scene(self):
        """Cambi di atmosfera: il nero del circuito colore non spegne e non fa perdere la luminosita' in attesa."""
        items = self.together("brightness:50", "color:0-0-0", "Snowflake")
        self.assertEqual(list(items), ["power", "brightness", "scene"])
        self.assertEqual((items["power"], items["brightness"][2]), (lg.POWER_ON, 50))
        items = self.together("brightness:70", "color:255-0-0", "color:0-0-0", "Aurora")
        self.assertEqual(sorted(items), ["brightness", "power", "scene"])
        self.assertEqual((items["power"], items["brightness"][2]), (lg.POWER_ON, 70))

    def test_known_limit_dark_right_after_a_value_of_the_same_kind(self):
        """Limite noto (README, "Limiti noti"): chi spegne arrivando subito dopo un valore dello stesso tipo cede.

        Il ponte non distingue questo caso dal nero che Loxone manda passando a una scena; farlo vincere fa
        perdere la luminosita' in attesa nel secondo cambio di atmosfera qui sopra. Con "off" si spegne sempre.
        """
        items = self.together("brightness:4", "brightness:off")
        self.assertEqual((items["power"], items["brightness"][2]), (lg.POWER_ON, 4))
        items = self.together("color:255-0-0", "color:0-0-0")
        self.assertEqual(items["power"], lg.POWER_ON)
        self.assertIn("color", items)
        self.assertEqual(self.together("brightness:4", "brightness:off", "off"), {"power": lg.POWER_OFF})


HARDENING = ("NoNewPrivileges=yes", "PrivateTmp=yes", "ProtectSystem=full", "ProtectHome=yes",
             "ProtectKernelTunables=yes", "ProtectKernelModules=yes", "ProtectControlGroups=yes",
             "RestrictSUIDSGID=yes", "LockPersonality=yes")


class ServiceFilesTest(unittest.TestCase):
    """Controlli sul testo dell'unita' systemd e di raspberry/install.sh, che qui non si possono eseguire."""

    def test_unit_is_hardened_without_breaking_bluetooth(self):
        lines = (Path(lg.__file__).parent / "loxone-govee.service").read_text(encoding="utf-8").splitlines()
        for wanted in HARDENING + ("User=pi", "EnvironmentFile=/etc/default/loxone-govee"):
            self.assertIn(wanted, lines)
        self.assertEqual([x for x in lines if x.startswith(("RestrictAddressFamilies", "DynamicUser"))], [])

    def test_install_script(self):
        text = (Path(lg.__file__).parents[2] / "raspberry" / "install.sh").read_text(encoding="utf-8")
        code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
        self.assertIn('SERVICE_USER="${SERVICE_USER:-${SUDO_USER:-pi}}"', code)    # non piu' "pi" fisso
        self.assertIsNone(re.search(r"(-o|-g|-u|-aG bluetooth) pi\b|\bpi:pi\b", code))
        self.assertEqual(code.count("apt-get install"), 1)                          # solo dentro apt_install ...
        self.assertLess(code.index("apt-get update"), code.index("apt-get install"))   # ... dopo l'aggiornamento
        for program in re.findall(r'^ *install -m \d+ .*"\$DEST/"$', code, flags=re.MULTILINE):
            self.assertIn("-o root -g root", program)                               # programmi di root
        self.assertEqual(len(re.findall(r'^ *install -m \d+ .*"\$DEST/"$', code, flags=re.MULTILINE)), 4)
        govee = code[code.index("install_govee() {"):code.index("fetch_model() {")]
        self.assertLess(govee.index("if govee_address_set; then"), govee.index("systemctl enable loxone-govee"))
        hik = code[code.index("install_hikvision() {"):code.index("install_amazon() {")]
        for optional in ("apt_install python3-opencv", "fetch_model face_detection", "fetch_model face_recognition"):
            self.assertLess(hik.index("systemctl restart hikvision-loxone.service"), hik.index(optional))
        self.assertEqual(hik.count("|| faces=0"), 2)                                # un modello mancato non ferma


if __name__ == "__main__":
    unittest.main()
