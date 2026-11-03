from datetime import date
from zoneinfo import ZoneInfo

import vobject
from icalendar import Calendar, Event

from mailcow_dav_mcp.caldav import (apply_event_fields, apply_todo_fields, event_to_dict, new_calendar, parse_alarm,
                                    parse_dt, serialize, todo_to_dict)
from mailcow_dav_mcp.carddav import apply_fields, new_vcard, vcard_to_dict
from mailcow_dav_mcp.dav import DavClient

TZ = ZoneInfo("Europe/Rome")


def test_vcard_roundtrip():
    card = new_vcard(given="Mario", family="Rossi", emails=["work:mario@acme.it", "privato@gmail.com"],
                     phones=["cell:+39 333 1234567", "fax:+39 02 1234"], org="ACME S.r.l.", title="CTO")
    text = card.serialize()
    assert "FN:Mario Rossi" in text
    assert "ORG:ACME S.r.l." in text
    d = vcard_to_dict(vobject.readOne(text))
    assert d["fn"] == "Mario Rossi"
    assert d["name"]["given"] == "Mario" and d["name"]["family"] == "Rossi"
    assert {e["value"] for e in d["emails"]} == {"mario@acme.it", "privato@gmail.com"}
    assert any(p["type"] == "FAX" for p in d["phones"])
    assert any(p["type"] == "CELL" and p["value"] == "+39 333 1234567" for p in d["phones"])
    assert d["org"] == "ACME S.r.l."


def test_vcard_update_replaces_lists_and_clears():
    card = new_vcard(fn="Test", emails=["a@x.it", "b@x.it"], note="ciao")
    apply_fields(card, emails=["c@x.it"], note="")
    d = vcard_to_dict(card)
    assert [e["value"] for e in d["emails"]] == ["c@x.it"]
    assert "note" not in d


def test_vcard_addresses_roundtrip_and_replace():
    card = new_vcard(fn="Test", addresses=["work:Via Roma 1;Milano;MI;20100;IT", "Via Po 2;Torino;;10100;"])
    text = card.serialize()
    assert "ADR;TYPE=WORK:;;Via Roma 1;Milano;MI;20100;IT" in text
    d = vcard_to_dict(vobject.readOne(text))
    assert len(d["addresses"]) == 2
    assert d["addresses"][0] == {"type": "WORK", "street": "Via Roma 1", "city": "Milano",
                                 "region": "MI", "code": "20100", "country": "IT"}
    assert d["addresses"][1]["type"] == "WORK" and d["addresses"][1]["city"] == "Torino"
    apply_fields(card, addresses=["home:Via Verdi 3;Roma;;00100;IT"])
    d = vcard_to_dict(card)
    assert len(d["addresses"]) == 1 and d["addresses"][0]["type"] == "HOME"
    apply_fields(card, addresses=[])
    assert "addresses" not in vcard_to_dict(card)


def test_parse_dt():
    assert parse_dt("2026-10-03", TZ, all_day=True) == date(2026, 10, 3)
    dt = parse_dt("2026-10-03T15:00", TZ)
    assert dt.tzinfo is TZ and dt.hour == 15
    z = parse_dt("2026-10-03T13:00:00Z", TZ)
    assert z.utcoffset().total_seconds() == 0


def test_event_build_and_parse():
    ev = Event()
    ev.add("uid", "abc")
    apply_event_fields(ev, TZ, summary="Riunione", start="2026-10-03T15:00", location="Samarate",
                       attendees=["Mario Rossi <mario@acme.it>"], alarms=["15m", "email:1d"],
                       rrule="FREQ=WEEKLY;COUNT=3", organizer="me@example.com")
    text = serialize(new_calendar(ev))
    assert "BEGIN:VTIMEZONE" in text
    assert "DTSTART;TZID=Europe/Rome:20261003T150000" in text
    assert "DTEND;TZID=Europe/Rome:20261003T160000" in text
    assert "RRULE:FREQ=WEEKLY;COUNT=3" in text
    assert "TRIGGER:-PT15M" in text
    parsed = Calendar.from_ical(text)
    d = event_to_dict(parsed.walk("VEVENT")[0], TZ)
    assert d["summary"] == "Riunione"
    assert d["start"].startswith("2026-10-03T15:00:00+02:00")
    assert d["attendees"][0]["email"] == "mario@acme.it"
    assert d["attendees"][0]["name"] == "Mario Rossi"
    assert d["rrule"] == "FREQ=WEEKLY;COUNT=3"
    assert d["all_day"] is False
    assert d["alarms"] == ["15m", "email:1d"]


def test_all_day_event():
    ev = Event()
    ev.add("uid", "x")
    apply_event_fields(ev, TZ, summary="Ferie", start="2026-08-10", all_day=True)
    text = serialize(new_calendar(ev))
    assert "DTSTART;VALUE=DATE:20260810" in text
    assert "DTEND;VALUE=DATE:20260811" in text
    d = event_to_dict(Calendar.from_ical(text).walk("VEVENT")[0], TZ)
    assert d["all_day"] is True and d["end"] == "2026-08-11"


def test_event_update_removes_rrule_and_alarm():
    ev = Event()
    ev.add("uid", "x")
    apply_event_fields(ev, TZ, summary="A", start="2026-10-03T10:00", rrule="FREQ=DAILY", alarms=["10m"])
    apply_event_fields(ev, TZ, rrule="", alarms=[], summary="B")
    text = serialize(new_calendar(ev))
    vevent = text.split("BEGIN:VEVENT")[1].split("END:VEVENT")[0]
    assert "RRULE" not in vevent and "VALARM" not in vevent and "SUMMARY:B" in vevent
    assert "SEQUENCE:1" in text


def test_multistatus_parse():
    xml = b"""<?xml version="1.0"?>
    <D:multistatus xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:carddav">
      <D:response><D:href>/SOGo/dav/u@x.it/Contacts/personal/</D:href>
        <D:propstat><D:prop><D:displayname>Rubrica personale</D:displayname>
        <D:resourcetype><D:collection/><C:addressbook/></D:resourcetype></D:prop>
        <D:status>HTTP/1.1 200 OK</D:status></D:propstat>
        <D:propstat><D:prop><D:getetag/></D:prop><D:status>HTTP/1.1 404 Not Found</D:status></D:propstat>
      </D:response>
    </D:multistatus>"""
    res = DavClient.parse_multistatus(xml)
    assert len(res) == 1
    r = res[0]
    assert r.name == "personal"
    assert r.text("DAV:", "displayname") == "Rubrica personale"
    assert r.has_child("DAV:", "resourcetype", "urn:ietf:params:xml:ns:carddav", "addressbook")
    assert r.etag is None


def test_nickname_categories_photo():
    import base64
    png = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==")
    card = new_vcard(fn="Pippo Rossi", nickname="Pippo", categories=["Cliente", "VIP"],
                     photo="data:image/png;base64," + base64.b64encode(png).decode())
    text = card.serialize()
    assert "NICKNAME:Pippo" in text and "CATEGORIES:Cliente,VIP" in text
    assert "PHOTO;ENCODING=b;TYPE=PNG:" in text or "PHOTO;TYPE=PNG;ENCODING=b:" in text
    d = vcard_to_dict(vobject.readOne(text))
    assert d["nickname"] == "Pippo" and d["categories"] == ["Cliente", "VIP"]
    assert d["photo"] == {"embedded": True, "type": "PNG", "bytes": len(png)}
    apply_fields(card, categories=[], photo="", nickname="")
    d = vcard_to_dict(card)
    assert "categories" not in d and "photo" not in d and "nickname" not in d


def test_event_extra_fields_and_move_keeps_duration():
    ev = Event()
    ev.add("uid", "x")
    apply_event_fields(ev, TZ, summary="A", start="2026-10-03T10:00", end="2026-10-03T12:30",
                       categories=["Progetto", "Cliente"], privacy="private", busy="free", url="https://example.com")
    d = event_to_dict(ev, TZ)
    assert d["categories"] == ["Progetto", "Cliente"] and d["privacy"] == "private" and d["busy"] == "free"
    apply_event_fields(ev, TZ, start="2026-10-05T09:00")
    d = event_to_dict(ev, TZ)
    assert d["start"].startswith("2026-10-05T09:00") and d["end"].startswith("2026-10-05T11:30")
    apply_event_fields(ev, TZ, busy="busy", privacy="")
    d = event_to_dict(ev, TZ)
    assert "busy" not in d and "privacy" not in d


def test_parse_alarm():
    from datetime import timedelta
    assert parse_alarm("15m") == ("DISPLAY", timedelta(minutes=-15))
    assert parse_alarm("email:2h") == ("EMAIL", timedelta(hours=-2))
    assert parse_alarm("1d") == ("DISPLAY", timedelta(days=-1))


def test_todo_roundtrip():
    from icalendar import Todo
    td = Todo()
    td.add("uid", "t1")
    apply_todo_fields(td, TZ, summary="Rinnovo dominio", due="2026-10-15", priority="high", categories=["Progetto"], alarms=["1d"])
    text = serialize(new_calendar(td))
    assert "DUE;VALUE=DATE:20261015" in text and "PRIORITY:1" in text
    d = todo_to_dict(Calendar.from_ical(text).walk("VTODO")[0], TZ)
    assert d["summary"] == "Rinnovo dominio" and d["due"] == "2026-10-15" and d["priority"] == 1
    assert d["status"] == "NEEDS-ACTION" and d["alarms"] == ["1d"]
    apply_todo_fields(td, TZ, completed=True)
    d = todo_to_dict(td, TZ)
    assert d["status"] == "COMPLETED" and d["percent"] == 100 and d["completed_at"]
    apply_todo_fields(td, TZ, completed=False)
    d = todo_to_dict(td, TZ)
    assert d["status"] == "NEEDS-ACTION" and "completed_at" not in d


def test_csv_row_roundtrip():
    from mailcow_dav_mcp.bulk import contact_to_row, row_to_fields
    card = new_vcard(given="Mario", family="Rossi", prefix="Ing.", org="ACME", department="R&D",
                     emails=["work:mario@acme.it"], phones=["cell:+39 333 1"],
                     addresses=["work:Via Roma 1;Milano;MI;20100;IT"], categories=["Cliente", "VIP"], im=["telegram:@mario"])
    row = contact_to_row(vcard_to_dict(card))
    assert row["emails"] == "work:mario@acme.it" and row["phones"] == "cell:+39 333 1"
    assert row["addresses"] == "work:Via Roma 1;Milano;MI;20100;IT" and row["categories"] == "Cliente | VIP"
    fields = row_to_fields(row)
    card2 = new_vcard(**fields)
    d1, d2 = vcard_to_dict(card), vcard_to_dict(card2)
    for k in ("fn", "name", "org", "department", "addresses", "categories", "im"):
        assert d1[k] == d2[k], k
    assert [e["value"] for e in d2["emails"]] == ["mario@acme.it"]
    assert [p["value"] for p in d2["phones"]] == ["+39 333 1"]


def test_group_helpers():
    from mailcow_dav_mcp.carddav import _group_to_dict, _is_group, _members
    text = ("BEGIN:VCARD\r\nVERSION:3.0\r\nFN:Fornitori\r\nN:Fornitori\r\nUID:G1\r\n"
            "X-ADDRESSBOOKSERVER-KIND:group\r\n"
            "X-ADDRESSBOOKSERVER-MEMBER:urn:uuid:AAA\r\nX-ADDRESSBOOKSERVER-MEMBER:urn:uuid:BBB\r\nEND:VCARD\r\n")
    card = vobject.readOne(text)
    assert _is_group(card)
    d = _group_to_dict(card)
    assert d["name"] == "Fornitori" and d["members"] == ["AAA", "BBB"] and d["member_count"] == 2
    card.add("x-addressbookserver-member").value = "urn:uuid:CCC"
    assert len(_members(card)) == 3
    assert "X-ADDRESSBOOKSERVER-MEMBER:urn:uuid:CCC" in card.serialize()
    assert not _is_group(new_vcard(fn="Mario"))


def test_uid_lookup_never_takes_first_result():
    """Regressione: SOGo ignora il filtro UID e restituisce tutto; il match deve essere sull'UID reale."""
    from mailcow_dav_mcp.caldav import _has_uid
    from mailcow_dav_mcp.carddav import _card_uid
    a = new_vcard(uid="AAA-1", fn="A").serialize()
    b = new_vcard(uid="BBB-2", fn="B").serialize()
    assert _card_uid(a) == "AAA-1" and _card_uid(b) == "BBB-2"
    assert _card_uid("BEGIN:VCARD\r\nUID;X-FOO=1:Zzz\r\nEND:VCARD\r\n") == "Zzz"
    assert _card_uid("BEGIN:VCARD\r\nFN:x\r\nEND:VCARD\r\n") == ""
    cal = Calendar(); ev = Event(); ev.add("uid", "EV-1"); ev.add("summary", "x"); cal.add_component(ev)
    assert _has_uid(cal, "VEVENT", "EV-1") and not _has_uid(cal, "VEVENT", "ev-1") and not _has_uid(cal, "VEVENT", "EV-2") and not _has_uid(cal, "VTODO", "EV-1")


def test_serialize_vcard_folding_no_empty_continuation():
    """Regressione: vobject emetteva 'IT\\r\\n \\r\\nCATEGORIES' e Contatti mostrava 'ITCATEGORIES:Fornitori'."""
    from mailcow_dav_mcp.carddav import serialize_vcard
    card = new_vcard(fn="Ernesto Esemplaroni", categories=["Fornitori"],
                     addresses=["work:Piazza Esempio Fittizio, 20;Comune Immaginario;MI;20100;IT"],
                     note="à" * 200)
    text = serialize_vcard(card)
    assert "\r\n \r\n" not in text
    for line in text.split("\r\n"):
        assert len(line.encode("utf-8")) <= 75, line
        assert line != " "
    back = vobject.readOne(text)
    d = vcard_to_dict(back)
    assert d["addresses"][0]["country"] == "IT" and d["categories"] == ["Fornitori"]
    assert d["note"] == "à" * 200


def test_sogo_fold_problems_detection():
    from mailcow_dav_mcp.carddav import sogo_fold_problems
    bad = ("BEGIN:VCARD\r\nVERSION:3.0\r\nADR;TYPE=WORK:;;Piazza Esempio Fittizio\\, 20;Comune Immaginario;MI;20100;IT"
           "\r\n \r\nCATEGORIES:Fornitori\r\nNOTE:x\r\n y\r\nEND:VCARD\r\n")
    p = sogo_fold_problems(bad)
    assert len(p) == 1 and p[0].startswith("ADR;TYPE=WORK (")
    assert sogo_fold_problems(bad.replace("\r\n \r\n", "\r\n")) == []


class _FakeBooks:
    """AddressBooks con all() finto: niente rete, solo la logica di ricerca."""

    def __init__(self, cards):
        from mailcow_dav_mcp.carddav import AddressBooks
        self._contacts = [vcard_to_dict(c) for c in cards]
        self.search = AddressBooks.search.__get__(self)

    def all(self, book="personal", limit=500, category=None):
        assert limit is None, "la ricerca deve scansionare tutta la rubrica"
        from mailcow_dav_mcp.carddav import _filter_category
        return _filter_category(self._contacts, category) if category else list(self._contacts)


def test_search_matches_given_name_surname_accents_and_phone():
    """Regressione: il nome di battesimo non veniva trovato (filtro SOGo parziale + scan troncato a 500)."""
    cards = [new_vcard(given="Giulia", family="Bianchi", org="Studio Esempio & Associati", categories=["Clienti"]),
             new_vcard(given="Niccolò", family="Rossi", phones=["cell:+39 333 123-4567"])]
    cards += [new_vcard(fn=f"Filler {i}") for i in range(600)]
    books = _FakeBooks(cards)
    for q in ("Giulia", "giulia", "Bianchi", "Giulia Bianchi", "bianchi giulia", "esempio"):
        assert [c["fn"] for c in books.search(q)] == ["Giulia Bianchi"], q
    assert [c["fn"] for c in books.search("niccolo")] == ["Niccolò Rossi"]
    assert [c["fn"] for c in books.search("3331234567")] == ["Niccolò Rossi"]
    assert books.search("Giulia Verdi") == []
    assert books.search("   ") == []
    assert [c["fn"] for c in books.search("giulia", category="clienti")] == ["Giulia Bianchi"]
    assert books.search("giulia", category="Fornitori") == []
    assert len(books.search("filler", limit=10)) == 10
