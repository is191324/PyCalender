"""Wiederverwendbare, themenfaehige Kivy-Widgets."""
from __future__ import annotations

from kivy.clock import Clock
from kivy.graphics import Color, Rectangle, RoundedRectangle
from kivy.metrics import dp, sp
from kivy.properties import (BooleanProperty, ListProperty, NumericProperty,
                             ObjectProperty, StringProperty)
from kivy.uix.behaviors import ButtonBehavior
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.label import Label
from kivy.uix.textinput import TextInput
from kivy.uix.widget import Widget

from ..theme import Theme, hex_to_rgba


class ThemedMixin:
    """Bindet ein Widget an den ThemeManager der App."""

    def attach_theme(self, manager):
        self.theme_manager = manager
        manager.bind(self._on_theme)
        self._on_theme(manager.theme)

    def _on_theme(self, theme: Theme):
        pass


class Card(BoxLayout, ThemedMixin):
    bg_color = ListProperty([1, 1, 1, 1])
    radius = NumericProperty(dp(14))

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        with self.canvas.before:
            self._color = Color(*self.bg_color)
            self._rect = RoundedRectangle(pos=self.pos, size=self.size,
                                          radius=[self.radius])
        self.bind(pos=self._redraw, size=self._redraw,
                  bg_color=self._redraw, radius=self._redraw)
        # Nach dem ersten Layoutdurchlauf einmal sicher neu zeichnen -
        # sonst bleiben einzelne Kacheln beim ersten Frame ungefuellt.
        Clock.schedule_once(self._redraw, 0)

    def _redraw(self, *_):
        self._color.rgba = self.bg_color
        self._rect.pos = self.pos
        self._rect.size = self.size
        self._rect.radius = [self.radius]

    def _on_theme(self, theme: Theme):
        self.bg_color = list(theme.rgba("surface"))
        self.radius = dp(theme.corner_radius)


class ThemedLabel(Label, ThemedMixin):
    role = StringProperty("text")          # text | text_muted | accent_text

    def _on_theme(self, theme: Theme):
        self.color = list(theme.rgba(self.role))
        self.font_size = sp(15) * theme.font_scale


class FlatButton(ButtonBehavior, Label, ThemedMixin):
    """Button ohne Bilder - Farben kommen vollstaendig aus dem Theme."""
    bg_color = ListProperty([0.3, 0.55, 0.95, 1])
    text_role = StringProperty("accent_text")
    fill_role = StringProperty("accent")
    radius = NumericProperty(dp(12))

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        with self.canvas.before:
            self._color = Color(*self.bg_color)
            self._rect = RoundedRectangle(pos=self.pos, size=self.size,
                                          radius=[self.radius])
        self.bind(pos=self._redraw, size=self._redraw, bg_color=self._redraw,
                  radius=self._redraw)
        Clock.schedule_once(self._redraw, 0)

    def _redraw(self, *_):
        self._color.rgba = self.bg_color
        self._rect.pos = self.pos
        self._rect.size = self.size
        self._rect.radius = [self.radius]

    def _on_theme(self, theme: Theme):
        self.bg_color = list(theme.rgba(self.fill_role))
        self.color = list(theme.rgba(self.text_role))
        self.radius = dp(theme.corner_radius)
        self.font_size = sp(15) * theme.font_scale

    def on_press(self):
        self._color.rgba = [min(1, c * 1.15) for c in self.bg_color[:3]] + \
                           [self.bg_color[3]]

    def on_release(self):
        self._color.rgba = self.bg_color


class ThemedInput(TextInput, ThemedMixin):
    def _on_theme(self, theme: Theme):
        self.background_color = list(theme.rgba("surface_alt"))
        self.foreground_color = list(theme.rgba("text"))
        self.cursor_color = list(theme.rgba("accent"))
        self.hint_text_color = list(theme.rgba("text_muted"))
        self.font_size = sp(15) * theme.font_scale


class ColorSwatch(ButtonBehavior, Widget, ThemedMixin):
    """Farbfeld in der Themeneinstellung."""
    hex_color = StringProperty("#4C8DF6")

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        with self.canvas:
            self._color = Color(*hex_to_rgba(self.hex_color))
            self._rect = RoundedRectangle(pos=self.pos, size=self.size,
                                          radius=[dp(8)])
        self.bind(pos=self._redraw, size=self._redraw, hex_color=self._redraw)
        Clock.schedule_once(self._redraw, 0)

    def _redraw(self, *_):
        self._color.rgba = hex_to_rgba(self.hex_color)
        # quadratisches, mittig sitzendes Farbfeld - unabhängig von der Zeilenhöhe
        side = max(dp(16), min(self.width, self.height) - dp(10))
        self._rect.size = (side, side)
        self._rect.pos = (self.x + (self.width - side) / 2,
                          self.y + (self.height - side) / 2)


class BackgroundCanvas(Widget):
    """Zeichnet den konfigurierbaren App-Hintergrund (Farbe/Verlauf/Bild)."""

    def __init__(self, theme_manager, **kwargs):
        super().__init__(**kwargs)
        self.theme_manager = theme_manager
        self._image = None
        with self.canvas.before:
            self._color = Color(*theme_manager.theme.rgba("background"))
            self._rect = Rectangle(pos=self.pos, size=self.size)
            self._img_color = Color(1, 1, 1, 0)
            self._img_rect = Rectangle(pos=self.pos, size=self.size)
        self.bind(pos=self._redraw, size=self._redraw)
        theme_manager.bind(lambda t: self._redraw())

    def _redraw(self, *_):
        theme = self.theme_manager.theme
        self._color.rgba = theme.rgba("background")
        self._rect.pos = self.pos
        self._rect.size = self.size
        self._img_rect.pos = self.pos
        self._img_rect.size = self.size
        if theme.background_mode == "image" and theme.background_image:
            self._img_rect.source = theme.background_image
            self._img_color.rgba = (1, 1, 1, theme.background_opacity)
        else:
            self._img_color.rgba = (1, 1, 1, 0)


def toast(message: str, duration: float = 2.5):
    """Kurze Rückmeldung - fällt auf ein Popup zurück, wenn Plyer fehlt."""
    try:
        from kivy.uix.label import Label as _L
        from kivy.animation import Animation
        from kivy.core.window import Window
        lbl = _L(text=str(message)[:200], size_hint=(None, None),
                 size=(Window.width * 0.8, dp(48)), halign="center",
                 valign="middle")
        lbl.pos = (Window.width * 0.1, dp(80))
        Window.add_widget(lbl)
        anim = Animation(opacity=1, d=0.2) + Animation(opacity=0, d=duration)
        anim.bind(on_complete=lambda *_: Window.remove_widget(lbl))
        anim.start(lbl)
    except Exception:
        print(message)
