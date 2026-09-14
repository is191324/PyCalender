"""Minimaler, gehärteter iCalendar-Parser und -Writer (RFC 5545).

Warum ein eigener Parser?  Fremdbibliotheken akzeptieren beliebig tiefe
Verschachtelung und beliebig lange Felder; ein bösartiger Kalender-Feed
kann damit den Speicher der App fuellen. Dieser Parser arbeitet strikt
zeilenbasiert mit harten Obergrenzen:

* maximale Dateigroesse (MAX_ICS_BYTES)
* maximale Anzahl VEVENTs
* maximale Zeilenzahl und Feldlaenge
* keine Ausführung, keine externen Referenzen, kein XML - damit sind
  XXE- und Entity-Expansion-Angriffe strukturell ausgeschlossen
"""
from __future__ import annotations

import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional

from ..models import Event, ensure_aware, utcnow
from ..security import (MAX_ICS_BYTES, sanitize_text, sanitize_title,
                        SecurityError)

MAX_EVENTS = 20_000
MAX_LINES = 1_000_000
MAX_LINE_LEN = 8_192
MAX_PROPS_PER_EVENT = 200
MAX_EVENT_DURATION = timedelta(days=366)


class IcsParseError(SecurityError):
    pass


_ESCAPES = {"\\n": "\n", "\\N": "\n", "\\,": ",", "\\;": ";",
            "\\\\": "\\"}


def _unescape(value: str) -> str:
    out = []
    i = 0
    while i < len(value):
        if value[i] == "\\" and i + 1 < len(value):
            out.append(_ESCAPES.get(value[i:i + 2], value[i + 1]))
            i += 2
        else:
            out.append(value[i])
            i += 1
    return "".join(out)


def _escape(value: str) -> str:
    return (str(value).replace("\\", "\\\\").replace("\n", "\\n")
            .replace(",", "\\,").replace(";", "\\;"))


def _unfold(text: str) -> list[str]:
    """RFC-5545-Zeilenfaltung aufloesen, mit Zeilenlimit."""
    lines: list[str] = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if len(lines) > MAX_LINES:
            raise IcsParseError("iCalendar-Datei hat zu viele Zeilen.")
        if raw[:1] in (" ", "\t") and lines:
            lines[-1] += raw[1:]
            if len(lines[-1]) > MAX_LINE_LEN * 4:
                raise IcsParseError("Ueberlange iCalendar-Zeile.")
        else:
            lines.append(raw[:MAX_LINE_LEN])
    return lines


def parse_dt(value: str, params: dict) -> tuple[Optional[datetime], bool]:
    """Gibt (datetime, all_day) zurück."""
    v = (value or "").strip()
    if not v:
        return None, False
    if params.get("VALUE") == "DATE" or re.fullmatch(r"\d{8}", v):
        try:
            return datetime.strptime(v[:8], "%Y%m%d").replace(tzinfo=timezone.utc), True
        except ValueError:
            return None, False
    fmt = "%Y%m%dT%H%M%SZ" if v.endswith("Z") else "%Y%m%dT%H%M%S"
    try:
        dt = datetime.strptime(v, fmt)
    except ValueError:
        try:
            dt = datetime.fromisoformat(v)
        except ValueError:
            return None, False
    return ensure_aware(dt), False


def format_dt(dt: datetime, all_day: bool = False) -> str:
    dt = ensure_aware(dt).astimezone(timezone.utc)
    return dt.strftime("%Y%m%d") if all_day else dt.strftime("%Y%m%dT%H%M%SZ")


def parse_ics(text: str, calendar_id: int = 1, source: str = "ics",
              max_bytes: int = MAX_ICS_BYTES) -> list[Event]:
    if text is None:
        return []
    if isinstance(text, bytes):
        text = text.decode("utf-8", errors="replace")
    if len(text.encode("utf-8", errors="ignore")) > max_bytes:
        raise IcsParseError("Kalenderdatei ist zu groß.")

    events: list[Event] = []
    current: Optional[dict] = None
    prop_count = 0
    depth = 0

    for line in _unfold(text):
        line = line.strip()
        if not line:
            continue
        upper = line.upper()
        if upper.startswith("BEGIN:"):
            depth += 1
            if depth > 5:
                raise IcsParseError("Zu tief verschachtelte Kalenderdatei.")
            if upper == "BEGIN:VEVENT":
                if len(events) >= MAX_EVENTS:
                    raise IcsParseError("Kalender enthält zu viele Termine.")
                current, prop_count = {}, 0
            continue
        if upper.startswith("END:"):
            depth = max(0, depth - 1)
            if upper == "END:VEVENT" and current is not None:
                ev = _dict_to_event(current, calendar_id, source)
                if ev is not None:
                    events.append(ev)
                current = None
            continue
        if current is None:
            continue
        prop_count += 1
        if prop_count > MAX_PROPS_PER_EVENT:
            continue
        if ":" not in line:
            continue
        head, value = line.split(":", 1)
        parts = head.split(";")
        name = parts[0].upper()
        params = {}
        for p in parts[1:]:
            if "=" in p:
                k, v = p.split("=", 1)
                params[k.upper()] = v.strip('"')
        current.setdefault(name, []).append((params, value))
    return events


def _first(data: dict, key: str) -> tuple[dict, str]:
    items = data.get(key)
    return items[0] if items else ({}, "")


def _dict_to_event(data: dict, calendar_id: int, source: str) -> Optional[Event]:
    params, dtstart = _first(data, "DTSTART")
    start, all_day = parse_dt(dtstart, params)
    if start is None:
        return None
    eparams, dtend = _first(data, "DTEND")
    end, _ = parse_dt(dtend, eparams)
    if end is None:
        _, dur = _first(data, "DURATION")
        default = timedelta(days=1) if all_day else timedelta(hours=1)
        end = _safe_add(start, _parse_duration(dur) if dur else default)
    # SICHERHEITSFIX PT-G6: DTEND vor DTSTART bzw. unplausible Zeiträume
    # (präparierter Feed) werden auf einen sinnvollen Wert zurückgesetzt.
    if end < start or (end - start) > MAX_EVENT_DURATION:
        end = _safe_add(start, timedelta(days=1) if all_day else timedelta(hours=1))

    exdates = []
    for p, value in data.get("EXDATE", [])[:200]:
        for chunk in value.split(",")[:200]:
            dt, _ = parse_dt(chunk, p)
            if dt:
                exdates.append(dt)

    _, uid = _first(data, "UID")
    _, summary = _first(data, "SUMMARY")
    _, desc = _first(data, "DESCRIPTION")
    _, loc = _first(data, "LOCATION")
    _, rrule = _first(data, "RRULE")
    _, status = _first(data, "STATUS")

    return Event(
        calendar_id=calendar_id,
        uid=sanitize_text(_unescape(uid), 512) or f"{uuid.uuid4()}@pycalendar",
        title=sanitize_title(_unescape(summary)) or "(ohne Titel)",
        description=sanitize_text(_unescape(desc)),
        location=sanitize_text(_unescape(loc), 500),
        start=start, end=end, all_day=all_day,
        rrule=sanitize_text(rrule, 512).upper(),
        exdates=exdates, deleted=status.upper() == "CANCELLED",
        dirty=False, source=source)


def _safe_add(start: datetime, delta: timedelta) -> datetime:
    """Addiert, ohne dass ein präparierter Wert einen OverflowError auslöst."""
    try:
        return start + delta
    except (OverflowError, OSError, ValueError):
        return start + timedelta(hours=1)


def _parse_duration(value: str) -> timedelta:
    m = re.fullmatch(r"[+-]?P(?:(\d+)W)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?"
                     r"(?:(\d+)S)?)?", (value or "").strip().upper())
    if not m:
        return timedelta(hours=1)
    w, d, h, mi, s = (min(int(g or 0), 10 ** 6) for g in m.groups())
    try:
        duration = timedelta(weeks=w, days=d, hours=h, minutes=mi, seconds=s)
    except OverflowError:
        return timedelta(hours=1)
    # Obergrenze: kein Termin dauert laenger als ein Jahr
    return min(duration, MAX_EVENT_DURATION)


def build_ics(events: Iterable[Event], prodid: str = "-//PyCalendar//DE") -> str:
    out = ["BEGIN:VCALENDAR", "VERSION:2.0", f"PRODID:{prodid}",
           "CALSCALE:GREGORIAN"]
    for ev in events:
        out.append("BEGIN:VEVENT")
        out.append(f"UID:{_escape(ev.uid)}")
        out.append(f"DTSTAMP:{format_dt(utcnow())}")
        if ev.all_day:
            out.append(f"DTSTART;VALUE=DATE:{format_dt(ev.start, True)}")
            end = ev.end or (ensure_aware(ev.start) + timedelta(days=1))
            out.append(f"DTEND;VALUE=DATE:{format_dt(end, True)}")
        else:
            out.append(f"DTSTART:{format_dt(ev.start)}")
            if ev.end:
                out.append(f"DTEND:{format_dt(ev.end)}")
        out.append(f"SUMMARY:{_escape(ev.title)}")
        if ev.description:
            out.append(f"DESCRIPTION:{_escape(ev.description)}")
        if ev.location:
            out.append(f"LOCATION:{_escape(ev.location)}")
        if ev.rrule:
            out.append(f"RRULE:{ev.rrule}")
        for ex in ev.exdates[:200]:
            out.append(f"EXDATE:{format_dt(ex)}")
        if ev.reminder_minutes is not None:
            out += ["BEGIN:VALARM", "ACTION:DISPLAY",
                    f"TRIGGER:-PT{int(ev.reminder_minutes)}M",
                    f"DESCRIPTION:{_escape(ev.title)}", "END:VALARM"]
        if ev.deleted:
            out.append("STATUS:CANCELLED")
        out.append("END:VEVENT")
    out.append("END:VCALENDAR")
    return "\r\n".join(_fold(line) for line in out) + "\r\n"


def _fold(line: str) -> str:
    if len(line) <= 73:
        return line
    chunks = [line[:73]]
    rest = line[73:]
    while rest:
        chunks.append(" " + rest[:72])
        rest = rest[72:]
    return "\r\n".join(chunks)
