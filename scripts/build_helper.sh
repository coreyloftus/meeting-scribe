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
echo "Built: $OUT"
