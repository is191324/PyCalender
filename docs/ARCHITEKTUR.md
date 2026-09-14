# Architektur

## 1. Schichten

```
                 ┌───────────────────────────────┐
   Oberfläche    │ pycal/ui/  (Kivy)             │  Screens, Widgets, Theme-Bindung
                 └──────────────┬────────────────┘
                                │  nur über CalendarApp
                 ┌──────────────▼────────────────┐
   Anwendung     │ pycal/core.py  CalendarApp    │  Fassade: Termine, Muster, Import, Sync
                 └──┬────────┬────────┬──────────┘
                    │        │        │
        ┌───────────▼──┐ ┌───▼─────┐ ┌▼─────────────┐ ┌──────────────┐
Fach-   │ patterns.py  │ │csvio.py │ │ sync/        │ │ theme.py     │
logik   │ Mustern      │ │Import   │ │ CalDAV/ICS   │ │ Farben       │
        └───────┬──────┘ └────┬────┘ └──────┬───────┘ └──────┬───────┘
                └─────────────┴─────────────┴────────────────┘
                                │
                 ┌──────────────▼────────────────┐
   Daten         │ pycal/db.py (SQLite)          │
                 │ pycal/models.py               │
                 │ pycal/security.py             │  quer zu allen Schichten
                 └───────────────────────────────┘
```

**Regel:** Kivy wird ausschließlich in `pycal/ui/` importiert. Die gesamte
Logik läuft ohne Oberfläche – deshalb sind 149 Tests ohne Display
ausführbar, und dieselbe Logik lässt sich per Kommandozeile oder von einem
Hintergrunddienst nutzen.

## 2. Module

| Modul | Aufgabe |
|---|---|
| `models.py` | `Event`, `Calendar`, `Recurrence` (RRULE-Teilmenge nach RFC 5545), Zeitwerkzeuge, Titelnormalisierung |
| `db.py` | SQLite-Schema, parametrisierte Abfragen, Transaktionen, Importstapel |
| `patterns.py` | `PatternStore`: lernen, vorschlagen, Rhythmus erkennen |
| `csvio.py` | `CsvImporter` (Vorschau/Commit), `ImportManager` (rückgängig, Regel-Löschung, Duplikate), Export |
| `sync/icsfmt.py` | gehärteter iCalendar-Parser/-Writer |
| `sync/http.py` | TLS-erzwingender HTTP-Client mit SSRF- und Größenprüfung |
| `sync/caldav.py` | PROPFIND/REPORT/PUT/DELETE, XXE-sicheres XML |
| `sync/manager.py` | Kontoverwaltung, Zwei-Wege-Abgleich, Konfliktauflösung |
| `theme.py` | `Theme` (Dataclass), Validierung, Vorlagen, Kontrastberechnung |
| `security.py` | Eingabereinigung, CSV-Formelschutz, URL-Prüfung, Schlüsseltresor, PIN, Rate-Limit |
| `core.py` | `CalendarApp` als einzige Schnittstelle für die Oberfläche |

## 3. Datenmodell

```sql
calendars(id, name, color, visible, account_id, remote_url, read_only, sync_token)
accounts (id, name, provider, url, username, secret_blob, enabled, last_sync, last_error)
events   (id, calendar_id, uid, title, title_key, description, location,
          start, end, all_day, color, rrule, exdates, reminder_minutes,
          etag, remote_href, dirty, deleted, import_batch_id, source,
          created_at, updated_at)
patterns (title_key, display_title, samples, durations, start_minutes,
          weekdays, last_seen, location, color, reminder_minutes, pinned_duration)
import_batches(id, filename, created_at, row_count, imported, skipped,
               calendar_id, note, undone)
settings (key, value)
```

Wesentliche Entwurfsentscheidungen:

* **`title_key`** ist der normalisierte Titel und wird beim Speichern
  automatisch mitgeschrieben. Er ist indiziert und verbindet Termine,
  Muster und Regel-Löschung miteinander.
* **`import_batch_id`** ist der Schlüssel zur Anforderung „Löschen muss
  leicht gehen": ein Fremdschlüssel auf `import_batches` genügt, um einen
  kompletten Import in einer einzigen Anweisung zurückzunehmen.
* **`dirty` / `deleted`** ermöglichen Offline-Betrieb: Änderungen werden
  lokal markiert und beim nächsten Sync übertragen; gelöschte Termine
  bleiben als Grabstein erhalten, bis der Server sie bestätigt hat.
* **`etag` / `remote_href`** liefern optimistisches Sperren (`If-Match`),
  damit ein Upload keine fremde Änderung überschreibt.
* Serientermine werden **nicht** als einzelne Zeilen gespeichert, sondern
  zur Anzeigezeit aus der RRULE expandiert (begrenzt auf 2000 Vorkommen pro
  Abfrage). Das hält die Datenbank klein und Änderungen an einer Serie
  einfach.

## 4. Wie die Mustererkennung rechnet

```
learn(event):
    key        = normalize_title(event.title)        # "Sport 18:00" -> "sport"
    durations += [dauer_in_minuten]                  # gleitendes Fenster, max. 40
    starts    += [startminute_des_tages]
    weekdays  += [wochentag]

suggest(title):
    pinned  -> feste Dauer, Sicherheit 1.0
    sonst   -> Median(Dauer ohne oberstes/unterstes Zehntel)
               Median(Startzeit)
               Wochentage mit Anteil >= 20 %
               Sicherheit = 0.35 * min(1, n/8) + 0.65 * (1 - Streuung/Median)

suggest_recurrence(title):
    Abstände zwischen den letzten 30 Terminen dieses Namens
    häufigster Abstand >= 60 % der Fälle -> RRULE-Vorschlag
```

## 5. Sync-Ablauf (CalDAV, zwei Wege)

```
1. PROPFIND  auf die Kontoadresse       -> Liste der Kalender (+ Schreibrecht)
2. REPORT    calendar-query je Kalender -> href + ETag aller Termine im Fenster
3. Vergleich mit der lokalen Tabelle
   a) href unbekannt oder ETag anders   -> calendar-multiget, parsen, speichern
   b) lokal dirty                       -> PUT  mit If-Match (bzw. If-None-Match)
   c) lokal deleted + dirty             -> DELETE mit If-Match
   d) remote verschwunden, lokal sauber -> lokal entfernen
4. Bei 412 Precondition Failed: Konflikt -> Sicherungskopie „(Konflikt)"
```

Aus Sicherheitsgründen läuft jede Adresse – auch die nach einer
Weiterleitung – vor dem Abruf durch `validate_sync_url`.

## 6. Theme-Fluss

`ThemeManager` hält genau ein `Theme`, speichert es als JSON in
`settings` und ruft bei Änderungen alle registrierten Rückrufe auf. Jedes
Widget mit `ThemedMixin` registriert sich einmal und färbt sich selbst um –
deshalb wirkt ein Farbwechsel sofort auf allen Bildschirmen, ohne Neustart.

## 7. Nebenläufigkeit

* SQLite läuft im WAL-Modus, jede Verbindung ist thread-lokal, Schreibzugriffe
  werden zusätzlich über ein `RLock` serialisiert (Test G10 prüft das mit
  vier parallelen Threads).
* Synchronisierung läuft in einem Daemon-Thread; die Oberfläche wird über
  `@mainthread` bzw. `Clock.schedule_once` aktualisiert.
