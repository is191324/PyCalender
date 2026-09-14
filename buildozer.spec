[app]
title = PyCalendar
package.name = pycalendar
package.domain = de.example.pycalendar
source.dir = .
source.include_exts = py,png,jpg,jpeg,webp,kv,atlas,ttf,json,md
source.exclude_dirs = tests,docs,build,bin,.git,__pycache__
version = 1.0.0

# Kivy + Netz + Krypto. pyjnius wird für Android-APIs benoetigt.
requirements = python3,kivy==2.3.0,requests,urllib3,certifi,chardet,idna,openssl,cryptography,cffi,pycparser,defusedxml,plyer,android,pyjnius
# Falls sich `cryptography` in der Build-Umgebung nicht übersetzen lässt
# (es braucht eine Rust-Toolchain für Android), stattdessen diese Zeile
# verwenden - die App erkennt den Unterbau selbst und das Format der
# gespeicherten Zugangsdaten bleibt identisch:
# requirements = python3,kivy==2.3.0,requests,urllib3,certifi,chardet,idna,openssl,pycryptodome,defusedxml,plyer,android,pyjnius

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

# Kein Klartext-HTTP: Android schaltet unverschlüsselten Verkehr für Apps mit
# targetSdk >= 28 bereits von sich aus ab (usesCleartextTraffic=false ist der
# Vorgabewert), und die App erzwingt HTTPS zusätzlich im Code - siehe
# pycal/sync/http.py. Eine eigene Manifest-Zeile ist daher nicht nötig.

p4a.branch = master

[buildozer]
log_level = 2
warn_on_root = 1
