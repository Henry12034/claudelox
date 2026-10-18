"""Configurazione: legge il file .env e definisce i percorsi del progetto."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = ROOT / ".state"   # token e cache: file privati, mai da condividere
DATA_DIR = ROOT / "data"      # esportazioni (snapshot, programmi estratti, report)

DEFAULT_BACKUPS = Path.home() / "Documents" / "Loxone" / "Loxone Config" / "Backups"


def load_env(path: Path = ROOT / ".env") -> None:
    """Carica KEY=VALUE dal file .env senza sovrascrivere variabili gia' impostate."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        os.environ.setdefault(key, value)


@dataclass
class Settings:
    host: str
    user: str | None
    password: str | None
    permission: int
    backups_dir: Path
    # utente con diritti FTP + Sys-WS, usato solo per CARICARE un programma (upload + riavvio logica).
    # Se non impostato, si prova l'utente normale (che potrebbe non avere i diritti).
    admin_user: str | None
    admin_password: str | None

    def upload_creds(self) -> tuple[str | None, str | None]:
        return (self.admin_user or self.user, self.admin_password or self.password)


def get_settings() -> Settings:
    load_env()
    host = os.environ.get("LOXONE_HOST", "").strip()
    if not host:
        raise SystemExit("ERRORE: indirizzo del Miniserver mancante. Copia .env.example come .env e imposta "
                         "LOXONE_HOST (vedi README, \"Installazione\").")
    return Settings(
        host=host,
        user=os.environ.get("LOXONE_USER") or None,
        password=os.environ.get("LOXONE_PASSWORD") or None,
        # 4 = token "app" (dura settimane, si rinnova da solo); 2 = token "web" (breve)
        permission=int(os.environ.get("LOXONE_TOKEN_PERMISSION", "4")),
        backups_dir=Path(os.environ.get("LOXONE_BACKUPS_DIR", str(DEFAULT_BACKUPS))),
        admin_user=os.environ.get("LOXONE_ADMIN_USER") or None,
        admin_password=os.environ.get("LOXONE_ADMIN_PASSWORD") or None,
    )
