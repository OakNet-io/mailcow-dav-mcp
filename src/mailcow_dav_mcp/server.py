"""MCP server: rubrica (CardDAV) + calendario (CalDAV) di mailcow/SOGo.

Avvio: `mailcow-dav-mcp` (stdio) — oppure `mailcow-dav-mcp --check` per testare la connessione.
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
        "Accesso alla rubrica (CardDAV) e al calendario (CalDAV) dell'utente su mailcow/SOGo. "
        "Usa list_addressbooks / list_calendars per scoprire gli ID; 'personal' è quello di default. "
        "Date/ore in ISO 8601 (es. 2026-10-03T15:00); senza offset si assume il fuso configurato. "
        "Prima di modificare o cancellare, conferma con l'utente e usa l'UID esatto restituito dalle ricerche; "
        "passa anche l'etag letto (parametro etag) così una modifica fatta nel frattempo da un altro client "
        "non viene sovrascritta."
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
    """Come @mcp.tool(), con annotazioni MCP (sola lettura / distruttivo) e corpo eseguito in un thread.

    Le annotazioni dicono al client quali tool richiedono conferma: testo ostile in una nota o in un invito
    non deve poter far cancellare dati senza che l'utente lo veda. FastMCP eseguirebbe le funzioni sincrone
    nell'event loop: in HTTP una scansione lenta bloccherebbe le altre richieste. functools.wraps conserva
    firma e docstring per lo schema. open_world: il tool contatta terzi (inviti, risposte, URL esterni)."""
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
    """Verifica connessione e credenziali verso SOGo; ritorna principal, rubriche e calendari disponibili."""
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
    """Elenca le rubriche CardDAV dell'utente (id, nome). 'personal' è la rubrica principale."""
    return _books().list()


@tool("read")
def list_contacts(addressbook: str = "personal", limit: int = 500, category: str | None = None) -> list[dict[str, Any]]:
    """Elenca i contatti di una rubrica (fino a `limit` contatti; i gruppi non contano), opzionalmente solo
    quelli con un certo tag (`category`). Per rubriche grandi preferisci search_contacts."""
    return _books().all(addressbook, limit=limit, category=category)


@tool("read")
def search_contacts(query: str, addressbook: str = "personal", limit: int = 50,
                    category: str | None = None) -> list[dict[str, Any]]:
    """Cerca contatti per nome, cognome, nickname, email, telefono, azienda, note… (match parziale,
    ignora maiuscole e accenti; con più parole devono comparire tutte). `category` restringe ai contatti con quel tag."""
    return _books().search(query, addressbook, limit=limit, category=category)


@tool("read")
def list_categories(addressbook: str = "personal") -> dict[str, int]:
    """Tag (categorie) usati nella rubrica, con numero di contatti per ciascuno."""
    return _books().categories(addressbook)


@tool("read", open_world=True)
def get_contact_photo(uid: str, addressbook: str = "personal", fetch_url: bool = False) -> list:
    """Ritorna la foto del contatto come immagine (o un messaggio se non ne ha).
    Se la foto è solo un URL esterno viene restituito l'URL; fetch_url=true la scarica (il sito vede il tuo IP):
    usalo solo se l'utente lo chiede."""
    res = _books().photo(uid, addressbook, fetch_url=fetch_url)
    if res is None:
        return [TextContent(type="text", text="Il contatto non ha una foto.")]
    if isinstance(res, str):
        return [TextContent(type="text", text=f"La foto è un URL esterno, non scaricato: {res}")]
    data, mime = res
    return [Image(data=data, format=mime.split("/")[1]).to_image_content()]


@tool("read")
def get_contact(uid: str, addressbook: str = "personal") -> dict[str, Any]:
    """Ritorna un contatto completo dato il suo UID."""
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
    """Crea un contatto.

    - fn: nome completo visualizzato (se omesso viene derivato da given+family o org)
    - emails/phones: liste di stringhe, opzionalmente con prefisso tipo: "work:mario@acme.it", "cell:+39 333 1234567",
      "home:...", "fax:...". Default: email=WORK, telefono=CELL.
    - addresses: liste di stringhe "tipo:via;città;provincia;cap;paese" (tipo opzionale, default WORK),
      es. "work:Via Roma 1;Milano;MI;20100;IT". Lascia vuoti i campi che non conosci: "Via Roma 1;Milano;;20100;".
    - birthday: YYYY-MM-DD
    - categories: tag per raggruppare (es. ["Cliente", "Fornitore"]); vedi list_categories per quelli già in uso.
    - photo: URL http(s) pubblico, data-URI o path di un file immagine locale (max 2 MB) nella cartella di export o in
      MAILCOW_DAV_LOCAL_DIRS; viene incorporata nel contatto.
    - prefix/additional/suffix: titolo (Dott., Ing.), secondo nome, suffisso (Jr.). department: reparto (con org).
      role: ruolo/funzione (title è la qualifica). im: ["telegram:@nome", "skype:id", "whatsapp:+39…"].
      assistant, spouse, anniversary (YYYY-MM-DD), timezone (es. Europe/Rome).
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
    """Aggiorna un contatto esistente. Solo i campi passati vengono modificati.

    Stringa vuota "" rimuove il campo; le liste (emails, phones, urls, addresses) SOSTITUISCONO integralmente
    quelle esistenti, quindi per aggiungere un'email rileggi il contatto e passa la lista completa.
    addresses: formato "tipo:via;città;provincia;cap;paese" (vedi create_contact); [] rimuove tutti gli indirizzi.
    categories: lista completa dei tag ([] li toglie tutti). photo: URL/data-URI/path per sostituirla, "" per rimuoverla.
    etag: l'etag restituito da get/search/list per questo elemento. Se lo passi e nel frattempo un altro client
    l'ha modificato, l'operazione fallisce invece di sovrascrivere: rileggi e riprova.
    """
    return _books().update(uid, addressbook, expected_etag=etag, fn=fn, given=given, family=family, emails=emails, phones=phones,
                           org=org, title=title, note=note, birthday=birthday, urls=urls, addresses=addresses,
                           nickname=nickname, categories=categories, photo=photo, prefix=prefix, additional=additional,
                           suffix=suffix, department=department, role=role, assistant=assistant, spouse=spouse,
                           anniversary=anniversary, im=im, timezone=timezone)


@tool("destructive")
def delete_contact(uid: str, addressbook: str = "personal", etag: str | None = None) -> dict[str, Any]:
    """Elimina definitivamente un contatto. Chiedi conferma all'utente prima di usarlo.
    etag: l'etag restituito da get/search/list per questo elemento. Se lo passi e nel frattempo un altro client
    l'ha modificato, l'operazione fallisce invece di sovrascrivere: rileggi e riprova."""
    return _books().delete(uid, addressbook, expected_etag=etag)


@tool("read")
def check_folding(addressbook: str = "personal") -> list[dict[str, Any]]:
    """Trova i contatti affetti dal bug di folding di SOGo (riga lunga esattamente 75, 149, 223… caratteri salvata
    con una continuazione vuota: Contatti Apple/BusyContacts mostrano il campo incollato a quello successivo,
    es. "ITCATEGORIES:Fornitori"). Per ognuno indica le righe da accorciare/allungare (es. "Italia" al posto di "IT").
    create_contact/update_contact restituiscono lo stesso avviso nel campo "warning"."""
    return _books().fold_problems(addressbook)


@tool("read")
def list_groups(addressbook: str = "personal") -> list[dict[str, Any]]:
    """Elenca i gruppi di contatti (quelli visibili in Contatti di Apple/SOGo): uid, name, members (UID dei contatti).

    Nota: i gruppi sono diversi dai tag (CATEGORIES); vedi list_categories per quelli.
    """
    return _books().groups(addressbook)


@tool("create")
def add_to_group(uid: str, group: str, addressbook: str = "personal", create: bool = False) -> dict[str, Any]:
    """Aggiunge un contatto (uid) a un gruppo, indicato per nome (es. "Fornitori") o per UID.

    Con create=True il gruppo viene creato se non esiste. Idempotente: se il contatto è già membro non cambia nulla.
    """
    return _books().add_to_group(uid, group, addressbook, create=create)


@tool("destructive")
def remove_from_group(uid: str, group: str, addressbook: str = "personal") -> dict[str, Any]:
    """Rimuove un contatto (uid) da un gruppo (nome o UID). Il contatto non viene eliminato."""
    return _books().remove_from_group(uid, group, addressbook)


@tool("destructive")
def export_contacts(addressbook: str = "personal", format: str = "vcf", path: str | None = None,
                    overwrite: bool = False) -> dict[str, Any]:
    """Esporta tutta una rubrica in un file locale: format vcf (copia esatta di ogni scheda, gruppi inclusi) o csv
    (solo contatti, colonne leggibili, liste separate da " | " con "\\|" per una barra letterale; le celle che
    iniziano con = + - @ hanno un apostrofo davanti, così un foglio di calcolo non le esegue come formule). Senza path crea un file nuovo in
    MAILCOW_DAV_EXPORT_DIR (default ~/mailcow-dav-exports). Un file esistente viene sovrascritto solo con
    overwrite=true. Ritorna path, numero di schede ed eventuali risorse mancanti."""
    return _bulk().export(addressbook, fmt=format, path=path, overwrite=overwrite)


@tool("create")
def import_contacts(path: str, addressbook: str = "personal", dry_run: bool = True,
                    skip_duplicates: bool = True) -> dict[str, Any]:
    """Importa contatti da un file .vcf (una o più vCard) o .csv (nella cartella di export o in MAILCOW_DAV_LOCAL_DIRS) (stesse colonne dell'export: fn, given, family, org,
    emails, phones, …) nella rubrica. Con dry_run=true (default) mostra solo cosa verrebbe creato: chiama prima
    così, poi con dry_run=false dopo conferma dell'utente. skip_duplicates salta chi ha già un'email (o nome) presente.
    I gruppi del file vengono importati dopo i contatti, con i membri riscritti sugli UID effettivi (un gruppo con
    lo stesso nome di uno esistente riceve solo i membri mancanti); i membri senza contatto sono in unresolved_members."""
    return _bulk().import_file(path, addressbook, dry_run=dry_run, skip_duplicates=skip_duplicates)


# ---------------------------------------------------------------- calendario

@tool("read")
def list_calendars() -> list[dict[str, Any]]:
    """Elenca i calendari CalDAV dell'utente (id, nome, colore). 'personal' è il calendario principale;
    gli altri hanno ID opachi (usa quello restituito qui). In tutti i tool `calendar="*"` = tutti i calendari."""
    return _cals().list()


@tool("read")
def list_events(
    start: str | None = None,
    end: str | None = None,
    calendar: str = "personal",
    expand_recurring: bool = True,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """Eventi nell'intervallo [start, end) in ISO 8601. Default: da oggi per 30 giorni. calendar="*" = tutti.

    Con expand_recurring=True le serie vengono espanse nelle singole occorrenze (campo recurrence_id valorizzato:
    è il valore da passare come `occurrence` a update_event/delete_event per agire su quella sola).
    """
    return _cals().events(calendar, start, end, expand=expand_recurring, limit=limit)


@tool("read")
def search_events(query: str, calendar: str = "personal", start: str | None = None,
                  end: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    """Cerca eventi per testo in titolo, luogo, descrizione o categorie. Default: ±1 anno da oggi. calendar="*" = tutti.
    Ogni serie ricorrente è espansa fino a 2000 occorrenze per richiesta (le eccezioni restano sempre incluse;
    "truncated": true sulle occorrenze di una serie tagliata: restringi l'intervallo)."""
    return _cals().search(query, calendar, start, end, limit=limit)


@tool("read")
def get_event(uid: str, calendar: str = "personal") -> dict[str, Any]:
    """Ritorna un evento completo (master + eventuali eccezioni della ricorrenza) dato l'UID. calendar="*" lo cerca ovunque."""
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
    """Crea un evento.

    - start/end: ISO 8601 ("2026-10-03T15:00" o "2026-10-03" per all_day). Senza end: 1 ora (o 1 giorno se all_day).
      Per all_day, end è ESCLUSIVO (evento del 3 ottobre: start=2026-10-03, end=2026-10-04 o omesso).
    - attendees: ["Mario Rossi <mario@acme.it>", "anna@acme.it"] — SOGo invierà gli inviti.
    - alarms: promemoria, es. ["15m", "1d"] (display) o ["email:1h"] (via mail). Unità: m, h, d, w.
    - rrule: regola iCalendar, es. "FREQ=WEEKLY;BYDAY=MO;COUNT=10".
    - categories: tag (es. ["Cliente", "Progetto X"]); privacy: public | private | confidential;
      busy: "busy" (default) o "free" (non occupa disponibilità).
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
    """Aggiorna un evento. Solo i campi passati cambiano; stringa vuota "" rimuove il campo, liste vuote [] svuotano
    (alarms=[] toglie i promemoria). rrule="" trasforma la serie in un evento singolo (la prima occorrenza):
    toglie anche date aggiunte/escluse ed eccezioni. FREQ=SECONDLY/MINUTELY non sono accettate.

    - Spostando solo `start` la durata viene conservata.
    - `occurrence`: il recurrence_id restituito da list_events (o l'inizio dell'occorrenza, es. "2026-10-10T15:00" o
      "2026-10-10"): modifica SOLO quell'occorrenza di una serie (crea un'eccezione); senza si modifica l'intera serie.
      Su un evento non ricorrente, o con un'occorrenza inesistente, dà errore (non modifica mai l'intero evento).
    - all_day=false su un evento di un giorno intero richiede start con l'ora.
    - status: TENTATIVE | CONFIRMED | CANCELLED. attendees sostituisce integralmente la lista.
    etag: l'etag restituito da get/search/list per questo elemento. Se lo passi e nel frattempo un altro client
    l'ha modificato, l'operazione fallisce invece di sovrascrivere: rileggi e riprova.
    """
    return _cals().update(uid, calendar, occurrence=occurrence, expected_etag=etag, summary=summary, start=start, end=end, all_day=all_day,
                          location=location, description=description, attendees=attendees, alarms=alarms,
                          rrule=rrule, status=status, categories=categories, privacy=privacy, busy=busy, url=url)


@tool("destructive", open_world=True)
def delete_event(uid: str, calendar: str = "personal", occurrence: str | None = None,
                 etag: str | None = None) -> dict[str, Any]:
    """Elimina un evento. Con `occurrence` (recurrence_id o inizio di un'occorrenza) elimina SOLO quella dalla serie;
    senza, elimina tutto (l'intera serie se ricorrente). Un'occorrenza su un evento non ricorrente o inesistente
    dà errore: non viene mai cancellato l'intero evento al suo posto. Chiedi conferma all'utente prima.
    etag: l'etag restituito da get/search/list per questo elemento. Se lo passi e nel frattempo un altro client
    l'ha modificato, l'operazione fallisce invece di sovrascrivere: rileggi e riprova."""
    return _cals().delete(uid, calendar, occurrence=occurrence, expected_etag=etag)


@tool("destructive", open_world=True)
def respond_to_invitation(uid: str, response: str, calendar: str = "personal", comment: str | None = None) -> dict[str, Any]:
    """Risponde a un invito ricevuto: response = accept | decline | tentative. SOGo notifica l'organizzatore.
    Funziona solo se l'utente è tra gli attendee dell'evento."""
    return _cals().respond(uid, response, calendar, comment=comment)


# ---------------------------------------------------------------- task

@tool("read")
def list_tasks(calendar: str = "personal", include_completed: bool = False, limit: int = 200) -> list[dict[str, Any]]:
    """Task (to-do) del calendario, ordinati per scadenza e priorità. calendar="*" = tutti i calendari."""
    return _cals().tasks(calendar, include_completed=include_completed, limit=limit)


@tool("read")
def get_task(uid: str, calendar: str = "personal") -> dict[str, Any]:
    """Ritorna un task completo dato l'UID."""
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
    """Crea un task. due/start in ISO 8601 (solo data o data+ora). priority: high | medium | low oppure 1 (alta) … 9 (bassa).
    alarms come per gli eventi, relativi alla scadenza (es. ["1d"]); senza due sono relativi a start (serve uno dei due)."""
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
    """Aggiorna un task. `completed=true` lo segna completato (false lo riapre). status: NEEDS-ACTION | IN-PROCESS |
    COMPLETED | CANCELLED; percent 0-100. Stringa vuota "" rimuove il campo (es. due="").
    etag: l'etag restituito da get/search/list per questo elemento. Se lo passi e nel frattempo un altro client
    l'ha modificato, l'operazione fallisce invece di sovrascrivere: rileggi e riprova."""
    return _cals().update_task(uid, calendar, expected_etag=etag, summary=summary, due=due, description=description, priority=priority,
                               start=start, status=status, percent=percent, completed=completed,
                               categories=categories, alarms=alarms, location=location, url=url)


@tool("destructive")
def delete_task(uid: str, calendar: str = "personal", etag: str | None = None) -> dict[str, Any]:
    """Elimina definitivamente un task. Chiedi conferma all'utente prima.
    etag: l'etag restituito da get/search/list per questo elemento. Se lo passi e nel frattempo un altro client
    l'ha modificato, l'operazione fallisce invece di sovrascrivere: rileggi e riprova."""
    return _cals().delete_task(uid, calendar, expected_etag=etag)


# ---------------------------------------------------------------- entrypoint

_LOOPBACK = ("127.0.0.1", "localhost", "::1")


class _BearerAuth:
    """Middleware ASGI: ogni richiesta HTTP deve avere `Authorization: Bearer <token>`."""

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
        raise SystemExit("ERRORE: per ascoltare su un indirizzo non locale imposta MAILCOW_DAV_MCP_TOKEN "
                         "(oppure resta su 127.0.0.1 dietro un tunnel/reverse proxy con autenticazione).")
    extra = [h.strip() for h in os.environ.get("MAILCOW_DAV_MCP_ALLOWED_HOSTS", "").split(",") if h.strip()]
    hosts = ["127.0.0.1:*", "localhost:*", "[::1]:*"] + [h if ":" in h else f"{h}:*" for h in extra] + extra
    origins = ["http://127.0.0.1:*", "http://localhost:*"] + [f"https://{h}" for h in extra]
    mcp.settings.host, mcp.settings.port, mcp.settings.streamable_http_path = host, port, path
    mcp.settings.transport_security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True, allowed_hosts=hosts, allowed_origins=origins)
    export_dir = _confine()
    if not token:
        print("ATTENZIONE: nessun MAILCOW_DAV_MCP_TOKEN: ogni processo di questa macchina può leggere e modificare "
              "rubrica e calendario tramite questo endpoint. Imposta un token anche in locale.", file=sys.stderr)
    app: Any = mcp.streamable_http_app()
    if token:
        app = _BearerAuth(app, token)
    print(f"mailcow-dav-mcp: http://{host}:{port}{path} (auth: {'bearer token' if token else 'nessuna, solo locale'}; "
          f"file locali limitati a {export_dir})", file=sys.stderr)
    uvicorn.run(app, host=host, port=port, log_level="warning")


def _confine() -> Any:
    """File locali (import, export, foto) limitati alla directory di export + MAILCOW_DAV_LOCAL_DIRS, sempre:
    testo ostile in una nota non deve poter far leggere o caricare sul server un file qualsiasi del computer."""
    export_dir = default_export_dir()
    try:
        export_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:  # niente export/import, ma ricerca e calendario devono funzionare lo stesso
        print(f"ATTENZIONE: impossibile creare {export_dir} ({e}): import/export non disponibili", file=sys.stderr)
    extra = [d for d in os.environ.get("MAILCOW_DAV_LOCAL_DIRS", "").split(os.pathsep) if d.strip()]
    confine_local_files([export_dir, *extra])
    return export_dir


def _apply_read_only() -> None:
    if os.environ.get("MAILCOW_DAV_READ_ONLY", "").strip().lower() in ("1", "true", "yes", "on"):
        for name in sorted(WRITE_TOOLS):
            mcp.remove_tool(name)
        print(f"mailcow-dav-mcp: sola lettura, {len(WRITE_TOOLS)} tool di scrittura disattivati", file=sys.stderr)


def _check() -> None:
    try:
        info = dav_status()
    except DavError as e:
        print(f"ERRORE DAV: {e}", file=sys.stderr)
        sys.exit(2)
    except RuntimeError as e:
        print(f"ERRORE CONFIG: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:  # URL malformato o errore imprevisto: una riga chiara invece del traceback
        print(f"ERRORE: {type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(3)
    print(f"OK  server={info['server']}  user={info['user']}  principal={info['principal']}")
    print("Rubriche:  " + (", ".join(f"{b['id']} ({b['name']})" for b in info["addressbooks"]) or "-"))
    print("Calendari: " + (", ".join(f"{c['id']} ({c['name']})" for c in info["calendars"]) or "-"))


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
        raise SystemExit(f"ERRORE CONFIG: {e}") from None
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
        raise SystemExit(f"ERRORE: MAILCOW_DAV_MCP_TRANSPORT={transport!r} (usa stdio o http)")
    else:
        _confine()
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
