"""Synchronisation mit Google Kalender, Outlook und beliebigen CalDAV-Servern."""
from .icsfmt import parse_ics, build_ics, IcsParseError
from .manager import SyncManager, SyncResult, AccountConfig

__all__ = ["parse_ics", "build_ics", "IcsParseError", "SyncManager",
           "SyncResult", "AccountConfig"]
