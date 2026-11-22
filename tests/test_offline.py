"""Test senza Miniserver: formato .LoxCC, pacchetto di caricamento, modifiche e confronto del programma.

Esecuzione:  python -m unittest discover -s tests -v
Usano un piccolo programma sintetico (nessun dato di un impianto reale).
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import re
import struct
import sys
import tempfile
import unittest
import zipfile
import zlib
from datetime import datetime
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from claudelox import appstructure, backups, cli, config, editor, uploader  # noqa: E402
from claudelox.cli import activation_outcome  # noqa: E402
from claudelox.client import HttpError, LoxoneError, Miniserver, listing_time, parse_file_listing  # noqa: E402
from claudelox.config import Settings  # noqa: E402
from claudelox.loxcc import MAGIC, decompress, read_program  # noqa: E402
from claudelox.program import (Program, access_changes, app_impact, custom_change, diff, redact,  # noqa: E402
                               redact_data, set_parameter)
from claudelox.structure import Structure  # noqa: E402

U = "1c000000-0000-{:04x}-ffff000000000000".format  # UUID sintetici nel formato Loxone 8-4-4-16
DOOR, DOOR_Q, PAGE, REF, REF_AI, REF_AQ = U(1), U(2), U(3), U(4), U(5), U(6)
PULSE, PULSE_IN, PULSE_TIME, PULSE_Q, LIGHT, LIGHT_ON, LIGHT_RESET, OTHER_Q = (U(i) for i in range(7, 15))
ROOM, LIGHT_IO = U(15), U(16)

SAMPLE = f"""﻿<?xml version="1.0" encoding="utf-8"?>
<ControlList Version="1" NextObj="100">
\t<C Type="Document" U="{U(0)}" Title="Prova" NumO="7">
\t\t<C Type="LoxLIVE" U="{U(20)}" Title="Miniserver">
\t\t\t<C Type="InputCaption" U="{U(21)}" Title="Ingressi digitali">
\t\t\t\t<C Type="DigitalIn" U="{DOOR}" Title="Porta">
\t\t\t\t\t<Co K="Q" U="{DOOR_Q}"/>
\t\t\t\t</C>
\t\t\t</C>
\t\t</C>
\t\t<C Type="PlaceCaption" U="{U(22)}" Title="Stanze">
\t\t\t<C Type="Place" U="{ROOM}" Title="Cucina"/>
\t\t</C>
\t\t<C Type="Page" U="{PAGE}" Title="Cucina">
\t\t\t<C Type="InputRef" U="{REF}" Title="Porta" Ref="{DOOR}" Px="0" Py="0" Px2="200" Py2="20">
\t\t\t\t<Co K="AI" Nc="1" U="{REF_AI}">
\t\t\t\t\t<In Input="{DOOR_Q}"/>
\t\t\t\t</Co>
\t\t\t\t<Co K="AQ" U="{REF_AQ}"/>
\t\t\t</C>
\t\t\t<C Type="OnPulseDelay" U="{PULSE}" Title="Impulso" Px="300" Py="0" Px2="400" Py2="70">
\t\t\t\t<Co K="InputTrigger" Inv="true" Nc="1" U="{PULSE_IN}">
\t\t\t\t\t<In Input="{REF_AQ}"/>
\t\t\t\t</Co>
\t\t\t\t<Co K="Time" Def="0.1" U="{PULSE_TIME}"/>
\t\t\t\t<Co K="Q" U="{PULSE_Q}"/>
\t\t\t</C>
\t\t\t<C Type="LightController2" U="{LIGHT}" Title="Luci" Px="500" Py="0" Px2="700" Py2="300">
\t\t\t\t<Co K="On" Nc="1" U="{LIGHT_ON}">
\t\t\t\t\t<In Input="{OTHER_Q}"/>
\t\t\t\t</Co>
\t\t\t\t<Co K="Reset" U="{LIGHT_RESET}"/>
\t\t\t\t<IoData Pr="{ROOM}" Visu="true"/>
\t\t\t</C>
\t\t\t<C Type="Memory" U="{U(17)}" Title="Altro" Px="0" Py="400" Px2="100" Py2="450">
\t\t\t\t<Co K="Q" U="{OTHER_Q}"/>
\t\t\t</C>
\t\t</C>
\t</C>
</ControlList>
""".replace("\n", "\r\n")


def add_pulse(text: str) -> tuple[str, str]:
    """Aggiunge un secondo impulso collegato a Luci.Reset (come fa uno scenario)."""
    prog = Program(text.encode("utf-8"))
    ids = editor.UuidFactory(text)
    block, block_u, co = editor.clone_block(prog.block("Impulso"), ids, title="Nuovo impulso", position=(300, 200),
                                            overrides={"InputTrigger": {"inputs": [REF_AQ], "Inv": False}})
    text = editor.insert_children(text, PAGE, [block])
    text = editor.add_input(text, LIGHT_RESET, co["Q"])
    return editor.bump_counters(text, 1), block_u


class LoxCCTest(unittest.TestCase):
    def test_round_trip_and_crc(self):
        xml = SAMPLE.encode("utf-8")
        packed = uploader.compress_loxcc(xml)
        magic, comp, size, crc = struct.unpack_from("<4I", packed)
        self.assertEqual((magic, size, crc), (MAGIC, len(xml), zlib.crc32(xml)))
        self.assertEqual(comp, len(packed) - 16)
        self.assertEqual(decompress(packed), xml)

    def test_short_payload(self):
        self.assertEqual(decompress(uploader.compress_loxcc(b"<a/>")), b"<a/>")

    def test_program_zip_keeps_other_members(self):
        src = io.BytesIO()
        with zipfile.ZipFile(src, "w") as z:
            z.writestr("sps0.LoxCC", uploader.compress_loxcc(b"<old/>"))
            z.writestr("LoxAPP3.json", b'{"controls": {}}')
        built = uploader.build_program_zip(src.getvalue(), SAMPLE.encode("utf-8"))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sps_0001_20260101000000.zip"
            path.write_bytes(built)
            xml, app = read_program(path)
        self.assertEqual(xml, SAMPLE.encode("utf-8"))
        self.assertEqual(app, b'{"controls": {}}')


class EditorTest(unittest.TestCase):
    def test_add_then_remove_restores_program(self):
        text, new_u = add_pulse(SAMPLE)
        d = diff(Program(SAMPLE.encode("utf-8")), Program(text.encode("utf-8")))
        self.assertEqual(len(d["aggiunti"]), 1)
        self.assertTrue(any("nuovo collegamento: Reset <- Nuovo impulso.Q" in n
                            for ch in d["modificati"] for n in ch["modifiche"]))
        back, removed = editor.remove_blocks(text, [new_u])
        self.assertEqual(removed, 1)
        # identico all'originale, tranne il contatore NextObj (gli id non si riusano)
        self.assertEqual(back.replace('NextObj="101"', 'NextObj="100"'), SAMPLE)

    def test_remove_drops_links_and_updates_counts(self):
        back, _ = editor.remove_blocks(SAMPLE, [U(17)])  # "Altro" alimenta Luci.On
        self.assertIn(f'<Co K="On" U="{LIGHT_ON}"/>', back)
        self.assertNotIn(OTHER_Q, back)
        self.assertIn('NumO="6"', back)
        Program(back.encode("utf-8"))  # resta XML valido

    def test_remove_refuses_referenced_block(self):
        with self.assertRaises(editor.EditError):  # l'InputRef usa ancora l'ingresso tramite Ref=
            editor.remove_blocks(SAMPLE, [DOOR])

    def test_remove_refuses_containers(self):
        with self.assertRaises(editor.EditError):
            editor.remove_blocks(SAMPLE, [PAGE])

    def test_remove_input_and_set_inverted(self):
        text = editor.remove_input(SAMPLE, PULSE_IN, REF_AQ)
        self.assertIn(f'<Co K="InputTrigger" Inv="true" U="{PULSE_IN}"/>', text)
        text = editor.set_inverted(text, PULSE_IN, False)
        self.assertIn(f'<Co K="InputTrigger" U="{PULSE_IN}"/>', text)
        text = editor.add_input(editor.set_inverted(text, PULSE_IN, True), PULSE_IN, REF_AQ)
        self.assertEqual(text.replace(" />", "/>"), SAMPLE)  # torna identico (a parte lo stile di chiusura)
        with self.assertRaises(editor.EditError):
            editor.remove_input(SAMPLE, PULSE_IN, OTHER_Q)  # collegamento che non esiste

    def test_clone_with_elements_and_children(self):
        prog = Program(SAMPLE.encode("utf-8"))
        ids = editor.UuidFactory(SAMPLE)
        inner, inner_u, _ = editor.clone_block(prog.block("Altro"), ids, title="Dentro")
        block, block_u, _ = editor.clone_block(prog.block("Luci"), ids, title="Copia", attrs={"Px": None, "Nuovo": "1"},
                                               elements={"IoData": {"Visu": "false", "Pr": None}}, children=[inner])
        first = block.split("\n")[0]
        self.assertIn('Nuovo="1"', first)
        self.assertNotIn(" Px=", first)
        self.assertIn('\t<IoData Visu="false" />', block)
        text = editor.bump_counters(editor.insert_children(SAMPLE, PAGE, [block]), 2)
        new = Program(text.encode("utf-8"))
        self.assertEqual(new.parent[new.by_uuid[inner_u]].get("U"), block_u)
        self.assertNotIn("IoData", editor.clone_block(prog.block("Luci"), ids)[0])  # senza elements: come prima

    def test_clone_group_rewires_inside_and_keeps_outside(self):
        ids = editor.UuidFactory(SAMPLE)
        blocks, mapping = editor.clone_group(SAMPLE, [REF, PULSE, LIGHT], ids, offset=(1000, 2000))
        text = editor.bump_counters(editor.insert_children(SAMPLE, PAGE, blocks), 3)
        self.assertNotIn("\r\r", text)
        new = Program(text.encode("utf-8"))
        ref, pulse, light = (new.by_uuid[mapping[u]] for u in (REF, PULSE, LIGHT))
        self.assertEqual(ref.get("Ref"), DOOR)                                     # riferimento esterno: uguale
        self.assertEqual(new.wiring(ref)["ingressi"], ["AI <- Porta.Q <DigitalIn>"])
        trig = next(co for co in pulse.findall("Co") if co.get("K") == "InputTrigger")
        self.assertEqual((trig.find("In").get("Input"), trig.get("Inv")), (mapping[REF_AQ], "true"))  # interno: copia
        self.assertEqual(new.wiring(light)["ingressi"], ["On <- Altro.Q <Memory>"])  # da fuori: come l'originale
        self.assertEqual(light.find("IoData").get("Pr"), ROOM)                    # contenuto copiato
        self.assertEqual((pulse.get("Px"), pulse.get("Py2")), ("1300", "2070"))
        self.assertEqual(new.parent[pulse].get("U"), PAGE)
        self.assertEqual(new.wiring(new.by_uuid[PULSE])["ingressi"], ["InputTrigger <- Porta.AQ <InputRef>"])
        self.assertEqual(len(set(mapping.values())), len(mapping))
        self.assertTrue(all(text.count(f'U="{u}"') == 1 for u in mapping.values()))
        with self.assertRaises(editor.EditError):
            editor.clone_group(SAMPLE, [U(20)], ids)                              # contenitore: no

    def test_set_child_attr_changes_only_the_own_element(self):
        text = SAMPLE.replace(f'<IoData Pr="{ROOM}" Visu="true"/>',
                              f'<IoData Pr="{ROOM}" Visu="true"/>\r\n\t\t\t\t<LoggerMailer On="Vecchio" NL="1"/>')
        text = editor.set_child_attr(text, LIGHT, "LoggerMailer", {"On": "Nuovo testo"})
        self.assertIn('<LoggerMailer On="Nuovo testo" NL="1"/>', text)
        with self.assertRaises(editor.EditError):
            editor.set_child_attr(text, PULSE, "LoggerMailer", {"On": "x"})

    def test_set_parameter_changes_only_value(self):
        new = set_parameter(SAMPLE.encode("utf-8"), PULSE_TIME, "0.5").decode("utf-8")
        self.assertEqual(new, SAMPLE.replace(f'Def="0.1" U="{PULSE_TIME}"', f'Def="0.5" U="{PULSE_TIME}"'))


class ImpactTest(unittest.TestCase):
    def test_logic_change_does_not_touch_app(self):
        a = Program(SAMPLE.encode("utf-8"))
        text, _ = add_pulse(SAMPLE)
        self.assertEqual(app_impact(a, Program(text.encode("utf-8")), b'{}'), [])

    def test_room_change_touches_app(self):
        a = Program(SAMPLE.encode("utf-8"))
        b = Program(SAMPLE.replace(f'Pr="{ROOM}"', f'Pr="{U(99)}"').encode("utf-8"))
        self.assertTrue(any("stanza" in x for x in app_impact(a, b, b"{}")))
        self.assertTrue(any("stanza" in n for ch in diff(a, b)["modificati"] for n in ch["modifiche"]))

    def test_inverted_input_is_reported(self):
        a = Program(SAMPLE.encode("utf-8"))
        b = Program(SAMPLE.replace('<Co K="InputTrigger" Inv="true"', '<Co K="InputTrigger"').encode("utf-8"))
        notes = [n for ch in diff(a, b)["modificati"] for n in ch["modifiche"]]
        self.assertIn("ingresso InputTrigger non piu' invertito", notes)
        self.assertEqual(app_impact(a, b, b"{}"), [])
        self.assertIn("InputTrigger (invertito) <- Porta", " ".join(a.wiring(a.block("Impulso"), detailed=True)["ingressi"]))

    def test_removing_visible_control_touches_app(self):
        a = Program(SAMPLE.encode("utf-8"))
        b = Program(editor.remove_blocks(SAMPLE, [LIGHT])[0].encode("utf-8"))
        self.assertTrue(any("eliminato" in x for x in app_impact(a, b, b"{}")))


# antifurto con un sensore scelto nella finestra di configurazione (collegamento nascosto su HI1 e voce in Objects)
# e una luce d'allarme (solo in Objects)
ALARM, A_I1, A_HI1, SENSOR, SENSOR_Q, SREF, SREF_AI, SREF_AQ, LAMP = (U(i) for i in range(60, 69))
ALARM_SAMPLE = f"""<?xml version="1.0" encoding="utf-8"?>
<ControlList Version="1" NextObj="100">
\t<C Type="Document" U="{U(0)}" Title="Prova" NumO="5">
\t\t<C Type="LoxLIVE" U="{U(20)}" Title="Miniserver">
\t\t\t<C Type="TreeSensor" U="{SENSOR}" Title="movimento Sala">
\t\t\t\t<Co K="Q" U="{SENSOR_Q}"/>
\t\t\t</C>
\t\t</C>
\t\t<C Type="Page" U="{PAGE}" Title="Generale">
\t\t\t<C Type="InputRef" U="{SREF}" Title="movimento Sala Rilevatore" Ref="{SENSOR}">
\t\t\t\t<Co K="AI" Nc="1" U="{SREF_AI}">
\t\t\t\t\t<In Input="{SENSOR_Q}"/>
\t\t\t\t</Co>
\t\t\t\t<Co K="AQ" U="{SREF_AQ}"/>
\t\t\t</C>
\t\t\t<C Type="Memory" U="{U(17)}" Title="Presenza in due stanze">
\t\t\t\t<Co K="Q" U="{OTHER_Q}"/>
\t\t\t</C>
\t\t\t<C Type="LightController2" U="{LAMP}" Title="Luci"/>
\t\t\t<C Type="Alarm" U="{ALARM}" Title="Antifurto" Objects="{SENSOR},{LAMP}">
\t\t\t\t<Co K="I1" Nc="1" U="{A_I1}">
\t\t\t\t\t<In Input="{OTHER_Q}"/>
\t\t\t\t</Co>
\t\t\t\t<Co K="HI1" Nc="1" U="{A_HI1}">
\t\t\t\t\t<In Input="{SREF_AQ}" FLG="1"/>
\t\t\t\t</Co>
\t\t\t\t<IoData Visu="true"/>
\t\t\t</C>
\t\t</C>
\t</C>
</ControlList>
"""
ALARM_APP = json.dumps({"rooms": {}, "cats": {}, "controls": {f"U:{ALARM}": {
    "name": "Antifurto", "type": "Alarm", "uuidAction": ALARM, "details": {"presenceConnected": True},
    "states": {}}}}).encode("utf-8")


class AlarmObjectsTest(unittest.TestCase):
    """Togliere dall'antifurto i sensori della finestra di configurazione e' una modifica della sola logica."""

    def setUp(self):
        self.a = Program(ALARM_SAMPLE.encode("utf-8"))
        text = editor.remove_input(ALARM_SAMPLE, A_HI1, SREF_AQ)
        self.senza_sensore = text.replace(f'Objects="{SENSOR},{LAMP}"', f'Objects="{LAMP}"')

    def update(self, text):
        return appstructure.update_app(self.a, Program(text.encode("utf-8")), ALARM_APP)

    def test_sensore_tolto(self):
        b = Program(self.senza_sensore.encode("utf-8"))
        self.assertEqual(app_impact(self.a, b, ALARM_APP), [])
        up = self.update(self.senza_sensore)
        self.assertEqual((up.unsupported, up.done), ([], []))

    def test_luce_d_allarme_o_nessuna_presenza(self):
        senza_luce = ALARM_SAMPLE.replace(f'Objects="{SENSOR},{LAMP}"', f'Objects="{SENSOR}"')
        self.assertEqual(self.update(senza_luce).unsupported, ["'Antifurto' <Alarm>: cambiano Objects"])
        nessuna_presenza = editor.remove_input(self.senza_sensore, A_I1, OTHER_Q)
        self.assertEqual(self.update(nessuna_presenza).unsupported, ["'Antifurto' <Alarm>: cambiano Objects"])


# programma con un ingresso virtuale HTTP e due valori visibili nell'app (cucina e bagno), con la struttura e i
# permessi come li scrive Config: ogni valore lo vedono un utente e i gruppi della sua stanza e della categoria
HTTP_IN, CMD, CMD_AQ, CMD_Q, CAT, ROOM2, ICON = U(30), U(31), U(32), U(33), U(34), U(35), U(36)
CMD2, CMD2_AQ, CMD2_Q, ROOM3, G_ROOM, G_ROOM2, G_CAT, USER, CAL = (U(i) for i in range(37, 46))
HTTP_SAMPLE = f"""<?xml version="1.0" encoding="utf-8"?>
<ControlList Version="1" NextObj="100">
\t<C Type="Document" U="{U(0)}" Title="Prova" NumO="4">
\t\t<C Type="LoxLIVE" U="{U(20)}" Title="Miniserver">
\t\t\t<C Type="VirtualInCaption" U="{U(23)}" Title="Ingressi virtuali">
\t\t\t\t<C Type="VirtualHttpIn" U="{HTTP_IN}" Title="Meteo" Address="http://10.0.0.5/meteo">
\t\t\t\t\t<C Type="VirtualHttpInCmd" U="{CMD}" Title="Umidita" Check="\\iumidita=\\i\\v" Analog="true" StatsType="11">
\t\t\t\t\t\t<Co K="AQ" U="{CMD_AQ}"/>
\t\t\t\t\t\t<Co K="Q" U="{CMD_Q}"/>
\t\t\t\t\t\t<IoData Visu="true" Cr="{CAT}" Pr="{ROOM}" IconV="{ICON}"/>
\t\t\t\t\t\t<Display Type="2" Unit="&lt;v.1&gt; %" StateOnly="true"/>
\t\t\t\t\t</C>
\t\t\t\t\t<C Type="VirtualHttpInCmd" U="{CMD2}" Title="Pressione" Check="\\ipressione=\\i\\v" Analog="true" StatsType="11">
\t\t\t\t\t\t<Co K="AQ" U="{CMD2_AQ}"/>
\t\t\t\t\t\t<Co K="Q" U="{CMD2_Q}"/>
\t\t\t\t\t\t<IoData Visu="true" Cr="{CAT}" Pr="{ROOM2}"/>
\t\t\t\t\t\t<Display Type="2" Unit="&lt;v&gt; hPa" StateOnly="true"/>
\t\t\t\t\t</C>
\t\t\t\t</C>
\t\t\t</C>
\t\t</C>
\t\t<C Type="LoxCaption" U="{U(24)}" Title="Berechtigungsgruppen">
\t\t\t<C Type="RightGroup" U="{G_ROOM}" Title="Cucina" GT="1"/>
\t\t\t<C Type="RightGroup" U="{G_ROOM2}" Title="Bagno" GT="1"/>
\t\t\t<C Type="RightGroup" U="{G_CAT}" Title="Sensori" GT="2"/>
\t\t</C>
\t</C>
</ControlList>
"""


def app_control(u, name, room, unit_fmt, stat_fmt, aq, q, icon="null"):
    return (f'"U:{u}":{{"name":"{name}","type":"InfoOnlyAnalog","uuidAction":"{u}","room":"{room}",'
            f'"cat":"{CAT}","defaultRating":0,"isFavorite":false,"isSecured":false,"defaultIcon":{icon},'
            f'"restrictions":0,"details":{{"jLockable":false,"format":"{unit_fmt}"}},'
            f'"states":{{"value":"{u}","error":"{q}"}},"statistic":{{"frequency":11,"outputs":[{{"id":0,'
            f'"name":"{name}","format":"{stat_fmt}","uuid":"{aq}","visuType":0}}]}}}}')


HTTP_APP = ('{"lastModified":"2026-01-01 10:00:00","msInfo":{"latitude":12.345000,"altitude":100},"rooms":{'
            f'"{ROOM}":{{"uuid":"{ROOM}","name":"Cucina"}},"{ROOM2}":{{"uuid":"{ROOM2}","name":"Bagno"}},'
            f'"{ROOM3}":{{"uuid":"{ROOM3}","name":"Cantina"}}}},"cats":{{"{CAT}":{{"uuid":"{CAT}","name":"Sensori"}}}},'
            '"controls":{'
            + app_control(CMD, "Umidita", ROOM, "%.1f %%", "%.1f %", CMD_AQ, CMD_Q, '"IconsFilled/humidity.svg"') + ","
            + app_control(CMD2, "Pressione", ROOM2, "%.0f hPa", "%.0f hPa", CMD2_AQ, CMD2_Q) + "}}").encode("utf-8")
SEP = {k: bytes([v]) * 16 for k, v in (("parti", 0xFE), ("accesso", 0xFC), ("altro", 0xFD), ("fine", 0xFF))}


def perm_record(head, parts, access, extra=None):
    ub = appstructure.uuid_bytes
    out = ub(head) + SEP["parti"] + b"".join(map(ub, parts)) + SEP["accesso"] + b"".join(map(ub, access))
    return out + (SEP["altro"] + b"".join(map(ub, extra)) if extra is not None else b"") + SEP["fine"]


HTTP_PERMS = (perm_record(CAL, [], [], extra=[]) + perm_record(CMD, [CMD, CMD_Q, CMD_AQ], [USER, G_ROOM, G_CAT])
              + perm_record(CMD2, [CMD2, CMD2_Q, CMD2_AQ], [USER, G_ROOM2, G_CAT]))
NOW = datetime(2026, 1, 2, 12, 0, 0)


def add_cmd(text: str, title: str, unit: str, room: str, visible: bool = True) -> tuple[str, str, dict]:
    prog = Program(text.encode("utf-8"))
    ids = editor.UuidFactory(text)
    block, block_u, co = editor.clone_block(
        prog.block("Umidita"), ids, title=title, attrs={"Check": r"\iluce=\i\v"},
        elements={"IoData": {"Pr": room, "IconV": None, "Visu": "true" if visible else "false"},
                  "Display": {"Unit": unit}})
    return editor.insert_children(text, HTTP_IN, [block]), block_u, co


class AppStructureTest(unittest.TestCase):
    """Struttura dell'app e permessi aggiornati dal toolkit per il caricamento diretto."""

    def update(self, new_text: str, perms: bytes | None = HTTP_PERMS):
        return appstructure.update_app(Program(HTTP_SAMPLE.encode("utf-8")), Program(new_text.encode("utf-8")),
                                       HTTP_APP, perms, now=NOW)

    @staticmethod
    def records(perms: bytes) -> dict:
        return {r.head: r.sections for r in appstructure.parse_permissions(perms)}

    def test_writes_like_config(self):
        self.assertEqual(appstructure.dump(json.loads(HTTP_APP)).encode("utf-8"), HTTP_APP)
        self.assertEqual(appstructure.build_permissions(appstructure.parse_permissions(HTTP_PERMS)), HTTP_PERMS)
        self.assertEqual(appstructure.uuid_bytes("1a2b3c4d-5e6f-7a8b-ffff0123456789ab").hex(),
                         "4d3c2b1a6f5e8b7affff0123456789ab")
        self.assertEqual(appstructure.unit_formats("<v.1> %"), ("%.1f %%", "%.1f %"))
        self.assertEqual(appstructure.unit_formats("<v>lx"), ("%.0flx", "%.0flx"))
        self.assertIsNone(appstructure.unit_formats("<v.u>"))

    def test_new_value_is_copied_from_a_control_of_the_same_type(self):
        text, new_u, co = add_cmd(HTTP_SAMPLE, "Luce", "<v>lx", ROOM2)
        upd = self.update(text)
        self.assertEqual(upd.unsupported, [])
        app = json.loads(upd.data)
        c = app["controls"][f"U:{new_u}"]
        self.assertEqual((c["name"], c["room"], c["uuidAction"], c["defaultIcon"]), ("Luce", ROOM2, new_u, None))
        self.assertEqual(c["details"]["format"], "%.0flx")
        self.assertEqual(c["states"], {"value": new_u, "error": co["Q"]})
        self.assertEqual(c["statistic"]["outputs"][0],
                         {"id": 0, "name": "Luce", "format": "%.0flx", "uuid": co["AQ"], "visuType": 0})
        self.assertEqual(app["lastModified"], "2026-01-02 12:00:00")
        # permessi: i pezzi del blocco nuovo, lo stesso utente, i gruppi del bagno e dei sensori
        ub = appstructure.uuid_bytes
        self.assertEqual(self.records(upd.permissions)[ub(new_u)],
                         [(0xFE, [ub(new_u), ub(co["Q"]), ub(co["AQ"])]), (0xFC, [ub(USER), ub(G_ROOM2), ub(G_CAT)])])
        self.assertTrue(upd.permissions.startswith(HTTP_PERMS))  # i record esistenti non cambiano
        del app["controls"][f"U:{new_u}"]
        app["lastModified"] = "2026-01-01 10:00:00"
        self.assertEqual(appstructure.dump(app).encode("utf-8"), HTTP_APP)  # il resto non cambia

    def test_rename_move_and_remove(self):
        moved = HTTP_SAMPLE.replace('Title="Umidita"', 'Title="Umidita bagno"').replace(
            f'Pr="{ROOM}"', f'Pr="{ROOM2}"')
        upd = self.update(moved)
        c = json.loads(upd.data)["controls"][f"U:{CMD}"]
        self.assertEqual((c["name"], c["room"], c["statistic"]["outputs"][0]["name"]),
                         ("Umidita bagno", ROOM2, "Umidita bagno"))
        ub = appstructure.uuid_bytes
        self.assertEqual(self.records(upd.permissions)[ub(CMD)][1], (0xFC, [ub(USER), ub(G_ROOM2), ub(G_CAT)]))
        upd = self.update(editor.remove_blocks(HTTP_SAMPLE, [CMD])[0])
        self.assertEqual((upd.unsupported, list(json.loads(upd.data)["controls"])), ([], [f"U:{CMD2}"]))
        self.assertEqual(upd.permissions, perm_record(CAL, [], [], extra=[]) + HTTP_PERMS[-160:])

    def test_hidden_control_still_used_by_another_is_left_to_config(self):
        app = json.loads(HTTP_APP)
        app["controls"][f"U:{CMD2}"]["details"]["controls"] = [{"uuid": CMD}]      # come un comando centrale
        hidden = editor.set_iodata(HTTP_SAMPLE, CMD, {"Visu": None})
        upd = appstructure.update_app(Program(HTTP_SAMPLE.encode("utf-8")), Program(hidden.encode("utf-8")),
                                      appstructure.dump(app).encode("utf-8"), HTTP_PERMS, now=NOW)
        self.assertTrue(any("ancora usati" in x for x in upd.unsupported))

    def test_hidden_value_does_not_touch_the_app(self):
        text, _, _ = add_cmd(HTTP_SAMPLE, "Nascosto", "<v>", ROOM, visible=False)
        upd = self.update(text)
        self.assertEqual((upd.done, upd.unsupported, upd.data, upd.permissions), ([], [], HTTP_APP, None))

    def test_unknown_cases_are_left_to_config(self):
        text, _, _ = add_cmd(HTTP_SAMPLE, "Strano", "<v.u>", ROOM)
        self.assertTrue(any("unita'" in x for x in self.update(text).unsupported))
        text, _, _ = add_cmd(HTTP_SAMPLE, "Altrove", "<v>", U(99))  # stanza che l'app non ha
        self.assertTrue(any("stanza o categoria" in x for x in self.update(text).unsupported))
        text, _, _ = add_cmd(HTTP_SAMPLE, "Cantina", "<v>", ROOM3)  # nessun altro controllo in cantina
        self.assertTrue(any("permessi della stanza" in x for x in self.update(text).unsupported))
        text, _, _ = add_cmd(HTTP_SAMPLE, "Senza permessi", "<v>", ROOM)
        self.assertTrue(any("permissions.bin" in x for x in self.update(text, perms=None).unsupported))
        prog = Program(SAMPLE.encode("utf-8"))  # nell'app nessun controllo luci da copiare
        block, _, _ = editor.clone_block(prog.block("Luci"), editor.UuidFactory(SAMPLE), title="Luci 2", elements={})
        lights = Program(editor.insert_children(SAMPLE, PAGE, [block]).encode("utf-8"))
        self.assertEqual(appstructure.update_app(prog, lights, b'{"rooms":{},"cats":{},"controls":{}}').unsupported,
                         ["'Luci 2' <LightController2>: nell'app non c'e' un controllo dello stesso tipo da copiare"])

    def test_package_carries_structure_and_permissions(self):
        src = io.BytesIO()
        with zipfile.ZipFile(src, "w") as z:
            z.writestr("sps0.LoxCC", uploader.compress_loxcc(b"<old/>"))
            z.writestr("LoxAPP3.json", HTTP_APP)
            z.writestr("permissions.bin", HTTP_PERMS)
            z.writestr("Music.json", b"{}")
        built = uploader.build_program_zip(src.getvalue(), b"<new/>", {"LoxAPP3.json": b'{"nuova":1}',
                                                                          "permissions.bin": b"\x01" * 16})
        with zipfile.ZipFile(io.BytesIO(built)) as z:
            self.assertEqual((z.read("LoxAPP3.json"), z.read("permissions.bin"), z.read("Music.json")),
                             (b'{"nuova":1}', b"\x01" * 16, b"{}"))
        self.assertEqual(uploader.read_member(built, "Music.json"), b"{}")
        self.assertIsNone(uploader.read_member(built, "manca.bin"))


# controllo luci con due circuiti (dimmer e colore), un dimmer collegato (linkC) e un valore solo tra i preferiti
LC, LC_SCENE, DIM, DIM_AQ, FAVONLY, LROOM, LCAT, G_LROOM, G_LCAT, LUSER = (U(i) for i in range(70, 80))
LIGHT_SAMPLE = f"""<?xml version="1.0" encoding="utf-8"?>
<ControlList Version="1" NextObj="100">
\t<C Type="Document" U="{U(0)}" Title="Prova" NumO="4">
\t\t<C Type="Page" U="{PAGE}" Title="Camera">
\t\t\t<C Type="LightController2" Desc="Led camera" U="{LC}" Title="Luci camera" linkC="{DIM}" NameAI0="Led" NameAI1="Scena luce RGB" PresM0="65535">
\t\t\t\t<Co K="Scene" U="{LC_SCENE}"/>
\t\t\t\t<LightscenesC Outputs="2" Num="1">
\t\t\t\t\t<LightsceneC Name="Off" UUID="{U(81)}" ReadOnly="true" Outputs="2" SID="778" CID="11" Q1="268435456" Q2="1073741824"/>
\t\t\t\t</LightscenesC>
\t\t\t\t<IoData Visu="true" Cr="{LCAT}" Pr="{LROOM}" Rating="8"/>
\t\t\t</C>
\t\t\t<C Type="PushDimmer" U="{DIM}" Title="Luminosita led">
\t\t\t\t<Co K="AQ" U="{DIM_AQ}"/>
\t\t\t\t<IoData Visu="true" Cr="{LCAT}" Pr="{LROOM}" Rating="9"/>
\t\t\t</C>
\t\t\t<C Type="LightsceneRGB" U="{FAVONLY}" Title="Scena luce RGB">
\t\t\t\t<IoData Cr="{LCAT}" Pr="{LROOM}" UseInFav="true"/>
\t\t\t</C>
\t\t</C>
\t</C>
</ControlList>
"""
LIGHT_APP = json.dumps({"lastModified": "2026-01-01 10:00:00", "rooms": {LROOM: {"uuid": LROOM, "name": "Camera"}},
                        "cats": {LCAT: {"uuid": LCAT, "name": "Luci"}}, "controls": {
    f"U:{LC}": {"name": "Led camera", "type": "LightControllerV2", "uuidAction": LC, "room": LROOM, "cat": LCAT,
                "defaultRating": 8, "isFavorite": False, "isSecured": False, "restrictions": 0, "details": {},
                "states": {}, "links": [DIM], "subControls": {
                    f"{LC}/masterValue": {"name": "Luminosita Generale", "type": "Dimmer"},
                    f"{LC}/AI1": {"name": "Led", "type": "Dimmer"},
                    f"{LC}/AI2": {"name": "Scena luce RGB", "type": "ColorPickerV2"}}},
    f"U:{DIM}": {"name": "Luminosita led", "type": "Dimmer", "uuidAction": DIM, "room": LROOM, "cat": LCAT,
                 "defaultRating": 9, "isFavorite": False, "isSecured": False, "defaultIcon": None, "restrictions": 0,
                 "states": {}},
    f"U:{FAVONLY}": {"name": "Scena luce RGB", "type": "LightsceneRGB", "uuidAction": FAVONLY, "room": LROOM,
                     "cat": LCAT, "defaultRating": 0, "isFavorite": True, "isSecured": False, "restrictions": 17,
                     "details": {}}}}, separators=(",", ":")).encode("utf-8")
LIGHT_PERMS = b"".join(perm_record(u, [u], [LUSER, G_LROOM, G_LCAT]) for u in (LC, DIM, FAVONLY))


class LightsAppTest(unittest.TestCase):
    """Controlli luci e riordino dell'app: preferiti, importanza, circuiti, controlli nascosti."""

    def update(self, text: str):
        return appstructure.update_app(Program(LIGHT_SAMPLE.encode("utf-8")), Program(text.encode("utf-8")),
                                       LIGHT_APP, LIGHT_PERMS, now=NOW)

    def test_favorite_rating_and_circuit_names(self):
        text = editor.set_iodata(LIGHT_SAMPLE, LC, {"UseInFav": "true", "Rating": "9"})
        text = editor.set_block_attr(text, LC, {"NameAI0": "Luminosità", "NameAI1": "Colore"})
        self.assertIn('<IoData Visu="true" Cr=', text)
        upd = self.update(text)
        self.assertEqual(upd.unsupported, [])
        c = json.loads(upd.data)["controls"][f"U:{LC}"]
        self.assertEqual((c["name"], c["isFavorite"], c["defaultRating"]), ("Led camera", True, 9))  # nome = Desc
        self.assertEqual([s["name"] for s in c["subControls"].values()], ["Luminosita Generale", "Luminosità", "Colore"])
        self.assertIsNone(upd.permissions)                                  # stanza e categoria invariate
        renamed = editor.set_block_attr(LIGHT_SAMPLE, LC, {"Desc": "Strisce LED"})
        self.assertEqual(json.loads(self.update(renamed).data)["controls"][f"U:{LC}"]["name"], "Strisce LED")

    def test_hidden_controls_and_links(self):
        text = editor.set_iodata(LIGHT_SAMPLE, DIM, {"Visu": None})
        text = editor.set_iodata(text, FAVONLY, {"UseInFav": None})        # era solo tra i preferiti
        upd = self.update(text)
        self.assertEqual(upd.unsupported, [])
        app = json.loads(upd.data)
        self.assertEqual(list(app["controls"]), [f"U:{LC}"])
        self.assertNotIn("links", app["controls"][f"U:{LC}"])               # il dimmer collegato non c'e' piu'
        self.assertEqual([r.head for r in appstructure.parse_permissions(upd.permissions)],
                         [appstructure.uuid_bytes(LC)])
        self.assertTrue(any("collegamenti" in d for d in upd.done))

    def test_moods_are_not_in_the_app_and_icons_are_left_to_config(self):
        mood = f'<LightsceneC Name="Aurora" UUID="{U(82)}" Outputs="2" SID="1" CID="12" Q1="268435526" Q2="1073741824"/>'
        text = LIGHT_SAMPLE.replace('Num="1">', 'Num="2">').replace("</LightscenesC>", mood + "\n</LightscenesC>")
        a, b = Program(LIGHT_SAMPLE.encode("utf-8")), Program(text.encode("utf-8"))
        notes = [n for ch in diff(a, b)["modificati"] for n in ch["modifiche"]]
        self.assertEqual(notes, ["atmosfere aggiunte: Aurora (1)"])
        self.assertEqual(app_impact(a, b, LIGHT_APP), [])
        upd = self.update(text)
        self.assertEqual((upd.done, upd.unsupported, upd.data), ([], [], LIGHT_APP))
        icon = editor.set_iodata(LIGHT_SAMPLE, LC, {"IconV": U(99)})
        self.assertEqual(self.update(icon).unsupported, ["'Led camera' <LightController2>: cambia l'icona di un "
                                                         "controllo luci"])

    def test_set_iodata_only_touches_the_own_element(self):
        text = editor.set_iodata(HTTP_SAMPLE, CMD2, {"Pr": ROOM, "Rating": "5"})
        self.assertIn(f'<IoData Visu="true" Cr="{CAT}" Pr="{ROOM}" Rating="5"/>', text)
        self.assertEqual(text.count(f'Pr="{ROOM}"'), 2)                     # anche l'altro comando era in cucina
        with self.assertRaises(editor.EditError):
            editor.set_iodata(HTTP_SAMPLE, HTTP_IN, {"Visu": None})         # l'ingresso non ha un IoData suo


class LogTest(unittest.TestCase):
    def test_activation_outcome_reads_only_after_restart(self):
        log = "\n".join([
            "2026-01-01 10:00:00.000;Read config: combine custom changes - file size 1; Items: 1, Errors: 3",
            "2026-01-01 11:00:00.000;Restart SPS by user 'Claude'",
            "2026-01-01 11:00:00.100;Loading sps_new.zip - Remove old custom changes",
            "2026-01-01 11:00:08.000;PRG start program",
            "2026-01-01 11:00:08.100;Program started: /lx/prog/sps_0001_20260101100000.zip",
        ])
        self.assertEqual(activation_outcome(log), {"errori": None, "caricato": True, "avviato": True,
                                                   "emergenza": False, "riavvio_visto": True})
        # riga del riavvio scritta, quelle successive non ancora: non e' un fallimento ne' un successo
        self.assertFalse(activation_outcome("\n".join(log.splitlines()[:2]))["avviato"])
        # un riavvio vecchio non conta se si chiede solo quello dopo un certo momento
        later = activation_outcome(log, since=datetime(2026, 1, 1, 12, 0))
        self.assertEqual((later["riavvio_visto"], later["avviato"]), (False, False))


# --- firmware 17.3: elenco dei file via HTTP, programma salvato come .LoxCC, modifiche fatte dall'app ---

LISTING = ("d      0 Nov 21 22:29 .\r\nd      0 May 17 13:24 ..\r\n-  52945 Feb 27 10:00 sps_0001_20240227100033.zip\r\n"
           "-  89647 Nov 21 22:29 sps_0003_20261121222946.LoxCC\r\n- 100757 Nov 07 21:55 sps_0002_20261107215554.zip\r\n"
           "-  11728 Nov 21 22:29 permissions.bin\r\n-      2 Nov 07 21:55 nome con spazi.json\r\nd      0 Nov 07 22:01 custom\r\n")


class FakeMiniserver(Miniserver):
    """Miniserver finto: risposte fisse a `request` (WebSocket) e `http_download` (HTTP), nessuna rete."""

    def __init__(self, ws: dict, http: dict):
        super().__init__(Settings(host="10.0.0.2", user="u", password=None, permission=4, backups_dir=Path("."),
                                  admin_user=None, admin_password=None))
        self.ws_replies, self.http_files, self.http_calls = ws, http, []

    def request(self, cmd, encrypted_response=False, timeout=None):
        return self.ws_replies.get(cmd, {"Code": "200", "value": "{'directory': 'x','content': []}"})

    def http_download(self, path, timeout=60):
        self.http_calls.append(path)
        reply = self.http_files.get(path, 404)
        if isinstance(reply, int):      # un numero = risposta HTTP di errore
            raise HttpError(f"download di {path} rifiutato (HTTP {reply})", reply)
        return reply

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass


class FileListingTest(unittest.TestCase):
    def test_text_listing_of_the_http_webservice(self):
        files = parse_file_listing(LISTING)
        self.assertEqual([f["filename"] for f in files][:2], ["sps_0001_20240227100033.zip",
                                                              "sps_0003_20261121222946.LoxCC"])
        self.assertEqual(files[4], {"filename": "nome con spazi.json", "size": 2, "dir": False, "date": "Nov 07 21:55"})
        self.assertTrue(files[5]["dir"])
        self.assertEqual(parse_file_listing("Directory empty"), [])

    def test_listing_dates_have_no_year(self):
        now = datetime(2026, 11, 22, 9, 0)
        self.assertEqual(listing_time("Nov 21 22:29", now), datetime(2026, 11, 21, 22, 29))
        self.assertEqual(listing_time("Dec 03 08:15", now), datetime(2025, 12, 3, 8, 15))   # mai nel futuro
        self.assertIsNone(listing_time("May 17  2021", now))
        self.assertIsNone(listing_time("", now))

    def test_websocket_first_then_http(self):
        old = FakeMiniserver({"dev/fslist/prog": {"Code": "200", "value": "{'content': [{'filename': 'a.zip'}]}"}}, {})
        self.assertEqual(old.list_files("/prog/"), [{"filename": "a.zip"}])
        self.assertEqual(old.http_calls, [])                                 # firmware fino al 17.2: basta il WebSocket
        new = FakeMiniserver({}, {"dev/fslist/prog": LISTING.encode()})
        self.assertEqual(len(new.list_files("prog")), 6)                     # 17.3: elenco vuoto, si passa a HTTP
        self.assertEqual(FakeMiniserver({}, {"dev/fslist/x": b"Directory empty"}).list_files("x"), [])
        self.assertEqual(FakeMiniserver({}, {}).list_files("x"), [])         # 404: la cartella non esiste
        refused = FakeMiniserver({"dev/fslist/x": {"Code": "403", "value": ""}}, {})
        with self.assertRaises(LoxoneError):
            refused.list_files("x")

    def test_a_listing_that_cannot_be_read_is_not_an_empty_folder(self):
        for reply in (503, 403, b"<html>errore</html>"):                     # mai "non ci sono file"
            with self.subTest(reply=reply), self.assertRaises(LoxoneError):
                FakeMiniserver({}, {"dev/fslist/x": reply}).list_files("x")


class FirmwareSavedProgramTest(unittest.TestCase):
    """Dopo un aggiornamento del firmware il programma e' un .LoxCC con struttura e permessi in file separati."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.saved = (cli.open_ms, cli.DOWNLOADS)
        cli.DOWNLOADS = Path(self.tmp.name)
        self.addCleanup(lambda: setattr(cli, "DOWNLOADS", self.saved[1]))
        self.addCleanup(lambda: setattr(cli, "open_ms", self.saved[0]))

    def test_pack_program_has_the_members_of_a_config_zip(self):
        loxcc = uploader.compress_loxcc(SAMPLE.encode("utf-8"))
        packed = uploader.pack_program(loxcc, {"permissions.bin": b"P" * 32, "LoxAPP3.json": b"{}", "Music.json": b"[]"})
        with zipfile.ZipFile(io.BytesIO(packed)) as z:
            self.assertEqual(z.namelist(), ["sps0.LoxCC", "LoxAPP3.json", "permissions.bin", "Music.json"])
        rebuilt = uploader.build_program_zip(packed, SAMPLE.encode("utf-8"), {"LoxAPP3.json": b'{"a":1}'})
        self.assertEqual(uploader.read_member(rebuilt, "LoxAPP3.json"), b'{"a":1}')
        with self.assertRaises(ValueError):
            uploader.pack_program(b"non e' un LoxCC valido", {})

    def test_download_rebuilds_the_package(self):
        loxcc = uploader.compress_loxcc(SAMPLE.encode("utf-8"))
        ms = FakeMiniserver({}, {"dev/fslist/prog": LISTING.encode(),
                                 "dev/fsget/prog/sps_0003_20261121222946.LoxCC": loxcc,
                                 "dev/fsget/web/data/LoxAPP3.json": b'{"controls":{}}',
                                 "dev/fsget/prog/permissions.bin": b"\xff" * 16})
        cli.open_ms = lambda s: ms
        notes: list[str] = []
        dest = cli.download_current_program(None, notes)
        self.assertEqual(dest.name, "sps_0003_20261121222946.zip")          # il piu' recente, anche se e' un .LoxCC
        xml, app = read_program(dest)
        self.assertEqual((xml, app), (SAMPLE.encode("utf-8"), b'{"controls":{}}'))
        self.assertIn("Emergency.LoxCC", notes[0])                           # dice cosa non ha trovato
        self.assertEqual(cli.program_saved(dest), datetime(2026, 11, 21, 22, 29, 46))

    def test_download_stops_if_a_piece_cannot_be_read(self):
        ms = FakeMiniserver({}, {"dev/fslist/prog": LISTING.encode(),
                                 "dev/fsget/prog/sps_0003_20261121222946.LoxCC": uploader.compress_loxcc(SAMPLE.encode()),
                                 "dev/fsget/web/data/LoxAPP3.json": b"{}", "dev/fsget/prog/permissions.bin": 503})
        cli.open_ms = lambda s: ms
        with self.assertRaisesRegex(LoxoneError, "permissions.bin"):         # mai un pacchetto senza permessi
            cli.download_current_program(None)

    def test_download_of_a_zip_is_unchanged(self):
        zipped = uploader.pack_program(uploader.compress_loxcc(SAMPLE.encode("utf-8")), {"LoxAPP3.json": b"{}"})
        listing = b"- 100757 Nov 07 21:55 sps_0002_20261107215554.zip\r\n-    10 Nov 07 21:55 sps_new.zip\r\n"
        ms = FakeMiniserver({}, {"dev/fslist/prog": listing, "dev/fsget/prog/sps_0002_20261107215554.zip": zipped})
        cli.open_ms = lambda s: ms
        dest = cli.download_current_program(None)
        self.assertEqual((dest.name, dest.read_bytes()), ("sps_0002_20261107215554.zip", zipped))
        cli.open_ms = lambda s: FakeMiniserver({}, {})
        with self.assertRaises(LoxoneError):
            cli.download_current_program(None)


LIGHT_OBJ = ('<C Type="LightController2" V="{v}" U="' + U(40) + '" Title="Luci" WF="{wf}" SpStates="{sp}">'
             '<Co K="I1" U="' + U(41) + '">{ins}</Co><HCL FROM="{frm}" TO="60481"/>'
             '<LightscenesC Num="2">{moods}</LightscenesC></C>')
MOOD_A, MOOD_B = '<LightsceneC Name="Giorno" SID="1"/>', '<LightsceneC Name="Sera" SID="2"/>'
IN_A, IN_B = f'<In Input="{U(50)}"/>', f'<In Input="{U(51)}" FLG="2"/>'
PUSH = '<C Type="PuDe" V="178" U="' + U(60) + '" Title="Telefono" pTkn="x"/>'
CUSTOM_PROGRAM = ('<ControlList Version="3"><C Type="Document" U="' + U(0) + '" Title="Prova">'
                  + LIGHT_OBJ.format(v=180, wf=1, sp="a", ins=IN_A + IN_B, frm=27199, moods=MOOD_A + MOOD_B)
                  + '<C Type="LoxCaption" U="' + U(61) + '" Title="Notifiche">' + PUSH + '</C></C></ControlList>')


class AppChangesTest(unittest.TestCase):
    """Modifiche fatte dall'app (cartella custom): quali andrebbero perse con un caricamento."""

    def setUp(self):
        self.prog = Program(CUSTOM_PROGRAM.encode("utf-8"))

    def kind(self, xml: str) -> str:
        return custom_change(xml.encode("utf-8"), self.prog)[0]

    def test_what_the_firmware_rewrites_by_itself_is_not_a_change(self):
        same = LIGHT_OBJ.format(v=178, wf=9, sp="b", ins=IN_B + IN_A, frm=26022, moods=MOOD_A + MOOD_B)
        self.assertEqual(self.kind(same), "integrata")

    def test_real_changes_are_pending(self):
        reordered = LIGHT_OBJ.format(v=180, wf=1, sp="a", ins=IN_A + IN_B, frm=27199, moods=MOOD_B + MOOD_A)
        self.assertEqual(custom_change(reordered.encode(), self.prog), ("in sospeso", "Luci <LightController2>"))
        renamed = LIGHT_OBJ.format(v=180, wf=1, sp="a", ins=IN_A + IN_B, frm=27199,
                                   moods=MOOD_A + MOOD_B.replace("Sera", "Notte"))
        self.assertEqual(self.kind(renamed), "in sospeso")
        new = '<C Type="AutopilotRule" U="' + U(70) + '" Title="Regola"/>'
        self.assertEqual(custom_change(new.encode(), self.prog),
                         ("in sospeso", "Regola <AutopilotRule> (creato dall'app)"))

    def test_push_registrations_are_counted_apart(self):
        self.assertEqual(self.kind(PUSH), "integrata")                       # gia' nel programma
        other = PUSH.replace(U(60), U(62))
        self.assertEqual(self.kind(other), "registrazione")
        caption = '<C Type="LoxCaption" U="' + U(61) + '" Title="Notifiche">' + other + '</C>'
        self.assertEqual(self.kind(caption), "registrazione")

    def test_only_files_newer_than_the_program_count(self):
        def loxcc(xml: str) -> bytes:
            return uploader.compress_loxcc(xml.encode("utf-8"))
        reordered = LIGHT_OBJ.format(v=180, wf=1, sp="a", ins=IN_A + IN_B, frm=27199, moods=MOOD_B + MOOD_A)
        listing = (f"-    300 Nov 07 21:55 {U(62)}.LoxCC\r\n-   3796 Nov 07 22:01 {U(40)}.LoxCC\r\n"
                   f"-    298 Nov 07 22:03 {U(63)}.LoxCC\r\n-     10 Nov 07 22:05 rotto.LoxCC\r\nd      0 Nov 07 22:01 .\r\n")
        ms = FakeMiniserver({}, {"dev/fslist/sys/custom": listing.encode(),
                                 f"dev/fsget/sys/custom/{U(62)}.LoxCC": loxcc(PUSH.replace(U(60), U(62))),
                                 f"dev/fsget/sys/custom/{U(40)}.LoxCC": loxcc(reordered),
                                 f"dev/fsget/sys/custom/{U(63)}.LoxCC": loxcc(PUSH.replace(U(60), U(63))),
                                 "dev/fsget/sys/custom/rotto.LoxCC": b"niente"})
        saved_open = cli.open_ms
        self.addCleanup(lambda: setattr(cli, "open_ms", saved_open))
        cli.open_ms = lambda s: ms
        year = cli.listing_time("Nov 07 22:01").year
        # programma salvato alle 21:55:54: il file delle 21:55 e' stato scritto all'avvio, non dall'app
        pending, registrations = cli.pending_app_changes(None, self.prog, datetime(year, 11, 7, 21, 55, 54))
        self.assertEqual(registrations, 1)
        self.assertEqual(pending[0], "Luci <LightController2>")
        self.assertTrue(pending[1].startswith("rotto.LoxCC (non leggibile"))   # nel dubbio, non si carica
        # programma salvato dopo (dal firmware o da Config): quelle modifiche sono gia' dentro
        self.assertEqual(cli.pending_app_changes(None, self.prog, datetime(year, 11, 21, 22, 29, 46)), ([], 0))


class LeftoverUploadTest(unittest.TestCase):
    """Un sps_new.zip rimasto sulla scheda verrebbe caricato a un riavvio successivo: va tolto."""

    def run_cleanup(self, result, quiet=False) -> str:
        saved = uploader.ftp_delete
        self.addCleanup(lambda: setattr(uploader, "ftp_delete", saved))

        def fake(host, user, password, path):
            self.assertEqual(path, "/prog/sps_new.zip")
            if isinstance(result, Exception):
                raise result
            return result
        uploader.ftp_delete = fake
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            cli.remove_leftover_upload(Settings(host="10.0.0.2", user="u", password="p", permission=4,
                                                backups_dir=Path("."), admin_user=None, admin_password=None),
                                       "u", "p", quiet=quiet)
        return err.getvalue()

    def test_reports_only_what_happened(self):
        self.assertIn("l'ho tolto", self.run_cleanup(True))
        self.assertEqual(self.run_cleanup(True, quiet=True), "")
        self.assertEqual(self.run_cleanup(False), "")                        # caricato e rinominato: tutto regolare
        self.assertIn("cancellalo", self.run_cleanup(OSError("rete")))       # non si sa: lo dice

    def test_a_program_in_another_format_is_not_uploaded(self):
        def prog(fmt: str | None) -> Program:
            attr = f' LxAV="{fmt}"' if fmt else ""
            return Program(SAMPLE.replace('<ControlList Version="1"', f'<ControlList Version="1"{attr}').encode("utf-8"))
        self.assertIsNone(cli.format_mismatch(prog("88"), prog("88")))
        self.assertIsNone(cli.format_mismatch(prog(None), prog("88")))        # formato non dichiarato: nessun giudizio
        self.assertIn("formato 87", cli.format_mismatch(prog("88"), prog("87")))

    def test_restart_does_not_fail_when_the_connection_drops(self):
        class Dropping(FakeMiniserver):
            def request(self, cmd, encrypted_response=False, timeout=None):
                raise ConnectionResetError("chiusa")
        self.assertEqual(Dropping({}, {}).restart_program()["Code"], "200")


class UploadGuardsTest(unittest.TestCase):
    """Le protezioni del caricamento: nel dubbio ci si ferma."""

    @staticmethod
    def alarm(states: dict | None = None) -> tuple[Structure, dict]:
        ids = {name: f"a-{name}" for name in ("armed", "level", "armedDelay")}
        st = Structure({"rooms": {}, "cats": {}, "controls": {
            "a": {"name": "Antifurto", "type": "Alarm", "uuidAction": "a", "states": ids}}})
        return st, {ids[k]: v for k, v in (states or {}).items()}

    def test_alarm_blocks_when_armed_arming_triggered_or_unknown(self):
        cases = {"INSERITO": {"armed": 1.0, "level": 0.0}, "IN ALLARME": {"armed": 0.0, "level": 2.0},
                 "fase di inserimento": {"armed": 0.0, "level": 0.0, "armedDelay": 240.0},
                 "non leggibile": {}}
        for expected, states in cases.items():
            st, values = self.alarm(states)
            with self.subTest(expected):
                self.assertIn(expected, cli.alarm_block_reason(st, values, {}, 1))

    def test_alarm_free_only_when_really_disarmed(self):
        st, values = self.alarm({"armed": 0.0, "level": 0.0, "armedDelay": 0.0})
        self.assertIsNone(cli.alarm_block_reason(st, values, {}, 1))
        no_alarm = Structure({"rooms": {}, "cats": {}, "controls": {}})
        self.assertIsNone(cli.alarm_block_reason(no_alarm, {}, {}, 0))       # casa senza antifurto
        self.assertIn("non visibili", cli.alarm_block_reason(no_alarm, {}, {}, 1))  # c'e', ma questo utente non lo vede

    def test_users_and_permissions_are_seen(self):
        user = f'<C Type="User" U="{U(80)}" Title="Nuovo" Admin="true"/>'
        changed = SAMPLE.replace('<C Type="PlaceCaption"', user + '<C Type="PlaceCaption"', 1)
        self.assertNotEqual(changed, SAMPLE)
        self.assertEqual(access_changes(Program(SAMPLE.encode()), Program(changed.encode())), ["User: 1 aggiunti"])
        self.assertEqual(access_changes(Program(changed.encode()), Program(changed.encode())), [])

    def test_inner_elements_of_a_block_are_seen(self):
        xml = ('<ControlList Version="1"><C Type="Document" U="' + U(0) + '" Title="P"><C Type="LoggerMailer" U="'
               + U(81) + '" Title="Avviso"><On Text="{}"/></C></C></ControlList>')
        before, after = Program(xml.format("porta").encode()), Program(xml.format("cancello").encode())
        notes = diff(before, after)["modificati"][0]["modifiche"]
        self.assertEqual(notes, ["On.Text: 'porta' -> 'cancello'"])
        self.assertEqual(diff(before, after, children=False)["modificati"], [])

    def test_secrets_never_shown(self):
        for text in ("http://admin:Segreta1@10.0.0.5/foto", "x?appid=Segreta1&a=1", "y?access_token=Segreta1",
                     "client_secret=Segreta1", "Authorization: Bearer Segreta1", "Bearer Segreta1abc"):
            with self.subTest(text):
                self.assertNotIn("Segreta1", redact(text))
        shown = redact_data({"video": {"user": "mario", "url": "http://u:Segreta1@h/", "nome": "Ingresso", "n": 3}})
        self.assertEqual(shown, {"video": {"user": "***", "url": "http://***@h/", "nome": "Ingresso", "n": 3}})

    def test_only_the_backups_of_this_miniserver(self):
        with tempfile.TemporaryDirectory() as tmp:
            for name in ("Backup_10.0.0.2_20260101100000_17030000", "Backup_10.0.0.9_20261201100000_17030000"):
                (Path(tmp) / name).mkdir()
            self.assertEqual(len(backups.list_backups(Path(tmp))), 2)
            self.assertEqual([b["host"] for b in backups.list_backups(Path(tmp), "10.0.0.2")], ["10.0.0.2"])
            self.assertEqual(len(backups.list_backups(Path(tmp), "10.0.0.2:8080")), 1)

    def test_admin_name_never_with_the_other_password(self):
        base = dict(host="h", permission=4, backups_dir=Path("."))
        self.assertEqual(Settings(user="u", password="p", admin_user="a", admin_password=None, **base).upload_creds(),
                         ("a", None))
        self.assertEqual(Settings(user="u", password="p", admin_user=None, admin_password=None, **base).upload_creds(),
                         ("u", "p"))

    def test_rollback_is_verified_only_when_the_upload_was_consumed(self):
        """Il file piu' recente e' gia' la versione giusta: finche' c'e' sps_new.zip non e' una prova."""
        saved = (cli.upload_pending, cli.download_current_program, cli.time.sleep)
        self.addCleanup(lambda: (setattr(cli, "upload_pending", saved[0]),
                                 setattr(cli, "download_current_program", saved[1]),
                                 setattr(cli.time, "sleep", saved[2])))
        with tempfile.TemporaryDirectory() as tmp:
            current = Path(tmp) / "sps_0001_20260101100000.Loxone"
            current.write_bytes(SAMPLE.encode("utf-8"))
            cli.download_current_program = lambda s: current
            cli.time.sleep = lambda seconds: None
            cli.upload_pending = lambda s: True
            self.assertFalse(cli.wait_for_program(None, Program(SAMPLE.encode("utf-8")), seconds=0.02))
            cli.upload_pending = lambda s: False
            self.assertTrue(cli.wait_for_program(None, Program(SAMPLE.encode("utf-8")), seconds=5))


class UploadFlowTest(unittest.TestCase):
    """Il percorso intero di 'lox carica' con Miniserver e FTP finti: cosa viene inviato, quando e cosa no."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.old, self.new = SAMPLE.encode("utf-8"), set_parameter(SAMPLE.encode("utf-8"), PULSE_TIME, "0.2")
        self.backup = Path(tmp.name) / "sps_0001_20260101100000.zip"
        self.backup.write_bytes(uploader.pack_program(uploader.compress_loxcc(self.old),
                                                      {"LoxAPP3.json": b'{"controls":{}}', "permissions.bin": b"\xff" * 16}))
        self.copy = Path(tmp.name) / "copia.Loxone"
        self.copy.write_bytes(self.new)
        self.sent, self.deleted, self.restarts = [], [], []
        self.settings = Settings(host="10.0.0.2", user="u", password="p", permission=4, backups_dir=Path("."),
                                 admin_user=None, admin_password=None)
        flow = self

        class FakeAdmin:
            def __init__(self, settings):
                pass

            def connect_admin(self):
                return self

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                pass

            def kill_token(self, forget_saved=True):
                return True

            def restart_program(self):
                flow.restarts.append(True)

        class FakeLive(FakeAdmin):
            values, texts = {}, {}

        def put(host, user, password, data, **kw):
            self.sent.append(data)
            return uploader.FtpResult(len(data), self.size_ok)

        def log(*a):
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            return (f"{now}.000;Restart SPS by user 'u'\n{now}.100;Loading sps_new.zip\n"
                    f"{now}.200;Program started: x\n").encode()

        self.size_ok = True
        structure = Structure({"rooms": {}, "cats": {}, "controls": {}})
        patches = [
            mock.patch.object(cli, "Miniserver", FakeAdmin),
            mock.patch.object(cli, "download_current_program", lambda s, notes=None: self.backup),
            mock.patch.object(cli, "pending_app_changes", lambda *a: ([], 0)),
            mock.patch.object(cli, "has_credentials", lambda s: True),
            mock.patch.object(cli, "live_states", lambda s: (structure, FakeLive(s))),
            mock.patch.object(cli.time, "sleep", lambda s: None),
            mock.patch.object(uploader, "ftp_reachable", lambda host: True),
            mock.patch.object(uploader, "ftp_put_program", put),
            mock.patch.object(uploader, "ftp_delete", lambda h, u, p, path: self.deleted.append(path) or True),
            mock.patch.object(uploader, "ftp_download", log),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def upload(self, active=(True,), applica=True, conferma=True):
        args = argparse.Namespace(file=str(self.copy), applica=applica, conferma=conferma, forza=False, ignora_app=False)
        with mock.patch.object(cli, "wait_for_program", side_effect=list(active)), \
                contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()):
            cli.cmd_upload(args, self.settings)
        return out.getvalue()

    def sent_program(self, i: int) -> bytes:
        return decompress(uploader.read_member(self.sent[i], "sps0.LoxCC"))

    def test_success(self):
        self.assertIn("CARICAMENTO DIRETTO RIUSCITO", self.upload())
        self.assertEqual((len(self.sent), len(self.restarts), self.deleted), (1, 1, []))
        self.assertEqual(self.sent_program(0), self.new)

    def test_dry_run_and_missing_consent_send_nothing(self):
        self.assertIn("[PROVA]", self.upload(applica=False))
        with self.assertRaisesRegex(LoxoneError, "--conferma"):
            self.upload(conferma=False)
        self.assertEqual((self.sent, self.restarts), ([], []))

    def test_failed_activation_goes_back_to_the_old_program(self):
        with self.assertRaisesRegex(LoxoneError, "ripristinata la versione precedente"):
            self.upload(active=(False, True))
        self.assertEqual((len(self.sent), len(self.restarts), self.deleted), (2, 2, []))
        self.assertEqual(self.sent_program(1), self.old)

    def test_unconfirmed_rollback_says_so_and_deletes_nothing(self):
        with self.assertRaisesRegex(LoxoneError, "NON confermato"):
            self.upload(active=(False, False))
        self.assertEqual(self.deleted, [])       # il Miniserver potrebbe star ripartendo proprio da quel file

    def test_broken_upload_is_removed_and_never_activated(self):
        self.size_ok = False
        with self.assertRaisesRegex(LoxoneError, "non verificato"):
            self.upload()
        self.assertEqual((self.restarts, self.deleted), ([], ["/prog/sps_new.zip"]))

    def test_lights_changed_by_the_restart_are_reported(self):
        before = {"a": {"nome": "Ingresso", "stato": "scene attive: Off", "scene": [778]},
                  "b": {"nome": "Cucina", "stato": "scene attive: nessuna", "scene": []},
                  "c": {"nome": "Bagno", "stato": "scene attive: Off", "scene": [778]}}
        after = {"a": {"nome": "Ingresso", "stato": "scene attive: Tanta luce", "scene": [777]},
                 "b": {"nome": "Cucina", "stato": "scene attive: Tanta luce", "scene": [777]},
                 "c": {"nome": "Bagno", "stato": "scene attive: Off", "scene": [778]}}
        lines = cli.light_changes(before, after)
        self.assertEqual(len(lines), 2)
        self.assertIn("lox comando a changeTo/778", lines[0])            # si dice come tornare, senza farlo
        self.assertIn("non aveva una scena", lines[1])

    def test_armed_alarm_stops_everything(self):
        with mock.patch.object(cli, "alarm_block_reason", lambda *a: "antifurto 'Casa' INSERITO"), \
                self.assertRaisesRegex(LoxoneError, "INSERITO"):
            self.upload()
        self.assertEqual((self.sent, self.restarts), ([], []))


class CopiesAndSecretsTest(unittest.TestCase):
    XML = ('<ControlList Version="1"><C Type="Document" U="' + U(0) + '" Title="P"><C Type="Page" U="' + U(3)
           + '" Title="Pagina">\n<C Type="PushButton" U="' + U(90) + '" Title="Tasto" SpStates="' + U(91) + ',' + U(92)
           + '" Nota="riga 1&#10;riga 2">\n<Co K="Q" U="' + U(93) + '"/>\n</C>\n</C></C></ControlList>')

    def test_a_copy_has_its_own_states_and_keeps_line_breaks(self):
        block = Program(self.XML.encode("utf-8")).by_uuid[U(90)]
        copy, _, _ = editor.clone_block(block, editor.UuidFactory(self.XML))
        self.assertNotIn(U(91), copy)
        self.assertEqual(len(re.search(r'SpStates="([^"]*)"', copy).group(1).split(",")), 2)
        self.assertIn("riga 1&#10;riga 2", copy)
        group, _ = editor.clone_group(self.XML, [U(90)], editor.UuidFactory(self.XML))
        self.assertNotIn(U(91), group[0])
        self.assertNotIn(U(92), group[0])

    def test_only_real_duplicate_ids_are_reported(self):
        a, b = U(95), U(96)
        firmware_17_3 = f'<C U="{a}" SwBU="{b}"/><C U="{b}" SwBU="{b}"/>'   # SwBU cita un altro oggetto
        self.assertEqual(editor.duplicate_ids(firmware_17_3), [])
        self.assertEqual(editor.duplicate_ids(f'<C U="{a}"/><C U="{a}"/>'), [a])
        self.assertEqual(editor.duplicate_ids(f'<C U="{a}"/><L UUID="{a}"/>'), [a])

    def test_a_copy_refuses_connectors_the_model_does_not_have(self):
        block = Program(self.XML.encode("utf-8")).by_uuid[U(90)]
        with self.assertRaisesRegex(editor.EditError, "Delay"):
            editor.clone_block(block, editor.UuidFactory(self.XML), overrides={"Delay": {"Def": "60"}})

    def test_env_passwords_do_not_reach_the_programs_we_start(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(config.os.environ), \
                mock.patch.dict(config._file_secrets, clear=True):
            config.os.environ.pop("LOXONE_PASSWORD", None)
            env = Path(tmp) / ".env"
            env.write_text("LOXONE_PASSWORD=Segreta1\nLOXONE_PROVA=valore\n", encoding="utf-8")
            config.load_env(env)
            self.assertNotIn("LOXONE_PASSWORD", config.os.environ)
            self.assertEqual(config._secret("LOXONE_PASSWORD"), "Segreta1")
            self.assertEqual(config.os.environ["LOXONE_PROVA"], "valore")


class SensitiveControlsTest(unittest.TestCase):
    """Quali comandi richiedono --conferma (e il consenso esplicito di chi abita la casa)."""

    def setUp(self):
        self.st = Structure({"rooms": {"r": {"name": "Ingresso"}}, "cats": {}, "controls": {
            "i": {"name": "Citofono", "type": "Intercom", "uuidAction": "i", "room": "r",
                  "subControls": {"i/1": {"name": "Apertura", "type": "Pushbutton", "uuidAction": "i/1"}}},
            "g": {"name": "Apri cancello", "type": "Pushbutton", "uuidAction": "g", "room": "r"},
            "l": {"name": "Luce sala", "type": "Switch", "uuidAction": "l", "room": "r"},
            "p": {"name": "Porta casa", "type": "InfoOnlyDigital", "uuidAction": "p", "room": "r"},
            "s": {"name": "Sirena esterna", "type": "Switch", "uuidAction": "s", "room": "r"},
            "f": {"name": "Ingresso", "type": "SmartDoorLock", "uuidAction": "f", "room": "r"},
            "w": {"name": "Sveglia", "type": "AlarmClock", "uuidAction": "w", "room": "r"},
            "t": {"name": "Monitor ventilatore", "type": "Switch", "uuidAction": "t", "room": "r"},
        }})

    def reason(self, key: str, protected=()):
        return self.st.sensitive_reason(self.st.controls[key], protected)

    def test_types_and_their_sub_controls(self):
        self.assertIn("Intercom", self.reason("i"))
        self.assertIn("fa parte di 'Citofono'", self.reason("i/1"))          # l'apriporta e' un semplice pulsante
        self.assertIn("SmartDoorLock", self.reason("f"))                     # tipo non in elenco, ma parla di porte
        self.assertIsNone(self.reason("w"))

    def test_names_and_user_list(self):
        self.assertIn("'apri'", self.reason("g"))
        self.assertIn("'sirena'", self.reason("s"))
        self.assertIsNone(self.reason("l"))
        self.assertIsNone(self.reason("t"))                                  # "monitor", "ventilatore": nessun falso allarme
        self.assertIsNone(self.reason("p"))                                  # sola lettura: non c'e' nulla da comandare
        self.assertIn("LOXONE_PROTETTI", self.reason("l", ("luce sala",)))
        self.assertIn("LOXONE_PROTETTI", self.reason("t", ("T",)))           # anche per UUID


if __name__ == "__main__":
    unittest.main()
