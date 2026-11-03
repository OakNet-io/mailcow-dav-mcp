"""Controlli di sicurezza condivisi: segmenti di path DAV, file locali, download foto anti-SSRF, ETag."""

from __future__ import annotations

import base64
import ipaddress
import os
import re
import socket
import uuid
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx

PHOTO_MAX = 2 * 1024 * 1024
_MAX_REDIRECTS = 3
_SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@+-]{0,199}$")
_COLLECTION_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._@+-]{0,199}$")


# -- path DAV ---------------------------------------------------------------------

def collection_segment(name: str, kind: str = "collection") -> str:
    """ID di rubrica/calendario come singolo segmento di path. Solo lettere, cifre e . _ @ + -:
    niente '/', '%' (traversal codificato), '?', '#', spazi o caratteri di controllo."""
    seg = (name or "").strip().strip("/")
    if not _COLLECTION_RE.match(seg) or ".." in seg:
        raise ValueError(f"ID {kind} non valido: {name!r} (usa l'id restituito da list_addressbooks/list_calendars)")
    return seg


def resource_name(uid: str | None) -> str:
    """Nome risorsa (senza estensione) per una nuova scheda: l'UID se innocuo, altrimenti un UUID nuovo.

    L'UID resta quello originale dentro la scheda; cambia solo il nome del file sul server."""
    if uid and _SAFE_NAME_RE.match(uid) and ".." not in uid:
        return uid
    return str(uuid.uuid4())


def is_safe_name(uid: str) -> bool:
    return bool(uid) and bool(_SAFE_NAME_RE.match(uid)) and ".." not in uid


# -- ETag ---------------------------------------------------------------------------

def norm_etag(etag: str | None) -> str:
    v = (etag or "").strip()
    if v.startswith("W/"):
        v = v[2:]
    return v.strip('"')


def check_expected_etag(expected: str | None, current: str | None, what: str) -> None:
    """Se il chiamante ha passato l'ETag letto in precedenza, la risorsa non deve essere cambiata nel frattempo."""
    from .dav import PreconditionFailed

    if expected and norm_etag(expected) != norm_etag(current):
        raise PreconditionFailed(
            f"{what} è stato modificato da un altro client dopo la tua lettura (etag {expected} → {current}): "
            "rileggilo e riapplica le modifiche."
        )


# -- file locali --------------------------------------------------------------------

_confine_dir: Path | None = None


def confine_local_files(directory: str | Path | None) -> None:
    """Con una directory impostata (modalità HTTP), import/export/foto possono usare solo file al suo interno."""
    global _confine_dir
    _confine_dir = Path(directory).expanduser().resolve() if directory else None


def confined_dir() -> Path | None:
    return _confine_dir


def local_path(raw: str) -> Path:
    """Risolve un path fornito dall'utente; se è attivo il confinamento lo vincola alla directory consentita."""
    p = Path(os.path.expanduser(raw.strip()))
    if _confine_dir is None:
        return p
    if not p.is_absolute():
        p = _confine_dir / p
    resolved = p.resolve()
    if resolved != _confine_dir and _confine_dir not in resolved.parents:
        raise ValueError(f"Path non consentito: in modalità HTTP sono accessibili solo i file in {_confine_dir}")
    return resolved


# -- foto ---------------------------------------------------------------------------

def _check_public_host(host: str, port: int) -> str:
    """Risolve l'host e ritorna un IP pubblico; rifiuta loopback, LAN, link-local, metadata cloud, ecc."""
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as e:
        raise ValueError(f"Host foto non risolvibile: {host}") from e
    ips = {info[4][0] for info in infos}
    if not ips:
        raise ValueError(f"Host foto non risolvibile: {host}")
    for ip in ips:
        addr = ipaddress.ip_address(ip.split("%", 1)[0])
        if not addr.is_global or addr.is_multicast:
            raise ValueError(f"URL foto rifiutato: {host} punta a un indirizzo non pubblico ({addr})")
    return sorted(ips)[0]


def fetch_url_limited(url: str, max_bytes: int = PHOTO_MAX, timeout: float = 20.0) -> bytes:
    """GET http(s) verso soli IP pubblici, redirect verificati uno per uno, corpo letto a blocchi con limite.

    La connessione va all'IP già verificato (Host e SNI restano quelli originali), così un DNS che cambia
    risposta tra controllo e connessione (DNS rebinding) non può portare verso la rete interna."""
    current = url
    with httpx.Client(timeout=timeout, follow_redirects=False) as client:
        for _ in range(_MAX_REDIRECTS + 1):
            parts = urlsplit(current)
            if parts.scheme not in ("http", "https") or not parts.hostname:
                raise ValueError(f"URL foto non valido: {current}")
            port = parts.port or (443 if parts.scheme == "https" else 80)
            ip = _check_public_host(parts.hostname, port)
            ip_host = f"[{ip}]" if ":" in ip else ip
            target = parts._replace(netloc=f"{ip_host}:{port}").geturl()
            name = f"[{parts.hostname}]" if ":" in parts.hostname else parts.hostname
            host_header = name if parts.port is None else f"{name}:{parts.port}"
            # identity: il limite deve valere sui byte ricevuti, non dopo una decompressione (gzip bomb)
            req = client.build_request("GET", target, headers={"Host": host_header, "Accept-Encoding": "identity"},
                                       extensions={"sni_hostname": parts.hostname})
            resp = client.send(req, stream=True)
            try:
                if resp.is_redirect:
                    location = resp.headers.get("Location")
                    if not location:
                        raise ValueError("Redirect senza Location")
                    current = urljoin(current, location)
                    continue
                resp.raise_for_status()
                declared = resp.headers.get("Content-Length")
                if declared and declared.isdigit() and int(declared) > max_bytes:
                    raise ValueError(_too_big(int(declared), max_bytes))
                encoding = resp.headers.get("Content-Encoding", "identity").strip().lower()
                if encoding not in ("", "identity"):
                    raise ValueError(f"URL foto rifiutato: risposta compressa ({encoding}) non richiesta")
                buf = bytearray()
                for chunk in resp.iter_raw():
                    buf.extend(chunk)
                    if len(buf) > max_bytes:
                        raise ValueError(_too_big(len(buf), max_bytes))
                return bytes(buf)
            finally:
                resp.close()
    raise ValueError(f"Troppi redirect per {url}")


def _too_big(size: int, max_bytes: int) -> str:
    return f"Immagine troppo grande (>{size // 1024} KB, max {max_bytes // 1024} KB): ridimensionala prima"


def decode_data_uri(src: str, max_bytes: int = PHOTO_MAX) -> bytes:
    header, _, payload = src.partition(",")
    if ";base64" in header:
        if len(payload) > (max_bytes * 4) // 3 + 8:
            raise ValueError(_too_big(len(payload) * 3 // 4, max_bytes))
        data = base64.b64decode(payload)
    else:
        if len(payload) > max_bytes:
            raise ValueError(_too_big(len(payload), max_bytes))
        data = payload.encode()
    if len(data) > max_bytes:
        raise ValueError(_too_big(len(data), max_bytes))
    return data


def read_local_limited(raw: str, max_bytes: int = PHOTO_MAX) -> bytes:
    path = local_path(raw)
    if not path.is_file():
        raise ValueError(f"File immagine non trovato: {raw}")
    size = path.stat().st_size
    if size > max_bytes:
        raise ValueError(_too_big(size, max_bytes))
    with path.open("rb") as fh:
        data = fh.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise ValueError(_too_big(len(data), max_bytes))
    return data
