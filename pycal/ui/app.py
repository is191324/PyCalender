"""Kivy-Anwendung: setzt Screens, Theme und Hintergrunddienste zusammen."""
from __future__ import annotations

import threading
from datetime import timedelta

from kivy.app import App
from kivy.clock import Clock
from kivy.core.window import Window
from kivy.uix.floatlayout import FloatLayout
from kivy.uix.screenmanager import FadeTransition, ScreenManager

from ..core import CalendarApp
from ..theme import Theme
from .screens import (AccountsScreen, AgendaScreen, EditorScreen, ImportScreen,
                      MonthScreen, PinScreen, SettingsScreen)
from .widgets import BackgroundCanvas


class PyCalendarApp(App):
    title = "PyCalendar"

    def __init__(self, data_dir=None, **kwargs):
        super().__init__(**kwargs)
        self.logic = CalendarApp(data_dir)
        self._sync_event = None

    # ------------------------------------------------------------------
    def build(self):
        root = FloatLayout()
        self.background = BackgroundCanvas(self.logic.themes,
                                           size_hint=(1, 1))
        root.add_widget(self.background)

        self.sm = ScreenManager(transition=FadeTransition(duration=0.15))
        for screen_cls in (MonthScreen, AgendaScreen, EditorScreen, ImportScreen,
                           AccountsScreen, SettingsScreen, PinScreen):
            self.sm.add_widget(screen_cls(self.logic))
        root.add_widget(self.sm)

        self.logic.themes.bind(self._on_theme)
        self._on_theme(self.logic.themes.theme)

        self.sm.current = "pin" if self.logic.pin_enabled else "month"
        Window.bind(on_keyboard=self._on_key)
        # Automatische Synchronisierung alle 30 Minuten
        self._sync_event = Clock.schedule_interval(
            lambda _dt: self.background_sync(), 30 * 60)
        Clock.schedule_once(lambda _dt: self.background_sync(), 5)
        return root

    def _on_theme(self, theme: Theme):
        Window.clearcolor = theme.rgba("background")

    def _on_key(self, _window, key, *_args):
        if key == 27:                       # Android: Zurück-Taste
            if self.sm.current not in ("month", "pin"):
                self.sm.transition.direction = "right"
                self.sm.current = "month"
                return True
        return False

    # ------------------------------------------------------------------
    def background_sync(self):
        """Synchronisiert in einem Hintergrund-Thread (UI bleibt flüssig)."""
        if not self.logic.db.list_accounts():
            return

        def work():
            try:
                self.logic.sync.sync_all()
            except Exception:
                pass
            Clock.schedule_once(lambda _dt: self._refresh_current(), 0)

        threading.Thread(target=work, daemon=True).start()

    def _refresh_current(self):
        screen = self.sm.current_screen
        if hasattr(screen, "refresh"):
            try:
                screen.refresh()
            except Exception:
                pass

    # ------------------------------------------------------------------
    def on_pause(self):
        return True                     # Android: App im Hintergrund behalten

    def on_resume(self):
        if self.logic.pin_enabled:
            self.sm.current = "pin"
        return True

    def on_stop(self):
        if self._sync_event:
            self._sync_event.cancel()
        self.logic.close()
        return True


def run(data_dir=None):
    PyCalendarApp(data_dir).run()
