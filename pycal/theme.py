"""Konfigurierbares Erscheinungsbild.

Alle Farben und der Hintergrund sind zur Laufzeit aenderbar und werden in
der Datenbank gespeichert. Ein Theme besteht aus:

* einer Farbpalette (Hintergrund, Flächen, Text, Akzent, Raster, Heute,
  Wochenende, Fehler)
* einem Hintergrundmodus: Volltonfarbe, Verlauf oder Bild
* optionaler Bilddeckkraft und Eckenrundung
* Schriftgroessen-Skalierung

Sicherheit: Farbwerte werden streng validiert (nur #RGB/#RRGGBB/#RRGGBBAA),
Bildpfade müssen innerhalb des App-Datenverzeichnisses liegen und eine
erlaubte Bildendung tragen. Damit kann ein importiertes oder
synchronisiertes Theme keine beliebige Datei referenzieren.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, asdict, field, fields as dc_fields
from pathlib import Path
from typing import Optional

from .security import SecurityError, sanitize_text

_HEX_RE = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$")
ALLOWED_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}
MAX_BACKGROUND_BYTES = 12 * 1024 * 1024


def validate_color(value: str, fallback: str = "#000000") -> str:
    v = sanitize_text(value, 12).strip()
    if not v:
        return fallback
    if not v.startswith("#"):
        v = "#" + v
    if not _HEX_RE.match(v):
        return fallback
    return v.upper()


def hex_to_rgba(value: str, alpha: Optional[float] = None) -> tuple:
    """Wandelt #RRGGBB(AA) in Kivys 0..1-Tupel."""
    v = validate_color(value)[1:]
    if len(v) == 3:
        v = "".join(c * 2 for c in v)
    r = int(v[0:2], 16) / 255.0
    g = int(v[2:4], 16) / 255.0
    b = int(v[4:6], 16) / 255.0
    a = int(v[6:8], 16) / 255.0 if len(v) == 8 else 1.0
    if alpha is not None:
        a = max(0.0, min(1.0, float(alpha)))
    return (r, g, b, a)


def relative_luminance(value: str) -> float:
    r, g, b, _ = hex_to_rgba(value)

    def lin(c):
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b)


def contrast_ratio(fg: str, bg: str) -> float:
    l1, l2 = relative_luminance(fg), relative_luminance(bg)
    hi, lo = max(l1, l2), min(l1, l2)
    return (hi + 0.05) / (lo + 0.05)


def readable_text_color(background: str) -> str:
    return "#111111" if relative_luminance(background) > 0.45 else "#FFFFFF"


@dataclass
class Theme:
    name: str = "Standard hell"
    background: str = "#F6F7FB"
    surface: str = "#FFFFFF"
    surface_alt: str = "#EEF1F7"
    text: str = "#16181D"
    text_muted: str = "#6B7280"
    accent: str = "#4C8DF6"
    accent_text: str = "#FFFFFF"
    grid: str = "#D8DCE6"
    today: str = "#FFE9A8"
    weekend: str = "#F0F2F7"
    danger: str = "#D64545"
    success: str = "#2E9E6B"
    # Hintergrund
    background_mode: str = "solid"          # solid | gradient | image
    gradient_to: str = "#DDE6FF"
    background_image: str = ""
    background_opacity: float = 0.35
    # Form & Schrift
    corner_radius: int = 14
    font_scale: float = 1.0
    dark: bool = False

    # ---------------- Validierung ----------------
    def validated(self, data_dir: Optional[Path] = None) -> "Theme":
        defaults = Theme()
        for f in dc_fields(self):
            value = getattr(self, f.name)
            if f.name in {"name"}:
                setattr(self, f.name, sanitize_text(value, 60) or defaults.name)
            elif f.name in {"background_mode"}:
                setattr(self, f.name, value if value in
                        {"solid", "gradient", "image"} else "solid")
            elif f.name == "background_image":
                setattr(self, f.name, self._validate_image(value, data_dir))
            elif f.name == "background_opacity":
                setattr(self, f.name, max(0.0, min(1.0, float(value or 0))))
            elif f.name == "corner_radius":
                setattr(self, f.name, max(0, min(40, int(value or 0))))
            elif f.name == "font_scale":
                setattr(self, f.name, max(0.7, min(1.8, float(value or 1.0))))
            elif f.name == "dark":
                setattr(self, f.name, bool(value))
            else:
                setattr(self, f.name, validate_color(value, getattr(defaults, f.name)))
        return self

    @staticmethod
    def _validate_image(value, data_dir: Optional[Path]) -> str:
        path = sanitize_text(value, 1024)
        if not path:
            return ""
        p = Path(path)
        if p.suffix.lower() not in ALLOWED_IMAGE_SUFFIXES:
            return ""
        try:
            resolved = p.resolve()
        except OSError:
            return ""
        if data_dir is not None:
            base = Path(data_dir).resolve()
            if base not in resolved.parents and resolved != base:
                return ""          # Pfad ausserhalb des App-Verzeichnisses
        if not resolved.is_file():
            return ""
        if resolved.stat().st_size > MAX_BACKGROUND_BYTES:
            return ""
        return str(resolved)

    # ---------------- Kivy-Helfer ----------------
    def rgba(self, key: str, alpha: Optional[float] = None) -> tuple:
        return hex_to_rgba(getattr(self, key, "#000000"), alpha)

    def contrast_warnings(self) -> list[str]:
        """Hinweise für die Einstellungsseite (Barrierefreiheit)."""
        out = []
        if contrast_ratio(self.text, self.background) < 4.5:
            out.append("Textfarbe hebt sich kaum vom Hintergrund ab (< 4.5:1).")
        if contrast_ratio(self.accent_text, self.accent) < 3.0:
            out.append("Schrift auf der Akzentfarbe ist schwer lesbar.")
        if contrast_ratio(self.text, self.surface) < 4.5:
            out.append("Text auf Flächen ist schwer lesbar.")
        return out

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @classmethod
    def from_json(cls, raw: str, data_dir: Optional[Path] = None) -> "Theme":
        try:
            data = json.loads(raw or "{}")
        except (ValueError, TypeError):
            return cls()
        if not isinstance(data, dict):
            return cls()
        known = {f.name for f in dc_fields(cls)}
        clean = {k: v for k, v in data.items() if k in known}
        return cls(**clean).validated(data_dir)


PRESETS: dict[str, Theme] = {
    "Hell": Theme(),
    "Dunkel": Theme(
        name="Dunkel", background="#12141A", surface="#1C1F27",
        surface_alt="#252935", text="#EDEFF5", text_muted="#9AA2B4",
        accent="#5B9DFF", accent_text="#0A0C10", grid="#2E3341",
        today="#3A3256", weekend="#181B23", danger="#FF6B6B",
        success="#45C08A", gradient_to="#1E2436", dark=True),
    "Mitternachtsblau": Theme(
        name="Mitternachtsblau", background="#0B1A2E", surface="#132844",
        surface_alt="#1B3557", text="#E8F0FC", text_muted="#8FA9C8",
        accent="#FFB454", accent_text="#1A1200", grid="#234872",
        today="#2A4E7D", weekend="#0E2138", danger="#FF7A7A",
        success="#57D39B", background_mode="gradient", gradient_to="#06101D",
        dark=True),
    "Waldgrün": Theme(
        name="Waldgrün", background="#F2F7F1", surface="#FFFFFF",
        surface_alt="#E4EFE2", text="#17251A", text_muted="#5B6F5E",
        accent="#3B8C5A", accent_text="#FFFFFF", grid="#C9DCC7",
        today="#DDF0CF", weekend="#EAF2E8", danger="#C1554A",
        success="#3B8C5A"),
    "Sepia": Theme(
        name="Sepia", background="#F6EFE2", surface="#FFF9EF",
        surface_alt="#EDE2CE", text="#33291B", text_muted="#7A6A53",
        accent="#B5772F", accent_text="#FFFFFF", grid="#DCCDB2",
        today="#F2DFA8", weekend="#EFE6D5", danger="#B04A3A",
        success="#6E8C3F"),
    "Kontraststark": Theme(
        name="Kontraststark", background="#000000", surface="#000000",
        surface_alt="#141414", text="#FFFFFF", text_muted="#D0D0D0",
        accent="#FFD400", accent_text="#000000", grid="#7A7A7A",
        today="#004C99", weekend="#101010", danger="#FF4A4A",
        success="#00D27A", corner_radius=4, font_scale=1.15, dark=True),
}


class ThemeManager:
    """Laedt/speichert das aktive Theme und benachrichtigt die UI."""

    SETTING_KEY = "theme"

    def __init__(self, db, data_dir):
        self.db = db
        self.data_dir = Path(data_dir)
        self._listeners: list = []
        self.theme = Theme.from_json(db.get_setting(self.SETTING_KEY, ""),
                                     self.data_dir)

    def bind(self, callback) -> None:
        self._listeners.append(callback)

    def _notify(self) -> None:
        for cb in list(self._listeners):
            try:
                cb(self.theme)
            except Exception:
                pass

    def apply(self, theme: Theme) -> Theme:
        self.theme = theme.validated(self.data_dir)
        self.db.set_setting(self.SETTING_KEY, self.theme.to_json())
        self._notify()
        return self.theme

    def apply_preset(self, name: str) -> Theme:
        preset = PRESETS.get(name)
        if preset is None:
            raise SecurityError(f"Unbekanntes Farbschema: {name}")
        import copy
        return self.apply(copy.deepcopy(preset))

    def update(self, **changes) -> Theme:
        import copy
        theme = copy.deepcopy(self.theme)
        known = {f.name for f in dc_fields(Theme)}
        for key, value in changes.items():
            if key in known:
                setattr(theme, key, value)
        return self.apply(theme)

    def set_background_image(self, source_path) -> Theme:
        """Kopiert ein Bild in das App-Verzeichnis und setzt es als Hintergrund."""
        src = Path(source_path)
        if src.suffix.lower() not in ALLOWED_IMAGE_SUFFIXES:
            raise SecurityError("Nur PNG-, JPG- oder WEBP-Bilder sind erlaubt.")
        if not src.is_file():
            raise SecurityError("Bilddatei nicht gefunden.")
        if src.stat().st_size > MAX_BACKGROUND_BYTES:
            raise SecurityError("Bild ist zu groß (max. 12 MB).")
        target_dir = self.data_dir / "backgrounds"
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / f"background{src.suffix.lower()}"
        target.write_bytes(src.read_bytes()[:MAX_BACKGROUND_BYTES])
        return self.update(background_image=str(target), background_mode="image")

    def clear_background_image(self) -> Theme:
        return self.update(background_image="", background_mode="solid")

    def export_theme(self) -> str:
        return self.theme.to_json()

    def import_theme(self, raw: str) -> Theme:
        return self.apply(Theme.from_json(raw, self.data_dir))
