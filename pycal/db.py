"""SQLite-Persistenz.

Sicherheitsleitlinien dieses Moduls:
* ausschliesslich parametrisierte Queries (keine String-Interpolation)
* Spaltennamen für dynamische Sortierung nur aus einer Allowlist
* Datenbankdatei mit 0600-Rechten im privaten App-Verzeichnis
* Fremdschlüssel + WAL aktiviert, Transaktionen über Kontextmanager
"""
from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable, Optional

from .models import (Calendar, Event, ensure_aware, from_iso, to_iso, utcnow,
                     normalize_title)
from .security import sanitize_text, sanitize_title, SecurityError

SCHEMA_VERSION = 3

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS accounts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL,
    provider      TEXT NOT NULL,           -- caldav | ics | google | outlook
    url           TEXT NOT NULL DEFAULT '',
    username      TEXT NOT NULL DEFAULT '',
    secret_blob   TEXT NOT NULL DEFAULT '',-- AES-GCM, nie Klartext
    enabled       INTEGER NOT NULL DEFAULT 1,
    last_sync     TEXT,
    last_error    TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS calendars (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    color       TEXT NOT NULL DEFAULT '#4C8DF6',
    visible     INTEGER NOT NULL DEFAULT 1,
    account_id  INTEGER REFERENCES accounts(id) ON DELETE CASCADE,
    remote_url  TEXT NOT NULL DEFAULT '',
    read_only   INTEGER NOT NULL DEFAULT 0,
    sync_token  TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS import_batches (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    filename     TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    row_count    INTEGER NOT NULL DEFAULT 0,
    imported     INTEGER NOT NULL DEFAULT 0,
    skipped      INTEGER NOT NULL DEFAULT 0,
    calendar_id  INTEGER REFERENCES calendars(id) ON DELETE CASCADE,
    note         TEXT NOT NULL DEFAULT '',
    undone       INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS events (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    calendar_id      INTEGER NOT NULL REFERENCES calendars(id) ON DELETE CASCADE,
    uid              TEXT NOT NULL,
    title            TEXT NOT NULL DEFAULT '',
    title_key        TEXT NOT NULL DEFAULT '',
    description      TEXT NOT NULL DEFAULT '',
    location         TEXT NOT NULL DEFAULT '',
    start            TEXT NOT NULL,
    end              TEXT,
    all_day          INTEGER NOT NULL DEFAULT 0,
    color            TEXT NOT NULL DEFAULT '',
    rrule            TEXT NOT NULL DEFAULT '',
    exdates          TEXT NOT NULL DEFAULT '',
    reminder_minutes INTEGER,
    etag             TEXT NOT NULL DEFAULT '',
    remote_href      TEXT NOT NULL DEFAULT '',
    dirty            INTEGER NOT NULL DEFAULT 1,
    deleted          INTEGER NOT NULL DEFAULT 0,
    import_batch_id  INTEGER REFERENCES import_batches(id) ON DELETE SET NULL,
    source           TEXT NOT NULL DEFAULT 'local',
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    UNIQUE(calendar_id, uid)
);

CREATE INDEX IF NOT EXISTS idx_events_start ON events(start);
CREATE INDEX IF NOT EXISTS idx_events_cal   ON events(calendar_id, deleted);
CREATE INDEX IF NOT EXISTS idx_events_key   ON events(title_key);
CREATE INDEX IF NOT EXISTS idx_events_batch ON events(import_batch_id);
CREATE INDEX IF NOT EXISTS idx_events_dirty ON events(dirty, deleted);

CREATE TABLE IF NOT EXISTS patterns (
    title_key        TEXT PRIMARY KEY,
    display_title    TEXT NOT NULL,
    samples          INTEGER NOT NULL DEFAULT 0,
    durations        TEXT NOT NULL DEFAULT '',   -- CSV der letzten Dauern (Min.)
    start_minutes    TEXT NOT NULL DEFAULT '',   -- CSV der Startzeiten (Min. ab 00:00)
    weekdays         TEXT NOT NULL DEFAULT '',   -- CSV der Wochentage 0-6
    last_seen        TEXT,
    location         TEXT NOT NULL DEFAULT '',
    color            TEXT NOT NULL DEFAULT '',
    reminder_minutes INTEGER,
    pinned_duration  INTEGER                     -- vom Nutzer fixiert
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

_SORTABLE = {"start": "start", "title": "title", "created": "created_at",
             "updated": "updated_at"}


class Database:
    """Dünner, thread-sicherer Wrapper um sqlite3."""

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        new_file = not self.path.exists()
        self._local = threading.local()
        self._lock = threading.RLock()
        with self.connection() as con:
            con.executescript(SCHEMA)
            self._migrate(con)
        if new_file:
            self._harden()

    def _harden(self) -> None:
        for suffix in ("", "-wal", "-shm"):
            p = Path(str(self.path) + suffix)
            if p.exists():
                try:
                    os.chmod(p, 0o600)
                except OSError:
                    pass

    # ------------------------------------------------------------------
    @property
    def con(self) -> sqlite3.Connection:
        con = getattr(self._local, "con", None)
        if con is None:
            con = sqlite3.connect(str(self.path), timeout=15,
                                  detect_types=0, isolation_level=None)
            con.row_factory = sqlite3.Row
            con.execute("PRAGMA foreign_keys=ON")
            con.execute("PRAGMA journal_mode=WAL")
            con.execute("PRAGMA synchronous=NORMAL")
            con.execute("PRAGMA trusted_schema=OFF")
            # Begrenzt Speicherverbrauch durch bösartige Abfragen/Daten
            con.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 50 * 1024 * 1024)
            self._local.con = con
        return con

    @contextmanager
    def connection(self):
        with self._lock:
            con = self.con
            try:
                if not con.in_transaction:
                    con.execute("BEGIN")
                yield con
                if con.in_transaction:
                    con.execute("COMMIT")
            except Exception:
                try:
                    if con.in_transaction:
                        con.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise

    def close(self) -> None:
        con = getattr(self._local, "con", None)
        if con is not None:
            con.close()
            self._local.con = None

    def _migrate(self, con) -> None:
        row = con.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        current = int(row["value"]) if row else 0
        if current < SCHEMA_VERSION:
            con.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('schema_version',?)",
                        (str(SCHEMA_VERSION),))
        if con.execute("SELECT COUNT(*) c FROM calendars").fetchone()["c"] == 0:
            con.execute("INSERT INTO calendars(name,color,visible) VALUES(?,?,1)",
                        ("Persönlich", "#4C8DF6"))

    # ------------------------------------------------------------------
    # Kalender
    # ------------------------------------------------------------------
    def list_calendars(self, only_visible: bool = False) -> list[Calendar]:
        sql = "SELECT * FROM calendars"
        if only_visible:
            sql += " WHERE visible=1"
        sql += " ORDER BY id"
        return [self._row_to_calendar(r) for r in self.con.execute(sql)]

    def get_calendar(self, cal_id: int) -> Optional[Calendar]:
        r = self.con.execute("SELECT * FROM calendars WHERE id=?", (int(cal_id),)).fetchone()
        return self._row_to_calendar(r) if r else None

    def save_calendar(self, cal: Calendar) -> Calendar:
        name = sanitize_title(cal.name) or "Kalender"
        color = _safe_color(cal.color)
        with self.connection() as con:
            if cal.id is None:
                cur = con.execute(
                    "INSERT INTO calendars(name,color,visible,account_id,remote_url,"
                    "read_only,sync_token) VALUES(?,?,?,?,?,?,?)",
                    (name, color, int(cal.visible), cal.account_id,
                     sanitize_text(cal.remote_url, 2048), int(cal.read_only),
                     sanitize_text(cal.sync_token, 512)))
                cal.id = cur.lastrowid
            else:
                con.execute(
                    "UPDATE calendars SET name=?,color=?,visible=?,account_id=?,"
                    "remote_url=?,read_only=?,sync_token=? WHERE id=?",
                    (name, color, int(cal.visible), cal.account_id,
                     sanitize_text(cal.remote_url, 2048), int(cal.read_only),
                     sanitize_text(cal.sync_token, 512), int(cal.id)))
        return cal

    def delete_calendar(self, cal_id: int) -> None:
        with self.connection() as con:
            con.execute("DELETE FROM calendars WHERE id=?", (int(cal_id),))

    @staticmethod
    def _row_to_calendar(r) -> Calendar:
        return Calendar(id=r["id"], name=r["name"], color=r["color"],
                        visible=bool(r["visible"]), account_id=r["account_id"],
                        remote_url=r["remote_url"], read_only=bool(r["read_only"]),
                        sync_token=r["sync_token"])

    # ------------------------------------------------------------------
    # Ereignisse
    # ------------------------------------------------------------------
    def save_event(self, ev: Event, mark_dirty: bool = True) -> Event:
        ev.title = sanitize_title(ev.title)
        ev.description = sanitize_text(ev.description)
        ev.location = sanitize_text(ev.location, 500)
        ev.color = _safe_color(ev.color, allow_empty=True)
        ev.updated_at = utcnow()
        if mark_dirty:
            ev.dirty = True
        exd = ",".join(to_iso(d) for d in ev.exdates if d)
        params = (int(ev.calendar_id), sanitize_text(ev.uid, 512), ev.title,
                  normalize_title(ev.title), ev.description, ev.location,
                  to_iso(ev.start), to_iso(ev.end), int(ev.all_day), ev.color,
                  sanitize_text(ev.rrule, 512), exd,
                  ev.reminder_minutes if ev.reminder_minutes is None
                  else int(ev.reminder_minutes),
                  sanitize_text(ev.etag, 256), sanitize_text(ev.remote_href, 2048),
                  int(ev.dirty), int(ev.deleted), ev.import_batch_id,
                  sanitize_text(ev.source, 32), to_iso(ev.created_at), to_iso(ev.updated_at))
        with self.connection() as con:
            if ev.id is None:
                cur = con.execute(
                    "INSERT INTO events(calendar_id,uid,title,title_key,description,"
                    "location,start,end,all_day,color,rrule,exdates,reminder_minutes,"
                    "etag,remote_href,dirty,deleted,import_batch_id,source,created_at,"
                    "updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    params)
                ev.id = cur.lastrowid
            else:
                con.execute(
                    "UPDATE events SET calendar_id=?,uid=?,title=?,title_key=?,"
                    "description=?,location=?,start=?,end=?,all_day=?,color=?,rrule=?,"
                    "exdates=?,reminder_minutes=?,etag=?,remote_href=?,dirty=?,deleted=?,"
                    "import_batch_id=?,source=?,created_at=?,updated_at=? WHERE id=?",
                    params + (int(ev.id),))
        return ev

    def save_events_bulk(self, events: Iterable[Event]) -> int:
        count = 0
        for ev in events:
            self.save_event(ev)
            count += 1
        return count

    def get_event(self, event_id: int) -> Optional[Event]:
        r = self.con.execute("SELECT * FROM events WHERE id=?", (int(event_id),)).fetchone()
        return self._row_to_event(r) if r else None

    def find_by_uid(self, calendar_id: int, uid: str) -> Optional[Event]:
        r = self.con.execute("SELECT * FROM events WHERE calendar_id=? AND uid=?",
                             (int(calendar_id), str(uid))).fetchone()
        return self._row_to_event(r) if r else None

    def list_events(self, start: datetime, end: datetime,
                    calendar_ids: Optional[list[int]] = None,
                    include_deleted: bool = False) -> list[Event]:
        """Alle Events, die im Fenster liegen koennten (inkl. Serien)."""
        sql = ["SELECT * FROM events WHERE 1=1"]
        args: list = []
        if not include_deleted:
            sql.append("AND deleted=0")
        if calendar_ids:
            ids = [int(c) for c in calendar_ids]
            sql.append("AND calendar_id IN (%s)" % ",".join("?" * len(ids)))
            args.extend(ids)
        # Einzeltermine grob vorfiltern, Serien immer laden
        sql.append("AND (rrule <> '' OR (start < ? AND COALESCE(end, start) >= ?))")
        args.extend([to_iso(ensure_aware(end)),
                     to_iso(ensure_aware(start) - timedelta(days=2))])
        sql.append("ORDER BY start")
        rows = self.con.execute(" ".join(sql), args)
        return [self._row_to_event(r) for r in rows]

    def occurrences(self, start: datetime, end: datetime,
                    calendar_ids: Optional[list[int]] = None) -> list[tuple[datetime, Event]]:
        out: list[tuple[datetime, Event]] = []
        for ev in self.list_events(start, end, calendar_ids):
            for occ in ev.occurrences(start, end):
                out.append((occ, ev))
        out.sort(key=lambda t: t[0])
        return out

    def search_events(self, query: str, limit: int = 200) -> list[Event]:
        """Volltextsuche - bewusst mit Parameterbindung und escaptem LIKE."""
        q = sanitize_text(query, 200)
        if not q:
            return []
        # %, _ und \ maskieren, damit der Nutzer keine LIKE-Wildcards einschleust
        esc = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = f"%{esc}%"
        rows = self.con.execute(
            "SELECT * FROM events WHERE deleted=0 AND ("
            "title LIKE ? ESCAPE '\\' OR description LIKE ? ESCAPE '\\' "
            "OR location LIKE ? ESCAPE '\\') ORDER BY start DESC LIMIT ?",
            (pattern, pattern, pattern, int(limit)))
        return [self._row_to_event(r) for r in rows]

    def events_by_title_key(self, title_key: str, limit: int = 200) -> list[Event]:
        rows = self.con.execute(
            "SELECT * FROM events WHERE title_key=? AND deleted=0 "
            "ORDER BY start DESC LIMIT ?", (str(title_key), int(limit)))
        return [self._row_to_event(r) for r in rows]

    def delete_event(self, event_id: int, hard: bool = False) -> None:
        with self.connection() as con:
            if hard:
                con.execute("DELETE FROM events WHERE id=?", (int(event_id),))
            else:
                con.execute("UPDATE events SET deleted=1, dirty=1, updated_at=? "
                            "WHERE id=?", (to_iso(utcnow()), int(event_id)))

    def delete_events(self, event_ids: Iterable[int], hard: bool = False) -> int:
        ids = [int(i) for i in event_ids]
        if not ids:
            return 0
        placeholders = ",".join("?" * len(ids))
        with self.connection() as con:
            if hard:
                cur = con.execute(f"DELETE FROM events WHERE id IN ({placeholders})", ids)
            else:
                cur = con.execute(
                    f"UPDATE events SET deleted=1,dirty=1,updated_at=? "
                    f"WHERE id IN ({placeholders})", [to_iso(utcnow())] + ids)
            return cur.rowcount

    def purge_deleted(self, older_than_days: int = 30) -> int:
        cutoff = to_iso(utcnow() - timedelta(days=int(older_than_days)))
        with self.connection() as con:
            cur = con.execute("DELETE FROM events WHERE deleted=1 AND dirty=0 "
                              "AND updated_at < ?", (cutoff,))
            return cur.rowcount

    @staticmethod
    def _row_to_event(r) -> Event:
        exdates = [from_iso(x) for x in (r["exdates"] or "").split(",") if x]
        return Event(
            id=r["id"], calendar_id=r["calendar_id"], uid=r["uid"], title=r["title"],
            description=r["description"], location=r["location"],
            start=from_iso(r["start"]), end=from_iso(r["end"]),
            all_day=bool(r["all_day"]), color=r["color"], rrule=r["rrule"],
            exdates=[d for d in exdates if d], reminder_minutes=r["reminder_minutes"],
            etag=r["etag"], remote_href=r["remote_href"], dirty=bool(r["dirty"]),
            deleted=bool(r["deleted"]), import_batch_id=r["import_batch_id"],
            source=r["source"], created_at=from_iso(r["created_at"]),
            updated_at=from_iso(r["updated_at"]))

    # ------------------------------------------------------------------
    # Import-Stapel  (Grundlage für das einfache Löschen nach CSV-Import)
    # ------------------------------------------------------------------
    def create_batch(self, filename: str, calendar_id: int, note: str = "") -> int:
        with self.connection() as con:
            cur = con.execute(
                "INSERT INTO import_batches(filename,created_at,calendar_id,note) "
                "VALUES(?,?,?,?)",
                (sanitize_text(filename, 255), to_iso(utcnow()), int(calendar_id),
                 sanitize_text(note, 500)))
            return cur.lastrowid

    def finish_batch(self, batch_id: int, row_count: int, imported: int,
                     skipped: int) -> None:
        with self.connection() as con:
            con.execute("UPDATE import_batches SET row_count=?,imported=?,skipped=? "
                        "WHERE id=?", (int(row_count), int(imported), int(skipped),
                                       int(batch_id)))

    def list_batches(self, include_undone: bool = False) -> list[dict]:
        sql = ("SELECT b.*, (SELECT COUNT(*) FROM events e "
               "WHERE e.import_batch_id=b.id AND e.deleted=0) AS live_events "
               "FROM import_batches b")
        if not include_undone:
            sql += " WHERE b.undone=0"
        sql += " ORDER BY b.id DESC"
        return [dict(r) for r in self.con.execute(sql)]

    def batch_events(self, batch_id: int) -> list[Event]:
        rows = self.con.execute(
            "SELECT * FROM events WHERE import_batch_id=? AND deleted=0 ORDER BY start",
            (int(batch_id),))
        return [self._row_to_event(r) for r in rows]

    def undo_batch(self, batch_id: int, hard: bool = False) -> int:
        """Macht einen kompletten CSV-Import mit einem Aufruf rückgängig."""
        with self.connection() as con:
            if hard:
                cur = con.execute("DELETE FROM events WHERE import_batch_id=?",
                                  (int(batch_id),))
            else:
                cur = con.execute(
                    "UPDATE events SET deleted=1,dirty=1,updated_at=? "
                    "WHERE import_batch_id=? AND deleted=0",
                    (to_iso(utcnow()), int(batch_id)))
            con.execute("UPDATE import_batches SET undone=1 WHERE id=?", (int(batch_id),))
            return cur.rowcount

    # ------------------------------------------------------------------
    # Konten
    # ------------------------------------------------------------------
    def save_account(self, account: dict) -> int:
        with self.connection() as con:
            if account.get("id"):
                con.execute(
                    "UPDATE accounts SET name=?,provider=?,url=?,username=?,"
                    "secret_blob=?,enabled=?,last_sync=?,last_error=? WHERE id=?",
                    (sanitize_title(account["name"]), sanitize_text(account["provider"], 32),
                     sanitize_text(account.get("url", ""), 2048),
                     sanitize_text(account.get("username", ""), 255),
                     account.get("secret_blob", ""), int(account.get("enabled", 1)),
                     account.get("last_sync"), sanitize_text(account.get("last_error", ""), 500),
                     int(account["id"])))
                return int(account["id"])
            cur = con.execute(
                "INSERT INTO accounts(name,provider,url,username,secret_blob,enabled) "
                "VALUES(?,?,?,?,?,?)",
                (sanitize_title(account["name"]), sanitize_text(account["provider"], 32),
                 sanitize_text(account.get("url", ""), 2048),
                 sanitize_text(account.get("username", ""), 255),
                 account.get("secret_blob", ""), int(account.get("enabled", 1))))
            return cur.lastrowid

    def list_accounts(self) -> list[dict]:
        return [dict(r) for r in self.con.execute("SELECT * FROM accounts ORDER BY id")]

    def delete_account(self, account_id: int) -> None:
        with self.connection() as con:
            con.execute("DELETE FROM accounts WHERE id=?", (int(account_id),))

    # ------------------------------------------------------------------
    # Einstellungen
    # ------------------------------------------------------------------
    def set_setting(self, key: str, value: str) -> None:
        with self.connection() as con:
            con.execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)",
                        (sanitize_text(key, 100), sanitize_text(str(value), 20000)))

    def get_setting(self, key: str, default: str = "") -> str:
        r = self.con.execute("SELECT value FROM settings WHERE key=?",
                             (sanitize_text(key, 100),)).fetchone()
        return r["value"] if r else default

    def integrity_check(self) -> bool:
        r = self.con.execute("PRAGMA integrity_check").fetchone()
        return r[0] == "ok"


_HEX = set("0123456789abcdefABCDEF")


def _safe_color(color: str, allow_empty: bool = False) -> str:
    """Akzeptiert nur #RGB/#RRGGBB - verhindert Style-Injection in der UI."""
    c = (color or "").strip()
    if not c:
        if allow_empty:
            return ""
        return "#4C8DF6"
    if not c.startswith("#"):
        c = "#" + c
    body = c[1:]
    if len(body) in (3, 6) and all(ch in _HEX for ch in body):
        return "#" + body.upper()
    if allow_empty:
        return ""
    return "#4C8DF6"
