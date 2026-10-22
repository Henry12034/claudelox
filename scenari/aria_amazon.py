"""Ingressi del sensore della qualita' dell'aria Amazon (ponte integrazioni/amazon-alexa) nel programma Loxone.

Uso:  python scenari/aria_amazon.py --indirizzo http://<raspberry>:8095/aria --stanza "Camera da Letto"
          [--categoria Sensori] [--nome "Sensore aria Amazon"] [--valori qualita,pm25,...]
          [--titoli "temperatura=Temperatura aria"] [--file programma.zip|.LoxCC|.Loxone]
      (senza --file usa l'ultima versione scaricata con 'lox programma scarica')

Aggiunge un ingresso virtuale HTTP (stesso tipo dei valori del meteo) che ogni minuto legge la pagina del
ponte sul Raspberry, con un comando per valore visibile nell'app nella stanza indicata (riconoscimento
"\\iqualita=\\i\\v" e cosi' via), piu' "Sensore aria in linea" (1/0, non visibile, utile per gli avvisi).
Come modello serve nel programma un ingresso virtuale HTTP con un comando analogico (es. quello del meteo).

Crea una COPIA modificata in data/programmi/ e stampa le differenze: non invia nulla al Miniserver.
Le modifiche cambiano l'app, quindi vanno caricate con Loxone Config ('lox programma apri <copia>',
poi "Salva nel Miniserver"). Per toglierle: 'lox programma rimuovi' con i comandi e l'ingresso creati.
"""
from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from claudelox import backups, editor  # noqa: E402
from claudelox.config import DATA_DIR, get_settings  # noqa: E402
from claudelox.loxcc import read_program  # noqa: E402
from claudelox.program import Program, app_impact, diff  # noqa: E402
from claudelox.structure import norm  # noqa: E402

# chiave nella pagina del ponte: (titolo nell'app, unita' di Loxone, parole per cercare un'icona simile)
VALUES = {
    "qualita": ("Qualità dell'aria", "<v>", ()),
    "pm25": ("Polveri sottili PM2.5", "<v.1> µg/m³", ()),
    "pm10": ("Polveri sottili PM10", "<v.1> µg/m³", ()),
    "voc": ("Composti organici volatili", "<v.1>", ()),
    "co": ("Monossido di carbonio", "<v.1> ppm", ()),
    "umidita": ("Umidità", "<v> %", ("umidit",)),
    "temperatura": ("Temperatura", "<v.1>°", ("temperatura",)),
}
ONLINE_TITLE = "Sensore aria in linea"


def only(items, what):
    items = list(items)
    if len(items) != 1:
        raise SystemExit(f"ERRORE: atteso un solo {what}, trovati {len(items)}")
    return items[0]


def by_title(prog: Program, type_: str, title: str):
    hits = [e for e in prog.components() if e.get("Type") == type_ and norm(e.get("Title")) == norm(title)]
    if len(hits) != 1:
        names = ", ".join(e.get("Title") for e in prog.components() if e.get("Type") == type_)
        raise SystemExit(f"ERRORE: '{title}' non trovato o ambiguo. Disponibili: {names}")
    return hits[0]


def templates(prog: Program):
    """Ingresso virtuale HTTP, stato online e comando analogico visibile nell'app da usare come modelli.

    Tra i comandi si preferisce uno con la statistica attiva e senza conversione del valore (come quelli
    del meteo): altri, per esempio quelli di uno Shelly, trasformano il testo "true"/"false" in 1/0.
    """
    best, best_score = None, -1
    for http_in in (e for e in prog.components() if e.get("Type") == "VirtualHttpIn"):
        online = next((c for c in http_in if c.tag == "C" and c.get("Type") == "Online"), None)
        for cmd in (c for c in http_in if c.tag == "C" and c.get("Type") == "VirtualHttpInCmd"):
            io = cmd.find("IoData")
            if (online is None or cmd.get("Analog") != "true" or io is None or io.get("Visu") != "true"
                    or cmd.find("Display") is None):
                continue
            score = 2 * bool(cmd.get("StatsType")) + (cmd.get("SourceValLow") is None
                                                      and cmd.get("SourceValHigh") == cmd.get("DestValHigh"))
            if score > best_score:
                best, best_score = (http_in, online, cmd), score
    if best is None:
        raise SystemExit("ERRORE: nel programma manca un ingresso virtuale HTTP con un comando analogico da usare "
                         "come modello (per esempio quello dei valori del meteo)")
    return best


def icon_like(prog: Program, words: tuple[str, ...]) -> str | None:
    """Icona di un valore simile gia' visibile nell'app (es. un'altra umidita'), se c'e'."""
    for e in prog.components():
        io = e.find("IoData")
        if words and io is not None and io.get("IconV") and any(w in norm(e.get("Title")) for w in words):
            return io.get("IconV")
    return None


def next_iname(text: str, prefix: str) -> int:
    return max((int(n) for n in re.findall(rf'\bIName="{prefix}(\d+)"', text)), default=0) + 1


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--indirizzo", required=True, help="pagina del ponte, es. http://10.0.0.5:8095/aria")
    ap.add_argument("--stanza", required=True, help="stanza dell'app in cui mostrare i valori")
    ap.add_argument("--categoria", default="Sensori", help="categoria dell'app (default: Sensori)")
    ap.add_argument("--nome", default="Sensore aria Amazon", help="titolo dell'ingresso virtuale")
    ap.add_argument("--valori", default=",".join(VALUES), help=f"valori da creare (default: {','.join(VALUES)})")
    ap.add_argument("--titoli", default="", help="titoli diversi nell'app, es. 'temperatura=Temperatura aria'")
    ap.add_argument("--file", help="programma di partenza (default: ultimo scaricato)")
    args = ap.parse_args()
    keys = [k.strip() for k in args.valori.split(",") if k.strip()]
    unknown = [k for k in keys if k not in VALUES]
    if unknown:
        raise SystemExit(f"ERRORE: valori sconosciuti {unknown}; possibili: {', '.join(VALUES)}")
    titles = {k: v[0] for k, v in VALUES.items()}
    for pair in filter(None, (p.strip() for p in args.titoli.split(","))):
        k, _, t = pair.partition("=")
        if k.strip() not in VALUES or not t.strip():
            raise SystemExit(f"ERRORE: titolo non valido '{pair}' (forma: chiave=titolo)")
        titles[k.strip()] = t.strip()

    if args.file:
        src = Path(args.file)
    else:
        p = backups.newest_program(get_settings().backups_dir, DATA_DIR / "programmi")
        if not p:
            raise SystemExit("ERRORE: nessun programma locale, esegui prima 'lox programma scarica'")
        src = p["path"]
    xml, app_json = read_program(src)
    text = xml.decode("utf-8")
    prog = Program(xml)
    if any(e.get("Type") == "VirtualHttpIn" and (e.get("Address") == args.indirizzo or e.get("Title") == args.nome)
           for e in prog.components()):
        raise SystemExit("Nel programma c'e' gia' un ingresso con questo indirizzo o questo nome.")
    room = by_title(prog, "Place", args.stanza)
    category = by_title(prog, "Category", args.categoria)
    taken = {norm(e.get("Title")) for e in prog.components()
             if e.find("IoData") is not None and e.find("IoData").get("Pr") == room.get("U")
             and e.find("IoData").get("Visu") == "true"}
    clash = [titles[k] for k in keys if norm(titles[k]) in taken]
    if clash:
        raise SystemExit(f"ERRORE: nella stanza '{args.stanza}' ci sono gia' controlli chiamati {clash}: "
                         f"scegli altri titoli con --titoli")
    http_t, online_t, cmd_t = templates(prog)
    container = prog.parent[http_t]

    ids = editor.UuidFactory(text)
    vci = next_iname(text, "VCI")
    cmds = []
    for n, k in enumerate(keys + ["online"]):
        visible = k != "online"
        unit = VALUES[k][1] if visible else "<v>"
        cmd_xml, _, _ = editor.clone_block(
            cmd_t, ids, title=titles[k] if visible else ONLINE_TITLE,
            attrs={"IName": f"VCI{vci + n}", "Check": rf"\i{k}=\i\v", "Analog": "true",
                   # valore cosi' com'e', senza conversione (0 -> 0, 100 -> 100)
                   "SourceValLow": None, "DestValLow": None, "SourceValHigh": "100", "DestValHigh": "100",
                   "StatsType": cmd_t.get("StatsType") if visible else None,
                   "StatsAutoDel": cmd_t.get("StatsAutoDel") if visible else None},
            elements={"IoData": {"Visu": "true" if visible else "false", "Pr": room.get("U"),
                                 "Cr": category.get("U"),
                                 "IconV": icon_like(prog, VALUES[k][2]) if visible else None},
                      "Display": {"Unit": unit}})
        cmds.append(cmd_xml)
    online_xml, _, _ = editor.clone_block(online_t, ids, title=f"Stato online {args.nome}", elements={})
    http_xml, http_u, _ = editor.clone_block(
        http_t, ids, title=args.nome, children=[online_xml] + cmds,
        attrs={"IName": f"VHI{next_iname(text, 'VHI')}", "Address": args.indirizzo, "Poll": "60",
               "Allowedtimeouts": "5", "on": "true"})

    text = editor.insert_children(text, container.get("U"), [http_xml])
    text = editor.bump_counters(text, 2 + len(cmds))
    new_xml = text.encode("utf-8")

    new_prog = Program(new_xml)  # il file deve restare XML valido
    new_in = new_prog.by_uuid[http_u]
    assert new_prog.parent[new_in] is not None and new_prog.parent[new_in].get("U") == container.get("U")
    assert len([c for c in new_in if c.tag == "C"]) == 1 + len(cmds), "comandi mancanti nell'ingresso"
    dup = [u for u in ids.used if text.count(f'U="{u}"') > 1]
    if dup:
        raise SystemExit(f"ERRORE: UUID duplicati {dup}")

    out = DATA_DIR / "programmi" / f"{src.stem}-aria-amazon-{datetime.now():%Y%m%d-%H%M}.Loxone"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(new_xml)
    d = diff(prog, new_prog)
    print(f"Partenza: {src.name}\nCopia modificata: {out}")
    print(f"Ingresso '{args.nome}' <- {args.indirizzo} (ogni 60 s), stanza '{room.get('Title')}', "
          f"categoria '{category.get('Title')}'")
    for c in (c for c in new_in if c.tag == "C" and c.get("Type") == "VirtualHttpInCmd"):
        io, disp = c.find("IoData"), c.find("Display")
        print(f"  {c.get('Title')}: {c.get('Check')}  unita' {disp.get('Unit')!r}"
              f"{'' if io.get('Visu') == 'true' else '  (non visibile)'}{'  icona' if io.get('IconV') else ''}")
    print(f"Aggiunti: {len(d['aggiunti'])}  Rimossi: {len(d['rimossi'])}  Modificati: {len(d['modificati'])}")
    for a in d["aggiunti"]:
        print(f"  + {a}")
    for ch in d["modificati"]:
        print(f"  ~ {ch['oggetto']}")
        for note in ch["modifiche"]:
            print(f"      {note}")
    impact = app_impact(prog, new_prog, app_json)
    if impact:
        print(f"Cambia l'app ({len(impact)} controlli nuovi): caricare con Loxone Config.")


if __name__ == "__main__":
    main()
