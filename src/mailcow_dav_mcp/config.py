"""Configurazione da variabili d'ambiente (o da un file .env indicato da MAILCOW_DAV_ENV_FILE)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _project_env() -> Path:
    """Il .env nella cartella del progetto (utile con un clone; con uvx non esiste)."""
    return Path(__file__).resolve().parents[2] / ".env"


def _load_dotenv() -> None:
    """Carica un .env minimale senza dipendenze esterne. Le variabili già presenti vincono.

    Fonti: MAILCOW_DAV_ENV_FILE (se impostata il file deve esistere: niente ripiego silenzioso) oppure il .env
    nella cartella del progetto (clone). Mai la cartella corrente: un repo qualsiasi aperto nel client potrebbe
    contenerne uno ostile. Solo chiavi MAILCOW_DAV_*: un .env non può impostare proxy o altre variabili."""
    explicit = os.environ.get("MAILCOW_DAV_ENV_FILE", "").strip()
    if explicit:
        path = Path(explicit).expanduser()
        if not path.is_file():
            raise RuntimeError(f"MAILCOW_DAV_ENV_FILE punta a un file inesistente: {path}")
    else:
        path = _project_env()
        if not path.is_file():
            return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not key.startswith("MAILCOW_DAV_"):
            continue
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


@dataclass(frozen=True)
class Settings:
    base_url: str          # es. https://mail.esempio.it
    user: str              # mailbox, es. utente@esempio.it
    password: str          # app password mailcow (DAV/SOGo)
    principal_path: str    # es. /SOGo/dav/utente@esempio.it/
    timezone: str = "Europe/Rome"
    verify_ssl: bool = True
    timeout: float = 30.0

    @classmethod
    def from_env(cls) -> "Settings":
        _load_dotenv()
        base_url = os.environ.get("MAILCOW_DAV_URL", "").rstrip("/")
        user = os.environ.get("MAILCOW_DAV_USER", "")
        password = os.environ.get("MAILCOW_DAV_PASSWORD", "")
        missing = [k for k, v in {
            "MAILCOW_DAV_URL": base_url,
            "MAILCOW_DAV_USER": user,
            "MAILCOW_DAV_PASSWORD": password,
        }.items() if not v]
        if missing:
            raise RuntimeError(
                "Variabili mancanti: " + ", ".join(missing)
                + ". Vedi .env.example."
            )
        principal = os.environ.get("MAILCOW_DAV_PRINCIPAL_PATH") or f"/SOGo/dav/{user}/"
        if not principal.endswith("/"):
            principal += "/"
        verify = os.environ.get("MAILCOW_DAV_VERIFY_SSL", "1").lower() not in ("0", "false", "no")
        return cls(
            base_url=base_url,
            user=user,
            password=password,
            principal_path=principal,
            timezone=os.environ.get("MAILCOW_DAV_TIMEZONE", "Europe/Rome"),
            verify_ssl=verify,
            timeout=float(os.environ.get("MAILCOW_DAV_TIMEOUT", "30")),
        )
