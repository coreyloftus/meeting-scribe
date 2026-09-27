#!/bin/bash
# Regenerate app/Resources/AppIcon.icns from scripts/make_icon.swift.
# Run by hand when the icon changes; the .icns is committed, not rebuilt per build.
#   SDKROOT=/Library/Developer/CommandLineTools/SDKs/MacOSX26.5.sdk bash scripts/make_icon.sh
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

swift "$REPO/scripts/make_icon.swift" "$WORK/icon_1024.png"

SET="$WORK/AppIcon.iconset"
mkdir -p "$SET"
for sz in 16 32 128 256 512; do
    sips -z "$sz" "$sz" "$WORK/icon_1024.png" --out "$SET/icon_${sz}x${sz}.png" >/dev/null
    dbl=$((sz * 2))
    sips -z "$dbl" "$dbl" "$WORK/icon_1024.png" --out "$SET/icon_${sz}x${sz}@2x.png" >/dev/null
done

mkdir -p "$REPO/app/Resources"
iconutil -c icns "$SET" -o "$REPO/app/Resources/AppIcon.icns"
echo "✓ wrote $REPO/app/Resources/AppIcon.icns"
