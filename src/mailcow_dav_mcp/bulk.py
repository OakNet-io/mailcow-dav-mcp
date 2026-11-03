"""Import/export in blocco della rubrica: vCard (.vcf) e CSV."""

from __future__ import annotations

import csv
import io
import os
import re
import tempfile
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

import vobject

from .carddav import (AddressBooks, _is_group, _member_uid, _members, apply_fields, new_vcard, serialize_vcard,
                      vcard_to_dict)
from .dav import PreconditionFailed
from .safety import confined_dir, local_path, resource_name

# Colonne CSV (stesso schema in import ed export). Le liste usano " | " come separatore ("\|" = barra letterale,
# "\\" = backslash), i valori tipizzati il prefisso "tipo:" (es. "work:mario@acme.it"), gli indirizzi
# "tipo:via;città;prov;cap;paese". Le celle che iniziano con = + - @ (formule nei fogli di calcolo) sono
# precedute da un apostrofo, tolto di nuovo in import.
CSV_COLUMNS = ["uid", "fn", "prefix", "given", "additional", "family", "suffix", "nickname", "org", "department",
               "title", "role", "emails", "phones", "addresses", "categories", "urls", "im", "birthday",
               "anniversary", "spouse", "assistant", "timezone", "note"]
LIST_SEP = " | "
_FORMULA_START = ("=", "+", "-", "@", "\t", "\r", "'")
_LIST_FIELDS = {"emails", "phones", "addresses", "categories", "urls", "im"}
# Alias di intestazione (italiano/inglese, Outlook/Google) → colonna canonica
COLUMN_ALIASES = {
    "nome": "given", "first name": "given", "firstname": "given", "given name": "given", "name": "fn",
    "cognome": "family", "last name": "family", "lastname": "family", "surname": "family", "family name": "family",
    "nome completo": "fn", "full name": "fn", "display name": "fn", "displayname": "fn",
    "secondo nome": "additional", "middle name": "additional", "titolo": "prefix", "name prefix": "prefix",
    "name suffix": "suffix", "soprannome": "nickname",
    "azienda": "org", "società": "org", "company": "org", "organization": "org", "organisation": "org",
    "reparto": "department", "dipartimento": "department", "job title": "title", "qualifica": "title",
    "ruolo": "role", "email": "emails", "e-mail": "emails", "mail": "emails", "e-mail address": "emails",
    "email address": "emails", "telefono": "phones", "phone": "phones", "tel": "phones", "cellulare": "phones",
    "mobile": "phones", "mobile phone": "phones", "indirizzo": "addresses", "address": "addresses",
    "tag": "categories", "tags": "categories", "categoria": "categories", "category": "categories",
    "gruppo": "categories", "group": "categories", "sito": "urls", "web": "urls", "website": "urls", "url": "urls",
    "compleanno": "birthday", "data di nascita": "birthday", "anniversario": "anniversary",
    "coniuge": "spouse", "assistente": "assistant", "fuso": "timezone", "notes": "note", "nota": "note",
}


def default_export_dir() -> Path:
    """MAILCOW_DAV_EXPORT_DIR, altrimenti ~/mailcow-dav-exports (fuori dal pacchetto installato)."""
    base = os.environ.get("MAILCOW_DAV_EXPORT_DIR") or "~/mailcow-dav-exports"
    return Path(os.path.expanduser(base)).resolve()


def _ensure_dir(p: Path) -> Path:
    p.mkdir(parents=True, exist_ok=True)
    return p


def _write_atomic(out: Path, data: str, overwrite: bool) -> None:
    """Scrive in un file temporaneo nella stessa directory e lo sposta al posto giusto solo se completo.

    Senza overwrite la destinazione non deve esistere (os.link fallisce se esiste: niente race)."""
    fd, tmp = tempfile.mkstemp(prefix=f".{out.name}.", suffix=".tmp", dir=out.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o600)
        if overwrite:
            os.replace(tmp, out)
        else:
            os.link(tmp, out)
            os.unlink(tmp)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def _join(items: Any) -> str:
    return LIST_SEP.join(str(x).replace("\\", "\\\\").replace("|", "\\|") for x in items)


def _split(raw: str) -> list[str]:
    """Inverso di _join: separa sulle "|" non precedute da backslash."""
    items, buf, i = [], [], 0
    while i < len(raw):
        ch = raw[i]
        if ch == "\\" and i + 1 < len(raw) and raw[i + 1] in "|\\":
            buf.append(raw[i + 1])
            i += 2
            continue
        if ch == "|":
            items.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
        i += 1
    items.append("".join(buf))
    return [x.strip() for x in items if x.strip()]


def _cell_safe(v: str) -> str:
    """Neutralizza le formule (CSV injection): '=1+1' → "'=1+1". Reversibile con _cell_unsafe."""
    return "'" + v if v and v.startswith(_FORMULA_START) else v


def _cell_unsafe(v: str) -> str:
    return v[1:] if len(v) > 1 and v[0] == "'" and v[1:].startswith(_FORMULA_START) else v


def contact_to_row(c: dict[str, Any]) -> dict[str, str]:
    name = c.get("name") or {}
    row = {
        "uid": c.get("uid", ""), "fn": c.get("fn", ""),
        "prefix": name.get("prefix", ""), "given": name.get("given", ""), "additional": name.get("additional", ""),
        "family": name.get("family", ""), "suffix": name.get("suffix", ""),
        "nickname": c.get("nickname", ""), "org": c.get("org", ""), "department": c.get("department", ""),
        "title": c.get("title", ""), "role": c.get("role", ""),
        "emails": _join(f"{e['type'].replace('INTERNET,', '').replace('INTERNET', '').lower() or 'other'}:{e['value']}"
                        for e in c.get("emails", [])),
        "phones": _join(f"{p['type'].lower()}:{p['value']}" for p in c.get("phones", [])),
        "addresses": _join(f"{a['type'].lower()}:{a['street']};{a['city']};{a['region']};{a['code']};{a['country']}"
                           for a in c.get("addresses", [])),
        "categories": _join(c.get("categories", [])),
        "urls": _join(c.get("urls", [])),
        "im": _join(c.get("im", [])),
        "birthday": c.get("birthday", ""), "anniversary": c.get("anniversary", ""),
        "spouse": c.get("spouse", ""), "assistant": c.get("assistant", ""),
        "timezone": c.get("timezone", ""), "note": c.get("note", ""),
    }
    return {k: _cell_safe(v or "") for k, v in row.items()}


def row_to_fields(row: dict[str, str]) -> dict[str, Any]:
    """Riga CSV → kwargs per apply_fields. Colonne sconosciute ignorate; celle vuote = campo non impostato."""
    fields: dict[str, Any] = {}
    for col in CSV_COLUMNS:
        if col == "uid":
            continue
        raw = _cell_unsafe((row.get(col) or "").strip())
        if not raw:
            continue
        if col in _LIST_FIELDS:
            items = _split(raw)
            if col in ("phones", "emails", "addresses"):  # "cell,voice,pref:+39..." → primo tipo utile
                items = [_first_type(x) for x in items]
            fields[col] = items
        else:
            fields[col] = raw
    return fields


def _first_type(value: str) -> str:
    t, _, v = value.partition(":")
    if not v:
        return value
    types = [x for x in t.split(",") if x and x not in ("voice", "pref", "internet")]
    return f"{types[0]}:{v}" if types else v


class Bulk:
    def __init__(self, books: AddressBooks):
        self.books = books

    # -- export --------------------------------------------------------

    def export(self, book: str = "personal", fmt: str = "vcf", path: str | None = None,
               overwrite: bool = False) -> dict[str, Any]:
        """Esporta tutta la rubrica. VCF: copia byte per byte di ogni risorsa (gruppi inclusi). CSV: solo contatti.

        Senza `path` crea un file nuovo con nome univoco; un file esistente viene sovrascritto solo con
        overwrite=True. La scrittura è atomica: un export interrotto non lascia backup troncati."""
        fmt = fmt.lower().lstrip(".")
        if fmt not in ("vcf", "csv"):
            raise ValueError("format: vcf | csv")
        resources, missing = self.books.raw_resources(book)
        result: dict[str, Any] = {"format": fmt, "addressbook": book}
        if fmt == "vcf":
            payload = "".join(text.strip() + "\r\n" for _, text, _ in resources)
            groups = sum(1 for _, text, _ in resources if re.search(r"^(X-ADDRESSBOOKSERVER-)?KIND:group", text, re.M | re.I))
            result.update({"count": len(resources), "groups": groups})
        else:
            buf = io.StringIO()
            w = csv.DictWriter(buf, fieldnames=CSV_COLUMNS)
            w.writeheader()
            count = groups = 0
            unparsable: list[str] = []
            for href, text, _ in resources:
                try:
                    card = vobject.readOne(text)
                except Exception:
                    unparsable.append(href)
                    continue
                if _is_group(card):
                    groups += 1
                    continue
                w.writerow(contact_to_row(vcard_to_dict(card)))
                count += 1
            payload = buf.getvalue()
            result.update({"count": count, "groups_not_exported": groups})
            if unparsable:
                result["unparsable"] = unparsable
        if missing:
            result["missing"] = missing
            result["warning"] = f"{len(missing)} risorse elencate dal server non sono state recuperate e mancano dall'export"
        out = self._export_target(book, fmt, path)
        _write_atomic(out, payload, overwrite=overwrite)
        result["path"] = str(out)
        return result

    @staticmethod
    def _export_target(book: str, fmt: str, path: str | None) -> Path:
        if path:
            out = local_path(path)
            _ensure_dir(out.parent)
            return out
        base = _ensure_dir(confined_dir() or default_export_dir())
        safe = re.sub(r"[^A-Za-z0-9._-]", "_", book.strip("/")).strip("._") or "rubrica"
        stem = f"{safe}-{datetime.now():%Y%m%d-%H%M%S}"
        out = base / f"{stem}.{fmt}"
        n = 1
        while out.exists():
            out = base / f"{stem}-{n}.{fmt}"
            n += 1
        return out

    # -- import --------------------------------------------------------

    def preview(self, path: str) -> list[dict[str, Any]]:
        """Contatti letti dal file (senza scrivere nulla)."""
        return [vcard_to_dict(card) for card in self._load(path)]

    def import_file(self, path: str, book: str = "personal", dry_run: bool = True,
                    skip_duplicates: bool = True) -> dict[str, Any]:
        """Importa in due fasi: prima i contatti, poi i gruppi, con i membri riscritti sugli UID effettivi
        (contatto già presente → UID esistente; UID già usato → UID nuovo). I membri che non corrispondono
        a nessun contatto sono segnalati in `unresolved_members`, anche in dry-run."""
        cards = self._load(path)
        contacts = [c for c in cards if not _is_group(c)]
        groups = [c for c in cards if _is_group(c)]
        existing = self.books.all(book, limit=None)
        existing_groups = self.books.groups(book) if groups or skip_duplicates else []
        by_email = {e["value"].lower(): c["uid"] for c in existing for e in c.get("emails", []) if c.get("uid")}
        by_name = {(c.get("fn") or "").strip().lower(): c["uid"] for c in existing if c.get("fn") and c.get("uid")}
        taken = {c["uid"] for c in existing if c.get("uid")} | {g["uid"] for g in existing_groups if g.get("uid")}
        existing_uids = {c["uid"] for c in existing if c.get("uid")}
        uid_map: dict[str, str] = {}

        to_create, skipped = [], []
        for card in contacts:
            orig = _uid(card)
            d = vcard_to_dict(card)
            emails = {e["value"].lower() for e in d.get("emails", [])}
            fn = (d.get("fn") or "").strip().lower()
            dup = next((by_email[e] for e in emails if e in by_email), None) or \
                (by_name.get(fn) if not emails and fn else None)
            if skip_duplicates and dup:
                skipped.append({"fn": d.get("fn"), "reason": "duplicato (stessa email o stesso nome)", "existing_uid": dup})
                if orig:
                    uid_map[orig] = dup
                continue
            if not orig or orig in taken:
                _set_uid(card, str(uuid.uuid4()))  # UID assente o già usato in rubrica
            if orig:
                uid_map[orig] = _uid(card)
            taken.add(_uid(card))
            to_create.append(card)
            for e in emails:
                by_email.setdefault(e, _uid(card))
            if fn:
                by_name.setdefault(fn, _uid(card))

        known = existing_uids | {_uid(c) for c in to_create}
        group_plan, unresolved = [], []
        groups_by_name = {g["name"].strip().lower(): g for g in existing_groups}
        planned: dict[str, dict[str, Any]] = {}
        for card in groups:
            name = str(getattr(card, "fn", None) and card.fn.value or "").strip()
            members, missing = self._map_members(card, uid_map, known)
            unresolved.extend({"group": name, "member": m} for m in missing)
            if skip_duplicates and name.lower() in planned:  # stesso nome due volte nel file: un solo gruppo
                plan = planned[name.lower()]
                plan["members"] += [m for m in members if m not in plan["members"]]
                plan["sources"].append(card)
                continue
            target = groups_by_name.get(name.lower()) if skip_duplicates else None
            plan = {"card": card, "name": name, "members": members, "merge_into": target, "sources": [card]}
            group_plan.append(plan)
            planned[name.lower()] = plan

        result: dict[str, Any] = {
            "file": path, "addressbook": book, "dry_run": dry_run, "found": len(cards),
            "to_create": len(to_create), "skipped": skipped,
            "preview": [vcard_to_dict(c).get("fn") for c in to_create[:50]],
            "groups": [{"name": g["name"], "action": "merge" if g["merge_into"] else "create",
                        "members": len(g["members"])} for g in group_plan],
        }
        if unresolved:
            result["unresolved_members"] = unresolved
        if dry_run:
            return result

        created, errors = [], []
        for card in to_create:
            orig = next((o for o, u in uid_map.items() if u == _uid(card)), None)
            try:
                created.append(self._put_card(card, book))
            except Exception as exc:  # continua con gli altri
                errors.append({"fn": str(getattr(card, "fn", None) and card.fn.value), "error": str(exc)})
                continue
            new_uid = created[-1]["uid"]
            if orig and uid_map.get(orig) != new_uid:  # dopo un 412 l'UID è cambiato: aggiorno ogni alias
                old = uid_map[orig]
                for k, v in list(uid_map.items()):
                    if v == old:
                        uid_map[k] = new_uid
        done = {c["uid"] for c in created} | existing_uids
        groups_out = []
        for g in group_plan:
            members: list[str] = []
            for src in g["sources"]:
                members += [m for m in self._map_members(src, uid_map, done)[0] if m not in members]
            try:
                if g["merge_into"]:
                    groups_out.append(self._merge_group(g["merge_into"]["uid"], members, book))
                else:
                    _set_members(g["card"], members)
                    if not _uid(g["card"]) or _uid(g["card"]) in taken:
                        _set_uid(g["card"], str(uuid.uuid4()).upper())
                    taken.add(_uid(g["card"]))
                    groups_out.append(self._put_card(g["card"], book))
            except Exception as exc:
                errors.append({"group": g["name"], "error": str(exc)})
        result.update({"created": len(created), "errors": errors, "created_uids": [c["uid"] for c in created],
                       "groups_written": len(groups_out)})
        return result

    @staticmethod
    def _map_members(card: vobject.base.Component, uid_map: dict[str, str], known: set[str]) -> tuple[list[str], list[str]]:
        """(valori MEMBER riscritti sugli UID effettivi, membri che non corrispondono a nessun contatto).

        Solo i riferimenti urn:uuid si riscrivono; gli altri (es. mailto:) restano come sono."""
        members, missing = [], []
        for m in _members(card):
            raw = str(m.value).strip()
            if not raw.lower().startswith("urn:uuid:"):
                if raw not in members:
                    members.append(raw)
                continue
            orig = _member_uid(raw)
            uid = uid_map.get(orig, orig)
            if uid not in known:
                missing.append(orig)  # lo conservo: magari il contatto arriverà con un import successivo
            value = f"urn:uuid:{uid}"
            if value not in members:
                members.append(value)
        return members, missing

    def _merge_group(self, group_uid: str, members: list[str], book: str) -> dict[str, Any]:
        """Aggiunge i membri mancanti a un gruppo già presente in rubrica (PUT condizionata sull'ETag)."""
        href, card, etag = self.books._find_group(group_uid, book)
        have = {_member_uid(m.value) for m in _members(card)}
        new = [m for m in members if _member_uid(m) not in have]
        if new:
            for m in new:
                card.add("x-addressbookserver-member").value = m
            etag = self.books.dav.put(href, serialize_vcard(card), "text/vcard; charset=utf-8", etag=etag)
        return {"uid": group_uid, "href": href, "etag": etag, "added": len(new)}

    def _put_card(self, card: vobject.base.Component, book: str) -> dict[str, Any]:
        if not _uid(card):
            _set_uid(card, str(uuid.uuid4()))
        if getattr(card, "version", None) is None:
            card.add("version").value = "3.0"
        if not _is_group(card):
            apply_fields(card)  # normalizza FN mancante
        for attempt in range(2):
            # il nome della risorsa non deriva mai da un UID arbitrario del file (es. "../altra-rubrica/x")
            href = f"{self.books.book_path(book)}{resource_name(card.uid.value)}.vcf"
            try:
                etag = self.books.dav.put(href, serialize_vcard(card), "text/vcard; charset=utf-8", create=True)
                return vcard_to_dict(card, href=href, etag=etag)
            except PreconditionFailed:
                if attempt:
                    raise
                card.uid.value = str(uuid.uuid4())  # UID già presente: nuovo UID
        raise RuntimeError("unreachable")

    def _load(self, path: str) -> list[vobject.base.Component]:
        p = local_path(path)
        if not p.is_file():
            raise ValueError(f"File non trovato: {path}")
        text = p.read_text(encoding="utf-8-sig")
        if p.suffix.lower() == ".csv":
            return self._from_csv(text)
        cards = []
        for comp in vobject.readComponents(text, ignoreUnreadable=True):
            if comp.name.upper() == "VCARD":
                cards.append(comp)
        if not cards:
            raise ValueError("Nessuna vCard trovata nel file")
        return cards

    @staticmethod
    def _from_csv(text: str) -> list[vobject.base.Component]:
        header = text.splitlines()[0] if text else ""
        delimiter = max(",;\t", key=header.count)  # il delimitatore è quello più frequente nell'intestazione
        reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
        names = []
        for f in reader.fieldnames or []:
            key = (f or "").strip().lower()
            names.append(COLUMN_ALIASES.get(key, key))
        reader.fieldnames = names
        cards = []
        for row in reader:
            fields = row_to_fields(row)
            if not fields:
                continue
            cards.append(new_vcard(uid=_cell_unsafe((row.get("uid") or "").strip()) or None, **fields))
        if not cards:
            raise ValueError(f"Nessun contatto nel CSV (colonne riconosciute: {', '.join(CSV_COLUMNS)})")
        return cards


def _uid(card: vobject.base.Component) -> str:
    return str(card.uid.value).strip() if getattr(card, "uid", None) is not None and card.uid.value else ""


def _set_uid(card: vobject.base.Component, uid: str) -> None:
    if getattr(card, "uid", None) is None:
        card.add("uid")
    card.uid.value = uid


def _set_members(card: vobject.base.Component, members: list[str]) -> None:
    for key in ("x-addressbookserver-member", "member"):
        for m in list(card.contents.get(key, [])):
            card.remove(m)
    for m in members:
        card.add("x-addressbookserver-member").value = m
