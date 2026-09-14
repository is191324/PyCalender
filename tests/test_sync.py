"""iCalendar-Verarbeitung und Synchronisationslogik."""
from datetime import datetime, timedelta, timezone

import pytest

from conftest import make_event
from pycal.sync.icsfmt import IcsParseError, build_ics, parse_ics
from pycal.sync.manager import PROVIDER_PRESETS

UTC = timezone.utc

SAMPLE = """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:abc-123
SUMMARY:Teambesprechung
DTSTART:20260914T090000Z
DTEND:20260914T101500Z
LOCATION:Raum 2
RRULE:FREQ=WEEKLY;BYDAY=MO
END:VEVENT
BEGIN:VEVENT
UID:ganztag-1
SUMMARY:Feiertag
DTSTART;VALUE=DATE:20261003
DTEND;VALUE=DATE:20261004
END:VEVENT
END:VCALENDAR
"""


def test_parse_basic_ics():
    events = parse_ics(SAMPLE)
    assert len(events) == 2
    first = events[0]
    assert first.title == "Teambesprechung" and first.duration_minutes == 75
    assert first.rrule == "FREQ=WEEKLY;BYDAY=MO"
    assert events[1].all_day is True


def test_roundtrip_preserves_fields():
    ev = make_event("Arzt, Kontrolle", minutes=45)
    ev.description = "Zeile1\nZeile2; mit Semikolon"
    ev.location = "Praxis"
    ev.reminder_minutes = 15
    back = parse_ics(build_ics([ev]))[0]
    assert back.title == ev.title
    assert back.description == ev.description
    assert back.duration_minutes == 45


def test_folded_lines_are_unfolded():
    long_title = "A" * 200
    ics = build_ics([make_event(long_title)])
    assert parse_ics(ics)[0].title == long_title


def test_cancelled_status_marks_deleted():
    ics = SAMPLE.replace("END:VEVENT", "STATUS:CANCELLED\nEND:VEVENT", 1)
    assert parse_ics(ics)[0].deleted is True


def test_duration_property_instead_of_dtend():
    ics = ("BEGIN:VCALENDAR\nBEGIN:VEVENT\nUID:d1\nSUMMARY:Kurz\n"
           "DTSTART:20260914T090000Z\nDURATION:PT45M\nEND:VEVENT\nEND:VCALENDAR\n")
    assert parse_ics(ics)[0].duration_minutes == 45


def test_garbage_input_does_not_crash():
    assert parse_ics("voellig kaputt") == []
    assert parse_ics("") == []


def test_provider_presets_are_https():
    for key, preset in PROVIDER_PRESETS.items():
        assert not preset["url"] or preset["url"].startswith("https://")
