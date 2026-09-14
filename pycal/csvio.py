"""CSV-Import und -Export.

Schwerpunkt laut Anforderung: **Das Löschen importierter Einträge muss
leicht sein.** Deshalb bekommt jeder Import eine Stapel-ID
(`import_batch_id`). Über diese ID kann der komplette Import mit einem
Fingertipp rückgängig gemacht werden ("Import rückgängig"), ohne dass
handgepflegte Termine betroffen sind. Zusätzlich gibt es:

* eine Vorschau vor dem Import (Dry-Run) inkl. Fehlerliste
* Duplikatsprüfung (gleicher Titel + Startzeit)
* selektives Löschen einzelner Zeilen eines Stapels
* Löschen nach Titel/Zeitraum über `delete_matching`

Sicherheitsmaßnahmen:
* Größen- und Zeilenlimit (DoS-Schutz)
* Sanitisierung jedes Feldes (Steuerzeichen, Länge, Unicode)
* Schutz vor CSV-Formel-Injection beim Export
* Pfadpruefung beim Schreiben von Exportdateien
"""
from __future__ import annotations

import csv
import io
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Optional

from .models import Event, ensure_aware, normalize_title, utcnow
from .security import (MAX_CSV_BYTES, MAX_CSV_ROWS, SecurityError, sanitize_csv_cell,
                       sanitize_text, sanitize_title, safe_output_path)

# Grosszuegige Spaltenerkennung für deutsche und englische Exporte
COLUMN_ALIASES = {
    "title": ["titel", "title", "subject", "betreff", "summary", "name",
              "ereignis", "termin", "event"],
    "start": ["start", "startdatum", "startzeit", "beginn", "start date",
              "start time", "von", "datum", "date", "start_date"],
    "end": ["ende", "end", "enddatum", "endzeit", "end date", "end time",
            "bis", "end_date"],
    "duration": ["dauer", "duration", "laenge", "länge", "minuten", "minutes"],
    "all_day": ["ganztägig", "ganztägig", "all day event", "all_day", "allday"],
    "description": ["beschreibung", "description", "notiz", "notes", "body"],
    "location": ["ort", "location", "raum", "where"],
    "rrule": ["rrule", "wiederholung", "recurrence", "serie"],
    "reminder": ["erinnerung", "reminder", "alarm"],
}

DATE_FORMATS = [
    "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M",
    "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d",
    "%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M", "%d.%m.%Y",
    "%d/%m/%Y %H:%M", "%d/%m/%Y", "%m/%d/%Y %H:%M:%S", "%m/%d/%Y %H:%M",
    "%m/%d/%Y", "%Y%m%dT%H%M%SZ", "%Y%m%d",
]

TRUE_VALUES = {"1", "true", "wahr", "ja", "yes", "y", "x", "on"}


@dataclass
class CsvRow:
    index: int
    event: Optional[Event] = None
    error: str = ""
    duplicate_of: Optional[int] = None
    raw: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.event is not None and not self.error


@dataclass
class ImportPreview:
    filename: str
    delimiter: str
    columns: list[str] = field(default_factory=list)
    mapping: dict[str, str] = field(default_factory=dict)
    rows: list[CsvRow] = field(default_factory=list)
    truncated: bool = False

    @property
    def valid(self) -> list[CsvRow]:
        return [r for r in self.rows if r.ok and r.duplicate_of is None]

    @property
    def duplicates(self) -> list[CsvRow]:
        return [r for r in self.rows if r.ok and r.duplicate_of is not None]

    @property
    def errors(self) -> list[CsvRow]:
        return [r for r in self.rows if r.error]

    def summary(self) -> str:
        return (f"{len(self.valid)} neu, {len(self.duplicates)} Duplikate, "
                f"{len(self.errors)} fehlerhaft")


# --------------------------------------------------------------------------
def parse_datetime(value: str, dayfirst: bool = True) -> Optional[datetime]:
    text = sanitize_text(value, 64)
    if not text:
        return None
    text = text.replace("Z", "+0000") if text.endswith("Z") else text
    formats = DATE_FORMATS if dayfirst else \
        [f.replace("%d.%m.", "%m.%d.") for f in DATE_FORMATS]
    for fmt in formats:
        try:
            dt = datetime.strptime(text, fmt)
            return ensure_aware(dt)
        except ValueError:
            continue
    try:
        return ensure_aware(datetime.fromisoformat(text))
    except ValueError:
        return None


def parse_duration(value: str) -> Optional[int]:
    """Akzeptiert "90", "1:30", "1h30", "90min"."""
    text = sanitize_text(value, 32).lower().strip()
    if not text:
        return None
    m = re.fullmatch(r"(\d{1,3}):(\d{2})", text)
    if m:
        return int(m.group(1)) * 60 + int(m.group(2))
    m = re.fullmatch(r"(?:(\d+)\s*h)?\s*(?:(\d+)\s*(?:m|min)?)?", text)
    if m and (m.group(1) or m.group(2)):
        return int(m.group(1) or 0) * 60 + int(m.group(2) or 0)
    if text.isdigit():
        return int(text)
    return None


def detect_mapping(columns: list[str]) -> dict[str, str]:
    mapping: dict[str, str] = {}
    lowered = {c: sanitize_text(c, 100).strip().lower().lstrip("﻿") for c in columns}
    for field_name, aliases in COLUMN_ALIASES.items():
        for col, low in lowered.items():
            if low in aliases and field_name not in mapping:
                mapping[field_name] = col
                break
        if field_name not in mapping:
            for col, low in lowered.items():
                if any(a in low for a in aliases) and col not in mapping.values():
                    mapping[field_name] = col
                    break
    return mapping


def sniff_delimiter(sample: str) -> str:
    try:
        return csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
    except csv.Error:
        counts = {d: sample.count(d) for d in ",;\t|"}
        return max(counts, key=counts.get) if any(counts.values()) else ","


def read_text(path, max_bytes: int = MAX_CSV_BYTES) -> str:
    p = Path(path)
    if not p.is_file():
        raise SecurityError(f"Datei nicht gefunden: {p.name}")
    size = p.stat().st_size
    if size > max_bytes:
        raise SecurityError(
            f"Datei ist zu groß ({size // 1024 // 1024} MB, erlaubt sind "
            f"{max_bytes // 1024 // 1024} MB).")
    raw = p.read_bytes()[:max_bytes]
    for encoding in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


# --------------------------------------------------------------------------
class CsvImporter:
    def __init__(self, db, patterns=None, default_duration: int = 60):
        self.db = db
        self.patterns = patterns
        self.default_duration = default_duration

    # ---------------- Vorschau ----------------
    def preview(self, path, calendar_id: int = 1,
                mapping: Optional[dict[str, str]] = None,
                dayfirst: bool = True, max_rows: int = MAX_CSV_ROWS,
                text: Optional[str] = None,
                filename: Optional[str] = None) -> ImportPreview:
        content = text if text is not None else read_text(path)
        name = filename or (Path(path).name if path else "eingabe.csv")
        delimiter = sniff_delimiter(content[:8192])
        reader = csv.DictReader(io.StringIO(content), delimiter=delimiter)
        columns = [c for c in (reader.fieldnames or []) if c is not None]
        if not columns:
            raise SecurityError("CSV ohne Kopfzeile - Import nicht möglich.")
        mapping = mapping or detect_mapping(columns)
        if "title" not in mapping or "start" not in mapping:
            raise SecurityError(
                "Es konnten keine Spalten für Titel und Start erkannt werden. "
                "Bitte die Zuordnung manuell setzen.")

        preview = ImportPreview(filename=name, delimiter=delimiter,
                                columns=columns, mapping=mapping)
        existing = self._existing_index(calendar_id)
        seen_in_file: set[tuple[str, str]] = set()

        for i, raw in enumerate(reader, start=2):   # Zeile 1 = Kopfzeile
            if len(preview.rows) >= max_rows:
                preview.truncated = True
                break
            row = CsvRow(index=i, raw={k: sanitize_text(v, 500)
                                       for k, v in list(raw.items())[:40]
                                       if k is not None})
            try:
                ev = self._build_event(raw, mapping, calendar_id, dayfirst)
            except ValueError as exc:
                row.error = str(exc)
                preview.rows.append(row)
                continue
            row.event = ev
            key = (normalize_title(ev.title), ensure_aware(ev.start).isoformat())
            if key in existing:
                row.duplicate_of = existing[key]
            elif key in seen_in_file:
                row.duplicate_of = -1           # Duplikat innerhalb der Datei
            seen_in_file.add(key)
            preview.rows.append(row)
        return preview

    def _existing_index(self, calendar_id: int) -> dict[tuple[str, str], int]:
        rows = self.db.con.execute(
            "SELECT id,title_key,start FROM events WHERE calendar_id=? AND deleted=0",
            (int(calendar_id),))
        return {(r["title_key"], r["start"]): r["id"] for r in rows}

    def _build_event(self, raw: dict, mapping: dict[str, str],
                     calendar_id: int, dayfirst: bool) -> Event:
        def get(field_name: str) -> str:
            col = mapping.get(field_name)
            return sanitize_text(raw.get(col, "")) if col else ""

        title = sanitize_title(get("title"))
        if not title:
            raise ValueError("Titel fehlt")
        start = parse_datetime(get("start"), dayfirst)
        if start is None:
            raise ValueError(f"Startdatum nicht lesbar: {get('start')!r}")

        all_day = get("all_day").strip().lower() in TRUE_VALUES
        end = parse_datetime(get("end"), dayfirst)
        if end is None:
            minutes = parse_duration(get("duration"))
            if minutes is None and self.patterns is not None:
                sug = self.patterns.suggest(title)
                if sug and sug.duration_minutes:
                    minutes = sug.duration_minutes
            if minutes is None:
                minutes = 24 * 60 if all_day else self.default_duration
            end = start + timedelta(minutes=minutes)
        if end < start:
            end = start + timedelta(minutes=self.default_duration)
        if (end - start) > timedelta(days=366):
            raise ValueError("Zeitraum unplausibel (> 1 Jahr)")

        reminder = parse_duration(get("reminder"))
        return Event(
            calendar_id=calendar_id, title=title, start=start, end=end,
            all_day=all_day, description=get("description"),
            location=sanitize_text(get("location"), 500),
            rrule=_safe_rrule(get("rrule")), reminder_minutes=reminder,
            source="csv")

    # ---------------- Ausfuehren ----------------
    def commit(self, preview: ImportPreview, calendar_id: int = 1,
               include_duplicates: bool = False,
               selected_rows: Optional[set[int]] = None,
               learn_patterns: bool = True) -> dict:
        rows = list(preview.valid)
        if include_duplicates:
            rows += preview.duplicates
        if selected_rows is not None:
            rows = [r for r in rows if r.index in selected_rows]

        batch_id = self.db.create_batch(preview.filename, calendar_id,
                                        note=f"Trennzeichen '{preview.delimiter}'")
        imported = 0
        for row in rows:
            ev = row.event
            ev.calendar_id = calendar_id
            ev.import_batch_id = batch_id
            ev.source = "csv"
            self.db.save_event(ev)
            if learn_patterns and self.patterns is not None:
                self.patterns.learn(ev)
            imported += 1
        skipped = len(preview.rows) - imported
        self.db.finish_batch(batch_id, len(preview.rows), imported, skipped)
        return {"batch_id": batch_id, "imported": imported, "skipped": skipped,
                "errors": len(preview.errors), "filename": preview.filename}


def _safe_rrule(value: str) -> str:
    """Laesst nur bekannte RRULE-Bestandteile durch."""
    text = sanitize_text(value, 512).upper().replace("RRULE:", "").strip()
    if not text or "FREQ=" not in text:
        return ""
    allowed = {"FREQ", "INTERVAL", "BYDAY", "BYMONTHDAY", "COUNT", "UNTIL",
               "WKST", "BYMONTH"}
    parts = []
    for chunk in text.split(";"):
        if "=" not in chunk:
            continue
        key, val = chunk.split("=", 1)
        if key.strip() in allowed and re.fullmatch(r"[A-Z0-9,\-+:]{1,60}", val.strip()):
            parts.append(f"{key.strip()}={val.strip()}")
    return ";".join(parts)


# --------------------------------------------------------------------------
class ImportManager:
    """Verwaltet Importstapel - das Herzstück für einfaches Löschen."""

    def __init__(self, db):
        self.db = db

    def batches(self) -> list[dict]:
        out = []
        for b in self.db.list_batches():
            created = b["created_at"][:16].replace("T", " ")
            out.append({**b, "label": f"{b['filename']} - {created}",
                        "deletable": b["live_events"] > 0})
        return out

    def preview_batch(self, batch_id: int, limit: int = 200) -> list[Event]:
        return self.db.batch_events(batch_id)[:limit]

    def undo(self, batch_id: int, hard: bool = False) -> int:
        """Loescht alle Ereignisse eines Imports (ein Tipp in der UI)."""
        return self.db.undo_batch(batch_id, hard=hard)

    def delete_rows(self, event_ids: Iterable[int], hard: bool = False) -> int:
        return self.db.delete_events(event_ids, hard=hard)

    def delete_matching(self, title: str = "", start: Optional[datetime] = None,
                        end: Optional[datetime] = None,
                        calendar_id: Optional[int] = None,
                        only_imported: bool = True, hard: bool = False) -> int:
        """Regelbasiertes Löschen ("alle 'Standby' im Oktober weg")."""
        sql = ["SELECT id FROM events WHERE deleted=0"]
        args: list = []
        if only_imported:
            sql.append("AND import_batch_id IS NOT NULL")
        if title:
            sql.append("AND title_key=?")
            args.append(normalize_title(title))
        if calendar_id:
            sql.append("AND calendar_id=?")
            args.append(int(calendar_id))
        if start:
            sql.append("AND start >= ?")
            args.append(ensure_aware(start).isoformat())
        if end:
            sql.append("AND start < ?")
            args.append(ensure_aware(end).isoformat())
        ids = [r["id"] for r in self.db.con.execute(" ".join(sql), args)]
        return self.db.delete_events(ids, hard=hard)

    def find_duplicates(self, calendar_id: Optional[int] = None) -> list[dict]:
        sql = ("SELECT title_key, start, COUNT(*) c, GROUP_CONCAT(id) ids "
               "FROM events WHERE deleted=0")
        args: list = []
        if calendar_id:
            sql += " AND calendar_id=?"
            args.append(int(calendar_id))
        sql += " GROUP BY title_key, start HAVING c > 1 ORDER BY c DESC"
        out = []
        for r in self.db.con.execute(sql, args):
            ids = [int(i) for i in r["ids"].split(",")]
            out.append({"title_key": r["title_key"], "start": r["start"],
                        "count": r["c"], "ids": ids, "keep": ids[0],
                        "remove": ids[1:]})
        return out

    def dedupe(self, calendar_id: Optional[int] = None, hard: bool = False) -> int:
        removed = 0
        for group in self.find_duplicates(calendar_id):
            removed += self.db.delete_events(group["remove"], hard=hard)
        return removed


# --------------------------------------------------------------------------
def export_events(events: Iterable[Event], out_dir, filename: str = "kalender.csv",
                  delimiter: str = ";") -> Path:
    """Schreibt Ereignisse als CSV - mit Schutz vor Formel-Injection."""
    target = safe_output_path(out_dir, filename)
    fields = ["Titel", "Start", "Ende", "Dauer_Minuten", "Ganztägig", "Ort",
              "Beschreibung", "Wiederholung", "Kalender", "Quelle"]
    fd = os.open(str(target), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.writer(fh, delimiter=delimiter, quoting=csv.QUOTE_MINIMAL)
        writer.writerow(fields)
        for ev in events:
            writer.writerow([
                sanitize_csv_cell(ev.title),
                ensure_aware(ev.start).strftime("%d.%m.%Y %H:%M"),
                ensure_aware(ev.end).strftime("%d.%m.%Y %H:%M") if ev.end else "",
                ev.duration_minutes,
                "ja" if ev.all_day else "nein",
                sanitize_csv_cell(ev.location),
                sanitize_csv_cell(ev.description.replace("\n", " ")),
                sanitize_csv_cell(ev.rrule),
                ev.calendar_id,
                sanitize_csv_cell(ev.source),
            ])
    return target
