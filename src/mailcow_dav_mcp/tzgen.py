"""Generate a compact RRULE-based VTIMEZONE (e.g. 'last Sunday of March') from zoneinfo.

VTIMEZONEs with explicit RDATEs (icalendar's default) are misread by some expanders;
the RRULE form is what Apple/Outlook/SOGo produce and is universally supported.
If the zone's transitions do not follow an 'Nth Sunday of the month' rule, it falls back to the explicit VTIMEZONE.
"""

from __future__ import annotations

import calendar
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from icalendar import Timezone, TimezoneDaylight, TimezoneStandard


def _fmt_offset(td: timedelta) -> str:
    total = int(td.total_seconds())
    sign = "+" if total >= 0 else "-"
    total = abs(total)
    return f"{sign}{total // 3600:02d}{(total % 3600) // 60:02d}"


def _transitions(tz: ZoneInfo, year: int) -> list[tuple[datetime, timedelta, timedelta]]:
    """Instants (in local time BEFORE the transition) at which the offset changes, in the given year."""
    from datetime import timezone as _utc

    out = []
    t = datetime(year, 1, 1, tzinfo=_utc.utc)
    end = datetime(year + 1, 1, 1, tzinfo=_utc.utc)
    prev_off = t.astimezone(tz).utcoffset()
    step = timedelta(hours=1)
    while t < end:
        t += step
        off = t.astimezone(tz).utcoffset()
        if off != prev_off:
            lo, hi = t - step, t  # raffina al minuto: hi è il primo istante col nuovo offset
            while (hi - lo) > timedelta(minutes=1):
                mid = lo + (hi - lo) / 2
                if mid.astimezone(tz).utcoffset() == prev_off:
                    lo = mid
                else:
                    hi = mid
            hi = hi.replace(second=0, microsecond=0)
            local_before = (hi + prev_off).replace(tzinfo=None)  # ora locale "di parete" al momento del cambio
            out.append((local_before, prev_off, off))
            prev_off = off
    return out


def _weekday_rule(d: date) -> str | None:
    """'-1SU' if last Sunday of the month, '2SU' if second, etc. Only Sundays/other fixed weekdays."""
    wd = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"][d.weekday()]
    last_day = calendar.monthrange(d.year, d.month)[1]
    if d.day + 7 > last_day:
        return f"-1{wd}"
    n = (d.day - 1) // 7 + 1
    return f"{n}{wd}"


_MAX_SCAN_YEARS = 80


def _signature(tz: ZoneInfo, year: int) -> tuple | None:
    """Zone rule for a year: ("fixed", offset) without DST, or the two transitions as
    (month, weekday, local time, offset from, offset to). None if the year does not follow a simple pattern."""
    trans = _transitions(tz, year)
    if not trans:
        return ("fixed", datetime(year, 1, 1, 12, tzinfo=tz).utcoffset())
    if len(trans) != 2:
        return None
    return tuple((local.month, _weekday_rule(local.date()), local.time(), frm, to) for local, frm, to in trans)


def rrule_vtimezone(tz: ZoneInfo, first_year: int | None = None, last_year: int | None = None) -> Timezone | None:
    """VTIMEZONE with RRULE if the zone follows the SAME rule throughout [first_year - 1, last_year].

    The DTSTART of each subcomponent is the real transition of the starting year (not 1970): before that
    date the definition does not claim to apply. If the rule changes within the range (e.g. a country
    that abolished daylight saving time) it returns None and the explicit VTIMEZONE is used."""
    this_year = date.today().year
    first_year = first_year or this_year
    last_year = max(last_year or this_year + 1, first_year + 1)
    start_year = first_year - 1
    if last_year - start_year > _MAX_SCAN_YEARS:
        return None
    sig = _signature(tz, start_year)
    if sig is None or any(_signature(tz, y) != sig for y in range(start_year + 1, last_year + 1)):
        return None
    key = tz.key or "UTC"
    vtz = Timezone()
    vtz.add("tzid", key)

    if sig[0] == "fixed":
        off = sig[1]
        st = TimezoneStandard()
        st.add("dtstart", datetime(start_year, 1, 1, 0, 0))
        st.add("tzoffsetfrom", off)
        st.add("tzoffsetto", off)
        st.add("tzname", datetime(start_year, 1, 1, 12, tzinfo=tz).tzname() or "STD")
        vtz.add_component(st)
        return vtz

    for (local, frm, to), (month, byday, _t, _f, _to) in zip(_transitions(tz, start_year), sig):
        comp = TimezoneDaylight() if to > frm else TimezoneStandard()
        comp.add("dtstart", local.replace(second=0, microsecond=0))
        comp.add("rrule", {"FREQ": "YEARLY", "BYMONTH": month, "BYDAY": byday})
        comp.add("tzoffsetfrom", frm)
        comp.add("tzoffsetto", to)
        sample = datetime(start_year, month, calendar.monthrange(start_year, month)[1], 23, 59, tzinfo=tz)
        comp.add("tzname", sample.tzname() or ("DST" if to > frm else "STD"))
        vtz.add_component(comp)
    return vtz


def vtimezone(tz: ZoneInfo, first_date: date | None = None, last_date: date | None = None) -> Timezone:
    """VTIMEZONE covering [first_date, last_date] (default: from this year; with no end, about +30 years)."""
    today = date.today()
    first = first_date or date(today.year, 1, 1)
    last = last_date or date(max(today.year, first.year) + 30, 1, 1)  # serie senza fine: copertura ampia
    vtz = rrule_vtimezone(tz, first.year, max(last.year, today.year + 1))
    if vtz is not None:
        return vtz
    return Timezone.from_tzinfo(tz, first_date=date(first.year - 1, 1, 1), last_date=date(last.year + 1, 12, 31))
