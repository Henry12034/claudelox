"""Test senza Miniserver: formato .LoxCC, pacchetto di caricamento, modifiche e confronto del programma.

Esecuzione:  python -m unittest discover -s tests -v
Usano un piccolo programma sintetico (nessun dato di un impianto reale).
"""
from __future__ import annotations

import io
import json
import struct
import sys
import tempfile
import unittest
import zipfile
import zlib
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from claudelox import appstructure, editor, uploader  # noqa: E402
from claudelox.cli import activation_outcome  # noqa: E402
from claudelox.loxcc import MAGIC, decompress, read_program  # noqa: E402
from claudelox.program import Program, app_impact, diff, set_parameter  # noqa: E402

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


HTTP_APP = ('{"lastModified":"2026-01-01 10:00:00","msInfo":{"latitude":45.697000,"altitude":273},"rooms":{'
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
        self.assertEqual(appstructure.uuid_bytes("1d0c2a08-01c2-eb67-ffff1402153adf25").hex(),
                         "082a0c1dc20167ebffff1402153adf25")
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


if __name__ == "__main__":
    unittest.main()
