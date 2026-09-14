import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pycal.core import CalendarApp          # noqa: E402
from pycal.models import Event              # noqa: E402


@pytest.fixture()
def app(tmp_path):
    application = CalendarApp(tmp_path / "data")
    yield application
    application.close()


@pytest.fixture()
def db(app):
    return app.db


def make_event(title="Termin", day=1, hour=10, minutes=60, **kw):
    start = datetime(2026, 3, day, hour, 0, tzinfo=timezone.utc)
    return Event(calendar_id=kw.pop("calendar_id", 1), title=title, start=start,
                 end=start + timedelta(minutes=minutes), **kw)
