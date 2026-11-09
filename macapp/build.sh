#!/bin/zsh
# Builds "Sportsbook Lines.app" and installs it in /Applications
# (or ~/Applications if /Applications isn't writable).
#   macapp/build.sh            build and install
#   macapp/build.sh --no-install   build into macapp/build only
set -euo pipefail
cd "$(dirname "$0")"
NAME="Sportsbook Lines"
BUILD=build
APP="$BUILD/$NAME.app"

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

swiftc -O -swift-version 5 -target arm64-apple-macos13 \
  -framework Cocoa -framework WebKit -framework ServiceManagement \
  -o "$APP/Contents/MacOS/SportsbookLines" SportsbookLines.swift

# App icon from the home-screen icon
ICONSET="$BUILD/AppIcon.iconset"
rm -rf "$ICONSET"; mkdir -p "$ICONSET"
SRC=../lines_agent/static/icon-512.png
for s in 16 32 128 256 512; do
  sips -z $s $s "$SRC" --out "$ICONSET/icon_${s}x${s}.png" >/dev/null
  d=$((s * 2)); [[ $d -le 512 ]] && sips -z $d $d "$SRC" --out "$ICONSET/icon_${s}x${s}@2x.png" >/dev/null
done
cp "$SRC" "$ICONSET/icon_512x512@2x.png"  # upscaled slot; the source is 512px
iconutil -c icns "$ICONSET" -o "$APP/Contents/Resources/AppIcon.icns"

cat > "$APP/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>Sportsbook Lines</string>
  <key>CFBundleDisplayName</key><string>Sportsbook Lines</string>
  <key>CFBundleIdentifier</key><string>com.sportsbook-lines-agent.app</string>
  <key>CFBundleExecutable</key><string>SportsbookLines</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>CFBundleVersion</key><string>1</string>
  <key>LSMinimumSystemVersion</key><string>13.0</string>
  <key>LSApplicationCategoryType</key><string>public.app-category.sports</string>
  <key>NSHighResolutionCapable</key><true/>
  <key>NSAppTransportSecurity</key>
  <dict><key>NSAllowsLocalNetworking</key><true/></dict>
</dict>
</plist>
PLIST

codesign --force --sign - "$APP" >/dev/null
echo "Built $APP"

if [[ "${1:-}" != "--no-install" ]]; then
  DEST=/Applications
  [[ -w $DEST ]] || { DEST="$HOME/Applications"; mkdir -p "$DEST"; }
  pkill -x SportsbookLines 2>/dev/null && sleep 1 || true
  rm -rf "$DEST/$NAME.app"
  cp -R "$APP" "$DEST/"
  echo "Installed $DEST/$NAME.app"
fi
