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
ACCESS_TYPES = PRIVATE_TYPES - {"PuDe"}  # chi puo' entrare e con quali diritti: il caricamento diretto non li cambia
URL_CRED_RE = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^/\s@]+@")
PARAM_CRED_RE = re.compile(r"(?i)\b([\w.-]*(?:pass|pwd|user|token|secret|key|auth|appid|app_id)[\w.-]*)="
                           r"([^&\s\"';]+)")
HEADER_CRED_RE = re.compile(r"(?i)\b(proxy-authorization|authorization|x-api-key|api-key|x-auth-token|cookie)"
                            r"\s*:\s*[^\r\n\"']+")
BEARER_RE = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}")

# contenitori di hardware/IO sotto il Miniserver da elencare nel riepilogo
HARDWARE_CAPTIONS = {"InputCaption", "AnalogInputCaption", "OutputCaption", "WeatherCaption",
                     "VirtualInCaption", "VirtualOutCaption", "LoggerOutCaption"}


def redact(text: str) -> str:
    """Oscura credenziali dentro un testo: utente:password negli indirizzi, parametri e intestazioni riservati."""
    text = URL_CRED_RE.sub(r"\1***@", text or "")
    text = HEADER_CRED_RE.sub(lambda m: f"{m.group(1)}: ***", text)
    text = BEARER_RE.sub(lambda m: f"{m.group(1)} ***", text)
    return PARAM_CRED_RE.sub(lambda m: f"{m.group(1)}=***", text)


def redact_data(value, key: str = ""):
    """Copia di dati JSON (es. i dettagli di un controllo) con i campi riservati oscurati."""
    if isinstance(value, dict):
        return {k: redact_data(v, k) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_data(v, key) for v in value]
    if isinstance(value, str):
        return "***" if key and SECRET_ATTR_RE.search(key) else redact(value)
    return value


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
        self._titles = collections.Counter(self.title(e) for e in self.components())

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

    def name(self, e) -> str:
        """Titolo, con un pezzo dell'UUID se altri oggetti del programma hanno lo stesso titolo."""
        t = self.title(e)
        return f"{t}#{(e.get('U') or '')[4:13]}" if self._titles[t] > 1 else t

    def wiring(self, e, detailed: bool = False) -> dict:
        """Ingressi (da dove arrivano i segnali) e uscite (dove vanno) di un blocco.

        detailed=True serve a leggere la logica: titoli resi univoci, ingressi invertiti, parametri.
        Il confronto tra versioni (diff) usa la forma semplice, stabile tra un programma e l'altro.
        """
        name = self.name if detailed else self.title

        def inverted(block, key) -> str:
            co = next((c for c in block.findall("Co") if c.get("K") == key), None)
            return " (invertito)" if detailed and co is not None and co.get("Inv") == "true" else ""

        ins, outs = [], []
        for co in e.findall("Co"):
            for i in co.findall("In"):
                src = self.conn_owner.get(i.get("Input"))
                if src:
                    ins.append(f"{co.get('K')}{inverted(e, co.get('K'))} <- {name(src[0])}.{src[1]} "
                               f"<{src[0].get('Type')}>")
            for dst, k in self.links_from.get(co.get("U"), []):
                outs.append(f"{co.get('K')} -> {name(dst)}.{k}{inverted(dst, k)} <{dst.get('Type')}>")
        info = {"tipo": e.get("Type"), "titolo": name(e), "posizione": self.path(e),
                "uuid": e.get("U"), "ingressi": ins, "uscite": outs}
        if detailed:
            params = {co.get("K"): _shown(co.get("K"), co.get("Def")) for co in e.findall("Co")
                      if co.get("Def") is not None}
            if params:
                info["parametri"] = params
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
        """Dispositivi esterni interrogati/comandati dal Miniserver (ingressi/uscite virtuali HTTP e UDP).

        'comandi' (percorso interrogato, comandi e valori letti) serve a riconoscere il tipo di dispositivo:
        puo' contenere dati riservati, quindi non va stampato."""
        out = []
        for e in self.components():
            addr = e.get("Address") or ""
            if e.get("Type") not in ("VirtualOut", "VirtualHttpIn") or not addr:
                continue
            path = ""
            if "://" in addr:
                try:
                    u = urllib.parse.urlsplit(addr)
                    host, port = u.hostname, u.port or (443 if u.scheme == "https" else 80)
                except ValueError:
                    continue
                proto, path = "tcp", u.path
            else:
                m = re.match(r"^/dev/(udp|tcp)/([^/]+)/(\d+)", addr)
                if not m:
                    continue
                proto, host, port = m.group(1), m.group(2), int(m.group(3))
            cmds = [path] + [c.get(k) for c in e.iter("C") if c is not e for k in ("CmdOn", "CmdOff", "Check")
                             if c.get(k)]
            out.append({"titolo": e.get("Title"), "tipo": e.get("Type"), "host": host, "porta": port, "proto": proto,
                        "comandi": cmds})
        return out


# --- confronto tra due versioni del programma ---
LAYOUT_ATTRS = {"Px", "Py", "Px2", "Py2", "WF", "LtE", "Cl", "V"}
SECRET_ATTR_RE = re.compile(r"(?i)(pwd|pass|user|usr|token|tkn|secret|key|cred|auth)")


def _shown(attr: str, value: str | None):
    """Valore di un attributo da mostrare: mai segreti, ovunque si trovino (indirizzi, comandi, testi)."""
    if value is None:
        return None
    if SECRET_ATTR_RE.search(attr):
        return "***"
    return redact(value)


# IoData: stanza, categoria e visibilita' di un blocco nell'app
IODATA_NAMES = {"Pr": "stanza", "Cr": "categoria", "Visu": "visibile nell'app", "UseInFav": "tra i preferiti",
                "IconV": "icona", "Rating": "importanza"}
IODATA_PRIVATE = {"Ugr", "Ugx"}  # permessi dei gruppi di utenti: si segnala solo che sono cambiati
LOGIC_ONLY = ("parametro ", "nuovo collegamento", "collegamento rimosso", "ingresso ",
              "NumO:",          # contatore oggetti
              "atmosfer")       # atmosfere di un controllo luci: l'app le chiede al Miniserver, non sono in LoxAPP3
UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{16}")


def _iodata(e) -> dict:
    io = e.find("IoData")
    return dict(io.attrib) if io is not None else {}


def _visible(e) -> bool:
    return _iodata(e).get("Visu") == "true"


def changed_children(ea, eb) -> list[str]:
    """Tag degli altri elementi di un blocco (Display, testi delle notifiche, regole di stato...) diversi tra due
    versioni. Connettori, IoData e blocchi contenuti hanno il loro confronto; le atmosfere le confronta _moods."""
    tags = ({c.tag for c in ea} | {c.tag for c in eb}) - {"C", "Co", "IoData"}
    if ea.get("Type") == "LightController2":
        tags.discard("LightscenesC")
    return sorted(t for t in tags if [_canon(c) for c in ea.findall(t)] != [_canon(c) for c in eb.findall(t)])


def _child_notes(ea, eb) -> list[str]:
    notes: list[str] = []
    for tag in changed_children(ea, eb):
        ca, cb = ea.findall(tag), eb.findall(tag)
        found: list[str] = []
        if len(ca) == len(cb):
            for x, y in zip(ca, cb):
                found += [f"{tag}.{k}: {_shown(k, x.get(k))!r} -> {_shown(k, y.get(k))!r}"
                          for k in sorted(set(x.attrib) | set(y.attrib))
                          if k not in VOLATILE_ATTRS and x.get(k) != y.get(k)]
        notes += found or [f"contenuto di <{tag}> modificato"]
    return notes


def _changes(a: Program, ea, b: Program, eb, children: bool = True) -> list[str]:
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
    notes += [f"parametro {k}: {_shown(k, pa.get(k))!r} -> {_shown(k, pb.get(k))!r}"
              for k in sorted(set(pa) | set(pb)) if pa.get(k) != pb.get(k)]
    inv_a = {co.get("K") for co in ea.findall("Co") if co.get("Inv") == "true"}
    inv_b = {co.get("K") for co in eb.findall("Co") if co.get("Inv") == "true"}
    notes += [f"ingresso {k} ora invertito" for k in sorted(inv_b - inv_a)]
    notes += [f"ingresso {k} non piu' invertito" for k in sorted(inv_a - inv_b)]
    # collegamenti confrontati per UUID (rinominare il blocco sorgente non e' un collegamento nuovo)
    ins_a, ins_b = _inputs(a, ea), _inputs(b, eb)
    notes += sorted(f"nuovo collegamento: {ins_b[k]}" for k in ins_b.keys() - ins_a.keys())
    notes += sorted(f"collegamento rimosso: {ins_a[k]}" for k in ins_a.keys() - ins_b.keys())
    moods_a, moods_b = _moods(ea), _moods(eb)
    added = [m for m in moods_b if m not in moods_a]
    removed = [m for m in moods_a if m not in moods_b]
    if added:
        notes.append("atmosfere aggiunte: " + ", ".join(f"{n} ({sid})" for sid, n, _ in added))
    if removed:
        notes.append("atmosfere tolte o cambiate: " + ", ".join(f"{n} ({sid})" for sid, n, _ in removed))
    if children:
        notes += _child_notes(ea, eb)
    return notes


def _inputs(prog: Program, e) -> dict[tuple[str, str], str]:
    """Collegamenti in ingresso del blocco: (connettore, UUID del connettore sorgente) -> descrizione."""
    out = {}
    for co in e.findall("Co"):
        for i in co.findall("In"):
            src = prog.conn_owner.get(i.get("Input"))
            if src:
                out[(co.get("K"), i.get("Input"))] = (f"{co.get('K')} <- {prog.title(src[0])}.{src[1]} "
                                                      f"<{src[0].get('Type')}>")
    return out


def _moods(e) -> list[tuple[str, str, tuple]]:
    """Atmosfere di un controllo luci (LightController2): numero, nome e valori dei circuiti."""
    return [(m.get("SID"), m.get("Name"), tuple(sorted(m.attrib.items())))
            for m in e.iter("LightsceneC")] if e.get("Type") == "LightController2" else []


def alarm_sensors_only(a: Program, ea, b: Program, eb) -> bool:
    """Antifurto (Alarm): Objects cambia solo perche' si tolgono sensori che la finestra di configurazione del blocco
    collegava agli ingressi nascosti (HI1-HI4), e una presenza resta collegata come prima. E' una modifica della sola
    logica: LoxAPP3.json non elenca sensori e luci della configurazione e il record dei permessi contiene solo
    blocco, connettori e stati (verificato il 25/10/2026). Le luci d'allarme (alert) non si possono togliere cosi'."""
    if ea.get("Type") != "Alarm" or eb.get("Type") != "Alarm":
        return False
    before = {u for u in (ea.get("Objects") or "").split(",") if u}
    after = {u for u in (eb.get("Objects") or "").split(",") if u}
    removed = before - after
    if not removed or after - before:
        return False
    hidden = set()
    for co in ea.findall("Co"):
        if (co.get("K") or "").startswith("HI"):
            for i in co.findall("In"):
                owner = a.conn_owner.get(i.get("Input"))
                if owner and owner[0].get("Type") == "InputRef":
                    hidden.add(owner[0].get("Ref"))

    def presence(e) -> bool:
        return any(co.findall("In") for co in e.findall("Co") if co.get("K") in ("I1", "HI1"))
    return removed <= hidden and presence(ea) == presence(eb)


def diff(a: Program, b: Program, children: bool = True) -> dict:
    """Differenze tra due versioni. children=False ignora gli elementi interni dei blocchi (Display, testi...)."""
    ua = {e.get("U"): e for e in a.components() if e.get("U")}
    ub = {e.get("U"): e for e in b.components() if e.get("U")}
    changed = []
    for u in sorted(ua.keys() & ub.keys()):
        notes = _changes(a, ua[u], b, ub[u], children)
        if notes:
            changed.append({"oggetto": b.label(ub[u]), "modifiche": notes})
    return {"aggiunti": [b.label(ub[u]) for u in sorted(ub.keys() - ua.keys())],
            "rimossi": [a.label(ua[u]) for u in sorted(ua.keys() - ub.keys())],
            "modificati": changed}


def access_changes(a: Program, b: Program) -> list[str]:
    """Utenti, gruppi, permessi e trust diversi tra due versioni: solo tipo e quanti, mai i dati.

    Non compaiono nel confronto normale (sono privati), ma un caricamento li cambierebbe davvero."""
    def index(p: Program) -> dict:
        return {e.get("U"): e for e in p.root.iter("C") if e.get("Type") in ACCESS_TYPES and e.get("U")}
    ia, ib = index(a), index(b)
    found: collections.Counter = collections.Counter()
    for u in ia.keys() | ib.keys():
        if u not in ia:
            found[(ib[u].get("Type"), "aggiunti")] += 1
        elif u not in ib:
            found[(ia[u].get("Type"), "tolti")] += 1
        elif _canon(ia[u]) != _canon(ib[u]):
            found[(ia[u].get("Type"), "modificati")] += 1
    return [f"{kind}: {n} {what}" for (kind, what), n in sorted(found.items())]


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
        visual = [n for n in _changes(a, ua[u], b, ub[u]) if not n.startswith(LOGIC_ONLY)
                  and not (n.startswith("Objects:") and alarm_sensors_only(a, ua[u], b, ub[u]))]
        if visual:
            out.append(f"{b.label(ub[u])}: {'; '.join(visual)}")
    return out


# --- modifiche fatte dall'app, non ancora dentro il programma ---
# Quando qualcosa cambia dall'app (atmosfere di un controllo luci, orari, regole) o un telefono si registra per le
# notifiche, il Miniserver scrive l'oggetto intero in un file <uuid>.LoxCC della cartella /sys/custom (misurato il
# 22/11/2026, firmware 17.2 e 17.3). Al caricamento di un programma nuovo li cancella ("Remove old custom
# changes"); quando salva da se' il programma (dopo un aggiornamento del firmware) li ha gia' messi dentro.
VOLATILE_ATTRS = {"V", "WF", "SpStates"}    # cambiano a ogni salvataggio o conversione: non sono modifiche
VOLATILE_BY_TAG = {"HCL": {"FROM", "TO"}}   # alba e tramonto del giorno in cui il Miniserver ha salvato
UNORDERED_TAGS = {"In"}                     # l'ordine dei collegamenti di un connettore non conta
APP_REGISTRATIONS = {"PuDe"}                # telefoni registrati per le notifiche push: li ricrea l'app


def _canon(e) -> tuple:
    """Forma confrontabile di un oggetto del programma, senza cio' che cambia da solo a ogni salvataggio."""
    skip = VOLATILE_ATTRS | VOLATILE_BY_TAG.get(e.tag, set())
    kids = [_canon(c) for c in e]
    ordered = tuple(k for k in kids if k[0] not in UNORDERED_TAGS)
    unordered = tuple(sorted(k for k in kids if k[0] in UNORDERED_TAGS))
    return (e.tag, tuple(sorted((k, v) for k, v in e.attrib.items() if k not in skip)), (e.text or "").strip(),
            ordered + unordered)


def custom_change(xml: bytes, program: Program) -> tuple[str, str]:
    """Classifica un file della cartella custom rispetto al programma attuale: (esito, descrizione).

    "integrata" = l'oggetto nel programma e' gia' uguale; "registrazione" = telefono registrato per le notifiche
    push (l'app lo rifa' quando si ricollega); "in sospeso" = modifica che un caricamento farebbe perdere.
    """
    obj = ET.fromstring(xml.lstrip(b"\xef\xbb\xbf"))
    kind = obj.get("Type") or "?"
    label = f"{obj.get('Desc') or obj.get('Title') or '(senza nome)'} <{kind}>"
    current = next((e for e in program.root.iter("C") if e.get("U") == obj.get("U")), None)
    if current is not None and _canon(current) == _canon(obj):
        return "integrata", label
    children = [c for c in obj if c.tag == "C"]
    if kind in APP_REGISTRATIONS or (kind == "LoxCaption" and children
                                     and all(c.get("Type") in APP_REGISTRATIONS for c in children)):
        return "registrazione", "telefono registrato per le notifiche push"
    return "in sospeso", label + ("" if current is not None else " (creato dall'app)")


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
