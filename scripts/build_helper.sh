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
# actually opens the SCStream, so it needs the stable designated requirement
# even more than the app does — the fallback warning is not decorative.
# shellcheck source=scripts/signing.sh
source "$HERE/scripts/signing.sh"
sign_with_local_identity "$OUT"

echo "Built: $OUT"
