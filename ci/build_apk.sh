#!/bin/bash
set -e
mkdir -p www
cp index.html manifest.webmanifest sw.js icon.svg icon-192.png icon-512.png icon-maskable-512.png apple-touch-icon.png jameel.woff www/
echo '{"appId":"com.salman.clinic","appName":"Clinic","webDir":"www","server":{"androidScheme":"http","cleartext":true}}' > capacitor.config.json
npm init -y
npm i @capacitor/core@6 @capacitor/cli@6 @capacitor/android@6
mkdir -p ~/.android && echo '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' | base64 -d > ~/.android/debug.keystore
npx cap add android
cp ~/.android/debug.keystore android/app/ci-debug.keystore
python3 - <<'PY'
p='android/app/build.gradle'
s=open(p).read()
s=s.replace("android {","android {\n    signingConfigs {\n        debug {\n            storeFile file('ci-debug.keystore')\n            storePassword 'android'\n            keyAlias 'androiddebugkey'\n            keyPassword 'android'\n        }\n    }",1)
open(p,'w').write(s)
PY
rm -rf android/app/src/main/res/mipmap-anydpi-v26 android/app/src/main/res/drawable/splash.png android/app/src/main/res/drawable-port-* android/app/src/main/res/drawable-land-* android/app/src/main/res/drawable-v24/ic_launcher_foreground.xml android/app/src/main/res/drawable/ic_launcher_background.xml
cp -r ci/res/* android/app/src/main/res/
mkdir -p android/app/src/main/java/com/salman/clinic
cp ci/java/*.java android/app/src/main/java/com/salman/clinic/
grep -q WRITE_EXTERNAL android/app/src/main/AndroidManifest.xml || sed -i 's#<application#<uses-permission android:name="android.permission.WRITE_EXTERNAL_STORAGE" android:maxSdkVersion="28" />\n    <application#' android/app/src/main/AndroidManifest.xml
grep -q REQUEST_INSTALL_PACKAGES android/app/src/main/AndroidManifest.xml || sed -i 's#<application#<uses-permission android:name="android.permission.REQUEST_INSTALL_PACKAGES" />\n    <application#' android/app/src/main/AndroidManifest.xml
VN=$(python3 -c "import json;print(json.load(open('version.json'))['v'])")
VC=$(python3 -c "import json;p=json.load(open('version.json'))['v'].split('.');print(int(p[0])*1000000+int(p[1])*10000+int(p[2]))")
sed -i "s/versionCode [0-9]*/versionCode $VC/; s/versionName \"[^\"]*\"/versionName \"$VN\"/" android/app/build.gradle
npx cap sync android
cd android && chmod +x gradlew && ./gradlew assembleDebug
cd .. && cp android/app/build/outputs/apk/debug/app-debug.apk ClinicApp.apk
FP=$(keytool -printcert -jarfile ClinicApp.apk | grep 'SHA256' | head -1)
echo "APK signature: $FP"
echo "$FP" | grep -q 'CD:A8:99:B6' || { echo 'ERROR: APK was NOT signed with the fixed key'; exit 1; }
