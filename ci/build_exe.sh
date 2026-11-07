#!/bin/bash
set -e
python3 - <<'PY'
h=open('index.html',encoding='utf-8').read()
for f in ['manifest.webmanifest','icon.svg','icon-192.png','apple-touch-icon.png']:
    h=h.replace('href="'+f+'"','href="/'+f+'"')
open('pc/app.html','w',encoding='utf-8').write(h)
PY
mkdir -p pc/drivers
curl -L --retry 5 -s -o pc/drivers/vc_redist.x64.exe https://aka.ms/vs/17/release/vc_redist.x64.exe || true
curl -L --retry 5 -s -o pc/drivers/accessdatabaseengine_X64.exe "https://download.microsoft.com/download/3/5/C/35C84C36-661A-44E6-9324-8786B8DBE231/accessdatabaseengine_X64.exe" || true
ls -l pc/drivers
for f in pc/drivers/*.exe; do [ "$(wc -c < "$f")" -gt 5000000 ] || { echo "driver download incomplete: $f"; exit 1; }; done
pyinstaller --onefile --add-data "pc/drivers;drivers" --icon pc/icon.ico --name ClinicSync \
  --add-data "pc/app.html;." --add-data "pc/sw.js;." --add-data "pc/manifest.webmanifest;." \
  --add-data "pc/icon.svg;." --add-data "pc/jameel.woff;." \
  --add-data "pc/icon-192.png;." --add-data "pc/icon-512.png;." --add-data "pc/icon-maskable-512.png;." --add-data "pc/apple-touch-icon.png;." \
  pc/clinic_sync.py
