#!/bin/bash
VER=$(python3 -c "import json;print(json.load(open('version.json'))['v'])" 2>/dev/null || python -c "import json;print(json.load(open('version.json'))['v'])")
export VER
if [ -z "$UPDATE_TOKEN" ]; then echo 'no UPDATE_TOKEN - skipped'; exit 0; fi
gh release view latest -R $UPD_REPO >/dev/null 2>&1 || gh release create latest -R $UPD_REPO -t "Latest" -n "Clinic app"
NAME=$(basename "$1")
WANT=$(wc -c < "$1" | tr -d ' ')
OKUP=0
for i in 1 2 3 4 5; do
  gh release delete-asset latest "$NAME" -R $UPD_REPO -y >/dev/null 2>&1 || true
  gh release upload latest "$1" -R $UPD_REPO --clobber || true
  GOT=$(gh release view latest -R $UPD_REPO --json assets -q ".assets[] | select(.name==\"$NAME\") | .size" 2>/dev/null || echo 0)
  echo "upload try $i: want=$WANT got=$GOT"
  if [ "$GOT" = "$WANT" ]; then OKUP=1; break; fi
  sleep 8
done
if [ "$OKUP" != "1" ]; then echo "ERROR: $NAME was not uploaded to $UPD_REPO"; exit 1; fi
if [ "$NAME" = "HealthClinic.exe" ]; then gh release delete-asset latest ClinicSync.exe -R $UPD_REPO -y >/dev/null 2>&1 || true; fi
ASSETS=$(gh release view latest -R $UPD_REPO --json assets -q ".assets[].name" 2>/dev/null)
EXE=ClinicSync.exe; echo "$ASSETS" | grep -qx HealthClinic.exe && EXE=HealthClinic.exe
git clone -q https://x-access-token:$UPDATE_TOKEN@github.com/$UPD_REPO.git upd && cd upd
B=https://github.com/$UPD_REPO/releases/latest/download
NOTES=$(python3 -c "import json;print(json.dumps(json.load(open('../version.json')).get('notes','')))" 2>/dev/null || echo '""')
PLAT=exev; case "$NAME" in *.apk) PLAT=apkv;; esac
export PLAT B EXE VER
python3 - <<'PY'
import json,os
try: d=json.load(open('version.json'))
except Exception: d={}
n=json.load(open('../version.json')).get('notes','')
d['v']=os.environ['VER']; d['notes']=n
d[os.environ['PLAT']]=os.environ['VER']
d['apk']=os.environ['B']+'/ClinicApp.apk'; d['exe']=os.environ['B']+'/'+os.environ['EXE']
d['web']='https://salmanabbasinm-hash.github.io/clinic-app/'
json.dump(d,open('version.json','w'),ensure_ascii=False)
PY
cat > index.html <<H
<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1"><title>Salman Herbal Clinic - Downloads</title>
<body dir=rtl style="font-family:sans-serif;max-width:420px;margin:0 auto;padding:24px 16px;text-align:center;background:#f4f7f4">
<h2 style="color:#1b5e20">سلمان ہربل کلینک</h2><p>اپنی مرضی کی ایپ ڈاؤنلوڈ کریں</p>
<a style="display:block;padding:18px;margin:14px 0;background:#2e7d32;color:#fff;border-radius:14px;text-decoration:none;font-size:20px" href="$B/ClinicApp.apk">📱 Android ایپ (APK)</a>
<a style="display:block;padding:18px;margin:14px 0;background:#1565c0;color:#fff;border-radius:14px;text-decoration:none;font-size:20px" href="$B/$EXE">💻 Windows ایپ (EXE)</a>
<a style="display:block;padding:18px;margin:14px 0;background:#6a1b9a;color:#fff;border-radius:14px;text-decoration:none;font-size:20px" href="https://salmanabbasinm-hash.github.io/clinic-app/">🍎 iPhone / ویب ایپ</a>
<small dir=ltr>v $VER</small>
H
git config user.name bot; git config user.email bot@users.noreply.github.com
git add -A; git commit -qm "version $VER" || true; for i in 1 2 3; do git pull -q --rebase; git push -q && break; sleep 5; done