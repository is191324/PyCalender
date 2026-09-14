"""Anwendungskern - verbindet Datenbank, Muster, Themes und Sync.

Bewusst ohne Kivy: dieselbe Klasse lässt sich in Tests, per Kommandozeile
oder von einem Hintergrunddienst benutzen.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from .csvio import CsvImporter, ImportManager, export_events
from .db import Database
from .models import Calendar, Event, ensure_aware, utcnow
from .patterns import PatternStore, PatternSuggestion
from .security import (CredentialVault, RateLimiter, SecurityError, hash_pin,
                       sanitize_title, verify_pin)
from .sync import SyncManager
from .theme import ThemeManager


def default_data_dir() -> Path:
    """Privates App-Verzeichnis - auf Android sandboxed pro App."""
    env = os.environ.get("PYCALENDAR_HOME")
    if env:
        return Path(env)
    android_dir = os.environ.get("ANDROID_PRIVATE") or os.environ.get("ANDROID_APP_PATH")
    if android_dir:
        return Path(android_dir) / "pycalendar"
    return Path.home() / ".local" / "share" / "pycalendar"


class CalendarApp:
    """Fassade für die gesamte Anwendungslogik."""

    def __init__(self, data_dir=None):
        self.data_dir = Path(data_dir or default_data_dir())
        self.data_dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.data_dir, 0o700)
        except OSError:
            pass
        self.db = Database(self.data_dir / "calendar.db")
        self.vault = CredentialVault(self.data_dir / "device.key")
        self.patterns = PatternStore(self.db)
        self.themes = ThemeManager(self.db, self.data_dir)
        self.sync = SyncManager(self.db, self.vault, self.patterns)
        self.importer = CsvImporter(self.db, self.patterns)
        self.imports = ImportManager(self.db)
        self.pin_limiter = RateLimiter()

    # ------------------------------------------------------------------
    # Termine
    # ------------------------------------------------------------------
    def create_event(self, title: str, start: datetime,
                     end: Optional[datetime] = None, **kwargs) -> Event:
        ev = Event(title=sanitize_title(title), start=ensure_aware(start), end=end,
                   calendar_id=kwargs.pop("calendar_id", self.default_calendar_id()),
                   **kwargs)
        if ev.end is None:
            self.patterns.apply(ev)                 # Dauer aus Muster
        if ev.end is None:
            ev.end = ensure_aware(ev.start) + timedelta(hours=1)
        self.db.save_event(ev)
        self.patterns.learn(ev)
        return ev

    def update_event(self, ev: Event) -> Event:
        self.db.save_event(ev)
        self.patterns.learn(ev)
        return ev

    def delete_event(self, event_id: int) -> None:
        self.db.delete_event(event_id)

    def delete_occurrence(self, event_id: int, occurrence: datetime) -> None:
        """Einzelnen Termin einer Serie absagen (EXDATE)."""
        ev = self.db.get_event(event_id)
        if ev is None:
            return
        if not ev.is_recurring:
            self.db.delete_event(event_id)
            return
        ev.exdates = list(ev.exdates) + [ensure_aware(occurrence)]
        self.db.save_event(ev)

    def day(self, day: datetime) -> list[tuple[datetime, Event]]:
        start = ensure_aware(day).replace(hour=0, minute=0, second=0, microsecond=0)
        return self.db.occurrences(start, start + timedelta(days=1),
                                   self.visible_calendar_ids())

    def month(self, year: int, month: int) -> dict[str, list[tuple[datetime, Event]]]:
        start = datetime(year, month, 1, tzinfo=timezone.utc)
        end = datetime(year + (month == 12), (month % 12) + 1, 1, tzinfo=timezone.utc)
        buckets: dict[str, list] = {}
        for occ, ev in self.db.occurrences(start, end, self.visible_calendar_ids()):
            buckets.setdefault(occ.date().isoformat(), []).append((occ, ev))
        return buckets

    def agenda(self, days: int = 30) -> list[tuple[datetime, Event]]:
        now = utcnow()
        return self.db.occurrences(now - timedelta(hours=12),
                                   now + timedelta(days=days),
                                   self.visible_calendar_ids())

    def search(self, query: str) -> list[Event]:
        return self.db.search_events(query)

    # ------------------------------------------------------------------
    def visible_calendar_ids(self) -> list[int]:
        return [c.id for c in self.db.list_calendars(only_visible=True)]

    def default_calendar_id(self) -> int:
        cals = self.db.list_calendars()
        local = [c for c in cals if not c.read_only]
        return (local or cals)[0].id

    # ------------------------------------------------------------------
    # Muster
    # ------------------------------------------------------------------
    def suggest(self, title: str) -> Optional[PatternSuggestion]:
        return self.patterns.suggest(title)

    def suggest_recurrence(self, title: str):
        return self.patterns.suggest_recurrence(title)

    # ------------------------------------------------------------------
    # CSV
    # ------------------------------------------------------------------
    def preview_csv(self, path, calendar_id: Optional[int] = None, **kw):
        return self.importer.preview(path,
                                     calendar_id or self.default_calendar_id(), **kw)

    def import_csv(self, preview, **kw) -> dict:
        return self.importer.commit(preview,
                                    kw.pop("calendar_id", None)
                                    or self.default_calendar_id(), **kw)

    def undo_import(self, batch_id: int, hard: bool = False) -> int:
        return self.imports.undo(batch_id, hard=hard)

    def export_csv(self, days: int = 365, filename: str = "kalender.csv") -> Path:
        now = utcnow()
        events = self.db.list_events(now - timedelta(days=days),
                                     now + timedelta(days=days))
        out_dir = self.data_dir / "export"
        out_dir.mkdir(parents=True, exist_ok=True)
        return export_events(events, out_dir, filename)

    # ------------------------------------------------------------------
    # App-Sperre
    # ------------------------------------------------------------------
    def set_pin(self, pin: str) -> None:
        pin = str(pin or "")
        if len(pin) < 4:
            raise SecurityError("Die PIN muss mindestens 4 Stellen haben.")
        self.db.set_setting("pin_hash", hash_pin(pin))

    def clear_pin(self) -> None:
        self.db.set_setting("pin_hash", "")

    @property
    def pin_enabled(self) -> bool:
        return bool(self.db.get_setting("pin_hash", ""))

    def check_pin(self, pin: str) -> bool:
        self.pin_limiter.check()
        stored = self.db.get_setting("pin_hash", "")
        if not stored:
            return True
        if verify_pin(str(pin or ""), stored):
            self.pin_limiter.record_success()
            return True
        self.pin_limiter.record_failure()
        return False

    # ------------------------------------------------------------------
    def maintenance(self) -> dict:
        """Aufräumarbeiten: geloeschte Einträge entfernen, Muster neu bauen."""
        purged = self.db.purge_deleted(30)
        rebuilt = self.patterns.rebuild()
        return {"purged": purged, "patterns": rebuilt,
                "integrity": self.db.integrity_check()}

    def close(self) -> None:
        self.db.close()
