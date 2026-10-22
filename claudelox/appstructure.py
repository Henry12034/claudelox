"""Struttura dell'app (LoxAPP3.json) e permessi (permissions.bin) aggiornati dal toolkit, per caricare
senza Loxone Config.

Loxone Config li genera quando salva il programma e li mette nello zip; il Miniserver usa quelli (non li
ricalcola), quindi per caricare direttamente una modifica visibile nell'app vanno aggiornati qui.
- LoxAPP3.json: JSON compatto in UTF-8, numeri decimali con 6 cifre (riscrivendo un file di Config non
  cambia nemmeno un byte).
- permissions.bin: blocchi da 16 byte. Per ogni oggetto: il suo UUID | FE*16 | UUID dei suoi pezzi (blocco,
  connettori) | FC*16 | chi lo puo' usare (utenti, gruppo della stanza, gruppo della categoria) |
  [FD*16 | ...] | FF*16. Gli UUID sono nel formato binario di Loxone (primi tre campi little-endian).
  Il Miniserver mostra un controllo solo a chi compare nel suo record: senza record non lo vede nessuno.

Come per i blocchi del programma, un controllo nuovo si copia da uno gia' presente, nato da un blocco
dello stesso tipo: gli UUID del modello diventano quelli del blocco nuovo (il blocco e i connettori con
lo stesso nome), poi nome, stanza, categoria, icona, preferito, importanza, formato e statistica si
ricavano dal blocco come fa Config (unita' "<v.1> %" -> formato "%.1f %%", nella statistica "%.1f %").
Nei permessi il controllo nuovo ha gli stessi utenti del modello e i gruppi della sua stanza e della sua
categoria, ricavati dagli altri controlli che stanno li'.
Gestiti: controlli nuovi, eliminati, nascosti; nome, stanza, categoria, unita', icona, preferito,
importanza e frequenza della statistica cambiati. Il resto finisce tra le modifiche non gestite: per
quelle serve Loxone Config.
"""
from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass, field
from datetime import datetime

from .program import UUID_RE, Program

UNIT_RE = re.compile(r"<v(?:\.(\d))?>")
# attributi dei blocchi che contano solo per la logica, non per l'app
LOGIC_ATTRS = {"Check", "Address", "Poll", "Timeout", "Allowedtimeouts", "Signed", "Analog", "SourceValLow",
               "SourceValHigh", "DestValLow", "DestValHigh", "MinVal", "MaxVal", "ValOT", "Formula", "Desc",
               "IName", "Cl", "WF", "V", "LtE", "Nio", "on", "Px", "Py", "Px2", "Py2", "StatsAutoDel"}
IODATA_HANDLED = {"Pr", "Cr", "Visu", "UseInFav", "Rating", "IconV"}
SEP_PARTS, SEP_ACCESS, SEP_END = 0xFE, 0xFC, 0xFF
SEPARATORS = {bytes([b]) * 16 for b in (0xFE, 0xFC, 0xFD, 0xFF)}
ROOM_GROUP, CAT_GROUP = "1", "2"  # attributo GT dei gruppi di permessi (RightGroup)


@dataclass
class AppUpdate:
    data: bytes                                          # nuovo LoxAPP3.json
    permissions: bytes | None = None                     # nuovo permissions.bin (None = invariato)
    done: list[str] = field(default_factory=list)         # cosa e' cambiato nell'app
    unsupported: list[str] = field(default_factory=list)  # modifiche che solo Loxone Config sa riportare


def dump(obj) -> str:
    """JSON nel formato di Loxone Config: compatto, caratteri UTF-8, decimali con 6 cifre."""
    if isinstance(obj, dict):
        return "{" + ",".join(json.dumps(k, ensure_ascii=False) + ":" + dump(v) for k, v in obj.items()) + "}"
    if isinstance(obj, list):
        return "[" + ",".join(dump(v) for v in obj) + "]"
    if isinstance(obj, float):
        return f"{obj:.6f}"
    return json.dumps(obj, ensure_ascii=False)


def unit_formats(unit: str) -> tuple[str, str] | None:
    """Unita' di Loxone ("<v.1> %") -> (formato del controllo "%.1f %%", formato della statistica "%.1f %")."""
    found = list(UNIT_RE.finditer(unit))
    rest = UNIT_RE.sub("", unit)
    if len(found) != 1 or "<" in rest or ">" in rest:
        return None
    m = found[0]
    number = f"%.{m.group(1) or 0}f"
    before, after = unit[:m.start()], unit[m.end():]
    return before.replace("%", "%%") + number + after.replace("%", "%%"), before + number + after


def uuid_bytes(u: str) -> bytes:
    """UUID Loxone (8-4-4-16) nel formato binario di permissions.bin."""
    a, b, c, d = u.split("-")
    return int(a, 16).to_bytes(4, "little") + int(b, 16).to_bytes(2, "little") + \
        int(c, 16).to_bytes(2, "little") + bytes.fromhex(d)


@dataclass
class PermRecord:
    head: bytes                                 # UUID dell'oggetto
    sections: list[tuple[int, list[bytes]]]     # (separatore, blocchi da 16 byte) in ordine


def parse_permissions(data: bytes) -> list[PermRecord]:
    if len(data) % 16:
        raise ValueError("permissions.bin: lunghezza non multipla di 16")
    recs: list[PermRecord] = []
    cur: PermRecord | None = None
    for off in range(0, len(data), 16):
        chunk = data[off:off + 16]
        if cur is None:
            cur = PermRecord(chunk, [])
        elif chunk in SEPARATORS:
            if chunk[0] == SEP_END:
                recs.append(cur)
                cur = None
            else:
                cur.sections.append((chunk[0], []))
        elif not cur.sections:
            raise ValueError("permissions.bin: formato inatteso")
        else:
            cur.sections[-1][1].append(chunk)
    if cur is not None:
        raise ValueError("permissions.bin: ultimo record non chiuso")
    return recs


def build_permissions(recs: list[PermRecord]) -> bytes:
    out = bytearray()
    for r in recs:
        out += r.head
        for sep, items in r.sections:
            out += bytes([sep]) * 16
            for item in items:
                out += item
        out += bytes([SEP_END]) * 16
    return bytes(out)


def _io(e) -> dict:
    io = e.find("IoData")
    return dict(io.attrib) if io is not None else {}


def _unit(e) -> str | None:
    d = e.find("Display")
    return d.get("Unit") if d is not None else None


def _visible(e) -> bool:
    return _io(e).get("Visu") == "true"


def _connectors(e) -> dict[str, str]:
    return {co.get("K"): co.get("U") for co in e.findall("Co")}


class _Updater:
    def __init__(self, old: Program, new: Program, app: dict, perms: list[PermRecord] | None):
        self.old, self.new, self.app, self.perms = old, new, app, perms
        self.controls: dict = app["controls"]
        self.done: list[str] = []
        self.unsupported: list[str] = []
        self.icons: dict[str | None, str | None] = {}  # IconV del blocco -> icona del controllo
        for c in self.controls.values():
            b = old.by_uuid.get(c.get("uuidAction"))
            if b is not None:
                self.icons.setdefault(_io(b).get("IconV"), c.get("defaultIcon"))
        # gruppi di permessi (stanza / categoria) e quale gruppo usano i controlli di ogni stanza e categoria
        self.group_kind = {uuid_bytes(e.get("U")): e.get("GT") for e in old.root.iter("C")
                           if e.get("Type") == "RightGroup" and e.get("U")}
        self.groups: dict[tuple[str, str], set[bytes]] = {}
        for r in perms or []:
            c = self.controls.get(f"U:{self._text(r.head)}")
            for item in (i for sep, items in r.sections if sep == SEP_ACCESS for i in items) if c else ():
                kind = self.group_kind.get(item)
                place = c.get("room") if kind == ROOM_GROUP else c.get("cat") if kind == CAT_GROUP else None
                if place:
                    self.groups.setdefault((kind, place), set()).add(item)

    @staticmethod
    def _text(b: bytes) -> str:
        return (f"{int.from_bytes(b[0:4], 'little'):08x}-{int.from_bytes(b[4:6], 'little'):04x}-"
                f"{int.from_bytes(b[6:8], 'little'):04x}-{b[8:16].hex()}")

    def key_of(self, block_u: str) -> str | None:
        return next((k for k, c in self.controls.items() if c.get("uuidAction") == block_u), None)

    def record(self, block_u: str) -> PermRecord | None:
        head = uuid_bytes(block_u)
        return next((r for r in self.perms or [] if r.head == head), None)

    def group_for(self, kind: str, place: str | None) -> bytes | None:
        found = self.groups.get((kind, place or ""), set())
        return next(iter(found)) if len(found) == 1 else None

    def regroup(self, rec: PermRecord, c: dict, label: str) -> bool:
        """Nel record dei permessi, i gruppi della stanza e della categoria diventano quelli del controllo."""
        for _, items in (s for s in rec.sections if s[0] == SEP_ACCESS):
            for i, item in enumerate(items):
                kind = self.group_kind.get(item)
                if kind not in (ROOM_GROUP, CAT_GROUP):
                    continue
                group = self.group_for(kind, c.get("room") if kind == ROOM_GROUP else c.get("cat"))
                if group is None:
                    what = "della stanza" if kind == ROOM_GROUP else "della categoria"
                    self.unsupported.append(f"{label}: permessi {what} sconosciuti (nessun altro controllo li')")
                    return False
                items[i] = group
        return True

    def icon(self, e) -> tuple[bool, str | None]:
        iv = _io(e).get("IconV")
        if iv is None:
            return True, None
        return (iv in self.icons), self.icons.get(iv)

    def fill(self, c: dict, e, label: str) -> bool:
        """Campi del controllo ricavati dal blocco (nome, stanza, categoria, icona, formato, statistica)."""
        io, title = _io(e), self.new.title(e)
        if io.get("Pr") not in self.app["rooms"] or io.get("Cr") not in self.app["cats"]:
            self.unsupported.append(f"{label}: stanza o categoria che l'app non conosce ancora")
            return False
        known, icon = self.icon(e)
        if not known:
            self.unsupported.append(f"{label}: icona mai usata nell'app (IconV {io.get('IconV')})")
            return False
        old_name = c.get("name")
        c.update(name=title, room=io["Pr"], cat=io["Cr"], defaultIcon=icon,
                 isFavorite=io.get("UseInFav") == "true", defaultRating=int(io.get("Rating") or 0))
        fmt = None
        if "format" in c.get("details", {}):
            fmt = unit_formats(_unit(e) or "")
            if fmt is None:
                self.unsupported.append(f"{label}: unita' {_unit(e)!r} non convertibile")
                return False
            c["details"]["format"] = fmt[0]
        stats = e.get("StatsType")
        if not stats:
            c.pop("statistic", None)
        elif "statistic" not in c:
            self.unsupported.append(f"{label}: statistica nuova su un controllo che non l'aveva")
            return False
        else:
            c["statistic"]["frequency"] = int(stats)
            for out in c["statistic"].get("outputs", []):
                if len(c["statistic"]["outputs"]) == 1 or out.get("name") == old_name:
                    out["name"] = title
                if "format" in out and fmt is not None:
                    out["format"] = fmt[1]
        return True

    def template(self, e) -> tuple[dict, dict[str, str]] | None:
        """Controllo da copiare per un blocco nuovo, con la corrispondenza degli UUID del modello."""
        best, best_score = None, -1
        new_co = _connectors(e)
        for c in self.controls.values():
            tb = self.old.by_uuid.get(c.get("uuidAction"))
            if (tb is None or tb.get("Type") != e.get("Type") or tb.get("Analog") != e.get("Analog")
                    or c.get("restrictions") or c.get("isSecured") or not _visible(tb)):
                continue
            mapping = {tb.get("U"): e.get("U")}
            for k, u in _connectors(tb).items():
                if k in new_co:
                    mapping[u] = new_co[k]
            others = set(UUID_RE.findall(dump({x: v for x, v in c.items() if x not in ("room", "cat")})))
            rec = self.record(tb.get("U")) if self.perms is not None else None
            if others - set(mapping) or (self.perms is not None and rec is None):
                continue  # il modello usa UUID di altri blocchi (sotto-controlli...) o non ha permessi: no
            if rec is not None:
                parts = {i for sep, items in rec.sections if sep == SEP_PARTS for i in items}
                if parts - {uuid_bytes(u) for u in mapping}:
                    continue
            score = 2 * (bool(tb.get("StatsType")) == bool(e.get("StatsType"))) + (_unit(tb) is not None)
            if score > best_score:
                best, best_score = (c, mapping), score
        return best

    def add(self, e) -> None:
        label = f"'{self.new.title(e)}' <{e.get('Type')}>"
        found = self.template(e)
        if found is None:
            self.unsupported.append(f"{label}: nell'app non c'e' un controllo dello stesso tipo da copiare")
            return
        tpl, mapping = found
        text = dump(tpl)
        for a, b in mapping.items():
            text = text.replace(a, b)
        c = json.loads(text)
        if not self.fill(c, e, label):
            return
        if self.perms is not None:
            rec = copy.deepcopy(self.record(tpl["uuidAction"]))
            by_bytes = {uuid_bytes(a): uuid_bytes(b) for a, b in mapping.items()}
            rec.head = uuid_bytes(e.get("U"))
            rec.sections = [(sep, [by_bytes.get(i, i) if sep == SEP_PARTS else i for i in items])
                            for sep, items in rec.sections]
            if not self.regroup(rec, c, label):
                return
            self.perms.append(rec)
        self.controls[f"U:{e.get('U')}"] = c
        self.done.append(f"nuovo nell'app: {label} ({c['type']}) in "
                         f"'{self.app['rooms'][c['room']]['name']}', categoria '{self.app['cats'][c['cat']]['name']}'")

    def remove(self, key: str, block_u: str, what: str) -> None:
        del self.controls[key]
        if self.perms is not None:
            head = uuid_bytes(block_u)
            self.perms[:] = [r for r in self.perms if r.head != head]
        self.done.append(what)

    def change(self, ea, eb, key: str) -> None:
        label = f"'{self.new.title(eb)}' <{eb.get('Type')}>"
        ia, ib = _io(ea), _io(eb)
        va, vb = ia.get("Visu") == "true", ib.get("Visu") == "true"
        if va and not vb and ib.get("UseInFav") != "true":
            self.remove(key, eb.get("U"), f"nascosto nell'app: {label}")
            return
        if va != vb or (not vb and ia.get("UseInFav") != ib.get("UseInFav")):
            # controlli solo tra i preferiti: Config li scrive con altre restrizioni, non ricostruibili qui
            self.unsupported.append(f"{label}: cambia la visibilita' nell'app")
            return
        attrs = {k for k in set(ea.attrib) | set(eb.attrib) if ea.get(k) != eb.get(k)} - LOGIC_ATTRS
        io_changes = {k for k in set(ia) | set(ib) if ia.get(k) != ib.get(k)}
        relevant = attrs - {"Title", "StatsType"} | (io_changes - IODATA_HANDLED)
        da, db = ea.find("Display"), eb.find("Display")
        disp = {k for k in set(da.attrib if da is not None else {}) | set(db.attrib if db is not None else {})
                if (da.get(k) if da is not None else None) != (db.get(k) if db is not None else None)}
        relevant |= {f"Display.{k}" for k in disp - {"Unit"}}
        if relevant:
            self.unsupported.append(f"{label}: cambiano {', '.join(sorted(relevant))}")
            return
        if not (attrs or io_changes or disp):
            return
        if {"subControls", "links"} & set(self.controls[key]):
            self.unsupported.append(f"{label}: controllo composto (sotto-controlli), lo aggiorna solo Config")
            return
        c = copy.deepcopy(self.controls[key])
        if not self.fill(c, eb, label):
            return
        if self.perms is not None and {"Pr", "Cr"} & io_changes:
            rec = self.record(eb.get("U"))
            if rec is None:
                self.unsupported.append(f"{label}: nessun record dei permessi da aggiornare")
                return
            new_rec = copy.deepcopy(rec)
            if not self.regroup(new_rec, c, label):
                return
            rec.sections = new_rec.sections
        self.controls[key] = c
        self.done.append(f"aggiornato nell'app: {label}")

    def run(self, referenced: set[str]) -> None:
        old_u = {e.get("U"): e for e in self.old.components() if e.get("U")}
        new_u = {e.get("U"): e for e in self.new.components() if e.get("U")}
        for u, ea in old_u.items():
            key = self.key_of(u)
            eb = new_u.get(u)
            if key is not None:
                if eb is None:
                    self.remove(key, u, f"tolto dall'app: '{self.old.title(ea)}' <{ea.get('Type')}>")
                else:
                    self.change(ea, eb, key)
            elif u in referenced and (eb is None or self._differs(ea, eb)):
                self.unsupported.append(f"'{self.old.title(ea)}' <{ea.get('Type')}>: fa parte di un altro "
                                        "controllo dell'app")
        for u, eb in new_u.items():
            if self.key_of(u) is None and _visible(eb) and (u not in old_u or not _visible(old_u[u])):
                self.add(eb)
        text = dump(self.app)
        gone = {u for u in old_u.keys() - new_u.keys()}
        gone |= {co for u in gone for co in _connectors(old_u[u]).values()}
        perm_bytes = build_permissions(self.perms) if self.perms is not None else b""
        left = sorted(u for u in gone if u in text or uuid_bytes(u) in perm_bytes)
        if left:
            self.unsupported.append(f"l'app o i permessi usano ancora oggetti eliminati: {', '.join(left[:3])}")

    @staticmethod
    def _differs(ea, eb) -> bool:
        return (ea.get("Title") != eb.get("Title") or _io(ea) != _io(eb) or _unit(ea) != _unit(eb))


def update_app(old: Program, new: Program, app_json: bytes, permissions: bytes | None = None,
               now: datetime | None = None) -> AppUpdate:
    """Struttura dell'app e permessi per il programma `new`, partendo da quelli di `old` (scritti da Config).

    Senza permissions (file assente) i controlli nuovi non si possono rendere visibili: restano non gestiti.
    """
    text = app_json.decode("utf-8-sig")
    app = json.loads(text)
    perms = parse_permissions(permissions) if permissions is not None else None
    up = _Updater(old, new, app, perms)
    up.run(set(UUID_RE.findall(text)))
    if perms is None and any(d.startswith("nuovo") for d in up.done):
        up.unsupported.append("manca permissions.bin: i controlli nuovi non sarebbero visibili a nessuno")
    if up.done:
        app["lastModified"] = (now or datetime.now()).strftime("%Y-%m-%d %H:%M:%S")
    new_perms = build_permissions(perms) if perms is not None else None
    return AppUpdate(dump(app).encode("utf-8"), new_perms if new_perms != permissions else None,
                     up.done, up.unsupported)
