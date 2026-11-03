#!/bin/bash
set -e
python3 - <<'PY'
h=open('index.html',encoding='utf-8').read()
for f in ['manifest.webmanifest','icon.svg','icon-192.png','apple-touch-icon.png']:
    h=h.replace('href="'+f+'"','href="/'+f+'"')
open('pc/app.html','w',encoding='utf-8').write(h)
PY
pyinstaller --onefile --icon pc/icon.ico --name ClinicSync \
  --add-data "pc/app.html;." --add-data "pc/sw.js;." --add-data "pc/manifest.webmanifest;." \
  --add-data "pc/icon.svg;." --add-data "pc/jameel.woff;." \
  --add-data "pc/icon-192.png;." --add-data "pc/icon-512.png;." --add-data "pc/icon-maskable-512.png;." --add-data "pc/apple-touch-icon.png;." \
  pc/clinic_sync.py
