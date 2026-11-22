"""Test del ponte Hikvision -> Loxone senza telecamere: configurazione, eventi XML, flusso multipart, regole."""
from __future__ import annotations

import contextlib
import io
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

if sys.version_info < (3, 11):      # hikvision_loxone.py legge la configurazione con tomllib
    raise unittest.SkipTest("l'integrazione Hikvision richiede Python 3.11 o successivo (tomllib)")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "integrazioni" / "hikvision-isapi"))

import hikvision_loxone as hk  # noqa: E402

CONFIG = """
[miniserver]
host = "10.0.0.2"
user = "raspberry"
password = "x"

[[cameras]]
name = "Esterna"
host = "10.0.0.5"
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
<ipAddress>10.0.0.5</ipAddress><channelID>1</channelID><activePostCount>1</activePostCount>
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
        self.assertEqual(ms["host"], "10.0.0.2")
        self.assertEqual(cams[0].rules[0], hk.Rule("linedetection", ["movimento_ingresso"], 1, "human", 5.0))
        self.assertEqual(cams[0].rules[1].type, "regionexiting")

    def test_missing_fields(self):
        with self.assertRaises(ValueError):
            hk.load_config('[miniserver]\nhost = "x"\n')
        with self.assertRaises(ValueError):
            hk.load_config('[miniserver]\nhost = "x"\nuser = "u"\npassword = "p"\n')


    def test_dump_roundtrip_keeps_rules_and_odd_passwords(self):
        ms, cams = hk.load_config(CONFIG)
        ms["password"] = 'a"b\\c\tè#'                   # virgolette, barra, tab, accento, cancelletto
        cams[0].password = "x'y = [z]"
        text = hk.dump_config(ms, cams)
        ms2, cams2 = hk.load_config(text)
        self.assertEqual(ms2["password"], ms["password"])
        self.assertEqual(cams2[0].password, cams[0].password)
        self.assertEqual(cams2[0].rules, cams[0].rules)
        self.assertNotIn("command", ms2)                       # i campi vuoti non vengono scritti
        self.assertNotIn("dry_run", text)                      # eventi semplici: niente prova, come prima

    def test_unknown_rule_key_is_an_error(self):
        """Un errore di battitura non viene ignorato: prima la regola partiva attiva con somiglianza 0."""
        for wrong in ("min_similarty = 80", "dry-run = true"):
            with self.assertRaises(ValueError) as ctx:
                hk.load_config(CONFIG + wrong + "\n")          # la riga finisce nell'ultima regola
            msg = str(ctx.exception)
            self.assertIn(repr(wrong.split(" = ")[0]), msg)    # dice la chiave sbagliata ...
            self.assertIn("min_similarity", msg)               # ... elenca quelle valide ...
            self.assertIn("dry_run", msg)
            self.assertIn("telecamera Esterna, regola 2", msg)  # ... e dove sta
        with self.assertRaisesRegex(ValueError, "'region_id'"):
            hk.rule_from_dict({"type": "vmd", "inputs": ["x"], "region_id": 1})
        with tempfile.TemporaryDirectory() as d:                # il servizio lo dice in una riga e si ferma
            path = Path(d) / "config.toml"
            path.write_text(CONFIG + "min_similarty = 80\n", encoding="utf-8")
            with mock.patch.object(hk.logging, "basicConfig"), mock.patch.object(hk, "CameraWorker") as worker, \
                    mock.patch.object(hk.time, "sleep", side_effect=RuntimeError("non doveva partire")), \
                    self.assertRaises(SystemExit) as ctx:
                hk.main(["--config", str(path), "run"])
            worker.assert_not_called()
        said = str(ctx.exception.code)
        self.assertIn("ERRORE nella configurazione", said)
        self.assertIn("regola 2: chiave sconosciuta 'min_similarty' (chiavi valide: type, inputs,", said)
        self.assertEqual(set(hk.RULE_KEYS), set(hk.Rule.__dataclass_fields__))   # nessuna chiave vera rifiutata

    def test_face_rules_start_in_test_mode(self):
        """Le regole dei volti possono disinserire un antifurto: senza dry_run partono in prova."""
        nvr = {"type": "faceContrast", "person": "Mario", "min_similarity": 85, "inputs": ["mario_entrata"]}
        self.assertTrue(hk.rule_from_dict(nvr).dry_run)
        self.assertFalse(hk.rule_from_dict({**nvr, "dry_run": False}).dry_run)     # attiva solo se c'e' scritto
        self.assertTrue(hk.rule_from_dict({**nvr, "dry_run": True}).dry_run)
        self.assertFalse(hk.rule_from_dict({"type": "linedetection", "inputs": ["scale"]}).dry_run)   # come prima
        self.assertTrue(hk.rule_from_dict({"type": "linedetection", "inputs": ["scale"], "dry_run": True}).dry_run)
        with self.assertRaisesRegex(ValueError, "'dry_run' deve essere true o false"):
            hk.rule_from_dict({**nvr, "dry_run": "false"})                         # testo: sarebbe "vero"

    def test_face_rules_want_explicit_similarity(self):
        nvr = {"type": "faceContrast", "person": "Mario", "inputs": ["mario_entrata"]}
        with self.assertRaisesRegex(ValueError, "'min_similarity' scritto"):
            hk.rule_from_dict(nvr)                                                # prima: attiva allo 0%
        for bad in (0, -5, 0.0, float("nan")):
            with self.assertRaisesRegex(ValueError, "maggiore di 0"):
                hk.rule_from_dict({**nvr, "min_similarity": bad})
        for bad in ("85", True):
            with self.assertRaisesRegex(ValueError, "'min_similarity' scritto"):
                hk.rule_from_dict({**nvr, "min_similarity": bad})
        self.assertEqual(hk.rule_from_dict({**nvr, "min_similarity": 20}).min_similarity, 20.0)
        with self.assertRaisesRegex(ValueError, "'person' e' vuoto"):
            hk.rule_from_dict({**nvr, "person": " ", "min_similarity": 85})
        self.assertEqual(hk.rule_from_dict({"type": "vmd", "inputs": ["x"]}).min_similarity, 0.0)   # eventi semplici

    def test_dump_writes_dry_run_and_window_for_face_rules(self):
        """Una regola dei volti resa attiva resta attiva dopo salvataggio e rilettura; "window" non si perde."""
        ms, cams = hk.load_config(CONFIG)
        cam = cams[0]
        cam.rules = []
        hk.change_rules(cam, '{type="faceContrast", person="Mario", min_similarity=85, window=12, '
                             'inputs=["mario_entrata"]}', None, None)
        self.assertTrue(cam.rules[0].dry_run)
        hk.change_rules(cam, None, None, ["1", "no"])          # regole --prova 1 no
        text = hk.dump_config(ms, cams)
        self.assertIn("dry_run = false", text)
        self.assertIn("window = 12", text)                     # una sola conferma: prima tornava 20
        again = hk.load_config(text)[1][0].rules[0]
        self.assertEqual(again, cam.rules[0])
        self.assertEqual((again.dry_run, again.confirmations, again.window), (False, 1, 12.0))
        hk.change_rules(cam, None, None, ["1", "si"])
        self.assertTrue(hk.load_config(hk.dump_config(ms, cams))[1][0].rules[0].dry_run)
        plain = hk.Rule("linedetection", ["scale"], window=7)   # anche per gli eventi semplici
        cam.rules = [plain]
        self.assertEqual(hk.load_config(hk.dump_config(ms, cams))[1][0].rules[0], plain)

    def test_readme_commands_and_example_file_are_valid(self):
        """Gli esempi scritti per le persone devono funzionare cosi' come sono (una riga, chiavi giuste)."""
        folder = Path(hk.__file__).parent
        tables = re.findall(r"--aggiungi '(\{.*\})'", (folder / "README.md").read_text(encoding="utf-8"))
        cam = hk.Camera("Scale", "10.0.0.5", "u", "p")
        for table in tables:
            hk.change_rules(cam, table, None, None)
        self.assertEqual([r.type for r in cam.rules], ["linedetection", "volto"])
        self.assertEqual((cam.rules[0].dry_run, cam.rules[1].dry_run), (False, True))   # il volto parte in prova
        example = (folder / "hikvision-loxone.example.toml").read_text(encoding="utf-8")
        self.assertFalse(hk.load_options(example)["save_unknown_faces"])       # diagnostica spenta se non si tocca
        example = re.sub(r'^(user|password) = ""', r'\1 = "x"', example, flags=re.MULTILINE)
        self.assertEqual(len(hk.load_config(example)[1][0].rules), 1)
        # ... e con tutte le righe di esempio commentate rese attive
        example = re.sub(r"^# (?=\[\[cameras\.events\]\]|\w+ = )", "", example, flags=re.MULTILINE)
        self.assertTrue(hk.load_options(example)["save_unknown_faces"])
        rules = hk.load_config(example)[1][0].rules
        self.assertEqual([(r.type, r.person, r.min_similarity, r.dry_run) for r in rules],
                         [("linedetection", None, 0.0, False), ("facecontrast", "Mario", 85.0, True),
                          ("volto", "Mario", 45.0, True)])
        self.assertEqual((rules[0].channel, rules[2].continuous, rules[2].window), (2, True, 8.0))
        without = hk.load_config(example.replace("dry_run = true", "# dry_run = true"))[1][0].rules
        self.assertEqual([r.dry_run for r in without], [False, True, True])    # anche senza la riga: prova

    def test_options(self):
        self.assertEqual(hk.load_options(CONFIG), {"save_unknown_faces": False})
        self.assertTrue(hk.load_options("save_unknown_faces = true\n" + CONFIG)["save_unknown_faces"])
        with self.assertRaisesRegex(ValueError, "save_unknown_faces deve essere true o false"):
            hk.load_options('save_unknown_faces = "si"\n' + CONFIG)
        ms, cams = hk.load_config(CONFIG)
        self.assertNotIn("save_unknown_faces", hk.dump_config(ms, cams))
        self.assertNotIn("save_unknown_faces", hk.dump_config(ms, cams, {"save_unknown_faces": False}))
        text = hk.dump_config(ms, cams, {"save_unknown_faces": True})
        self.assertLess(text.index("save_unknown_faces = true"), text.index("[miniserver]"))
        self.assertTrue(hk.load_options(text)["save_unknown_faces"])     # resta dopo "regole --aggiungi"
        self.assertEqual(hk.load_config(text)[1][0].rules, cams[0].rules)


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

    def test_channel_filter_for_recorders(self):
        """Registratore (NVR): un solo flusso eventi per piu' telecamere, distinte dal canale."""
        rule = hk.Rule("linedetection", ["scale"], channel=3)
        self.assertTrue(hk.matches(rule, hk.Event("linedetection", "active", {1}, "human", "3")))
        self.assertFalse(hk.matches(rule, hk.Event("linedetection", "active", {1}, "human", "1")))
        nvr_event = LINE_EVENT.replace(b"<channelID>1</channelID>", b"<dynChannelID>3</dynChannelID>")
        self.assertEqual(hk.parse_event(nvr_event).channel, "3")
        ms, cams = hk.load_config(CONFIG)
        cams[0].rules.append(rule)
        self.assertEqual(hk.load_config(hk.dump_config(ms, cams))[1][0].rules[-1].channel, 3)


NVR_XML = {
    "/ISAPI/System/deviceInfo": "<DeviceInfo><model>NVR-TEST</model><deviceType>NVR</deviceType>"
                                "<firmwareVersion>V1</firmwareVersion></DeviceInfo>",
    "/ISAPI/ContentMgmt/InputProxy/channels": (
        '<InputProxyChannelList xmlns="http://www.hikvision.com/ver20/XMLSchema">'
        "<InputProxyChannel><id>1</id><name>Scale</name><sourceInputPortDescriptor><ipAddress>10.0.0.5"
        "</ipAddress></sourceInputPortDescriptor></InputProxyChannel>"
        "<InputProxyChannel><id>2</id><name>Garage</name><sourceInputPortDescriptor><ipAddress>10.0.0.6"
        "</ipAddress></sourceInputPortDescriptor></InputProxyChannel></InputProxyChannelList>"),
    "/ISAPI/ContentMgmt/InputProxy/channels/status": (
        "<InputProxyChannelStatusList><InputProxyChannelStatus><id>1</id><online>true</online>"
        "</InputProxyChannelStatus><InputProxyChannelStatus><id>2</id><online>false</online>"
        "</InputProxyChannelStatus></InputProxyChannelStatusList>"),
    "/ISAPI/Smart/LineDetection/1": "<LineDetection><id>1</id><enabled>true</enabled></LineDetection>",
    "/ISAPI/Smart/LineDetection/2": "<LineDetection><id>2</id><enabled>false</enabled></LineDetection>",
    "/ISAPI/System/Video/inputs/channels/1/motionDetection": "<MotionDetection><enabled>true</enabled></MotionDetection>",
    "/ISAPI/Event/triggers": (
        "<EventTriggerList><EventTrigger><eventType>linedetection</eventType><videoInputChannelID>1"
        "</videoInputChannelID><EventTriggerNotificationList><EventTriggerNotification><notificationMethod>center"
        "</notificationMethod></EventTriggerNotification></EventTriggerNotificationList></EventTrigger>"
        "<EventTrigger><eventType>VMD</eventType><videoInputChannelID>1</videoInputChannelID>"
        "<EventTriggerNotificationList><EventTriggerNotification><notificationMethod>beep</notificationMethod>"
        "</EventTriggerNotification></EventTriggerNotificationList></EventTrigger>"
        "<EventTrigger><eventType>diskfull</eventType></EventTrigger></EventTriggerList>"),
}


def fake_get(cam, path):
    text = NVR_XML.get(path)
    if text is None:
        return None, "HTTP 404"
    return hk._strip_ns(hk.ET.fromstring(text)), ""


class DeviceInfoTest(unittest.TestCase):
    def test_recorder_report(self):
        cam = hk.Camera("interna", "10.0.0.2", "u", "p")
        lines = hk.describe_device(cam, fake_get)
        self.assertIn("NVR-TEST, tipo NVR", lines[0])
        self.assertIn("canale 1: Scale, 10.0.0.5 (in linea); rilevazioni attive: attraversamento linea, movimento",
                      lines[1])
        self.assertIn("canale 2: Garage, 10.0.0.6 (NON in linea); rilevazioni attive: nessuna", lines[2])
        self.assertEqual(lines[3], "  eventi che arrivano al Raspberry: attraversamento linea (canali 1)")
        self.assertIn("movimento (canali 1); disco pieno", lines[4])
        self.assertTrue(lines[5].endswith("non disponibile"))
        self.assertFalse(any("p" == part for line in lines for part in line.split()))   # niente password

    def test_redact_xml(self):
        root = hk._strip_ns(hk.ET.fromstring(
            '<X xmlns="http://www.hikvision.com/ver20/XMLSchema"><name>Scale</name><password>segreta</password>'
            "<Sub><apiKey>k</apiKey><passwordLevel></passwordLevel></Sub></X>"))
        text = hk.ET.tostring(hk.redact_xml(root), encoding="unicode")
        self.assertNotIn("segreta", text)
        self.assertNotIn(">k<", text)
        self.assertIn("<name>Scale</name>", text)
        self.assertTrue(hk.PRIVATE_PATH_RE.search("/ISAPI/Security/users"))
        self.assertFalse(hk.PRIVATE_PATH_RE.search("/ISAPI/Intelligent/FDLib"))

    SECRETS_XML = (
        '<Network xmlns="http://www.hikvision.com/ver20/XMLSchema"><EZVIZ><enabled>true</enabled>'
        "<verificationCode>ABCDEF</verificationCode></EZVIZ><SNMPv1c><readCommunity>pubblica</readCommunity>"
        "<writeCommunity>riservata</writeCommunity></SNMPv1c><WPA><sharedKey>chiave-wifi</sharedKey>"
        "<PSK>psk-wifi</PSK><wpsPinCode>12345670</wpsPinCode></WPA><adminPwd>pwd-uno</adminPwd>"
        "<userPasswd>pwd-due</userPasswd><clientSecret>segreto</clientSecret><accessToken>gettone</accessToken>"
        "<model>DS-TEST</model><portNo>8000</portNo></Network>")
    SECRET_VALUES = ("ABCDEF", "pubblica", "riservata", "chiave-wifi", "psk-wifi", "12345670", "pwd-uno", "pwd-due",
                     "segreto", "gettone")

    def test_redact_codes_communities_and_keys(self):
        """Codice di verifica EZVIZ, community SNMP, PSK e PIN: mai stampati, con qualunque maiuscola."""
        text = hk.ET.tostring(hk.redact_xml(hk._strip_ns(hk.ET.fromstring(self.SECRETS_XML))), encoding="unicode")
        for value in self.SECRET_VALUES:
            self.assertNotIn(value, text)
        self.assertEqual(text.count("***"), len(self.SECRET_VALUES))
        for kept in ("<enabled>true</enabled>", "<model>DS-TEST</model>", "<portNo>8000</portNo>"):
            self.assertIn(kept, text)

    def test_leggi_prints_redacted_answer(self):
        root = hk._strip_ns(hk.ET.fromstring(self.SECRETS_XML))
        out = io.StringIO()
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "config.toml"
            path.write_text(CONFIG, encoding="utf-8")
            with mock.patch.object(hk, "_get_xml", lambda cam, percorso, attesa=10: (root, "")), \
                    mock.patch.object(hk.logging, "basicConfig"), contextlib.redirect_stdout(out):
                hk.main(["--config", str(path), "leggi", "Esterna", "/ISAPI/System/Network/EZVIZ"])
        for value in self.SECRET_VALUES:
            self.assertNotIn(value, out.getvalue())
        self.assertIn("<verificationCode>***</verificationCode>", out.getvalue())
        self.assertIn("<model>DS-TEST</model>", out.getvalue())

    def test_unreachable(self):
        cam = hk.Camera("x", "10.0.0.9", "u", "p")
        self.assertEqual(hk.describe_device(cam, lambda c, p: (None, "ConnectTimeout")),
                         ["x (10.0.0.9): non leggibile (ConnectTimeout)"])


FACE_EVENT = b"""<EventNotificationAlert version="2.0" xmlns="http://www.hikvision.com/ver20/XMLSchema">
<ipAddress>10.0.0.2</ipAddress><channelID>2</channelID><eventType>faceContrast</eventType><eventState>active</eventState>
<channelName>Ingresso</channelName><name>registratore</name>
<FaceContrastResult><faceContrastInfo><FDLibName>Casa</FDLibName><similarity>0.87</similarity>
<PersonInfo><name>Mario</name></PersonInfo></faceContrastInfo></FaceContrastResult></EventNotificationAlert>"""


class FaceEventTest(unittest.TestCase):
    def rule(self, **kw):
        base = dict(type="facecontrast", inputs=["mario_entrata"], channel=2, person="Mario", min_similarity=80,
                    cooldown=60)
        base.update(kw)
        return hk.Rule(**base)

    def test_parse_face_event(self):
        ev = hk.parse_event(FACE_EVENT)
        self.assertEqual((ev.type, ev.channel, ev.person), ("facecontrast", "2", "Mario"))
        self.assertAlmostEqual(ev.similarity, 87.0)
        percent = FACE_EVENT.replace(b"0.87", b"91")                   # altri modelli: gia' in percento
        self.assertAlmostEqual(hk.parse_event(percent).similarity, 91.0)
        self.assertIsNone(hk.parse_event(LINE_EVENT).person)          # gli altri eventi non hanno persona

    def candidates(self, inner: bytes) -> hk.Event:
        return hk.parse_event(b'<EventNotificationAlert xmlns="http://www.hikvision.com/ver20/XMLSchema">'
                              b"<channelID>2</channelID><eventType>faceContrast</eventType><eventState>active"
                              b"</eventState><name>registratore</name><FaceContrastResult>" + inner
                              + b"</FaceContrastResult></EventNotificationAlert>")

    def test_several_candidates_name_goes_with_its_similarity(self):
        """Piu' candidati: vale il piu' somigliante con il SUO nome (prima: primo nome + somiglianza piu' alta)."""
        mario = (b"<faceContrastInfo><similarity>0.41</similarity><PersonInfo><name>Mario</name></PersonInfo>"
                 b"</faceContrastInfo>")
        luigi = (b"<faceContrastInfo><similarity>0.93</similarity><PersonInfo><name>Luigi</name></PersonInfo>"
                 b"</faceContrastInfo>")
        for inner in (mario + luigi, luigi + mario, b"<maxSimilarity>0.93</maxSimilarity>" + mario + luigi):
            ev = self.candidates(inner)
            self.assertEqual((ev.person, round(ev.similarity)), ("Luigi", 93))
            self.assertFalse(hk.matches(self.rule(), ev))             # Mario all'80%: no, lui e' al 41%
            self.assertTrue(hk.matches(self.rule(person="Luigi"), ev))
        ev = self.candidates(mario + luigi.replace(b"0.93", b"0.30"))
        self.assertEqual((ev.person, round(ev.similarity)), ("Mario", 41))

    def test_candidates_that_cannot_be_told_apart_match_nobody(self):
        """Nomi e somiglianze in elenchi separati: non si sa di chi sia il valore, quindi nessuna persona."""
        ev = self.candidates(b"<names><name>Mario</name><name>Luigi</name></names>"
                             b"<values><similarity>0.41</similarity><similarity>0.93</similarity></values>")
        self.assertEqual((ev.person, round(ev.similarity)), (None, 93))
        self.assertFalse(hk.matches(self.rule(), ev))
        self.assertFalse(hk.matches(self.rule(person="Luigi"), ev))

    def test_single_candidate_formats_still_work(self):
        both = self.candidates(b"<faceContrastInfo><similarity>0.87</similarity><maxSimilarity>0.90</maxSimilarity>"
                               b"<PersonInfo><name>Mario</name><humanName>Mario Rossi</humanName></PersonInfo>"
                               b"</faceContrastInfo>")
        self.assertEqual((both.person, round(both.similarity)), ("Mario", 90))
        flat = self.candidates(b"<similarity>88</similarity><personName>Mario</personName>")
        self.assertEqual((flat.person, round(flat.similarity)), ("Mario", 88))
        apart = hk.parse_event(FACE_EVENT.replace(b"<PersonInfo><name>Mario</name></PersonInfo>", b"").replace(
            b"</EventNotificationAlert>", b"<Person><name>Mario</name></Person></EventNotificationAlert>"))
        self.assertEqual((apart.person, round(apart.similarity)), ("Mario", 87))   # nome in un altro ramo
        unknown = self.candidates(b"<faceContrastInfo><similarity>0.35</similarity></faceContrastInfo>")
        self.assertEqual((unknown.person, round(unknown.similarity)), (None, 35))
        nameless = self.candidates(b"<PersonInfo><name>Mario</name></PersonInfo>")
        self.assertEqual((nameless.person, nameless.similarity), ("Mario", None))
        self.assertFalse(hk.matches(self.rule(), nameless))           # senza somiglianza non scatta

    def test_face_rule_checks_person_similarity_and_type(self):
        ev = hk.parse_event(FACE_EVENT)
        self.assertTrue(hk.matches(self.rule(), ev))
        self.assertFalse(hk.matches(self.rule(person="Luigi"), ev))
        self.assertFalse(hk.matches(self.rule(min_similarity=90), ev))
        self.assertFalse(hk.matches(self.rule(channel=1), ev))
        failure = hk.parse_event(FACE_EVENT.replace(b"faceContrast<", b"faceContrastFailure<"))
        self.assertFalse(hk.matches(self.rule(), failure))            # volto sconosciuto: mai

    def test_confirmations_and_dry_run(self):
        lox = FakeLoxone()
        cam = hk.Camera("interna", "10.0.0.2", "u", "p", [self.rule(confirmations=2, window=20)])
        w = hk.CameraWorker(cam, lox)
        ev = hk.parse_event(FACE_EVENT)
        w.handle(ev, now=100.0)
        w.handle(ev, now=100.3)                 # stesso evento ripetuto: non conta come seconda conferma
        self.assertEqual(lox.calls, [])
        w.handle(ev, now=104.0)
        self.assertEqual(lox.calls, ["mario_entrata"])
        w.handle(ev, now=110.0)
        w.handle(ev, now=112.0)                 # entro il cooldown di 60 s: niente
        self.assertEqual(lox.calls, ["mario_entrata"])
        w2 = hk.CameraWorker(hk.Camera("i", "h", "u", "p", [self.rule(dry_run=True)]), lox)
        with self.assertLogs("hikvision-loxone", level="WARNING") as logs:
            w2.handle(ev, now=200.0)
        self.assertEqual(lox.calls, ["mario_entrata"])  # in prova non comanda Loxone
        self.assertIn("PROVA", logs.output[0])

    def test_config_roundtrip_face_rule(self):
        ms, cams = hk.load_config(CONFIG)
        cams[0].rules = [self.rule(confirmations=2, window=15, dry_run=True)]
        rule = hk.load_config(hk.dump_config(ms, cams))[1][0].rules[0]
        self.assertEqual((rule.person, rule.min_similarity, rule.confirmations, rule.window, rule.dry_run),
                         ("Mario", 80.0, 2, 15.0, True))

    def test_change_rules(self):
        cam = hk.Camera("interna", "10.0.0.2", "u", "p")
        hk.change_rules(cam, '{type="faceContrast", channel=2, person="Mario", min_similarity=80, confirmations=2, '
                             'inputs=["mario_entrata"], cooldown=120, dry_run=true}', None, None)
        self.assertEqual(hk.describe_rule(cam.rules[0]),
                         "evento facecontrast, canale 2, persona Mario almeno 80%, 2 conferme in 20 s -> "
                         "mario_entrata (pausa 120 s) [PROVA: non comanda Loxone]")
        hk.change_rules(cam, None, None, ["1", "no"])
        self.assertFalse(cam.rules[0].dry_run)
        with self.assertRaises(IndexError):
            hk.change_rules(cam, None, 2, None)
        with self.assertRaises(ValueError):
            hk.change_rules(cam, '{type="vmd"}', None, None)           # mancano gli ingressi
        hk.change_rules(cam, None, 1, None)
        self.assertEqual(cam.rules, [])

    def test_save_face_file_keeps_last(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d, mock.patch.object(hk, "FACE_KEEP", 3):
            for i in range(5):
                hk.save_face_file(f"evento{i}", ".xml", b"<x/>", stamp=False, folder=d)
            self.assertEqual(sorted(Path(d).iterdir())[0].name, "evento2.xml")
            self.assertEqual(len(list(Path(d).iterdir())), 3)


OK_STATUS = (b'<ResponseStatus version="2.0" xmlns="http://www.hikvision.com/ver20/XMLSchema"><statusCode>1'
             b"</statusCode><statusString>OK</statusString><subStatusCode>ok</subStatusCode></ResponseStatus>")


class IsapiWriteTest(unittest.TestCase):
    def test_isapi_error(self):
        bad = (b"<ResponseStatus><statusCode>6</statusCode><statusString>Invalid Content</statusString>"
               b"<subStatusCode>badXmlContent</subStatusCode></ResponseStatus>")
        self.assertIsNone(hk.isapi_error(200, OK_STATUS))
        self.assertEqual(hk.isapi_error(400, bad), "Invalid Content badXmlContent (HTTP 400)")
        self.assertIsNone(hk.isapi_error(200, b"<FDLibInfoList/>"))
        self.assertEqual(hk.isapi_error(500, b"errore"), "HTTP 500")

    def test_face_search(self):
        xml = (b"<FDSearchResult><MatchList><MatchElement><FDID>F</FDID><PID>P1</PID><FaceAppendData><name>Mario"
               b"</name></FaceAppendData><modelStatus>success</modelStatus></MatchElement></MatchList></FDSearchResult>")
        self.assertEqual(hk.parse_face_search(xml), [{"PID": "P1", "name": "Mario", "modello": "pronto", "qualita": ""}])
        with_status = xml.replace(b"<modelStatus>success</modelStatus>",
                                  b"<modelStatus>modeling</modelStatus><ModelingStatus><status>failed</status>"
                                  b"</ModelingStatus><faceScore>40</faceScore>")
        self.assertEqual(hk.parse_face_search(with_status)[0]["modello"], "non riuscito (foto da cambiare)")
        self.assertEqual(hk.parse_face_search(with_status)[0]["qualita"], "40")

    def test_face_contrast_keeps_other_settings(self):
        current = ('<FaceContrastList version="2.0" xmlns="http://www.hikvision.com/ver20/XMLSchema"><FaceContrast>'
                   "<id>1</id><enable>false</enable><thresholdValue>75</thresholdValue><FDLibList></FDLibList>"
                   "<contrastFailureAlarmUpload>true</contrastFailureAlarmUpload></FaceContrast>"
                   "</FaceContrastList>").encode()
        sent = {}

        def fake_isapi(cam, method, path, body=None, files=None, timeout=30):
            if method == "GET":
                return 200, current
            sent["body"] = body
            return 200, OK_STATUS
        with mock.patch.object(hk, "_isapi", fake_isapi):
            hk.set_face_contrast(hk.Camera("n", "h", "u", "p"), "2", "ABC", 80)
        self.assertIn('xmlns="http://www.hikvision.com/ver20/XMLSchema"', sent["body"])
        root = hk._strip_ns(hk.ET.fromstring(sent["body"]))
        self.assertEqual(root.findtext("FaceContrast/enable"), "true")
        self.assertEqual(root.findtext("FaceContrast/thresholdValue"), "80")
        self.assertEqual(root.findtext("FaceContrast/FDLibList/FDLib/FDID"), "ABC")
        self.assertEqual(root.findtext("FaceContrast/contrastFailureAlarmUpload"), "true")    # non toccato

    def test_face_detection_switch(self):
        current = (b'<FaceDetect version="2.0" xmlns="http://www.hikvision.com/ver20/XMLSchema"><id>2</id>'
                   b"<enabled>false</enabled><sensitivityLevel>3</sensitivityLevel></FaceDetect>")
        sent = {}

        def fake_isapi(cam, method, path, body=None, files=None, timeout=30):
            sent[method] = path
            if method == "PUT":
                sent["body"] = body
            return 200, current if method == "GET" else OK_STATUS
        with mock.patch.object(hk, "_isapi", fake_isapi):
            hk.set_face_detection(hk.Camera("n", "h", "u", "p"), "2", True, 4)
        self.assertEqual(sent["PUT"], "/ISAPI/Smart/FaceDetect/2")
        root = hk._strip_ns(hk.ET.fromstring(sent["body"]))
        self.assertEqual((root.findtext("enabled"), root.findtext("sensitivityLevel")), ("true", "4"))

    def test_trigger_center(self):
        state = {"xml": (b'<EventTrigger version="2.0" xmlns="http://www.hikvision.com/ver20/XMLSchema">'
                         b"<id>facedetection-2</id><eventType>facedetection</eventType><EventTriggerNotificationList>"
                         b"<EventTriggerNotification><id>record-2</id><notificationMethod>record</notificationMethod>"
                         b"</EventTriggerNotification></EventTriggerNotificationList></EventTrigger>")}

        def fake_isapi(cam, method, path, body=None, files=None, timeout=30):
            if method == "PUT":
                state["xml"] = body.encode()
            return 200, state["xml"] if method == "GET" else OK_STATUS

        def methods():
            root = hk._strip_ns(hk.ET.fromstring(state["xml"]))
            return [n.findtext("notificationMethod") for n in root.iter("EventTriggerNotification")]
        cam = hk.Camera("n", "h", "u", "p")
        with mock.patch.object(hk, "_isapi", fake_isapi):
            hk.set_trigger_center(cam, "facedetection-2", True)
            hk.set_trigger_center(cam, "facedetection-2", True)          # gia' presente: non si duplica
            self.assertEqual(methods(), ["record", "center"])
            hk.set_trigger_center(cam, "facedetection-2", False)
            self.assertEqual(methods(), ["record"])

    def test_face_results(self):
        xml = (b"<FCSearchResult><MatchList><MatchElement><snapTime>2026-10-20T17:20:01+01:00</snapTime>"
               b"<channelID>2</channelID><FaceMatchInfoList><FaceMatchInfo><FDID>F</FDID><name>Mario</name>"
               b"<similarity>0.91</similarity></FaceMatchInfo></FaceMatchInfoList></MatchElement>"
               b"<MatchElement><snapTime>2026-10-20T17:22:00+01:00</snapTime><channelID>2</channelID>"
               b"</MatchElement></MatchList></FCSearchResult>")
        found = hk.parse_face_results(xml)
        self.assertEqual((found[0]["persona"], round(found[0]["somiglianza"]), found[0]["canale"]), ("Mario", 91, "2"))
        self.assertEqual((found[1]["persona"], found[1]["somiglianza"]), (None, None))   # volto non riconosciuto

    def test_upgrade_outcome(self):
        reboot = b"<ResponseStatus><statusCode>7</statusCode><subStatusCode>rebootRequired</subStatusCode></ResponseStatus>"
        bad = (b"<ResponseStatus><statusCode>6</statusCode><statusString>Invalid Content</statusString>"
               b"<subStatusCode>upgradeFailed</subStatusCode></ResponseStatus>")
        self.assertTrue(hk.upgrade_outcome(200, reboot))
        self.assertIs(hk.upgrade_outcome(200, OK_STATUS), False)
        self.assertIsNone(hk.upgrade_outcome(400, bad))
        self.assertIsNone(hk.upgrade_outcome(500, b"errore"))

    def test_upgrade_refuses_wrong_files(self):
        import tempfile
        cam = hk.Camera("n", "h", "u", "p")
        with tempfile.TemporaryDirectory() as d:
            small = Path(d) / "digicap.dav"
            small.write_bytes(b"x" * 1000)
            with self.assertRaisesRegex(RuntimeError, "troppo piccolo"):
                hk.upgrade_firmware(cam, str(small))
            with self.assertRaisesRegex(RuntimeError, ".dav"):
                hk.upgrade_firmware(cam, str(Path(d) / "firmware.zip"))

    def test_write_raises_with_reason(self):
        with mock.patch.object(hk, "_isapi", lambda *a, **k: (403, b"<ResponseStatus><statusCode>4</statusCode>"
                                                              b"<statusString>Invalid Operation</statusString>"
                                                              b"<subStatusCode>notSupport</subStatusCode>"
                                                              b"</ResponseStatus>")):
            with self.assertRaisesRegex(RuntimeError, "notSupport"):
                hk._write(hk.Camera("n", "h", "u", "p"), "PUT", "/ISAPI/System/time", "<Time/>")


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


class ServiceFileTest(unittest.TestCase):
    def test_unit_is_hardened_and_keeps_its_state_directory(self):
        lines = (Path(hk.__file__).parent / "hikvision-loxone.service").read_text(encoding="utf-8").splitlines()
        for wanted in ("NoNewPrivileges=yes", "PrivateTmp=yes", "ProtectSystem=full", "ProtectHome=yes",
                       "ProtectKernelTunables=yes", "ProtectKernelModules=yes", "ProtectControlGroups=yes",
                       "RestrictSUIDSGID=yes", "LockPersonality=yes", "User=pi", "StateDirectory=hikvision-loxone",
                       "StateDirectoryMode=0750", "ConditionPathExists=/etc/hikvision-loxone/config.toml"):
            self.assertIn(wanted, lines)
        self.assertEqual([x for x in lines if x.startswith(("RestrictAddressFamilies", "DynamicUser"))], [])


def multipart(parts) -> io.BufferedReader:
    """Flusso eventi finto: una lista di (content-type, corpo)."""
    part = b"--boundary\r\nContent-Type: %s\r\nContent-Length: %d\r\n\r\n%s\r\n"
    raw = b"".join(part % (ctype.encode(), len(body), body) for ctype, body in parts)
    return io.BufferedReader(io.BytesIO(raw + b"--boundary\r\n"))


class FacePrivacyTest(unittest.TestCase):
    """Delle persone sconosciute non si salva niente, salvo con l'opzione di diagnostica save_unknown_faces."""
    JPEG = b"\xff\xd8volto\xff\xd9"
    DETECTED = (b'<EventNotificationAlert xmlns="http://www.hikvision.com/ver20/XMLSchema"><channelID>2</channelID>'
                b"<eventType>facedetection</eventType><eventState>active</eventState></EventNotificationAlert>")
    FAILURE = FACE_EVENT.replace(b"faceContrast<", b"faceContrastFailure<")
    NO_NAME = FACE_EVENT.replace(b"<PersonInfo><name>Mario</name></PersonInfo>", b"").replace(b"faceContrast<",
                                                                                               b"alarmResult<")

    def saved(self, save_unknown: bool) -> list[str]:
        xml, jpeg = "application/xml", "image/jpeg"
        parts = [(xml, self.DETECTED), (jpeg, self.JPEG), (jpeg, self.JPEG),           # un volto qualsiasi
                 (xml, self.FAILURE), (jpeg, self.JPEG),                               # confronto fallito
                 (xml, self.NO_NAME), (jpeg, self.JPEG),                               # risultato senza nome
                 (xml, FACE_EVENT), (jpeg, self.JPEG), (jpeg, self.JPEG), (jpeg, self.JPEG),   # Mario riconosciuto
                 (xml, LINE_EVENT), (jpeg, self.JPEG)]                                 # non e' un evento dei volti
        with tempfile.TemporaryDirectory() as d, mock.patch.object(hk, "FACE_DIR", d):
            worker = hk.CameraWorker(hk.Camera("interna", "10.0.0.2", "u", "p"), FakeLoxone(), save_unknown)
            worker.read_stream(multipart(parts), b"boundary")
            return sorted(name.split("_", 1)[1] for name in os.listdir(d))           # senza data e ora

    def test_only_recognised_people_are_saved_by_default(self):
        self.assertEqual(self.saved(False), ["interna_facecontrast.xml", "interna_facecontrast_1.jpg",
                                             "interna_facecontrast_2.jpg"])            # volto e scena, non di piu'

    def test_diagnostic_option_saves_everything(self):
        self.assertEqual(self.saved(True), sorted(
            ["interna_facedetection.xml", "interna_facedetection_1.jpg", "interna_facedetection_2.jpg",
             "interna_facecontrastfailure.xml", "interna_facecontrastfailure_1.jpg",
             "interna_alarmresult.xml", "interna_alarmresult_1.jpg",
             "interna_facecontrast.xml", "interna_facecontrast_1.jpg", "interna_facecontrast_2.jpg"]))

    def test_keep_face_event(self):
        known, unknown = hk.parse_event(FACE_EVENT), hk.parse_event(self.DETECTED)
        self.assertTrue(hk.keep_face_event(known))
        self.assertFalse(hk.keep_face_event(unknown))
        self.assertFalse(hk.keep_face_event(hk.parse_event(self.FAILURE)))
        self.assertTrue(hk.keep_face_event(unknown, save_unknown=True))
        self.assertFalse(hk.keep_face_event(hk.parse_event(LINE_EVENT), save_unknown=True))
        self.assertFalse(hk.CameraWorker(hk.Camera("n", "h", "u", "p"), FakeLoxone()).save_unknown_faces)


WIZARD_OLD = """save_unknown_faces = true

[miniserver]
host = "10.0.0.1"
user = "ponte"
password = "vecchia-lox"

[[cameras]]
name = "Scale"
host = "10.0.0.5"
user = "admin"
password = "vecchia-scale"

[[cameras.events]]
type = "linedetection"
inputs = ["movimento_scale"]

[[cameras]]
name = "Garage"
host = "10.0.0.6"
user = "operatore"
password = "vecchia-garage"

[[cameras.events]]
type = "faceContrast"
channel = 2
person = "Mario"
min_similarity = 85
inputs = ["mario_entrata"]
dry_run = false

[[cameras]]
name = "Cortile"
host = "10.0.0.7"
user = "admin"
password = "vecchia-cortile"

[[cameras.events]]
type = "vmd"
inputs = ["movimento_cortile"]
"""


def fake_camera(host, user, password):
    """Telecamere finte: .7 spenta, password che iniziano per "nuova" giuste, le altre sbagliate."""
    if host == "10.0.0.7":
        return "telecamera non raggiungibile (ConnectTimeout)", ""
    if password.startswith("nuova"):
        return None, "DS-TEST / V1"
    return "utente o password non accettati (dopo vari tentativi la telecamera si blocca per un po')", ""


class WizardTest(unittest.TestCase):
    """Procedura guidata "configura" senza rete: le risposte sono i valori proposti, le password una lista."""

    def wizard(self, path, hosts, passwords, gid=1000):
        typed = iter(passwords)
        out = io.StringIO()
        with mock.patch.object(hk.os, "geteuid", lambda: 0, create=True), \
                mock.patch.object(hk.os, "chown", lambda *a, **k: None, create=True), \
                mock.patch.object(hk, "service_gid", lambda p: gid), \
                mock.patch.object(hk, "_ask", lambda prompt, default="": default or "ponte"), \
                mock.patch("getpass.getpass", lambda prompt="": next(typed)), \
                mock.patch.object(hk, "check_loxone", lambda host, user, password: None), \
                mock.patch.object(hk, "camera_info", fake_camera), \
                mock.patch.object(hk.subprocess, "run") as run, contextlib.redirect_stdout(out):
            hk.configure(str(path), "10.0.0.1", hosts)
        self.assertEqual(list(typed), [])                      # tutte le password previste sono state chieste
        return out.getvalue(), run

    def test_skipped_unreachable_and_unverified_cameras_are_kept(self):
        """Prima una telecamera saltata, spenta o con la password sbagliata spariva con le sue regole."""
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "config.toml"
            path.write_text(WIZARD_OLD, encoding="utf-8")
            mode_before = os.stat(path).st_mode & 0o777
            # Miniserver; .8 nuova con password sbagliata 3 volte; Scale ok; Garage saltata; Cortile spenta
            out, run = self.wizard(path, ["10.0.0.8"], ["nuova-lox", "no", "no", "no", "nuova-scale", "", "qualcosa"])
            text = path.read_text(encoding="utf-8")
            ms, cams = hk.load_config(text)
            self.assertEqual(ms["password"], "nuova-lox")
            self.assertEqual([(c.name, c.host, c.user, c.password, len(c.rules)) for c in cams],
                             [("Scale", "10.0.0.5", "admin", "nuova-scale", 1),
                              ("Garage", "10.0.0.6", "operatore", "vecchia-garage", 1),
                              ("Cortile", "10.0.0.7", "admin", "vecchia-cortile", 1)])
            self.assertEqual((cams[1].rules[0].person, cams[1].rules[0].dry_run), ("Mario", False))   # resta attiva
            self.assertTrue(hk.load_options(text)["save_unknown_faces"])       # le opzioni generali restano
            self.assertEqual(Path(str(path) + ".bak").read_text(encoding="utf-8"), WIZARD_OLD)   # copia di prima
            self.assertIn("Garage resta com'era", out)
            self.assertIn("Cortile resta com'era", out)
            self.assertNotIn("Scale resta com'era", out)
            self.assertIn(f"{path}.bak", out)
            run.assert_called_once_with(["systemctl", "restart", "hikvision-loxone.service"], check=False)
            if os.name == "posix":                             # la copia ha i permessi del file di prima
                self.assertEqual(os.stat(str(path) + ".bak").st_mode & 0o777, mode_before)
                self.assertEqual(os.stat(path).st_mode & 0o777, 0o640)
            # seconda volta: tre password sbagliate su Scale, le altre saltate -> resta tutto, copia aggiornata
            before = path.read_text(encoding="utf-8")
            out, _ = self.wizard(path, [], ["nuova-lox", "no", "no", "no", "", "x"])
            self.assertIn("Scale resta com'era", out)
            self.assertEqual([(c.name, c.password) for c in hk.load_config(path.read_text(encoding="utf-8"))[1]],
                             [("Scale", "nuova-scale"), ("Garage", "vecchia-garage"), ("Cortile", "vecchia-cortile")])
            self.assertEqual(Path(str(path) + ".bak").read_text(encoding="utf-8"), before)

    def test_new_cameras_only_when_verified(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "config.toml"
            with self.assertRaisesRegex(SystemExit, "niente e' stato salvato"):
                self.wizard(path, ["10.0.0.5"], ["nuova-lox", ""])               # l'unica telecamera saltata
            self.assertEqual(os.listdir(d), [])                                 # ne' file ne' copia
            out, _ = self.wizard(path, ["10.0.0.5", "10.0.0.7"], ["nuova-lox", "nuova-uno", "x"])
            cams = hk.load_config(path.read_text(encoding="utf-8"))[1]
            self.assertEqual([(c.name, c.host, c.password, c.rules) for c in cams],
                             [("Telecamera_5", "10.0.0.5", "nuova-uno", [])])   # la .7 spenta non c'era: non entra
            self.assertEqual(os.listdir(d), ["config.toml"])                    # niente da copiare la prima volta
            self.assertNotIn("ATTENZIONE", out)

    def test_missing_service_group_does_not_lose_the_passwords(self):
        """Senza il gruppo del servizio la procedura non cade dopo le password: salva per il solo root e avvisa."""
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "config.toml"
            out, _ = self.wizard(path, ["10.0.0.5"], ["nuova-lox", "nuova-uno"], gid=None)
            self.assertEqual(hk.load_config(path.read_text(encoding="utf-8"))[1][0].password, "nuova-uno")
            self.assertIn("ATTENZIONE", out)
            self.assertIn("solo da root", out)
            self.assertIn(f"sudo chgrp <gruppo> {path}", out)
            if os.name == "posix":
                self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)

    def test_invalid_old_config_is_explained_and_backed_up(self):
        broken = WIZARD_OLD.replace("min_similarity = 85", "min_similarty = 85")     # errore di battitura
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "config.toml"
            path.write_text(broken, encoding="utf-8")
            out, _ = self.wizard(path, ["10.0.0.5"], ["nuova-lox", "nuova-uno"])
            self.assertIn("non valida", out)
            self.assertIn("'min_similarty'", out)                               # dice perche'
            self.assertIn("si riparte da zero", out)
            self.assertEqual(Path(str(path) + ".bak").read_text(encoding="utf-8"), broken)   # niente va perso
            self.assertEqual(len(hk.load_config(path.read_text(encoding="utf-8"))[1]), 1)

    def test_service_group(self):
        """Gruppo della configurazione: variabile, gruppo che il file ha gia', gruppo della cartella, poi "pi"."""
        groups = {"pi": 1000, "casa": 1234}

        def getgrnam(name):
            if name not in groups:
                raise KeyError(name)
            return SimpleNamespace(gr_gid=groups[name])

        def stat_with(gids):
            def fake_stat(path, *args, **kwargs):
                if path not in gids:
                    raise FileNotFoundError(path)
                return SimpleNamespace(st_gid=gids[path])
            return fake_stat

        cfg = "/etc/hikvision-loxone/config.toml"
        folder = os.path.dirname(cfg)
        with mock.patch.dict(sys.modules, {"grp": SimpleNamespace(getgrnam=getgrnam)}), mock.patch.dict(os.environ):
            os.environ.pop(hk.SERVICE_GROUP_ENV, None)
            with mock.patch.object(hk.os, "stat", stat_with({cfg: 1500, folder: 1600})):
                self.assertEqual(hk.service_gid(cfg), 1500)                     # quello che il file ha gia'
            with mock.patch.object(hk.os, "stat", stat_with({cfg: 0, folder: 1600})):
                self.assertEqual(hk.service_gid(cfg), 1600)                     # file di root: vale la cartella
            with mock.patch.object(hk.os, "stat", stat_with({folder: 0})):
                self.assertEqual(hk.service_gid(cfg), 1000)                     # altrimenti "pi"
                os.environ[hk.SERVICE_GROUP_ENV] = "casa"
                self.assertEqual(hk.service_gid(cfg), 1234)                     # la variabile vince
                os.environ[hk.SERVICE_GROUP_ENV] = "inesistente"
                self.assertIsNone(hk.service_gid(cfg))
                del os.environ[hk.SERVICE_GROUP_ENV], groups["pi"]
                self.assertIsNone(hk.service_gid(cfg))                          # un sistema senza l'utente pi


if __name__ == "__main__":
    unittest.main()
