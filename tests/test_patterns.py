"""Mustererkennung: Dauer pro Ereignisname."""
from datetime import datetime, timedelta, timezone

from conftest import make_event

UTC = timezone.utc


def _feed(app, title, durations, start_day=2, weekly=True):
    for i, minutes in enumerate(durations):
        start = datetime(2026, 3, start_day, 18, 0, tzinfo=UTC) + \
            timedelta(days=7 * i if weekly else i)
        ev = app.create_event(title, start, start + timedelta(minutes=minutes))
    return ev


def test_duration_is_suggested_after_repeats(app):
    _feed(app, "Sport", [90, 90, 90])
    sug = app.suggest("Sport")
    assert sug.duration_minutes == 90
    assert sug.samples == 3
    assert sug.confidence > 0.5


def test_median_ignores_single_outlier(app):
    _feed(app, "Meeting", [60, 60, 60, 60, 60, 600])
    assert app.suggest("Meeting").duration_minutes == 60


def test_new_event_inherits_learned_duration(app):
    _feed(app, "Yoga", [75, 75])
    ev = app.create_event("Yoga", datetime(2026, 6, 1, 7, 0, tzinfo=UTC))
    assert ev.duration_minutes == 75


def test_title_variants_share_one_pattern(app):
    _feed(app, "Sport", [45, 45])
    app.create_event("  SPORT 20:00 ", datetime(2026, 5, 4, 20, 0, tzinfo=UTC),
                     datetime(2026, 5, 4, 20, 45, tzinfo=UTC))
    sug = app.suggest("sport")
    assert sug.samples == 3


def test_pinned_duration_wins_over_statistics(app):
    _feed(app, "Standup", [15, 15, 15])
    app.patterns.pin_duration("Standup", 30)
    sug = app.suggest("Standup")
    assert sug.duration_minutes == 30 and sug.pinned and sug.confidence == 1.0


def test_weekly_rhythm_is_detected(app):
    _feed(app, "Teamrunde", [60] * 4)
    rec = app.suggest_recurrence("Teamrunde")
    assert rec.freq == "WEEKLY" and rec.byday == ["MO"]


def test_daily_rhythm_is_detected(app):
    _feed(app, "Tagebuch", [10] * 5, weekly=False)
    assert app.suggest_recurrence("Tagebuch").freq == "DAILY"


def test_no_recurrence_for_random_dates(app):
    for day in (1, 5, 17, 23):
        start = datetime(2026, 4, day, 9, 0, tzinfo=UTC)
        app.create_event("Zufall", start, start + timedelta(minutes=30))
    assert app.suggest_recurrence("Zufall") is None


def test_forget_removes_pattern(app):
    _feed(app, "Altlast", [30, 30])
    app.patterns.forget("Altlast")
    assert app.suggest("Altlast") is None


def test_rebuild_restores_patterns(app):
    _feed(app, "Wiederaufbau", [55, 55])
    app.db.con.execute("DELETE FROM patterns")
    assert app.patterns.rebuild() >= 2
    assert app.suggest("Wiederaufbau").duration_minutes == 55


def test_fuzzy_prefix_match(app):
    _feed(app, "Physiotherapie", [50, 50, 50])
    sug = app.suggest("Physio")
    assert sug is not None and sug.duration_minutes == 50
    assert sug.confidence < 1.0
