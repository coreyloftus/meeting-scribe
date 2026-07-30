#!/bin/bash
# Build the ScreenCaptureKit system-audio helper.
# Output: bin/syscap  (a small native binary, gitignored).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC="$HERE/helper/syscap.swift"
OUT="$HERE/bin/syscap"

if ! command -v swiftc >/dev/null 2>&1; then
  echo "swiftc not found. Install Xcode Command Line Tools:  xcode-select --install" >&2
  exit 1
fi

mkdir -p "$HERE/bin"
echo "Compiling $SRC → $OUT"
swiftc -O \
  -framework ScreenCaptureKit \
  -framework AVFoundation \
  -framework CoreMedia \
  "$SRC" -o "$OUT"

# Sign with the same stable identity as the app. syscap is the process that
# actually opens the SCStream, so give it a stable identity too rather than the
# linker-signed ad-hoc default. See scripts/setup_signing.sh.
SIGN_ID="Meeting Scribe Local Signing"
SIGN_KC="$HOME/Library/Keychains/meeting-scribe-signing.keychain-db"
if [[ -f "$SIGN_KC" ]] && security find-identity "$SIGN_KC" 2>/dev/null | grep -qF "$SIGN_ID"; then
  security unlock-keychain -p meetingscribe "$SIGN_KC"
  codesign --force -s "$SIGN_ID" --keychain "$SIGN_KC" "$OUT"
fi

echo "Built: $OUT"
