[app]
title = PyCalendar
package.name = pycalendar
package.domain = de.example.pycalendar
source.dir = .
source.include_exts = py,png,jpg,jpeg,webp,kv,atlas,ttf,json,md
source.exclude_dirs = tests,docs,build,bin,.git,__pycache__
version = 1.0.0

# Kivy + Netz + Krypto. pyjnius wird für Android-APIs benoetigt.
requirements = python3,kivy,requests,urllib3,certifi,chardet,idna,openssl,pycryptodome,defusedxml,plyer,android,pyjnius
# Hinweise zu dieser Zeile:
# * kivy wird bewusst OHNE Versionsangabe genannt. python-for-android bringt ein
#   eigenes, auf die gebaute Python-Fassung abgestimmtes Kivy-Rezept mit (hier
#   2.3.0); eine feste Angabe setzt dieses Rezept ausser Kraft und führt zu
#   Übersetzungsfehlern wie "_PyUnicode_FastCopyCharacters" undeclared.
# * pycryptodome statt cryptography: gleichwertige Verschlüsselung (AES-256-GCM,
#   scrypt), aber ohne Rust-Werkzeugkette beim Bauen. Der Zugangsdaten-Tresor
#   erkennt den Unterbau selbst, das Format bleibt identisch.
#   Alternative mit cryptography (braucht Rust für Android):
#   requirements = python3,kivy,requests,urllib3,certifi,chardet,idna,openssl,cryptography,cffi,pycparser,defusedxml,plyer,android,pyjnius

orientation = portrait
fullscreen = 0
icon.filename = assets/icon.png
presplash.filename = assets/presplash.png

# Nur die wirklich noetigen Rechte:
#   INTERNET          - CalDAV/ICS-Synchronisierung
#   READ_MEDIA_IMAGES - Hintergrundbild wählen (Android 13+)
#   POST_NOTIFICATIONS- Terminerinnerungen (Android 13+)
# Bewusst NICHT angefordert: READ/WRITE_EXTERNAL_STORAGE, READ_CALENDAR,
# WRITE_CALENDAR, ACCESS_FINE_LOCATION, READ_CONTACTS.
android.permissions = INTERNET,POST_NOTIFICATIONS,READ_MEDIA_IMAGES

android.api = 34
android.minapi = 24
android.ndk_api = 24
android.archs = arm64-v8a,armeabi-v7a
android.allow_backup = False

# Android-SDK-Lizenz automatisch bestätigen. Ohne diese Zeile überspringt der
# SDK-Manager in einem automatischen Lauf die Build-Tools ("Skipping following
# packages as the license is not accepted") und Buildozer bricht anschließend
# mit "Aidl not found, please install it." ab.
android.accept_sdk_license = True

# Build-Tools nicht automatisch hochrüsten. Buildozer würde sonst immer die
# neueste Version nehmen - und die enthält kein "aidl" mehr, was den Build mit
# "Aidl not found, please install it." abbrechen lässt. Der GitHub-Ablauf
# installiert stattdessen gezielt eine Version, die aidl mitbringt.
android.skip_update = True

# Kein Klartext-HTTP: Android schaltet unverschlüsselten Verkehr für Apps mit
# targetSdk >= 28 bereits von sich aus ab (usesCleartextTraffic=false ist der
# Vorgabewert), und die App erzwingt HTTPS zusätzlich im Code - siehe
# pycal/sync/http.py. Eine eigene Manifest-Zeile ist daher nicht nötig.

# Feste, stabile Fassung des Android-Werkzeugs statt des master-Zweigs.
# master baut derzeit CPython 3.14 mit einer neuen Paketmechanik, an der binäre
# Pakete scheitern ("not a supported wheel on this platform", "from versions:
# none"). Diese Marke baut CPython 3.11.5 mit Kivy 2.3.0 - die seit Jahren
# erprobte Kombination.
p4a.branch = v2024.01.21

[buildozer]
log_level = 2
warn_on_root = 1
