"""Analisi del log di sistema del Miniserver (def.log)."""
from __future__ import annotations

import collections
import re
from dataclasses import dataclass
from datetime import datetime

LINE_RE = re.compile(r"^\ufeff?(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})(?:\.\d+)?;(.*)$")
EVENT_RE = re.compile(r"^(Warning|Important|Critical|Error|Fatal|Info)\s+(\d+)(\s+QUITTED)?,\s*(.*)$")
TAIL_UUID_RE = re.compile(r"\s*\([^()]*[0-9a-f]{8}-[0-9a-f]{4}-[^()]*\)\s*$", re.I)


@dataclass
class Entry:
    ts: datetime
    text: str
    level: str | None = None
    code: int | None = None
    quitted: bool = False
    message: str = ""


def parse(text: str) -> list[Entry]:
    entries: list[Entry] = []
    for line in text.splitlines():
        m = LINE_RE.match(line)
        if not m:
            if entries and line.strip():  # riga di continuazione (es. errori di script)
                entries[-1].text += "\n" + line.rstrip()
            continue
        ts = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
        body = m.group(2).strip()
        e = Entry(ts, body)
        ev = EVENT_RE.match(body)
        if ev:
            e.level, e.code, e.quitted = ev.group(1), int(ev.group(2)), bool(ev.group(3))
            e.message = TAIL_UUID_RE.sub("", ev.group(4))
        entries.append(e)
    return entries


def summarize(entries: list[Entry], since: datetime | None = None) -> dict:
    sel = [e for e in entries if since is None or e.ts >= since]
    groups: dict[tuple, dict] = {}
    for e in sel:
        if not e.level:
            continue
        key = (e.level, e.code, e.message)
        g = groups.setdefault(key, {"livello": e.level, "codice": e.code, "messaggio": e.message,
                                    "volte": 0, "prima": e.ts, "ultima": e.ts, "risolto": False,
                                    "con_chiusura": False})
        if not e.quitted:
            g["volte"] += 1
        g["ultima"] = max(g["ultima"], e.ts)
        g["risolto"] = e.quitted  # stato dell'ultima occorrenza
        g["con_chiusura"] = g["con_chiusura"] or e.quitted
    starts = [e for e in sel if e.text.startswith("PRG Start")]
    other = collections.Counter(" ".join(e.text.split()[:3]) for e in sel if not e.level)
    script_errors = [e for e in sel if "\n" in e.text and ("error" in e.text.lower() or "^" in e.text)]
    rank = {"Fatal": 0, "Critical": 1, "Error": 2, "Important": 3, "Warning": 4, "Info": 5}
    return {
        "periodo": (sel[0].ts, sel[-1].ts) if sel else None,
        "righe": len(sel),
        "eventi": sorted(groups.values(), key=lambda g: (rank.get(g["livello"], 9), -g["volte"])),
        "avvii": [(e.ts, e.text) for e in starts],
        "errori_script": [(e.ts, e.text) for e in script_errors],
        "altri_messaggi": other.most_common(12),
    }
