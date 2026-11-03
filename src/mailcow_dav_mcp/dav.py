"""Client WebDAV minimale (PROPFIND / REPORT / GET / PUT / DELETE) su httpx + lxml."""

from __future__ import annotations

import os
import posixpath
import zlib
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Iterator
from urllib.parse import unquote, urlsplit

import httpx
from lxml import etree

NS_D = "DAV:"
NS_CARD = "urn:ietf:params:xml:ns:carddav"
NS_CAL = "urn:ietf:params:xml:ns:caldav"
NS_CS = "http://calendarserver.org/ns/"
NS_APPLE = "http://apple.com/ns/ical/"

NSMAP = {"D": NS_D, "C": NS_CARD, "CAL": NS_CAL, "CS": NS_CS, "A": NS_APPLE}


# Tetto sulle risposte del server (byte dopo la decompressione): una risposta ostile o enorme non esaurisce la RAM.
def max_response() -> int:
    return int(os.environ.get("MAILCOW_DAV_MAX_RESPONSE_MB", "64")) * 1024 * 1024


class DavError(RuntimeError):
    pass


class NotFound(DavError):
    pass


class PreconditionFailed(DavError):
    """ETag non corrispondente: la risorsa è cambiata nel frattempo."""


@dataclass
class Resource:
    href: str
    props: dict[str, etree._Element] = field(default_factory=dict)  # chiave "{ns}name"

    def text(self, ns: str, name: str) -> str | None:
        el = self.props.get(f"{{{ns}}}{name}")
        if el is None:
            return None
        return (el.text or "").strip() if el.text is not None else ""

    def has_child(self, ns: str, name: str, child_ns: str, child_name: str) -> bool:
        el = self.props.get(f"{{{ns}}}{name}")
        return el is not None and el.find(f"{{{child_ns}}}{child_name}") is not None

    @property
    def etag(self) -> str | None:
        return self.text(NS_D, "getetag")

    @property
    def name(self) -> str:
        """Ultimo segmento del path (senza slash finale)."""
        return unquote(self.href.rstrip("/").rsplit("/", 1)[-1])


class DavClient:
    def __init__(self, base_url: str, user: str, password: str, *, verify: bool = True, timeout: float = 30.0):
        self.base_url = base_url.rstrip("/")
        self._origin = _origin(urlsplit(self.base_url))
        self._client = httpx.Client(
            base_url=self.base_url,
            auth=(user, password),
            verify=verify,
            timeout=timeout,
            headers={"User-Agent": "mailcow-dav-mcp/0.2", "Accept-Encoding": "gzip, deflate"},
            follow_redirects=False,
        )

    # -- basso livello --------------------------------------------------

    @staticmethod
    def safe_path(path: str) -> str:
        """Solo un path assoluto sul server configurato: niente URL completi (porterebbero le credenziali Basic
        verso un altro host), niente '//' iniziale, niente segmenti '..' (anche codificati)."""
        if not path.startswith("/") or path.startswith("//") or "\\" in path:
            raise DavError(f"Path DAV non valido: {path!r}")
        segments = unquote(path).split("/")
        if any(seg in ("..", ".") for seg in segments) or any(ord(c) < 32 for c in unquote(path)):
            raise DavError(f"Path DAV non valido: {path!r}")
        return path

    def _href_path(self, raw: str) -> str:
        """href restituito dal server → path locale. Un href assoluto è accettato solo se punta allo stesso
        schema/host/porta della configurazione; altrimenti è un errore (server o proxy ostile)."""
        parts = urlsplit(raw)
        if parts.scheme or parts.netloc:
            if _origin(parts) != self._origin:
                raise DavError(f"Risposta DAV con href verso un altro host: {raw!r}")
        return self.safe_path(parts.path or "/")

    @staticmethod
    def _within(href: str, base: str) -> bool:
        h, b = posixpath.normpath(unquote(href)), posixpath.normpath(unquote(base))
        return h == b or h.startswith(b.rstrip("/") + "/")

    def request(self, method: str, path: str, *, content: bytes | str | None = None,
                headers: dict[str, str] | None = None, ok: Iterable[int] = (200, 201, 204, 207)) -> httpx.Response:
        req = self._client.build_request(method, self.safe_path(path), content=content, headers=headers or {})
        raw = self._client.send(req, stream=True)
        try:
            declared = raw.headers.get("Content-Length")
            limit = max_response()
            if declared and declared.isdigit() and int(declared) > limit:
                raise DavError(f"{method} {path}: risposta troppo grande ({int(declared) // 1048576} MB, "
                               f"max {limit // 1048576} MB: MAILCOW_DAV_MAX_RESPONSE_MB)")
            buf = bytearray()
            decoder = _decoder(raw.headers.get("Content-Encoding", ""))
            for chunk in raw.iter_raw():  # decompressione a pezzi limitati: niente picchi di memoria (gzip bomb)
                for piece in decoder(chunk, limit + 1 - len(buf)):
                    buf.extend(piece)
                    if len(buf) > limit:
                        raise DavError(f"{method} {path}: risposta oltre {limit // 1048576} MB "
                                       "(MAILCOW_DAV_MAX_RESPONSE_MB)")
        finally:
            raw.close()
        hdrs = [(k, v) for k, v in raw.headers.multi_items() if k.lower() not in ("content-encoding", "content-length")]
        r = httpx.Response(raw.status_code, headers=hdrs, content=bytes(buf), request=req)
        if r.status_code == 404:
            raise NotFound(f"{method} {path}: 404 Not Found")
        if r.status_code == 412:
            raise PreconditionFailed(f"{method} {path}: 412 (ETag non corrispondente, risorsa modificata da altri)")
        if r.status_code in (401, 403):
            raise DavError(f"{method} {path}: {r.status_code} — credenziali errate o app password senza permesso DAV/SOGo")
        if r.status_code not in ok:
            snippet = r.text[:300].replace("\n", " ")
            raise DavError(f"{method} {path}: HTTP {r.status_code} {snippet}")
        return r

    def parse_multistatus(self, content: bytes, within: str | None = None) -> list[Resource]:
        """Parsing rigoroso: un XML troncato o malformato è un errore, non un elenco parziale (un backup
        incompleto non deve sembrare riuscito). Niente entità esterne né accesso alla rete."""
        parser = etree.XMLParser(recover=False, huge_tree=False, resolve_entities=False, no_network=True)
        try:
            root = etree.fromstring(content, parser=parser)
        except etree.XMLSyntaxError as e:
            raise DavError(f"Risposta DAV non valida (XML malformato o troncato): {e}") from e
        if root is None or root.tag != f"{{{NS_D}}}multistatus":
            raise DavError(f"Risposta DAV inattesa: atteso multistatus, ricevuto {getattr(root, 'tag', None)!r}")
        out: list[Resource] = []
        for resp in root.iter(f"{{{NS_D}}}response"):
            href_el = resp.find(f"{{{NS_D}}}href")
            if href_el is None or not href_el.text:
                continue
            href = self._href_path(href_el.text.strip())
            if within is not None and not self._within(href, within):
                raise DavError(f"Risposta DAV con href fuori da {within}: {href!r}")
            res = Resource(href=href)
            for propstat in resp.findall(f"{{{NS_D}}}propstat"):
                status = propstat.findtext(f"{{{NS_D}}}status") or ""
                if " 200 " not in f" {status} " and "200" not in status:
                    continue
                prop = propstat.find(f"{{{NS_D}}}prop")
                if prop is None:
                    continue
                for el in prop:
                    if isinstance(el.tag, str):
                        res.props[el.tag] = el
            out.append(res)
        return out

    def propfind(self, path: str, props: list[tuple[str, str]], depth: int = 1) -> list[Resource]:
        root = etree.Element(f"{{{NS_D}}}propfind", nsmap=NSMAP)
        prop = etree.SubElement(root, f"{{{NS_D}}}prop")
        for ns, name in props:
            etree.SubElement(prop, f"{{{ns}}}{name}")
        body = etree.tostring(root, xml_declaration=True, encoding="utf-8")
        r = self.request("PROPFIND", path, content=body,
                         headers={"Depth": str(depth), "Content-Type": "application/xml; charset=utf-8"})
        return self.parse_multistatus(r.content, within=path)

    def report(self, path: str, body: bytes, depth: int = 1) -> list[Resource]:
        r = self.request("REPORT", path, content=body,
                         headers={"Depth": str(depth), "Content-Type": "application/xml; charset=utf-8"})
        return self.parse_multistatus(r.content, within=path)

    def get(self, path: str) -> tuple[str, str | None]:
        r = self.request("GET", path)
        return r.text, r.headers.get("ETag")

    def put(self, path: str, body: str, content_type: str, *, etag: str | None = None, create: bool = False) -> str | None:
        """PUT condizionato: `create` usa If-None-Match: *, altrimenti serve l'ETag (If-Match).

        Senza ETag rifiuto la scrittura: sovrascriverei alla cieca eventuali modifiche concorrenti."""
        headers = {"Content-Type": content_type}
        if create:
            headers["If-None-Match"] = "*"
        elif etag:
            headers["If-Match"] = etag
        else:
            raise DavError(f"PUT {path}: ETag sconosciuto, rifiuto una sovrascrittura non condizionata")
        r = self.request("PUT", path, content=body.encode("utf-8"), headers=headers, ok=(200, 201, 204))
        return r.headers.get("ETag")

    def delete(self, path: str, *, etag: str | None) -> None:
        if not etag:
            raise DavError(f"DELETE {path}: ETag sconosciuto, rifiuto una cancellazione non condizionata")
        self.request("DELETE", path, headers={"If-Match": etag}, ok=(200, 202, 204))

    def close(self) -> None:
        self._client.close()


_DEFAULT_PORT = {"http": 80, "https": 443}


def _origin(parts: Any) -> tuple[str, str, int | None]:
    scheme = parts.scheme.lower()
    return scheme, (parts.hostname or "").lower(), parts.port or _DEFAULT_PORT.get(scheme)


def _decoder(encoding: str) -> Callable[[bytes, int], Iterator[bytes]]:
    """Decompressione incrementale con tetto: ogni pezzo prodotto è al massimo `room` byte (+64 KB)."""
    enc = encoding.strip().lower()
    if enc in ("", "identity"):
        return lambda chunk, room: iter([chunk])
    if enc not in ("gzip", "x-gzip", "deflate"):
        raise DavError(f"Codifica della risposta non supportata: {encoding!r}")
    state: dict[str, Any] = {"d": zlib.decompressobj(zlib.MAX_WBITS | 32)}  # gzip o zlib, autodetect

    def run(chunk: bytes, room: int) -> Iterator[bytes]:
        d = state["d"]
        data = chunk
        while data:
            try:
                out = d.decompress(data, max(1, min(room, 1 << 20)) + 65536)
            except zlib.error:
                if enc != "deflate" or state.get("raw"):
                    raise DavError("Risposta compressa non valida") from None
                state["d"], state["raw"] = zlib.decompressobj(-zlib.MAX_WBITS), True  # deflate "nudo"
                d, data = state["d"], chunk
                continue
            yield out
            room -= len(out)
            if room <= 0:
                return
            data = d.unconsumed_tail
    return run


def multiget_xml(ns_root: str, root_name: str, data_ns: str, data_name: str, hrefs: list[str],
                 data_attrs: dict[str, str] | None = None) -> bytes:
    """Costruisce un addressbook-multiget / calendar-multiget."""
    root = etree.Element(f"{{{ns_root}}}{root_name}", nsmap=NSMAP)
    prop = etree.SubElement(root, f"{{{NS_D}}}prop")
    etree.SubElement(prop, f"{{{NS_D}}}getetag")
    etree.SubElement(prop, f"{{{data_ns}}}{data_name}", **(data_attrs or {}))
    for h in hrefs:
        etree.SubElement(root, f"{{{NS_D}}}href").text = h
    return etree.tostring(root, xml_declaration=True, encoding="utf-8")
