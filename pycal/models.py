"""Datenmodelle des Kalenders.

Bewusst frei von Kivy-Abhängigkeiten, damit die Logik auch ohne UI
(z.B. in Tests oder auf dem Desktop) laeuft.
"""
from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, date, time, timedelta, timezone
from typing import Iterator, Optional

# --------------------------------------------------------------------------
# Hilfsfunktionen für Zeit
# --------------------------------------------------------------------------

def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def ensure_aware(dt: datetime, tz=timezone.utc) -> datetime:
    """Naive datetimes werden als tz interpretiert."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=tz)
    return dt


def to_iso(dt: Optional[datetime]) -> Optional[str]:
    return None if dt is None else ensure_aware(dt).isoformat()


def from_iso(value) -> Optional[datetime]:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return ensure_aware(value)
    return ensure_aware(datetime.fromisoformat(str(value)))


# --------------------------------------------------------------------------
# Wiederholungsregel (vereinfachtes RRULE, iCalendar-kompatibel)
# --------------------------------------------------------------------------

WEEKDAYS = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"]


@dataclass
class Recurrence:
    """Teilmenge von RFC 5545 RRULE, die für Alltagskalender genügt."""

    freq: str = "WEEKLY"          # DAILY | WEEKLY | MONTHLY | YEARLY
    interval: int = 1
    byday: list[str] = field(default_factory=list)   # ["MO", "WE"]
    bymonthday: list[int] = field(default_factory=list)
    count: Optional[int] = None
    until: Optional[datetime] = None

    # ---------------- Serialisierung ----------------
    def to_rrule(self) -> str:
        parts = [f"FREQ={self.freq}"]
        if self.interval and self.interval != 1:
            parts.append(f"INTERVAL={self.interval}")
        if self.byday:
            parts.append("BYDAY=" + ",".join(self.byday))
        if self.bymonthday:
            parts.append("BYMONTHDAY=" + ",".join(str(d) for d in self.bymonthday))
        if self.count:
            parts.append(f"COUNT={self.count}")
        if self.until:
            parts.append("UNTIL=" + ensure_aware(self.until)
                         .astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
        return ";".join(parts)

    @classmethod
    def from_rrule(cls, rule: Optional[str]) -> Optional["Recurrence"]:
        if not rule:
            return None
        rule = rule.strip()
        if rule.upper().startswith("RRULE:"):
            rule = rule[6:]
        data: dict[str, str] = {}
        for chunk in rule.split(";"):
            if "=" in chunk:
                k, v = chunk.split("=", 1)
                data[k.strip().upper()] = v.strip()
        if "FREQ" not in data:
            return None
        until = None
        if data.get("UNTIL"):
            raw = data["UNTIL"].replace("Z", "")
            fmt = "%Y%m%dT%H%M%S" if "T" in raw else "%Y%m%d"
            until = datetime.strptime(raw, fmt).replace(tzinfo=timezone.utc)
        return cls(
            freq=data["FREQ"].upper(),
            interval=int(data.get("INTERVAL", 1) or 1),
            byday=[d for d in data.get("BYDAY", "").split(",") if d],
            bymonthday=[int(d) for d in data.get("BYMONTHDAY", "").split(",") if d],
            count=int(data["COUNT"]) if data.get("COUNT") else None,
            until=until,
        )

    def describe(self) -> str:
        """Menschenlesbare deutsche Beschreibung für die UI."""
        names = {"MO": "Mo", "TU": "Di", "WE": "Mi", "TH": "Do",
                 "FR": "Fr", "SA": "Sa", "SU": "So"}
        iv = self.interval or 1
        if self.freq == "DAILY":
            base = "täglich" if iv == 1 else f"alle {iv} Tage"
        elif self.freq == "WEEKLY":
            base = "wöchentlich" if iv == 1 else f"alle {iv} Wochen"
            if self.byday:
                base += " (" + ", ".join(names.get(d, d) for d in self.byday) + ")"
        elif self.freq == "MONTHLY":
            base = "monatlich" if iv == 1 else f"alle {iv} Monate"
            if self.bymonthday:
                base += " am " + ", ".join(f"{d}." for d in self.bymonthday)
        else:
            base = "jährlich" if iv == 1 else f"alle {iv} Jahre"
        if self.count:
            base += f", {self.count}x"
        elif self.until:
            base += f", bis {self.until.date().isoformat()}"
        return base

    # ---------------- Expansion ----------------
    def occurrences(self, start: datetime, window_start: datetime,
                    window_end: datetime, limit: int = 2000) -> Iterator[datetime]:
        """Liefert Startzeitpunkte im Fenster [window_start, window_end)."""
        start = ensure_aware(start)
        window_start = ensure_aware(window_start)
        window_end = ensure_aware(window_end)
        iv = max(1, self.interval or 1)
        emitted = 0
        produced = 0

        def stop(dt: datetime) -> bool:
            if self.until and dt > ensure_aware(self.until):
                return True
            if self.count is not None and produced >= self.count:
                return True
            return dt >= window_end

        if self.freq == "WEEKLY" and self.byday:
            targets = sorted({WEEKDAYS.index(d) for d in self.byday if d in WEEKDAYS})
            week0 = start - timedelta(days=start.weekday())
            week = week0
            guard = 0
            while guard < 5000:
                guard += 1
                if self.until and week > ensure_aware(self.until) + timedelta(days=7):
                    break
                if week > window_end:
                    break
                weeks_since = round((week - week0).days / 7)
                if weeks_since % iv == 0:
                    for wd in targets:
                        cand = week + timedelta(days=wd)
                        cand = cand.replace(hour=start.hour, minute=start.minute,
                                            second=start.second)
                        if cand < start:
                            continue
                        if self.until and cand > ensure_aware(self.until):
                            return
                        if self.count is not None and produced >= self.count:
                            return
                        produced += 1
                        if window_start <= cand < window_end:
                            emitted += 1
                            yield cand
                            if emitted >= limit:
                                return
                week += timedelta(days=7)
            return

        cur = start
        guard = 0
        while guard < 20000:
            guard += 1
            if stop(cur):
                return
            produced += 1
            if window_start <= cur < window_end:
                emitted += 1
                yield cur
                if emitted >= limit:
                    return
            if self.freq == "DAILY":
                cur = cur + timedelta(days=iv)
            elif self.freq == "WEEKLY":
                cur = cur + timedelta(weeks=iv)
            elif self.freq == "MONTHLY":
                cur = _add_months(cur, iv)
            elif self.freq == "YEARLY":
                cur = _add_months(cur, 12 * iv)
            else:
                return


def _add_months(dt: datetime, months: int) -> datetime:
    month = dt.month - 1 + months
    year = dt.year + month // 12
    month = month % 12 + 1
    day = min(dt.day, _days_in_month(year, month))
    return dt.replace(year=year, month=month, day=day)


def _days_in_month(year: int, month: int) -> int:
    if month == 12:
        return 31
    return (date(year, month + 1, 1) - date(year, month, 1)).days


# --------------------------------------------------------------------------
# Kalender & Ereignis
# --------------------------------------------------------------------------

@dataclass
class Calendar:
    id: Optional[int] = None
    name: str = "Persönlich"
    color: str = "#4C8DF6"
    visible: bool = True
    account_id: Optional[int] = None      # None = rein lokal
    remote_url: str = ""
    read_only: bool = False
    sync_token: str = ""


@dataclass
class Event:
    id: Optional[int] = None
    calendar_id: int = 1
    uid: str = field(default_factory=lambda: f"{uuid.uuid4()}@pycalendar")
    title: str = ""
    description: str = ""
    location: str = ""
    start: datetime = field(default_factory=utcnow)
    end: Optional[datetime] = None
    all_day: bool = False
    color: str = ""                        # leer = Kalenderfarbe
    rrule: str = ""
    exdates: list[datetime] = field(default_factory=list)
    reminder_minutes: Optional[int] = None
    # Synchronisation
    etag: str = ""
    remote_href: str = ""
    dirty: bool = True                     # muss hochgeladen werden
    deleted: bool = False
    # Herkunft: erlaubt stapelweises Löschen importierter Einträge
    import_batch_id: Optional[int] = None
    source: str = "local"                  # local | csv | caldav | ics
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)

    # ---------------- abgeleitete Werte ----------------
    @property
    def duration(self) -> timedelta:
        if self.end is None:
            return timedelta(hours=1)
        return ensure_aware(self.end) - ensure_aware(self.start)

    @property
    def duration_minutes(self) -> int:
        return max(0, int(self.duration.total_seconds() // 60))

    @property
    def recurrence(self) -> Optional[Recurrence]:
        return Recurrence.from_rrule(self.rrule)

    @property
    def is_recurring(self) -> bool:
        return bool(self.rrule)

    def normalized_title(self) -> str:
        return normalize_title(self.title)

    def occurrences(self, window_start: datetime, window_end: datetime,
                    limit: int = 2000) -> list[datetime]:
        """Alle Starts im Fenster, Ausnahmetermine (EXDATE) beruecksichtigt."""
        ws, we = ensure_aware(window_start), ensure_aware(window_end)
        rec = self.recurrence
        if rec is None:
            s = ensure_aware(self.start)
            e = ensure_aware(self.end) if self.end else s + timedelta(hours=1)
            return [s] if s < we and e > ws else []
        ex = {ensure_aware(d).replace(second=0, microsecond=0) for d in self.exdates}
        out = []
        for occ in rec.occurrences(self.start, ws - self.duration, we, limit=limit):
            if occ.replace(second=0, microsecond=0) in ex:
                continue
            out.append(occ)
        return out

    def to_dict(self) -> dict:
        d = asdict(self)
        d["start"] = to_iso(self.start)
        d["end"] = to_iso(self.end)
        d["exdates"] = [to_iso(x) for x in self.exdates]
        d["created_at"] = to_iso(self.created_at)
        d["updated_at"] = to_iso(self.updated_at)
        return d


# --------------------------------------------------------------------------
# Titel-Normalisierung für die Mustererkennung
# --------------------------------------------------------------------------

_NOISE = re.compile(r"[^0-9a-zA-Z\u00c0-\u024f]+")
_NUMBERS = re.compile(r"\b\d+\b")


def normalize_title(title: str) -> str:
    """Vereinheitlicht Titel, damit "Sport 18:00" und "sport" zusammenfallen.

    - klein geschrieben
    - Uhrzeiten und freistehende Zahlen entfernt
    - Sonderzeichen zu einfachen Leerzeichen
    """
    t = (title or "").strip().lower()
    t = re.sub(r"\b\d{1,2}[:.]\d{2}\b", " ", t)      # Uhrzeiten
    t = re.sub(r"\b\d{1,2}\.\d{1,2}\.(\d{2,4})?\b", " ", t)  # Datumsangaben
    t = _NUMBERS.sub(" ", t)
    t = _NOISE.sub(" ", t)
    return " ".join(t.split())
