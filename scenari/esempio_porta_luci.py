"""Esempio di nuova logica: aprendo una porta si accendono delle luci, chiudendola si spengono.

Uso:  python scenari/esempio_porta_luci.py --ingresso "Porta casa" --luci "Luci_sala" --pagina "Cucina/Sala"
          [--titolo "Test porta"] [--file programma.zip|.LoxCC|.Loxone]
      (senza --file usa l'ultima versione scaricata con 'lox programma scarica')

Crea una COPIA modificata in data/programmi/ e stampa le differenze: non invia nulla al Miniserver.
Per caricarla: 'lox carica <copia>' (prova a secco), poi con '--applica --conferma'.
Per toglierla: 'lox programma rimuovi' con i tre blocchi creati (i due impulsi e il riferimento all'ingresso).

Logica aggiunta nella pagina indicata (stesso schema usato di solito nei programmi Loxone):
  ingresso --(invertito)--> [impulso 0,1 s] --> luci.On     (porta che si apre)
  ingresso ---------------> [impulso 0,1 s] --> luci.Reset  (porta che si chiude)
Si assume un contatto che vale 1 a porta CHIUSA (il caso piu' comune): per questo l'apertura usa
l'ingresso invertito. Gli impulsi evitano che il Reset resti attivo a porta chiusa (bloccherebbe le luci).
Come modelli servono, nel programma, un riferimento (InputRef) all'ingresso e un blocco OnPulseDelay.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from claudelox import backups, editor  # noqa: E402
from claudelox.config import DATA_DIR, get_settings  # noqa: E402
from claudelox.loxcc import read_program  # noqa: E402
from claudelox.program import Program, diff  # noqa: E402


def only(items, what):
    items = list(items)
    if len(items) != 1:
        raise SystemExit(f"ERRORE: atteso un solo {what}, trovati {len(items)}")
    return items[0]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--ingresso", required=True, help="titolo dell'ingresso digitale della porta")
    ap.add_argument("--luci", required=True, help="titolo del blocco controllo luci (LightController2)")
    ap.add_argument("--pagina", required=True, help="pagina del programma dove aggiungere la logica")
    ap.add_argument("--titolo", default="Test porta", help="prefisso dei titoli dei nuovi blocchi")
    ap.add_argument("--file", help="programma di partenza (default: ultimo scaricato)")
    args = ap.parse_args()
    title_on, title_off = f"{args.titolo}: accende luci", f"{args.titolo}: spegne luci"

    if args.file:
        src = Path(args.file)
    else:
        p = backups.newest_program(get_settings().backups_dir, DATA_DIR / "programmi")
        if not p:
            raise SystemExit("ERRORE: nessun programma locale, esegui prima 'lox programma scarica'")
        src = p["path"]
    xml, _ = read_program(src)
    text = xml.decode("utf-8")
    if title_on in text:
        raise SystemExit("La logica e' gia' presente in questo programma.")
    prog = Program(xml)

    door = only((e for e in prog.components() if e.get("Type") == "DigitalIn" and e.get("Title") == args.ingresso),
                f"ingresso digitale '{args.ingresso}'")
    page = prog.page(args.pagina)
    lights = prog.block(args.luci)
    if lights.get("Type") != "LightController2":
        raise SystemExit(f"ERRORE: '{args.luci}' non e' un controllo luci")
    refs = [e for e in prog.components() if e.get("Type") == "InputRef" and e.get("Ref") == door.get("U")]
    if not refs:
        raise SystemExit(f"ERRORE: nessun riferimento a '{args.ingresso}' da usare come modello")
    pulses = [e for e in prog.components() if e.get("Type") == "OnPulseDelay"]
    if not pulses:
        raise SystemExit("ERRORE: nessun blocco OnPulseDelay da usare come modello")
    ref_template = next((e for e in refs if e in page), refs[0])
    pulse_template = next((e for e in pulses if e in page), pulses[0])
    light_inputs = {co.get("K"): co.get("U") for co in lights.findall("Co")}

    ids = editor.UuidFactory(text)
    x, y = editor.free_area_below(page)
    ref_xml, _, ref_co = editor.clone_block(ref_template, ids, position=(x, y + 336))
    on_xml, _, on_co = editor.clone_block(
        pulse_template, ids, title=title_on, position=(x + 3264, y),
        overrides={"InputTrigger": {"inputs": [ref_co["AQ"]], "Inv": True}, "Delay": {"Def": None},
                   "Reset": {"inputs": []}, "Time": {"Def": "0.1"}})
    off_xml, _, off_co = editor.clone_block(
        pulse_template, ids, title=title_off, position=(x + 3264, y + 960),
        overrides={"InputTrigger": {"inputs": [ref_co["AQ"]], "Inv": False}, "Delay": {"Def": None},
                   "Reset": {"inputs": []}, "Time": {"Def": "0.1"}})

    text = editor.insert_children(text, page.get("U"), [ref_xml, on_xml, off_xml])
    text = editor.add_input(text, light_inputs["On"], on_co["Q"])
    text = editor.add_input(text, light_inputs["Reset"], off_co["Q"])
    text = editor.bump_counters(text, 3)
    new_xml = text.encode("utf-8")

    new_prog = Program(new_xml)  # il file deve restare XML valido
    for e in new_prog.components():
        if e.get("Title") in (title_on, title_off):
            assert e in new_prog.page(args.pagina), "blocco finito fuori dalla pagina"
    dup = [u for u in ids.used if text.count(f'U="{u}"') > 1]
    if dup:
        raise SystemExit(f"ERRORE: UUID duplicati {dup}")

    out = DATA_DIR / "programmi" / f"{src.stem}-porta-luci-{datetime.now():%Y%m%d-%H%M}.Loxone"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(new_xml)
    d = diff(prog, new_prog)
    print(f"Partenza: {src.name}\nCopia modificata: {out}")
    print(f"Aggiunti: {len(d['aggiunti'])}  Rimossi: {len(d['rimossi'])}  Modificati: {len(d['modificati'])}")
    for a in d["aggiunti"]:
        print(f"  + {a}")
    for ch in d["modificati"]:
        print(f"  ~ {ch['oggetto']}")
        for note in ch["modifiche"]:
            print(f"      {note}")


if __name__ == "__main__":
    main()
