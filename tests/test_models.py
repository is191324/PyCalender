"""Datenmodell, Wiederholungsregeln und Titelnormalisierung."""
from datetime import datetime, timedelta, timezone

from pycal.models import Event, Recurrence, normalize_title

UTC = timezone.utc


def test_rrule_roundtrip():
    rec = Recurrence(freq="WEEKLY", interval=2, byday=["MO", "WE"], count=10)
    again = Recurrence.from_rrule(rec.to_rrule())
    assert again.freq == "WEEKLY" and again.interval == 2
    assert again.byday == ["MO", "WE"] and again.count == 10


def test_weekly_byday_expansion():
    ev = Event(title="Sport", start=datetime(2026, 3, 2, 18, 0, tzinfo=UTC),
               end=datetime(2026, 3, 2, 19, 0, tzinfo=UTC),
               rrule="FREQ=WEEKLY;BYDAY=MO,WE")
    occ = ev.occurrences(datetime(2026, 3, 1, tzinfo=UTC),
                         datetime(2026, 3, 15, tzinfo=UTC))
    assert [d.day for d in occ] == [2, 4, 9, 11]


def test_count_and_until_limits():
    ev = Event(start=datetime(2026, 1, 1, 8, 0, tzinfo=UTC),
               end=datetime(2026, 1, 1, 9, 0, tzinfo=UTC),
               rrule="FREQ=DAILY;COUNT=3")
    occ = ev.occurrences(datetime(2026, 1, 1, tzinfo=UTC),
                         datetime(2026, 2, 1, tzinfo=UTC))
    assert len(occ) == 3


def test_monthly_handles_short_months():
    ev = Event(start=datetime(2026, 1, 31, 9, 0, tzinfo=UTC),
               end=datetime(2026, 1, 31, 10, 0, tzinfo=UTC),
               rrule="FREQ=MONTHLY")
    occ = ev.occurrences(datetime(2026, 1, 1, tzinfo=UTC),
                         datetime(2026, 5, 1, tzinfo=UTC))
    assert [d.month for d in occ] == [1, 2, 3, 4]
    assert occ[1].day == 28          # Februar 2026 hat 28 Tage


def test_exdate_removes_occurrence():
    ev = Event(start=datetime(2026, 3, 2, 18, 0, tzinfo=UTC),
               end=datetime(2026, 3, 2, 19, 0, tzinfo=UTC),
               rrule="FREQ=WEEKLY",
               exdates=[datetime(2026, 3, 9, 18, 0, tzinfo=UTC)])
    occ = ev.occurrences(datetime(2026, 3, 1, tzinfo=UTC),
                         datetime(2026, 3, 20, tzinfo=UTC))
    assert [d.day for d in occ] == [2, 16]


def test_normalize_title_groups_variants():
    assert normalize_title("Sport 18:00") == normalize_title("sport")
    assert normalize_title("Team-Meeting #3") == "team meeting"
    assert normalize_title("") == ""


def test_recurrence_description_is_german():
    assert "wöchentlich" in Recurrence(freq="WEEKLY", byday=["TU"]).describe()
