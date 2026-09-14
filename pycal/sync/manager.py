"""Synchronisationssteuerung.

Unterstuetzte Kontotypen:
  caldav   - beliebiger CalDAV-Server (Nextcloud, Radicale, Fastmail ...)
  google   - Google Kalender via CalDAV (App-Passwort erforderlich)
  outlook  - Outlook/Microsoft 365 via CalDAV bzw. ICS-Abo
  ics      - schreibgeschuetztes Abo einer .ics-Adresse (Webcal)

Konfliktstrategie (Zwei-Wege-Sync):
  * lokal geändert + remote unverändert  -> hochladen
  * lokal unverändert + remote geändert  -> herunterladen
  * beides geändert                       -> "newest wins" nach updated_at,
    die unterlegene Fassung wird als Kopie "(Konflikt)" behalten, damit
    keine Nutzerdaten verloren gehen
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from ..models import Calendar, Event, ensure_aware, to_iso, utcnow
from ..security import SecurityError, redact, sanitize_text, validate_sync_url
from .caldav import CalDavClient, ConflictError, RemoteCalendar
from .http import HttpError, SecureHttpClient
from .icsfmt import build_ics, parse_ics

PROVIDER_PRESETS = {
    "google": {
        "label": "Google Kalender",
        "url": "https://apidata.googleusercontent.com/caldav/v2/",
        "hint": ("Google verlangt ein App-Passwort (Konto -> Sicherheit -> "
                 "App-Passwörter), wenn die Zwei-Faktor-Anmeldung aktiv ist. "
                 "Als Benutzername die vollständige Gmail-Adresse eintragen."),
    },
    "outlook": {
        "label": "Outlook / Microsoft 365",
        "url": "https://outlook.office365.com/",
        "hint": ("Outlook.com stellt Kalender als ICS-Abo bereit "
                 "(Einstellungen -> Kalender -> Freigegebene Kalender). "
                 "Für Schreibzugriff einen CalDAV-faehigen Zugang nutzen."),
    },
    "caldav": {"label": "CalDAV (allgemein)", "url": "", "hint": ""},
    "ics": {"label": "ICS-Abo (nur lesen)", "url": "", "hint":
            "Adresse endet meist auf .ics und wird nur gelesen."},
}


@dataclass
class AccountConfig:
    id: Optional[int] = None
    name: str = ""
    provider: str = "caldav"
    url: str = ""
    username: str = ""
    password: str = ""            # nur im Speicher, verschlüsselt persistiert
    enabled: bool = True
    allow_insecure: bool = False


@dataclass
class SyncResult:
    account: str = ""
    downloaded: int = 0
    uploaded: int = 0
    deleted_remote: int = 0
    deleted_local: int = 0
    conflicts: int = 0
    calendars: int = 0
    error: str = ""
    started: datetime = field(default_factory=utcnow)
    finished: Optional[datetime] = None

    @property
    def ok(self) -> bool:
        return not self.error

    def summary(self) -> str:
        if self.error:
            return f"Fehler: {self.error}"
        return (f"{self.downloaded} geladen, {self.uploaded} gesendet, "
                f"{self.deleted_local} lokal entfernt, {self.conflicts} Konflikte")


class SyncManager:
    def __init__(self, db, vault, patterns=None, window_days: int = 400):
        self.db = db
        self.vault = vault
        self.patterns = patterns
        self.window_days = window_days

    # ------------------------------------------------------------------
    # Kontoverwaltung
    # ------------------------------------------------------------------
    def add_account(self, cfg: AccountConfig) -> int:
        preset = PROVIDER_PRESETS.get(cfg.provider, {})
        url = cfg.url or preset.get("url", "")
        if cfg.provider == "google" and cfg.username and url.endswith("/v2/"):
            url = url + cfg.username + "/user"
        validate_sync_url(url, allow_insecure=cfg.allow_insecure)
        blob = self.vault.encrypt(cfg.password or "")
        return self.db.save_account({
            "id": cfg.id, "name": cfg.name or preset.get("label", "Konto"),
            "provider": cfg.provider, "url": url, "username": cfg.username,
            "secret_blob": blob, "enabled": 1 if cfg.enabled else 0})

    def remove_account(self, account_id: int) -> None:
        self.db.delete_account(account_id)

    def accounts(self) -> list[dict]:
        out = []
        for row in self.db.list_accounts():
            data = dict(row)
            data.pop("secret_blob", None)      # Geheimnis verlässt die Schicht nie
            out.append(data)
        return out

    def test_connection(self, cfg: AccountConfig) -> list[RemoteCalendar]:
        client = CalDavClient(cfg.url, cfg.username, cfg.password, cfg.allow_insecure)
        try:
            return client.discover()
        finally:
            client.close()

    # ------------------------------------------------------------------
    # Synchronisieren
    # ------------------------------------------------------------------
    def sync_all(self, progress: Optional[Callable[[str], None]] = None
                 ) -> list[SyncResult]:
        results = []
        for account in self.db.list_accounts():
            if not account["enabled"]:
                continue
            results.append(self.sync_account(account["id"], progress))
        return results

    def sync_account(self, account_id: int,
                     progress: Optional[Callable[[str], None]] = None) -> SyncResult:
        row = next((a for a in self.db.list_accounts()
                    if a["id"] == int(account_id)), None)
        if row is None:
            raise SecurityError("Konto nicht gefunden.")
        result = SyncResult(account=row["name"])
        try:
            password = self.vault.decrypt(row["secret_blob"])
            cfg = AccountConfig(id=row["id"], name=row["name"],
                                provider=row["provider"], url=row["url"],
                                username=row["username"], password=password)
            if cfg.provider == "ics":
                self._sync_ics(cfg, result, progress)
            else:
                self._sync_caldav(cfg, result, progress)
            self.db.save_account({**dict(row), "last_sync": to_iso(utcnow()),
                                  "last_error": ""})
        except Exception as exc:
            result.error = redact(str(exc))[:400]
            self.db.save_account({**dict(row), "last_error": result.error})
        finally:
            result.finished = utcnow()
            # Passwort so schnell wie möglich aus dem Speicher nehmen
            password = None
        return result

    # ---------------- ICS (nur lesen) ----------------
    def _sync_ics(self, cfg: AccountConfig, result: SyncResult, progress) -> None:
        url = cfg.url.replace("webcal://", "https://").replace("webcals://", "https://")
        client = SecureHttpClient(cfg.username, cfg.password, cfg.allow_insecure)
        try:
            status, _, text, _ = client.request("GET", url)
        finally:
            client.close()
        if status != 200:
            raise HttpError(f"Abo nicht erreichbar (Status {status}).")
        calendar = self._local_calendar_for(cfg, cfg.name, cfg.url, read_only=True)
        result.calendars = 1
        remote_events = parse_ics(text, calendar.id, source="ics")
        seen = set()
        for ev in remote_events:
            seen.add(ev.uid)
            self._merge_remote_event(calendar, ev, "", "", result)
        self._remove_vanished(calendar, seen, result)

    # ---------------- CalDAV (zwei Wege) ----------------
    def _sync_caldav(self, cfg: AccountConfig, result: SyncResult, progress) -> None:
        client = CalDavClient(cfg.url, cfg.username, cfg.password, cfg.allow_insecure)
        try:
            remotes = client.discover()
            result.calendars = len(remotes)
            for remote in remotes:
                if progress:
                    progress(f"{cfg.name}: {remote.name}")
                calendar = self._local_calendar_for(
                    cfg, remote.name, remote.href, read_only=remote.read_only,
                    color=remote.color)
                self._sync_calendar(client, calendar, remote, result)
        finally:
            client.close()

    def _sync_calendar(self, client: CalDavClient, calendar: Calendar,
                       remote: RemoteCalendar, result: SyncResult) -> None:
        now = utcnow()
        start = (now - timedelta(days=self.window_days)).strftime("%Y%m%dT%H%M%SZ")
        end = (now + timedelta(days=self.window_days)).strftime("%Y%m%dT%H%M%SZ")
        items = client.list_items(remote.href, start, end)
        by_href = {i.href: i for i in items}

        local = {e.remote_href: e for e in self.db.list_events(
            now - timedelta(days=self.window_days),
            now + timedelta(days=self.window_days), [calendar.id],
            include_deleted=True) if e.remote_href}

        # 1) Neues/geaendertes vom Server holen
        stale = [h for h, item in by_href.items()
                 if h not in local or local[h].etag != item.etag]
        for fetched in client.fetch(remote.href, stale):
            for ev in parse_ics(fetched.data, calendar.id, source="caldav"):
                self._merge_remote_event(calendar, ev, fetched.href,
                                         fetched.etag, result)

        # 2) Lokale Änderungen hochladen
        if not calendar.read_only:
            dirty = [e for e in self.db.list_events(
                now - timedelta(days=self.window_days),
                now + timedelta(days=self.window_days), [calendar.id],
                include_deleted=True) if e.dirty]
            for ev in dirty:
                try:
                    if ev.deleted:
                        if ev.remote_href:
                            client.delete(ev.remote_href, ev.etag)
                            result.deleted_remote += 1
                        ev.dirty = False
                        self.db.save_event(ev, mark_dirty=False)
                    else:
                        href, etag = client.put(remote.href, ev.uid,
                                                build_ics([ev]), ev.etag)
                        ev.remote_href, ev.etag, ev.dirty = href, etag, False
                        self.db.save_event(ev, mark_dirty=False)
                        result.uploaded += 1
                except ConflictError:
                    result.conflicts += 1
                    self._keep_conflict_copy(ev)

        # 3) Auf dem Server geloeschte Termine lokal entfernen
        remote_hrefs = set(by_href)
        for href, ev in local.items():
            if href not in remote_hrefs and not ev.dirty and not ev.deleted:
                self.db.delete_event(ev.id, hard=True)
                result.deleted_local += 1

    # ------------------------------------------------------------------
    def _merge_remote_event(self, calendar: Calendar, remote_ev: Event,
                            href: str, etag: str, result: SyncResult) -> None:
        existing = self.db.find_by_uid(calendar.id, remote_ev.uid)
        remote_ev.remote_href = href
        remote_ev.etag = etag
        remote_ev.dirty = False
        if existing is None:
            self.db.save_event(remote_ev, mark_dirty=False)
            if self.patterns and not remote_ev.deleted:
                self.patterns.learn(remote_ev)
            result.downloaded += 1
            return
        if existing.dirty:
            # beide Seiten geändert -> juengere Fassung gewinnt
            result.conflicts += 1
            if ensure_aware(existing.updated_at) >= ensure_aware(remote_ev.updated_at):
                return                      # lokal gewinnt, wird später hochgeladen
            self._keep_conflict_copy(existing)
        remote_ev.id = existing.id
        remote_ev.created_at = existing.created_at
        remote_ev.import_batch_id = existing.import_batch_id
        self.db.save_event(remote_ev, mark_dirty=False)
        if self.patterns and not remote_ev.deleted:
            self.patterns.learn(remote_ev)
        result.downloaded += 1

    def _keep_conflict_copy(self, ev: Event) -> None:
        import copy
        clone = copy.deepcopy(ev)
        clone.id = None
        clone.uid = f"conflict-{utcnow().strftime('%Y%m%d%H%M%S')}-{ev.uid}"[:200]
        clone.title = f"{ev.title} (Konflikt)"
        clone.remote_href = ""
        clone.etag = ""
        clone.dirty = False
        clone.source = "local"
        self.db.save_event(clone, mark_dirty=False)

    def _remove_vanished(self, calendar: Calendar, seen_uids: set, result) -> None:
        rows = self.db.con.execute(
            "SELECT id,uid FROM events WHERE calendar_id=? AND source='ics' "
            "AND deleted=0", (calendar.id,))
        for r in rows:
            if r["uid"] not in seen_uids:
                self.db.delete_event(r["id"], hard=True)
                result.deleted_local += 1

    def _local_calendar_for(self, cfg: AccountConfig, name: str, href: str,
                            read_only: bool = False, color: str = "") -> Calendar:
        for cal in self.db.list_calendars():
            if cal.account_id == cfg.id and cal.remote_url == href:
                return cal
        cal = Calendar(name=sanitize_text(name, 100) or "Kalender",
                       account_id=cfg.id, remote_url=href, read_only=read_only,
                       color=color or "#4C8DF6")
        return self.db.save_calendar(cal)
