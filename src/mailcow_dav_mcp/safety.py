"""Shared security checks: DAV path segments, local files, anti-SSRF photo download, ETag."""

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
    """Address book/calendar ID as a single path segment. Only letters, digits and . _ @ + -:
    no '/', '%' (encoded traversal), '?', '#', spaces or control characters."""
    seg = (name or "").strip().strip("/")
    if not _COLLECTION_RE.match(seg) or ".." in seg:
        raise ValueError(f"Invalid {kind} ID: {name!r} (use the id returned by list_addressbooks/list_calendars)")
    return seg


def resource_name(uid: str | None) -> str:
    """Resource name (without extension) for a new card: the UID if harmless, otherwise a new UUID.

    The original UID stays inside the card; only the file name on the server changes."""
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
    """If the caller passed the previously read ETag, the resource must not have changed in the meantime."""
    from .dav import PreconditionFailed

    if expected and norm_etag(expected) != norm_etag(current):
        raise PreconditionFailed(
            f"{what} was modified by another client after you read it (etag {expected} → {current}): "
            "read it again and reapply the changes."
        )


# -- file locali --------------------------------------------------------------------

_confine_dirs: list[Path] | None = None


def confine_local_files(directories: str | Path | list[str | Path] | None) -> None:
    """With one or more directories set, import/export/photos may only use files inside them.

    The server always enables it (stdio and HTTP): malicious text in a note must not be able to make it read
    or upload any file on the computer to the DAV server. None = no restriction (library use)."""
    global _confine_dirs
    if directories is None:
        _confine_dirs = None
        return
    items = directories if isinstance(directories, list) else [directories]
    _confine_dirs = [Path(d).expanduser().resolve() for d in items if str(d).strip()]


def confined_dir() -> Path | None:
    """Main allowed directory (base for relative paths and for exports), if confinement is active."""
    return _confine_dirs[0] if _confine_dirs else None


def local_path(raw: str) -> Path:
    """Resolve a user-supplied path; if confinement is active, restrict it to the allowed directories."""
    p = Path(os.path.expanduser(raw.strip()))
    if _confine_dirs is None:
        return p
    if not p.is_absolute():
        p = _confine_dirs[0] / p
    resolved = p.resolve()
    if not any(resolved == d or d in resolved.parents for d in _confine_dirs):
        allowed = ", ".join(str(d) for d in _confine_dirs)
        raise ValueError(f"Path not allowed: only files in {allowed} are accessible "
                         "(add folders with MAILCOW_DAV_LOCAL_DIRS)")
    return resolved


# -- foto ---------------------------------------------------------------------------

def _check_public_host(host: str, port: int) -> str:
    """Resolve the host and return a public IP; reject loopback, LAN, link-local, cloud metadata, etc."""
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as e:
        raise ValueError(f"Photo host cannot be resolved: {host}") from e
    ips = {info[4][0] for info in infos}
    if not ips:
        raise ValueError(f"Photo host cannot be resolved: {host}")
    for ip in ips:
        addr = ipaddress.ip_address(ip.split("%", 1)[0])
        if not addr.is_global or addr.is_multicast:
            raise ValueError(f"Photo URL rejected: {host} points to a non-public address ({addr})")
    return sorted(ips)[0]


def fetch_url_limited(url: str, max_bytes: int = PHOTO_MAX, timeout: float = 20.0) -> bytes:
    """http(s) GET to public IPs only, redirects checked one by one, body read in chunks with a limit.

    The connection goes to the already-verified IP (Host and SNI stay the original ones), so a DNS that changes
    its answer between check and connection (DNS rebinding) cannot lead into the internal network."""
    current = url
    with httpx.Client(timeout=timeout, follow_redirects=False) as client:
        for _ in range(_MAX_REDIRECTS + 1):
            parts = urlsplit(current)
            if parts.scheme not in ("http", "https") or not parts.hostname:
                raise ValueError(f"Invalid photo URL: {current}")
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
                        raise ValueError("Redirect without Location")
                    current = urljoin(current, location)
                    continue
                resp.raise_for_status()
                declared = resp.headers.get("Content-Length")
                if declared and declared.isdigit() and int(declared) > max_bytes:
                    raise ValueError(_too_big(int(declared), max_bytes))
                encoding = resp.headers.get("Content-Encoding", "identity").strip().lower()
                if encoding not in ("", "identity"):
                    raise ValueError(f"Photo URL rejected: unrequested compressed response ({encoding})")
                buf = bytearray()
                for chunk in resp.iter_raw():
                    buf.extend(chunk)
                    if len(buf) > max_bytes:
                        raise ValueError(_too_big(len(buf), max_bytes))
                return bytes(buf)
            finally:
                resp.close()
    raise ValueError(f"Too many redirects for {url}")


def _too_big(size: int, max_bytes: int) -> str:
    return f"Image too large (>{size // 1024} KB, max {max_bytes // 1024} KB): resize it first"


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
        raise ValueError(f"Image file not found: {raw}")
    size = path.stat().st_size
    if size > max_bytes:
        raise ValueError(_too_big(size, max_bytes))
    with path.open("rb") as fh:
        data = fh.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise ValueError(_too_big(len(data), max_bytes))
    return data
