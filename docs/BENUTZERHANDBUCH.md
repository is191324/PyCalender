# Benutzerhandbuch

## 1. Überblick

PyCalendar hat fünf Bereiche, die über die Leiste am unteren Rand erreichbar
sind:

| Bereich | Zweck |
|---|---|
| **Monat** | Monatsraster mit Punkten für Termine, darunter die Tagesliste |
| **Agenda** | Kommende Termine als Liste, mit Suchfeld |
| **Import** | CSV einlesen, Vorschau, und – besonders wichtig – Importe wieder entfernen |
| **Konten** | Google, Outlook, CalDAV oder ICS-Abos verbinden und synchronisieren |
| **Design** | Farben, Hintergrund, Schriftgröße, PIN-Sperre, gelernte Muster |

## 2. Termine anlegen und bearbeiten

1. Im Monat auf einen Tag tippen, dann oben rechts auf **+**.
2. Titel eingeben. **Sobald der Titel einem früheren Termin entspricht,
   erscheint unter dem Feld ein Vorschlag**, zum Beispiel:
   `Vorschlag: 1h 30min (aus 6 früheren Terminen) | meist 18:00 Uhr | Rhythmus: wöchentlich (Di)`
   Dauer, Ort und Wiederholung werden dann automatisch vorbelegt; alles lässt
   sich überschreiben.
3. **Speichern**.

**Serientermine**: Über das Auswahlfeld `einmalig / täglich / wöchentlich /
alle 2 Wochen / monatlich / jährlich`. Beim Löschen eines Serientermins
fragt die App, ob nur dieser Termin oder die ganze Serie entfernt werden
soll. „Nur dieser Termin" legt einen Ausnahmetermin (EXDATE) an, der auch
korrekt zum Server synchronisiert wird.

## 3. Muster: gelernte Dauer pro Ereignisname

Das ist die Besonderheit der App. Jedes Mal, wenn ein Termin gespeichert
(oder synchronisiert oder importiert) wird, merkt sich PyCalendar zum Namen:

* die **Dauer** (das Kernstück),
* die typische **Startzeit**,
* die **Wochentage**,
* **Ort**, Farbe und Erinnerung.

Wichtige Eigenschaften:

* **Namen werden normalisiert.** „Sport", „sport ", „SPORT 20:00" und
  „Sport #3" gehören zum selben Muster: Groß-/Kleinschreibung, Uhrzeiten,
  Zahlen und Sonderzeichen werden ignoriert.
* **Median statt Mittelwert.** Ein einzelner Ausreißer („das Meeting ging
  ausnahmsweise 6 Stunden") verschiebt den Vorschlag nicht.
* **Teiltreffer.** Wer „Physio" tippt, bekommt den Vorschlag von
  „Physiotherapie" – mit erkennbar geringerer Sicherheit.
* **Dauer festnageln.** Der Knopf *„Dauer für diesen Namen merken"* im
  Editor fixiert den Wert; die Statistik überschreibt ihn dann nicht mehr.
* **Rhythmus-Erkennung.** Taucht derselbe Name mehrfach in gleichem Abstand
  auf, schlägt die App eine passende Wiederholung vor (täglich,
  wöchentlich an einem bestimmten Wochentag, alle zwei Wochen, monatlich,
  jährlich).
* **Vergessen.** Unter *Design → Gelernte Muster* lässt sich jedes Muster
  einzeln löschen.

## 4. CSV-Import – und wie man ihn leicht wieder los wird

### Import

1. **Import → Datei wählen** (oder Pfad eintippen).
2. **Vorschau** zeigt je Zeile, was erkannt wurde, welche Zeilen Duplikate
   sind und welche fehlerhaft (mit Grund, z. B. „Startdatum nicht lesbar").
3. **Importieren**.

Erkannt werden deutsche und englische Spaltenüberschriften
(`Betreff/Titel/Subject`, `Startdatum/Start/Beginn/Von`, `Ende/End/Bis`,
`Dauer/Duration/Minuten`, `Ort/Location`, `Beschreibung/Notes`,
`Ganztägig/All day`, `Wiederholung/RRULE`, `Erinnerung/Reminder`) sowie die
Trennzeichen `;`, `,`, Tabulator und `|`. Datumsformate: `14.09.2026 09:00`,
`2026-09-14T09:00`, `09/14/2026` und weitere. Dauer: `90`, `1:30`, `1h30`.

Fehlt in der Datei eine Dauer, greift **die gelernte Dauer zum Ereignisnamen** –
sonst 60 Minuten.

### Wieder löschen

Jeder Import wird als **Stapel** gespeichert und erscheint als Karte mit
Dateiname, Datum und der Anzahl noch aktiver Termine. Dazu gibt es:

| Knopf | Wirkung |
|---|---|
| **Import rückgängig** | entfernt genau die Termine dieses Imports – nach Rückfrage, mit einem Tipp |
| **Anzeigen** | listet alle Termine des Stapels; jede Zeile hat ihren eigenen Löschknopf |
| **Duplikate entfernen** | behält je Titel+Startzeit einen Eintrag, löscht die übrigen |
| **Nach Regel löschen** | löscht importierte Termine nach Titel und/oder Zeitraum („alle ‚Standby' im Oktober") |

Selbst angelegte Termine sind davon **nie** betroffen – die Regel-Löschung
arbeitet standardmäßig nur auf importierten Einträgen.

Auf der Kommandozeile geht dasselbe:

```bash
python3 main.py --import termine.csv   # nennt die Stapelnummer
python3 main.py --undo 3
```

## 5. Synchronisierung

### Google Kalender

1. **Konten → Konto hinzufügen**, Anbieter `google`.
2. Benutzername = vollständige Gmail-Adresse.
3. Passwort = **App-Passwort** (Google-Konto → Sicherheit → App-Passwörter).
   Bei aktiver Zwei-Faktor-Anmeldung funktioniert das normale Passwort nicht.

### Outlook / Microsoft 365

* Für **Lesen** genügt das ICS-Abo: in Outlook.com unter
  *Einstellungen → Kalender → Freigegebene Kalender* eine ICS-Adresse
  erzeugen und als Anbieter `ics` eintragen.
* Für **Schreiben** einen CalDAV-fähigen Zugang verwenden (Anbieter
  `outlook` bzw. `caldav`).

### Beliebige CalDAV-Server

Nextcloud, Radicale, Fastmail, mailbox.org: Anbieter `caldav`, Adresse des
Kalender-Hauptordners eintragen. Die App findet die enthaltenen Kalender
selbst.

### Ablauf und Konflikte

* Synchronisiert wird beim Start, danach alle 30 Minuten und auf Knopfdruck –
  immer in einem Hintergrund-Thread, die Oberfläche bleibt bedienbar.
* Fenster: ±400 Tage um heute.
* Wurde ein Termin **auf beiden Seiten** geändert, gewinnt die neuere
  Fassung; die unterlegene bleibt als Kopie „… (Konflikt)" erhalten. Es geht
  nichts verloren.
* Schreibgeschützte Kalender (ICS-Abos, fremdfreigegebene Kalender) werden
  erkannt und nie überschrieben.

## 6. Aussehen anpassen

*Design & Einstellungen* bietet:

* **Sechs Vorlagen**: Hell, Dunkel, Mitternachtsblau, Waldgrün, Sepia,
  Kontraststark (Letztere mit größerer Schrift für schlechte Sichtverhältnisse).
* **Zwölf Einzelfarben** als Hex-Wert (`#RRGGBB`), jeweils mit Farbfeld zur
  Kontrolle: Hintergrund, Flächen, Flächen (2), Text, Text gedämpft, Akzent,
  Schrift auf Akzent, Raster, Heute, Wochenende, Warnung, Erfolg.
* **Hintergrund**: Volltonfarbe, Verlauf oder eigenes Bild (PNG/JPG/WEBP)
  mit einstellbarer Deckkraft – so bleibt der Text lesbar.
* **Eckenrundung** (0–30) und **Schriftgröße** (0,8–1,6).
* **Kontrastwarnung**: Unterschreitet eine Kombination 4,5:1 (WCAG AA),
  weist die App darauf hin, statt sie zu verbieten.

Alles wird sofort übernommen und dauerhaft gespeichert.

## 7. Sicherheit im Alltag

* **PIN-Sperre** (*Design → Sicherheit*): mindestens vier Ziffern. Nach fünf
  Fehlversuchen sperrt die App zunehmend länger. Beim Zurückkehren aus dem
  Hintergrund wird erneut gefragt.
* **Zugangsdaten** werden mit AES-256-GCM verschlüsselt abgelegt, nie im
  Klartext.
* Details: [SICHERHEIT.md](SICHERHEIT.md).

## 8. Daten sichern

* *Design → CSV exportieren* schreibt eine Datei ins App-Verzeichnis
  (`export/kalender.csv`), die sich in Excel oder LibreOffice öffnen lässt.
* *Aufräumen* entfernt endgültig gelöschte Einträge (älter als 30 Tage) und
  baut die gelernten Muster aus den vorhandenen Terminen neu auf.
