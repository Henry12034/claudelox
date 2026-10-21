"""Test del ponte Hikvision -> Loxone senza telecamere: configurazione, eventi XML, flusso multipart, regole."""
from __future__ import annotations

import io
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "integrazioni" / "hikvision-isapi"))

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
        ms["password"] = 'a"b\\c\tè#'                   # virgolette, barra, tab, accento, cancelletto
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


if __name__ == "__main__":
    unittest.main()
