#!/usr/bin/env bash
# Bygg Zeus-appen og publiser den til Olympus: telefonen ser den nye versjonen neste gang appen åpnes
# («⇪ Zeus x.y er klar på PC-en – [ oppdater ]»). Kopi legges også i ~/Olympus/output/zeus-x.y.apk.
set -euo pipefail
cd "$(dirname "$0")/../zeus-app"
nice ./gradlew -q assembleRelease --no-daemon
apk=app/build/outputs/apk/release/app-release.apk
code=$(grep -oP 'versionCode = \K[0-9]+' app/build.gradle.kts)
name=$(grep -oP 'versionName = "\K[^"]+' app/build.gradle.kts)
dest="$HOME/.olympus/app"
mkdir -p "$dest" "$HOME/Olympus/output"
cp "$apk" "$dest/zeus.apk.tmp" && mv "$dest/zeus.apk.tmp" "$dest/zeus.apk"
printf '{"code": %s, "name": "%s"}\n' "$code" "$name" > "$dest/version.json"
cp "$apk" "$HOME/Olympus/output/zeus-$name.apk"
echo "Zeus $name (kode $code) publisert – appen tilbyr oppdateringen ved neste åpning."
