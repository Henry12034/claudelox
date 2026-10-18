"""Indice del file di struttura LoxAPP3.json: stanze, categorie, controlli, stati."""
from __future__ import annotations

import json
import re
import unicodedata

# Controlli legati alla sicurezza di persone e casa: ogni comando richiede --conferma
# (e Claude deve prima chiedere il permesso esplicito all'utente).
SENSITIVE_TYPES = {
    "Alarm", "AlarmChain", "CentralAlarm", "AalSmartAlarm", "AalEmergency", "SmokeAlarm",
    "Gate", "CentralGate", "Window", "CentralWindow", "Intercom", "IntercomV2", "NfcCodeTouch",
}

READ_ONLY_TYPES = {"InfoOnlyAnalog", "InfoOnlyDigital", "InfoOnlyText", "TextState", "Webpage"}

# Comandi documentati da Loxone ("Structure File" v16.0) per i tipi piu' comuni
COMMAND_HINTS = {
    "Switch": ["on", "off"],
    "Pushbutton": ["on", "off", "pulse"],
    "TimedSwitch": ["on", "off", "pulse"],
    "Dimmer": ["on", "off", "<valore 0-100>"],
    "EIBDimmer": ["on", "off", "<valore 0-100>"],
    "LightControllerV2": ["changeTo/<idScena>", "addMood/<idScena>", "removeMood/<idScena>", "plus", "minus"],
    "LightsceneRGB": ["on", "off", "<numeroScena>"],
    "CentralLightController": ["on", "reset"],
    "TextInput": ["<testo>"],
}

UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{16}$", re.I)


def norm(text: str) -> str:
    """Minuscole, senza accenti e spazi multipli: 'Luminosità  Bagno' -> 'luminosita bagno'."""
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return " ".join(text.lower().replace("_", " ").split())


class Structure:
    def __init__(self, data: dict):
        self.data = data
        self.ms = data.get("msInfo", {})
        self.rooms = {u: r.get("name", "?") for u, r in (data.get("rooms") or {}).items()}
        self.cats = {u: c.get("name", "?") for u, c in (data.get("cats") or {}).items()}
        self.controls: dict[str, dict] = {}
        self.state_owner: dict[str, tuple[str, str]] = {}
        for uid, c in (data.get("controls") or {}).items():
            self._add(uid, c, None)

    def _add(self, uid: str, c: dict, parent: str | None) -> None:
        c = dict(c)
        c["_uuid"], c["_parent"] = uid, parent
        self.controls[uid] = c
        for name, su in (c.get("states") or {}).items():
            for s in (su if isinstance(su, list) else [su]):
                self.state_owner[s] = (uid, name)
        for sub_uid, sub in (c.get("subControls") or {}).items():
            self._add(sub_uid, sub, uid)

    @property
    def last_modified(self) -> str:
        return self.data.get("lastModified", "?")

    def top_level(self) -> list[dict]:
        return [c for c in self.controls.values() if c["_parent"] is None]

    def room_name(self, c: dict) -> str:
        while c.get("room") is None and c["_parent"]:
            c = self.controls[c["_parent"]]
        return self.rooms.get(c.get("room"), "-")

    def cat_name(self, c: dict) -> str:
        while c.get("cat") is None and c["_parent"]:
            c = self.controls[c["_parent"]]
        return self.cats.get(c.get("cat"), "-")

    def label(self, c: dict) -> str:
        parent = f" (sotto '{self.controls[c['_parent']]['name']}')" if c["_parent"] else ""
        return f"{c['name']} [{self.room_name(c)}] <{c['type']}>{parent}"

    def find(self, text: str | None = None, room: str | None = None, type_: str | None = None,
             cat: str | None = None, include_sub: bool = False) -> list[dict]:
        out = []
        for c in self.controls.values():
            if c["_parent"] and not include_sub:
                continue
            if text and norm(text) not in norm(c["name"]):
                continue
            if room and norm(room) not in norm(self.room_name(c)):
                continue
            if cat and norm(cat) not in norm(self.cat_name(c)):
                continue
            if type_ and norm(type_) != norm(c["type"]):
                continue
            out.append(c)
        return sorted(out, key=lambda c: (self.room_name(c), c["name"]))

    def resolve(self, ref: str, room: str | None = None) -> dict:
        """Trova un controllo per UUID, nome esatto o parte del nome ("Stanza/Nome" ammesso)."""
        ref = ref.strip()
        for c in self.controls.values():  # UUID, anche di sotto-controlli ("<uuid>/AI1")
            if ref.lower() in (c["_uuid"].lower(), str(c.get("uuidAction", "")).lower()):
                return c
        if UUID_RE.match(ref):
            raise LookupError(f"nessun controllo con UUID {ref}")
        if "/" in ref and room is None:  # "Stanza/Nome": la stanza puo' contenere "/" (es. "Sala/cucina")
            parts = ref.split("/")
            splits = [("/".join(parts[:i]), "/".join(parts[i:])) for i in range(len(parts) - 1, 0, -1)]
            exact = [sp for sp in splits if any(norm(sp[0]) == norm(r) for r in self.rooms.values())]
            loose = [sp for sp in splits if any(norm(sp[0]) in norm(r) for r in self.rooms.values())]
            if exact or loose:
                room, ref = (exact or loose)[0]
        pool = [c for c in self.controls.values() if not room or norm(room) in norm(self.room_name(c))]
        for matcher in (lambda c: norm(c["name"]) == norm(ref), lambda c: norm(ref) in norm(c["name"])):
            hits = [c for c in pool if matcher(c)]
            top = [c for c in hits if c["_parent"] is None]
            hits = top if top else hits
            if len(hits) == 1:
                return hits[0]
            if len(hits) > 1:
                lines = "\n".join(f"  - {self.label(c)}  uuid={c['_uuid']}" for c in hits[:25])
                raise LookupError(f"'{ref}' e' ambiguo, specifica la stanza (--stanza o \"Stanza/Nome\") "
                                  f"oppure usa l'UUID:\n{lines}")
        raise LookupError(f"nessun controllo trovato per '{ref}'" + (f" nella stanza '{room}'" if room else ""))

    def states_of(self, c: dict, values: dict, texts: dict) -> dict:
        out = {}
        for name, su in (c.get("states") or {}).items():
            uids = su if isinstance(su, list) else [su]
            vals = [texts.get(u, values.get(u)) for u in uids]
            out[name] = vals if isinstance(su, list) else vals[0]
        return out


def _json(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def _fmt_number(value: float, fmt: str | None) -> str:
    if fmt:
        try:
            return fmt % value
        except (TypeError, ValueError):
            pass
    return f"{value:g}"


def summarize(c: dict, st: dict) -> str:
    """Descrizione leggibile dello stato di un controllo."""
    t, d = c.get("type"), c.get("details") or {}
    if all(v is None for v in st.values()) and st:
        return "(nessun valore ricevuto)"
    if t in ("Switch", "Pushbutton", "TimedSwitch"):
        return "ACCESO" if st.get("active") else "spento"
    if t in ("Dimmer", "EIBDimmer"):
        pos = st.get("position") or 0
        return f"{pos:g}%" if pos else "spento (0%)"
    if t == "InfoOnlyAnalog" and isinstance(st.get("value"), (int, float)):
        return _fmt_number(st["value"], d.get("format"))
    if t == "InfoOnlyDigital":
        texts = d.get("text") or {}
        return texts.get("on", "attivo") if st.get("active") else texts.get("off", "non attivo")
    if t in ("InfoOnlyText", "TextInput"):
        return str(st.get("text", ""))
    if t == "TextState":
        return str(st.get("textAndIcon", ""))
    if t == "LightControllerV2":
        active = _json(st.get("activeMoods")) or []
        moods = {m.get("id"): m.get("name") for m in (_json(st.get("moodList")) or []) if isinstance(m, dict)}
        names = [moods.get(i, str(i)) for i in active] if isinstance(active, list) else [str(active)]
        return "scene attive: " + (", ".join(names) if names else "nessuna")
    if t == "LightsceneRGB":
        return f"scena {st.get('activeScene')}, colore {st.get('color')}"
    if t == "Alarm":
        return ("INSERITO" if st.get("armed") else "disinserito") + f", livello {st.get('level')}"
    compact = {k: v for k, v in st.items() if k != "jLocked"}
    return json.dumps(compact, ensure_ascii=False)[:300]


def mood_list(st: dict) -> list[dict]:
    return [m for m in (_json(st.get("moodList")) or []) if isinstance(m, dict)]
