"""Backup che Loxone Config salva sul PC (copia della scheda SD del Miniserver)."""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

from .loxcc import read_program

BACKUP_RE = re.compile(r"^Backup_(.+?)_(\d{14})_(\d{7,8})$")
PROGRAM_RE = re.compile(r"^sps_(\d+)_(\d{14})\.(zip|loxcc)$", re.I)


def firmware_from_code(code: str) -> str:
    """'16000610' -> '16.0.6.10' (codice versione nel nome della cartella di backup)."""
    n = int(code)
    return f"{n // 1000000}.{n // 10000 % 100}.{n // 100 % 100}.{n % 100}"


def same_host(a: str, b: str) -> bool:
    """Stesso Miniserver? Il nome di una cartella di backup contiene il suo indirizzo, senza porta."""
    return a.split(":")[0].lower() == b.split(":")[0].lower()


def list_backups(base: Path, host: str | None = None) -> list[dict]:
    """Backup di Loxone Config, dal piu' vecchio. Config tiene nella stessa cartella quelli di tutti gli impianti a
    cui si e' collegato: con `host` restano solo quelli di quel Miniserver."""
    out = []
    if base.is_dir():
        for d in base.iterdir():
            m = BACKUP_RE.match(d.name)
            if d.is_dir() and m and (host is None or same_host(m.group(1), host)):
                out.append({"path": d, "host": m.group(1),
                            "date": datetime.strptime(m.group(2), "%Y%m%d%H%M%S"),
                            "firmware": firmware_from_code(m.group(3))})
    return sorted(out, key=lambda b: b["date"])


def list_programs(backup_dir: Path) -> list[dict]:
    out = []
    prog = backup_dir / "prog"
    if prog.is_dir():
        for f in prog.iterdir():
            m = PROGRAM_RE.match(f.name)
            if m:
                out.append({"path": f, "version": int(m.group(1)),
                            "saved": datetime.strptime(m.group(2), "%Y%m%d%H%M%S")})
    return sorted(out, key=lambda p: p["version"])


def latest_program(base: Path, host: str | None = None) -> dict | None:
    """Ultima versione del programma presente nel backup piu' recente."""
    for b in reversed(list_backups(base, host)):
        progs = list_programs(b["path"])
        if progs:
            return {**progs[-1], "backup": b}
    return None


def downloaded_programs(folder: Path) -> list[dict]:
    """Programmi scaricati dal Miniserver con 'lox programma scarica'."""
    out = []
    if folder.is_dir():
        for f in folder.iterdir():
            m = PROGRAM_RE.match(f.name)
            if m:
                out.append({"path": f, "version": int(m.group(1)),
                            "saved": datetime.strptime(m.group(2), "%Y%m%d%H%M%S"),
                            "origine": "scaricato dal Miniserver"})
    return out


def newest_program(base: Path, downloads: Path, host: str | None = None) -> dict | None:
    """Versione piu' recente tra quelle scaricate e quelle nei backup di Loxone Config (di `host`, se indicato)."""
    candidates = downloaded_programs(downloads)
    for b in list_backups(base, host):
        candidates += [{**p, "origine": f"backup di Config del {b['date']:%d/%m/%Y}"} for p in list_programs(b["path"])]
    return max(candidates, key=lambda p: (p["saved"], p["version"])) if candidates else None


def latest_structure(base: Path, downloads: Path | None = None, host: str | None = None) -> tuple[dict, dict] | None:
    """LoxAPP3.json contenuto nell'ultimo programma salvato: (struttura, info programma)."""
    p = newest_program(base, downloads, host) if downloads else latest_program(base, host)
    if not p or p["path"].suffix.lower() != ".zip":
        return None
    _, app = read_program(p["path"])
    return (json.loads(app.decode("utf-8-sig")), p) if app else None
