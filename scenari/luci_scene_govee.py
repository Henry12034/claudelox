"""Scene Govee (anche animate) come atmosfere di un controllo luci Loxone (ponte integrazioni/govee-bluetooth).

Uso:  python scenari/luci_scene_govee.py --blocco "Luci camera" --uscita "Manda UDP led"
          --scene "Alba=Sunrise,Aurora=Aurora,Fuoco=Fire" [--luminosita 70]
          [--circuiti "AI1=Luminosita',AI2=Colore"] [--file programma.zip|.LoxCC|.Loxone]
      (senza --file usa l'ultima versione scaricata con 'lox programma scarica')

Per ogni scena aggiunge un'atmosfera al controllo luci (LightController2): i circuiti dimmer alla luminosita'
indicata, i circuiti colore spenti (nero). Un blocco di stato traduce il numero dell'atmosfera attiva (uscita
Scene) nel nome della scena Govee e un comando dell'uscita virtuale UDP verso il ponte lo manda (testo <v>):
scegliendo l'atmosfera, il ponte riceve "brightness:70", "color:0-0-0" e il nome della scena insieme e mostra la
scena (come gia' fa la scena "Snowflake" con un blocco "uguale"). I nomi delle scene sono quelli del catalogo del
ponte ('loxone_govee.py scene'); le scene classiche (Sunrise, Movie...) funzionano anche senza catalogo.
Con --circuiti rinomina i circuiti del controllo (nell'app: i sotto-controlli, es. "Scena luce RGB" -> "Colore").

Crea una COPIA in data/programmi/ e stampa le differenze: non invia nulla al Miniserver. Le atmosfere non sono in
LoxAPP3.json (l'app le chiede al Miniserver), i nomi dei circuiti si': li riporta 'lox carica'.
Per togliere le scene: 'lox programma rimuovi' del blocco di stato, del riferimento e del comando "Scena Govee"
(le atmosfere restano e si possono cancellare dall'app o da Loxone Config).
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
from claudelox.program import Program, diff  # noqa: E402
from claudelox.structure import norm  # noqa: E402

TITLE = "Scena Govee"
TYPE_SHIFT = 28                      # valore di un circuito in un'atmosfera: tipo << 28 | valore
DIMMER, RGB = 1, 4                   # tipi osservati: 1 = dimmer (0-100), 4 = RGB (BBBGGGRRR, 0 = nero)
SPECIAL_MOODS = {777, 778}           # "tutto acceso" e "Off": non contano per i numeri nuovi
NO_ICON = "00000000-0000-0000-0000000000000000"


def pick(prog: Program, type_: str, ref: str):
    if ref in prog.by_uuid and prog.by_uuid[ref].get("Type") == type_:
        return prog.by_uuid[ref]
    hits = [e for e in prog.components() if e.get("Type") == type_ and norm(prog.title(e)) == norm(ref)]
    if len(hits) != 1:
        names = ", ".join(sorted(prog.title(e) for e in prog.components() if e.get("Type") == type_))
        raise SystemExit(f"ERRORE: {type_} '{ref}' non trovato o ambiguo. Disponibili: {names}")
    return hits[0]


def parse_pairs(text: str, what: str) -> list[tuple[str, str]]:
    out = []
    for pair in filter(None, (p.strip() for p in text.split(","))):
        a, sep, b = pair.partition("=")
        if not sep or not a.strip() or not b.strip():
            raise SystemExit(f"ERRORE: {what} non valido '{pair}' (forma: nome=valore)")
        out.append((a.strip(), b.strip()))
    return out


def connector(block, key: str) -> str:
    co = next((c for c in block.findall("Co") if c.get("K") == key), None)
    if co is None:
        raise SystemExit(f"ERRORE: il blocco {block.get('Title')} non ha il connettore {key}")
    return co.get("U")


def mood_values(off_mood, outputs: int, brightness: int) -> list[str]:
    """Valori dei circuiti per un'atmosfera "scena": dimmer alla luminosita' data, colori neri, il resto come Off."""
    values = []
    for n in range(1, outputs + 1):
        q = int(off_mood.get(f"Q{n}") or 0)
        kind = q >> TYPE_SHIFT
        if kind == DIMMER:
            values.append(str(DIMMER << TYPE_SHIFT | brightness))
        elif kind == RGB:
            values.append(str(RGB << TYPE_SHIFT))
        else:
            values.append(str(q))
    return values


def add_moods(text: str, lc_uuid: str, lines: list[str]) -> str:
    """Atmosfere nuove in fondo a <LightscenesC> del controllo luci, con il contatore Num aggiornato."""
    start, end = editor.element_span(text, lc_uuid)
    m = re.compile(r"<LightscenesC\b[^>]*>").search(text, start, end)
    close = text.find("</LightscenesC>", m.end() if m else start, end)
    if not m or close < 0:
        raise SystemExit("ERRORE: atmosfere del controllo luci non trovate nel file")
    num = len(re.findall(r"<LightsceneC\b", text[m.end():close])) + len(lines)
    head = re.sub(r'(\sNum=")\d+(")', rf"\g<1>{num}\g<2>", m.group(0), count=1)
    line_start = text.rfind("\n", 0, close) + 1
    indent = text[line_start:close]
    nl = editor.newline_of(text)
    block = "".join(f"{indent}\t{line}{nl}" for line in lines)
    return text[:m.start()] + head + text[m.end():line_start] + block + text[line_start:]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--blocco", required=True, help="controllo luci (LightController2): titolo o UUID")
    ap.add_argument("--uscita", required=True, help="uscita virtuale UDP verso il ponte Govee: titolo o UUID")
    ap.add_argument("--scene", required=True, help="atmosfere da creare: 'Nome nell'app=nome scena Govee,...'")
    ap.add_argument("--luminosita", type=int, default=70, help="luminosita' dei circuiti dimmer nelle scene (70)")
    ap.add_argument("--circuiti", default="", help="nuovi nomi dei circuiti, es. 'AI1=Luminosita,AI2=Colore'")
    ap.add_argument("--file", help="programma di partenza (default: ultimo scaricato)")
    args = ap.parse_args()
    if not 1 <= args.luminosita <= 100:
        raise SystemExit("ERRORE: --luminosita va da 1 a 100")
    scenes = parse_pairs(args.scene, "scena")
    circuits = parse_pairs(args.circuiti, "circuito")

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
    lc = pick(prog, "LightController2", args.blocco)
    vout = pick(prog, "VirtualOut", args.uscita)
    if not (vout.get("Address") or "").startswith("/dev/udp/"):
        raise SystemExit(f"ERRORE: l'uscita '{vout.get('Title')}' non e' un'uscita UDP")
    page = prog.parent[lc]
    if page.get("Type") != "Page":
        raise SystemExit("ERRORE: il controllo luci non sta in una pagina del programma")
    moods = list(lc.iter("LightsceneC"))
    taken = {norm(m.get("Name")) for m in moods}
    clash = [name for name, _ in scenes if norm(name) in taken]
    if clash:
        raise SystemExit(f"ERRORE: il controllo ha gia' atmosfere chiamate {clash}")
    off = next((m for m in moods if m.get("SID") == "778"), None)
    if off is None:
        raise SystemExit("ERRORE: manca l'atmosfera 'Off' (778) da usare come riferimento dei circuiti")
    if any(e.get("Type") == "VirtualOutCmd" and e.get("Title") == TITLE for e in vout):
        raise SystemExit(f"ERRORE: l'uscita ha gia' un comando '{TITLE}': le scene sono gia' collegate")
    outputs = int(off.get("Outputs") or 0)
    names_attr = {f"AI{n + 1}": f"NameAI{n}" for n in range(outputs)}
    for key, _ in circuits:
        if key not in names_attr or lc.get(names_attr[key]) is None:
            raise SystemExit(f"ERRORE: circuito {key} inesistente nel controllo (circuiti: "
                             f"{', '.join(k for k, a in names_attr.items() if lc.get(a) is not None)})")

    ids = editor.UuidFactory(text)
    sid = max((int(m.get("SID")) for m in moods if int(m.get("SID")) not in SPECIAL_MOODS), default=0) + 1
    cid = max((int(m.get("CID") or 0) for m in moods), default=0) + 1
    values = mood_values(off, outputs, args.luminosita)
    mood_lines, rules = [], []
    for n, (name, scene) in enumerate(scenes):
        q = " ".join(f'Q{i + 1}="{v}"' for i, v in enumerate(values))
        mood_lines.append(f'<LightsceneC Name="{editor.escape_attr(name)}" UUID="{ids.block()}" '
                          f'Outputs="{outputs}" SID="{sid + n}" CID="{cid + n}" {q}/>')
        rules.append((sid + n, scene))

    # blocco di stato: numero dell'atmosfera (uscita Scene) -> nome della scena Govee
    state_u = ids.block()
    x, y = editor.free_area_below(page)
    co = [("I1", [connector(lc, "Scene")])] + [(f"I{i}", []) for i in range(2, 9)] + \
        [("TQ", []), ("AQ", []), ("OutputAPI", [])]
    state_co = {}
    lines = [f'<C Type="State" V="178" U="{state_u}" Title="{TITLE}" Px="{x}" Py="{y}" Px2="{x + 2688}" '
             f'Py2="{y + 888}" Cl="141,255,112" Nio="11" WF="16384">']
    for i, (k, inputs) in enumerate(co):
        cu = ids.connector(state_u, i)
        state_co[k] = cu
        if inputs:
            lines += [f'\t<Co K="{k}" Nc="{len(inputs)}" U="{cu}">'] + \
                     [f'\t\t<In Input="{u}"/>' for u in inputs] + ["\t</Co>"]
        else:
            lines.append(f'\t<Co K="{k}" U="{cu}"/>')
    lines += ["\t<IoData/>", '\t<Display Unit="&lt;v.pr&gt;"/>', f'\t<StateTexts Num="{len(rules) + 1}">']
    for number, scene in rules:
        lines.append(f'\t\t<StateText Valid="true" ValidV="true" Text="{editor.escape_attr(scene)}" TextV="0" '
                     f'Icon="{NO_ICON}" IcC="7405453" Input0="1" CondV0="{number}" CondT0="{number}"/>')
    lines += [f'\t\t<StateText Valid="true" ValidV="true" Text="" TextV="0" Icon="{NO_ICON}" IcC="7405453"/>',
              "\t</StateTexts>", "</C>"]
    state_xml = "\n".join(lines)

    # comando dell'uscita virtuale (testo <v>) e riferimento in pagina che gli porta il testo del blocco di stato
    cmd_u, ref_u = ids.block(), ids.block()
    ref_ai, ref_aq = ids.connector(ref_u, 0), ids.connector(ref_u, 1)
    cmd_i = ids.connector(cmd_u, 0)
    vqc = max((int(n) for n in re.findall(r'\bIName="VQC(\d+)"', text)), default=0) + 1
    ref_xml = "\n".join([
        f'<C Type="OutputRef" V="178" U="{ref_u}" Title="{TITLE}" Px="{x + 3264}" Py="{y + 192}" '
        f'Px2="{x + 5376}" Py2="{y + 384}" Cl="0,0,0" Nio="2" Ref="{cmd_u}" WF="147456" Analog="true" '
        f'Text="true" LinkRefType="74">',
        f'\t<Co K="AI" Nc="1" U="{ref_ai}">', f'\t\t<In Input="{state_co["TQ"]}"/>', "\t</Co>",
        f'\t<Co K="AQ" U="{ref_aq}"/>', "</C>"])
    cmd_xml = "\n".join([
        f'<C Type="VirtualOutCmd" IName="VQC{vqc}" V="178" U="{cmd_u}" Title="{TITLE}" Cl="238,238,238" Nio="1" '
        f'WF="16400" Analog="true" CmdOn="&lt;v&gt;" SourceValHigh="10" DestValHigh="10" Tx="true">',
        f'\t<Co K="I" Nc="1" U="{cmd_i}">', f'\t\t<In Input="{ref_aq}"/>', "\t</Co>",
        '\t<IoData St="4"/>', '\t<Display Type="2" Unit="&lt;v.1&gt;" StateOnly="true"/>', "</C>"])

    text = add_moods(text, lc.get("U"), mood_lines)
    if circuits:
        text = editor.set_block_attr(text, lc.get("U"), {names_attr[key]: name for key, name in circuits})
    text = editor.insert_children(text, page.get("U"), [state_xml, ref_xml])
    text = editor.insert_children(text, vout.get("U"), [cmd_xml])
    text = editor.bump_counters(text, 3)
    new_xml = text.encode("utf-8")

    new_prog = Program(new_xml)          # il file deve restare XML valido e collegato come previsto
    state, ref, cmd = (new_prog.by_uuid[u] for u in (state_u, ref_u, cmd_u))
    assert new_prog.wiring(state)["ingressi"] == [f"I1 <- {prog.title(lc)}.Scene <LightController2>"]
    assert new_prog.wiring(ref)["ingressi"] == [f"AI <- {TITLE}.TQ <State>"]
    assert new_prog.wiring(cmd)["ingressi"] == [f"I <- {TITLE}.AQ <OutputRef>"]
    assert new_prog.parent[cmd].get("U") == vout.get("U") and new_prog.parent[state].get("U") == page.get("U")
    new_lc = new_prog.by_uuid[lc.get("U")]
    assert len(list(new_lc.iter("LightsceneC"))) == len(moods) + len(scenes)
    dup = [u for u in ids.used if len(re.findall(rf'\b(?:U|UUID)="{u}"', text)) > 1]
    if dup:
        raise SystemExit(f"ERRORE: UUID duplicati {dup}")

    out = DATA_DIR / "programmi" / f"{src.stem}-scene-govee-{datetime.now():%Y%m%d-%H%M}.Loxone"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(new_xml)
    print(f"Partenza: {src.name}\nCopia modificata: {out}")
    print(f"Controllo luci '{prog.title(lc)}' (nell'app '{lc.get('Desc') or prog.title(lc)}'): "
          f"{len(scenes)} atmosfere nuove, circuiti dimmer al {args.luminosita}%")
    for (name, scene), (number, _) in zip(scenes, rules):
        print(f"  {number:>3}  {name}  -> scena Govee '{scene}'")
    for key, name in circuits:
        print(f"  circuito {key}: '{lc.get(names_attr[key])}' -> '{name}'")
    d = diff(prog, new_prog)
    print(f"Aggiunti: {len(d['aggiunti'])}  Rimossi: {len(d['rimossi'])}  Modificati: {len(d['modificati'])}")
    for a in d["aggiunti"]:
        print(f"  + {a}")
    for ch in d["modificati"]:
        print(f"  ~ {ch['oggetto']}")
        for note in ch["modifiche"]:
            print(f"      {note}")


if __name__ == "__main__":
    main()
