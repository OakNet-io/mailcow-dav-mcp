"""Regressioni dell'audit di sicurezza e privacy."""

import asyncio
import gzip
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
import pytest
from icalendar import Calendar, Event

from mailcow_dav_mcp import safety, server
from mailcow_dav_mcp.bulk import Bulk
from mailcow_dav_mcp.caldav import Calendars, _series_end, apply_event_fields, new_calendar
from mailcow_dav_mcp.carddav import AddressBooks, new_vcard, serialize_vcard
from mailcow_dav_mcp.config import _load_dotenv
from mailcow_dav_mcp.dav import DavClient, DavError, PreconditionFailed

TZ = ZoneInfo("Europe/Rome")
PRINCIPAL = "/SOGo/dav/u@example.com/"


def _ms(*hrefs):
    body = "".join(f"<D:response><D:href>{h}</D:href><D:propstat><D:prop><D:getetag>\"1\"</D:getetag></D:prop>"
                   "<D:status>HTTP/1.1 200 OK</D:status></D:propstat></D:response>" for h in hrefs)
    return f'<?xml version="1.0"?><D:multistatus xmlns:D="DAV:">{body}</D:multistatus>'.encode()


def _client(handler):
    dav = DavClient("https://dav.example.com", "u", "secret")
    dav._client = httpx.Client(base_url=dav.base_url, auth=("u", "secret"),
                               transport=httpx.MockTransport(handler))
    return dav


# -- 1. href verso altri host / fuori dalla collection -------------------------------------

def test_foreign_or_escaping_href_rejected():
    dav = DavClient("https://dav.example.com", "u", "p")
    assert dav.parse_multistatus(_ms("https://dav.example.com/a/1.vcf"))[0].href == "/a/1.vcf"
    for bad in ("https://attacker.example/x.vcf", "//attacker.example/x", "/a/../../etc/x", "/a/%2e%2e/x"):
        with pytest.raises(DavError):
            dav.parse_multistatus(_ms(bad))
    with pytest.raises(DavError, match="fuori"):
        dav.parse_multistatus(_ms("/SOGo/dav/other/Contacts/personal/x.vcf"), within=PRINCIPAL + "Contacts/personal/")


def test_request_never_leaves_configured_host():
    seen = []
    dav = _client(lambda req: seen.append(str(req.url)) or httpx.Response(200, stream=httpx.ByteStream(b"x")))
    for bad in ("https://attacker.example/x", "//attacker.example/x", "/a/../b"):
        with pytest.raises(DavError):
            dav.get(bad)
    assert seen == []


# -- 2. ricorrenze che non terminano ---------------------------------------------------------

def test_rrule_interval_zero_rejected_and_not_expanded():
    with pytest.raises(ValueError, match="INTERVAL"):
        apply_event_fields(Event(), TZ, start="2026-11-03T12:00", rrule="FREQ=DAILY;INTERVAL=0;COUNT=1;BYMONTH=2")
    ev = Event.from_ical("BEGIN:VEVENT\r\nUID:Z\r\nDTSTART:20261103T120000Z\r\n"
                         "RRULE:FREQ=DAILY;INTERVAL=0;COUNT=1;BYMONTH=2\r\nEND:VEVENT\r\n")
    assert _series_end(ev) is None
    cals = Calendars(None, PRINCIPAL)
    cal = Calendar()
    cal.add_component(ev)
    out, truncated = cals._expand(cal, datetime(2026, 1, 1, tzinfo=TZ), datetime(2027, 1, 1, tzinfo=TZ))
    assert truncated and len(out) == 1
    sec = Event.from_ical("BEGIN:VEVENT\r\nUID:S\r\nDTSTART:19700101T000000Z\r\nRRULE:FREQ=SECONDLY\r\nEND:VEVENT\r\n")
    cal2 = Calendar()
    cal2.add_component(sec)
    assert cals._expand(cal2, datetime(2026, 1, 1, tzinfo=TZ), datetime(2027, 1, 1, tzinfo=TZ))[1]


def test_vtimezone_horizon_capped():
    ev = Event()
    ev.add("uid", "Y")
    ev.add("dtstart", datetime(2026, 1, 1, 10, 0, tzinfo=TZ))
    ev.add("rrule", {"FREQ": "YEARLY", "UNTIL": datetime(9998, 12, 31, tzinfo=ZoneInfo("UTC"))})
    cal = new_calendar(ev)
    assert len(cal.to_ical()) < 20000


def test_attendee_and_rrule_control_chars_rejected():
    for bad in ("a@example.com\rX-EVIL:1", "nome <a@ex\nample.com>", "non-una-email"):
        with pytest.raises(ValueError):
            apply_event_fields(Event(), TZ, start="2026-11-03T12:00", attendees=[bad])
    with pytest.raises(ValueError):
        apply_event_fields(Event(), TZ, start="2026-11-03T12:00", rrule="FREQ=DAILY\r\nX-EVIL:1")


# -- 3. GET di riserva senza ETag: l'UID va ricontrollato ------------------------------------

def test_fallback_get_rechecks_uid():
    class Dav:
        def get(self, href):
            return serialize_vcard(new_vcard(uid="OTHER", fn="Altro")), '"new"'
    books = AddressBooks(Dav(), PRINCIPAL)
    with pytest.raises(PreconditionFailed):
        books._with_etag("/x.vcf", serialize_vcard(new_vcard(uid="TARGET", fn="T")), None)


def test_event_master_matches_uid():
    cal = Calendar.from_ical(
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//t//EN\r\n"
        "BEGIN:VEVENT\r\nUID:OTHER\r\nDTSTAMP:20261101T000000Z\r\nDTSTART:20261110T100000Z\r\nSUMMARY:Altro\r\nEND:VEVENT\r\n"
        "BEGIN:VEVENT\r\nUID:MINE\r\nDTSTAMP:20261101T000000Z\r\nDTSTART:20261111T100000Z\r\nSUMMARY:Mio\r\nEND:VEVENT\r\n"
        "END:VCALENDAR\r\n")
    assert str(Calendars(None, PRINCIPAL)._master(cal, "MINE")["summary"]) == "Mio"


# -- 4. risposte limitate -------------------------------------------------------------------

def test_dav_response_size_capped_after_decompression(monkeypatch):
    monkeypatch.setenv("MAILCOW_DAV_MAX_RESPONSE_MB", "1")
    bomb = gzip.compress(b"A" * (3 * 1024 * 1024))
    dav = _client(lambda req: httpx.Response(200, stream=httpx.ByteStream(bomb), headers={"Content-Encoding": "gzip"}))
    with pytest.raises(DavError, match="oltre"):
        dav.get("/a/x.vcf")
    small = gzip.compress(b"BEGIN:VCARD")
    dav = _client(lambda req: httpx.Response(200, stream=httpx.ByteStream(small),
                                                 headers={"Content-Encoding": "gzip", "ETag": '"1"'}))
    assert dav.get("/a/x.vcf") == ("BEGIN:VCARD", '"1"')


# -- 5. import limitato ---------------------------------------------------------------------

def test_import_limits(tmp_path, monkeypatch):
    monkeypatch.setenv("MAILCOW_DAV_MAX_IMPORT_CARDS", "2")
    f = tmp_path / "in.vcf"
    f.write_text("".join(serialize_vcard(new_vcard(uid=f"U{i}", fn=f"N{i}")) for i in range(3)), encoding="utf-8")
    with pytest.raises(ValueError, match="Troppe"):
        Bulk(None)._load(str(f))
    monkeypatch.setenv("MAILCOW_DAV_MAX_IMPORT_MB", "0")
    with pytest.raises(ValueError, match="grande"):
        Bulk(None)._load(str(f))


# -- 6. annotazioni e sola lettura -----------------------------------------------------------

def test_tool_annotations_and_read_only(monkeypatch):
    tools = {t.name: t for t in asyncio.run(server.mcp.list_tools())}
    assert tools["search_contacts"].annotations.readOnlyHint is True
    assert tools["delete_event"].annotations.destructiveHint is True
    assert tools["create_event"].annotations.openWorldHint is True
    assert "delete_contact" in server.WRITE_TOOLS and "export_contacts" not in server.WRITE_TOOLS
    removed = []
    monkeypatch.setenv("MAILCOW_DAV_READ_ONLY", "1")
    monkeypatch.setattr(server.mcp, "remove_tool", removed.append)
    server._apply_read_only()
    assert set(removed) == server.WRITE_TOOLS and "search_contacts" not in removed


# -- 7. .env --------------------------------------------------------------------------------

def test_dotenv_only_explicit_file_and_prefixed_keys(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("MAILCOW_DAV_USER=cwd-user\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("mailcow_dav_mcp.config._project_env", lambda: tmp_path / "no-project.env")
    monkeypatch.delenv("MAILCOW_DAV_ENV_FILE", raising=False)
    monkeypatch.delenv("MAILCOW_DAV_USER", raising=False)
    monkeypatch.delenv("HTTPS_PROXY", raising=False)
    _load_dotenv()
    import os
    assert os.environ.get("MAILCOW_DAV_USER") != "cwd-user"  # la cartella corrente non conta
    env = tmp_path / "conf.env"
    env.write_text("MAILCOW_DAV_USER=ok\nHTTPS_PROXY=http://attacker:8080\n")
    monkeypatch.setenv("MAILCOW_DAV_ENV_FILE", str(env))
    monkeypatch.delenv("MAILCOW_DAV_USER", raising=False)
    _load_dotenv()
    assert os.environ["MAILCOW_DAV_USER"] == "ok" and "HTTPS_PROXY" not in os.environ
    monkeypatch.setenv("MAILCOW_DAV_ENV_FILE", str(tmp_path / "missing.env"))
    with pytest.raises(RuntimeError, match="inesistente"):
        _load_dotenv()


# -- 8. file locali sempre confinati ---------------------------------------------------------

def test_stdio_confines_local_files(tmp_path, monkeypatch):
    allowed = tmp_path / "exports"
    extra = tmp_path / "extra"
    extra.mkdir()
    monkeypatch.setenv("MAILCOW_DAV_EXPORT_DIR", str(allowed))
    monkeypatch.setenv("MAILCOW_DAV_LOCAL_DIRS", str(extra))
    try:
        server._confine()
        assert safety.local_path(str(extra / "a.png")) == (extra / "a.png").resolve()
        assert safety.local_path("x.vcf") == (allowed / "x.vcf").resolve()
        with pytest.raises(ValueError, match="non consentito"):
            safety.local_path(str(tmp_path / "secret.png"))
    finally:
        safety.confine_local_files(None)


# -- 9. foto ----------------------------------------------------------------------------------

def test_photo_url_not_fetched_by_default_and_embedded_capped(monkeypatch):
    card = new_vcard(uid="P1", fn="Foto")
    card.add("photo").value = "https://tracker.example/pixel.png"
    card.photo.params["VALUE"] = ["uri"]

    class Dav:
        def __init__(self, text):
            self.text = text

        def get(self, href):
            return self.text, '"1"'
    books = AddressBooks(Dav(serialize_vcard(card)), PRINCIPAL)
    monkeypatch.setattr("mailcow_dav_mcp.carddav.fetch_url_limited", lambda *a: pytest.fail("non doveva scaricare"))
    assert books.photo("P1") == "https://tracker.example/pixel.png"
    monkeypatch.setattr("mailcow_dav_mcp.carddav.PHOTO_MAX", 10)
    big = new_vcard(uid="P1", fn="Foto")
    big.add("photo").value = b"\xff\xd8" + b"0" * 100
    big.photo.params["ENCODING"] = ["b"]
    books.dav.text = serialize_vcard(big)
    with pytest.raises(ValueError, match="troppo grande"):
        books.photo("P1")


# -- verifica dei fix (seconda passata Codex) -------------------------------------------------

def test_default_port_href_accepted():
    dav = DavClient("https://dav.example.com", "u", "p")
    assert dav.parse_multistatus(_ms("https://dav.example.com:443/a/1.vcf"))[0].href == "/a/1.vcf"
    with pytest.raises(DavError):
        dav.parse_multistatus(_ms("https://dav.example.com:8443/a/1.vcf"))


def test_deflate_and_identity_responses():
    import zlib
    for enc, body in (("deflate", zlib.compress(b"OK")), ("deflate", zlib.compress(b"OK")[2:-4]), ("", b"OK")):
        dav = _client(lambda req, b=body, e=enc: httpx.Response(200, stream=httpx.ByteStream(b),
                                                                 headers={"Content-Encoding": e} if e else {}))
        assert dav.get("/a/x")[0] == "OK"


def test_data_uri_photo_returned_as_image_and_capped(monkeypatch):
    import base64
    png = b"\x89PNG\r\n\x1a\n" + b"0" * 32
    card = new_vcard(uid="D1", fn="Data")
    card.add("photo").value = "data:image/png;base64," + base64.b64encode(png).decode()
    card.photo.params["VALUE"] = ["uri"]

    class Dav:
        def get(self, href):
            return serialize_vcard(card), '"1"'
    books = AddressBooks(Dav(), PRINCIPAL)
    assert books.photo("D1") == (png, "image/png")
    monkeypatch.setattr("mailcow_dav_mcp.carddav.PHOTO_MAX", 8)
    with pytest.raises(ValueError):
        books.photo("D1")


def test_export_and_photo_annotations():
    tools = {t.name: t for t in asyncio.run(server.mcp.list_tools())}
    assert tools["export_contacts"].annotations.destructiveHint is True
    assert tools["get_contact_photo"].annotations.openWorldHint is True
    assert "export_contacts" not in server.WRITE_TOOLS


def test_unwritable_export_dir_does_not_block_start(tmp_path, monkeypatch):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    monkeypatch.setenv("MAILCOW_DAV_EXPORT_DIR", str(blocker / "sub"))
    try:
        server._confine()
    finally:
        safety.confine_local_files(None)


def test_csv_with_cr_line_endings(tmp_path):
    f = tmp_path / "in.csv"
    f.write_bytes(b"fn,email\rMario,mario@example.com\r")
    assert [c.fn.value for c in Bulk(None)._load(str(f))] == ["Mario"]


def test_vtimezone_for_far_future_event():
    ev = Event()
    ev.add("uid", "G")
    ev.add("dtstart", datetime(2079, 5, 1, 12, 0, tzinfo=ZoneInfo("Asia/Gaza")))
    cal = new_calendar(ev)
    assert len(list(cal.walk("VTIMEZONE"))) == 1
    assert list(cal.walk("VTIMEZONE"))[0].subcomponents


def test_impossible_rrule_rejected_on_write():
    with pytest.raises(ValueError, match="nessuna occorrenza"):
        apply_event_fields(Event(), TZ, start="2026-01-01T10:00", rrule="FREQ=YEARLY;BYMONTH=2;BYMONTHDAY=30")


def test_exceptions_and_respond_scoped_to_uid():
    cal = Calendar.from_ical(
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//t//EN\r\n"
        "BEGIN:VEVENT\r\nUID:MINE\r\nDTSTAMP:20261101T000000Z\r\nDTSTART:20261102T100000Z\r\n"
        "RRULE:FREQ=WEEKLY;COUNT=3\r\nSUMMARY:Mio\r\nEND:VEVENT\r\n"
        "BEGIN:VEVENT\r\nUID:OTHER\r\nDTSTAMP:20261101T000000Z\r\nRECURRENCE-ID:20261109T100000Z\r\n"
        "DTSTART:20261109T100000Z\r\nSUMMARY:Altro\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n")
    occ = Calendars(None, PRINCIPAL)._occurrence(cal, "2026-11-09T10:00:00Z", "MINE")
    assert str(occ["uid"]) == "MINE"
