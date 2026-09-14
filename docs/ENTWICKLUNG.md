# Entwicklung, Test und Build

## 1. Projektstruktur

```
pycalendar/
├── main.py                 Einstiegspunkt (Android + Desktop + Kommandozeile)
├── buildozer.spec          Android-Build (Rechte, Abhängigkeiten, Manifest)
├── requirements.txt
├── pycal/
│   ├── models.py           Event, Calendar, Recurrence, Titelnormalisierung
│   ├── db.py               SQLite-Schicht
│   ├── patterns.py         Mustererkennung (Dauer je Ereignisname)
│   ├── csvio.py            CSV-Import/-Export, Importstapel
│   ├── theme.py            Farben, Hintergrund, Vorlagen
│   ├── security.py         Sicherheitsbausteine
│   ├── core.py             CalendarApp (Fassade)
│   ├── sync/
│   │   ├── icsfmt.py       iCalendar-Parser/-Writer
│   │   ├── http.py         gehärteter HTTP-Client
│   │   ├── caldav.py       CalDAV-Client
│   │   └── manager.py      Kontoverwaltung, Abgleich
│   └── ui/
│       ├── widgets.py      themenfähige Kivy-Widgets
│       ├── screens.py      Monat, Agenda, Editor, Import, Konten, Design, PIN
│       └── app.py          Kivy-App
├── .github/workflows/      APK-Bau per GitHub Actions
├── tests/                  152 Tests
└── docs/                   diese Dokumentation
```

## 2. Entwicklungsumgebung

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python3 main.py
```

Kivy braucht auf dem Desktop eine Grafikausgabe. Ohne Bildschirm (CI,
Container) läuft die Oberfläche mit einem virtuellen X-Server:

```bash
xvfb-run -s "-screen 0 480x900x24" python3 main.py
```

Die gesamte Logik ist davon unabhängig – `python3 main.py --selftest` und
die Testsuite laufen ohne Kivy-Fenster.

## 3. Tests

```bash
python3 -m pytest tests -q             # alles
python3 -m pytest tests -v -k pattern  # nur Mustererkennung
python3 -m pytest tests/test_pentest.py -v
```

Die Testsuite nutzt eine temporäre Datenbank je Test (`tmp_path`), es wird
nie auf echte Nutzerdaten zugegriffen und es gehen keine Netzanfragen
hinaus – Serverantworten werden über `monkeypatch` eingespielt.

## 4. Screenshots erzeugen

Die Bilder in `docs/bilder/` entstehen automatisiert (Testdaten anlegen,
Bildschirme durchschalten, `Window.screenshot`). Nützlich, um
Designänderungen zu prüfen, ohne ein Gerät anzuschließen.

## 5. APK bauen lassen (GitHub Actions)

Der bequemste Weg zu einer installierbaren Datei, ohne Android-SDK auf dem
eigenen Rechner:

1. Projekt zu GitHub hochladen:

```bash
git init && git add . && git commit -m "PyCalendar 1.0.0"
git branch -M main
git remote add origin git@github.com:<konto>/pycalendar.git
git push -u origin main
```

2. Im Reiter **Actions** den Ablauf **„APK bauen"** starten (*Run workflow*).
   Wählbar sind `debug` oder `release` und die Zielarchitekturen.
3. Nach dem Lauf liegt die Datei unter **Artifacts** →
   `pycalendar-apk-debug.zip`. Entpacken, auf das Telefon kopieren,
   Installation aus unbekannten Quellen erlauben, antippen.

Der erste Lauf dauert 30–50 Minuten (SDK, NDK und alle Recipes werden
übersetzt), danach greift der Cache und es sind meist 8–15 Minuten. Vor dem
Build läuft die vollständige Testsuite; schlägt sie fehl, wird nichts gebaut.

**Für signierte Release-Builds** vier Repository-Secrets hinterlegen
(*Settings → Secrets and variables → Actions*):

| Secret | Inhalt |
|---|---|
| `ANDROID_KEYSTORE_BASE64` | `base64 -w0 meine.keystore` |
| `ANDROID_KEYSTORE_PASSWORD` | Passwort des Keystores |
| `ANDROID_KEY_ALIAS` | Alias des Schlüssels |
| `ANDROID_KEY_PASSWORD` | Passwort des Schlüssels |

Schlägt ein Build fehl, lädt der Ablauf das Buildozer-Protokoll als eigenes
Artefakt hoch – damit lässt sich die Ursache ohne lokale Nachstellung finden.

## 6. APK lokal bauen

```bash
pip install buildozer cython
buildozer -v android debug
```

Der erste Lauf lädt das Android-SDK/NDK (mehrere GB) und dauert je nach
Rechner 20–60 Minuten. Danach liegt die Datei unter
`bin/pycalendar-1.0.0-debug.apk`.

Auf ein angeschlossenes Gerät aufspielen und Protokoll mitlesen:

```bash
buildozer android deploy run logcat
```

Release-Build (signiert):

```bash
export P4A_RELEASE_KEYSTORE=~/keys/pycalendar.keystore
export P4A_RELEASE_KEYSTORE_PASSWD=…
export P4A_RELEASE_KEYALIAS=pycalendar
export P4A_RELEASE_KEYALIAS_PASSWD=…
buildozer android release
```

### Stolpersteine

| Problem | Ursache und Lösung |
|---|---|
| `cryptography` baut nicht | Die Recipe braucht eine Rust-Toolchain für Android (`rustup target add aarch64-linux-android armv7-linux-androideabi`; im GitHub-Ablauf bereits enthalten). Alternativ in `buildozer.spec` die vorbereitete Zeile mit **`pycryptodome`** verwenden – `CredentialVault` erkennt den Unterbau selbst, und das Format der gespeicherten Zugangsdaten bleibt gleich (Test `test_both_crypto_backends_are_interchangeable`). |
| Keine Netzverbindung in der App | `INTERNET`-Recht in `buildozer.spec` prüfen |
| Sync scheitert mit Zertifikatsfehler | `certifi` muss in `requirements` stehen; Android-eigene Wurzelzertifikate reichen für `requests` nicht immer |
| App startet nicht, kein Fehler sichtbar | `buildozer android logcat | grep python` |
| Hintergrundbild wird nicht angezeigt | Android 13+ braucht `READ_MEDIA_IMAGES`; die Datei wird beim Auswählen ohnehin ins App-Verzeichnis kopiert |

## 7. Code-Richtlinien

1. **Kivy nur in `pycal/ui/`.** Alles andere bleibt testbar ohne Oberfläche.
2. **Jede SQL-Abfrage parametrisiert.** Kein `f"…{wert}…"` in SQL – ein Test
   (`test_all_sql_uses_parameters`) überwacht das.
3. **Jede Eingabe von außen durch `security.sanitize_*`.** Das gilt für CSV,
   ICS, Servernamen, Themes und Dateinamen gleichermaßen.
4. **Sicherheitsrelevante Korrekturen mit `SICHERHEITSFIX <ID>` markieren**
   und einen Test ergänzen, der vor der Korrektur fehlschlägt.
5. **Grenzen setzen statt hoffen**: neue Formate brauchen Obergrenzen für
   Größe, Anzahl und Verschachtelung.

## 8. Erweiterungsideen

* **Wochen- und Tagesansicht** – die Datenschicht liefert bereits
  `occurrences(start, end)`; es fehlt nur die Darstellung.
* **Erinnerungen** als Android-Benachrichtigung (`plyer.notification` bzw.
  `AlarmManager` über `pyjnius`); `reminder_minutes` wird bereits gespeichert
  und synchronisiert.
* **Weitere Muster**: gelernte Teilnehmer, automatische Farbzuordnung je
  Ereignisart, Vorschlag freier Zeitfenster.
* **Widgets für den Startbildschirm** über `pyjnius`.
