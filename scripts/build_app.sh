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
    <!-- Required whenever the app spawns scribed itself: ffmpeg captures the
         mic as a child of the daemon, so TCC attributes the request to this
         bundle. With no usage string macOS KILLS the requesting process instead
         of prompting — and Microphone has no "+" button in System Settings, so
         there is no way to grant it by hand. Symptom: ffmpeg exits instantly,
         logging only its banner and no error at all. (Under launchd the daemon
         is a child of launchd, not of us, and carries its own grant.) -->
    <key>NSMicrophoneUsageDescription</key>
    <string>Meeting Scribe records your microphone to transcribe meetings.</string>
    <!-- Menu-bar-only app: no Dock icon; the window opens on demand. -->
    <key>LSUIElement</key>               <true/>
    <key>NSHighResolutionCapable</key>   <true/>
</dict>
</plist>
PLIST

# Sign with the stable local identity. Not cosmetic — see scripts/signing.sh.
# shellcheck source=scripts/signing.sh
source "$REPO/scripts/signing.sh"
sign_with_local_identity "$OUT"

if [[ "${1:-}" == "--install" ]]; then
    echo "→ installing to /Applications"
    rm -rf "/Applications/MeetingScribe.app"
    cp -R "$OUT" "/Applications/MeetingScribe.app"
fi

echo "✓ built: $OUT"
