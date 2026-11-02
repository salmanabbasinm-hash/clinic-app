#!/bin/bash
set -e
pyinstaller --onefile --icon pc/icon.ico --name ClinicSync \
  --add-data "pc/app.html;." --add-data "pc/sw.js;." --add-data "pc/manifest.webmanifest;." \
  --add-data "pc/icon.svg;." --add-data "pc/jameel.woff;." \
  --add-data "pc/icon-192.png;." --add-data "pc/icon-512.png;." --add-data "pc/icon-maskable-512.png;." --add-data "pc/apple-touch-icon.png;." \
  pc/clinic_sync.py
