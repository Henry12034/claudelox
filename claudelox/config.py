"""Configurazione: legge il file .env e definisce i percorsi del progetto."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# token e cache: file privati, mai da condividere. CLAUDELOX_STATE_DIR li sposta altrove per chi usa il
# pacchetto fuori dal toolkit (es. un servizio sul Raspberry con i suoi dati in /var/lib)
STATE_DIR = Path(os.environ.get("CLAUDELOX_STATE_DIR") or ROOT / ".state")
DATA_DIR = ROOT / "data"      # esportazioni (snapshot, programmi estratti, report)

DEFAULT_BACKUPS = Path.home() / "Documents" / "Loxone" / "Loxone Config" / "Backups"


SECRET_KEYS = ("LOXONE_PASSWORD", "LOXONE_ADMIN_PASSWORD")
_file_secrets: dict[str, str] = {}  # password del .env: fuori da os.environ, che ping, adb e Config erediterebbero


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
        if key in SECRET_KEYS:
            _file_secrets[key] = value
        else:
            os.environ.setdefault(key, value)


def _secret(key: str) -> str | None:
    return os.environ.get(key) or _file_secrets.get(key) or None


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
    # controlli in piu' che richiedono --conferma, scelti dall'utente (parte del nome o UUID): LOXONE_PROTETTI
    protected: tuple[str, ...] = ()

    def upload_creds(self) -> tuple[str | None, str | None]:
        """Utente e password per caricare: mai il nome di uno con la password dell'altro."""
        return (self.admin_user, self.admin_password) if self.admin_user else (self.user, self.password)


def get_settings() -> Settings:
    load_env()
    host = os.environ.get("LOXONE_HOST", "").strip()
    if not host:
        raise SystemExit("ERRORE: indirizzo del Miniserver mancante. Copia .env.example come .env e imposta "
                         "LOXONE_HOST (vedi README, \"Installazione\").")
    if "://" in host or "/" in host:
        raise SystemExit(f"ERRORE: LOXONE_HOST deve essere solo l'indirizzo del Miniserver (es. 192.168.1.10), "
                         f"senza http:// e senza barre: nel file .env c'e' '{host}'.")
    try:
        # 4 = token "app" (dura settimane, si rinnova da solo); 2 = token "web" (breve)
        permission = int(os.environ.get("LOXONE_TOKEN_PERMISSION") or "4")
    except ValueError:
        raise SystemExit("ERRORE: LOXONE_TOKEN_PERMISSION nel file .env deve essere un numero (4 oppure 2).")
    return Settings(
        host=host,
        user=os.environ.get("LOXONE_USER") or None,
        password=_secret("LOXONE_PASSWORD"),
        permission=permission,
        backups_dir=Path(os.environ.get("LOXONE_BACKUPS_DIR") or str(DEFAULT_BACKUPS)),
        admin_user=os.environ.get("LOXONE_ADMIN_USER") or None,
        admin_password=_secret("LOXONE_ADMIN_PASSWORD"),
        protected=tuple(p.strip() for p in os.environ.get("LOXONE_PROTETTI", "").split(",") if p.strip()),
    )
