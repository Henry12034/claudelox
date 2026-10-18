"""Analisi in sola lettura del programma Loxone (il progetto che si apre in Loxone Config).

Privacy: utenti, permessi, dispositivi push e trust sono esclusi dall'analisi;
password, utenti e chiavi di dispositivi (intercom, Air, indirizzi con utente:password)
non vengono mai mostrati.
"""
from __future__ import annotations

import collections
import re
import urllib.parse
import xml.etree.ElementTree as ET

from .structure import norm

PRIVATE_TYPES = {"User", "UserGroup", "UserCaption", "UserGroupCaption", "Permission", "RightGroup",
                 "PuDe", "Trust", "TrustCaption"}
URL_CRED_RE = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^/\s@]+@")
PARAM_CRED_RE = re.compile(r"(?i)\b(pass(?:word)?|pwd|user(?:name)?|token|api_?key|key|secret|auth)=([^&\s\"';]+)")

# contenitori di hardware/IO sotto il Miniserver da elencare nel riepilogo
HARDWARE_CAPTIONS = {"InputCaption", "AnalogInputCaption", "OutputCaption", "WeatherCaption",
                     "VirtualInCaption", "VirtualOutCaption", "LoggerOutCaption"}


def redact(text: str) -> str:
    text = URL_CRED_RE.sub(r"\1***@", text or "")
    return PARAM_CRED_RE.sub(lambda m: f"{m.group(1)}=***", text)


def safe_address(addr: str | None) -> str:
    """Mostra solo schema e host di un indirizzo (mai utente, password o percorso)."""
    if not addr:
        return ""
    if "://" in addr:
        try:
            u = urllib.parse.urlsplit(addr)
            port = f":{u.port}" if u.port else ""
            return f"{u.scheme}://{u.hostname}{port}"
        except ValueError:
            return "(indirizzo configurato)"
    return redact(addr)


class Program:
    def __init__(self, xml_bytes: bytes):
        self.root = ET.fromstring(xml_bytes.lstrip(b"\xef\xbb\xbf"))
        self.version = self.root.get("Version")
        self.parent: dict = {}
        for p in self.root.iter():
            for ch in p:
                self.parent[ch] = p
        self._private: set = set()
        for e in self.root.iter("C"):
            if self._is_private(e):
                self._private.update(e.iter())
        self.by_uuid = {e.get("U"): e for e in self.components() if e.get("U")}
        self.conn_owner: dict[str, tuple] = {}
        self.links_from: dict[str, list] = collections.defaultdict(list)
        for e in self.components():
            for co in e.findall("Co"):
                self.conn_owner[co.get("U")] = (e, co.get("K"))
        for e in self.components():
            for co in e.findall("Co"):
                for i in co.findall("In"):
                    self.links_from[i.get("Input")].append((e, co.get("K")))

    def _is_private(self, e) -> bool:
        t = e.get("Type")
        if t in PRIVATE_TYPES:
            return True
        return t == "LoxCaption" and any(c.get("Type") in PRIVATE_TYPES for c in e if c.tag == "C")

    def components(self) -> list:
        return [e for e in self.root.iter("C") if e not in self._private]

    def count_private(self, type_: str) -> int:
        return sum(1 for e in self.root.iter("C") if e.get("Type") == type_)

    def path(self, e) -> str:
        parts, p = [], self.parent.get(e)
        while p is not None and p.tag == "C":
            if p.get("Type") != "Document":
                parts.append(p.get("Title") or p.get("Type"))
            p = self.parent.get(p)
        return " > ".join(reversed(parts))

    def title(self, e) -> str:
        t = e.get("Title")
        if t:
            return t
        ref = self.by_uuid.get(e.get("Ref") or "")
        return (ref.get("Title") if ref is not None else None) or e.get("Type")

    # --- riepilogo ---
    def summary(self) -> dict:
        comps = self.components()
        types = collections.Counter(e.get("Type") for e in comps)
        doc = next((e for e in comps if e.get("Type") == "Document"), None)
        ms = next((e for e in comps if e.get("Type") == "LoxLIVE"), None)
        pages = []
        for p in comps:
            if p.get("Type") == "Page":
                kids = collections.Counter(c.get("Type") for c in p if c.tag == "C")
                pages.append({"titolo": p.get("Title"), "oggetti": sum(kids.values()),
                              "tipi": dict(kids.most_common(6))})
        hardware = {}
        if ms is not None:
            for cap in ms:
                if cap.tag != "C":
                    continue
                items = []
                for d in cap.iter("C"):
                    if d is cap or d in self._private or d.get("Type", "").endswith("Cmd"):
                        continue
                    if d.get("Type") in ("LoxCaption",):
                        continue
                    entry = f"{d.get('Type')}: {d.get('Title')}"
                    if d.get("Address"):
                        entry += f" ({safe_address(d.get('Address'))})"
                    items.append(entry)
                if items:
                    hardware[cap.get("Title") or cap.get("Type")] = items
        rooms = [e.get("Title") for e in comps if e.get("Type") == "Place"]
        cats = [e.get("Title") for e in comps if e.get("Type") == "Category"]
        return {
            "progetto": doc.get("Title") if doc is not None else None,
            "versione_programma": self.version,
            "miniserver": ms.get("Title") if ms is not None else None,
            "stanze": rooms,
            "categorie": cats,
            "pagine": pages,
            "hardware_e_io": hardware,
            "blocchi_per_tipo": dict(types.most_common()),
            "utenti_configurati": self.count_private("User"),
        }

    # --- ricerca e collegamenti ---
    def find(self, text: str) -> list:
        q = norm(text)
        return [e for e in self.components()
                if e.get("Type") not in ("Page",) and (q in norm(self.title(e)) or q in norm(e.get("Desc") or ""))]

    def wiring(self, e) -> dict:
        """Ingressi (da dove arrivano i segnali) e uscite (dove vanno) di un blocco."""
        ins, outs = [], []
        for co in e.findall("Co"):
            for i in co.findall("In"):
                src = self.conn_owner.get(i.get("Input"))
                if src:
                    ins.append(f"{co.get('K')} <- {self.title(src[0])}.{src[1]} <{src[0].get('Type')}>")
            for dst, k in self.links_from.get(co.get("U"), []):
                outs.append(f"{co.get('K')} -> {self.title(dst)}.{k} <{dst.get('Type')}>")
        info = {"tipo": e.get("Type"), "titolo": self.title(e), "posizione": self.path(e),
                "uuid": e.get("U"), "ingressi": ins, "uscite": outs}
        if e.get("Desc"):
            info["descrizione"] = redact(e.get("Desc"))
        if e.get("Type") == "Formula" and e.get("Formula"):
            info["formula"] = e.get("Formula")
        ref = self.by_uuid.get(e.get("Ref") or "")
        if ref is not None:
            info["riferimento"] = f"{ref.get('Title')} <{ref.get('Type')}> in {self.path(ref)}"
        return info

    def page(self, name: str):
        q = norm(name)
        pages = [p for p in self.components() if p.get("Type") == "Page"]
        exact = [p for p in pages if norm(p.get("Title")) == q]
        hits = exact or [p for p in pages if q in norm(p.get("Title"))]
        if len(hits) != 1:
            titles = ", ".join(p.get("Title") for p in pages)
            raise LookupError(f"pagina '{name}' non trovata o ambigua. Pagine: {titles}")
        return hits[0]

    def block(self, ref: str):
        """Un solo blocco per UUID o titolo (esatto, poi parziale)."""
        if ref in self.by_uuid:
            return self.by_uuid[ref]
        hits = self.find(ref)
        exact = [e for e in hits if norm(self.title(e)) == norm(ref)]
        hits = exact or hits
        if len(hits) != 1:
            listed = "\n".join(f"  - {self.title(e)} <{e.get('Type')}> in {self.path(e)} uuid={e.get('U')}"
                               for e in hits[:20])
            raise LookupError(f"blocco '{ref}' " + ("non trovato" if not hits else f"ambiguo:\n{listed}"))
        return hits[0]

    def label(self, e) -> str:
        return f"{self.title(e)} <{e.get('Type')}> in {self.path(e)}"

    def device_hosts(self) -> list[dict]:
        """Dispositivi esterni interrogati/comandati dal Miniserver (ingressi/uscite virtuali HTTP e UDP)."""
        out = []
        for e in self.components():
            addr = e.get("Address") or ""
            if e.get("Type") not in ("VirtualOut", "VirtualHttpIn") or not addr:
                continue
            if "://" in addr:
                try:
                    u = urllib.parse.urlsplit(addr)
                    host, port = u.hostname, u.port or (443 if u.scheme == "https" else 80)
                except ValueError:
                    continue
                proto = "tcp"
            else:
                m = re.match(r"^/dev/(udp|tcp)/([^/]+)/(\d+)", addr)
                if not m:
                    continue
                proto, host, port = m.group(1), m.group(2), int(m.group(3))
            out.append({"titolo": e.get("Title"), "tipo": e.get("Type"), "host": host, "porta": port, "proto": proto})
        return out


# --- confronto tra due versioni del programma ---
LAYOUT_ATTRS = {"Px", "Py", "Px2", "Py2", "WF", "LtE", "Cl", "V"}
SECRET_ATTR_RE = re.compile(r"(?i)(pwd|pass|user|token|secret|key)")
REDACT_ATTRS = {"Address", "CmdOn", "CmdOff", "ExtVideoUrl", "IntVideoUrl", "ExtAudioHost", "IntAudioHost"}


def _shown(attr: str, value: str | None):
    if value is None:
        return None
    if SECRET_ATTR_RE.search(attr):
        return "***"
    return redact(value) if attr in REDACT_ATTRS else value


# IoData: stanza, categoria e visibilita' di un blocco nell'app
IODATA_NAMES = {"Pr": "stanza", "Cr": "categoria", "Visu": "visibile nell'app", "UseInFav": "tra i preferiti",
                "IconV": "icona", "Rating": "importanza"}
IODATA_PRIVATE = {"Ugr", "Ugx"}  # permessi dei gruppi di utenti: si segnala solo che sono cambiati
LOGIC_ONLY = ("parametro ", "nuovo collegamento", "collegamento rimosso", "NumO:")  # NumO = contatore oggetti
UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{16}")


def _iodata(e) -> dict:
    io = e.find("IoData")
    return dict(io.attrib) if io is not None else {}


def _visible(e) -> bool:
    return _iodata(e).get("Visu") == "true"


def _changes(a: Program, ea, b: Program, eb) -> list[str]:
    notes = [f"{k}: {_shown(k, ea.get(k))!r} -> {_shown(k, eb.get(k))!r}"
             for k in sorted(set(ea.attrib) | set(eb.attrib))
             if k not in LAYOUT_ATTRS and ea.get(k) != eb.get(k)]
    ia, ib = _iodata(ea), _iodata(eb)
    for k in sorted(set(ia) | set(ib)):
        if ia.get(k) == ib.get(k):
            continue
        if k in IODATA_PRIVATE:
            notes.append("permessi degli utenti modificati")
            continue
        def name(prog, v):
            ref = prog.by_uuid.get(v or "")
            return ref.get("Title") if ref is not None else v
        notes.append(f"{IODATA_NAMES.get(k, k)}: {name(a, ia.get(k))!r} -> {name(b, ib.get(k))!r}")
    pa = {co.get("K"): co.get("Def") for co in ea.findall("Co")}
    pb = {co.get("K"): co.get("Def") for co in eb.findall("Co")}
    notes += [f"parametro {k}: {pa.get(k)!r} -> {pb.get(k)!r}" for k in sorted(set(pa) | set(pb))
              if pa.get(k) != pb.get(k)]
    ins_a, ins_b = set(a.wiring(ea)["ingressi"]), set(b.wiring(eb)["ingressi"])
    notes += [f"nuovo collegamento: {x}" for x in sorted(ins_b - ins_a)]
    notes += [f"collegamento rimosso: {x}" for x in sorted(ins_a - ins_b)]
    return notes


def diff(a: Program, b: Program) -> dict:
    ua = {e.get("U"): e for e in a.components() if e.get("U")}
    ub = {e.get("U"): e for e in b.components() if e.get("U")}
    changed = []
    for u in sorted(ua.keys() & ub.keys()):
        notes = _changes(a, ua[u], b, ub[u])
        if notes:
            changed.append({"oggetto": b.label(ub[u]), "modifiche": notes})
    return {"aggiunti": [b.label(ub[u]) for u in sorted(ub.keys() - ua.keys())],
            "rimossi": [a.label(ua[u]) for u in sorted(ua.keys() - ub.keys())],
            "modificati": changed}


def app_impact(a: Program, b: Program, app_json: bytes | None) -> list[str]:
    """Modifiche che cambiano cio' che si vede nell'app Loxone (controlli, nomi, stanze).

    La struttura dell'app (LoxAPP3.json) la genera Loxone Config quando salva: il Miniserver
    non la ricalcola. Queste modifiche vanno quindi caricate con Loxone Config, non via FTP.
    """
    in_app = set(UUID_RE.findall(app_json.decode("utf-8", "replace"))) if app_json else set()
    ua = {e.get("U"): e for e in a.components() if e.get("U")}
    ub = {e.get("U"): e for e in b.components() if e.get("U")}
    out = [f"nuovo controllo visibile: {b.label(ub[u])}" for u in sorted(ub.keys() - ua.keys())
           if _visible(ub[u]) or u in in_app]
    out += [f"controllo dell'app eliminato: {a.label(ua[u])}" for u in sorted(ua.keys() - ub.keys())
            if _visible(ua[u]) or u in in_app]
    for u in sorted(ua.keys() & ub.keys()):
        if not (u in in_app or _visible(ua[u]) or _visible(ub[u])):
            continue
        visual = [n for n in _changes(a, ua[u], b, ub[u]) if not n.startswith(LOGIC_ONLY)]
        if visual:
            out.append(f"{b.label(ub[u])}: {'; '.join(visual)}")
    return out


# --- modifica di un parametro (sul testo XML: il resto del file resta identico byte per byte) ---
def _escape_attr(value: str) -> str:
    return (value.replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;").replace(">", "&gt;"))


def set_parameter(xml: bytes, connector_uuid: str, value: str) -> bytes:
    text = xml.decode("utf-8")
    m = re.search(r'<Co\b[^>]*\bU="%s"[^>]*>' % re.escape(connector_uuid), text)
    if not m:
        raise LookupError(f"connettore {connector_uuid} non trovato nel file")
    tag = m.group(0)
    if re.search(r'\sDef="[^"]*"', tag):
        new_tag = re.sub(r'(\s)Def="[^"]*"', lambda mm: f'{mm.group(1)}Def="{_escape_attr(value)}"', tag, count=1)
    else:
        new_tag = tag.replace(f' U="{connector_uuid}"', f' Def="{_escape_attr(value)}" U="{connector_uuid}"', 1)
    return (text[:m.start()] + new_tag + text[m.end():]).encode("utf-8")
