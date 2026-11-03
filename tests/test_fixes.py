"""Regressioni per i problemi emersi dalla code review (sicurezza, perdita dati, fusi, ricorrenze)."""

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
import vobject
from icalendar import Calendar, Event, Todo

from mailcow_dav_mcp import safety
from mailcow_dav_mcp.bulk import Bulk, _write_atomic
from mailcow_dav_mcp.caldav import Calendars, apply_event_fields, apply_todo_fields, new_calendar, parse_dt, serialize
from mailcow_dav_mcp.carddav import AddressBooks, _card_uid, apply_fields, load_photo, new_vcard, serialize_vcard
from mailcow_dav_mcp.dav import DavClient, DavError, PreconditionFailed
from mailcow_dav_mcp.tzgen import vtimezone

TZ = ZoneInfo("Europe/Rome")
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


# -- foto: SSRF, file locali, memoria ------------------------------------------------

@pytest.mark.parametrize("url", ["http://127.0.0.1/a.png", "http://169.254.169.254/latest/meta-data",
                                 "http://[::1]/x.png", "http://10.0.0.5/x.png", "http://192.168.1.1/x.png"])
def test_photo_url_rejects_non_public_addresses(url):
    with pytest.raises(ValueError, match="non-public"):
        safety.fetch_url_limited(url)


def test_photo_url_rejects_hostname_resolving_to_lan(monkeypatch):
    monkeypatch.setattr(safety.socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("192.168.0.10", 80))])
    with pytest.raises(ValueError, match="non-public"):
        safety.fetch_url_limited("http://photos.example.com/me.png")


def test_photo_url_rejects_non_http_scheme():
    with pytest.raises(ValueError):
        safety.fetch_url_limited("file:///etc/passwd")


def test_server_photo_value_never_reads_local_files(tmp_path):
    img = tmp_path / "secret.png"
    img.write_bytes(PNG)
    assert load_photo(str(img))[1] == "PNG"  # path esplicito dell'utente: ok
    with pytest.raises(ValueError):
        load_photo(str(img), allow_local=False)  # PHOTO;VALUE=URI arrivata dal server: mai


def test_photo_size_checked_before_loading(tmp_path):
    big = tmp_path / "big.png"
    big.write_bytes(PNG + b"\x00" * (safety.PHOTO_MAX + 10))
    with pytest.raises(ValueError, match="too large"):
        load_photo(str(big))
    with pytest.raises(ValueError, match="too large"):
        load_photo("data:image/png;base64," + "A" * (safety.PHOTO_MAX * 2))


def test_local_files_confined_in_http_mode(tmp_path):
    safety.confine_local_files(tmp_path)
    try:
        assert safety.local_path("ok.vcf") == (tmp_path / "ok.vcf").resolve()
        for bad in ("../fuori.vcf", "/etc/hosts", "~/x.vcf"):
            with pytest.raises(ValueError, match="not allowed"):
                safety.local_path(bad)
    finally:
        safety.confine_local_files(None)


# -- path DAV ----------------------------------------------------------------------

def test_collection_ids_cannot_escape():
    books = AddressBooks(None, "/SOGo/dav/u@example.com/")
    assert books.book_path("/personal/") == "/SOGo/dav/u@example.com/Contacts/personal/"
    for bad in ("../Calendar/personal", "a/b", "..", "", "x\\y"):
        with pytest.raises(ValueError):
            books.book_path(bad)
    cals = Calendars(None, "/SOGo/dav/u@example.com/", "Europe/Rome")
    with pytest.raises(ValueError):
        cals.cal_path("../Contacts/personal")


class _RecordingDav:
    def __init__(self):
        self.puts = []

    def put(self, href, body, ctype, **kw):
        self.puts.append(href)
        return '"1"'


def test_import_uid_never_becomes_path():
    dav = _RecordingDav()
    bulk = Bulk(AddressBooks(dav, "/SOGo/dav/u@example.com/"))
    card = new_vcard(uid="../other/injected", fn="X")
    res = bulk._put_card(card, "personal")
    href = dav.puts[0]
    assert href.startswith("/SOGo/dav/u@example.com/Contacts/personal/") and ".." not in href
    assert href.count("/") == "/SOGo/dav/u@example.com/Contacts/personal/x".count("/")
    assert res["uid"] == "../other/injected"  # l'UID nella scheda resta quello del file
    assert safety.resource_name("ABC-123@host") == "ABC-123@host"


# -- concorrenza / ETag ---------------------------------------------------------------

def test_dav_refuses_unconditional_writes():
    dav = DavClient("https://dav.example.com", "u", "p")
    with pytest.raises(DavError, match="ETag"):
        dav.put("/x.vcf", "BEGIN:VCARD", "text/vcard")
    with pytest.raises(DavError, match="ETag"):
        dav.delete("/x.vcf", etag=None)


def test_expected_etag_detects_concurrent_change():
    safety.check_expected_etag('"v1"', 'W/"v1"', "x")  # stessa versione, formati diversi
    safety.check_expected_etag(None, '"v2"', "x")       # nessuna aspettativa
    with pytest.raises(PreconditionFailed):
        safety.check_expected_etag('"v1"', '"v2"', "Il contatto")


class _OneCardDav:
    """Una sola scheda al path {uid}.vcf, con ETag che cambia a ogni PUT."""

    def __init__(self, text, etag='"v2"'):
        self.text, self.etag, self.puts = text, etag, 0

    def get(self, href):
        return self.text, self.etag

    def put(self, href, body, ctype, etag=None, create=False):
        self.puts += 1
        self.text = body
        return '"v3"'


def test_update_contact_with_stale_etag_fails_without_writing():
    dav = _OneCardDav(serialize_vcard(new_vcard(uid="AAA", fn="Mario", emails=["a@x.it", "b@x.it"])))
    books = AddressBooks(dav, "/SOGo/dav/u@example.com/")
    with pytest.raises(PreconditionFailed):
        books.update("AAA", expected_etag='"v1"', emails=["a@x.it", "c@x.it"])
    assert dav.puts == 0
    books._with_fold_warning = lambda result, href: result
    assert books.update("AAA", expected_etag='"v2"', note="ok")["etag"] == '"v3"'


# -- eventi -----------------------------------------------------------------------

def _event_with_duration():
    ev = Event()
    ev.add("uid", "E1")
    ev.add("dtstart", datetime(2026, 11, 3, 10, 0, tzinfo=TZ))
    ev.add("duration", timedelta(hours=3))
    ev.add("summary", "Workshop")
    return ev


def test_title_change_keeps_duration_event_length():
    ev = _event_with_duration()
    apply_event_fields(ev, TZ, summary="Workshop (aula 2)")
    assert ev.decoded("duration") == timedelta(hours=3) and "dtend" not in ev


def test_moving_duration_event_keeps_length():
    ev = _event_with_duration()
    apply_event_fields(ev, TZ, start="2026-11-04T14:00")
    assert ev.decoded("dtend") - ev.decoded("dtstart") == timedelta(hours=3)


def test_all_day_to_timed_conversion():
    ev = Event()
    ev.add("uid", "E2")
    ev.add("dtstart", date(2026, 11, 3))
    ev.add("dtend", date(2026, 11, 4))
    with pytest.raises(ValueError, match="start"):
        apply_event_fields(ev, TZ, all_day=False)
    apply_event_fields(ev, TZ, all_day=False, start="2026-11-03T15:00")
    assert isinstance(ev.decoded("dtstart"), datetime) and isinstance(ev.decoded("dtend"), datetime)
    assert ev.decoded("dtend") - ev.decoded("dtstart") == timedelta(hours=1)


def test_explicit_offset_keeps_instant_and_gets_real_timezone():
    dt = parse_dt("2026-11-03T10:00:00+02:00", TZ)
    assert dt.tzinfo is TZ and dt.astimezone(ZoneInfo("UTC")).hour == 8
    ev = Event()
    ev.add("uid", "E3")
    apply_event_fields(ev, TZ, summary="x", start="2026-11-03T10:00:00+02:00", all_day=False)
    text = serialize(new_calendar(ev))
    assert "TZID=Europe/Rome" in text and "BEGIN:VTIMEZONE" in text and "UTC+02:00" not in text
    back = Calendar.from_ical(text).walk("VEVENT")[0].decoded("dtstart")
    assert back.utcoffset() is not None and back.astimezone(ZoneInfo("UTC")).hour == 8


def test_moved_occurrence_found_by_original_recurrence_id():
    master = Event()
    master.add("uid", "S1")
    master.add("dtstart", datetime(2026, 11, 2, 10, 0, tzinfo=TZ))
    master.add("dtend", datetime(2026, 11, 2, 11, 0, tzinfo=TZ))
    master.add("rrule", {"FREQ": "WEEKLY", "COUNT": 4})
    master.add("summary", "Weekly")
    moved = Event()
    moved.add("uid", "S1")
    moved.add("recurrence-id", datetime(2026, 11, 9, 10, 0, tzinfo=TZ))
    moved.add("dtstart", datetime(2026, 11, 12, 14, 0, tzinfo=TZ))  # spostata di 3 giorni
    moved.add("dtend", datetime(2026, 11, 12, 15, 0, tzinfo=TZ))
    moved.add("summary", "Weekly (moved)")
    cal = new_calendar(master)
    cal.add_component(moved)
    cals = Calendars(None, "/SOGo/dav/u@example.com/", "Europe/Rome")
    occ = cals._occurrence(cal, "2026-11-09T10:00:00+01:00")
    assert str(occ.get("summary")) == "Weekly (moved)"
    assert str(cals._occurrence(cal, "2026-11-09").get("summary")) == "Weekly (moved)"


def test_vtimezone_uses_rules_valid_at_event_date():
    sp = ZoneInfo("America/Sao_Paulo")
    vtz = vtimezone(sp, date(2018, 1, 15), date(2018, 1, 15))
    text = ("BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:x\r\n" + vtz.to_ical().decode() +
            "END:VCALENDAR\r\n")
    tzc = Calendar.from_ical(text).walk("VTIMEZONE")[0]
    offsets = {str(c.get("tzoffsetto").to_ical()) for c in tzc.subcomponents}
    assert any("-0200" in o for o in offsets), offsets  # in gennaio 2018 c'era l'ora legale (-02:00)
    rome = vtimezone(TZ, date(2026, 5, 1), date(2026, 5, 1)).to_ical().decode()
    assert "RRULE" in rome and "19700" not in rome


# -- task ---------------------------------------------------------------------------

def test_task_alarms_relative_to_due():
    td = Todo()
    td.add("uid", "T1")
    apply_todo_fields(td, TZ, summary="Rinnovo", due="2026-11-20", alarms=["1d"])
    trig = td.walk("VALARM")[0]["trigger"]
    assert trig.params.get("RELATED") == "END"
    td2 = Todo()
    td2.add("uid", "T2")
    with pytest.raises(ValueError, match="deadline"):
        apply_todo_fields(td2, TZ, summary="Senza date", alarms=["15m"])


# -- vCard --------------------------------------------------------------------------

def test_long_uid_survives_folding():
    uid = "U" * 100
    text = serialize_vcard(new_vcard(uid=uid, fn="Long"))
    assert "\r\n " in text  # l'UID è davvero ripiegato
    assert _card_uid(text) == uid


def test_org_update_keeps_extra_units():
    card = vobject.readOne("BEGIN:VCARD\r\nVERSION:3.0\r\nFN:X\r\nORG:Company;Department;Team\r\nEND:VCARD\r\n")
    apply_fields(card, org="NewCompany")
    assert list(card.org.value) == ["NewCompany", "Department", "Team"]
    apply_fields(card, department="Sales")
    assert list(card.org.value) == ["NewCompany", "Sales", "Team"]


# -- export ---------------------------------------------------------------------------

class _FakeRawBooks:
    def __init__(self, resources, missing=()):
        self._res, self._missing = resources, list(missing)

    def raw_resources(self, book):
        return self._res, self._missing


def test_export_vcf_includes_groups_and_never_overwrites(tmp_path, monkeypatch):
    monkeypatch.setenv("MAILCOW_DAV_EXPORT_DIR", str(tmp_path))
    contact = serialize_vcard(new_vcard(uid="C1", fn="Mario"))
    group = ("BEGIN:VCARD\r\nVERSION:3.0\r\nUID:G1\r\nFN:Fornitori\r\nX-ADDRESSBOOKSERVER-KIND:group\r\n"
             "X-ADDRESSBOOKSERVER-MEMBER:urn:uuid:C1\r\nEND:VCARD\r\n")
    bulk = Bulk(_FakeRawBooks([("/a/C1.vcf", contact, '"1"'), ("/a/G1.vcf", group, '"1"')], missing=["/a/lost.vcf"]))
    r1 = bulk.export("personal", "vcf")
    r2 = bulk.export("personal", "vcf")
    assert r1["path"] != r2["path"]
    body = open(r1["path"], encoding="utf-8").read()
    assert "X-ADDRESSBOOKSERVER-KIND:group" in body and r1["count"] == 2 and r1["groups"] == 1
    assert r1["missing"] == ["/a/lost.vcf"] and "warning" in r1
    csv_res = bulk.export("personal", "csv")
    assert csv_res["count"] == 1 and csv_res["groups_not_exported"] == 1
    explicit = tmp_path / "backup.vcf"
    explicit.write_text("vecchio backup")
    with pytest.raises(FileExistsError):
        bulk.export("personal", "vcf", path=str(explicit))
    assert explicit.read_text() == "vecchio backup"
    bulk.export("personal", "vcf", path=str(explicit), overwrite=True)
    assert "Mario" in explicit.read_text()


def test_export_name_sanitized(tmp_path, monkeypatch):
    monkeypatch.setenv("MAILCOW_DAV_EXPORT_DIR", str(tmp_path))
    out = Bulk._export_target("/../../etc/personal", "vcf", None)
    assert out.parent == tmp_path.resolve() and "/" not in out.name


def test_write_atomic_leaves_no_temp_on_conflict(tmp_path):
    out = tmp_path / "x.vcf"
    _write_atomic(out, "uno", overwrite=False)
    with pytest.raises(FileExistsError):
        _write_atomic(out, "due", overwrite=False)
    assert out.read_text() == "uno" and [p.name for p in tmp_path.iterdir()] == ["x.vcf"]


# -- seconda review --------------------------------------------------------------------

@pytest.mark.parametrize("bad", ["%2e%2e%2fother", "personal?x=1", "personal#frag", "a b", "per%2Fsonal"])
def test_collection_ids_reject_encoded_and_query(bad):
    with pytest.raises(ValueError):
        AddressBooks(None, "/p/").book_path(bad)


@pytest.mark.parametrize("ok", ["personal", "team.example", "4A-66BCC400-1-546E4A80", "user_D_x_A_example_D_com_personal"])
def test_collection_ids_accept_sogo_ids(ok):
    assert AddressBooks(None, "/p/").book_path(ok).endswith(f"/{ok}/")


def test_ambiguous_dst_offset_keeps_instant():
    for raw, utc_hour in (("2026-10-25T02:30:00+01:00", 1), ("2026-10-25T02:30:00+02:00", 0)):
        ev = Event()
        ev.add("uid", "D1")
        apply_event_fields(ev, TZ, summary="x", start=raw, all_day=False)
        back = Calendar.from_ical(serialize(new_calendar(ev))).walk("VEVENT")[0].decoded("dtstart")
        assert back.astimezone(ZoneInfo("UTC")).hour == utc_hour, raw


def test_end_before_start_across_dst_rejected_in_utc():
    ev = Event()
    ev.add("uid", "D2")
    with pytest.raises(ValueError, match="end"):
        apply_event_fields(ev, TZ, summary="x", start="2026-10-25T02:15:00+01:00", end="2026-10-25T02:45:00+02:00",
                           all_day=False)
    apply_event_fields(ev, TZ, summary="x", start="2026-10-25T02:45:00+02:00", end="2026-10-25T02:15:00+01:00",
                       all_day=False)  # 00:45Z → 01:15Z: valido


def test_removing_due_reanchors_or_blocks_alarms():
    td = Todo()
    td.add("uid", "T3")
    apply_todo_fields(td, TZ, summary="x", due="2026-11-20", start="2026-11-10", alarms=["1d"])
    apply_todo_fields(td, TZ, due="")
    assert "RELATED" not in td.walk("VALARM")[0]["trigger"].params
    apply_todo_fields(td, TZ, due="2026-11-30")
    assert "RELATED" not in td.walk("VALARM")[0]["trigger"].params  # ancora valida (start): non si sposta
    with pytest.raises(ValueError, match="alarms"):
        apply_todo_fields(td, TZ, due="", start="")


def test_vtimezone_covers_whole_finite_series():
    ny = ZoneInfo("America/New_York")
    master = Event()
    master.add("uid", "NY")
    master.add("dtstart", datetime(2000, 7, 1, 10, 0, tzinfo=ny))
    master.add("dtend", datetime(2000, 7, 1, 11, 0, tzinfo=ny))
    master.add("rrule", {"FREQ": "YEARLY", "COUNT": 51})
    text = serialize(new_calendar(master))
    from mailcow_dav_mcp.caldav import _series_end
    assert _series_end(master) == date(2050, 7, 1)
    tzc = Calendar.from_ical(text).walk("VTIMEZONE")[0]
    # o una VTIMEZONE a regole senza fine, o una esplicita che arriva almeno al 2050
    has_rrule = any(c.get("rrule") is not None for c in tzc.subcomponents)
    assert has_rrule or "2050" in text


def test_ipv6_host_header_bracketed(monkeypatch):
    seen = {}

    def fake_send(self, request, **kw):
        seen["host"] = request.headers["Host"]
        seen["enc"] = request.headers["Accept-Encoding"]
        raise RuntimeError("stop")

    monkeypatch.setattr(safety, "_check_public_host", lambda h, p: "2001:db8::1")
    monkeypatch.setattr(safety.httpx.Client, "send", fake_send)
    with pytest.raises(RuntimeError):
        safety.fetch_url_limited("http://[2001:4860:4860::8888]:8080/x.png")
    assert seen == {"host": "[2001:4860:4860::8888]:8080", "enc": "identity"}


def test_http_token_read_from_dotenv(tmp_path, monkeypatch):
    from mailcow_dav_mcp import server
    env = tmp_path / ".env"
    env.write_text("MAILCOW_DAV_MCP_TOKEN=from-dotenv\n")
    monkeypatch.setenv("MAILCOW_DAV_ENV_FILE", str(env))
    monkeypatch.delenv("MAILCOW_DAV_MCP_TOKEN", raising=False)
    captured = {}
    monkeypatch.setattr(server, "_run_http", lambda h, p, path: captured.update(token=server.os.environ.get("MAILCOW_DAV_MCP_TOKEN")))
    monkeypatch.setattr(server.sys, "argv", ["mailcow-dav-mcp", "--transport", "http"])
    server.main()
    assert captured["token"] == "from-dotenv"
    server.os.environ.pop("MAILCOW_DAV_MCP_TOKEN", None)
