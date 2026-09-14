"""Alle Bildschirme der App."""
from __future__ import annotations

import calendar as pycalendar
import threading
from datetime import datetime, timedelta, timezone

from kivy.clock import Clock, mainthread
from kivy.metrics import dp, sp
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.gridlayout import GridLayout
from kivy.uix.popup import Popup
from kivy.uix.screenmanager import Screen
from kivy.uix.scrollview import ScrollView
from kivy.uix.slider import Slider
from kivy.uix.spinner import Spinner
from kivy.uix.switch import Switch

from ..models import Event, Recurrence, WEEKDAYS, ensure_aware, utcnow
from ..security import SecurityError
from ..sync.manager import PROVIDER_PRESETS, AccountConfig
from ..theme import PRESETS, Theme, validate_color
from .widgets import Card, ColorSwatch, FlatButton, ThemedInput, ThemedLabel, toast

WEEKDAY_LABELS = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]
MONTH_NAMES = ["Januar", "Februar", "März", "April", "Mai", "Juni", "Juli",
               "August", "September", "Oktober", "November", "Dezember"]


class BaseScreen(Screen):
    """Gemeinsame Basis: Theme-Anbindung und Kopfzeile."""

    def __init__(self, app, **kwargs):
        super().__init__(**kwargs)
        self.app = app                 # CalendarApp (Logik)
        self.themes = app.themes
        self.root_box = BoxLayout(orientation="vertical", spacing=dp(6),
                                  padding=dp(10))
        self.add_widget(self.root_box)
        self.themes.bind(self.on_theme)

    def on_theme(self, theme: Theme):
        pass

    def themed(self, widget):
        widget.attach_theme(self.themes)
        return widget

    def header(self, title: str, right_buttons=None) -> BoxLayout:
        bar = BoxLayout(size_hint_y=None, height=dp(52), spacing=dp(6))
        lbl = self.themed(ThemedLabel(text=f"[b]{title}[/b]", markup=True,
                                      halign="left", valign="middle"))
        lbl.bind(size=lambda w, *_: setattr(w, "text_size", w.size))
        bar.add_widget(lbl)
        for text, callback in (right_buttons or []):
            btn = self.themed(FlatButton(text=text, size_hint_x=None,
                                         width=dp(52)))
            btn.bind(on_release=lambda _w, cb=callback: cb())
            bar.add_widget(btn)
        return bar

    def nav(self) -> BoxLayout:
        bar = BoxLayout(size_hint_y=None, height=dp(56), spacing=dp(6))
        items = [("Monat", "month"), ("Agenda", "agenda"), ("Import", "import"),
                 ("Konten", "accounts"), ("Design", "settings")]
        for label, target in items:
            btn = self.themed(FlatButton(text=label))
            if self.name == target:
                btn.fill_role, btn.text_role = "accent", "accent_text"
            else:
                btn.fill_role, btn.text_role = "surface_alt", "text"
            btn.bind(on_release=lambda _w, t=target: self.go(t))
            bar.add_widget(btn)
        return bar

    def go(self, target: str, **kwargs):
        self.manager.transition.direction = "left"
        screen = self.manager.get_screen(target)
        if hasattr(screen, "prepare"):
            screen.prepare(**kwargs)
        self.manager.current = target


# --------------------------------------------------------------------------
class MonthScreen(BaseScreen):
    def __init__(self, app, **kwargs):
        super().__init__(app, name="month", **kwargs)
        today = datetime.now(timezone.utc)
        self.year, self.month = today.year, today.month
        self.grid = GridLayout(cols=7, spacing=dp(2))
        self.day_list = BoxLayout(orientation="vertical", size_hint_y=None,
                                  spacing=dp(4))
        self.day_list.bind(minimum_height=self.day_list.setter("height"))
        self.selected = today.date()
        self.build()

    def build(self):
        self.root_box.clear_widgets()
        self.title_label = self.themed(ThemedLabel(text="", markup=True))
        bar = BoxLayout(size_hint_y=None, height=dp(52), spacing=dp(6))
        prev_btn = self.themed(FlatButton(text="<", size_hint_x=None, width=dp(48)))
        next_btn = self.themed(FlatButton(text=">", size_hint_x=None, width=dp(48)))
        today_btn = self.themed(FlatButton(text="Heute", size_hint_x=None,
                                           width=dp(80)))
        add_btn = self.themed(FlatButton(text="+", size_hint_x=None, width=dp(48)))
        prev_btn.bind(on_release=lambda *_: self.shift(-1))
        next_btn.bind(on_release=lambda *_: self.shift(1))
        today_btn.bind(on_release=lambda *_: self.jump_today())
        add_btn.bind(on_release=lambda *_: self.new_event())
        bar.add_widget(prev_btn)
        bar.add_widget(self.title_label)
        bar.add_widget(next_btn)
        bar.add_widget(today_btn)
        bar.add_widget(add_btn)
        self.root_box.add_widget(bar)

        weekdays = GridLayout(cols=7, size_hint_y=None, height=dp(22))
        for label in WEEKDAY_LABELS:
            weekdays.add_widget(self.themed(ThemedLabel(text=label,
                                                        role="text_muted")))
        self.root_box.add_widget(weekdays)
        self.root_box.add_widget(self.grid)

        scroll = ScrollView(size_hint_y=0.45)
        scroll.add_widget(self.day_list)
        self.root_box.add_widget(scroll)
        self.root_box.add_widget(self.nav())
        self.refresh()

    def shift(self, delta: int):
        month = self.month + delta
        self.year += (month - 1) // 12
        self.month = (month - 1) % 12 + 1
        self.refresh()

    def jump_today(self):
        today = datetime.now(timezone.utc)
        self.year, self.month, self.selected = today.year, today.month, today.date()
        self.refresh()

    def new_event(self):
        start = datetime.combine(self.selected, datetime.min.time(),
                                 tzinfo=timezone.utc).replace(hour=9)
        self.go("editor", event=None, start=start)

    def prepare(self, **kwargs):
        self.refresh()

    def on_theme(self, theme):
        self.refresh()

    def refresh(self):
        if not self.manager and not self.parent:
            return
        theme = self.themes.theme
        self.title_label.text = f"[b]{MONTH_NAMES[self.month - 1]} {self.year}[/b]"
        buckets = self.app.month(self.year, self.month)
        self.grid.clear_widgets()
        first_weekday, days_in_month = pycalendar.monthrange(self.year, self.month)
        today = datetime.now(timezone.utc).date()

        for _ in range(first_weekday):
            self.grid.add_widget(BoxLayout())
        for day in range(1, days_in_month + 1):
            date_obj = datetime(self.year, self.month, day).date()
            key = date_obj.isoformat()
            count = len(buckets.get(key, []))
            cell = Card(orientation="vertical", padding=dp(2))
            cell.attach_theme(self.themes)
            if date_obj == today:
                cell.bg_color = list(theme.rgba("today"))
            elif date_obj == self.selected:
                cell.bg_color = list(theme.rgba("accent", 0.35))
            elif date_obj.weekday() >= 5:
                cell.bg_color = list(theme.rgba("weekend"))
            num = self.themed(ThemedLabel(text=str(day), font_size=sp(13)))
            cell.add_widget(num)
            # Immer eine Punktzeile anlegen (auch leer), damit alle Zellen
            # dieselbe Aufteilung haben und die Zahlen auf einer Linie sitzen.
            dots = ("•" * min(count, 4) + ("+" if count > 4 else "")) if count else ""
            cell.add_widget(self.themed(ThemedLabel(text=dots, role="accent",
                                                    font_size=sp(11))))
            btn = FlatButton(text="", size_hint=(1, None), height=dp(0))
            cell.bind(on_touch_down=lambda w, touch, d=date_obj:
                      self.select_day(d) if w.collide_point(*touch.pos) else None)
            self.grid.add_widget(cell)
        self.show_day()

    def select_day(self, date_obj):
        self.selected = date_obj
        self.show_day()

    def show_day(self):
        self.day_list.clear_widgets()
        start = datetime.combine(self.selected, datetime.min.time(),
                                 tzinfo=timezone.utc)
        entries = self.app.day(start)
        head = self.themed(ThemedLabel(
            text=f"[b]{self.selected.strftime('%d.%m.%Y')}[/b] - "
                 f"{len(entries)} Termin(e)", markup=True, size_hint_y=None,
            height=dp(28)))
        self.day_list.add_widget(head)
        if not entries:
            self.day_list.add_widget(self.themed(ThemedLabel(
                text="Keine Termine - tippe auf +", role="text_muted",
                size_hint_y=None, height=dp(32))))
        for occ, ev in entries:
            row = Card(orientation="horizontal", size_hint_y=None, height=dp(56),
                       padding=dp(8), spacing=dp(8))
            row.attach_theme(self.themes)
            time_lbl = self.themed(ThemedLabel(
                text=occ.strftime("%H:%M"), size_hint_x=None, width=dp(56)))
            title = self.themed(ThemedLabel(
                text=f"{ev.title}" + (f"  ({ev.duration_minutes} min)"
                                      if ev.duration_minutes else ""),
                halign="left", valign="middle"))
            title.bind(size=lambda w, *_: setattr(w, "text_size", w.size))
            edit = self.themed(FlatButton(text="Bearb.", size_hint_x=None,
                                          width=dp(70)))
            edit.bind(on_release=lambda _w, e=ev, o=occ:
                      self.go("editor", event=e, occurrence=o))
            row.add_widget(time_lbl)
            row.add_widget(title)
            row.add_widget(edit)
            self.day_list.add_widget(row)


# --------------------------------------------------------------------------
class AgendaScreen(BaseScreen):
    def __init__(self, app, **kwargs):
        super().__init__(app, name="agenda", **kwargs)
        self.list_box = BoxLayout(orientation="vertical", size_hint_y=None,
                                  spacing=dp(4))
        self.list_box.bind(minimum_height=self.list_box.setter("height"))
        self.search_input = self.themed(ThemedInput(
            hint_text="Suchen...", multiline=False, size_hint_y=None, height=dp(42)))
        self.search_input.bind(text=lambda *_: self.refresh())
        self.root_box.add_widget(self.header("Agenda"))
        self.root_box.add_widget(self.search_input)
        scroll = ScrollView()
        scroll.add_widget(self.list_box)
        self.root_box.add_widget(scroll)
        self.root_box.add_widget(self.nav())

    def prepare(self, **kwargs):
        self.refresh()

    def on_pre_enter(self, *_):
        self.refresh()

    def refresh(self):
        self.list_box.clear_widgets()
        query = self.search_input.text.strip()
        if query:
            entries = [(ensure_aware(e.start), e) for e in self.app.search(query)]
        else:
            entries = self.app.agenda(60)
        if not entries:
            self.list_box.add_widget(self.themed(ThemedLabel(
                text="Nichts gefunden.", role="text_muted", size_hint_y=None,
                height=dp(40))))
        for occ, ev in entries[:300]:
            row = Card(orientation="horizontal", size_hint_y=None, height=dp(58),
                       padding=dp(8), spacing=dp(8))
            row.attach_theme(self.themes)
            left = self.themed(ThemedLabel(
                text=occ.strftime("%d.%m.\n%H:%M"), size_hint_x=None, width=dp(64),
                halign="center"))
            mid = self.themed(ThemedLabel(
                text=f"[b]{ev.title}[/b]\n{ev.location or ''}", markup=True,
                halign="left", valign="middle"))
            mid.bind(size=lambda w, *_: setattr(w, "text_size", w.size))
            btn = self.themed(FlatButton(text="Bearb.", size_hint_x=None,
                                         width=dp(70)))
            btn.bind(on_release=lambda _w, e=ev, o=occ:
                     self.go("editor", event=e, occurrence=o))
            row.add_widget(left)
            row.add_widget(mid)
            row.add_widget(btn)
            self.list_box.add_widget(row)


# --------------------------------------------------------------------------
class EditorScreen(BaseScreen):
    """Anlegen/Bearbeiten - hier greift die Mustererkennung."""

    def __init__(self, app, **kwargs):
        super().__init__(app, name="editor", **kwargs)
        self.event = None
        self.occurrence = None
        self.build()

    def build(self):
        self.root_box.clear_widgets()
        self.root_box.add_widget(self.header("Termin", [("X", self.cancel)]))

        form = BoxLayout(orientation="vertical", size_hint_y=None, spacing=dp(6))
        form.bind(minimum_height=form.setter("height"))

        self.title_input = self.themed(ThemedInput(
            hint_text="Titel", multiline=False, size_hint_y=None, height=dp(46)))
        self.title_input.bind(text=self.on_title_change)
        self.hint_label = self.themed(ThemedLabel(
            text="", role="text_muted", size_hint_y=None, height=dp(26),
            halign="left"))
        self.hint_label.bind(size=lambda w, *_: setattr(w, "text_size", w.size))

        self.date_input = self.themed(ThemedInput(
            hint_text="Datum TT.MM.JJJJ", multiline=False, size_hint_y=None,
            height=dp(46)))
        self.time_input = self.themed(ThemedInput(
            hint_text="Uhrzeit HH:MM", multiline=False, size_hint_y=None,
            height=dp(46)))
        self.duration_input = self.themed(ThemedInput(
            hint_text="Dauer in Minuten", multiline=False, size_hint_y=None,
            height=dp(46)))
        self.location_input = self.themed(ThemedInput(
            hint_text="Ort", multiline=False, size_hint_y=None, height=dp(46)))
        self.notes_input = self.themed(ThemedInput(
            hint_text="Notizen", size_hint_y=None, height=dp(80)))

        self.repeat_spinner = Spinner(
            text="einmalig", size_hint_y=None, height=dp(44),
            values=("einmalig", "täglich", "wöchentlich", "alle 2 Wochen",
                    "monatlich", "jährlich"))
        self.calendar_spinner = Spinner(text="", size_hint_y=None, height=dp(44))

        row_dt = BoxLayout(size_hint_y=None, height=dp(46), spacing=dp(6))
        row_dt.add_widget(self.date_input)
        row_dt.add_widget(self.time_input)

        for widget in (self.title_input, self.hint_label, row_dt,
                       self.duration_input, self.location_input,
                       self.repeat_spinner, self.calendar_spinner,
                       self.notes_input):
            form.add_widget(widget)

        self.pin_btn = self.themed(FlatButton(
            text="Dauer für diesen Namen merken", size_hint_y=None, height=dp(44),
            fill_role="surface_alt", text_role="text"))
        self.pin_btn.bind(on_release=lambda *_: self.pin_duration())
        form.add_widget(self.pin_btn)

        scroll = ScrollView()
        scroll.add_widget(form)
        self.root_box.add_widget(scroll)

        actions = BoxLayout(size_hint_y=None, height=dp(54), spacing=dp(6))
        save = self.themed(FlatButton(text="Speichern"))
        save.bind(on_release=lambda *_: self.save())
        self.delete_btn = self.themed(FlatButton(text="Löschen",
                                                 fill_role="danger"))
        self.delete_btn.bind(on_release=lambda *_: self.delete())
        actions.add_widget(save)
        actions.add_widget(self.delete_btn)
        self.root_box.add_widget(actions)

    def prepare(self, event=None, start=None, occurrence=None, **kwargs):
        self.event = event
        self.occurrence = occurrence
        cals = self.app.db.list_calendars()
        self.calendar_spinner.values = [f"{c.id}: {c.name}" for c in cals]
        if event:
            when = ensure_aware(occurrence or event.start)
            self.title_input.text = event.title
            self.date_input.text = when.strftime("%d.%m.%Y")
            self.time_input.text = when.strftime("%H:%M")
            self.duration_input.text = str(event.duration_minutes)
            self.location_input.text = event.location
            self.notes_input.text = event.description
            self.repeat_spinner.text = _rrule_to_label(event.rrule)
            cal = self.app.db.get_calendar(event.calendar_id)
            self.calendar_spinner.text = f"{cal.id}: {cal.name}" if cal else ""
            self.delete_btn.disabled = False
        else:
            when = ensure_aware(start or utcnow())
            self.title_input.text = ""
            self.date_input.text = when.strftime("%d.%m.%Y")
            self.time_input.text = when.strftime("%H:%M")
            self.duration_input.text = ""
            self.location_input.text = ""
            self.notes_input.text = ""
            self.repeat_spinner.text = "einmalig"
            first = cals[0] if cals else None
            self.calendar_spinner.text = f"{first.id}: {first.name}" if first else ""
            self.delete_btn.disabled = True
        self.on_title_change()

    # ---------------- Mustererkennung ----------------
    def on_title_change(self, *_):
        title = self.title_input.text.strip()
        if not title:
            self.hint_label.text = ""
            return
        sug = self.app.suggest(title)
        if not sug or not sug.duration_minutes:
            self.hint_label.text = ""
            return
        parts = [f"Vorschlag: {sug.describe()}"]
        if sug.start_minutes is not None and not self.event:
            h, m = divmod(sug.start_minutes, 60)
            parts.append(f"meist {h:02d}:{m:02d} Uhr")
        rec = self.app.suggest_recurrence(title)
        if rec:
            parts.append(f"Rhythmus: {rec.describe()}")
        self.hint_label.text = " | ".join(parts)
        if not self.duration_input.text.strip():
            self.duration_input.text = str(sug.duration_minutes)
        if not self.location_input.text.strip() and sug.location:
            self.location_input.text = sug.location
        if rec and self.repeat_spinner.text == "einmalig" and not self.event:
            self.repeat_spinner.text = _rrule_to_label(rec.to_rrule())

    def pin_duration(self):
        title = self.title_input.text.strip()
        if not title:
            return
        try:
            minutes = int(self.duration_input.text or "0")
        except ValueError:
            toast("Dauer bitte als Zahl eingeben.")
            return
        self.app.patterns.pin_duration(title, minutes)
        toast(f"Dauer {minutes} min für '{title}' fest hinterlegt.")
        self.on_title_change()

    # ---------------- Speichern ----------------
    def save(self):
        try:
            start = _parse_local(self.date_input.text, self.time_input.text)
        except ValueError as exc:
            toast(str(exc))
            return
        title = self.title_input.text.strip()
        if not title:
            toast("Bitte einen Titel eingeben.")
            return
        try:
            minutes = int(self.duration_input.text or "60")
        except ValueError:
            minutes = 60
        minutes = max(0, min(minutes, 60 * 24 * 14))
        cal_id = int((self.calendar_spinner.text or "1").split(":")[0] or 1)

        ev = self.event or Event()
        ev.title = title
        ev.start = start
        ev.end = start + timedelta(minutes=minutes)
        ev.location = self.location_input.text.strip()
        ev.description = self.notes_input.text.strip()
        ev.rrule = _label_to_rrule(self.repeat_spinner.text)
        ev.calendar_id = cal_id
        self.app.update_event(ev)
        toast("Gespeichert.")
        self.go("month")

    def delete(self):
        if not self.event:
            return
        if self.event.is_recurring and self.occurrence:
            self._confirm_series_delete()
        else:
            self.app.delete_event(self.event.id)
            toast("Termin gelöscht.")
            self.go("month")

    def _confirm_series_delete(self):
        box = BoxLayout(orientation="vertical", spacing=dp(8), padding=dp(10))
        box.add_widget(ThemedLabel(text="Serientermin löschen:"))
        popup = Popup(title="Löschen", content=box, size_hint=(0.9, 0.4))
        one = FlatButton(text="Nur diesen Termin", size_hint_y=None, height=dp(46))
        allev = FlatButton(text="Ganze Serie", size_hint_y=None, height=dp(46))
        cancel = FlatButton(text="Abbrechen", size_hint_y=None, height=dp(46))
        for btn in (one, allev, cancel):
            btn.attach_theme(self.themes)
            box.add_widget(btn)
        one.bind(on_release=lambda *_: (
            self.app.delete_occurrence(self.event.id, self.occurrence),
            popup.dismiss(), self.go("month")))
        allev.bind(on_release=lambda *_: (
            self.app.delete_event(self.event.id), popup.dismiss(), self.go("month")))
        cancel.bind(on_release=lambda *_: popup.dismiss())
        popup.open()

    def cancel(self):
        self.go("month")


def _parse_local(date_text: str, time_text: str) -> datetime:
    date_text = (date_text or "").strip()
    time_text = (time_text or "").strip() or "09:00"
    for fmt in ("%d.%m.%Y", "%Y-%m-%d", "%d.%m.%y"):
        try:
            day = datetime.strptime(date_text, fmt)
            break
        except ValueError:
            continue
    else:
        raise ValueError("Datum bitte als TT.MM.JJJJ eingeben.")
    try:
        hour, minute = [int(x) for x in time_text.replace(".", ":").split(":")[:2]]
    except ValueError:
        raise ValueError("Uhrzeit bitte als HH:MM eingeben.")
    if not (0 <= hour < 24 and 0 <= minute < 60):
        raise ValueError("Uhrzeit ausserhalb des gültigen Bereichs.")
    return day.replace(hour=hour, minute=minute, tzinfo=timezone.utc)


_LABEL_RRULE = {
    "einmalig": "",
    "täglich": "FREQ=DAILY",
    "wöchentlich": "FREQ=WEEKLY",
    "alle 2 Wochen": "FREQ=WEEKLY;INTERVAL=2",
    "monatlich": "FREQ=MONTHLY",
    "jährlich": "FREQ=YEARLY",
}


def _label_to_rrule(label: str) -> str:
    return _LABEL_RRULE.get(label, "")


def _rrule_to_label(rrule: str) -> str:
    rule = (rrule or "").upper()
    for label, value in _LABEL_RRULE.items():
        if value and value == rule:
            return label
    if rule.startswith("FREQ=WEEKLY"):
        return "wöchentlich"
    if rule.startswith("FREQ=DAILY"):
        return "täglich"
    if rule.startswith("FREQ=MONTHLY"):
        return "monatlich"
    if rule.startswith("FREQ=YEARLY"):
        return "jährlich"
    return "einmalig"


# --------------------------------------------------------------------------
class ImportScreen(BaseScreen):
    """CSV-Import mit Vorschau - und vor allem: leichtes Löschen.

    Jeder Import erscheint als Karte mit einem großen Button
    "Import rückgängig". Damit verschwinden genau die Termine dieses
    Imports, ohne dass der Nutzer einzeln suchen muss.
    """

    def __init__(self, app, **kwargs):
        super().__init__(app, name="import", **kwargs)
        self.preview = None
        self.path_input = self.themed(ThemedInput(
            hint_text="Pfad zur CSV-Datei", multiline=False, size_hint_y=None,
            height=dp(46)))
        self.status = self.themed(ThemedLabel(text="", role="text_muted",
                                              size_hint_y=None, height=dp(30)))
        self.body = BoxLayout(orientation="vertical", size_hint_y=None,
                              spacing=dp(6))
        self.body.bind(minimum_height=self.body.setter("height"))

        self.root_box.add_widget(self.header("CSV-Import"))
        self.root_box.add_widget(self.path_input)

        buttons = BoxLayout(size_hint_y=None, height=dp(48), spacing=dp(6))
        pick = self.themed(FlatButton(text="Datei wählen",
                                      fill_role="surface_alt", text_role="text"))
        prev = self.themed(FlatButton(text="Vorschau"))
        imp = self.themed(FlatButton(text="Importieren", fill_role="success"))
        pick.bind(on_release=lambda *_: self.choose_file())
        prev.bind(on_release=lambda *_: self.do_preview())
        imp.bind(on_release=lambda *_: self.do_import())
        for b in (pick, prev, imp):
            buttons.add_widget(b)
        self.root_box.add_widget(buttons)
        self.root_box.add_widget(self.status)

        scroll = ScrollView()
        scroll.add_widget(self.body)
        self.root_box.add_widget(scroll)
        self.root_box.add_widget(self.nav())

    def prepare(self, **kwargs):
        self.refresh_batches()

    def on_pre_enter(self, *_):
        self.refresh_batches()

    # ---------------- Dateiauswahl ----------------
    def choose_file(self):
        try:
            from plyer import filechooser
            selection = filechooser.open_file(filters=[["CSV", "*.csv"]])
            if selection:
                self.path_input.text = selection[0]
                return
        except Exception:
            pass
        from kivy.uix.filechooser import FileChooserListView
        box = BoxLayout(orientation="vertical")
        chooser = FileChooserListView(filters=["*.csv", "*.CSV"])
        box.add_widget(chooser)
        popup = Popup(title="CSV wählen", content=box, size_hint=(0.95, 0.9))
        take = FlatButton(text="Übernehmen", size_hint_y=None, height=dp(48))
        take.attach_theme(self.themes)
        take.bind(on_release=lambda *_: (
            setattr(self.path_input, "text", chooser.selection[0]
                    if chooser.selection else ""), popup.dismiss()))
        box.add_widget(take)
        popup.open()

    # ---------------- Vorschau ----------------
    def do_preview(self):
        path = self.path_input.text.strip()
        if not path:
            toast("Bitte zuerst eine Datei auswaehlen.")
            return
        try:
            self.preview = self.app.preview_csv(path)
        except SecurityError as exc:
            self.status.text = str(exc)
            return
        except Exception as exc:
            self.status.text = f"Datei nicht lesbar: {exc}"
            return
        self.status.text = f"{self.preview.filename}: {self.preview.summary()}"
        self.show_preview()

    def show_preview(self):
        self.body.clear_widgets()
        head = self.themed(ThemedLabel(
            text="[b]Vorschau (erste 30 Zeilen)[/b]", markup=True,
            size_hint_y=None, height=dp(28)))
        self.body.add_widget(head)
        for row in self.preview.rows[:30]:
            if row.error:
                text = f"Zeile {row.index}: FEHLER - {row.error}"
                role = "danger"
            elif row.duplicate_of is not None:
                text = (f"Zeile {row.index}: {row.event.title} "
                        f"({row.event.start:%d.%m. %H:%M}) - Duplikat")
                role = "text_muted"
            else:
                text = (f"Zeile {row.index}: {row.event.title} "
                        f"({row.event.start:%d.%m. %H:%M}, "
                        f"{row.event.duration_minutes} min)")
                role = "text"
            lbl = self.themed(ThemedLabel(text=text, role=role, halign="left",
                                          size_hint_y=None, height=dp(24)))
            lbl.bind(size=lambda w, *_: setattr(w, "text_size", w.size))
            self.body.add_widget(lbl)
        self.body.add_widget(self.themed(ThemedLabel(
            text="", size_hint_y=None, height=dp(10))))
        self.add_batch_cards()

    def do_import(self):
        if self.preview is None:
            self.do_preview()
            if self.preview is None:
                return
        result = self.app.import_csv(self.preview)
        self.status.text = (f"{result['imported']} Termine importiert "
                            f"(Stapel #{result['batch_id']}). "
                            "Mit einem Tipp wieder entfernbar.")
        self.preview = None
        self.refresh_batches()

    # ---------------- Stapelverwaltung ----------------
    def refresh_batches(self):
        self.body.clear_widgets()
        self.add_batch_cards()

    def add_batch_cards(self):
        batches = self.app.imports.batches()
        title = self.themed(ThemedLabel(
            text="[b]Bisherige Importe[/b]", markup=True, size_hint_y=None,
            height=dp(30)))
        self.body.add_widget(title)
        if not batches:
            self.body.add_widget(self.themed(ThemedLabel(
                text="Noch keine Importe.", role="text_muted", size_hint_y=None,
                height=dp(28))))
        for batch in batches:
            card = Card(orientation="vertical", size_hint_y=None, height=dp(104),
                        padding=dp(8), spacing=dp(4))
            card.attach_theme(self.themes)
            info = self.themed(ThemedLabel(
                text=f"[b]{batch['filename']}[/b]\n{batch['created_at'][:16].replace('T',' ')} "
                     f"- {batch['live_events']} von {batch['imported']} Terminen aktiv",
                markup=True, halign="left", valign="middle"))
            info.bind(size=lambda w, *_: setattr(w, "text_size", w.size))
            card.add_widget(info)
            row = BoxLayout(size_hint_y=None, height=dp(44), spacing=dp(6))
            undo = self.themed(FlatButton(text="Import rückgängig",
                                          fill_role="danger"))
            undo.bind(on_release=lambda _w, b=batch: self.confirm_undo(b))
            show = self.themed(FlatButton(text="Anzeigen",
                                          fill_role="surface_alt",
                                          text_role="text"))
            show.bind(on_release=lambda _w, b=batch: self.show_batch(b))
            row.add_widget(undo)
            row.add_widget(show)
            card.add_widget(row)
            self.body.add_widget(card)

        tools = BoxLayout(size_hint_y=None, height=dp(48), spacing=dp(6))
        dedupe = self.themed(FlatButton(text="Duplikate entfernen",
                                        fill_role="surface_alt", text_role="text"))
        dedupe.bind(on_release=lambda *_: self.dedupe())
        rule = self.themed(FlatButton(text="Nach Regel löschen",
                                      fill_role="surface_alt", text_role="text"))
        rule.bind(on_release=lambda *_: self.rule_delete_dialog())
        tools.add_widget(dedupe)
        tools.add_widget(rule)
        self.body.add_widget(tools)

    def confirm_undo(self, batch):
        box = BoxLayout(orientation="vertical", spacing=dp(8), padding=dp(10))
        box.add_widget(ThemedLabel(
            text=f"{batch['live_events']} Termine aus\n'{batch['filename']}'\n"
                 "wirklich entfernen?", halign="center"))
        popup = Popup(title="Import rückgängig", content=box,
                      size_hint=(0.9, 0.45))
        yes = FlatButton(text="Ja, entfernen", size_hint_y=None, height=dp(46))
        no = FlatButton(text="Abbrechen", size_hint_y=None, height=dp(46))
        for b in (yes, no):
            b.attach_theme(self.themes)
            box.add_widget(b)
        yes.fill_role = "danger"
        yes.bind(on_release=lambda *_: (
            self._undo(batch), popup.dismiss()))
        no.bind(on_release=lambda *_: popup.dismiss())
        popup.open()

    def _undo(self, batch):
        count = self.app.undo_import(batch["id"])
        toast(f"{count} Termine entfernt.")
        self.status.text = f"{count} Termine aus '{batch['filename']}' entfernt."
        self.refresh_batches()

    def show_batch(self, batch):
        events = self.app.imports.preview_batch(batch["id"], 100)
        box = BoxLayout(orientation="vertical", spacing=dp(4), padding=dp(8))
        scroll = ScrollView()
        inner = BoxLayout(orientation="vertical", size_hint_y=None, spacing=dp(2))
        inner.bind(minimum_height=inner.setter("height"))
        popup = Popup(title=batch["filename"], content=box, size_hint=(0.95, 0.9))
        for ev in events:
            row = BoxLayout(size_hint_y=None, height=dp(40), spacing=dp(4))
            lbl = ThemedLabel(text=f"{ev.start:%d.%m. %H:%M}  {ev.title}",
                              halign="left", valign="middle")
            lbl.attach_theme(self.themes)
            lbl.bind(size=lambda w, *_: setattr(w, "text_size", w.size))
            btn = FlatButton(text="Löschen", size_hint_x=None, width=dp(90))
            btn.attach_theme(self.themes)
            btn.fill_role = "danger"
            btn.bind(on_release=lambda _w, e=ev, r=row: (
                self.app.delete_event(e.id), inner.remove_widget(r),
                toast("Termin gelöscht.")))
            row.add_widget(lbl)
            row.add_widget(btn)
            inner.add_widget(row)
        scroll.add_widget(inner)
        box.add_widget(scroll)
        close = FlatButton(text="Schließen", size_hint_y=None, height=dp(46))
        close.attach_theme(self.themes)
        close.bind(on_release=lambda *_: (popup.dismiss(), self.refresh_batches()))
        box.add_widget(close)
        popup.open()

    def dedupe(self):
        removed = self.app.imports.dedupe()
        toast(f"{removed} doppelte Termine entfernt.")
        self.refresh_batches()

    def rule_delete_dialog(self):
        box = BoxLayout(orientation="vertical", spacing=dp(8), padding=dp(10))
        title_in = ThemedInput(hint_text="Titel (leer = alle)", multiline=False,
                               size_hint_y=None, height=dp(44))
        from_in = ThemedInput(hint_text="von TT.MM.JJJJ", multiline=False,
                              size_hint_y=None, height=dp(44))
        to_in = ThemedInput(hint_text="bis TT.MM.JJJJ", multiline=False,
                            size_hint_y=None, height=dp(44))
        for w in (title_in, from_in, to_in):
            w.attach_theme(self.themes)
            box.add_widget(w)
        popup = Popup(title="Nach Regel löschen", content=box, size_hint=(0.92, 0.6))
        run = FlatButton(text="Passende Importtermine löschen", size_hint_y=None,
                         height=dp(48))
        run.attach_theme(self.themes)
        run.fill_role = "danger"

        def execute(*_):
            def parse(text):
                text = text.strip()
                if not text:
                    return None
                try:
                    return datetime.strptime(text, "%d.%m.%Y").replace(
                        tzinfo=timezone.utc)
                except ValueError:
                    return None
            count = self.app.imports.delete_matching(
                title=title_in.text.strip(), start=parse(from_in.text),
                end=parse(to_in.text), only_imported=True)
            toast(f"{count} Termine gelöscht.")
            popup.dismiss()
            self.refresh_batches()

        run.bind(on_release=execute)
        box.add_widget(run)
        popup.open()


# --------------------------------------------------------------------------
class AccountsScreen(BaseScreen):
    def __init__(self, app, **kwargs):
        super().__init__(app, name="accounts", **kwargs)
        self.body = BoxLayout(orientation="vertical", size_hint_y=None,
                              spacing=dp(6))
        self.body.bind(minimum_height=self.body.setter("height"))
        self.status = self.themed(ThemedLabel(text="", role="text_muted",
                                              size_hint_y=None, height=dp(30)))
        self.root_box.add_widget(self.header("Konten & Synchronisierung"))
        self.root_box.add_widget(self.status)
        scroll = ScrollView()
        scroll.add_widget(self.body)
        self.root_box.add_widget(scroll)
        bar = BoxLayout(size_hint_y=None, height=dp(50), spacing=dp(6))
        add = self.themed(FlatButton(text="Konto hinzufügen"))
        add.bind(on_release=lambda *_: self.add_dialog())
        sync = self.themed(FlatButton(text="Jetzt synchronisieren",
                                      fill_role="success"))
        sync.bind(on_release=lambda *_: self.sync_now())
        bar.add_widget(add)
        bar.add_widget(sync)
        self.root_box.add_widget(bar)
        self.root_box.add_widget(self.nav())

    def on_pre_enter(self, *_):
        self.refresh()

    def prepare(self, **kwargs):
        self.refresh()

    def refresh(self):
        self.body.clear_widgets()
        accounts = self.app.sync.accounts()
        if not accounts:
            self.body.add_widget(self.themed(ThemedLabel(
                text="Noch kein Konto verbunden.", role="text_muted",
                size_hint_y=None, height=dp(30))))
        for acc in accounts:
            card = Card(orientation="vertical", size_hint_y=None, height=dp(112),
                        padding=dp(8), spacing=dp(4))
            card.attach_theme(self.themes)
            info = self.themed(ThemedLabel(
                text=f"[b]{acc['name']}[/b] ({acc['provider']})\n"
                     f"{acc['username']}\nZuletzt: {acc['last_sync'] or 'nie'}"
                     + (f"\nFehler: {acc['last_error']}" if acc["last_error"] else ""),
                markup=True, halign="left", valign="top"))
            info.bind(size=lambda w, *_: setattr(w, "text_size", w.size))
            card.add_widget(info)
            row = BoxLayout(size_hint_y=None, height=dp(42), spacing=dp(6))
            one = self.themed(FlatButton(text="Sync"))
            one.bind(on_release=lambda _w, a=acc: self.sync_now(a["id"]))
            rm = self.themed(FlatButton(text="Entfernen", fill_role="danger"))
            rm.bind(on_release=lambda _w, a=acc: (
                self.app.sync.remove_account(a["id"]), self.refresh()))
            row.add_widget(one)
            row.add_widget(rm)
            card.add_widget(row)
            self.body.add_widget(card)

    def add_dialog(self):
        box = BoxLayout(orientation="vertical", spacing=dp(6), padding=dp(10))
        provider = Spinner(text="google", size_hint_y=None, height=dp(44),
                           values=tuple(PROVIDER_PRESETS))
        name = ThemedInput(hint_text="Anzeigename", multiline=False,
                           size_hint_y=None, height=dp(44))
        url = ThemedInput(hint_text="Server-Adresse (https://...)",
                          multiline=False, size_hint_y=None, height=dp(44))
        user = ThemedInput(hint_text="Benutzername / E-Mail", multiline=False,
                           size_hint_y=None, height=dp(44))
        pwd = ThemedInput(hint_text="App-Passwort", multiline=False,
                          password=True, size_hint_y=None, height=dp(44))
        hint = ThemedLabel(text=PROVIDER_PRESETS["google"]["hint"],
                           role="text_muted", size_hint_y=None, height=dp(70),
                           halign="left")
        hint.bind(size=lambda w, *_: setattr(w, "text_size", w.size))
        for w in (name, url, user, pwd, hint):
            w.attach_theme(self.themes)

        def on_provider(_spinner, value):
            preset = PROVIDER_PRESETS.get(value, {})
            url.text = preset.get("url", "")
            hint.text = preset.get("hint", "")
            name.text = preset.get("label", value)
        provider.bind(text=on_provider)
        on_provider(provider, provider.text)

        for w in (provider, name, url, user, pwd, hint):
            box.add_widget(w)
        popup = Popup(title="Konto hinzufügen", content=box, size_hint=(0.95, 0.9))
        save = FlatButton(text="Speichern & testen", size_hint_y=None, height=dp(48))
        save.attach_theme(self.themes)

        def do_save(*_):
            cfg = AccountConfig(name=name.text.strip(), provider=provider.text,
                                url=url.text.strip(), username=user.text.strip(),
                                password=pwd.text)
            try:
                self.app.sync.add_account(cfg)
            except SecurityError as exc:
                hint.text = f"Nicht gespeichert: {exc}"
                return
            pwd.text = ""
            popup.dismiss()
            self.refresh()
            toast("Konto gespeichert.")
        save.bind(on_release=do_save)
        box.add_widget(save)
        popup.open()

    def sync_now(self, account_id=None):
        self.status.text = "Synchronisiere..."

        def work():
            try:
                if account_id:
                    results = [self.app.sync.sync_account(account_id)]
                else:
                    results = self.app.sync.sync_all()
                text = " | ".join(f"{r.account}: {r.summary()}" for r in results) \
                    or "Kein aktives Konto."
            except Exception as exc:
                text = f"Fehler: {exc}"
            self._done(text)

        threading.Thread(target=work, daemon=True).start()

    @mainthread
    def _done(self, text):
        self.status.text = text
        self.refresh()


# --------------------------------------------------------------------------
class SettingsScreen(BaseScreen):
    """Farben, Hintergrund, Schrift, App-Sperre."""

    COLOR_FIELDS = [
        ("background", "Hintergrund"), ("surface", "Flächen"),
        ("surface_alt", "Flächen (2)"), ("text", "Text"),
        ("text_muted", "Text gedämpft"), ("accent", "Akzent"),
        ("accent_text", "Schrift auf Akzent"), ("grid", "Raster"),
        ("today", "Heute"), ("weekend", "Wochenende"), ("danger", "Warnung"),
        ("success", "Erfolg"),
    ]

    def __init__(self, app, **kwargs):
        super().__init__(app, name="settings", **kwargs)
        self.body = BoxLayout(orientation="vertical", size_hint_y=None,
                              spacing=dp(6))
        self.body.bind(minimum_height=self.body.setter("height"))
        self.root_box.add_widget(self.header("Design & Einstellungen"))
        scroll = ScrollView()
        scroll.add_widget(self.body)
        self.root_box.add_widget(scroll)
        self.root_box.add_widget(self.nav())
        self.build_body()

    def prepare(self, **kwargs):
        self.build_body()

    def on_pre_enter(self, *_):
        self.build_body()

    def build_body(self):
        theme = self.themes.theme
        self.body.clear_widgets()

        self.body.add_widget(self._section("Farbschema"))
        presets = GridLayout(cols=3, size_hint_y=None, height=dp(96), spacing=dp(6))
        for preset_name in PRESETS:
            btn = self.themed(FlatButton(text=preset_name, size_hint_y=None,
                                         height=dp(44)))
            btn.bind(on_release=lambda _w, n=preset_name: self.apply_preset(n))
            presets.add_widget(btn)
        self.body.add_widget(presets)

        self.body.add_widget(self._section("Einzelfarben"))
        for key, label in self.COLOR_FIELDS:
            row = BoxLayout(size_hint_y=None, height=dp(48), spacing=dp(6))
            swatch = ColorSwatch(hex_color=getattr(theme, key),
                                 size_hint=(None, 1), width=dp(48))
            name = self.themed(ThemedLabel(text=label, halign="left",
                                           valign="middle",
                                           size_hint_x=None, width=dp(120)))
            name.bind(size=lambda w, *_: setattr(w, "text_size", w.size))
            field = self.themed(ThemedInput(text=getattr(theme, key),
                                            multiline=False))
            field.bind(on_text_validate=lambda w, k=key, s=swatch:
                       self.set_color(k, w.text, s))
            row.add_widget(swatch)
            row.add_widget(name)
            row.add_widget(field)
            self.body.add_widget(row)

        self.body.add_widget(self._section("Hintergrund"))
        mode = Spinner(text=theme.background_mode, size_hint_y=None, height=dp(44),
                       values=("solid", "gradient", "image"))
        mode.bind(text=lambda _w, value: self.themes.update(background_mode=value))
        self.body.add_widget(mode)

        pick_row = BoxLayout(size_hint_y=None, height=dp(48), spacing=dp(6))
        pick = self.themed(FlatButton(text="Bild wählen"))
        pick.bind(on_release=lambda *_: self.pick_background())
        clear = self.themed(FlatButton(text="Bild entfernen",
                                       fill_role="surface_alt", text_role="text"))
        clear.bind(on_release=lambda *_: (self.themes.clear_background_image(),
                                          self.build_body()))
        pick_row.add_widget(pick)
        pick_row.add_widget(clear)
        self.body.add_widget(pick_row)

        self.body.add_widget(self._labelled_slider(
            "Deckkraft des Bildes", theme.background_opacity, 0.05, 1.0,
            lambda v: self.themes.update(background_opacity=v)))
        self.body.add_widget(self._labelled_slider(
            "Eckenrundung", theme.corner_radius, 0, 30,
            lambda v: self.themes.update(corner_radius=int(v))))
        self.body.add_widget(self._labelled_slider(
            "Schriftgröße", theme.font_scale, 0.8, 1.6,
            lambda v: self.themes.update(font_scale=round(v, 2))))

        warnings = theme.contrast_warnings()
        if warnings:
            self.body.add_widget(self.themed(ThemedLabel(
                text="Hinweis: " + " ".join(warnings), role="danger",
                size_hint_y=None, height=dp(44), halign="left")))

        self.body.add_widget(self._section("Sicherheit"))
        pin_row = BoxLayout(size_hint_y=None, height=dp(48), spacing=dp(6))
        self.pin_input = self.themed(ThemedInput(
            hint_text="Neue PIN (min. 4 Ziffern)", password=True, multiline=False))
        set_pin = self.themed(FlatButton(text="PIN setzen", size_hint_x=None,
                                         width=dp(110)))
        set_pin.bind(on_release=lambda *_: self.set_pin())
        off = self.themed(FlatButton(text="Aus", size_hint_x=None, width=dp(60),
                                     fill_role="surface_alt", text_role="text"))
        off.bind(on_release=lambda *_: (self.app.clear_pin(), toast("PIN entfernt.")))
        pin_row.add_widget(self.pin_input)
        pin_row.add_widget(set_pin)
        pin_row.add_widget(off)
        self.body.add_widget(pin_row)

        self.body.add_widget(self._section("Daten"))
        data_row = BoxLayout(size_hint_y=None, height=dp(48), spacing=dp(6))
        export = self.themed(FlatButton(text="CSV exportieren"))
        export.bind(on_release=lambda *_: self.export())
        maint = self.themed(FlatButton(text="Aufräumen",
                                       fill_role="surface_alt", text_role="text"))
        maint.bind(on_release=lambda *_: toast(str(self.app.maintenance())))
        data_row.add_widget(export)
        data_row.add_widget(maint)
        self.body.add_widget(data_row)

        self.body.add_widget(self._section("Gelernte Muster"))
        for pattern in self.app.patterns.all_patterns()[:20]:
            text = (f"{pattern['title']}: {pattern['duration_minutes']} min "
                    f"({pattern['samples']}x"
                    + (", fest" if pattern["pinned"] else "") + ")")
            row = BoxLayout(size_hint_y=None, height=dp(40), spacing=dp(6))
            lbl = self.themed(ThemedLabel(text=text, halign="left"))
            lbl.bind(size=lambda w, *_: setattr(w, "text_size", w.size))
            forget = self.themed(FlatButton(text="Vergessen", size_hint_x=None,
                                            width=dp(100), fill_role="danger"))
            forget.bind(on_release=lambda _w, p=pattern: (
                self.app.patterns.forget(p["title"]), self.build_body()))
            row.add_widget(lbl)
            row.add_widget(forget)
            self.body.add_widget(row)

    def _section(self, title: str):
        lbl = self.themed(ThemedLabel(text=f"[b]{title}[/b]", markup=True,
                                      size_hint_y=None, height=dp(34),
                                      halign="left"))
        lbl.bind(size=lambda w, *_: setattr(w, "text_size", w.size))
        return lbl

    def _labelled_slider(self, label, value, low, high, callback):
        box = BoxLayout(orientation="vertical", size_hint_y=None, height=dp(64))
        lbl = self.themed(ThemedLabel(text=f"{label}: {round(float(value), 2)}",
                                      size_hint_y=None, height=dp(24),
                                      halign="left"))
        lbl.bind(size=lambda w, *_: setattr(w, "text_size", w.size))
        slider = Slider(min=low, max=high, value=float(value), size_hint_y=None,
                        height=dp(36))

        def on_change(_w, new_value):
            lbl.text = f"{label}: {round(float(new_value), 2)}"
        slider.bind(value=on_change)
        slider.bind(on_touch_up=lambda w, *_: callback(w.value))
        box.add_widget(lbl)
        box.add_widget(slider)
        return box

    def apply_preset(self, name):
        self.themes.apply_preset(name)
        self.build_body()

    def set_color(self, key, value, swatch):
        clean = validate_color(value, getattr(self.themes.theme, key))
        self.themes.update(**{key: clean})
        swatch.hex_color = clean
        toast(f"{key} = {clean}")

    def pick_background(self):
        from kivy.uix.filechooser import FileChooserListView
        box = BoxLayout(orientation="vertical")
        chooser = FileChooserListView(filters=["*.png", "*.jpg", "*.jpeg", "*.webp"])
        box.add_widget(chooser)
        popup = Popup(title="Hintergrundbild", content=box, size_hint=(0.95, 0.9))
        take = FlatButton(text="Übernehmen", size_hint_y=None, height=dp(48))
        take.attach_theme(self.themes)

        def use(*_):
            if chooser.selection:
                try:
                    self.themes.set_background_image(chooser.selection[0])
                except SecurityError as exc:
                    toast(str(exc))
            popup.dismiss()
            self.build_body()
        take.bind(on_release=use)
        box.add_widget(take)
        popup.open()

    def set_pin(self):
        try:
            self.app.set_pin(self.pin_input.text)
            self.pin_input.text = ""
            toast("PIN gesetzt.")
        except SecurityError as exc:
            toast(str(exc))

    def export(self):
        try:
            path = self.app.export_csv()
            toast(f"Exportiert: {path.name}")
        except Exception as exc:
            toast(f"Export fehlgeschlagen: {exc}")


# --------------------------------------------------------------------------
class PinScreen(BaseScreen):
    """Sperrbildschirm - schützt Kalenderdaten auf einem entsperrten Gerät."""

    def __init__(self, app, **kwargs):
        super().__init__(app, name="pin", **kwargs)
        self.root_box.add_widget(ThemedLabel(text=""))
        self.info = self.themed(ThemedLabel(text="PIN eingeben",
                                            size_hint_y=None, height=dp(40)))
        self.input = self.themed(ThemedInput(password=True, multiline=False,
                                             size_hint_y=None, height=dp(52)))
        ok = self.themed(FlatButton(text="Entsperren", size_hint_y=None,
                                    height=dp(52)))
        ok.bind(on_release=lambda *_: self.unlock())
        self.input.bind(on_text_validate=lambda *_: self.unlock())
        self.root_box.add_widget(self.info)
        self.root_box.add_widget(self.input)
        self.root_box.add_widget(ok)
        self.root_box.add_widget(ThemedLabel(text=""))

    def unlock(self):
        try:
            if self.app.check_pin(self.input.text):
                self.input.text = ""
                self.manager.current = "month"
            else:
                self.info.text = "Falsche PIN."
        except SecurityError as exc:
            self.info.text = str(exc)
        finally:
            self.input.text = ""
