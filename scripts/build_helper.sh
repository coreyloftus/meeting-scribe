#!/bin/bash
# Build the native capture helpers.
# Outputs: bin/syscap (system audio, ScreenCaptureKit)
#          bin/miccap (microphone, AVAudioEngine)
# Both are small native binaries, gitignored.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if ! command -v swiftc >/dev/null 2>&1; then
  echo "swiftc not found. Install Xcode Command Line Tools:  xcode-select --install" >&2
  exit 1
fi

mkdir -p "$HERE/bin"

# Both helpers open a TCC-protected resource — syscap the SCStream, miccap the
# microphone — so both need the stable designated requirement rather than the
# linker-signed ad-hoc default. The fallback warning is not decorative.
# shellcheck source=scripts/signing.sh
source "$HERE/scripts/signing.sh"

build() {
  local name="$1"; shift
  local src="$HERE/helper/$name.swift"
  local out="$HERE/bin/$name"
  echo "Compiling $src → $out"
  swiftc -O "$@" "$src" -o "$out"
  sign_with_local_identity "$out"
  echo "Built: $out"
}

build syscap \
  -framework ScreenCaptureKit \
  -framework AVFoundation \
  -framework CoreMedia

# miccap carries its own Info.plist in __TEXT,__info_plist. Without a
# NSMicrophoneUsageDescription TCC kills it on the first buffer instead of
# prompting, and Microphone has no "+" button in System Settings to repair it
# by hand. See helper/miccap-Info.plist.
build miccap \
  -framework AVFoundation \
  -framework CoreAudio \
  -Xlinker -sectcreate \
  -Xlinker __TEXT -Xlinker __info_plist -Xlinker "$HERE/helper/miccap-Info.plist"
