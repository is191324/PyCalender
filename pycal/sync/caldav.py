"""Minimaler CalDAV-Client (RFC 4791) für Google, Outlook und Nextcloud.

Nur die vier Operationen, die eine Kalender-App braucht:
  discover()  - Kalender unter einer Startadresse finden
  list()      - href + etag aller Termine holen
  fetch()     - iCalendar-Daten einzelner Termine laden
  put()/delete() - Termine schreiben bzw. löschen (mit ETag-Schutz)

XML wird bewusst NICHT mit einem generischen Parser gelesen, sondern mit
`defusedxml` (falls vorhanden) bzw. einem restriktiven ElementTree-Aufruf
ohne Entity-Aufloesung - damit sind XXE und Billion-Laughs ausgeschlossen.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urljoin, quote

from ..security import SecurityError, sanitize_text
from .http import SecureHttpClient, HttpError

try:
    from defusedxml import ElementTree as SafeET      # bevorzugt
    _DEFUSED = True
except ImportError:                                    # pragma: no cover
    import xml.etree.ElementTree as SafeET
    _DEFUSED = False

DAV_NS = "{DAV:}"
CAL_NS = "{urn:ietf:params:xml:ns:caldav}"

PROPFIND_CALENDARS = """<?xml version="1.0" encoding="utf-8"?>
<d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav"
            xmlns:cs="http://apple.com/ns/ical/">
  <d:prop>
    <d:resourcetype/><d:displayname/><d:current-user-privilege-set/>
    <cs:calendar-color/><c:supported-calendar-component-set/>
  </d:prop>
</d:propfind>"""

REPORT_ETAGS = """<?xml version="1.0" encoding="utf-8"?>
<c:calendar-query xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">
  <d:prop><d:getetag/></d:prop>
  <c:filter><c:comp-filter name="VCALENDAR">
    <c:comp-filter name="VEVENT">{range}</c:comp-filter>
  </c:comp-filter></c:filter>
</c:calendar-query>"""

MULTIGET = """<?xml version="1.0" encoding="utf-8"?>
<c:calendar-multiget xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">
  <d:prop><d:getetag/><c:calendar-data/></d:prop>
  {hrefs}
</c:calendar-multiget>"""


@dataclass
class RemoteCalendar:
    href: str
    name: str
    color: str = ""
    read_only: bool = False


@dataclass
class RemoteItem:
    href: str
    etag: str = ""
    data: str = ""


def _parse_xml(text: str):
    if not text.strip():
        raise SecurityError("Leere Serverantwort.")
    if len(text) > 20 * 1024 * 1024:
        raise SecurityError("XML-Antwort ist zu groß.")
    if not _DEFUSED:
        # Grobfilter gegen Entity-Angriffe, wenn defusedxml fehlt
        head = text[:4096].upper()
        if "<!ENTITY" in head or "<!DOCTYPE" in head:
            raise SecurityError(
                "Serverantwort enthält eine DTD - aus Sicherheitsgründen "
                "abgelehnt. Bitte 'defusedxml' installieren.")
    try:
        return SafeET.fromstring(text)
    except Exception as exc:
        raise SecurityError(f"Serverantwort nicht lesbar: {exc}") from exc


class CalDavClient:
    def __init__(self, base_url: str, username: str = "", password: str = "",
                 allow_insecure: bool = False):
        self.base_url = base_url
        self.http = SecureHttpClient(username, password, allow_insecure)

    def close(self) -> None:
        self.http.close()

    # ------------------------------------------------------------------
    def discover(self) -> list[RemoteCalendar]:
        status, _, text, url = self.http.request(
            "PROPFIND", self.base_url, PROPFIND_CALENDARS,
            {"Depth": "1", "Content-Type": "application/xml; charset=utf-8"})
        if status == 401:
            raise HttpError("Anmeldung abgelehnt (401). Bitte Benutzername und "
                            "App-Passwort prüfen.")
        if status not in (207, 200):
            raise HttpError(f"Server antwortete mit Status {status}.")
        root = _parse_xml(text)
        out: list[RemoteCalendar] = []
        for resp in root.findall(f"{DAV_NS}response"):
            href_el = resp.find(f"{DAV_NS}href")
            if href_el is None or not href_el.text:
                continue
            props = resp.find(f"{DAV_NS}propstat/{DAV_NS}prop")
            if props is None:
                continue
            rtype = props.find(f"{DAV_NS}resourcetype")
            if rtype is None or rtype.find(f"{CAL_NS}calendar") is None:
                continue
            name_el = props.find(f"{DAV_NS}displayname")
            color_el = props.find("{http://apple.com/ns/ical/}calendar-color")
            privs = props.find(f"{DAV_NS}current-user-privilege-set")
            writable = True
            if privs is not None:
                names = {p.tag for p in privs.iter()}
                writable = f"{DAV_NS}write" in names or f"{DAV_NS}all" in names
            out.append(RemoteCalendar(
                href=urljoin(url, href_el.text.strip()),
                name=sanitize_text(name_el.text if name_el is not None else "", 100)
                or "Kalender",
                color=(color_el.text or "")[:9] if color_el is not None else "",
                read_only=not writable))
        return out

    def list_items(self, calendar_href: str, start: Optional[str] = None,
                   end: Optional[str] = None) -> list[RemoteItem]:
        rng = ""
        if start and end:
            rng = f'<c:time-range start="{start}" end="{end}"/>'
        body = REPORT_ETAGS.format(range=rng)
        status, _, text, url = self.http.request(
            "REPORT", calendar_href, body,
            {"Depth": "1", "Content-Type": "application/xml; charset=utf-8"})
        if status not in (207, 200):
            raise HttpError(f"REPORT fehlgeschlagen (Status {status}).")
        root = _parse_xml(text)
        items = []
        for resp in root.findall(f"{DAV_NS}response"):
            href_el = resp.find(f"{DAV_NS}href")
            etag_el = resp.find(f"{DAV_NS}propstat/{DAV_NS}prop/{DAV_NS}getetag")
            if href_el is None or not href_el.text:
                continue
            items.append(RemoteItem(
                href=urljoin(url, href_el.text.strip()),
                etag=(etag_el.text or "").strip('"') if etag_el is not None else ""))
        return items

    def fetch(self, calendar_href: str, hrefs: list[str],
              chunk_size: int = 50) -> list[RemoteItem]:
        out: list[RemoteItem] = []
        for i in range(0, len(hrefs), chunk_size):
            chunk = hrefs[i:i + chunk_size]
            body = MULTIGET.format(hrefs="".join(
                f"<d:href>{_xml_escape(h)}</d:href>" for h in chunk))
            status, _, text, url = self.http.request(
                "REPORT", calendar_href, body,
                {"Depth": "1", "Content-Type": "application/xml; charset=utf-8"})
            if status not in (207, 200):
                raise HttpError(f"Abruf fehlgeschlagen (Status {status}).")
            root = _parse_xml(text)
            for resp in root.findall(f"{DAV_NS}response"):
                href_el = resp.find(f"{DAV_NS}href")
                prop = resp.find(f"{DAV_NS}propstat/{DAV_NS}prop")
                if href_el is None or prop is None:
                    continue
                data_el = prop.find(f"{CAL_NS}calendar-data")
                etag_el = prop.find(f"{DAV_NS}getetag")
                out.append(RemoteItem(
                    href=urljoin(url, href_el.text.strip()),
                    etag=(etag_el.text or "").strip('"') if etag_el is not None else "",
                    data=data_el.text or "" if data_el is not None else ""))
        return out

    def put(self, calendar_href: str, uid: str, ics: str,
            etag: str = "") -> tuple[str, str]:
        """Legt an oder aktualisiert. Gibt (href, neuer_etag) zurück."""
        safe_uid = re.sub(r"[^A-Za-z0-9._@\-]", "_", sanitize_text(uid, 200))
        href = urljoin(calendar_href.rstrip("/") + "/", quote(f"{safe_uid}.ics"))
        headers = {"Content-Type": "text/calendar; charset=utf-8"}
        # Optimistic Locking: keine fremden Änderungen überschreiben
        headers["If-Match"] = f'"{etag}"' if etag else None
        if not etag:
            headers["If-None-Match"] = "*"
        headers = {k: v for k, v in headers.items() if v is not None}
        status, resp_headers, _, _ = self.http.request("PUT", href, ics, headers)
        if status == 412:
            raise ConflictError("Der Termin wurde auf dem Server geändert.")
        if status not in (200, 201, 204):
            raise HttpError(f"Speichern fehlgeschlagen (Status {status}).")
        return href, (resp_headers.get("ETag", "") or "").strip('"')

    def delete(self, href: str, etag: str = "") -> None:
        headers = {"If-Match": f'"{etag}"'} if etag else {}
        status, _, _, _ = self.http.request("DELETE", href, None, headers)
        if status == 412:
            raise ConflictError("Der Termin wurde auf dem Server geändert.")
        if status not in (200, 204, 404):
            raise HttpError(f"Löschen fehlgeschlagen (Status {status}).")


class ConflictError(SecurityError):
    pass


def _xml_escape(value: str) -> str:
    return (str(value).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))
