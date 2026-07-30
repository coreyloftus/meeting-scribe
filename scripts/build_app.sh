#!/bin/bash
# Build MeetingScribe.app from the Swift package in app/ (personal, unsigned build).
#   bash scripts/build_app.sh [--install]     # --install copies to /Applications
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
APP_SRC="$REPO/app"
OUT="$APP_SRC/dist/MeetingScribe.app"

# Compile with swiftc directly — no SwiftPM needed (the app has no external
# dependencies, and bare CommandLineTools' SwiftPM manifest lib can be broken).
echo "→ swiftc (release)…"
BUILD_DIR="$APP_SRC/.build"
mkdir -p "$BUILD_DIR"
BIN="$BUILD_DIR/MeetingScribe"
swiftc -O -parse-as-library \
    -target arm64-apple-macos14.0 \
    "$APP_SRC"/Sources/MeetingScribe/*.swift \
    -o "$BIN"

echo "→ assembling bundle at $OUT"
rm -rf "$OUT"
mkdir -p "$OUT/Contents/MacOS" "$OUT/Contents/Resources"
cp "$BIN" "$OUT/Contents/MacOS/MeetingScribe"

cat > "$OUT/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key>              <string>Meeting Scribe</string>
    <key>CFBundleDisplayName</key>       <string>Meeting Scribe</string>
    <key>CFBundleIdentifier</key>        <string>com.meetingscribe.app</string>
    <key>CFBundleVersion</key>           <string>0.2.0</string>
    <key>CFBundleShortVersionString</key><string>0.2.0</string>
    <key>CFBundleExecutable</key>        <string>MeetingScribe</string>
    <key>CFBundlePackageType</key>       <string>APPL</string>
    <key>LSMinimumSystemVersion</key>    <string>14.0</string>
    <!-- Required. ffmpeg captures the mic as a child of scribed, which the app
         spawns, so TCC attributes the request to this bundle. With no usage
         string macOS KILLS the requesting process instead of prompting — and
         Microphone has no "+" button in System Settings, so there is no way to
         grant it by hand. Symptom: ffmpeg exits instantly, logging only its
         banner and no error at all. -->
    <key>NSMicrophoneUsageDescription</key>
    <string>Meeting Scribe records your microphone to transcribe meetings.</string>
    <!-- Menu-bar-only app: no Dock icon; the window opens on demand. -->
    <key>LSUIElement</key>               <true/>
    <key>NSHighResolutionCapable</key>   <true/>
</dict>
</plist>
PLIST

# Sign with the stable local identity if it exists. This is not cosmetic: an
# ad-hoc signature has no designated requirement, so TCC pins the Screen
# Recording grant to the binary's cdhash and every rebuild silently revokes it.
# See scripts/setup_signing.sh.
SIGN_ID="Meeting Scribe Local Signing"
SIGN_KC="$HOME/Library/Keychains/meeting-scribe-signing.keychain-db"
if [[ -f "$SIGN_KC" ]] && security find-identity "$SIGN_KC" 2>/dev/null | grep -qF "$SIGN_ID"; then
    echo "→ codesign with \"$SIGN_ID\""
    security unlock-keychain -p meetingscribe "$SIGN_KC"
    codesign --force -s "$SIGN_ID" --keychain "$SIGN_KC" "$OUT"
else
    echo "⚠ no local signing identity — falling back to ad-hoc." >&2
    echo "  Screen Recording permission will break on every rebuild." >&2
    echo "  Fix once with:  bash scripts/setup_signing.sh" >&2
    codesign --force -s - "$OUT"
fi
codesign -d -r- "$OUT" 2>&1 | grep designated || true

if [[ "${1:-}" == "--install" ]]; then
    echo "→ installing to /Applications"
    rm -rf "/Applications/MeetingScribe.app"
    cp -R "$OUT" "/Applications/MeetingScribe.app"
fi

echo "✓ built: $OUT"
