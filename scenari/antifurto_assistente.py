"""Antifurto con la conferma di un assistente esterno (per esempio un assistente vocale sul Raspberry che guarda
telecamere, rumori e presenze): l'allarme del Miniserver scatta quando l'assistente lo conferma; se l'assistente non
risponde, scatta lo stesso dopo un ritardo di riserva.

Uso:  python scenari/antifurto_assistente.py [--assistente Assistente] [--antifurto "Allarme Antifurto"]
          [--ritardo 60] [--conferma <nome>_conferma] [--annulla <nome>_annulla] [--file programma.zip|.LoxCC|.Loxone]
      (senza --file usa l'ultima versione scaricata con 'lox programma scarica')

Cosa cambia (tutto nella pagina dell'antifurto, nessun controllo nuovo nell'app):
  - la presenza (gli ingressi "Presenza" dell'antifurto, di solito due stanze insieme) non fa piu' scattare l'allarme
    subito: passa da un ritardo "Presenza da verificare" (--ritardo, 60 s), che l'assistente annulla se e' un falso
    allarme e che il disinserimento annulla sempre;
  - i sensori di presenza scelti nella finestra di configurazione del blocco (collegamenti nascosti, ingresso HI1 nel
    file) facevano scattare l'allarme senza ritardo: si tolgono dal blocco (collegamento e voce in "Objects", cosi'
    Loxone Config non li rimette). Li controlla l'assistente; la presenza in due stanze resta all'antifurto;
  - il ritardo d'ingresso della porta (l'impulso ritardato sull'ingresso "Porta" dell'antifurto) si annulla anche con
    il segnale dell'assistente;
  - due ingressi virtuali di testo, come quelli dei segnali che arrivano gia' da fuori (ingresso virtuale di testo ->
    pulsante, copiati da uno esistente):
    --conferma -> pulsante "<assistente> conferma allarme" -> ingresso "Altro" dell'antifurto: allarme subito;
    --annulla  -> pulsante "<assistente> annulla allarme" -> annulla i ritardi della presenza e della porta.
    L'assistente li preme con jdev/sps/io/<nome>/SET(Sw;On;Pulse) (comando dei "Loxone API Commands" per l'ingresso
    API del pulsante, lo stesso del ponte delle telecamere).
Se l'assistente e' spento, la presenza fa scattare l'allarme dopo il ritardo e la porta dopo il suo ritardo di sempre.
Gli altri ingressi diretti (finestre, vetri, sabotaggio di un dispositivo) restano come sono: lo script li elenca.

Crea una COPIA modificata in data/programmi/ e stampa le differenze: non invia nulla al Miniserver.
Per caricarla: 'lox carica <copia>' (prova a secco), poi con '--applica --conferma', ad antifurto disinserito.
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

TITOLO_RITARDO = "Presenza da verificare"
# ingressi del blocco Alarm nel file (in ordine come nella documentazione: Presenza, Vetri, Finestre, Porte, Altro);
# HI1-HI4 sono gli stessi tipi per i sensori scelti nella finestra di configurazione (collegamenti nascosti)
INGRESSI = {"I1": "Presenza", "I2": "Vetri", "I3": "Finestre", "I4": "Porte", "I5": "Altro",
            "HI1": "Presenza (configurazione)", "HI2": "Vetri (configurazione)", "HI3": "Finestre (configurazione)",
            "HI4": "Porte (configurazione)"}


def fonti(prog: Program, blocco, connettore: str) -> list[tuple]:
    """I blocchi (con il connettore di uscita) collegati a un ingresso: [(blocco, chiave, uuid_connettore)]."""
    co = next((c for c in blocco.findall("Co") if c.get("K") == connettore), None)
    if co is None:
        raise SystemExit(f"ERRORE: '{blocco.get('Title')}' non ha il connettore {connettore}")
    out = []
    for i in co.findall("In"):
        owner = prog.conn_owner.get(i.get("Input"))
        if owner:
            out.append((owner[0], owner[1], i.get("Input")))
    return out


def connettore(blocco, chiave: str) -> str:
    co = next((c for c in blocco.findall("Co") if c.get("K") == chiave), None)
    if co is None:
        raise SystemExit(f"ERRORE: '{blocco.get('Title')}' non ha il connettore {chiave}")
    return co.get("U")


def togli_oggetti(text: str, blocco: str, via: set[str]) -> str:
    """Toglie degli oggetti dalla configurazione di un blocco (attributo Objects del suo tag)."""
    m = re.search(r'<C\b[^>]*\sU="%s"[^>]*>' % re.escape(blocco), text)
    o = re.search(r'\sObjects="([^"]*)"', m.group(0)) if m else None
    if not o:
        return text
    resto = [u for u in o.group(1).split(",") if u and u not in via]
    tag = m.group(0).replace(o.group(0), f' Objects="{",".join(resto)}"' if resto else "", 1)
    return text[:m.start()] + tag + text[m.end():]


def descrivi(prog: Program, blocco) -> str:
    """'Titolo <Tipo>', con il tipo dell'oggetto a cui punta un riferimento (InputRef)."""
    ref = prog.by_uuid.get(blocco.get("Ref") or "")
    tipo = ref.get("Type") if blocco.get("Type") == "InputRef" and ref is not None else blocco.get("Type")
    return f"{blocco.get('Title')} <{tipo}>"


def modelli(prog: Program):
    """Un ingresso virtuale di testo che comanda un pulsante (VirtualTextIn -> InputRef -> PushButton.OutputAPI): i
    nuovi segnali si fanno uguali. Meglio un pulsante che non si vede nell'app (i nuovi non si vedono)."""
    trovati = []
    for vti in (e for e in prog.components() if e.get("Type") == "VirtualTextIn"):
        for ref in (e for e in prog.components() if e.get("Type") == "InputRef" and e.get("Ref") == vti.get("U")):
            for dest, k in prog.links_from.get(connettore(ref, "AQ"), []):
                if dest.get("Type") == "PushButton" and k == "OutputAPI":
                    io = dest.find("IoData")
                    trovati.append((io is not None and io.get("Visu") == "true", vti, ref, dest))
    if not trovati:
        raise SystemExit("ERRORE: manca un modello (ingresso virtuale di testo che comanda un pulsante)")
    _, vti, ref, dest = min(trovati, key=lambda t: t[0])
    return vti, ref, dest


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--assistente", default="Assistente", help="nome dell'assistente nei titoli dei pulsanti")
    ap.add_argument("--antifurto", default="Allarme Antifurto", help="titolo del blocco antifurto")
    ap.add_argument("--ritardo", type=int, default=60, help="secondi di riserva per la presenza (default 60)")
    ap.add_argument("--conferma", help="nome dell'ingresso virtuale che conferma (default <assistente>_conferma)")
    ap.add_argument("--annulla", help="nome dell'ingresso virtuale che annulla (default <assistente>_annulla)")
    ap.add_argument("--file", help="programma di partenza (default: ultimo scaricato)")
    args = ap.parse_args()
    if not 10 <= args.ritardo <= 600:
        raise SystemExit("ERRORE: il ritardo va da 10 a 600 secondi")
    base = re.sub(r"\W+", "_", args.assistente.strip().lower()).strip("_") or "assistente"
    args.conferma, args.annulla = args.conferma or f"{base}_conferma", args.annulla or f"{base}_annulla"
    titolo_conferma = f"{args.assistente.strip()} conferma allarme"
    titolo_annulla = f"{args.assistente.strip()} annulla allarme"
    for nome in (args.conferma, args.annulla):
        if not re.fullmatch(r"[A-Za-z0-9_]+", nome):
            raise SystemExit(f"ERRORE: nome '{nome}' non valido (solo lettere, numeri e _: va nell'indirizzo)")

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
    if any(e.get("Title") in (titolo_conferma, titolo_annulla, TITOLO_RITARDO) for e in prog.components()):
        raise SystemExit("La modifica e' gia' presente in questo programma.")
    if any(e.get("Type") == "VirtualTextIn" and e.get("Title") in (args.conferma, args.annulla)
           for e in prog.components()):
        raise SystemExit("Nel programma c'e' gia' un ingresso virtuale con quel nome.")

    allarme = [e for e in prog.components() if e.get("Type") == "Alarm" and e.get("Title") == args.antifurto]
    if len(allarme) != 1:
        raise SystemExit(f"ERRORE: blocco antifurto '{args.antifurto}' non trovato (o piu' d'uno)")
    allarme = allarme[0]
    pagina = prog.parent[allarme]
    presenze = fonti(prog, allarme, "I1")          # ingresso "Presenza" (P)
    nascoste = fonti(prog, allarme, "HI1")         # presenze scelte nella configurazione del blocco: senza ritardo
    porte = fonti(prog, allarme, "I4")             # ingresso "Porte" (Dc): il ritardo d'ingresso
    if not presenze:
        raise SystemExit("ERRORE: all'ingresso Presenza dell'antifurto non e' collegato niente")
    if len(porte) != 1 or porte[0][0].get("Type") != "OnPulseDelay":
        raise SystemExit("ERRORE: l'ingresso Porte dell'antifurto non arriva da un solo ritardo (OnPulseDelay)")
    ritardo_porta = porte[0][0]
    disinserito = fonti(prog, ritardo_porta, "Reset")   # l'impulso al disinserimento, gia' usato dalla porta
    if len(disinserito) != 1:
        raise SystemExit("ERRORE: il ritardo della porta non ha un solo azzeramento (il disinserimento)")
    if fonti(prog, allarme, "I5"):
        raise SystemExit("ERRORE: l'ingresso Altro (I5) dell'antifurto e' gia' usato")
    vti_t, ref_t, puls_t = modelli(prog)
    contenitore = prog.parent[vti_t]

    ids = editor.UuidFactory(text)
    vti_n = max((int(n) for n in re.findall(r'\bIName="VTI(\d+)"', text)), default=0) + 1
    x, y = editor.free_area_below(pagina)
    nuovi_vti, nuovi_pagina, uscite = [], [], {}
    for k, (nome, titolo) in enumerate(((args.conferma, titolo_conferma), (args.annulla, titolo_annulla))):
        vti_xml, vti_u, vti_co = editor.clone_block(vti_t, ids, title=nome, attrs={"IName": f"VTI{vti_n + k}"},
                                                     elements={"IoData": {"Pr": None, "Cr": None, "Visu": None}})
        ref_xml, _, ref_co = editor.clone_block(ref_t, ids, title=nome, position=(x, y + k * 1440),
                                                attrs={"Ref": vti_u}, overrides={"AI": {"inputs": [vti_co["TQ"]]}})
        # gli stati del pulsante (SpStates) hanno UUID propri: nuovi, non quelli del modello
        stati = ",".join(ids.block() for _ in (puls_t.get("SpStates") or "").split(",") if _) or None
        puls_xml, _, puls_co = editor.clone_block(
            puls_t, ids, title=titolo, position=(x + 2688, y + k * 1440 - 192), attrs={"SpStates": stati},
            overrides={"OutputAPI": {"inputs": [ref_co["AQ"]]}, "Reset": {"inputs": []},
                       "InputTrigger": {"inputs": []}, "On": {"inputs": []}},
            elements={"IoData": {"Visu": None, "Pr": None}})
        nuovi_vti.append(vti_xml)
        nuovi_pagina += [ref_xml, puls_xml]
        uscite[titolo] = puls_co["Q"]
    rit_xml, _, rit_co = editor.clone_block(
        ritardo_porta, ids, title=TITOLO_RITARDO, position=(x + 6144, y),
        overrides={"InputTrigger": {"inputs": [u for _, _, u in presenze], "Inv": False},
                   "Reset": {"inputs": [disinserito[0][2], uscite[titolo_annulla]]},
                   "Delay": {"Def": str(args.ritardo)}, "Time": {"Def": "1"}})
    nuovi_pagina.append(rit_xml)

    text = editor.insert_children(text, contenitore.get("U"), nuovi_vti)
    text = editor.insert_children(text, pagina.get("U"), nuovi_pagina)
    i1, i5 = connettore(allarme, "I1"), connettore(allarme, "I5")
    for _, _, u in presenze:                       # la presenza ora passa dal ritardo
        text = editor.remove_input(text, i1, u)
    text = editor.add_input(text, i1, rit_co["Q"])
    text = editor.add_input(text, i5, uscite[titolo_conferma])
    text = editor.add_input(text, connettore(ritardo_porta, "Reset"), uscite[titolo_annulla])
    text = editor.bump_counters(text, len(nuovi_vti) + len(nuovi_pagina))
    # le presenze nascoste: via il collegamento, la voce della configurazione (se no Config li ricrea) e il
    # riferimento che la configurazione aveva messo nella pagina, se non serve ad altro
    hi1 = connettore(allarme, "HI1")
    for _, _, u in nascoste:
        text = editor.remove_input(text, hi1, u)
    sensori = {o.get("Ref") for o, _, _ in nascoste if o.get("Type") == "InputRef"}
    text = togli_oggetti(text, allarme.get("U"), sensori)
    riferimenti = [o.get("U") for o, _, u in nascoste
                   if o.get("Type") == "InputRef" and len(prog.links_from.get(u, [])) == 1]
    if riferimenti:
        text, _ = editor.remove_blocks(text, riferimenti)
    new_xml = text.encode("utf-8")

    new_prog = Program(new_xml)  # il file deve restare XML valido
    nuovo_allarme = new_prog.by_uuid[allarme.get("U")]
    assert [o.get("Title") for o, _, _ in fonti(new_prog, nuovo_allarme, "I1")] == [TITOLO_RITARDO]
    assert [o.get("Title") for o, _, _ in fonti(new_prog, nuovo_allarme, "I5")] == [titolo_conferma]
    assert not fonti(new_prog, nuovo_allarme, "HI1"), "presenze nascoste ancora collegate all'antifurto"
    assert not sensori & set((nuovo_allarme.get("Objects") or "").split(",")), "presenze ancora nella configurazione"
    for e in new_prog.components():
        if e.get("Title") in (titolo_conferma, titolo_annulla, TITOLO_RITARDO):
            assert new_prog.parent[e].get("U") == pagina.get("U"), "blocco finito fuori dalla pagina"
    dup = [u for u in ids.used if text.count(f'U="{u}"') > 1]
    if dup:
        raise SystemExit(f"ERRORE: UUID duplicati {dup}")

    out = DATA_DIR / "programmi" / f"{src.stem}-antifurto-{base}-{datetime.now():%Y%m%d-%H%M}.Loxone"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(new_xml)
    d = diff(prog, new_prog)
    print(f"Partenza: {src.name}\nCopia modificata: {out}")
    print(f"Presenza ({', '.join(o.get('Title') for o, _, _ in presenze)}) -> '{TITOLO_RITARDO}' "
          f"({args.ritardo} s) -> antifurto; '{ritardo_porta.get('Title')}' annullabile; "
          f"segnali: {args.conferma} (conferma), {args.annulla} (annulla)")
    if nascoste:
        print("Tolte dall'antifurto (scattavano senza ritardo): "
              + ", ".join(descrivi(prog, o) for o, _, _ in nascoste))
    diretti = [(k, o) for k in ("I2", "I3", "HI2", "HI3", "HI4") for o, _, _ in fonti(new_prog, nuovo_allarme, k)]
    if diretti:
        print("Restano senza ritardo: " + "; ".join(f"{INGRESSI[k]}: {descrivi(new_prog, o)}" for k, o in diretti))
    print(f"Aggiunti: {len(d['aggiunti'])}  Rimossi: {len(d['rimossi'])}  Modificati: {len(d['modificati'])}")
    for a in d["aggiunti"]:
        print(f"  + {a}")
    for r in d["rimossi"]:
        print(f"  - {r}")
    for ch in d["modificati"]:
        print(f"  ~ {ch['oggetto']}")
        for note in ch["modifiche"]:
            print(f"      {note}")
    impact = app_impact(prog, new_prog, app_json)
    print("Effetti sull'app: " + ("; ".join(impact) if impact else "nessuno (blocchi non visibili)"))


if __name__ == "__main__":
    main()
