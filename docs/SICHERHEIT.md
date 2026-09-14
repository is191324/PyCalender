# Sicherheitskonzept

Dieses Dokument beschreibt, **wogegen** PyCalendar schützt und **wie**.
Die konkreten Angriffsversuche und die daraus entstandenen Korrekturen
stehen im [Penetrationstest-Bericht](PENTEST.md).

## 1. Was ist hier schützenswert?

| Gut | Warum kritisch |
|---|---|
| Kalenderinhalte | Bewegungsprofil, Gesundheitsdaten (Arzttermine), Geschäftsgeheimnisse |
| Zugangsdaten zu Google/Outlook/CalDAV | Vollzugriff auf fremde Konten |
| Gerätezugriff der App | Ein Fehler in der App ist ein Fehler auf dem Telefon |
| Verfügbarkeit | Eine App, die bei einem manipulierten Feed abstürzt, ist unbrauchbar |

## 2. Angreifermodelle

| # | Angreifer | Fähigkeiten |
|---|---|---|
| A1 | **Bösartige Importdatei** | CSV/ICS aus dem Netz, per Mail oder aus einem geteilten Ordner |
| A2 | **Bösartiger oder gekaperter Server** | antwortet beliebig auf CalDAV/ICS-Anfragen, kann umleiten |
| A3 | **Netzwerkangreifer** | öffentliches WLAN, DNS-Manipulation, TLS-Downgrade-Versuch |
| A4 | **Person mit dem entsperrten Gerät** | Neugieriger Bekannter, Finder, Dieb |
| A5 | **Andere App auf dem Gerät** | versucht, Dateien der App zu lesen |

Ausdrücklich **nicht** im Modell: ein gerootetes Gerät mit aktiver
Schadsoftware und physischer Zugriff auf einen entsperrten, laufenden
Speicher. Dagegen kann eine App-seitige Maßnahme nicht verlässlich schützen.

## 3. Maßnahmen im Überblick

### 3.1 Eingaben (A1)

* `sanitize_text()` entfernt Steuerzeichen **und vollständige
  ANSI-Escape-Sequenzen**, normalisiert Unicode (NFKC), löscht unsichtbare
  Formatierungszeichen (Bidi-Overrides – „Trojan Source") und begrenzt die
  Länge auf 10 000 Zeichen (Titel: 500).
* **Keine Formeln aus fremden Dateien**: `sanitize_csv_cell()` stellt beim
  Export `'` vor Zellen, die mit `= + - @ TAB CR LF` beginnen. Damit kann
  eine importierte CSV keine `=HYPERLINK(...)`-Falle in den Export
  schmuggeln (OWASP „CSV Injection").
* Harte Obergrenzen: CSV 25 MB / 50 000 Zeilen, ICS 25 MB / 20 000 Termine /
  200 Eigenschaften je Termin / 200 EXDATEs, XML 20 MB, HTTP-Antwort 25 MB.
* Serienregeln werden beim Expandieren auf 2000 Vorkommen und feste
  Schleifenzähler begrenzt – auch `COUNT=999999999` friert nichts ein.

### 3.2 Datenbank

* **Ausnahmslos parametrisierte Abfragen.** Wo eine Liste von IDs nötig ist,
  werden ausschließlich Platzhalter (`?,?,?`) erzeugt, nie Werte
  interpoliert. Ein statischer Test (`test_all_sql_uses_parameters`)
  überwacht das dauerhaft.
* Suchbegriffe werden für `LIKE` maskiert (`%`, `_`, `\` mit `ESCAPE '\'`),
  damit `%` nicht die gesamte Datenbank ausliest.
* `PRAGMA trusted_schema=OFF`, Fremdschlüssel an, Längenlimit gesetzt.
* Datenbankdatei mit `0600` im privaten App-Verzeichnis (`0700`).

### 3.3 Zugangsdaten (A4, A5)

* `CredentialVault`: **AES-256-GCM**, wahlweise über `cryptography` oder
  `pycryptodome` – zwei etablierte Bibliotheken mit identischem Tokenformat,
  damit der Android-Build nicht an einer fehlenden Rust-Toolchain scheitert
  (eigene Kryptografie wird nirgends geschrieben). Der Schlüssel wird abgeleitet per
  **scrypt (N=2¹⁴, r=8, p=1)** aus einem 32-Byte-Gerätegeheimnis, das mit
  `0600` und `O_CREAT|O_EXCL`-artigem Ablauf angelegt wird. Das Magic-Wort
  dient als zusätzliche authentifizierte Daten (AAD) – manipulierte Token
  scheitern.
* Passwörter verlassen die Sync-Schicht nicht: `SyncManager.accounts()`
  entfernt `secret_blob`, bevor die Oberfläche die Daten sieht.
* `wipe()` überschreibt das Gerätegeheimnis mit Zufall und löscht es –
  alle gespeicherten Passwörter werden damit unbrauchbar.

### 3.4 App-Sperre (A4)

* PIN als **PBKDF2-HMAC-SHA256 mit 310 000 Iterationen** und
  16-Byte-Zufallssalz (OWASP-Empfehlung 2023), Vergleich zeitkonstant.
* `RateLimiter`: ab dem fünften Fehlversuch exponentiell wachsende Sperre
  (30 s, 60 s, 120 s …), die auch bei anschließend korrekter PIN greift.
* Beim Zurückkehren aus dem Hintergrund wird erneut gefragt (`on_resume`).

### 3.5 Netzwerk (A2, A3)

* **TLS ist nicht abschaltbar.** `verify=True` steht fest im Code; es gibt
  keinen Schalter, keine Einstellung und keinen Parameter, der die
  Zertifikatsprüfung ausschaltet. Ein statischer Test verbietet
  entsprechende Schlüsselwörter im gesamten Quelltext.
* **Zugangsdaten ausschließlich über HTTPS.** Auch der
  Entwicklungsschalter `allow_insecure` hebt das nicht auf (siehe Befund
  PT-C4 im Pentest-Bericht).
* **SSRF-Schutz**: `validate_sync_url()` erlaubt nur `https`/`webcals`,
  lehnt Zugangsdaten in der URL ab und löst den Hostnamen auf, um private,
  Loopback-, Link-Local-, Multicast- und reservierte Adressen zu sperren –
  insbesondere `169.254.169.254` (Cloud-Metadaten).
* **Weiterleitungen** werden manuell verfolgt (max. 3) und **jede**
  Zieladresse erneut geprüft; der `Authorization`-Header wird nach einem
  Hostwechsel neu berechnet, damit er nicht an fremde Server gerät.
* Antworten werden **gestreamt** und beim Überschreiten des Limits
  abgebrochen, statt sie komplett in den Speicher zu laden.
* `redact()` entfernt Passwörter und Token aus Fehlermeldungen, bevor sie
  gespeichert oder angezeigt werden.

### 3.6 XML und iCalendar (A2)

* CalDAV-Antworten werden mit **defusedxml** gelesen. Fehlt das Paket,
  lehnt die App Antworten mit `<!DOCTYPE`/`<!ENTITY` rundweg ab. Damit sind
  **XXE** (`file:///etc/passwd`) und **Billion Laughs** ausgeschlossen.
* Der iCalendar-Parser ist bewusst selbst geschrieben: rein zeilenbasiert,
  ohne Ausführung, ohne externe Referenzen, mit Zeilen-, Tiefen- und
  Mengenbegrenzung.

### 3.7 Dateien

* `safe_output_path()` erzwingt, dass Exporte im vorgesehenen Verzeichnis
  bleiben: Pfadanteile werden abgeschnitten, gefährliche Zeichen ersetzt,
  das Ergebnis gegen das Basisverzeichnis geprüft (auch gegen
  Symlink-Ausbrüche).
* Hintergrundbilder werden in das App-Verzeichnis **kopiert**; ein Theme
  kann nur auf Dateien darin verweisen, nur mit PNG/JPG/WEBP-Endung und
  höchstens 12 MB.
* Exportdateien werden mit `0600` angelegt.

### 3.8 Android-Plattform

| Maßnahme | Umsetzung |
|---|---|
| Minimale Rechte | nur `INTERNET`, `POST_NOTIFICATIONS`, `READ_MEDIA_IMAGES` |
| Kein Klartext-HTTP | `android:usesCleartextTraffic="false"` im Manifest |
| Keine Cloud-Sicherung | `android.allow_backup = False` (sonst landen DB und Schlüssel im Google-Backup) |
| Privater Speicher | alle Daten unter dem App-eigenen Verzeichnis, nicht im gemeinsamen Speicher |
| Kein `READ_CALENDAR` | die App liest keine fremden Kalender-Provider; getestet in F3 |

## 4. Was die App bewusst *nicht* tut

* Keine Telemetrie, keine Analyse-SDKs, keine Werbe-IDs.
* Keine Kontaktaufnahme zu anderen Servern als den vom Nutzer eingetragenen.
* Kein `eval`, `exec`, `pickle`, `os.system` oder `shell=True` – dauerhaft
  durch einen statischen Test abgesichert.

## 5. Bekannte Restrisiken

| Risiko | Bewertung | Umgang |
|---|---|---|
| **DNS-Rebinding** (Host löst beim zweiten Aufruf auf eine interne IP auf) | gering: betrifft nur selbst eingetragene Server | Prüfung vor jeder Anfrage; vollständige Abwehr bräuchte Pinning der IP in der Verbindung |
| Datenbank ist **nicht** vollverschlüsselt | mittel bei gerootetem Gerät | Passwörter sind einzeln verschlüsselt; für Vollverschlüsselung wäre SQLCipher nötig (siehe Ausblick) |
| Kivy-eigene Abhängigkeiten (SDL2, OpenSSL im APK) | extern | Versionen in `buildozer.spec` pinnen und regelmäßig aktualisieren |
| PIN statt biometrischer Sperre | gering | Biometrie ließe sich über `plyer`/`pyjnius` ergänzen |

## 6. Ausblick

1. **SQLCipher** für eine vollverschlüsselte Datenbank.
2. **OAuth 2.0 + PKCE** für Google/Microsoft statt App-Passwörtern.
3. **Certificate Pinning** für bekannte Anbieter.
4. Signierte Release-Builds mit reproduzierbarer Erstellung.
