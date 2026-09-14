"""Mustererkennung für wiederkehrende Ereignisse.

Kernidee: Die App merkt sich zu jedem (normalisierten) Ereignisnamen
statistische Merkmale - vor allem die *Dauer*, ausserdem typische
Startzeit, Wochentage, Ort, Farbe und Erinnerung. Beim nächsten Anlegen
eines Termins mit demselben Namen werden diese Werte automatisch
vorgeschlagen.

Zusätzlich erkennt `suggest_recurrence`, ob ein Name in einem festen
Rhythmus auftritt (z.B. immer dienstags), und schlaegt eine RRULE vor.

Robustheit: Es wird der Median statt des Mittelwerts verwendet, damit ein
einzelner Ausreißer ("Meeting ging 6 Stunden") den Vorschlag nicht kippt.
Pro Name werden höchstens MAX_SAMPLES Werte gespeichert (gleitendes
Fenster) - das begrenzt zugleich den Speicherverbrauch.
"""
from __future__ import annotations

import statistics
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

from .models import Event, Recurrence, WEEKDAYS, ensure_aware, normalize_title, to_iso
from .security import clamp, sanitize_text, sanitize_title

MAX_SAMPLES = 40
MIN_SAMPLES_FOR_SUGGESTION = 2
MIN_SAMPLES_FOR_RECURRENCE = 3
MAX_DURATION_MINUTES = 60 * 24 * 14      # 14 Tage Obergrenze


@dataclass
class PatternSuggestion:
    """Was die App beim Tippen eines Titels vorschlaegt."""
    title: str = ""
    duration_minutes: Optional[int] = None
    start_minutes: Optional[int] = None       # Minuten ab Mitternacht
    weekdays: list[int] = field(default_factory=list)
    location: str = ""
    color: str = ""
    reminder_minutes: Optional[int] = None
    samples: int = 0
    confidence: float = 0.0                   # 0..1
    pinned: bool = False
    rrule: str = ""

    @property
    def duration(self) -> Optional[timedelta]:
        if self.duration_minutes is None:
            return None
        return timedelta(minutes=self.duration_minutes)

    def describe(self) -> str:
        if self.duration_minutes is None:
            return ""
        h, m = divmod(self.duration_minutes, 60)
        dur = f"{h}h {m:02d}min" if h else f"{m} min"
        txt = f"{dur} (aus {self.samples} früheren Terminen)"
        if self.pinned:
            txt = f"{dur} (fest hinterlegt)"
        return txt


def _parse_list(raw: str) -> list[int]:
    out = []
    for chunk in (raw or "").split(","):
        chunk = chunk.strip()
        if chunk.lstrip("-").isdigit():
            out.append(int(chunk))
    return out


def _join(values: list[int]) -> str:
    return ",".join(str(int(v)) for v in values[-MAX_SAMPLES:])


class PatternStore:
    """Lernt und liefert Muster; arbeitet direkt auf der Datenbank."""

    def __init__(self, db):
        self.db = db

    # ------------------------------------------------------------------
    # Lernen
    # ------------------------------------------------------------------
    def learn(self, event: Event) -> None:
        """Nimmt ein gespeichertes Ereignis in die Statistik auf."""
        key = normalize_title(event.title)
        if not key:
            return
        minutes = clamp(event.duration_minutes, 0, MAX_DURATION_MINUTES)
        if event.all_day:
            minutes = 24 * 60
        start = ensure_aware(event.start)
        start_min = start.hour * 60 + start.minute
        weekday = start.weekday()

        row = self.db.con.execute(
            "SELECT * FROM patterns WHERE title_key=?", (key,)).fetchone()
        if row:
            durations = _parse_list(row["durations"]) + [minutes]
            starts = _parse_list(row["start_minutes"]) + [start_min]
            wdays = _parse_list(row["weekdays"]) + [weekday]
            samples = int(row["samples"]) + 1
            pinned = row["pinned_duration"]
        else:
            durations, starts, wdays, samples, pinned = [minutes], [start_min], [weekday], 1, None

        with self.db.connection() as con:
            con.execute(
                "INSERT OR REPLACE INTO patterns(title_key,display_title,samples,"
                "durations,start_minutes,weekdays,last_seen,location,color,"
                "reminder_minutes,pinned_duration) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (key, sanitize_title(event.title), samples, _join(durations),
                 _join(starts), _join(wdays), to_iso(start),
                 sanitize_text(event.location, 500), event.color or "",
                 event.reminder_minutes, pinned))

    def forget(self, title: str) -> None:
        key = normalize_title(title)
        with self.db.connection() as con:
            con.execute("DELETE FROM patterns WHERE title_key=?", (key,))

    def pin_duration(self, title: str, minutes: Optional[int]) -> None:
        """Nutzer fixiert eine Dauer - sie ueberschreibt die Statistik."""
        key = normalize_title(title)
        value = None if minutes is None else clamp(minutes, 0, MAX_DURATION_MINUTES)
        with self.db.connection() as con:
            exists = con.execute("SELECT 1 FROM patterns WHERE title_key=?",
                                 (key,)).fetchone()
            if exists:
                con.execute("UPDATE patterns SET pinned_duration=? WHERE title_key=?",
                            (value, key))
            else:
                con.execute(
                    "INSERT INTO patterns(title_key,display_title,samples,"
                    "pinned_duration) VALUES(?,?,0,?)",
                    (key, sanitize_title(title), value))

    def rebuild(self) -> int:
        """Baut alle Muster aus den vorhandenen Ereignissen neu auf."""
        pinned = {r["title_key"]: r["pinned_duration"]
                  for r in self.db.con.execute(
                      "SELECT title_key,pinned_duration FROM patterns "
                      "WHERE pinned_duration IS NOT NULL")}
        with self.db.connection() as con:
            con.execute("DELETE FROM patterns")
        rows = self.db.con.execute(
            "SELECT * FROM events WHERE deleted=0 ORDER BY start")
        count = 0
        for r in rows:
            self.learn(self.db._row_to_event(r))
            count += 1
        for key, value in pinned.items():
            with self.db.connection() as con:
                con.execute("UPDATE patterns SET pinned_duration=? WHERE title_key=?",
                            (value, key))
        return count

    # ------------------------------------------------------------------
    # Abfragen
    # ------------------------------------------------------------------
    def suggest(self, title: str) -> Optional[PatternSuggestion]:
        key = normalize_title(title)
        if not key:
            return None
        row = self.db.con.execute(
            "SELECT * FROM patterns WHERE title_key=?", (key,)).fetchone()
        if row is None:
            return self._suggest_fuzzy(key)
        return self._row_to_suggestion(row)

    def _suggest_fuzzy(self, key: str) -> Optional[PatternSuggestion]:
        """Teiltreffer: "sport" findet auch "sport verein"."""
        if len(key) < 3:
            return None
        esc = key.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        row = self.db.con.execute(
            "SELECT * FROM patterns WHERE title_key LIKE ? ESCAPE '\\' "
            "ORDER BY samples DESC LIMIT 1", (f"{esc}%",)).fetchone()
        if row is None:
            return None
        sug = self._row_to_suggestion(row)
        if sug:
            sug.confidence *= 0.7          # Teiltreffer sind unsicherer
        return sug

    def _row_to_suggestion(self, row) -> Optional[PatternSuggestion]:
        durations = _parse_list(row["durations"])
        starts = _parse_list(row["start_minutes"])
        wdays = _parse_list(row["weekdays"])
        samples = int(row["samples"] or 0)
        pinned = row["pinned_duration"]

        if pinned is not None:
            duration = int(pinned)
        elif durations:
            duration = int(statistics.median(_trim_outliers(durations)))
        else:
            duration = None

        if duration is None and not starts:
            return None

        start_minutes = int(statistics.median(starts)) if starts else None
        wd_counter = Counter(wdays)
        top = [wd for wd, c in wd_counter.most_common()
               if samples and c / max(1, len(wdays)) >= 0.2]

        return PatternSuggestion(
            title=row["display_title"], duration_minutes=duration,
            start_minutes=start_minutes, weekdays=sorted(top),
            location=row["location"] or "", color=row["color"] or "",
            reminder_minutes=row["reminder_minutes"], samples=samples,
            confidence=_confidence(durations, samples, pinned is not None),
            pinned=pinned is not None)

    def top_titles(self, prefix: str = "", limit: int = 10) -> list[dict]:
        """Autovervollstaendigung im Titelfeld."""
        prefix = normalize_title(prefix)
        if prefix:
            esc = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            rows = self.db.con.execute(
                "SELECT * FROM patterns WHERE title_key LIKE ? ESCAPE '\\' "
                "ORDER BY samples DESC, last_seen DESC LIMIT ?",
                (f"%{esc}%", int(limit)))
        else:
            rows = self.db.con.execute(
                "SELECT * FROM patterns ORDER BY samples DESC, last_seen DESC "
                "LIMIT ?", (int(limit),))
        out = []
        for r in rows:
            s = self._row_to_suggestion(r)
            if s:
                out.append({"title": r["display_title"], "samples": r["samples"],
                            "duration_minutes": s.duration_minutes,
                            "summary": s.describe()})
        return out

    def all_patterns(self) -> list[dict]:
        out = []
        for r in self.db.con.execute("SELECT * FROM patterns ORDER BY samples DESC"):
            s = self._row_to_suggestion(r)
            if not s:
                continue
            out.append({
                "title_key": r["title_key"], "title": r["display_title"],
                "samples": r["samples"], "duration_minutes": s.duration_minutes,
                "start_minutes": s.start_minutes, "weekdays": s.weekdays,
                "pinned": s.pinned, "confidence": round(s.confidence, 2),
                "location": s.location})
        return out

    # ------------------------------------------------------------------
    # Serienerkennung
    # ------------------------------------------------------------------
    def suggest_recurrence(self, title: str,
                           min_samples: int = MIN_SAMPLES_FOR_RECURRENCE
                           ) -> Optional[Recurrence]:
        """Erkennt taegliche/woechentliche/monatliche Rhythmen im Verlauf."""
        key = normalize_title(title)
        if not key:
            return None
        rows = self.db.con.execute(
            "SELECT start FROM events WHERE title_key=? AND deleted=0 AND rrule='' "
            "ORDER BY start DESC LIMIT 30", (key,)).fetchall()
        dates = sorted({datetime.fromisoformat(r["start"]) for r in rows})
        if len(dates) < min_samples:
            return None
        gaps = [round((b - a).total_seconds() / 86400) for a, b in zip(dates, dates[1:])]
        if not gaps:
            return None
        common, freq = Counter(gaps).most_common(1)[0]
        ratio = freq / len(gaps)
        if ratio < 0.6:
            return None
        if common == 1:
            return Recurrence(freq="DAILY", interval=1)
        if common % 7 == 0 and common <= 28:
            wd = Counter(d.weekday() for d in dates).most_common(1)[0][0]
            return Recurrence(freq="WEEKLY", interval=common // 7,
                              byday=[WEEKDAYS[wd]])
        if 28 <= common <= 31:
            return Recurrence(freq="MONTHLY", interval=1,
                              bymonthday=[dates[-1].day])
        if 364 <= common <= 366:
            return Recurrence(freq="YEARLY", interval=1)
        return None

    # ------------------------------------------------------------------
    def apply(self, event: Event, suggestion: Optional[PatternSuggestion] = None,
              fields: Optional[set[str]] = None) -> Event:
        """Füllt leere Felder eines Ereignisses aus dem Muster."""
        sug = suggestion or self.suggest(event.title)
        if sug is None:
            return event
        fields = fields or {"duration", "location", "color", "reminder"}
        if "duration" in fields and sug.duration_minutes and event.end is None:
            event.end = ensure_aware(event.start) + timedelta(minutes=sug.duration_minutes)
        if "location" in fields and sug.location and not event.location:
            event.location = sug.location
        if "color" in fields and sug.color and not event.color:
            event.color = sug.color
        if "reminder" in fields and sug.reminder_minutes is not None \
                and event.reminder_minutes is None:
            event.reminder_minutes = sug.reminder_minutes
        return event


def _trim_outliers(values: list[int]) -> list[int]:
    """Entfernt extreme Werte (10./90. Perzentil), wenn genug Daten da sind."""
    if len(values) < 5:
        return values
    ordered = sorted(values)
    k = max(1, len(ordered) // 10)
    trimmed = ordered[k:-k] or ordered
    return trimmed


def _confidence(durations: list[int], samples: int, pinned: bool) -> float:
    if pinned:
        return 1.0
    if not durations or samples < MIN_SAMPLES_FOR_SUGGESTION:
        return 0.3 if durations else 0.0
    med = statistics.median(durations)
    if med <= 0:
        return 0.4
    spread = statistics.pstdev(durations) / med if len(durations) > 1 else 0
    volume = min(1.0, samples / 8)
    stability = max(0.0, 1.0 - min(1.0, spread))
    return round(0.35 * volume + 0.65 * stability, 3)
