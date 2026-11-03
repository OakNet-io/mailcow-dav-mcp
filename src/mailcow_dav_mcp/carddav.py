"""Rubrica CardDAV (SOGo)."""

from __future__ import annotations

import re
import threading
import unicodedata
import uuid
from typing import Any
from urllib.parse import unquote

import vobject
from lxml import etree

from .dav import NS_CARD, NS_CS, NS_D, NSMAP, DavClient, DavError, NotFound, PreconditionFailed, multiget_xml
from .safety import (PHOTO_MAX, check_expected_etag, collection_segment, decode_data_uri, fetch_url_limited,
                     is_safe_name, read_local_limited, resource_name)

_TYPE_PREFIX = re.compile(r"^(work|home|cell|mobile|fax|main|other|pref|internet)\s*:\s*(.+)$", re.I)
_MULTIGET_BATCH = 100


def _split_typed(value: str, default: str) -> tuple[str, str]:
    """'cell:+39 333...' -> ('CELL', '+39 333...'); senza prefisso usa default."""
    m = _TYPE_PREFIX.match(value.strip())
    if m:
        t = m.group(1).upper()
        return ("CELL" if t == "MOBILE" else t), m.group(2).strip()
    return default, value.strip()


def vcard_to_dict(card: vobject.base.Component, href: str | None = None, etag: str | None = None) -> dict[str, Any]:
    def one(name: str) -> str | None:
        el = getattr(card, name, None)
        return str(el.value).strip() if el is not None and el.value is not None else None

    def typed_list(name: str) -> list[dict[str, str]]:
        items = []
        for el in getattr(card, f"{name}_list", []) or []:
            types = [t.upper() for t in el.params.get("TYPE", [])]
            items.append({"value": str(el.value).strip(), "type": ",".join(types) or "OTHER"})
        return items

    org = department = None
    if getattr(card, "org", None) is not None:
        v = card.org.value
        parts = [str(x) for x in v] if isinstance(v, (list, tuple)) else [str(v)]
        org = parts[0] or None
        department = "; ".join(x for x in parts[1:] if x) or None

    im: list[str] = []
    for el in getattr(card, "impp_list", []) or []:
        v = str(el.value)
        service = (el.params.get("X-SERVICE-TYPE") or [None])[0]
        scheme, _, handle = v.partition(":")
        im.append(f"{(service or scheme).lower()}:{handle or scheme}")
    for el in getattr(card, "x-aim_list", []) or []:
        im.append(f"aim:{el.value}")

    n = getattr(card, "n", None)
    name = None
    if n is not None and n.value is not None:
        name = {"given": n.value.given or "", "family": n.value.family or "",
                "additional": n.value.additional or "", "prefix": n.value.prefix or "", "suffix": n.value.suffix or ""}

    addresses = []
    for adr in getattr(card, "adr_list", []) or []:
        a = adr.value
        addresses.append({
            "type": ",".join(t.upper() for t in adr.params.get("TYPE", [])) or "OTHER",
            "street": a.street or "", "city": a.city or "", "region": a.region or "",
            "code": a.code or "", "country": a.country or "",
        })

    categories: list[str] = []
    for c in getattr(card, "categories_list", []) or []:
        v = c.value
        categories.extend([x.strip() for x in v if x and x.strip()] if isinstance(v, (list, tuple))
                          else [x.strip() for x in str(v).split(",") if x.strip()])

    photo = None
    ph = getattr(card, "photo", None)
    if ph is not None and ph.value:
        if isinstance(ph.value, (bytes, bytearray)):
            photo = {"embedded": True, "type": (ph.params.get("TYPE") or ["?"])[0].upper(), "bytes": len(ph.value)}
        else:
            photo = {"embedded": False, "url": str(ph.value)}

    out: dict[str, Any] = {
        "uid": one("uid"),
        "fn": one("fn"),
        "name": name,
        "nickname": one("nickname"),
        "categories": categories,
        "photo": photo,
        "department": department,
        "role": one("role"),
        "assistant": one("x-assistant"),
        "spouse": one("x-spouse"),
        "anniversary": one("x-anniversary"),
        "im": im,
        "timezone": one("tz"),
        "emails": typed_list("email"),
        "phones": typed_list("tel"),
        "org": org,
        "title": one("title"),
        "note": one("note"),
        "birthday": one("bday"),
        "urls": [str(u.value) for u in getattr(card, "url_list", []) or []],
        "addresses": addresses,
    }
    if href:
        out["href"] = href
    if etag:
        out["etag"] = etag
    return {k: v for k, v in out.items() if v not in (None, [], "")}


def apply_fields(card: vobject.base.Component, *, fn: str | None = None, given: str | None = None,
                 family: str | None = None, emails: list[str] | None = None, phones: list[str] | None = None,
                 org: str | None = None, title: str | None = None, note: str | None = None,
                 birthday: str | None = None, urls: list[str] | None = None,
                 addresses: list[str] | None = None, nickname: str | None = None,
                 categories: list[str] | None = None, photo: str | None = None,
                 additional: str | None = None, prefix: str | None = None, suffix: str | None = None,
                 department: str | None = None, role: str | None = None, assistant: str | None = None,
                 spouse: str | None = None, anniversary: str | None = None, im: list[str] | None = None,
                 timezone: str | None = None) -> None:
    """Applica i campi non-None alla vCard. Le liste sostituiscono integralmente quelle esistenti.

    additional/prefix/suffix: secondo nome, titolo (Dott., Ing.) e suffisso (Jr.) — componenti di N.
    department: seconda unità di ORG (come fa SOGo). role: ROLE. im: ["telegram:@nome", "skype:id", "whatsapp:+39..."]
    → IMPP. assistant/spouse/anniversary: X-ASSISTANT / X-SPOUSE / X-ANNIVERSARY (SOGo li conserva ma non li mostra).

    categories: tag SOGo (es. ["Cliente", "VIP"]); [] li rimuove tutti.
    photo: URL http(s), data-URI o path di file locale (jpg/png/gif/webp); viene scaricata/letta e incorporata
    in base64. "" rimuove la foto.

    addresses: lista di stringhe "tipo:via;città;provincia;cap;paese" (tipo opzionale, default WORK).
    I campi vuoti si lasciano vuoti, es. "work:Via Roma 1;Milano;MI;20100;IT" oppure "home:Via Roma 1;Milano;;20100;".
    """

    def set_single(name: str, value: str | None) -> None:
        if value is None:
            return
        existing = getattr(card, name, None)
        if value == "":
            if existing is not None:
                card.remove(existing)
            return
        if existing is None:
            existing = card.add(name)
        existing.value = value

    def replace_list(name: str, values: list[str] | None, default_type: str, param: str = "TYPE") -> None:
        if values is None:
            return
        for el in list(getattr(card, f"{name}_list", []) or []):
            card.remove(el)
        for raw in values:
            if not raw or not raw.strip():
                continue
            t, v = _split_typed(raw, default_type)
            el = card.add(name)
            el.value = v
            el.params[param] = [t] if name != "email" else ["INTERNET", t] if t != "INTERNET" else ["INTERNET"]

    set_single("fn", fn)
    if any(x is not None for x in (given, family, additional, prefix, suffix)):
        n = getattr(card, "n", None)
        if n is None:
            n = card.add("n")
            n.value = vobject.vcard.Name(family="", given="")
        for attr, val in (("given", given), ("family", family), ("additional", additional),
                          ("prefix", prefix), ("suffix", suffix)):
            if val is not None:
                setattr(n.value, attr, val)
    replace_list("email", emails, "WORK")
    replace_list("tel", phones, "CELL")
    if org is not None or department is not None:
        existing = getattr(card, "org", None)
        cur = [str(x) for x in existing.value] if existing is not None and isinstance(existing.value, (list, tuple)) \
            else ([str(existing.value)] if existing is not None else [])
        new_org = org if org is not None else (cur[0] if cur else "")
        new_dep = department if department is not None else (cur[1] if len(cur) > 1 else "")
        units = [new_org, new_dep] + cur[2:]  # unità ulteriori (es. team) restano intatte
        while len(units) > 1 and not units[-1]:
            units.pop()
        if existing is not None:
            card.remove(existing)
        if any(units):
            card.add("org").value = units
    set_single("role", role)
    set_single("x-assistant", assistant)
    set_single("x-spouse", spouse)
    set_single("x-anniversary", anniversary)
    set_single("tz", timezone)
    if im is not None:
        for name in ("impp_list", "x-aim_list"):
            for el in list(getattr(card, name, []) or []):
                card.remove(el)
        for raw in im:
            if not raw or ":" not in raw:
                continue
            service, _, handle = raw.strip().partition(":")
            service, handle = service.strip().lower(), handle.strip()
            if not service or not handle:
                continue
            el = card.add("impp")
            el.value = f"{service}:{handle}"
            el.params["X-SERVICE-TYPE"] = [service.capitalize()]
    set_single("title", title)
    set_single("note", note)
    set_single("bday", birthday)
    set_single("nickname", nickname)
    if categories is not None:
        for el in list(getattr(card, "categories_list", []) or []):
            card.remove(el)
        clean = [c.strip() for c in categories if c and c.strip()]
        if clean:
            card.add("categories").value = clean
    if photo is not None:
        existing = getattr(card, "photo", None)
        if existing is not None:
            card.remove(existing)
        if photo != "":
            data, mime = load_photo(photo)
            el = card.add("photo")
            el.value = data
            el.encoding_param = "b"
            el.type_param = mime
    if urls is not None:
        for el in list(getattr(card, "url_list", []) or []):
            card.remove(el)
        for u in urls:
            if u.strip():
                card.add("url").value = u.strip()
    if addresses is not None:
        for el in list(getattr(card, "adr_list", []) or []):
            card.remove(el)
        for raw in addresses:
            if not raw or not raw.strip():
                continue
            t, v = _split_typed(raw, "WORK")
            parts = [p.strip() for p in v.split(";")]
            parts += [""] * (5 - len(parts))
            street, city, region, code, country = parts[:5]
            if not any((street, city, region, code, country)):
                continue
            el = card.add("adr")
            el.value = vobject.vcard.Address(street=street, city=city, region=region, code=code, country=country)
            el.params["TYPE"] = [t]

    # FN è obbligatorio: se manca lo derivo da N o ORG
    if getattr(card, "fn", None) is None or not card.fn.value:
        n = getattr(card, "n", None)
        derived = " ".join(x for x in ((n.value.given if n else ""), (n.value.family if n else "")) if x).strip()
        if not derived and getattr(card, "org", None) is not None:
            derived = " ".join(card.org.value)
        if getattr(card, "fn", None) is None:
            card.add("fn")
        card.fn.value = derived or "Senza nome"


_PHOTO_MAX = PHOTO_MAX
_MIME_TO_VCARD = {"image/jpeg": "JPEG", "image/png": "PNG", "image/gif": "GIF", "image/webp": "WEBP"}


def _sniff_mime(data: bytes) -> str:
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    raise ValueError("Formato immagine non riconosciuto (accetto JPEG, PNG, GIF, WebP)")


def load_photo(source: str, *, allow_local: bool = True) -> tuple[bytes, str]:
    """Ritorna (bytes, tipo vCard es. 'JPEG') da URL http(s), data-URI o path locale (max 2 MB).

    Gli URL passano da fetch_url_limited (solo IP pubblici, limite di byte durante il download).
    `allow_local=False` per valori che arrivano dal server DAV: una PHOTO;VALUE=URI:/percorso/locale
    non deve far leggere file dal computer su cui gira il server MCP."""
    src = source.strip()
    if src.startswith("data:"):
        data = decode_data_uri(src, _PHOTO_MAX)
    elif src.lower().startswith(("http://", "https://")):
        data = fetch_url_limited(src, _PHOTO_MAX)
    elif allow_local:
        data = read_local_limited(src, _PHOTO_MAX)
    else:
        raise ValueError("Foto non incorporata con riferimento non supportato (accetto solo URL http(s) o data-URI)")
    return data, _MIME_TO_VCARD[_sniff_mime(data)]


def serialize_vcard(card: vobject.base.Component) -> str:
    """Serializza una vCard con folding RFC 6350 corretto.

    vobject, quando una riga è lunga esattamente il limite, emette una riga di continuazione vuota ("\\r\\n \\r\\n"):
    Contatti di macOS/iOS la unfolda incollando la riga successiva (es. "IT" + "CATEGORIES:..."). Qui unfoldo
    l'output di vobject e ripiego a 75 ottetti senza mai produrre continuazioni vuote.
    """
    raw = card.serialize()
    unfolded = re.sub(r"\r?\n[ \t]", "", raw)
    out: list[str] = []
    for line in unfolded.split("\r\n" if "\r\n" in unfolded else "\n"):
        if not line:
            continue
        chunks: list[str] = []
        buf = ""
        limit = 75
        for ch in line:
            if len((buf + ch).encode("utf-8")) > limit:
                chunks.append(buf)
                buf = ch
                limit = 74  # le continuazioni portano lo spazio iniziale
            else:
                buf += ch
        if buf:
            chunks.append(buf)
        out.append("\r\n ".join(chunks))
    return "\r\n".join(out) + "\r\n"


def new_vcard(uid: str | None = None, **fields: Any) -> vobject.base.Component:
    card = vobject.vCard()
    card.add("uid").value = uid or str(uuid.uuid4())
    n = card.add("n")
    n.value = vobject.vcard.Name(family="", given="")
    apply_fields(card, **fields)
    return card


class AddressBooks:
    def __init__(self, dav: DavClient, principal_path: str):
        self.dav = dav
        self.root = principal_path.rstrip("/") + "/Contacts/"
        self._cache: dict[str, dict[str, tuple[str | None, str, Any]]] = {}
        self._lock = threading.Lock()

    def book_path(self, book: str) -> str:
        return f"{self.root}{collection_segment(book, 'rubrica')}/"

    def list(self) -> list[dict[str, Any]]:
        res = self.dav.propfind(self.root, [(NS_D, "displayname"), (NS_D, "resourcetype"), (NS_CS, "getctag")], depth=1)
        out = []
        for r in res:
            if not r.has_child(NS_D, "resourcetype", NS_CARD, "addressbook"):
                continue
            out.append({"id": r.name, "name": r.text(NS_D, "displayname") or r.name, "href": r.href,
                        "ctag": r.text(NS_CS, "getctag")})
        return out

    # -- lettura ---------------------------------------------------------

    def _scan(self, book: str, stop_after: int | None = None) -> list[tuple[str, str, Any]]:
        """Indice della rubrica: [(href, kind, data)] in ordine di elenco; kind = contact | group | bad.

        Le schede si scaricano solo se l'ETag è cambiato dall'ultima scansione (cache per rubrica), così una
        ricerca non riscarica ogni volta tutte le foto. Con `stop_after` si ferma appena ha quel numero di contatti."""
        path = self.book_path(book)
        listing = self.dav.propfind(path, [(NS_D, "getetag"), (NS_D, "getcontenttype"), (NS_D, "resourcetype")], depth=1)
        items = [(r.href, r.etag) for r in listing
                 if r.href.rstrip("/") != path.rstrip("/")
                 and not r.has_child(NS_D, "resourcetype", NS_D, "collection")
                 and (r.href.lower().endswith(".vcf") or "vcard" in (r.text(NS_D, "getcontenttype") or ""))]
        with self._lock:
            cache = self._cache.setdefault(path, {})
            live = {unquote(h) for h, _ in items}
            for k in [k for k in cache if k not in live]:
                del cache[k]
            out: list[tuple[str, str, Any]] = []
            contacts = 0
            for i in range(0, len(items), _MULTIGET_BATCH):
                batch = items[i:i + _MULTIGET_BATCH]
                stale = [h for h, et in batch if not et or cache.get(unquote(h), (None,))[0] != et]
                if stale:
                    for h in stale:  # una versione vecchia non deve sopravvivere se il server non dà la nuova
                        cache.pop(unquote(h), None)
                    body = multiget_xml(NS_CARD, "addressbook-multiget", NS_CARD, "address-data", stale)
                    for r in self.dav.report(path, body, depth=1):
                        data = r.text(NS_CARD, "address-data")
                        if data:
                            cache[unquote(r.href)] = (r.etag, *_classify(data, r.href, r.etag))
                for h, _ in batch:
                    entry = cache.get(unquote(h))
                    if entry is None:
                        continue  # elencata ma non restituita dal multiget
                    out.append((h, entry[1], entry[2]))
                    contacts += entry[1] == "contact"
                if stop_after is not None and contacts >= stop_after:
                    break
            return out

    def all(self, book: str = "personal", limit: int | None = 500, category: str | None = None) -> list[dict[str, Any]]:
        """Tutti i contatti della rubrica (gruppi esclusi). `limit=None` = nessun limite.

        Il limite conta solo i contatti (i gruppi non lo consumano). Con `category` il filtro è applicato
        prima del limite, altrimenti un tag presente solo su contatti oltre i primi `limit` risulterebbe vuoto."""
        entries = self._scan(book, stop_after=limit if limit is not None and not category else None)
        results = [dict(data) for _, kind, data in entries if kind == "contact"]
        if category:
            results = _filter_category(results, category)
        return results if limit is None else results[:limit]

    def search(self, query: str, book: str = "personal", limit: int = 50, category: str | None = None) -> list[dict[str, Any]]:
        """Cerca per nome/nickname/email/telefono/azienda/note… su TUTTA la rubrica, lato client.

        Non uso `addressbook-query`: SOGo lo applica solo ad alcuni campi indicizzati e in modo
        non documentato (il cognome viene trovato, il nome di battesimo sullo stesso FN no; il filtro
        NICKNAME è ignorato e restituisce tutto), quindi un risultato non vuoto non è affidabile. Ogni parola della
        query deve comparire nel contatto, ignorando maiuscole e accenti."""
        terms = _norm(query).split()
        if not terms:
            return []
        results = [c for c in self.all(book, limit=None, category=category) if _matches(c, terms)]
        results.sort(key=lambda c: _norm(c.get("fn") or ""))
        return results[:limit]

    def _find_href(self, uid: str, book: str) -> tuple[str, str, str | None]:
        """Ritorna (href, vcard_text, etag). Prova {uid}.vcf, poi query su UID, poi l'indice della rubrica.

        L'UID si confronta in modo esatto: "ABC" e "abc" sono schede diverse."""
        path = self.book_path(book)
        if is_safe_name(uid):
            direct = f"{path}{uid}.vcf"
            try:
                text, etag = self.dav.get(direct)
                if _card_uid(text) == uid:
                    return direct, text, etag
            except NotFound:
                pass
        root = etree.Element(f"{{{NS_CARD}}}addressbook-query", nsmap=NSMAP)
        prop = etree.SubElement(root, f"{{{NS_D}}}prop")
        etree.SubElement(prop, f"{{{NS_D}}}getetag")
        etree.SubElement(prop, f"{{{NS_CARD}}}address-data")
        filt = etree.SubElement(root, f"{{{NS_CARD}}}filter")
        pf = etree.SubElement(filt, f"{{{NS_CARD}}}prop-filter", name="UID")
        tm = etree.SubElement(pf, f"{{{NS_CARD}}}text-match", collation="i;octet")
        tm.set("match-type", "equals")
        tm.text = uid
        body = etree.tostring(root, xml_declaration=True, encoding="utf-8")
        # SOGo può ignorare il filtro e restituire TUTTE le vCard: verifico sempre l'UID, mai "il primo risultato".
        for r in self.dav.report(path, body, depth=1):
            data = r.text(NS_CARD, "address-data")
            if data and _card_uid(data) == uid:
                return self._with_etag(r.href, data, r.etag)
        for href, kind, data in self._scan(book):
            if kind != "bad" and data.get("uid") == uid:
                text, etag = self.dav.get(href)  # contenuto fresco, non quello in cache
                if _card_uid(text) == uid:
                    return href, text, etag
        raise NotFound(f"Contatto con UID {uid!r} non trovato in {book!r}")

    def _with_etag(self, href: str, text: str, etag: str | None) -> tuple[str, str, str | None]:
        """Se il REPORT non ha dato l'ETag, rileggo contenuto ed ETag insieme con un GET (coerenti tra loro).

        Se nel frattempo la risorsa contiene un'altra scheda (UID diverso), mi fermo: l'ETag nuovo
        autorizzerebbe una modifica o cancellazione sulla scheda sbagliata."""
        if etag:
            return href, text, etag
        expected = _card_uid(text)
        fresh, etag = self.dav.get(href)
        if _card_uid(fresh) != expected:
            raise PreconditionFailed(f"La scheda {expected!r} è stata sostituita da un altro client: riprova")
        return href, fresh, etag

    def get(self, uid: str, book: str = "personal") -> dict[str, Any]:
        href, text, etag = self._find_href(uid, book)
        return vcard_to_dict(vobject.readOne(text), href=href, etag=etag)

    def photo(self, uid: str, book: str = "personal", fetch_url: bool = False) -> tuple[bytes, str] | str | None:
        """(bytes, mime) della foto incorporata, None se manca. Una foto per URL viene scaricata solo con
        fetch_url=True (altrimenti ritorna l'URL): scaricarla rivela a quel sito IP e momento dell'accesso."""
        _, text, _ = self._find_href(uid, book)
        card = vobject.readOne(text)
        ph = getattr(card, "photo", None)
        if ph is None or not ph.value:
            return None
        if isinstance(ph.value, (bytes, bytearray)):
            if len(ph.value) > PHOTO_MAX:
                raise ValueError(f"Foto incorporata troppo grande ({len(ph.value) // 1024} KB, max {PHOTO_MAX // 1024} KB)")
            return bytes(ph.value), _sniff_mime(bytes(ph.value))
        value = str(ph.value).strip()
        if value.startswith("data:"):
            data = decode_data_uri(value, PHOTO_MAX)
            return data, _sniff_mime(data)
        if not fetch_url:
            return value
        data, _ = load_photo(str(ph.value), allow_local=False)
        return data, _sniff_mime(data)

    def categories(self, book: str = "personal") -> dict[str, int]:
        """Tag usati nella rubrica con conteggio contatti."""
        counts: dict[str, int] = {}
        for c in self.all(book, limit=None):
            for tag in c.get("categories", []):
                counts[tag] = counts.get(tag, 0) + 1
        return dict(sorted(counts.items(), key=lambda kv: kv[0].lower()))

    # -- scrittura -------------------------------------------------------

    def create(self, book: str = "personal", **fields: Any) -> dict[str, Any]:
        card = new_vcard(**fields)
        href = f"{self.book_path(book)}{resource_name(card.uid.value)}.vcf"
        etag = self.dav.put(href, serialize_vcard(card), "text/vcard; charset=utf-8", create=True)
        return self._with_fold_warning(vcard_to_dict(card, href=href, etag=etag), href)

    def update(self, uid: str, book: str = "personal", expected_etag: str | None = None, **fields: Any) -> dict[str, Any]:
        """`expected_etag`: l'etag della lettura su cui il chiamante ha basato le modifiche (consigliato
        quando si sostituiscono liste): se nel frattempo il contatto è cambiato, l'update fallisce invece
        di cancellare in silenzio le modifiche altrui."""
        href, text, etag = self._find_href(uid, book)
        check_expected_etag(expected_etag, etag, f"Il contatto {uid}")
        card = vobject.readOne(text)
        apply_fields(card, **fields)
        new_etag = self.dav.put(href, serialize_vcard(card), "text/vcard; charset=utf-8", etag=etag)
        return self._with_fold_warning(vcard_to_dict(card, href=href, etag=new_etag), href)

    def _with_fold_warning(self, result: dict[str, Any], href: str) -> dict[str, Any]:
        """Rilegge la vCard come l'ha salvata SOGo e segnala il bug di folding (vedi sogo_fold_problems)."""
        try:
            text, _ = self.dav.get(href)
        except Exception:
            return result
        bad = sogo_fold_problems(text)
        if bad:
            result["warning"] = (
                "Bug di folding SOGo: la riga " + "; ".join(bad) +
                " è lunga esattamente 75 (o 149, 223…) caratteri e SOGo la salva con una continuazione vuota; "
                "i client Apple/BusyContacts la mostrano incollata al campo successivo. "
                "Cambia la lunghezza del campo (es. 'Italia' invece di 'IT', o una parola in più nella nota)."
            )
        return result

    def fold_problems(self, book: str = "personal") -> list[dict[str, Any]]:
        """Contatti della rubrica affetti dal bug di folding SOGo, con le righe incriminate."""
        out = []
        for href, text, _ in self._raw_all(book):
            bad = sogo_fold_problems(text)
            if bad:
                m = re.search(r"^FN:(.*?)\r?$", text, re.M)
                out.append({"uid": _card_uid(text), "fn": m.group(1).strip() if m else "", "lines": bad, "href": href})
        return out

    def delete(self, uid: str, book: str = "personal", expected_etag: str | None = None) -> dict[str, Any]:
        href, _, etag = self._find_href(uid, book)
        check_expected_etag(expected_etag, etag, f"Il contatto {uid}")
        self.dav.delete(href, etag=etag)
        return {"deleted": True, "uid": uid, "href": href}

    # -- gruppi (vCard con X-ADDRESSBOOKSERVER-KIND:group, formato Apple/SOGo/BusyContacts) --

    def _raw_all(self, book: str) -> list[tuple[str, str, str | None]]:
        """Tutte le risorse della rubrica come (href, vcard_text, etag), gruppi inclusi."""
        return self.raw_resources(book)[0]

    def raw_resources(self, book: str) -> tuple[list[tuple[str, str, str | None]], list[str]]:
        """(risorse recuperate, href elencati ma non restituiti dal multiget né da un GET singolo)."""
        path = self.book_path(book)
        listing = self.dav.propfind(path, [(NS_D, "getetag"), (NS_D, "resourcetype")], depth=1)
        hrefs = [r.href for r in listing
                 if r.href.rstrip("/") != path.rstrip("/")
                 and not r.has_child(NS_D, "resourcetype", NS_D, "collection")]
        out: list[tuple[str, str, str | None]] = []
        seen: set[str] = set()
        for i in range(0, len(hrefs), _MULTIGET_BATCH):
            body = multiget_xml(NS_CARD, "addressbook-multiget", NS_CARD, "address-data", hrefs[i:i + _MULTIGET_BATCH])
            for r in self.dav.report(path, body, depth=1):
                data = r.text(NS_CARD, "address-data")
                if data:
                    out.append((r.href, data, r.etag))
                    seen.add(unquote(r.href))
        missing: list[str] = []
        for href in hrefs:
            if unquote(href) in seen:
                continue
            try:
                text, etag = self.dav.get(href)
                out.append((href, text, etag))
            except DavError:
                missing.append(href)
        return out, missing

    def groups(self, book: str = "personal") -> list[dict[str, Any]]:
        """Elenca i gruppi: uid, name, members (lista di UID contatto), href."""
        out = [dict(data) for _, kind, data in self._scan(book) if kind == "group"]
        return sorted(out, key=lambda g: g["name"].lower())

    def _find_group(self, group: str, book: str) -> tuple[str, vobject.base.Component, str | None]:
        """group = UID (esatto) oppure nome (senza distinzione di maiuscole). Ritorna (href, card, etag) freschi."""
        q = group.strip()
        found = [(h, d) for h, kind, d in self._scan(book) if kind == "group"]
        hit = next(((h, d) for h, d in found if d.get("uid") == q), None) or \
            next(((h, d) for h, d in found if d.get("name", "").strip().lower() == q.lower()), None)
        if hit is None:
            raise NotFound(f"Gruppo {group!r} non trovato in {book!r}")
        href, info = hit
        text, etag = self.dav.get(href)  # la cache serve a trovarlo; per modificarlo rileggo contenuto ed ETag
        card = vobject.readOne(text)
        if not _is_group(card) or _card_uid(text) != info.get("uid"):
            raise PreconditionFailed(f"Il gruppo {group!r} è stato sostituito da un altro client: riprova")
        return href, card, etag

    def create_group(self, name: str, book: str = "personal") -> dict[str, Any]:
        card = vobject.vCard()
        uid = str(uuid.uuid4()).upper()
        card.add("uid").value = uid
        card.add("fn").value = name
        card.add("n").value = vobject.vcard.Name(family=name, given="")
        card.add("x-addressbookserver-kind").value = "group"
        href = f"{self.book_path(book)}{uid}.vcf"
        etag = self.dav.put(href, serialize_vcard(card), "text/vcard; charset=utf-8", create=True)
        return _group_to_dict(card, href, etag)

    def add_to_group(self, uid: str, group: str, book: str = "personal", create: bool = False) -> dict[str, Any]:
        """Aggiunge il contatto `uid` al gruppo (per nome o UID). Con create=True lo crea se manca."""
        self._find_href(uid, book)  # verifica che il contatto esista
        try:
            href, card, etag = self._find_group(group, book)
        except NotFound:
            if not create:
                raise
            g = self.create_group(group, book)
            href, card, etag = self._find_group(g["uid"], book)
        member = f"urn:uuid:{uid}"
        if any(_member_uid(m.value) == uid for m in _members(card)):
            return _group_to_dict(card, href, etag)
        card.add("x-addressbookserver-member").value = member
        new_etag = self.dav.put(href, serialize_vcard(card), "text/vcard; charset=utf-8", etag=etag)
        return _group_to_dict(card, href, new_etag)

    def remove_from_group(self, uid: str, group: str, book: str = "personal") -> dict[str, Any]:
        href, card, etag = self._find_group(group, book)
        removed = False
        for m in list(_members(card)):
            if _member_uid(m.value) == uid:
                card.remove(m)
                removed = True
        if not removed:
            return _group_to_dict(card, href, etag)
        new_etag = self.dav.put(href, serialize_vcard(card), "text/vcard; charset=utf-8", etag=etag)
        return _group_to_dict(card, href, new_etag)


_EMPTY_FOLD_RE = re.compile(r"^([^\r\n]+)\r?\n[ \t]\r?\n(?=[^ \t\r\n])", re.M)


def sogo_fold_problems(text: str) -> list[str]:
    """Righe seguite da una continuazione vuota (bug di folding SOGo: riga lunga 75, 149, 223… ottetti).

    Ritorna il nome della proprietà + inizio del valore per ogni riga affetta, [] se la vCard è pulita.
    """
    out = []
    for m in _EMPTY_FOLD_RE.finditer(text):
        line = m.group(1)
        name = line.split(":", 1)[0]
        out.append(f"{name} ({line[len(name) + 1:][:40]}…)")
    return out


_UID_RE = re.compile(r"^UID(?:;[^:]*)?:(.+?)\r?$", re.M | re.I)


def _card_uid(text: str) -> str:
    """UID (così com'è, case-sensitive) dal testo di una vCard, senza parsarla per intero. '' se assente.

    Unfolding prima della regex: un UID lungo è ripiegato su più righe fisiche."""
    m = _UID_RE.search(re.sub(r"\r?\n[ \t]", "", text))
    return m.group(1).strip() if m else ""


def _member_uid(value: Any) -> str:
    """'urn:uuid:ABC' → 'ABC' (prefisso senza distinzione di maiuscole, UID esatto)."""
    v = str(value).strip()
    return v[9:] if v.lower().startswith("urn:uuid:") else v


def _classify(text: str, href: str, etag: str | None) -> tuple[str, Any]:
    """(kind, dati) per l'indice della rubrica: contatto, gruppo o scheda illeggibile."""
    try:
        card = vobject.readOne(text)
    except Exception:
        return "bad", None
    if _is_group(card):
        return "group", _group_to_dict(card, href, etag)
    return "contact", vcard_to_dict(card, href=href, etag=etag)


def _members(card: vobject.base.Component) -> list[Any]:
    return list(card.contents.get("x-addressbookserver-member", [])) + list(card.contents.get("member", []))


def _is_group(card: vobject.base.Component) -> bool:
    for key in ("x-addressbookserver-kind", "kind"):
        for el in card.contents.get(key, []):
            if str(el.value).strip().lower() == "group":
                return True
    return False


def _group_to_dict(card: vobject.base.Component, href: str | None = None, etag: str | None = None) -> dict[str, Any]:
    members = [_member_uid(m.value) for m in _members(card)]
    out: dict[str, Any] = {
        "uid": str(card.uid.value) if getattr(card, "uid", None) is not None else None,
        "name": str(card.fn.value) if getattr(card, "fn", None) is not None else "",
        "members": members,
        "member_count": len(members),
    }
    if href:
        out["href"] = href
    if etag:
        out["etag"] = etag
    return out


def _filter_category(contacts: list[dict[str, Any]], category: str) -> list[dict[str, Any]]:
    q = category.strip().lower()
    return [c for c in contacts if any(q == t.lower() for t in c.get("categories", []))]


def _flat(contact: dict[str, Any]) -> str:
    parts: list[str] = []
    for k, v in contact.items():
        if k in ("photo", "href", "etag"):
            continue
        if isinstance(v, str):
            parts.append(v)
        elif isinstance(v, list):
            for x in v:
                parts.append(x if isinstance(x, str) else " ".join(str(y) for y in x.values()))
        elif isinstance(v, dict):
            parts.append(" ".join(str(y) for y in v.values()))
    return " ".join(parts).lower()


def _norm(text: str) -> str:
    """Minuscolo e senza accenti: 'Niccolò' -> 'niccolo'."""
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch)).casefold()


def _matches(contact: dict[str, Any], terms: list[str]) -> bool:
    """Ogni termine deve comparire nel contatto; i numeri valgono anche ignorando spazi/trattini dei telefoni."""
    hay = _norm(_flat(contact))
    phones = [re.sub(r"\D", "", p.get("value", "")) for p in contact.get("phones", [])]
    return all(t in hay or _digits_in_phones(t, phones) for t in terms)


def _digits_in_phones(term: str, phones: list[str]) -> bool:
    digits = re.sub(r"[\s\-./()+]", "", term)
    return len(digits) >= 3 and digits.isdigit() and any(digits in p for p in phones)
