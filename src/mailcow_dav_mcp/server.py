"""MCP server: address book (CardDAV) + calendar (CalDAV) for mailcow/SOGo.

Start: `mailcow-dav-mcp` (stdio) — or `mailcow-dav-mcp --check` to test the connection.
"""

from __future__ import annotations

import argparse
import functools
import hmac
import ipaddress
import logging
import os
import sys
import threading
from functools import lru_cache
from typing import Any, Callable

import anyio

from mcp.server.fastmcp import FastMCP, Image
from mcp.server.fastmcp import server as _fastmcp_server
from mcp.types import TextContent, ToolAnnotations

from .bulk import Bulk, default_export_dir
from .caldav import Calendars
from .carddav import AddressBooks
from .config import Settings, _load_dotenv
from .dav import NS_D, DavClient, DavError
from .safety import confine_local_files

# FastMCP leggerebbe un .env dalla cartella corrente (variabili FASTMCP_*): un repo aperto nel client non deve
# poter cambiare la configurazione del server.
_fastmcp_server.Settings.model_config["env_file"] = None

mcp = FastMCP(
    "mailcow-dav",
    instructions=(
        "Access to the user's address book (CardDAV) and calendar (CalDAV) on mailcow/SOGo. "
        "Use list_addressbooks / list_calendars to discover the IDs; 'personal' is the default one. "
        "Dates/times in ISO 8601 (e.g. 2026-10-03T15:00); without an offset the configured time zone is assumed. "
        "Before modifying or deleting, confirm with the user and use the exact UID returned by searches; "
        "also pass the etag you read (etag parameter) so that a change made in the meantime by another client "
        "is not overwritten."
    ),
)


_init_lock = threading.Lock()


_HINTS = {  # (readOnly, destructive, idempotent)
    "read": (True, False, True),
    "create": (False, False, False),
    "destructive": (False, True, False),  # modifica o cancella dati esistenti
}
WRITE_TOOLS: set[str] = set()  # tool che modificano il DAV: esclusi con MAILCOW_DAV_READ_ONLY=1


def tool(kind: str, *, open_world: bool = False) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Like @mcp.tool(), with MCP annotations (read-only / destructive) and the body run in a thread.

    The annotations tell the client which tools require confirmation: hostile text in a note or an invitation
    must not be able to delete data without the user seeing it. FastMCP would run sync functions
    in the event loop: over HTTP a slow scan would block other requests. functools.wraps keeps
    signature and docstring for the schema. open_world: the tool contacts third parties (invitations, replies, external URLs)."""
    ro, destructive, idempotent = _HINTS[kind]

    def deco(fn: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(fn)
        async def run(*args: Any, **kwargs: Any) -> Any:
            return await anyio.to_thread.run_sync(functools.partial(fn, *args, **kwargs))
        mcp.tool(annotations=ToolAnnotations(readOnlyHint=ro, destructiveHint=destructive,
                                             idempotentHint=idempotent, openWorldHint=open_world))(run)
        if kind in ("create", "destructive") and fn.__name__ != "export_contacts":  # export: legge il DAV
            WRITE_TOOLS.add(fn.__name__)
        return fn
    return deco


def _ctx() -> tuple[Settings, DavClient, AddressBooks, Calendars]:
    with _init_lock:  # i tool girano in thread: una sola inizializzazione anche con richieste parallele
        return _ctx_once()


@lru_cache(maxsize=1)
def _ctx_once() -> tuple[Settings, DavClient, AddressBooks, Calendars]:
    cfg = Settings.from_env()
    dav = DavClient(cfg.base_url, cfg.user, cfg.password, verify=cfg.verify_ssl, timeout=cfg.timeout)
    return cfg, dav, AddressBooks(dav, cfg.principal_path), Calendars(dav, cfg.principal_path, cfg.timezone, user=cfg.user)


def _books() -> AddressBooks:
    return _ctx()[2]


def _cals() -> Calendars:
    return _ctx()[3]


def _bulk() -> Bulk:
    with _init_lock:
        return _bulk_once()


@lru_cache(maxsize=1)
def _bulk_once() -> Bulk:
    return Bulk(_ctx_once()[2])


# ---------------------------------------------------------------- generale

@tool("read")
def dav_status() -> dict[str, Any]:
    """Check connection and credentials against SOGo; return the principal and the available address books and calendars."""
    cfg, dav, books, cals = _ctx()
    res = dav.propfind(cfg.principal_path, [(NS_D, "displayname"), (NS_D, "current-user-principal")], depth=0)
    return {
        "server": cfg.base_url,
        "user": cfg.user,
        "principal": cfg.principal_path,
        "timezone": cfg.timezone,
        "displayname": res[0].text(NS_D, "displayname") if res else None,
        "addressbooks": books.list(),
        "calendars": cals.list(),
    }


# ---------------------------------------------------------------- rubrica

@tool("read")
def list_addressbooks() -> list[dict[str, Any]]:
    """List the user's CardDAV address books (id, name). 'personal' is the main address book."""
    return _books().list()


@tool("read")
def list_contacts(addressbook: str = "personal", limit: int = 500, category: str | None = None) -> list[dict[str, Any]]:
    """List the contacts of an address book (up to `limit` contacts; groups do not count), optionally only
    those with a given tag (`category`). For large address books prefer search_contacts."""
    return _books().all(addressbook, limit=limit, category=category)


@tool("read")
def search_contacts(query: str, addressbook: str = "personal", limit: int = 50,
                    category: str | None = None) -> list[dict[str, Any]]:
    """Search contacts by first name, last name, nickname, email, phone, company, notes… (partial match,
    case- and accent-insensitive; with several words all of them must appear). `category` restricts to contacts with that tag."""
    return _books().search(query, addressbook, limit=limit, category=category)


@tool("read")
def list_categories(addressbook: str = "personal") -> dict[str, int]:
    """Tags (categories) used in the address book, with the number of contacts for each."""
    return _books().categories(addressbook)


@tool("read", open_world=True)
def get_contact_photo(uid: str, addressbook: str = "personal", fetch_url: bool = False) -> list:
    """Return the contact's photo as an image (or a message if it has none).
    If the photo is only an external URL, the URL is returned; fetch_url=true downloads it (the site sees your IP):
    use it only if the user asks."""
    res = _books().photo(uid, addressbook, fetch_url=fetch_url)
    if res is None:
        return [TextContent(type="text", text="The contact has no photo.")]
    if isinstance(res, str):
        return [TextContent(type="text", text=f"The photo is an external URL, not downloaded: {res}")]
    data, mime = res
    return [Image(data=data, format=mime.split("/")[1]).to_image_content()]


@tool("read")
def get_contact(uid: str, addressbook: str = "personal") -> dict[str, Any]:
    """Return a full contact given its UID."""
    return _books().get(uid, addressbook)


@tool("create")
def create_contact(
    fn: str | None = None,
    given: str | None = None,
    family: str | None = None,
    emails: list[str] | None = None,
    phones: list[str] | None = None,
    org: str | None = None,
    title: str | None = None,
    note: str | None = None,
    birthday: str | None = None,
    urls: list[str] | None = None,
    addresses: list[str] | None = None,
    nickname: str | None = None,
    categories: list[str] | None = None,
    photo: str | None = None,
    prefix: str | None = None,
    additional: str | None = None,
    suffix: str | None = None,
    department: str | None = None,
    role: str | None = None,
    assistant: str | None = None,
    spouse: str | None = None,
    anniversary: str | None = None,
    im: list[str] | None = None,
    timezone: str | None = None,
    addressbook: str = "personal",
) -> dict[str, Any]:
    """Create a contact.

    - fn: full display name (if omitted it is derived from given+family or org)
    - emails/phones: lists of strings, optionally with a type prefix: "work:mario@acme.it", "cell:+39 333 1234567",
      "home:...", "fax:...". Default: email=WORK, phone=CELL.
    - addresses: lists of strings "type:street;city;region;postcode;country" (type optional, default WORK),
      e.g. "work:Via Roma 1;Milano;MI;20100;IT". Leave the fields you do not know empty: "Via Roma 1;Milano;;20100;".
    - birthday: YYYY-MM-DD
    - categories: tags for grouping (e.g. ["Client", "Supplier"]); see list_categories for those already in use.
    - photo: public http(s) URL, data-URI or path of a local image file (max 2 MB) in the export folder or in
      MAILCOW_DAV_LOCAL_DIRS; it is embedded in the contact.
    - prefix/additional/suffix: title (Dr., Eng.), middle name, suffix (Jr.). department: department (with org).
      role: role/function (title is the job title). im: ["telegram:@name", "skype:id", "whatsapp:+39…"].
      assistant, spouse, anniversary (YYYY-MM-DD), timezone (e.g. Europe/Rome).
    """
    return _books().create(addressbook, fn=fn, given=given, family=family, emails=emails, phones=phones,
                           org=org, title=title, note=note, birthday=birthday, urls=urls, addresses=addresses,
                           nickname=nickname, categories=categories, photo=photo, prefix=prefix, additional=additional,
                           suffix=suffix, department=department, role=role, assistant=assistant, spouse=spouse,
                           anniversary=anniversary, im=im, timezone=timezone)


@tool("destructive")
def update_contact(
    uid: str,
    fn: str | None = None,
    given: str | None = None,
    family: str | None = None,
    emails: list[str] | None = None,
    phones: list[str] | None = None,
    org: str | None = None,
    title: str | None = None,
    note: str | None = None,
    birthday: str | None = None,
    urls: list[str] | None = None,
    addresses: list[str] | None = None,
    nickname: str | None = None,
    categories: list[str] | None = None,
    photo: str | None = None,
    prefix: str | None = None,
    additional: str | None = None,
    suffix: str | None = None,
    department: str | None = None,
    role: str | None = None,
    assistant: str | None = None,
    spouse: str | None = None,
    anniversary: str | None = None,
    im: list[str] | None = None,
    timezone: str | None = None,
    addressbook: str = "personal",
    etag: str | None = None,
) -> dict[str, Any]:
    """Update an existing contact. Only the fields passed are changed.

    Empty string "" removes the field; lists (emails, phones, urls, addresses) entirely REPLACE
    the existing ones, so to add an email re-read the contact and pass the full list.
    addresses: format "type:street;city;region;postcode;country" (see create_contact); [] removes all addresses.
    categories: full list of tags ([] removes them all). photo: URL/data-URI/path to replace it, "" to remove it.
    etag: the etag returned by get/search/list for this item. If you pass it and another client has
    modified the item in the meantime, the operation fails instead of overwriting: re-read and try again.
    """
    return _books().update(uid, addressbook, expected_etag=etag, fn=fn, given=given, family=family, emails=emails, phones=phones,
                           org=org, title=title, note=note, birthday=birthday, urls=urls, addresses=addresses,
                           nickname=nickname, categories=categories, photo=photo, prefix=prefix, additional=additional,
                           suffix=suffix, department=department, role=role, assistant=assistant, spouse=spouse,
                           anniversary=anniversary, im=im, timezone=timezone)


@tool("destructive")
def delete_contact(uid: str, addressbook: str = "personal", etag: str | None = None) -> dict[str, Any]:
    """Permanently delete a contact. Ask the user for confirmation before using it.
    etag: the etag returned by get/search/list for this item. If you pass it and another client has
    modified the item in the meantime, the operation fails instead of overwriting: re-read and try again."""
    return _books().delete(uid, addressbook, expected_etag=etag)


@tool("read")
def check_folding(addressbook: str = "personal") -> list[dict[str, Any]]:
    """Find contacts affected by the SOGo folding bug (a line exactly 75, 149, 223… characters long saved
    with an empty continuation: Apple Contacts/BusyContacts show the field glued to the next one,
    e.g. "ITCATEGORIES:Fornitori"). For each one it lists the lines to shorten/lengthen (e.g. "Italia" instead of "IT").
    create_contact/update_contact return the same warning in the "warning" field."""
    return _books().fold_problems(addressbook)


@tool("read")
def list_groups(addressbook: str = "personal") -> list[dict[str, Any]]:
    """List contact groups (those visible in Apple/SOGo Contacts): uid, name, members (contact UIDs).

    Note: groups are different from tags (CATEGORIES); see list_categories for those.
    """
    return _books().groups(addressbook)


@tool("create")
def add_to_group(uid: str, group: str, addressbook: str = "personal", create: bool = False) -> dict[str, Any]:
    """Add a contact (uid) to a group, given by name (e.g. "Suppliers") or by UID.

    With create=True the group is created if it does not exist. Idempotent: if the contact is already a member nothing changes.
    """
    return _books().add_to_group(uid, group, addressbook, create=create)


@tool("destructive")
def remove_from_group(uid: str, group: str, addressbook: str = "personal") -> dict[str, Any]:
    """Remove a contact (uid) from a group (name or UID). The contact is not deleted."""
    return _books().remove_from_group(uid, group, addressbook)


@tool("destructive")
def export_contacts(addressbook: str = "personal", format: str = "vcf", path: str | None = None,
                    overwrite: bool = False) -> dict[str, Any]:
    """Export a whole address book to a local file: format vcf (exact copy of each card, groups included) or csv
    (contacts only, readable columns, lists separated by " | " with "\\|" for a literal bar; cells that
    start with = + - @ get a leading apostrophe, so a spreadsheet does not run them as formulas). Without path it creates a new file in
    MAILCOW_DAV_EXPORT_DIR (default ~/mailcow-dav-exports). An existing file is overwritten only with
    overwrite=true. Returns path, number of cards and any missing resources."""
    return _bulk().export(addressbook, fmt=format, path=path, overwrite=overwrite)


@tool("create")
def import_contacts(path: str, addressbook: str = "personal", dry_run: bool = True,
                    skip_duplicates: bool = True) -> dict[str, Any]:
    """Import contacts from a .vcf file (one or more vCards) or .csv (in the export folder or in MAILCOW_DAV_LOCAL_DIRS) (same columns as the export: fn, given, family, org,
    emails, phones, …) into the address book. With dry_run=true (default) it only shows what would be created: call it
    that way first, then with dry_run=false after the user confirms. skip_duplicates skips anyone whose email (or name) is already present.
    Groups in the file are imported after the contacts, with members rewritten to the actual UIDs (a group with
    the same name as an existing one only receives the missing members); members without a contact are in unresolved_members."""
    return _bulk().import_file(path, addressbook, dry_run=dry_run, skip_duplicates=skip_duplicates)


# ---------------------------------------------------------------- calendario

@tool("read")
def list_calendars() -> list[dict[str, Any]]:
    """List the user's CalDAV calendars (id, name, color). 'personal' is the main calendar;
    the others have opaque IDs (use the one returned here). In all tools `calendar="*"` = all calendars."""
    return _cals().list()


@tool("read")
def list_events(
    start: str | None = None,
    end: str | None = None,
    calendar: str = "personal",
    expand_recurring: bool = True,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """Events in the range [start, end) in ISO 8601. Default: from today for 30 days. calendar="*" = all.

    With expand_recurring=True series are expanded into single occurrences (recurrence_id field set:
    it is the value to pass as `occurrence` to update_event/delete_event to act on that one only).
    """
    return _cals().events(calendar, start, end, expand=expand_recurring, limit=limit)


@tool("read")
def search_events(query: str, calendar: str = "personal", start: str | None = None,
                  end: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    """Search events by text in title, location, description or categories. Default: ±1 year from today. calendar="*" = all.
    Each recurring series is expanded up to 2000 occurrences per request (exceptions are always included;
    "truncated": true on the occurrences of a cut-off series: narrow the range)."""
    return _cals().search(query, calendar, start, end, limit=limit)


@tool("read")
def get_event(uid: str, calendar: str = "personal") -> dict[str, Any]:
    """Return a full event (master + any recurrence exceptions) given the UID. calendar="*" searches everywhere."""
    return _cals().get(uid, calendar)


@tool("create", open_world=True)
def create_event(
    summary: str,
    start: str,
    end: str | None = None,
    all_day: bool = False,
    location: str | None = None,
    description: str | None = None,
    attendees: list[str] | None = None,
    alarms: list[str] | None = None,
    rrule: str | None = None,
    categories: list[str] | None = None,
    privacy: str | None = None,
    busy: str | None = None,
    url: str | None = None,
    calendar: str = "personal",
) -> dict[str, Any]:
    """Create an event.

    - start/end: ISO 8601 ("2026-10-03T15:00" or "2026-10-03" for all_day). Without end: 1 hour (or 1 day if all_day).
      For all_day, end is EXCLUSIVE (event on October 3: start=2026-10-03, end=2026-10-04 or omitted).
    - attendees: ["Mario Rossi <mario@acme.it>", "anna@acme.it"] — SOGo will send the invitations.
    - alarms: reminders, e.g. ["15m", "1d"] (display) or ["email:1h"] (by mail). Units: m, h, d, w.
    - rrule: iCalendar rule, e.g. "FREQ=WEEKLY;BYDAY=MO;COUNT=10".
    - categories: tags (e.g. ["Client", "Project X"]); privacy: public | private | confidential;
      busy: "busy" (default) or "free" (does not take up availability).
    """
    return _cals().create(calendar, summary=summary, start=start, end=end, all_day=all_day, location=location,
                          description=description, attendees=attendees, alarms=alarms, rrule=rrule,
                          categories=categories, privacy=privacy, busy=busy, url=url)


@tool("destructive", open_world=True)
def update_event(
    uid: str,
    summary: str | None = None,
    start: str | None = None,
    end: str | None = None,
    all_day: bool | None = None,
    location: str | None = None,
    description: str | None = None,
    attendees: list[str] | None = None,
    alarms: list[str] | None = None,
    rrule: str | None = None,
    status: str | None = None,
    categories: list[str] | None = None,
    privacy: str | None = None,
    busy: str | None = None,
    url: str | None = None,
    occurrence: str | None = None,
    calendar: str = "personal",
    etag: str | None = None,
) -> dict[str, Any]:
    """Update an event. Only the fields passed change; empty string "" removes the field, empty lists [] clear it
    (alarms=[] removes the reminders). rrule="" turns the series into a single event (the first occurrence):
    it also removes added/excluded dates and exceptions. FREQ=SECONDLY/MINUTELY are not accepted.

    - Moving only `start` keeps the duration.
    - `occurrence`: the recurrence_id returned by list_events (or the start of the occurrence, e.g. "2026-10-10T15:00" or
      "2026-10-10"): changes ONLY that occurrence of a series (creates an exception); without it the whole series is changed.
      On a non-recurring event, or with a non-existent occurrence, it fails (it never changes the whole event).
    - all_day=false on an all-day event requires start with a time.
    - status: TENTATIVE | CONFIRMED | CANCELLED. attendees replaces the list entirely.
    etag: the etag returned by get/search/list for this item. If you pass it and another client has
    modified the item in the meantime, the operation fails instead of overwriting: re-read and try again.
    """
    return _cals().update(uid, calendar, occurrence=occurrence, expected_etag=etag, summary=summary, start=start, end=end, all_day=all_day,
                          location=location, description=description, attendees=attendees, alarms=alarms,
                          rrule=rrule, status=status, categories=categories, privacy=privacy, busy=busy, url=url)


@tool("destructive", open_world=True)
def delete_event(uid: str, calendar: str = "personal", occurrence: str | None = None,
                 etag: str | None = None) -> dict[str, Any]:
    """Delete an event. With `occurrence` (recurrence_id or start of an occurrence) it deletes ONLY that one from the series;
    without it, it deletes everything (the whole series if recurring). An occurrence on a non-recurring or non-existent event
    fails: the whole event is never deleted in its place. Ask the user for confirmation first.
    etag: the etag returned by get/search/list for this item. If you pass it and another client has
    modified the item in the meantime, the operation fails instead of overwriting: re-read and try again."""
    return _cals().delete(uid, calendar, occurrence=occurrence, expected_etag=etag)


@tool("destructive", open_world=True)
def respond_to_invitation(uid: str, response: str, calendar: str = "personal", comment: str | None = None) -> dict[str, Any]:
    """Reply to a received invitation: response = accept | decline | tentative. SOGo notifies the organizer.
    Works only if the user is among the event's attendees."""
    return _cals().respond(uid, response, calendar, comment=comment)


# ---------------------------------------------------------------- task

@tool("read")
def list_tasks(calendar: str = "personal", include_completed: bool = False, limit: int = 200) -> list[dict[str, Any]]:
    """Calendar tasks (to-dos), sorted by due date and priority. calendar="*" = all calendars."""
    return _cals().tasks(calendar, include_completed=include_completed, limit=limit)


@tool("read")
def get_task(uid: str, calendar: str = "personal") -> dict[str, Any]:
    """Return a full task given the UID."""
    return _cals().get_task(uid, calendar)


@tool("create")
def create_task(
    summary: str,
    due: str | None = None,
    description: str | None = None,
    priority: str | int | None = None,
    start: str | None = None,
    categories: list[str] | None = None,
    alarms: list[str] | None = None,
    location: str | None = None,
    url: str | None = None,
    calendar: str = "personal",
) -> dict[str, Any]:
    """Create a task. due/start in ISO 8601 (date only or date+time). priority: high | medium | low or 1 (high) … 9 (low).
    alarms as for events, relative to the due date (e.g. ["1d"]); without due they are relative to start (one of the two is required)."""
    return _cals().create_task(calendar, summary=summary, due=due, description=description, priority=priority,
                               start=start, categories=categories, alarms=alarms, location=location, url=url)


@tool("destructive")
def update_task(
    uid: str,
    summary: str | None = None,
    due: str | None = None,
    description: str | None = None,
    priority: str | int | None = None,
    start: str | None = None,
    status: str | None = None,
    percent: int | None = None,
    completed: bool | None = None,
    categories: list[str] | None = None,
    alarms: list[str] | None = None,
    location: str | None = None,
    url: str | None = None,
    calendar: str = "personal",
    etag: str | None = None,
) -> dict[str, Any]:
    """Update a task. `completed=true` marks it completed (false reopens it). status: NEEDS-ACTION | IN-PROCESS |
    COMPLETED | CANCELLED; percent 0-100. Empty string "" removes the field (e.g. due="").
    etag: the etag returned by get/search/list for this item. If you pass it and another client has
    modified the item in the meantime, the operation fails instead of overwriting: re-read and try again."""
    return _cals().update_task(uid, calendar, expected_etag=etag, summary=summary, due=due, description=description, priority=priority,
                               start=start, status=status, percent=percent, completed=completed,
                               categories=categories, alarms=alarms, location=location, url=url)


@tool("destructive")
def delete_task(uid: str, calendar: str = "personal", etag: str | None = None) -> dict[str, Any]:
    """Permanently delete a task. Ask the user for confirmation first.
    etag: the etag returned by get/search/list for this item. If you pass it and another client has
    modified the item in the meantime, the operation fails instead of overwriting: re-read and try again."""
    return _cals().delete_task(uid, calendar, expected_etag=etag)


# ---------------------------------------------------------------- entrypoint

_LOOPBACK = ("127.0.0.1", "localhost", "::1")


class _BearerAuth:
    """ASGI middleware: every HTTP request must carry `Authorization: Bearer <token>`."""

    def __init__(self, app: Any, token: str):
        self.app = app
        self.expected = f"Bearer {token}".encode()

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] == "http":
            auth = dict(scope.get("headers") or []).get(b"authorization", b"")
            if not hmac.compare_digest(auth, self.expected):
                await send({"type": "http.response.start", "status": 401,
                            "headers": [(b"content-type", b"application/json"), (b"www-authenticate", b"Bearer")]})
                await send({"type": "http.response.body", "body": b'{"error":"unauthorized"}'})
                return
        await self.app(scope, receive, send)


def _is_loopback(host: str) -> bool:
    if host in _LOOPBACK:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _run_http(host: str, port: int, path: str) -> None:
    import uvicorn
    from mcp.server.transport_security import TransportSecuritySettings

    token = os.environ.get("MAILCOW_DAV_MCP_TOKEN", "").strip()
    if not token and not _is_loopback(host):
        raise SystemExit("ERROR: to listen on a non-local address set MAILCOW_DAV_MCP_TOKEN "
                         "(or stay on 127.0.0.1 behind a tunnel/reverse proxy with authentication).")
    extra = [h.strip() for h in os.environ.get("MAILCOW_DAV_MCP_ALLOWED_HOSTS", "").split(",") if h.strip()]
    hosts = ["127.0.0.1:*", "localhost:*", "[::1]:*"] + [h if ":" in h else f"{h}:*" for h in extra] + extra
    origins = ["http://127.0.0.1:*", "http://localhost:*"] + [f"https://{h}" for h in extra]
    mcp.settings.host, mcp.settings.port, mcp.settings.streamable_http_path = host, port, path
    mcp.settings.transport_security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True, allowed_hosts=hosts, allowed_origins=origins)
    export_dir = _confine()
    if not token:
        print("WARNING: no MAILCOW_DAV_MCP_TOKEN: any process on this machine can read and modify "
              "the address book and calendar through this endpoint. Set a token even locally.", file=sys.stderr)
    app: Any = mcp.streamable_http_app()
    if token:
        app = _BearerAuth(app, token)
    print(f"mailcow-dav-mcp: http://{host}:{port}{path} (auth: {'bearer token' if token else 'none, local only'}; "
          f"local files restricted to {export_dir})", file=sys.stderr)
    uvicorn.run(app, host=host, port=port, log_level="warning")


def _confine() -> Any:
    """Local files (import, export, photos) are always restricted to the export directory + MAILCOW_DAV_LOCAL_DIRS:
    hostile text in a note must not be able to make it read or upload any file on the computer to the server."""
    export_dir = default_export_dir()
    try:
        export_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:  # niente export/import, ma ricerca e calendario devono funzionare lo stesso
        print(f"WARNING: cannot create {export_dir} ({e}): import/export unavailable", file=sys.stderr)
    extra = [d for d in os.environ.get("MAILCOW_DAV_LOCAL_DIRS", "").split(os.pathsep) if d.strip()]
    confine_local_files([export_dir, *extra])
    return export_dir


def _apply_read_only() -> None:
    if os.environ.get("MAILCOW_DAV_READ_ONLY", "").strip().lower() in ("1", "true", "yes", "on"):
        for name in sorted(WRITE_TOOLS):
            mcp.remove_tool(name)
        print(f"mailcow-dav-mcp: read-only, {len(WRITE_TOOLS)} write tools disabled", file=sys.stderr)


def _check() -> None:
    try:
        info = dav_status()
    except DavError as e:
        print(f"DAV ERROR: {e}", file=sys.stderr)
        sys.exit(2)
    except RuntimeError as e:
        print(f"CONFIG ERROR: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:  # URL malformato o errore imprevisto: una riga chiara invece del traceback
        print(f"ERROR: {type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(3)
    print(f"OK  server={info['server']}  user={info['user']}  principal={info['principal']}")
    print("Address books: " + (", ".join(f"{b['id']} ({b['name']})" for b in info["addressbooks"]) or "-"))
    print("Calendars:     " + (", ".join(f"{c['id']} ({c['name']})" for c in info["calendars"]) or "-"))


def main() -> None:
    parser = argparse.ArgumentParser(prog="mailcow-dav-mcp", description="MCP server for mailcow/SOGo CardDAV + CalDAV")
    parser.add_argument("--check", action="store_true", help="test the DAV connection and exit")
    parser.add_argument("--transport", choices=("stdio", "http"), default=None,
                        help="stdio (default, local clients) or http (Streamable HTTP, remote clients)")
    parser.add_argument("--host", default=None, help="default 127.0.0.1 (MAILCOW_DAV_MCP_HOST)")
    parser.add_argument("--port", type=int, default=None, help="default 8000 (MAILCOW_DAV_MCP_PORT)")
    parser.add_argument("--path", default=None, help="default /mcp (MAILCOW_DAV_MCP_PATH)")
    args = parser.parse_args()
    try:
        _load_dotenv()  # prima di leggere token/host/export dir: devono valere anche se stanno solo nel .env
    except RuntimeError as e:
        raise SystemExit(f"CONFIG ERROR: {e}") from None
    logging.getLogger("httpx").setLevel(logging.WARNING)
    if args.check:
        _check()
        return
    transport = args.transport or os.environ.get("MAILCOW_DAV_MCP_TRANSPORT", "stdio")
    _apply_read_only()
    if transport == "http":
        _run_http(args.host or os.environ.get("MAILCOW_DAV_MCP_HOST", "127.0.0.1"),
                  args.port or int(os.environ.get("MAILCOW_DAV_MCP_PORT", "8000")),
                  args.path or os.environ.get("MAILCOW_DAV_MCP_PATH", "/mcp"))
    elif transport != "stdio":
        raise SystemExit(f"ERROR: MAILCOW_DAV_MCP_TRANSPORT={transport!r} (use stdio or http)")
    else:
        _confine()
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
