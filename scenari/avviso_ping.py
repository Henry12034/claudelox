"""Avviso sul telefono quando un dispositivo di rete smette di rispondere (copia di una logica "Ping" gia' presente).

Uso:  python scenari/avviso_ping.py --modello "<titolo del blocco Ping modello>" --indirizzo <IP> --nome "<nome>"
          --testi "<parola>=<nuove parole>" [--ritardo 120] [--file programma.zip|.LoxCC|.Loxone]
      (senza --file usa l'ultima versione scaricata con 'lox programma scarica')

Copia, nella stessa pagina e sotto ai blocchi esistenti, tutta la logica collegata al blocco Ping indicato come
modello: il Ping, i blocchi che contano le cadute e decidono quando avvisare, i riferimenti alle notifiche
(OutputRefLM). Nella copia:
- il Ping controlla il nuovo indirizzo e si chiama "Ping <nome>";
- i testi delle notifiche cambiano con --testi (es. "Telecamera=Telecamera interna": "Telecamera non
  raggiungibile" diventa "Telecamera interna non raggiungibile"); ogni testo deve cambiare, cosi' gli avvisi
  dei due dispositivi non si confondono;
- con --ritardo N l'avviso parte solo se il dispositivo resta irraggiungibile per N secondi: l'impulso che conta
  le cadute (Not(Online) -> OnPulseDelay -> contatore) aspetta N secondi e si azzera quando il dispositivo torna in
  linea (impulso Online -> OnPulseDelay). Le cadute brevi (Wi-Fi, riavvio) non mandano niente, e nemmeno il
  primo controllo dopo un riavvio del Miniserver.
Le notifiche sono le stesse del modello (stessi destinatari). Crea una COPIA in data/programmi/ e stampa le
differenze e gli UUID dei blocchi nuovi (per toglierli: 'lox programma rimuovi' con tutti gli UUID): non invia
nulla al Miniserver. Per caricarla: 'lox carica <copia>' (prova a secco), poi con '--applica --conferma'.
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
from claudelox.program import Program, diff, set_parameter  # noqa: E402


def group_of(prog: Program, start, page) -> list:
    """Blocchi della pagina collegati (direttamente o tramite altri) al blocco di partenza."""
    inside = {e for e in page if e.tag == "C"}
    seen, todo = {start}, [start]
    while todo:
        e = todo.pop()
        near = []
        for co in e.findall("Co"):
            near += [prog.conn_owner[i.get("Input")][0] for i in co.findall("In") if i.get("Input") in prog.conn_owner]
            near += [dst for dst, _ in prog.links_from.get(co.get("U"), [])]
        for n in near:
            if n in inside and n not in seen:
                seen.add(n)
                todo.append(n)
    return [e for e in page if e in seen]       # nell'ordine della pagina


def source(prog: Program, block, key: str):
    """(blocco, connettore) che alimenta l'ingresso `key` del blocco, se ne ha uno solo."""
    co = next((c for c in block.findall("Co") if c.get("K") == key), None)
    links = co.findall("In") if co is not None else []
    return prog.conn_owner.get(links[0].get("Input")) if len(links) == 1 else None


def connector(block, key: str) -> str:
    return next(c.get("U") for c in block.findall("Co") if c.get("K") == key)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--modello", required=True, help="titolo del blocco Ping da copiare (con la sua logica)")
    ap.add_argument("--indirizzo", required=True, help="indirizzo IP o nome del dispositivo da controllare")
    ap.add_argument("--nome", required=True, help="nome del dispositivo (titolo del nuovo Ping)")
    ap.add_argument("--testi", required=True, action="append", metavar="VECCHIO=NUOVO",
                    help="sostituzione nei testi delle notifiche (ripetibile)")
    ap.add_argument("--ritardo", type=int, default=0, help="secondi di irraggiungibilita' prima dell'avviso (0 = subito)")
    ap.add_argument("--file", help="programma di partenza (default: ultimo scaricato)")
    args = ap.parse_args()
    swaps = []
    for item in args.testi:
        old, sep, new = item.partition("=")
        if not sep or not old or not new:
            raise SystemExit(f"ERRORE: --testi '{item}': serve VECCHIO=NUOVO")
        swaps.append((old, new))
    if not re.fullmatch(r"[0-9A-Za-z.\-]+", args.indirizzo):
        raise SystemExit("ERRORE: indirizzo non valido")

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

    ping = prog.block(args.modello)
    if ping.get("Type") != "Ping":
        raise SystemExit(f"ERRORE: '{args.modello}' non e' un blocco Ping")
    if any(e.get("Type") == "Ping" and e.get("Addr") == args.indirizzo for e in prog.components()):
        raise SystemExit(f"Nel programma c'e' gia' un Ping verso {args.indirizzo}: niente da fare.")
    page = prog.parent[ping]
    if page.get("Type") != "Page":
        raise SystemExit("ERRORE: il Ping modello non e' in una pagina di programma")
    group = group_of(prog, ping, page)
    refs = [e for e in group if e.get("Type") == "OutputRefLM" and e.find("LoggerMailer") is not None]
    if not refs:
        raise SystemExit("ERRORE: la logica del modello non manda notifiche (nessun OutputRefLM)")
    texts = {}
    for r in refs:
        old = r.find("LoggerMailer").get("On") or ""
        new = old
        for a, b in swaps:
            new = new.replace(a, b)
        if new == old:
            raise SystemExit(f"ERRORE: il testo '{old}' non cambia con --testi: gli avvisi si confonderebbero")
        texts[r.get("U")] = (old, new)

    # ruoli per il ritardo: impulso che conta le cadute (da Not(Online)) e impulso del ritorno in linea (da Online)
    online = connector(ping, "Online")
    pulse_off = pulse_on = None
    if args.ritardo:
        ons = [e for e in group if e.get("Type") == "OnPulseDelay" and source(prog, e, "InputTrigger")
               and source(prog, e, "InputTrigger")[0].get("U") == ping.get("U")]
        offs = []
        for e in group:
            s = source(prog, e, "InputTrigger") if e.get("Type") == "OnPulseDelay" else None
            if s and s[0].get("Type") == "Not" and (source(prog, s[0], "I") or (None,))[0] is ping:
                offs.append(e)
        if len(ons) != 1 or len(offs) != 1:
            raise SystemExit("ERRORE: schema del modello diverso dal previsto (serve un impulso da Online e uno da "
                             "Not(Online)): usa --ritardo 0 oppure prepara la logica in Config")
        pulse_on, pulse_off = ons[0], offs[0]
        reset_co = next((c for c in pulse_off.findall("Co") if c.get("K") == "Reset"), None)
        if reset_co is None or reset_co.findall("In"):
            raise SystemExit("ERRORE: l'impulso delle cadute non ha un Reset libero")
    outside = [e for e in group for co in e.findall("Co") for i in co.findall("In")
               if prog.conn_owner.get(i.get("Input"), (None,))[0] not in group]
    if outside:
        raise SystemExit("ERRORE: la logica del modello riceve segnali da fuori: non la copio")

    ids = editor.UuidFactory(text)
    x, y = editor.free_area_below(page)
    x0 = min(int(e.get("Px")) for e in group if e.get("Px"))
    y0 = min(int(e.get("Py")) for e in group if e.get("Py"))
    blocks, mapping = editor.clone_group(text, [e.get("U") for e in group], ids, offset=(x - x0, y - y0))
    text = editor.insert_children(text, page.get("U"), blocks)
    text = editor.bump_counters(text, len(blocks))
    new_ping = mapping[ping.get("U")]
    text = editor.set_block_attr(text, new_ping, {"Title": f"Ping {args.nome}", "Addr": args.indirizzo})
    for u, (old, new) in texts.items():
        text = editor.set_child_attr(text, mapping[u], "LoggerMailer", {"On": new})
        desc = prog.by_uuid[u].get("Desc")
        if desc:
            for a, b in swaps:
                desc = desc.replace(a, b)
            text = editor.set_block_attr(text, mapping[u], {"Desc": desc})
    if args.ritardo:
        text = set_parameter(text.encode("utf-8"), mapping[connector(pulse_off, "Delay")],
                             str(args.ritardo)).decode("utf-8")
        text = editor.add_input(text, mapping[connector(pulse_off, "Reset")], mapping[connector(pulse_on, "Q")])
    new_xml = text.encode("utf-8")

    # controlli: XML valido, copie nella pagina, stessi collegamenti del modello (piu' il Reset del ritardo)
    new_prog = Program(new_xml)
    copies = {new_prog.by_uuid[mapping[e.get("U")]] for e in group}
    for e in group:
        c = new_prog.by_uuid[mapping[e.get("U")]]
        assert new_prog.parent[c].get("U") == page.get("U"), "blocco finito fuori dalla pagina"
        n_old = sum(len(co.findall("In")) for co in e.findall("Co"))
        n_new = sum(len(co.findall("In")) for co in c.findall("Co"))
        assert n_new == n_old + (1 if args.ritardo and e is pulse_off else 0), f"collegamenti diversi in {e.get('U')}"
        assert all(new_prog.conn_owner[i.get("Input")][0] in copies
                   for co in c.findall("Co") for i in co.findall("In")), "collegamento verso il modello"
    assert new_prog.by_uuid[new_ping].get("Addr") == args.indirizzo
    d = diff(prog, new_prog)
    changed = [ch for ch in d["modificati"] if not all(n.startswith("NumO:") for n in ch["modifiche"])]
    if d["rimossi"] or changed:                        # solo aggiunte (e il contatore di oggetti): il resto com'era
        raise SystemExit(f"ERRORE: modifiche inattese: {d['rimossi']} {changed}")
    dup = editor.duplicate_ids(text)
    if dup:
        raise SystemExit(f"ERRORE: UUID duplicati {dup}")

    slug = re.sub(r"[^0-9a-z]+", "-", args.nome.lower()).strip("-")
    out = DATA_DIR / "programmi" / f"{src.stem}-avviso-{slug}-{datetime.now():%Y%m%d-%H%M}.Loxone"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(new_xml)
    print(f"Partenza: {src.name}\nCopia modificata: {out}")
    print(f"Ping '{args.nome}' verso {args.indirizzo}, pagina '{page.get('Title')}', {len(blocks)} blocchi copiati da "
          f"'{prog.title(ping)}'" + (f", avviso dopo {args.ritardo} s" if args.ritardo else ", avviso subito"))
    for old, new in texts.values():
        print(f"  notifica: '{old}' -> '{new}'")
    print(f"Aggiunti: {len(d['aggiunti'])}  Rimossi: 0  Modificati: 0")
    for a in d["aggiunti"]:
        print(f"  + {a}")
    print("UUID dei blocchi nuovi: " + " ".join(mapping[e.get("U")] for e in group))


if __name__ == "__main__":
    main()
