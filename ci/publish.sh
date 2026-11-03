#!/bin/bash
VER=$(python3 -c "import json;print(json.load(open('version.json'))['v'])" 2>/dev/null || python -c "import json;print(json.load(open('version.json'))['v'])")
export VER
if [ -z "$UPDATE_TOKEN" ]; then echo 'no UPDATE_TOKEN - skipped'; exit 0; fi
gh release view latest -R $UPD_REPO >/dev/null 2>&1 || gh release create latest -R $UPD_REPO -t "Latest" -n "Clinic app"
gh release upload latest "$1" -R $UPD_REPO --clobber
git clone -q https://x-access-token:$UPDATE_TOKEN@github.com/$UPD_REPO.git upd && cd upd
B=https://github.com/$UPD_REPO/releases/latest/download
echo "{\"v\":\"$VER\",\"apk\":\"$B/ClinicApp.apk\",\"exe\":\"$B/ClinicSync.exe\",\"web\":\"https://salmanabbasinm-hash.github.io/clinic-app/\"}" > version.json
cat > index.html <<H
<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1"><title>Salman Herbal Clinic - Downloads</title>
<body dir=rtl style="font-family:sans-serif;max-width:420px;margin:0 auto;padding:24px 16px;text-align:center;background:#f4f7f4">
<h2 style="color:#1b5e20">سلمان ہربل کلینک</h2><p>اپنی مرضی کی ایپ ڈاؤنلوڈ کریں</p>
<a style="display:block;padding:18px;margin:14px 0;background:#2e7d32;color:#fff;border-radius:14px;text-decoration:none;font-size:20px" href="$B/ClinicApp.apk">📱 Android ایپ (APK)</a>
<a style="display:block;padding:18px;margin:14px 0;background:#1565c0;color:#fff;border-radius:14px;text-decoration:none;font-size:20px" href="$B/ClinicSync.exe">💻 Windows ایپ (EXE)</a>
<a style="display:block;padding:18px;margin:14px 0;background:#6a1b9a;color:#fff;border-radius:14px;text-decoration:none;font-size:20px" href="https://salmanabbasinm-hash.github.io/clinic-app/">🍎 iPhone / ویب ایپ</a>
<small dir=ltr>v $VER</small>
H
git config user.name bot; git config user.email bot@users.noreply.github.com
git add -A; git commit -qm "version $VER" || true; for i in 1 2 3; do git pull -q --rebase; git push -q && break; sleep 5; done