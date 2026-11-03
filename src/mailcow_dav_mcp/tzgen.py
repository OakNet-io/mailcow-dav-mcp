"""Genera una VTIMEZONE compatta basata su RRULE (es. 'ultima domenica di marzo') a partire da zoneinfo.

Le VTIMEZONE con RDATE espliciti (default di icalendar) vengono interpretate male da alcuni expander;
la forma con RRULE è quella che producono Apple/Outlook/SOGo ed è universalmente supportata.
Se le transizioni del fuso non seguono una regola 'N-esima domenica del mese', si ricade sulla VTIMEZONE esplicita.
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
    """Istanti (in ora locale PRIMA della transizione) in cui cambia l'offset, nell'anno dato."""
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
    """'-1SU' se ultima domenica del mese, '2SU' se seconda, ecc. Solo domeniche/altri giorni fissi."""
    wd = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"][d.weekday()]
    last_day = calendar.monthrange(d.year, d.month)[1]
    if d.day + 7 > last_day:
        return f"-1{wd}"
    n = (d.day - 1) // 7 + 1
    return f"{n}{wd}"


_MAX_SCAN_YEARS = 80


def _signature(tz: ZoneInfo, year: int) -> tuple | None:
    """Regola del fuso in un anno: ("fixed", offset) senza DST, oppure le due transizioni come
    (mese, giorno-settimana, ora locale, offset da, offset a). None se l'anno non segue uno schema semplice."""
    trans = _transitions(tz, year)
    if not trans:
        return ("fixed", datetime(year, 1, 1, 12, tzinfo=tz).utcoffset())
    if len(trans) != 2:
        return None
    return tuple((local.month, _weekday_rule(local.date()), local.time(), frm, to) for local, frm, to in trans)


def rrule_vtimezone(tz: ZoneInfo, first_year: int | None = None, last_year: int | None = None) -> Timezone | None:
    """VTIMEZONE con RRULE se il fuso segue la STESSA regola in tutto [first_year - 1, last_year].

    La DTSTART di ogni sotto-componente è la transizione reale dell'anno iniziale (non il 1970): prima di
    quella data la definizione non pretende di valere. Se la regola cambia nell'intervallo (es. un Paese
    che ha abolito l'ora legale) ritorna None e si usa la VTIMEZONE esplicita."""
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
    """VTIMEZONE che copre [first_date, last_date] (default: da quest'anno; senza fine, +10 anni)."""
    today = date.today()
    first = first_date or date(today.year, 1, 1)
    last = last_date or date(max(today.year, first.year) + 30, 1, 1)  # serie senza fine: copertura ampia
    vtz = rrule_vtimezone(tz, first.year, max(last.year, today.year + 1))
    if vtz is not None:
        return vtz
    return Timezone.from_tzinfo(tz, first_date=date(first.year - 1, 1, 1), last_date=date(last.year + 1, 12, 31))
