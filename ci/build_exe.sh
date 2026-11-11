#!/bin/bash
set -e
python3 - <<'PY'
h=open('index.html',encoding='utf-8').read()
for f in ['manifest.webmanifest','icon.svg','icon-192.png','apple-touch-icon.png']:
    h=h.replace('href="'+f+'"','href="/'+f+'"')
open('pc/app.html','w',encoding='utf-8').write(h)
PY
pyinstaller --onefile --noconsole --icon pc/icon.ico --name ClinicSync \
  --add-data "pc/app.html;." --add-data "pc/sw.js;." --add-data "pc/manifest.webmanifest;." \
  --add-data "pc/icon.svg;." --add-data "pc/jameel.woff;." \
  --add-data "pc/icon-192.png;." --add-data "pc/icon-512.png;." --add-data "pc/icon-maskable-512.png;." --add-data "pc/apple-touch-icon.png;." \
  pc/clinic_sync.py

# ---- make a real installer (Setup wizard, Start-menu + desktop shortcut, uninstaller) ----
export CLINIC_VER=$(python -c "import json;print(json.load(open('version.json'))['v'])")
mkdir -p build_app && cp dist/ClinicSync.exe build_app/ClinicSync.exe
ISCC="/c/Program Files (x86)/Inno Setup 6/ISCC.exe"
if [ ! -f "$ISCC" ]; then choco install innosetup -y --no-progress || true; fi
if [ -f "$ISCC" ]; then
  cp dist/ClinicSync.exe dist/ClinicSync-portable.exe
  "$ISCC" ci/clinic.iss && echo "installer built" || { echo "installer failed - keeping portable exe"; cp dist/ClinicSync-portable.exe dist/ClinicSync.exe; }
else
  echo "Inno Setup missing - keeping portable exe"
fi
ls -la dist
