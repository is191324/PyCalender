"""Gehärteter HTTP-Client für CalDAV/ICS.

Sicherheitsmerkmale:
* TLS-Zertifikatspruefung ist fest verdrahtet (verify=True) und lässt
  sich durch keine Einstellung und keinen Parameter abschalten
* jede Ziel-URL laeuft durch `validate_sync_url` (SSRF-Schutz), auch nach
  jedem Redirect (Redirects werden manuell verfolgt, max. 3)
* harte Obergrenze für die Antwortgroesse (Streaming statt r.content)
* Timeouts für Verbindung und Lesen
* Basic-Auth nur über https; Zugangsdaten werden nie geloggt
"""
from __future__ import annotations

import base64
from typing import Optional

from ..security import (HTTP_TIMEOUT, MAX_HTTP_REDIRECTS, MAX_ICS_BYTES,
                        SecurityError, redact, validate_sync_url)

try:
    import requests
except ImportError:      # pragma: no cover
    requests = None


class HttpError(SecurityError):
    pass


class SecureHttpClient:
    def __init__(self, username: str = "", password: str = "",
                 allow_insecure: bool = False, timeout: int = HTTP_TIMEOUT,
                 max_bytes: int = MAX_ICS_BYTES, user_agent: str = "PyCalendar/1.0"):
        if requests is None:
            raise HttpError("Paket 'requests' ist nicht installiert.")
        self.username = username or ""
        self.password = password or ""
        self.allow_insecure = bool(allow_insecure)
        self.timeout = timeout
        self.max_bytes = max_bytes
        self.session = requests.Session()
        self.session.max_redirects = 0
        self.session.headers.update({"User-Agent": user_agent,
                                     "Accept-Encoding": "gzip"})

    # ------------------------------------------------------------------
    def _auth_header(self, url: str) -> dict:
        if not self.username:
            return {}
        # SICHERHEITSFIX PT-C4: Basic-Auth wird ausnahmslos nur über TLS
        # gesendet. `allow_insecure` (Entwicklungsschalter) hebt diese Regel
        # bewusst NICHT auf - sonst würde ein Downgrade-Angriff das Passwort
        # im Klartext über das Netz schicken.
        if not url.lower().startswith("https://"):
            raise HttpError("Zugangsdaten werden ausschliesslich über HTTPS "
                            "gesendet - die Verbindung ist unverschlüsselt.")
        token = base64.b64encode(
            f"{self.username}:{self.password}".encode("utf-8")).decode("ascii")
        return {"Authorization": f"Basic {token}"}

    def request(self, method: str, url: str, body: Optional[str] = None,
                headers: Optional[dict] = None, send_auth: bool = True):
        current = validate_sync_url(url, allow_insecure=self.allow_insecure)
        hdrs = dict(headers or {})
        if send_auth:
            hdrs.update(self._auth_header(current))
        data = body.encode("utf-8") if isinstance(body, str) else body

        for hop in range(MAX_HTTP_REDIRECTS + 1):
            try:
                resp = self.session.request(
                    method, current, data=data, headers=hdrs,
                    timeout=(self.timeout, self.timeout), allow_redirects=False,
                    stream=True, verify=True)
            except Exception as exc:      # requests.RequestException & TLS-Fehler
                raise HttpError(f"Verbindung fehlgeschlagen: {redact(str(exc))}") from exc

            if resp.status_code in (301, 302, 303, 307, 308):
                location = resp.headers.get("Location", "")
                resp.close()
                if not location or hop >= MAX_HTTP_REDIRECTS:
                    raise HttpError("Zu viele Weiterleitungen.")
                current = validate_sync_url(
                    _absolutize(current, location), allow_insecure=self.allow_insecure)
                # Auth-Header nach Host-Wechsel neu berechnen (kein Leak!)
                hdrs = dict(headers or {})
                if send_auth:
                    hdrs.update(self._auth_header(current))
                continue

            declared = resp.headers.get("Content-Length")
            if declared and declared.isdigit() and int(declared) > self.max_bytes:
                resp.close()
                raise HttpError("Antwort ist zu groß.")
            chunks, total = [], 0
            for chunk in resp.iter_content(64 * 1024):
                total += len(chunk)
                if total > self.max_bytes:
                    resp.close()
                    raise HttpError("Antwort überschreitet das Größenlimit.")
                chunks.append(chunk)
            resp.close()
            text = b"".join(chunks).decode("utf-8", errors="replace")
            return resp.status_code, resp.headers, text, current
        raise HttpError("Zu viele Weiterleitungen.")

    def close(self) -> None:
        try:
            self.session.close()
        except Exception:
            pass


def _absolutize(base: str, location: str) -> str:
    from urllib.parse import urljoin
    return urljoin(base, location)
