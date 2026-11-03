"""Calendario CalDAV (SOGo) — eventi VEVENT (con occorrenze singole), task VTODO, risposta inviti."""

from __future__ import annotations

import copy
import re
import time as _clock
import uuid
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import recurring_ical_events
from icalendar import Alarm, Calendar, Event, Todo, vCalAddress, vRecur, vText
from lxml import etree

from .dav import NS_APPLE, NS_CAL, NS_CS, NS_D, NSMAP, DavClient, NotFound, PreconditionFailed, Resource
from .safety import check_expected_etag, collection_segment, is_safe_name
from .tzgen import vtimezone

PRODID = "-//mailcow-dav-mcp//EN"
_ALARM_RE = re.compile(r"^(?:(display|email|audio)\s*:\s*)?(\d+)\s*(m|min|h|d|w)?$", re.I)
_UNIT = {"m": "minutes", "min": "minutes", "h": "hours", "d": "days", "w": "weeks"}
_PRIORITY_WORDS = {"high": 1, "alta": 1, "medium": 5, "media": 5, "normal": 5, "low": 9, "bassa": 9, "none": 0}
_RECURRENCE_PROPS = {"RRULE", "RDATE", "EXDATE", "EXRULE", "RECURRENCE-ID"}
_GENERATED = "X-MAILCOW-DAV-MCP"  # marca le VTIMEZONE generate da noi (le altre non si toccano)
MAX_EXPAND = 2000  # occorrenze massime espanse per singola risorsa in una lettura
_SERIES_SCAN = 10000  # occorrenze massime percorse per calcolare la fine di una serie con COUNT
_EXPAND_SECONDS = 5.0  # budget di tempo per espandere una risorsa
_TZ_YEARS = 50  # copertura massima delle VTIMEZONE generate oltre oggi
_CTRL_RE = re.compile(r"[\r\n\x00]")


def _rrule_problem(comp: Event | Todo) -> str | None:
    """Motivo per cui una regola di ricorrenza non va espansa (dateutil cicla all'infinito con INTERVAL=0,
    e SECONDLY/MINUTELY generano milioni di occorrenze), oppure None se è gestibile."""
    for key in ("rrule", "exrule"):
        rules = comp.get(key)
        for rule in (rules if isinstance(rules, list) else [rules] if rules is not None else []):
            freq = str((rule.get("FREQ") or [""])[0]).upper()
            if freq in ("SECONDLY", "MINUTELY"):
                return f"FREQ={freq} non supportata"
            try:
                interval = int((rule.get("INTERVAL") or [1])[0])
            except (TypeError, ValueError):
                return "INTERVAL non valido"
            if interval < 1:
                return "INTERVAL deve essere almeno 1"
    return None


def _check_rrule(text: str, start: date | datetime | None = None) -> vRecur:
    """Valida una RRULE scritta dall'utente prima di salvarla: niente a capo, niente regole pericolose, e
    deve produrre almeno un'occorrenza in tempi brevi (una regola impossibile fa girare dateutil per secondi)."""
    if _CTRL_RE.search(text):
        raise ValueError("rrule non può contenere a capo")
    rule = vRecur.from_ical(text)
    probe = Event()
    probe.add("rrule", rule)
    problem = _rrule_problem(probe)
    if problem:
        raise ValueError(f"rrule: {problem} (usa almeno FREQ=HOURLY e INTERVAL>=1)")
    if start is not None:
        _probe_rrule(rule.to_ical().decode(), start)
    return rule


_PROBE = """
import sys
from datetime import datetime
from zoneinfo import ZoneInfo
from dateutil.rrule import rrulestr
start = datetime.fromisoformat(sys.argv[2])
if sys.argv[3]:
    start = start.replace(tzinfo=ZoneInfo(sys.argv[3]))
print("ok" if next(iter(rrulestr(sys.argv[1], dtstart=start)), None) is not None else "none")
"""


def _probe_rrule(text: str, start: date | datetime) -> None:
    """La regola deve produrre almeno un'occorrenza. Il calcolo gira in un processo separato con timeout:
    dateutil non è interrompibile, e un thread bloccato su una regola impossibile resterebbe occupato."""
    import subprocess
    import sys

    zone = ""
    if isinstance(start, datetime) and start.tzinfo is not None:
        # ora locale nel suo fuso: BYHOUR & co. valgono sull'ora locale, non su quella UTC
        zone = getattr(start.tzinfo, "key", None) or "UTC"
        dt = start.astimezone(ZoneInfo(zone)).replace(tzinfo=None)
        text = re.sub(r"UNTIL=(\d{8})(?:T(\d{6}))?Z?", lambda m: f"UNTIL={m.group(1)}T{m.group(2) or '235959'}Z", text)
    else:
        dt = start if isinstance(start, datetime) else datetime.combine(start, time(0, 0))
        text = re.sub(r"(UNTIL=\d{8}(?:T\d{6})?)Z", r"\1", text)
    try:
        res = subprocess.run([sys.executable, "-I", "-c", _PROBE, text, dt.isoformat(), zone],
                             capture_output=True, text=True, timeout=3)
    except subprocess.TimeoutExpired:
        raise ValueError("rrule troppo costosa da calcolare (nessuna occorrenza trovata in 3 secondi)") from None
    if res.returncode != 0:
        raise ValueError("rrule non valida: " + (res.stderr.strip().splitlines() or ["errore"])[-1])
    if res.stdout.strip() != "ok":
        raise ValueError("rrule non produce nessuna occorrenza (combinazione di BYxxx impossibile?)")


# -- date/ora ---------------------------------------------------------------

def parse_dt(value: str, tz: ZoneInfo, *, all_day: bool = False) -> date | datetime:
    """ISO 8601 → date (all_day) o datetime tz-aware. Senza offset assume tz."""
    v = value.strip()
    if all_day:
        return date.fromisoformat(v[:10])
    if len(v) == 10:  # solo data → mezzanotte locale
        return datetime.combine(date.fromisoformat(v), time(0, 0), tzinfo=tz)
    dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        return dt.replace(tzinfo=tz)
    if not isinstance(dt.tzinfo, ZoneInfo) and dt.utcoffset() != timedelta(0):
        # offset fisso ("+02:00"): stesso istante nel fuso IANA configurato, così la TZID ha una VTIMEZONE vera.
        # Nell'ora ripetuta del cambio d'ora l'ora locale è ambigua (DTSTART;TZID non porta il "fold"): resto in UTC.
        local = dt.astimezone(tz)
        if local.replace(fold=0).utcoffset() != local.replace(fold=1).utcoffset():
            return dt.astimezone(timezone.utc)
        return local
    return dt


def _utc_stamp(dt: date | datetime, tz: ZoneInfo) -> str:
    if isinstance(dt, datetime):
        return dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return datetime.combine(dt, time(0, 0), tzinfo=tz).astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _iso(v: Any, tz: ZoneInfo) -> str | None:
    if v is None:
        return None
    if isinstance(v, datetime):
        return (v.astimezone(tz) if v.tzinfo else v.replace(tzinfo=tz)).isoformat()
    if isinstance(v, date):
        return v.isoformat()
    return str(v)


def _utc(x: date | datetime, tz: ZoneInfo) -> datetime:
    """Istante in UTC (confronti corretti anche a cavallo del cambio d'ora, dove l'ora locale si ripete)."""
    if isinstance(x, datetime):
        return (x if x.tzinfo else x.replace(tzinfo=tz)).astimezone(timezone.utc)
    return datetime.combine(x, time(0, 0), tzinfo=tz).astimezone(timezone.utc)


def _same_instant(a: date | datetime, b: date | datetime, tz: ZoneInfo) -> bool:
    def norm(x: date | datetime) -> datetime:
        if isinstance(x, datetime):
            return (x if x.tzinfo else x.replace(tzinfo=tz)).astimezone(timezone.utc)
        return datetime.combine(x, time(0, 0), tzinfo=tz).astimezone(timezone.utc)
    return norm(a) == norm(b)


# -- promemoria -----------------------------------------------------------------

def parse_alarm(spec: str) -> tuple[str, timedelta]:
    """'15m' → (DISPLAY, -15min); 'email:1d' → (EMAIL, -1 giorno); '0' → all'inizio."""
    m = _ALARM_RE.match(spec.strip())
    if not m:
        raise ValueError(f"Promemoria non valido: {spec!r} (usa es. '15m', '2h', '1d', 'email:1d')")
    action = (m.group(1) or "display").upper()
    n = int(m.group(2))
    unit = _UNIT[(m.group(3) or "m").lower()]
    return action, -timedelta(**{unit: n})


def alarm_to_str(comp: Alarm) -> str:
    action = str(comp.get("action", "DISPLAY")).upper()
    trig = comp.get("trigger")
    prefix = "" if action == "DISPLAY" else action.lower() + ":"
    if trig is None:
        return prefix + "?"
    v = trig.dt
    if isinstance(v, timedelta):
        secs = int(abs(v.total_seconds()))
        if secs % 86400 == 0 and secs:
            return f"{prefix}{secs // 86400}d"
        if secs % 3600 == 0 and secs:
            return f"{prefix}{secs // 3600}h"
        return f"{prefix}{secs // 60}m"
    return prefix + v.isoformat()


def _set_alarms(comp: Event | Todo, alarms: list[str] | None, summary_hint: str, email: str | None,
                related: str | None = None) -> None:
    """related="END": trigger relativo alla fine (per i task: la scadenza DUE)."""
    if alarms is None:
        return
    for sub in list(comp.subcomponents):
        if sub.name == "VALARM":
            comp.subcomponents.remove(sub)
    for spec in alarms:
        if not spec or not spec.strip():
            continue
        action, delta = parse_alarm(spec)
        al = Alarm()
        al.add("action", action)
        al.add("trigger", delta, parameters={"RELATED": related} if related == "END" else None)
        if action == "EMAIL":
            al.add("summary", summary_hint)
            al.add("description", summary_hint)
            if email:
                al.add("attendee", vCalAddress(f"mailto:{email}"), encode=0)
        else:
            al.add("description", summary_hint)
        comp.add_component(al)


# -- helper comuni --------------------------------------------------------------

def _mail(v: Any) -> str:
    return str(v).replace("mailto:", "").replace("MAILTO:", "").strip()


def _categories(comp: Event | Todo) -> list[str]:
    out: list[str] = []
    cats = comp.get("categories")
    for c in (cats if isinstance(cats, list) else [cats] if cats is not None else []):
        v = getattr(c, "cats", None)
        if v is None:
            v = [x for x in str(c).split(",")]
        out.extend(str(x).strip() for x in v if str(x).strip())
    return out


def _set_text(comp: Event | Todo, name: str, value: str | None) -> None:
    if value is None:
        return
    if name in comp:
        del comp[name]
    if value != "":
        comp.add(name, vText(value))


def _set_categories(comp: Event | Todo, categories: list[str] | None) -> None:
    if categories is None:
        return
    if "categories" in comp:
        del comp["categories"]
    clean = [c.strip() for c in categories if c and c.strip()]
    if clean:
        comp.add("categories", clean)


def _touch(comp: Event | Todo) -> None:
    if "dtstamp" not in comp:
        comp.add("dtstamp", datetime.now(timezone.utc))
    if "last-modified" in comp:
        del comp["last-modified"]
    comp.add("last-modified", datetime.now(timezone.utc))
    if "sequence" in comp:
        comp["sequence"] = int(comp["sequence"]) + 1
    else:
        comp.add("sequence", 0)


# -- eventi: conversione ----------------------------------------------------------

def event_to_dict(ev: Event, tz: ZoneInfo, href: str | None = None, etag: str | None = None) -> dict[str, Any]:
    def s(name: str) -> str | None:
        v = ev.get(name)
        return str(v).strip() if v is not None else None

    dtstart = ev.decoded("dtstart") if ev.get("dtstart") is not None else None
    dtend = ev.decoded("dtend") if ev.get("dtend") is not None else None
    if dtend is None and dtstart is not None and ev.get("duration") is not None:
        dtend = dtstart + ev.decoded("duration")
    all_day = isinstance(dtstart, date) and not isinstance(dtstart, datetime)
    if all_day and dtend is None and dtstart is not None:
        dtend = dtstart + timedelta(days=1)

    attendees = []
    att = ev.get("attendee")
    for a in (att if isinstance(att, list) else [att] if att is not None else []):
        params = getattr(a, "params", {}) or {}
        attendees.append({"email": _mail(a), "name": params.get("CN"), "status": params.get("PARTSTAT"),
                          "role": params.get("ROLE")})

    organizer = ev.get("organizer")
    rrule = ev.get("rrule")
    rec_id = ev.decoded("recurrence-id") if ev.get("recurrence-id") is not None else None
    exdates: list[str] = []
    ex = ev.get("exdate")
    for e in (ex if isinstance(ex, list) else [ex] if ex is not None else []):
        for d in e.dts:
            exdates.append(_iso(d.dt, tz) or "")
    transp = s("transp")

    out: dict[str, Any] = {
        "uid": s("uid"),
        "summary": s("summary"),
        "start": _iso(dtstart, tz),
        "end": _iso(dtend, tz),
        "all_day": all_day,
        "location": s("location"),
        "description": s("description"),
        "status": s("status"),
        "organizer": _mail(organizer) if organizer else None,
        "attendees": attendees,
        "categories": _categories(ev),
        "privacy": (s("class") or "PUBLIC").lower() if s("class") else None,
        "busy": "free" if transp == "TRANSPARENT" else None,
        "url": s("url"),
        "rrule": rrule.to_ical().decode() if rrule is not None else None,
        "exdates": exdates,
        "recurrence_id": _iso(rec_id, tz),
        "alarms": [alarm_to_str(c) for c in ev.walk("VALARM")],
        "last_modified": _iso(ev.decoded("last-modified"), tz) if ev.get("last-modified") is not None else None,
        "href": href,
        "etag": etag,
    }
    return {k: v for k, v in out.items() if v not in (None, [], "") or k == "all_day"}


def apply_event_fields(ev: Event, tz: ZoneInfo, *, summary: str | None = None, start: str | None = None,
                       end: str | None = None, all_day: bool | None = None, location: str | None = None,
                       description: str | None = None, attendees: list[str] | None = None,
                       alarms: list[str] | None = None, rrule: str | None = None,
                       status: str | None = None, organizer: str | None = None,
                       categories: list[str] | None = None, privacy: str | None = None,
                       busy: str | None = None, url: str | None = None) -> None:
    """Applica i campi non-None. Stringa vuota = rimuovi. Liste sostituiscono integralmente."""
    _set_text(ev, "summary", summary)
    _set_text(ev, "location", location)
    _set_text(ev, "description", description)
    _set_text(ev, "url", url)
    if status is not None:
        _set_text(ev, "status", status.upper() if status else "")
    if privacy is not None:
        _set_text(ev, "class", privacy.upper() if privacy else "")
    if busy is not None:
        b = busy.strip().lower()
        _set_text(ev, "transp", "" if not b else "TRANSPARENT" if b in ("free", "libero", "transparent") else "OPAQUE")
    _set_categories(ev, categories)

    current_start = ev.decoded("dtstart") if ev.get("dtstart") is not None else None
    current_end = ev.decoded("dtend") if ev.get("dtend") is not None else None
    if current_end is None and current_start is not None and ev.get("duration") is not None:
        current_end = current_start + ev.decoded("duration")
    cur_all_day = isinstance(current_start, date) and not isinstance(current_start, datetime)

    # Le date si toccano solo se richiesto: cambiare il titolo non deve ricalcolare (e accorciare) l'evento.
    if start is not None or end is not None or all_day is not None:
        target_all_day = cur_all_day if all_day is None else all_day
        to_timed = current_start is not None and cur_all_day and not target_all_day
        to_all_day = current_start is not None and not cur_all_day and target_all_day
        if to_timed and not start:
            raise ValueError("Per trasformare un evento di un giorno intero in un evento con orario "
                             "indica start (ed eventualmente end) con l'ora")
        new_start = parse_dt(start, tz, all_day=target_all_day) if start else current_start
        if target_all_day and isinstance(new_start, datetime):
            new_start = new_start.astimezone(tz).date()
        if end:
            new_end = parse_dt(end, tz, all_day=target_all_day)
        elif to_timed:
            new_end = None  # la durata in giorni non ha senso per un evento con orario: default 1 ora
        elif start and current_start is not None and current_end is not None and not to_all_day:
            new_end = new_start + (current_end - current_start)  # spostamento: conserva la durata
        else:
            new_end = current_end
        if target_all_day and isinstance(new_end, datetime):
            new_end = new_end.astimezone(tz).date()
        if not target_all_day and new_end is not None and not isinstance(new_end, datetime):
            new_end = None  # fine "giornaliera" rimasta su un evento con orario
        if target_all_day and new_start is not None and (new_end is None or new_end <= new_start):
            new_end = new_start + timedelta(days=1)  # DTEND esclusivo per eventi giornalieri
        if not target_all_day and new_start is not None and new_end is None:
            new_end = new_start + timedelta(hours=1)
        if not target_all_day and new_start is not None and new_end is not None and _utc(new_end, tz) < _utc(new_start, tz):
            raise ValueError("end deve essere successivo a start")
        if new_start is not None:
            if "dtstart" in ev:
                del ev["dtstart"]
            ev.add("dtstart", new_start)
        if new_end is not None:
            for k in ("dtend", "duration"):
                if k in ev:
                    del ev[k]
            ev.add("dtend", new_end)

    if attendees is not None:
        if "attendee" in ev:
            del ev["attendee"]
        for raw in attendees:
            raw = raw.strip()
            if not raw:
                continue
            if _CTRL_RE.search(raw):
                raise ValueError(f"attendee non valido (contiene a capo): {raw!r}")
            name, email = (raw.rsplit("<", 1)[0].strip(), raw.rsplit("<", 1)[1].rstrip(">").strip()) if "<" in raw else (None, raw)
            if not re.fullmatch(r"[^\s<>@]+@[^\s<>@]+", email):
                raise ValueError(f"Email partecipante non valida: {email!r}")
            a = vCalAddress(f"mailto:{email}")
            if name:
                a.params["CN"] = vText(name)
            a.params["ROLE"] = vText("REQ-PARTICIPANT")
            a.params["PARTSTAT"] = vText("NEEDS-ACTION")
            a.params["RSVP"] = vText("TRUE")
            ev.add("attendee", a, encode=0)
        if attendees and "organizer" not in ev and organizer:
            ev.add("organizer", vCalAddress(f"mailto:{organizer}"), encode=0)

    if rrule is not None:
        if "rrule" in ev:
            del ev["rrule"]
        if rrule:
            start_val = ev.decoded("dtstart") if ev.get("dtstart") is not None else None
            ev.add("rrule", _check_rrule(rrule.strip().removeprefix("RRULE:"), start_val))
        else:
            # rrule="" = evento singolo: via anche date aggiuntive/escluse (le eccezioni le toglie Calendars.update)
            for k in ("rdate", "exdate", "exrule"):
                if k in ev:
                    del ev[k]

    _set_alarms(ev, alarms, summary or str(ev.get("summary", "Promemoria")), organizer)
    _touch(ev)


# -- task: conversione ------------------------------------------------------------

def todo_to_dict(td: Todo, tz: ZoneInfo, href: str | None = None, etag: str | None = None) -> dict[str, Any]:
    def s(name: str) -> str | None:
        v = td.get(name)
        return str(v).strip() if v is not None else None

    def d(name: str) -> str | None:
        return _iso(td.decoded(name), tz) if td.get(name) is not None else None

    prio = td.get("priority")
    out: dict[str, Any] = {
        "uid": s("uid"),
        "summary": s("summary"),
        "description": s("description"),
        "status": s("status") or "NEEDS-ACTION",
        "due": d("due"),
        "start": d("dtstart"),
        "completed_at": d("completed"),
        "percent": int(td.get("percent-complete")) if td.get("percent-complete") is not None else None,
        "priority": int(prio) if prio is not None and int(prio) > 0 else None,
        "categories": _categories(td),
        "location": s("location"),
        "url": s("url"),
        "alarms": [alarm_to_str(c) for c in td.walk("VALARM")],
        "last_modified": d("last-modified"),
        "href": href,
        "etag": etag,
    }
    return {k: v for k, v in out.items() if v not in (None, [], "")}


def apply_todo_fields(td: Todo, tz: ZoneInfo, *, summary: str | None = None, description: str | None = None,
                      due: str | None = None, start: str | None = None, priority: str | int | None = None,
                      status: str | None = None, percent: int | None = None, completed: bool | None = None,
                      categories: list[str] | None = None, alarms: list[str] | None = None,
                      location: str | None = None, url: str | None = None, email: str | None = None) -> None:
    _set_text(td, "summary", summary)
    _set_text(td, "description", description)
    _set_text(td, "location", location)
    _set_text(td, "url", url)
    _set_categories(td, categories)

    if start == "" and td.get("duration") is not None and td.get("dtstart") is not None and due is None \
            and td.get("due") is None:
        # DURATION senza DTSTART non è valida: la converto nella scadenza equivalente
        td.add("due", td.decoded("dtstart") + td.decoded("duration"))
    for name, value in (("due", due), ("dtstart", start)):
        if value is None:
            continue
        if name in td:
            del td[name]
        if value != "":
            td.add(name, parse_dt(value, tz, all_day=len(value.strip()) == 10))
    if td.get("due") is not None or td.get("dtstart") is None:
        if "duration" in td:
            del td["duration"]  # DUE e DURATION si escludono (RFC 5545 §3.6.2)
    if (due or start) and td.get("due") is not None and td.get("dtstart") is not None:
        d_due, d_start = td.decoded("due"), td.decoded("dtstart")
        if isinstance(d_due, datetime) != isinstance(d_start, datetime):
            raise ValueError("due e start devono essere entrambe date (YYYY-MM-DD) o entrambe data+ora")
        if _utc(d_due, tz) <= _utc(d_start, tz):
            raise ValueError("due (scadenza) deve essere successiva a start")

    if priority is not None:
        if "priority" in td:
            del td["priority"]
        if priority != "":
            p = _PRIORITY_WORDS.get(str(priority).lower(), None) if isinstance(priority, str) else int(priority)
            if p is None:
                p = int(priority)
            if not 0 <= p <= 9:
                raise ValueError("priority: 1 (alta) … 9 (bassa), 0 = nessuna, oppure high/medium/low")
            if p:
                td.add("priority", p)

    if completed is not None:
        status = "COMPLETED" if completed else "NEEDS-ACTION"
        percent = 100 if completed else (percent if percent is not None else 0)
    if status is not None:
        _set_text(td, "status", status.upper() if status else "")
        if "completed" in td:
            del td["completed"]
        if status.upper() == "COMPLETED":
            td.add("completed", datetime.now(timezone.utc))
            percent = 100
    if percent is not None:
        if "percent-complete" in td:
            del td["percent-complete"]
        td.add("percent-complete", max(0, min(100, int(percent))))

    related = "END" if td.get("due") is not None else None  # promemoria relativi alla scadenza
    _set_alarms(td, alarms, summary or str(td.get("summary", "Promemoria")), email, related=related)
    _anchor_todo_alarms(td)
    _touch(td)


def _anchor_todo_alarms(td: Todo) -> None:
    """Riaggancia solo i trigger relativi rimasti senza ancora: RELATED=END senza DUE passa a DTSTART,
    RELATED=START (default) senza DTSTART passa a DUE. Gli ancoraggi validi restano come sono."""
    has_due = td.get("due") is not None or (td.get("dtstart") is not None and td.get("duration") is not None)
    has_start = td.get("dtstart") is not None
    for a in td.subcomponents:
        if a.name != "VALARM" or a.get("trigger") is None or not isinstance(a["trigger"].dt, timedelta):
            continue
        trig = a["trigger"]
        related = str(trig.params.get("RELATED", "START")).upper()
        if related == "END" and not has_due:
            if not has_start:
                raise _no_anchor()
            del trig.params["RELATED"]
        elif related != "END" and not has_start:
            if not has_due:
                raise _no_anchor()
            trig.params["RELATED"] = "END"


def _no_anchor() -> ValueError:
    return ValueError("I promemoria di un task richiedono una scadenza (due) o un inizio (start): "
                      "imposta una data oppure togli i promemoria con alarms=[]")


# -- VCALENDAR ---------------------------------------------------------------------

def new_calendar(comp: Event | Todo) -> Calendar:
    cal = Calendar()
    cal.add("prodid", PRODID)
    cal.add("version", "2.0")
    cal.add_component(comp)
    _add_timezones(cal)
    return cal


def _as_date(v: Any) -> date | None:
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    return None


def _series_end(comp: Event | Todo) -> date | None:
    """Data (limite superiore) dell'ultima occorrenza di una serie con COUNT/UNTIL; None se la serie non
    finisce o è troppo lunga da percorrere. UNTIL si legge direttamente, senza espandere la serie."""
    from dateutil.rrule import rrulestr

    rule = comp.get("rrule")
    start = comp.decoded("dtstart") if comp.get("dtstart") is not None else None
    if rule is None or start is None or _rrule_problem(comp):
        return None
    text = rule.to_ical().decode()
    m = re.search(r"UNTIL=(\d{8})", text)
    if m:
        return datetime.strptime(m.group(1), "%Y%m%d").date()
    if "COUNT" not in rule:
        return None
    naive = start.replace(tzinfo=None) if isinstance(start, datetime) else datetime.combine(start, time(0, 0))
    last = naive
    deadline = _clock.monotonic() + _EXPAND_SECONDS
    try:
        for i, occ in enumerate(rrulestr(text, dtstart=naive)):
            if i >= _SERIES_SCAN or _clock.monotonic() > deadline:
                return None
            last = occ
    except Exception:
        return None
    return last.date()


def _add_timezones(cal: Calendar) -> None:
    """Aggiunge una VTIMEZONE per ogni TZID usato e non ancora presente, valida sull'intervallo delle date
    che la usano (le regole attuali non vanno applicate a date passate con regole diverse)."""
    used: dict[str, list[date]] = {}
    for comp in cal.walk():
        if comp.name in ("VTIMEZONE", "STANDARD", "DAYLIGHT"):
            continue
        for _, prop in comp.property_items(recursive=False):
            params = getattr(prop, "params", None)
            tzid = params.get("TZID") if params else None
            if not tzid:
                continue
            dates = [_as_date(getattr(prop, "dt", None))]
            dates += [_as_date(x.dt) for x in getattr(prop, "dts", [])]
            used.setdefault(str(tzid), []).extend(d for d in dates if d)
    # Le VTIMEZONE generate da noi si rigenerano sull'intervallo attuale delle date (dopo uno spostamento possono
    # non coprirle più). Quelle scritte da altri client restano intatte: sono la loro definizione dell'ora.
    for t in list(cal.subcomponents):
        if t.name == "VTIMEZONE" and str(t.get("tzid")) in used and t.get(_GENERATED) is not None:
            cal.subcomponents.remove(t)
    present = {str(t.get("tzid")) for t in cal.walk("VTIMEZONE")}
    missing = [t for t in used if t not in present]
    unknown = []
    for tzid in missing:
        try:
            zi = ZoneInfo(tzid)
        except Exception:
            unknown.append(tzid)
            continue
        dates = used[tzid]
        last = max(dates) if dates else None
        for c in list(cal.walk("VEVENT")) + list(cal.walk("VTODO")):
            if c.get("rrule") is not None:
                series_end = _series_end(c)
                last = None if series_end is None or last is None else max(last, series_end)
                if series_end is None:
                    break
        # le serie lunghissime si coprono fino a oggi+50 anni; le date vere degli eventi sempre
        horizon = max(date(date.today().year + _TZ_YEARS, 12, 31), max(dates) if dates else date.min)
        first = max(min(dates), date(1900, 1, 1)) if dates else None
        last = min(last, horizon) if last else None
        if first and last and last < first:
            last = first
        vtz = vtimezone(zi, first, last)
        vtz.add(_GENERATED, "generated")
        cal.add_component(vtz)
    if unknown:
        try:
            cal.add_missing_timezones()
        except Exception:
            pass
        present = {str(t.get("tzid")) for t in cal.walk("VTIMEZONE")}
        still = [t for t in unknown if t not in present]
        if still:
            raise ValueError(f"Fuso orario sconosciuto {', '.join(still)}: impossibile generare la VTIMEZONE")


def serialize(cal: Calendar) -> str:
    return cal.to_ical().decode("utf-8")


# -- client ---------------------------------------------------------------------

def _has_uid(cal: Calendar, comp: str, uid: str) -> bool:
    """True se il Calendar contiene un componente `comp` con esattamente quell'UID (gli UID sono case-sensitive)."""
    u = uid.strip()
    return any(str(c.get("UID", "")).strip() == u for c in cal.walk(comp))


def _master_of(cal: Calendar, comp: str, uid: str | None = None) -> Event | Todo:
    """Il componente principale della serie (senza RECURRENCE-ID), qualunque sia l'ordine nel file.
    Con `uid` considera solo i componenti con quell'UID (una risorsa con più UID non deve confondere)."""
    items = [c for c in cal.walk(comp) if uid is None or str(c.get("UID", "")).strip() == uid.strip()]
    masters = [c for c in items if c.get("recurrence-id") is None]
    if masters:
        return masters[0]
    if items:
        return items[0]
    raise NotFound(f"Nessun {comp} nella risorsa")


def _vevents(cal: Calendar, uid: str) -> list[Event]:
    return [ev for ev in cal.walk("VEVENT") if str(ev.get("UID", "")).strip() == uid.strip()]


def _only_uid(cal: Calendar, uid: str) -> Calendar:
    """Copia del Calendar con le sole VTIMEZONE e i componenti di quell'UID."""
    out = Calendar()
    for k, v in cal.items():
        out[k] = v
    for c in cal.subcomponents:
        if c.name == "VTIMEZONE" or str(c.get("UID", "")).strip() == uid.strip():
            out.add_component(c)
    return out


def _is_series(master: Event | Todo) -> bool:
    return master.get("rrule") is not None or master.get("rdate") is not None


class Calendars:
    ALL = "*"

    def __init__(self, dav: DavClient, principal_path: str, tz: str = "Europe/Rome", user: str | None = None):
        self.dav = dav
        self.root = principal_path.rstrip("/") + "/Calendar/"
        self.tz = ZoneInfo(tz)
        self.user = user

    def cal_path(self, calendar: str) -> str:
        return f"{self.root}{collection_segment(calendar, 'calendario')}/"

    def list(self) -> list[dict[str, Any]]:
        res = self.dav.propfind(self.root, [
            (NS_D, "displayname"), (NS_D, "resourcetype"), (NS_CS, "getctag"),
            (NS_APPLE, "calendar-color"), (NS_CAL, "supported-calendar-component-set"),
        ], depth=1)
        out = []
        for r in res:
            if not r.has_child(NS_D, "resourcetype", NS_CAL, "calendar"):
                continue
            comps_el = r.props.get(f"{{{NS_CAL}}}supported-calendar-component-set")
            comps = [c.get("name") for c in comps_el] if comps_el is not None else []
            out.append({"id": r.name, "name": r.text(NS_D, "displayname") or r.name, "href": r.href,
                        "color": r.text(NS_APPLE, "calendar-color"), "components": comps,
                        "ctag": r.text(NS_CS, "getctag")})
        return out

    def _calendar_ids(self, calendar: str) -> list[tuple[str, str]]:
        """[(id, nome)] — tutti i calendari se calendar == '*'."""
        if calendar != self.ALL:
            return [(calendar, calendar)]
        return [(c["id"], c["name"]) for c in self.list()]

    # -- query ------------------------------------------------------------

    def _query(self, calendar: str, comp: str, *, time_range: tuple[str, str] | None = None,
               uid: str | None = None) -> list[Resource]:
        root = etree.Element(f"{{{NS_CAL}}}calendar-query", nsmap=NSMAP)
        prop = etree.SubElement(root, f"{{{NS_D}}}prop")
        etree.SubElement(prop, f"{{{NS_D}}}getetag")
        etree.SubElement(prop, f"{{{NS_CAL}}}calendar-data")
        filt = etree.SubElement(root, f"{{{NS_CAL}}}filter")
        cf = etree.SubElement(filt, f"{{{NS_CAL}}}comp-filter", name="VCALENDAR")
        cf2 = etree.SubElement(cf, f"{{{NS_CAL}}}comp-filter", name=comp)
        if time_range:
            etree.SubElement(cf2, f"{{{NS_CAL}}}time-range", start=time_range[0], end=time_range[1])
        if uid:
            pf = etree.SubElement(cf2, f"{{{NS_CAL}}}prop-filter", name="UID")
            tm = etree.SubElement(pf, f"{{{NS_CAL}}}text-match", collation="i;octet")
            tm.text = uid
        body = etree.tostring(root, xml_declaration=True, encoding="utf-8")
        return self.dav.report(self.cal_path(calendar), body, depth=1)

    def _parse_events(self, resources: list[Resource], *, expand_between: tuple[datetime, datetime] | None = None
                      ) -> list[dict[str, Any]]:
        """Con expand_between, le ricorrenze vengono espanse LOCALMENTE (recurring-ical-events):
        non ci si affida all'expand del server, che in alcuni casi sbaglia i fusi."""
        out = []
        for r in resources:
            data = r.text(NS_CAL, "calendar-data")
            if not data:
                continue
            try:
                cal = Calendar.from_ical(data)
            except Exception:
                continue
            if expand_between is None:
                for ev in cal.walk("VEVENT"):
                    out.append(event_to_dict(ev, self.tz, href=r.href, etag=r.etag))
                continue
            recurring_uids = {str(ev.get("uid")) for ev in cal.walk("VEVENT")
                              if ev.get("rrule") is not None or ev.get("rdate") is not None
                              or ev.get("recurrence-id") is not None}
            occurrences, truncated = self._expand(cal, *expand_between)
            for ev in occurrences:
                d = event_to_dict(ev, self.tz, href=r.href, etag=r.etag)
                if truncated:
                    d["truncated"] = True  # serie oltre MAX_EXPAND: mancano occorrenze, restringi l'intervallo
                if d.get("uid") not in recurring_uids:
                    d.pop("recurrence_id", None)  # recurring-ical-events aggiunge RECURRENCE-ID anche ai singoli
                elif not d.get("recurrence_id"):
                    d["recurrence_id"] = d["start"]
                out.append(d)
        return out

    def _expand(self, cal: Calendar, start: datetime, end: datetime) -> tuple[list[Event], bool]:
        """Occorrenze in [start, end) con un tetto (MAX_EXPAND): una regola enorme non esaurisce la memoria."""
        out: list[Event] = []
        end_utc = _utc(end, self.tz)
        if any(_rrule_problem(ev) for ev in cal.walk("VEVENT")):
            return list(cal.walk("VEVENT")), True  # regola non espandibile in sicurezza: componenti così come sono
        deadline = _clock.monotonic() + _EXPAND_SECONDS
        try:
            for occ in recurring_ical_events.of(cal, keep_recurrence_attributes=True).after(start):
                if occ.get("dtstart") is not None and _utc(occ.decoded("dtstart"), self.tz) >= end_utc:
                    break
                if len(out) >= MAX_EXPAND or _clock.monotonic() > deadline:
                    # oltre il tetto restano comunque le eccezioni (occorrenze modificate) nell'intervallo
                    start_utc = _utc(start, self.tz)
                    for ex in cal.walk("VEVENT"):
                        if ex.get("recurrence-id") is not None and ex.get("dtstart") is not None \
                                and start_utc <= _utc(ex.decoded("dtstart"), self.tz) < end_utc:
                            out.append(ex)
                    return out, True
                out.append(occ)
        except Exception:
            return list(cal.walk("VEVENT")), False
        return out, False

    def events(self, calendar: str = "personal", start: str | None = None, end: str | None = None,
               expand: bool = True, limit: int | None = 200) -> list[dict[str, Any]]:
        """Eventi nell'intervallo [start, end). Default: da oggi a +30 giorni. calendar='*' = tutti."""
        s = parse_dt(start, self.tz) if start else datetime.now(self.tz).replace(hour=0, minute=0, second=0, microsecond=0)
        e = parse_dt(end, self.tz) if end else s + timedelta(days=30)
        rng = (_utc_stamp(s, self.tz), _utc_stamp(e, self.tz))
        results: list[dict[str, Any]] = []
        for cal_id, cal_name in self._calendar_ids(calendar):
            found = self._parse_events(self._query(cal_id, "VEVENT", time_range=rng),
                                       expand_between=(s, e) if expand else None)
            for d in found:
                d["calendar"] = cal_id
                if cal_name != cal_id:
                    d["calendar_name"] = cal_name
            results.extend(found)
        results.sort(key=lambda x: x.get("start") or "")
        return results if limit is None else results[:limit]

    def search(self, query: str, calendar: str = "personal", start: str | None = None,
               end: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        """Ricerca testuale in summary/location/description/categorie (default: ±365 giorni)."""
        now = datetime.now(self.tz)
        s = start or (now - timedelta(days=365)).isoformat()
        e = end or (now + timedelta(days=365)).isoformat()
        q = query.lower()
        found = [ev for ev in self.events(calendar, s, e, expand=True, limit=None)
                 if q in " ".join(str(ev.get(k, "")) for k in ("summary", "location", "description", "categories")).lower()]
        return found[:limit]

    def _find(self, uid: str, calendar: str, comp: str = "VEVENT") -> tuple[str, Calendar, str | None, str]:
        """Ritorna (href, Calendar, etag, calendar_id). calendar='*' cerca in tutti."""
        for cal_id, _ in self._calendar_ids(calendar):
            path = self.cal_path(cal_id)
            if is_safe_name(uid):
                try:
                    text, etag = self.dav.get(f"{path}{uid}.ics")
                    cal = Calendar.from_ical(text)
                    if _has_uid(cal, comp, uid):
                        return f"{path}{uid}.ics", cal, etag, cal_id
                except NotFound:
                    pass
            # Il server può ignorare il filtro UID e restituire più componenti: verifico sempre l'UID.
            for r in self._query(cal_id, comp, uid=uid):
                data = r.text(NS_CAL, "calendar-data")
                if not data:
                    continue
                cal = Calendar.from_ical(data)
                if _has_uid(cal, comp, uid):
                    if not r.etag:  # contenuto ed ETag devono essere coerenti: li rileggo insieme
                        text, etag = self.dav.get(r.href)
                        fresh = Calendar.from_ical(text)
                        if not _has_uid(fresh, comp, uid):  # sostituita tra REPORT e GET: non è più lei
                            raise PreconditionFailed(f"{comp} {uid!r} sostituito da un altro client: riprova")
                        return r.href, fresh, etag, cal_id
                    return r.href, cal, r.etag, cal_id
        raise NotFound(f"{comp} con UID {uid!r} non trovato in {calendar!r}")

    def _put(self, href: str, cal: Calendar, etag: str | None) -> str | None:
        _add_timezones(cal)
        return self.dav.put(href, serialize(cal), "text/calendar; charset=utf-8", etag=etag)

    # -- eventi -------------------------------------------------------------

    def get(self, uid: str, calendar: str = "personal") -> dict[str, Any]:
        href, cal, etag, cal_id = self._find(uid, calendar)
        evs = [event_to_dict(ev, self.tz, href=href, etag=etag) for ev in cal.walk("VEVENT")
               if str(ev.get("UID", "")).strip() == uid.strip()]
        if not evs:
            raise NotFound(f"Nessun VEVENT in {href}")
        master = next((e for e in evs if not e.get("recurrence_id")), evs[0])
        master["calendar"] = cal_id
        if len(evs) > 1:
            master["exceptions"] = [e for e in evs if e is not master]
        return master

    def create(self, calendar: str = "personal", **fields: Any) -> dict[str, Any]:
        ev = Event()
        uid = str(uuid.uuid4())
        ev.add("uid", uid)
        ev.add("dtstamp", datetime.now(timezone.utc))
        ev.add("created", datetime.now(timezone.utc))
        apply_event_fields(ev, self.tz, organizer=self.user, **fields)
        if ev.get("dtstart") is None:
            raise ValueError("start è obbligatorio")
        href = f"{self.cal_path(calendar)}{uid}.ics"
        etag = self.dav.put(href, serialize(new_calendar(ev)), "text/calendar; charset=utf-8", create=True)
        d = event_to_dict(ev, self.tz, href=href, etag=etag)
        d["calendar"] = calendar
        return d

    def _occurrence(self, cal: Calendar, occurrence: str, uid: str | None = None) -> Event:
        """L'occorrenza di una serie identificata da `occurrence`: il suo RECURRENCE-ID (valore restituito da
        list_events) oppure l'inizio attuale. Le eccezioni già esistenti si cercano prima per RECURRENCE-ID,
        così un'occorrenza spostata resta raggiungibile con l'identificatore originale."""
        wanted = parse_dt(occurrence, self.tz, all_day=False)
        date_only = len(occurrence.strip()) == 10

        def same(v: date | datetime) -> bool:
            if date_only:
                vd = v.astimezone(self.tz).date() if isinstance(v, datetime) else v
                return vd == wanted.date()
            return _same_instant(v, wanted, self.tz)

        if uid is not None:
            cal = _only_uid(cal, uid)  # una risorsa con più UID: solo la serie richiesta
        for ev in cal.walk("VEVENT"):
            if ev.get("recurrence-id") is not None and same(ev.decoded("recurrence-id")):
                return ev
        master = _master_of(cal, "VEVENT")
        problem = _rrule_problem(master)
        if problem:
            raise ValueError(f"Serie non gestibile ({problem}): modificala da un altro client")
        query = recurring_ical_events.of(cal, keep_recurrence_attributes=True)
        lo = datetime.combine(wanted.date(), time(0, 0), tzinfo=self.tz) if date_only else wanted - timedelta(days=1)
        hi = lo + timedelta(days=1) if date_only else wanted + timedelta(days=1)
        candidates = []
        for occ in query.after(lo):  # iteratore con tetto: niente espansioni enormi
            if len(candidates) >= MAX_EXPAND or (occ.get("dtstart") is not None
                                                 and _utc(occ.decoded("dtstart"), self.tz) >= _utc(hi, self.tz)):
                break
            candidates.append(occ)
        for occ in candidates:
            if occ.get("recurrence-id") is not None and same(occ.decoded("recurrence-id")):
                return occ
        for occ in candidates:
            if same(occ.decoded("dtstart")):
                return occ
        raise NotFound(f"Nessuna occorrenza della serie corrisponde a {occurrence}")

    def _master(self, cal: Calendar, uid: str | None = None) -> Event:
        masters = [ev for ev in cal.walk("VEVENT") if ev.get("recurrence-id") is None
                   and (uid is None or str(ev.get("UID", "")).strip() == uid.strip())]
        if not masters:
            raise NotFound("Nessun VEVENT master")
        return masters[0]

    @staticmethod
    def _require_series(master: Event, uid: str) -> None:
        if not _is_series(master):
            raise ValueError(f"L'evento {uid} non è ricorrente: `occurrence` non si applica. "
                             "Ometti occurrence per agire sull'intero evento.")

    def update(self, uid: str, calendar: str = "personal", occurrence: str | None = None,
               expected_etag: str | None = None, **fields: Any) -> dict[str, Any]:
        href, cal, etag, cal_id = self._find(uid, calendar)
        check_expected_etag(expected_etag, etag, f"L'evento {uid}")
        master = self._master(cal, uid)
        if occurrence:
            self._require_series(master, uid)
            if fields.get("rrule") is not None:
                raise ValueError("rrule si cambia sull'intera serie: ometti occurrence")
        if not occurrence:
            apply_event_fields(master, self.tz, organizer=self.user, **fields)
            if fields.get("rrule") == "":
                for ev in _vevents(cal, uid):  # evento singolo: le eccezioni della serie non hanno più senso
                    if ev is not master and ev.get("recurrence-id") is not None:
                        cal.subcomponents.remove(ev)
            new_etag = self._put(href, cal, etag)
            d = event_to_dict(master, self.tz, href=href, etag=new_etag)
            d["calendar"] = cal_id
            return d

        occ = self._occurrence(cal, occurrence, uid)
        rid = occ.decoded("recurrence-id") if occ.get("recurrence-id") is not None else occ.decoded("dtstart")
        target = next((ev for ev in _vevents(cal, uid) if ev.get("recurrence-id") is not None
                       and _same_instant(ev.decoded("recurrence-id"), rid, self.tz)), None)
        if target is None:
            target = Event()
            for k in master.keys():
                if k.upper() not in _RECURRENCE_PROPS:
                    target[k] = copy.deepcopy(master[k])
            for k in ("dtstart", "dtend", "duration"):
                if k in target:
                    del target[k]
            target.add("dtstart", occ.decoded("dtstart"))
            if occ.get("dtend") is not None:
                target.add("dtend", occ.decoded("dtend"))
            target.add("recurrence-id", rid)
            for sub in master.subcomponents:
                if sub.name == "VALARM":
                    target.add_component(copy.deepcopy(sub))
            cal.add_component(target)
        apply_event_fields(target, self.tz, organizer=self.user, **fields)
        new_etag = self._put(href, cal, etag)
        d = event_to_dict(target, self.tz, href=href, etag=new_etag)
        d["calendar"] = cal_id
        return d

    def delete(self, uid: str, calendar: str = "personal", occurrence: str | None = None,
               expected_etag: str | None = None) -> dict[str, Any]:
        href, cal, etag, cal_id = self._find(uid, calendar)
        check_expected_etag(expected_etag, etag, f"L'evento {uid}")
        master = self._master(cal, uid)
        if not occurrence:
            self.dav.delete(href, etag=etag)
            return {"deleted": True, "uid": uid, "href": href, "calendar": cal_id}
        self._require_series(master, uid)  # mai trasformare "una occorrenza" in "tutto l'evento"
        occ = self._occurrence(cal, occurrence, uid)
        rid = occ.decoded("recurrence-id") if occ.get("recurrence-id") is not None else occ.decoded("dtstart")
        for ev in _vevents(cal, uid):  # rimuovi eventuale eccezione già esistente
            if ev.get("recurrence-id") is not None and _same_instant(ev.decoded("recurrence-id"), rid, self.tz):
                cal.subcomponents.remove(ev)
        mstart = master.decoded("dtstart")
        if isinstance(mstart, datetime) and isinstance(rid, datetime) and mstart.tzinfo is not None:
            rid = rid.astimezone(mstart.tzinfo)  # stesso TZID di DTSTART
        elif not isinstance(mstart, datetime) and isinstance(rid, datetime):
            rid = rid.astimezone(self.tz).date()
        master.add("exdate", rid)
        _touch(master)
        new_etag = self._put(href, cal, etag)
        return {"deleted_occurrence": _iso(rid, self.tz), "uid": uid, "href": href, "calendar": cal_id, "etag": new_etag}

    def respond(self, uid: str, response: str, calendar: str = "personal", comment: str | None = None) -> dict[str, Any]:
        """Imposta il PARTSTAT dell'utente su un invito ricevuto; SOGo invia la risposta all'organizzatore."""
        partstat = {"accept": "ACCEPTED", "accepted": "ACCEPTED", "decline": "DECLINED", "declined": "DECLINED",
                    "tentative": "TENTATIVE", "maybe": "TENTATIVE"}.get(response.strip().lower())
        if not partstat:
            raise ValueError("response: accept | decline | tentative")
        if not self.user:
            raise ValueError("Utente non configurato")
        href, cal, etag, cal_id = self._find(uid, calendar)
        me = self.user.lower()
        hit = False
        for ev in _vevents(cal, uid):
            att = ev.get("attendee")
            for a in (att if isinstance(att, list) else [att] if att is not None else []):
                if _mail(a).lower() == me:
                    a.params["PARTSTAT"] = vText(partstat)
                    if "RSVP" in a.params:
                        del a.params["RSVP"]
                    hit = True
            if hit and comment:
                _set_text(ev, "comment", comment)
            if hit:
                if "dtstamp" in ev:
                    del ev["dtstamp"]
                ev.add("dtstamp", datetime.now(timezone.utc))
        if not hit:
            raise ValueError(f"{self.user} non è tra i partecipanti di questo evento")
        new_etag = self._put(href, cal, etag)
        d = self.get(uid, cal_id)
        d["etag"] = new_etag
        d["responded"] = partstat
        return d

    # -- task ---------------------------------------------------------------

    def tasks(self, calendar: str = "personal", include_completed: bool = False, limit: int = 200) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for cal_id, cal_name in self._calendar_ids(calendar):
            for r in self._query(cal_id, "VTODO"):
                data = r.text(NS_CAL, "calendar-data")
                if not data:
                    continue
                try:
                    cal = Calendar.from_ical(data)
                except Exception:
                    continue
                for td in cal.walk("VTODO"):
                    d = todo_to_dict(td, self.tz, href=r.href, etag=r.etag)
                    if not include_completed and d.get("status") in ("COMPLETED", "CANCELLED"):
                        continue
                    d["calendar"] = cal_id
                    if cal_name != cal_id:
                        d["calendar_name"] = cal_name
                    results.append(d)
        results.sort(key=lambda x: (x.get("due") or "9999", x.get("priority") or 9))
        return results[:limit]

    def get_task(self, uid: str, calendar: str = "personal") -> dict[str, Any]:
        href, cal, etag, cal_id = self._find(uid, calendar, comp="VTODO")
        d = todo_to_dict(_master_of(cal, "VTODO", uid), self.tz, href=href, etag=etag)
        d["calendar"] = cal_id
        return d

    def create_task(self, calendar: str = "personal", **fields: Any) -> dict[str, Any]:
        td = Todo()
        uid = str(uuid.uuid4())
        td.add("uid", uid)
        td.add("dtstamp", datetime.now(timezone.utc))
        td.add("created", datetime.now(timezone.utc))
        apply_todo_fields(td, self.tz, email=self.user, **fields)
        if td.get("summary") is None:
            raise ValueError("summary è obbligatorio")
        if "status" not in td:
            td.add("status", "NEEDS-ACTION")
        href = f"{self.cal_path(calendar)}{uid}.ics"
        etag = self.dav.put(href, serialize(new_calendar(td)), "text/calendar; charset=utf-8", create=True)
        d = todo_to_dict(td, self.tz, href=href, etag=etag)
        d["calendar"] = calendar
        return d

    def update_task(self, uid: str, calendar: str = "personal", expected_etag: str | None = None,
                    **fields: Any) -> dict[str, Any]:
        href, cal, etag, cal_id = self._find(uid, calendar, comp="VTODO")
        check_expected_etag(expected_etag, etag, f"Il task {uid}")
        td = _master_of(cal, "VTODO", uid)
        apply_todo_fields(td, self.tz, email=self.user, **fields)
        new_etag = self._put(href, cal, etag)
        d = todo_to_dict(td, self.tz, href=href, etag=new_etag)
        d["calendar"] = cal_id
        return d

    def delete_task(self, uid: str, calendar: str = "personal", expected_etag: str | None = None) -> dict[str, Any]:
        href, _, etag, cal_id = self._find(uid, calendar, comp="VTODO")
        check_expected_etag(expected_etag, etag, f"Il task {uid}")
        self.dav.delete(href, etag=etag)
        return {"deleted": True, "uid": uid, "href": href, "calendar": cal_id}
