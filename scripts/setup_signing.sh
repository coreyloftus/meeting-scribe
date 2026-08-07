#!/bin/bash
# Create (once) a stable local code-signing identity for MeetingScribe.
#
# Why this exists: an ad-hoc signature (`codesign -s -`) has no Team ID and no
# designated requirement, so TCC pins the grant to the binary's cdhash:
#
#     designated => cdhash H"d3ae2da1..."
#
# Every rebuild changes that hash, silently invalidating the Screen Recording
# grant — System Settings still lists the app as allowed, but tccd refuses to
# match it and re-prompts forever. A self-signed cert yields instead:
#
#     designated => identifier "com.meetingscribe.app" and certificate leaf = H"..."
#
# which is stable across rebuilds, so the grant survives.
#
# The key lives in a dedicated keychain with a known password so signing never
# needs your login-keychain password. Idempotent: safe to re-run.
#
#   bash scripts/setup_signing.sh
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=scripts/signing.sh
source "$HERE/scripts/signing.sh"

CN="$SIGN_CN"
KC_NAME="$SIGN_KC_NAME"
KC_PATH="$SIGN_KC"
KC_PASS="$SIGN_KC_PASS"
# Kept outside the repo: this holds a private key, never commit it.
STORE="$HOME/.config/meeting-scribe/signing"

if have_signing_identity; then
    echo "✓ signing identity already present in $KC_NAME"
    security find-identity -p codesigning "$KC_PATH" 2>/dev/null | grep -F "$CN"
    exit 0
fi

echo "→ creating signing identity \"$CN\""
mkdir -p "$STORE"
chmod 700 "$STORE"

if [[ ! -f "$STORE/ms-signing.p12" ]]; then
    TMP="$(mktemp -d)"
    trap 'rm -rf "$TMP"' EXIT
    cat > "$TMP/codesign.cnf" <<EOF
[ req ]
distinguished_name = dn
x509_extensions = v3
prompt = no

[ dn ]
CN = $CN

[ v3 ]
basicConstraints = critical,CA:false
keyUsage = critical,digitalSignature
extendedKeyUsage = critical,codeSigning
subjectKeyIdentifier = hash
EOF
    openssl req -x509 -newkey rsa:2048 -nodes -days 3650 \
        -keyout "$TMP/ms-signing.key" -out "$TMP/ms-signing.crt" \
        -config "$TMP/codesign.cnf" 2>/dev/null
    # -certpbe/-keypbe/-macalg: OpenSSL 3 defaults to algorithms macOS's
    # Security framework cannot read ("MAC verification failed" on import).
    openssl pkcs12 -export -out "$TMP/ms-signing.p12" \
        -inkey "$TMP/ms-signing.key" -in "$TMP/ms-signing.crt" \
        -certpbe PBE-SHA1-3DES -keypbe PBE-SHA1-3DES -macalg sha1 \
        -passout "pass:$KC_PASS" 2>/dev/null
    cp "$TMP/ms-signing.p12" "$TMP/ms-signing.crt" "$STORE/"
    chmod 600 "$STORE/ms-signing.p12"
fi

if [[ ! -f "$KC_PATH" ]]; then
    security create-keychain -p "$KC_PASS" "$KC_NAME"
    security set-keychain-settings "$KC_NAME"     # no auto-lock timeout
fi
security unlock-keychain -p "$KC_PASS" "$KC_NAME"

# -T /usr/bin/codesign + set-key-partition-list: let codesign — and only
# codesign — use the key without popping a GUI "allow access" dialog on every
# build. Deliberately NOT `-A`, which would hand the key to every process on
# the machine, letting anything sign code that inherits this app's TCC grants.
security import "$STORE/ms-signing.p12" -k "$KC_NAME" -P "$KC_PASS" \
    -T /usr/bin/codesign
security set-key-partition-list -S apple-tool:,apple:,codesign: \
    -s -k "$KC_PASS" "$KC_NAME" >/dev/null 2>&1

# Add to the user search list so `codesign -s` can find it. Read the current
# entries into an array — keychain paths may contain spaces, which word
# splitting would silently shred, dropping the user's other keychains.
if ! security list-keychains -d user | grep -qF "$KC_NAME"; then
    EXISTING=()
    while IFS= read -r line; do
        line="${line#"${line%%[![:space:]]*}"}"   # strip leading whitespace
        line="${line#\"}"; line="${line%\"}"      # strip surrounding quotes
        [[ -n "$line" ]] && EXISTING+=("$line")
    done < <(security list-keychains -d user)
    if [[ ${#EXISTING[@]} -gt 0 ]]; then
        security list-keychains -d user -s "${EXISTING[@]}" "$KC_PATH"
    else
        security list-keychains -d user -s "$KC_PATH"
    fi
fi

echo "✓ identity ready (the cert is self-signed and untrusted, which is fine —"
echo "  codesign accepts it; only Gatekeeper distribution would need trust)"
security find-identity -p codesigning "$KC_PATH" 2>/dev/null | grep -F "$CN"
echo
echo "NOTE: switching off ad-hoc changes the designated requirement, so any"
echo "  Screen Recording grant you already have is now stale. After the next"
echo "  build you will be asked for it once more. If macOS refuses to re-prompt"
echo "  because System Settings still lists a stale entry, clear it with:"
echo "      tccutil reset ScreenCapture com.meetingscribe.app"
echo "  From then on the grant survives rebuilds."
