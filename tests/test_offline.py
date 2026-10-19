"""Test senza Miniserver: formato .LoxCC, pacchetto di caricamento, modifiche e confronto del programma.

Esecuzione:  python -m unittest discover -s tests -v
Usano un piccolo programma sintetico (nessun dato di un impianto reale).
"""
from __future__ import annotations

import io
import struct
import sys
import tempfile
import unittest
import zipfile
import zlib
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from claudelox import editor, uploader  # noqa: E402
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
