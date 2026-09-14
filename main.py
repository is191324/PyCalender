#!/usr/bin/env python3
"""Einstiegspunkt von PyCalendar.

Auf Android startet Buildozer genau diese Datei. Auf dem Desktop lässt sich
die App ebenso starten (praktisch zum Entwickeln):

    python3 main.py                 # grafische App
    python3 main.py --selftest      # Logik ohne Kivy prüfen
    python3 main.py --import x.csv  # CSV auf der Kommandozeile importieren
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


def cli() -> int:
    from pycal.core import CalendarApp
    args = sys.argv[1:]
    app = CalendarApp()
    if "--selftest" in args:
        info = app.maintenance()
        print("Datenverzeichnis:", app.data_dir)
        print("Kalender:", [c.name for c in app.db.list_calendars()])
        print("Wartung:", info)
        print("Muster:", app.patterns.all_patterns()[:5])
        return 0
    if "--import" in args:
        path = args[args.index("--import") + 1]
        preview = app.preview_csv(path)
        print("Vorschau:", preview.summary())
        result = app.import_csv(preview)
        print(f"Importiert: {result['imported']} (Stapel #{result['batch_id']})")
        print("Rückgängig mit: python3 main.py --undo", result["batch_id"])
        return 0
    if "--undo" in args:
        batch = int(args[args.index("--undo") + 1])
        print("Entfernt:", app.undo_import(batch), "Termine")
        return 0
    if "--export" in args:
        print("Exportiert nach:", app.export_csv())
        return 0
    if "--sync" in args:
        for result in app.sync.sync_all():
            print(result.account, "->", result.summary())
        return 0
    return -1


def main() -> int:
    if len(sys.argv) > 1:
        code = cli()
        if code >= 0:
            return code
    from pycal.ui.app import run
    run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
