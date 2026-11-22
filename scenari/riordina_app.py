"""Riordino dell'app Loxone da un piano: controlli da nascondere, spostare, rinominare, mettere tra i preferiti.

Uso:  python scenari/riordina_app.py --piano data/riordino-app.json [--file programma.zip|.LoxCC|.Loxone]
      (senza --file usa l'ultima versione scaricata con 'lox programma scarica')

Il piano e' un file JSON con i dati dell'impianto (tenerlo in data/, escluso da git). I controlli si indicano per
UUID (i nomi si ripetono spesso: "Status", "Valori meteo"...), il resto e' facoltativo:
  {
    "nascondi":   ["<uuid>", ...],                                   tolti dall'app (la logica non cambia)
    "sposta":     {"<uuid>": {"stanza": "Generale", "categoria": "Sensori"}},
    "preferiti":  {"<uuid>": true, "<uuid>": false},                 nella pagina dei preferiti dell'app
    "importanza": {"<uuid>": 9},                                     ordine nell'app (0-9, prima i piu' alti)
    "rinomina":   {"<uuid>": "Nuovo nome"},                          nome nell'app (descrizione, se il blocco
                                                                     ne ha una, altrimenti titolo)
    "note":       {"<uuid>": "promemoria per chi legge il piano"}
  }
Cambia solo l'elemento IoData dei blocchi (Visu, UseInFav, Pr, Cr, Rating) e il loro nome: niente collegamenti.
Crea una COPIA in data/programmi/ e stampa le differenze: non invia nulla al Miniserver. Le modifiche cambiano
l'app: 'lox carica' aggiorna anche LoxAPP3.json e permissions.bin (prova a secco prima di --applica).
Per rimettere un controllo nascosto serve Loxone Config (o un controllo dello stesso tipo ancora nell'app).
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from claudelox import backups, editor  # noqa: E402
from claudelox.config import DATA_DIR, get_settings  # noqa: E402
from claudelox.loxcc import read_program  # noqa: E402
from claudelox.program import Program, diff  # noqa: E402
from claudelox.structure import norm  # noqa: E402

KEYS = {"nascondi", "sposta", "preferiti", "importanza", "rinomina", "note"}


def in_app(e) -> bool:
    io = e.find("IoData")
    return io is not None and (io.get("Visu") == "true" or io.get("UseInFav") == "true")


def by_title(prog: Program, type_: str, title: str) -> str:
    hits = [e for e in prog.components() if e.get("Type") == type_ and norm(e.get("Title")) == norm(title)]
    if len(hits) != 1:
        names = ", ".join(sorted(e.get("Title") for e in prog.components() if e.get("Type") == type_))
        raise SystemExit(f"ERRORE: {type_} '{title}' non trovato o ambiguo. Disponibili: {names}")
    return hits[0].get("U")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--piano", required=True, help="piano JSON (vedi sopra)")
    ap.add_argument("--file", help="programma di partenza (default: ultimo scaricato)")
    args = ap.parse_args()
    plan = json.loads(Path(args.piano).read_text(encoding="utf-8"))
    unknown = set(plan) - KEYS
    if unknown:
        raise SystemExit(f"ERRORE: voci sconosciute nel piano: {sorted(unknown)} (possibili: {sorted(KEYS)})")

    if args.file:
        src = Path(args.file)
    else:
        p = backups.newest_program(get_settings().backups_dir, DATA_DIR / "programmi", get_settings().host)
        if not p:
            raise SystemExit("ERRORE: nessun programma locale, esegui prima 'lox programma scarica'")
        src = p["path"]
    xml, _ = read_program(src)
    text = xml.decode("utf-8")
    prog = Program(xml)

    def block(u: str):
        e = prog.by_uuid.get(u)
        if e is None:
            raise SystemExit(f"ERRORE: nessun blocco con UUID {u} nel programma")
        if e.find("IoData") is None:
            raise SystemExit(f"ERRORE: '{prog.title(e)}' ({u}) non ha impostazioni per l'app (IoData)")
        return e

    def name(e) -> str:
        return e.get("Desc") or prog.title(e)

    changes: dict[str, dict[str, str | None]] = {}    # uuid -> modifiche di IoData
    report: list[str] = []
    hidden = set(plan.get("nascondi", []))
    for u in plan.get("nascondi", []):
        e = block(u)
        if not in_app(e):
            raise SystemExit(f"ERRORE: '{name(e)}' ({u}) non e' nell'app: niente da nascondere")
        changes.setdefault(u, {}).update(Visu=None, UseInFav=None)
        report.append(f"nascosto: {name(e)} <{e.get('Type')}> ({prog.path(e).split(' > ')[-1]})")
    for section in ("sposta", "preferiti", "importanza", "rinomina"):
        clash = hidden & set(plan.get(section, {}))
        if clash:
            raise SystemExit(f"ERRORE: '{section}' riguarda controlli che il piano nasconde: {sorted(clash)}")
    for u, where in plan.get("sposta", {}).items():
        e = block(u)
        io = changes.setdefault(u, {})
        if "stanza" in where:
            io["Pr"] = by_title(prog, "Place", where["stanza"])
        if "categoria" in where:
            io["Cr"] = by_title(prog, "Category", where["categoria"])
        report.append(f"spostato: {name(e)} -> " + ", ".join(f"{k} {v}" for k, v in where.items()))
    for u, fav in plan.get("preferiti", {}).items():
        e = block(u)
        changes.setdefault(u, {})["UseInFav"] = "true" if fav else None
        report.append(f"{'tra i preferiti' if fav else 'tolto dai preferiti'}: {name(e)}")
    for u, rating in plan.get("importanza", {}).items():
        e = block(u)
        if not 0 <= int(rating) <= 9:
            raise SystemExit(f"ERRORE: importanza di '{name(e)}' fuori da 0-9")
        changes.setdefault(u, {})["Rating"] = str(int(rating)) if int(rating) else None
        report.append(f"importanza {int(rating)}: {name(e)}")
    for u, change in changes.items():
        text = editor.set_iodata(text, u, change)
    for u, new_name in plan.get("rinomina", {}).items():
        e = block(u)
        text = editor.set_block_attr(text, u, {"Desc" if e.get("Desc") else "Title": new_name})
        report.append(f"rinominato: {name(e)} -> {new_name}")
    new_xml = text.encode("utf-8")
    new_prog = Program(new_xml)       # il file deve restare XML valido
    still = [u for u in hidden if in_app(new_prog.by_uuid[u])]
    assert not still, f"controlli ancora visibili: {still}"
    d = diff(prog, new_prog)
    if d["aggiunti"] or d["rimossi"]:
        raise SystemExit("ERRORE: il riordino non deve aggiungere o togliere blocchi")

    out = DATA_DIR / "programmi" / f"{src.stem}-riordino-{datetime.now():%Y%m%d-%H%M}.Loxone"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(new_xml)
    print(f"Partenza: {src.name}\nCopia modificata: {out}")
    print(f"Nascosti {len(hidden)} controlli, altre modifiche dell'app su {len(set(changes) - hidden)} controlli, "
          f"{len(plan.get('rinomina', {}))} rinominati")
    for line in report:
        print(f"  {line}")
    print(f"Blocchi modificati: {len(d['modificati'])} (solo impostazioni dell'app e nomi, nessun collegamento)")


if __name__ == "__main__":
    main()
