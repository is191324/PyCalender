"""CSV-Import, Duplikate und das einfache Löschen importierter Einträge."""
from datetime import datetime, timezone

import pytest

from pycal.csvio import detect_mapping, export_events, parse_duration, parse_datetime
from pycal.security import SecurityError

UTC = timezone.utc

BASIC = ("Betreff;Startdatum;Dauer;Ort\n"
         "Zahnarzt;14.09.2026 09:00;45;Praxis\n"
         "Sport;15.09.2026 18:00;1:30;Halle\n"
         "Elternabend;16.09.2026 19:30;90;Schule\n")


def _preview(app, text=BASIC, **kw):
    return app.importer.preview(None, app.default_calendar_id(), text=text,
                                filename="test.csv", **kw)


def test_column_detection_german_and_english():
    assert detect_mapping(["Betreff", "Startdatum", "Ort"])["title"] == "Betreff"
    assert detect_mapping(["Subject", "Start Date", "Location"])["start"] == "Start Date"


def test_duration_formats():
    assert parse_duration("90") == 90
    assert parse_duration("1:30") == 90
    assert parse_duration("2h15") == 135
    assert parse_duration("kaputt") is None


def test_date_formats():
    assert parse_datetime("14.09.2026 09:00").hour == 9
    assert parse_datetime("2026-09-14T09:00:00").day == 14
    assert parse_datetime("keine Zeit") is None


def test_import_creates_batch_and_events(app):
    preview = _preview(app)
    assert len(preview.valid) == 3
    result = app.import_csv(preview)
    assert result["imported"] == 3
    assert len(app.imports.batches()) == 1


def test_undo_import_removes_exactly_that_batch(app):
    manual = app.create_event("Handtermin", datetime(2026, 9, 20, 10, 0, tzinfo=UTC))
    result = app.import_csv(_preview(app))
    assert app.undo_import(result["batch_id"]) == 3
    remaining = [e.title for e in app.db.search_events("")] or \
                [e.title for e in app.db.list_events(datetime(2026, 1, 1, tzinfo=UTC),
                                                     datetime(2027, 1, 1, tzinfo=UTC))]
    assert remaining == ["Handtermin"]
    assert app.db.get_event(manual.id).deleted is False


def test_duplicates_are_detected_not_imported_twice(app):
    app.import_csv(_preview(app))
    second = _preview(app)
    assert len(second.duplicates) == 3 and len(second.valid) == 0


def test_broken_rows_are_reported_not_fatal(app):
    text = BASIC + ";;;\nOhneDatum;;;\n"
    preview = _preview(app, text)
    assert len(preview.errors) == 2
    assert len(preview.valid) == 3


def test_missing_duration_uses_learned_pattern(app):
    app.create_event("Zahnarzt", datetime(2026, 1, 5, 9, 0, tzinfo=UTC),
                     datetime(2026, 1, 5, 9, 25, tzinfo=UTC))
    preview = _preview(app, "Betreff;Startdatum\nZahnarzt;14.09.2026 09:00\n")
    assert preview.valid[0].event.duration_minutes == 25


def test_delete_matching_by_title_and_range(app):
    app.import_csv(_preview(app))
    removed = app.imports.delete_matching(
        title="Sport", start=datetime(2026, 9, 1, tzinfo=UTC),
        end=datetime(2026, 10, 1, tzinfo=UTC))
    assert removed == 1


def test_delete_matching_spares_manual_events(app):
    manual = app.create_event("Sport", datetime(2026, 9, 17, 7, 0, tzinfo=UTC))
    app.import_csv(_preview(app))
    assert app.imports.delete_matching(title="Sport", only_imported=True) == 1
    assert app.db.get_event(manual.id).deleted is False


def test_dedupe_keeps_one(app):
    app.import_csv(_preview(app))
    app.import_csv(_preview(app), include_duplicates=True)
    assert app.imports.dedupe() == 3


def test_export_roundtrip(app, tmp_path):
    app.import_csv(_preview(app))
    events = app.db.list_events(datetime(2026, 1, 1, tzinfo=UTC),
                               datetime(2027, 1, 1, tzinfo=UTC))
    path = export_events(events, tmp_path, "export.csv")
    content = path.read_text(encoding="utf-8-sig")
    assert "Zahnarzt" in content and path.parent == tmp_path


def test_missing_columns_raise_clear_error(app):
    with pytest.raises(SecurityError):
        _preview(app, "Spalte1;Spalte2\nx;y\n")
