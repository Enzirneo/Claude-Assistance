#!/bin/bash
# Gera ccwatch.app (macOS) com o ícone do projeto. O .app roda o código desta pasta.
set -e
cd "$(dirname "$0")"
APP=ccwatch.app
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

# ícone: icon.ico -> .iconset -> .icns
TMP=$(mktemp -d)
sips -s format png icon.ico --out "$TMP/base.png" >/dev/null
mkdir "$TMP/ccwatch.iconset"
for s in 16 32 128 256; do
  sips -z $s $s "$TMP/base.png" --out "$TMP/ccwatch.iconset/icon_${s}x${s}.png" >/dev/null
  sips -z $((s*2)) $((s*2)) "$TMP/base.png" --out "$TMP/ccwatch.iconset/icon_${s}x${s}@2x.png" >/dev/null
done
iconutil -c icns "$TMP/ccwatch.iconset" -o "$APP/Contents/Resources/ccwatch.icns"
rm -rf "$TMP"

cat > "$APP/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>ccwatch</string>
  <key>CFBundleDisplayName</key><string>ccwatch</string>
  <key>CFBundleIdentifier</key><string>local.ccwatch</string>
  <key>CFBundleExecutable</key><string>ccwatch</string>
  <key>CFBundleIconFile</key><string>ccwatch</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleVersion</key><string>1</string>
  <key>LSUIElement</key><true/>
</dict></plist>
PLIST

# guarda o caminho do projeto: o .app pode ser movido (ex.: para /Applications)
printf '%s\n' "$PWD" > "$APP/Contents/Resources/project_path"

cat > "$APP/Contents/MacOS/ccwatch" <<'LAUNCH'
#!/bin/bash
DIR="$(cat "$(dirname "$0")/../Resources/project_path")"
cd "$DIR" || exit 1
exec /usr/bin/python3 server.py
LAUNCH
chmod +x "$APP/Contents/MacOS/ccwatch"
echo "Gerado: $PWD/$APP"
