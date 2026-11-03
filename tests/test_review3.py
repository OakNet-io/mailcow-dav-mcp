"""Regressioni della terza review globale."""

import asyncio
import csv
import io
import time as _time
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from icalendar import Calendar, Event, Todo
from lxml import etree

from mailcow_dav_mcp.bulk import Bulk, _join, _split, contact_to_row, row_to_fields
from mailcow_dav_mcp.caldav import (Calendars, _add_timezones, _master_of, _series_end, apply_event_fields,
                                    apply_todo_fields, new_calendar, serialize, todo_to_dict)
from mailcow_dav_mcp.carddav import AddressBooks, new_vcard, serialize_vcard
from mailcow_dav_mcp.dav import NS_CAL, NS_CARD, NS_D, DavClient, DavError, NotFound, PreconditionFailed, Resource

TZ = ZoneInfo("Europe/Rome")
PRINCIPAL = "/SOGo/dav/u@example.com/"
BOOK = PRINCIPAL + "Contacts/personal/"
CAL = PRINCIPAL + "Calendar/personal/"


def _el(ns, name, text=None):
    e = etree.Element(f"{{{ns}}}{name}")
    if text is not None:
        e.text = text
    return e


class FakeDav:
    """Server DAV in memoria. Come SOGo, le query con filtro UID restituiscono TUTTO."""

    def __init__(self):
        self.store: dict[str, tuple[str, str]] = {}
        self.n = 0
        self.fetched: list[str] = []

    def add(self, href, text):
        self.n += 1
        self.store[href] = (text, f'"e{self.n}"')

    def _res(self, href, data_ns=None, data_name=None):
        r = Resource(href=href)
        text, etag = self.store[href]
        r.props[f"{{{NS_D}}}getetag"] = _el(NS_D, "getetag", etag)
        r.props[f"{{{NS_D}}}resourcetype"] = _el(NS_D, "resourcetype")
        if data_name:
            r.props[f"{{{data_ns}}}{data_name}"] = _el(data_ns, data_name, text)
            self.fetched.append(href)
        return r

    def propfind(self, path, props, depth=1):
        return [self._res(h) for h in self.store if h.startswith(path)]

    def report(self, path, body, depth=1):
        root = etree.fromstring(body)
        ns, kind = (NS_CARD, "address-data") if path.startswith(PRINCIPAL + "Contacts") else (NS_CAL, "calendar-data")
        hrefs = [h.text for h in root.iter(f"{{{NS_D}}}href")]
        if not hrefs:
            hrefs = [h for h in self.store if h.startswith(path)]
        return [self._res(h, ns, kind) for h in hrefs if h in self.store]

    def get(self, href):
        if href not in self.store:
            raise NotFound(href)
        return self.store[href]

    def put(self, href, body, ctype, etag=None, create=False):
        if create and href in self.store:
            raise PreconditionFailed(href)
        if not create and (href not in self.store or self.store[href][1] != etag):
            raise PreconditionFailed(href)
        self.add(href, body)
        return self.store[href][1]

    def delete(self, href, etag=None):
        if self.store.get(href, (None, None))[1] != etag:
            raise PreconditionFailed(href)
        del self.store[href]


def _books(dav):
    b = AddressBooks(dav, PRINCIPAL)
    b._with_fold_warning = lambda result, href: result
    return b


def _group(uid, name, members):
    lines = ["BEGIN:VCARD", "VERSION:3.0", f"UID:{uid}", f"FN:{name}", "X-ADDRESSBOOKSERVER-KIND:group"]
    lines += [f"X-ADDRESSBOOKSERVER-MEMBER:urn:uuid:{m}" for m in members]
    return "\r\n".join(lines + ["END:VCARD"]) + "\r\n"


# -- 1. occorrenza su evento singolo ------------------------------------------------------

def _single_event_dav():
    dav = FakeDav()
    ev = Event()
    ev.add("uid", "E1")
    ev.add("summary", "Singolo")
    ev.add("dtstart", datetime(2026, 11, 10, 10, 0, tzinfo=TZ))
    ev.add("dtend", datetime(2026, 11, 10, 11, 0, tzinfo=TZ))
    dav.add(CAL + "E1.ics", serialize(new_calendar(ev)))
    return dav


def test_occurrence_on_single_event_never_deletes_whole_event():
    dav = _single_event_dav()
    cals = Calendars(dav, PRINCIPAL)
    with pytest.raises(ValueError, match="non è ricorrente"):
        cals.delete("E1", occurrence="2030-01-01")
    with pytest.raises(ValueError, match="non è ricorrente"):
        cals.update("E1", occurrence="2030-01-01", summary="x")
    assert CAL + "E1.ics" in dav.store and "Singolo" in dav.store[CAL + "E1.ics"][0]


# -- 2. UID case-sensitive ------------------------------------------------------------------

def test_uid_lookup_is_case_sensitive():
    dav = FakeDav()
    dav.add(BOOK + "first.vcf", serialize_vcard(new_vcard(uid="ABC", fn="Maiuscolo")))
    dav.add(BOOK + "second.vcf", serialize_vcard(new_vcard(uid="abc", fn="Minuscolo")))
    books = _books(dav)
    assert books.get("abc")["fn"] == "Minuscolo"
    assert books.get("ABC")["fn"] == "Maiuscolo"
    with pytest.raises(NotFound):
        books.get("Abc")


# -- 3. import gruppi -----------------------------------------------------------------------

def test_import_rewrites_group_members_and_reports_unresolved(tmp_path):
    dav = FakeDav()
    dav.add(BOOK + "old.vcf", serialize_vcard(new_vcard(uid="EXIST", fn="Gia Presente", emails=["dup@example.com"])))
    dav.add(BOOK + "taken.vcf", serialize_vcard(new_vcard(uid="TAKEN", fn="Altro", emails=["altro@example.com"])))
    f = tmp_path / "in.vcf"
    f.write_text(
        serialize_vcard(new_vcard(uid="F-DUP", fn="Duplicato", emails=["dup@example.com"]))
        + serialize_vcard(new_vcard(uid="TAKEN", fn="Nuovo", emails=["nuovo@example.com"]))
        + _group("G1", "Squadra", ["F-DUP", "TAKEN", "GHOST"]), encoding="utf-8")
    bulk = Bulk(_books(dav))
    plan = bulk.import_file(str(f))
    assert plan["unresolved_members"] == [{"group": "Squadra", "member": "GHOST"}]
    res = bulk.import_file(str(f), dry_run=False)
    assert res["errors"] == [] and res["created"] == 1
    new_uid = res["created_uids"][0]
    assert new_uid != "TAKEN"
    group = next(g for g in _books(dav).groups("personal") if g["name"] == "Squadra")
    assert set(group["members"]) == {"EXIST", new_uid, "GHOST"}


def test_import_merges_into_existing_group(tmp_path):
    dav = FakeDav()
    dav.add(BOOK + "c.vcf", serialize_vcard(new_vcard(uid="C1", fn="Uno", emails=["uno@example.com"])))
    dav.add(BOOK + "g.vcf", _group("GX", "Squadra", []))
    f = tmp_path / "in.vcf"
    f.write_text(_group("OTHER", "squadra", ["C1"]), encoding="utf-8")
    res = Bulk(_books(dav)).import_file(str(f), dry_run=False)
    assert res["errors"] == [] and res["groups"] == [{"name": "squadra", "action": "merge", "members": 1}]
    groups = _books(dav).groups("personal")
    assert len(groups) == 1 and groups[0]["members"] == ["C1"]


# -- 4. XML malformato -----------------------------------------------------------------------

def test_truncated_multistatus_is_an_error():
    ok = (b'<?xml version="1.0"?><D:multistatus xmlns:D="DAV:"><D:response><D:href>/a/1.vcf</D:href>'
          b'<D:propstat><D:prop><D:getetag>"1"</D:getetag></D:prop><D:status>HTTP/1.1 200 OK</D:status>'
          b'</D:propstat></D:response></D:multistatus>')
    assert len(DavClient("https://dav.example.com", "u", "p").parse_multistatus(ok)) == 1
    with pytest.raises(DavError, match="malformato"):
        DavClient("https://dav.example.com", "u", "p").parse_multistatus(ok[:-40])
    with pytest.raises(DavError, match="multistatus"):
        DavClient("https://dav.example.com", "u", "p").parse_multistatus(b'<?xml version="1.0"?><html/>')


# -- 5. promemoria dei task --------------------------------------------------------------------

def test_task_title_change_keeps_alarm_anchor():
    td = Todo()
    td.add("uid", "T1")
    apply_todo_fields(td, TZ, summary="x", start="2026-11-03", alarms=["1d"])
    apply_todo_fields(td, TZ, due="2026-11-10")
    apply_todo_fields(td, TZ, summary="nuovo titolo")
    assert "RELATED" not in td.walk("VALARM")[0]["trigger"].params  # resta prima dell'inizio


# -- 6. rrule="" -----------------------------------------------------------------------------

def test_removing_rrule_makes_single_event():
    master = Event()
    master.add("uid", "S1")
    master.add("summary", "Serie")
    master.add("dtstart", datetime(2026, 11, 2, 10, 0, tzinfo=TZ))
    master.add("dtend", datetime(2026, 11, 2, 11, 0, tzinfo=TZ))
    master.add("rrule", {"FREQ": "WEEKLY", "COUNT": 4})
    master.add("rdate", datetime(2026, 12, 25, 10, 0, tzinfo=TZ))
    master.add("exdate", datetime(2026, 11, 16, 10, 0, tzinfo=TZ))
    moved = Event()
    moved.add("uid", "S1")
    moved.add("recurrence-id", datetime(2026, 11, 9, 10, 0, tzinfo=TZ))
    moved.add("dtstart", datetime(2026, 11, 12, 14, 0, tzinfo=TZ))
    moved.add("dtend", datetime(2026, 11, 12, 15, 0, tzinfo=TZ))
    cal = new_calendar(master)
    cal.add_component(moved)
    dav = FakeDav()
    dav.add(CAL + "S1.ics", serialize(cal))
    Calendars(dav, PRINCIPAL).update("S1", rrule="")
    out = Calendar.from_ical(dav.store[CAL + "S1.ics"][0])
    evs = list(out.walk("VEVENT"))
    assert len(evs) == 1
    assert all(k not in evs[0] for k in ("RRULE", "RDATE", "EXDATE"))


def test_rrule_rejected_on_single_occurrence_and_for_secondly():
    ev = Event()
    with pytest.raises(ValueError, match="SECONDLY"):
        apply_event_fields(ev, TZ, start="2026-11-02T10:00", rrule="FREQ=SECONDLY;COUNT=1000000000")


# -- 7. VTIMEZONE rigenerata --------------------------------------------------------------------

def test_vtimezone_regenerated_after_moving_dates():
    tz = ZoneInfo("America/Sao_Paulo")
    ev = Event()
    ev.add("uid", "TZ1")
    ev.add("dtstart", datetime(2026, 1, 10, 10, 0, tzinfo=tz))
    ev.add("dtend", datetime(2026, 1, 10, 11, 0, tzinfo=tz))
    cal = new_calendar(ev)
    del ev["dtstart"], ev["dtend"]
    ev.add("dtstart", datetime(2018, 1, 10, 10, 0, tzinfo=tz))  # in Brasile nel 2018 c'era l'ora legale
    ev.add("dtend", datetime(2018, 1, 10, 11, 0, tzinfo=tz))
    _add_timezones(cal)
    tzs = list(cal.walk("VTIMEZONE"))
    assert len(tzs) == 1
    assert "-0200" in tzs[0].to_ical().decode()


# -- 8. DUE e DURATION ---------------------------------------------------------------------

def test_todo_due_replaces_duration_and_validates_order():
    td = Todo.from_ical("BEGIN:VTODO\r\nUID:T2\r\nSUMMARY:x\r\nDTSTART:20261103T090000Z\r\n"
                        "DURATION:PT2H\r\nEND:VTODO\r\n")
    apply_todo_fields(td, TZ, due="2026-11-05T10:00")
    assert "DURATION" not in td and "DUE" in td
    with pytest.raises(ValueError, match="due"):
        apply_todo_fields(td, TZ, due="2026-11-01T10:00")
    td2 = Todo.from_ical("BEGIN:VTODO\r\nUID:T3\r\nSUMMARY:x\r\nDTSTART:20261103T090000Z\r\n"
                         "DURATION:PT2H\r\nEND:VTODO\r\n")
    apply_todo_fields(td2, TZ, start="")
    assert "DURATION" not in td2 and "DTSTART" not in td2
    assert todo_to_dict(td2, TZ)["due"].startswith("2026-11-03T12:00")  # 11:00Z = 12:00 a Roma


# -- 9. espansione limitata -----------------------------------------------------------------

def test_series_end_does_not_materialize_huge_series():
    ev = Event()
    ev.add("dtstart", datetime(2026, 1, 1, 0, 0, tzinfo=TZ))
    ev.add("rrule", {"FREQ": "HOURLY", "COUNT": 1_000_000_000})
    t0 = _time.monotonic()
    assert _series_end(ev) is None
    assert _time.monotonic() - t0 < 5
    ev2 = Event()
    ev2.add("dtstart", datetime(2026, 1, 1, 0, 0, tzinfo=TZ))
    ev2.add("rrule", {"FREQ": "HOURLY", "UNTIL": datetime(2040, 1, 1, tzinfo=ZoneInfo("UTC"))})
    assert str(_series_end(ev2)) == "2040-01-01"


def test_read_expansion_capped():
    from mailcow_dav_mcp import caldav
    ev = Event()
    ev.add("uid", "H1")
    ev.add("summary", "Ogni ora")
    ev.add("dtstart", datetime(2026, 11, 1, 0, 0, tzinfo=TZ))
    ev.add("dtend", datetime(2026, 11, 1, 0, 30, tzinfo=TZ))
    ev.add("rrule", {"FREQ": "HOURLY"})
    dav = FakeDav()
    dav.add(CAL + "H1.ics", serialize(new_calendar(ev)))
    cals = Calendars(dav, PRINCIPAL)
    out = cals.events(start="2026-11-01", end="2027-11-01", limit=None)
    assert len(out) == caldav.MAX_EXPAND and "truncated" in out[-1]


# -- 10. tool non bloccanti ------------------------------------------------------------------

def test_sync_tools_run_off_the_event_loop(monkeypatch):
    from mailcow_dav_mcp import server

    class SlowBooks:
        def list(self):
            _time.sleep(0.3)
            return []

    monkeypatch.setattr(server, "_books", lambda: SlowBooks())

    async def scenario():
        tick = asyncio.get_running_loop().time()
        task = asyncio.create_task(server.mcp.call_tool("list_addressbooks", {}))
        await asyncio.sleep(0.01)
        lag = asyncio.get_running_loop().time() - tick
        await task
        return lag

    assert asyncio.run(scenario()) < 0.15


# -- 11/13. cache e limite -------------------------------------------------------------------

def test_search_uses_etag_cache_and_groups_do_not_eat_limit():
    dav = FakeDav()
    dav.add(BOOK + "a-group.vcf", _group("G1", "Gruppo", []))
    dav.add(BOOK + "b.vcf", serialize_vcard(new_vcard(uid="B", fn="Bianca")))
    dav.add(BOOK + "c.vcf", serialize_vcard(new_vcard(uid="C", fn="Carlo")))
    books = _books(dav)
    assert [c["uid"] for c in books.all(limit=1)] == ["B"]
    books.search("carlo")
    dav.fetched.clear()
    assert [c["uid"] for c in books.search("carlo")] == ["C"]
    assert dav.fetched == []  # nulla riscaricato: ETag invariati
    text, etag = dav.store[BOOK + "c.vcf"]
    dav.put(BOOK + "c.vcf", text.replace("Carlo", "Carla"), "text/vcard", etag=etag)
    assert books.search("carla")[0]["fn"] == "Carla" and dav.fetched == [BOOK + "c.vcf"]


# -- 12. limite di ricerca eventi ------------------------------------------------------------

def test_search_events_filters_before_limit():
    dav = FakeDav()
    cal = Calendar()
    cal.add("prodid", "-//t//EN")
    cal.add("version", "2.0")
    for i in range(30):
        ev = Event()
        ev.add("uid", f"N{i}")
        ev.add("summary", "Ago" if i == 29 else "Rumore")
        ev.add("dtstart", datetime(2026, 11, 1, 8, 0, tzinfo=TZ).replace(hour=8 + i % 10, day=1 + i // 10))
        ev.add("dtend", datetime(2026, 11, 1, 8, 30, tzinfo=TZ).replace(hour=8 + i % 10, day=1 + i // 10))
        c = new_calendar(ev)
        dav.add(CAL + f"N{i}.ics", serialize(c))
    cals = Calendars(dav, PRINCIPAL)
    found = cals.search("ago", start="2026-10-01", end="2026-12-01", limit=1)
    assert [e["uid"] for e in found] == ["N29"]


# -- 14/17. CSV ----------------------------------------------------------------------------

def test_csv_neutralizes_formulas_and_roundtrips():
    contact = {"uid": "X", "fn": "=1+1", "note": "=WEBSERVICE(\"http://evil\")", "categories": ["A|B", "C\\D"],
               "phones": [{"type": "CELL", "value": "+39 333"}]}
    row = contact_to_row(contact)
    assert row["fn"] == "'=1+1" and row["note"].startswith("'=")
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=list(row))
    w.writeheader()
    w.writerow(row)
    back = row_to_fields(next(csv.DictReader(io.StringIO(buf.getvalue()))))
    assert back["fn"] == "=1+1" and back["note"].startswith("=WEBSERVICE")
    assert back["categories"] == ["A|B", "C\\D"]
    assert _split(_join(["x | y", "z"])) == ["x | y", "z"]


# -- 15. master dei task ricorrenti ----------------------------------------------------------

def test_task_master_selected_regardless_of_order():
    cal = Calendar.from_ical(
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//t//EN\r\n"
        "BEGIN:VTODO\r\nUID:R1\r\nSUMMARY:Eccezione\r\nRECURRENCE-ID:20261110T090000Z\r\nEND:VTODO\r\n"
        "BEGIN:VTODO\r\nUID:R1\r\nSUMMARY:Master\r\nDTSTART:20261103T090000Z\r\nRRULE:FREQ=WEEKLY\r\nEND:VTODO\r\n"
        "END:VCALENDAR\r\n")
    assert str(_master_of(cal, "VTODO")["summary"]) == "Master"


# -- 18. priority numerica -------------------------------------------------------------------

def test_priority_tool_schema_accepts_numbers():
    from mailcow_dav_mcp import server
    tools = {t.name: t for t in asyncio.run(server.mcp.list_tools())}
    schema = tools["create_task"].inputSchema["properties"]["priority"]
    types = {s.get("type") for s in schema.get("anyOf", [schema])}
    assert {"string", "integer"} <= types


# -- regressioni trovate nella verifica dei fix ----------------------------------------------

def test_foreign_vtimezone_is_preserved():
    cal = Calendar.from_ical(
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//altro//EN\r\n"
        "BEGIN:VTIMEZONE\r\nTZID:Europe/Rome\r\nBEGIN:STANDARD\r\nDTSTART:19700101T000000\r\n"
        "TZOFFSETFROM:+0300\r\nTZOFFSETTO:+0300\r\nEND:STANDARD\r\nEND:VTIMEZONE\r\n"
        "BEGIN:VEVENT\r\nUID:F1\r\nDTSTAMP:20261101T000000Z\r\nSUMMARY:x\r\n"
        "DTSTART;TZID=Europe/Rome:20261110T100000\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n")
    _add_timezones(cal)
    tzs = list(cal.walk("VTIMEZONE"))
    assert len(tzs) == 1 and "+0300" in tzs[0].to_ical().decode()


def test_exception_beyond_expansion_cap_still_found():
    from mailcow_dav_mcp import caldav
    master = Event()
    master.add("uid", "H2")
    master.add("summary", "Rumore")
    master.add("dtstart", datetime(2026, 11, 1, 0, 0, tzinfo=TZ))
    master.add("dtend", datetime(2026, 11, 1, 0, 30, tzinfo=TZ))
    master.add("rrule", {"FREQ": "HOURLY", "COUNT": caldav.MAX_EXPAND + 200})
    late = datetime(2026, 11, 1, 0, 0, tzinfo=TZ) + (caldav.MAX_EXPAND + 100) * (datetime(2026, 1, 1, 1) - datetime(2026, 1, 1, 0))
    ex = Event()
    ex.add("uid", "H2")
    ex.add("summary", "Ago")
    ex.add("recurrence-id", late)
    ex.add("dtstart", late)
    ex.add("dtend", late.replace(minute=30))
    cal = new_calendar(master)
    cal.add_component(ex)
    dav = FakeDav()
    dav.add(CAL + "H2.ics", serialize(cal))
    found = Calendars(dav, PRINCIPAL).search("ago", start="2026-10-01", end="2027-03-01")
    assert len(found) == 1 and found[0]["truncated"] is True


def test_cache_drops_entry_when_new_version_not_returned():
    dav = FakeDav()
    dav.add(BOOK + "c.vcf", serialize_vcard(new_vcard(uid="C", fn="Carlo")))
    books = _books(dav)
    assert books.search("carlo")
    text, etag = dav.store[BOOK + "c.vcf"]
    dav.put(BOOK + "c.vcf", text.replace("Carlo", "Carla"), "text/vcard", etag=etag)
    dav.report = lambda path, body, depth=1: []  # multiget senza address-data
    assert books.search("carlo") == []


def test_find_group_rejects_replaced_group():
    dav = FakeDav()
    dav.add(BOOK + "g.vcf", _group("G1", "Squadra", []))
    books = _books(dav)
    books.groups()
    dav.store[BOOK + "g.vcf"] = (_group("G2", "Squadra", []), '"x"')
    dav.propfind = lambda *a, **k: [Resource(href=BOOK + "g.vcf", props={
        f"{{{NS_D}}}getetag": _el(NS_D, "getetag", dav.n and '"e1"'),
        f"{{{NS_D}}}resourcetype": _el(NS_D, "resourcetype")})]  # elenco ancora vecchio
    with pytest.raises(PreconditionFailed):
        books._find_group("G1", "personal")


def test_import_same_named_groups_merged_and_non_uuid_members_kept(tmp_path):
    dav = FakeDav()
    dav.add(BOOK + "c.vcf", serialize_vcard(new_vcard(uid="C1", fn="Uno", emails=["uno@example.com"])))
    f = tmp_path / "in.vcf"
    f.write_text(_group("GA", "Squadra", ["C1"]) + _group("GB", "Squadra", [])
                 .replace("END:VCARD", "X-ADDRESSBOOKSERVER-MEMBER:mailto:person@example.com\r\nEND:VCARD"),
                 encoding="utf-8")
    res = Bulk(_books(dav)).import_file(str(f), dry_run=False)
    assert res["errors"] == []
    groups = _books(dav).groups("personal")
    assert len(groups) == 1 and set(groups[0]["members"]) == {"C1", "mailto:person@example.com"}


def test_reuid_after_412_updates_every_alias(tmp_path):
    dav = FakeDav()
    f = tmp_path / "in.vcf"
    f.write_text(serialize_vcard(new_vcard(uid="A1", fn="Primo", emails=["x@example.com"]))
                 + serialize_vcard(new_vcard(uid="A2", fn="Doppione", emails=["x@example.com"]))
                 + _group("G", "Squadra", ["A1", "A2"]), encoding="utf-8")
    bulk = Bulk(_books(dav))
    orig_put = dav.put
    # un altro client crea A1 tra la lettura e la scrittura: 412 → nuovo UID
    def racing_put(href, body, ctype, etag=None, create=False):
        if href.endswith("/A1.vcf") and href not in dav.store:
            dav.add(href, "BEGIN:VCARD\r\nUID:A1\r\nEND:VCARD\r\n")
        return orig_put(href, body, ctype, etag=etag, create=create)
    dav.put = racing_put
    res = bulk.import_file(str(f), dry_run=False)
    new_uid = res["created_uids"][0]
    group = next(g for g in _books(dav).groups("personal") if g["name"] == "Squadra")
    assert new_uid != "A1" and group["members"] == [new_uid]


def test_todo_due_start_types_and_order():
    td = Todo()
    td.add("uid", "T9")
    with pytest.raises(ValueError, match="entrambe"):
        apply_todo_fields(td, TZ, summary="x", start="2026-11-03T10:00", due="2026-11-05")
    with pytest.raises(ValueError, match="successiva"):
        apply_todo_fields(Todo(), TZ, summary="x", start="2026-11-03T10:00", due="2026-11-03T10:00")
