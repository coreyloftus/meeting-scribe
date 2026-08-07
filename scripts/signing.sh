#!/bin/bash
# Shared code-signing settings. Sourced by build_app.sh, build_helper.sh and
# setup_signing.sh — not meant to be run directly.
#
# Why a real identity instead of `codesign -s -`: an ad-hoc signature has no
# designated requirement, so TCC pins the Screen Recording grant to the binary's
# cdhash and every rebuild silently revokes it. See scripts/setup_signing.sh.

SIGN_CN="Meeting Scribe Local Signing"
SIGN_KC_NAME="meeting-scribe-signing.keychain"
SIGN_KC="$HOME/Library/Keychains/$SIGN_KC_NAME-db"
# Dev-only passphrase. It guards a self-signed, untrusted, local-build-only cert
# and is deliberately well-known so builds never prompt. Don't reuse it.
SIGN_KC_PASS="meetingscribe"

# True when the stable identity is present in the keychain.
#
# Do NOT add `-v` here. Our cert is self-signed and deliberately untrusted, so
# it reports CSSMERR_TP_NOT_TRUSTED and `find-identity -v` lists "0 valid
# identities" — every build would then silently fall back to ad-hoc, which is
# the exact grant-revoking behavior this whole setup exists to prevent.
# `codesign` itself is happy with an untrusted cert; the `--verify` in
# sign_with_local_identity is what actually confirms the signature took.
have_signing_identity() {
    [[ -f "$SIGN_KC" ]] \
        && security find-identity -p codesigning "$SIGN_KC" 2>/dev/null \
        | grep -qF "$SIGN_CN"
}

# sign_with_local_identity <path>
# Signs with the stable identity, or falls back to ad-hoc with a loud warning —
# an ad-hoc binary loses its Screen Recording grant on every single rebuild.
sign_with_local_identity() {
    local target="$1"
    local name; name="$(basename "$target")"

    if have_signing_identity; then
        echo "→ codesign $name with \"$SIGN_CN\""
        security unlock-keychain -p "$SIGN_KC_PASS" "$SIGN_KC"
        codesign --force -s "$SIGN_CN" --keychain "$SIGN_KC" "$target"
    else
        echo "⚠ no local signing identity — falling back to ad-hoc for $name." >&2
        echo "  Screen Recording permission will break on every rebuild." >&2
        echo "  Fix once with:  bash scripts/setup_signing.sh" >&2
        codesign --force -s - "$target"
    fi

    codesign --verify --strict "$target"
    codesign -d -r- "$target" 2>&1 | grep designated || true
}
