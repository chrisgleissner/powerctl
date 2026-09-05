#!/usr/bin/env bash
# Keep the user's own network and credentials out of this repository.
#
# The repository is meant to be publishable: nothing in it should reveal what
# hardware someone owns, what their addresses are, or any secret. This script
# fails on
#
#   * private IPv4 addresses (RFC 1918) and link-local addresses,
#   * MAC addresses other than the documentation example AA:BB:CC:DD:EE:FF,
#   * email addresses outside the reserved example domains,
#   * common credential and token shapes,
#   * WiFi network names and pre-shared keys,
#   * the local state files, which belong in ~/.config/powerctl.
#
# Documentation values are allowed on purpose: the RFC 5737 ranges
# (192.0.2.0/24, 198.51.100.0/24, 203.0.113.0/24), the example MAC above, and
# the reserved example.com / example.org / example.net domains.
#
# Usage:
#   check-repo-clean.sh              check the tracked working tree
#   check-repo-clean.sh --history    also check every commit reachable from any ref
set -uo pipefail

cd "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
status=0
self="scripts/check-repo-clean.sh"

report() {
    echo "LEAK: $1" >&2
    status=1
}

# Patterns are kept in one place so the working tree check and the history check
# cannot drift apart.
PRIVATE_IP='\b(10\.[0-9]{1,3}|192\.168\.[0-9]{1,3}|172\.(1[6-9]|2[0-9]|3[01])\.[0-9]{1,3}|169\.254)\.[0-9]{1,3}\b'
MAC='\b([0-9a-fA-F]{2}[:-]){5}[0-9a-fA-F]{2}\b'
# The local part must end in a letter or digit, so decorators such as
# "@pytest.fixture" are not mistaken for addresses.
EMAIL='[A-Za-z0-9._%+-]*[A-Za-z0-9]@[A-Za-z0-9.-]+\.[A-Za-z]{2,}'
TOKEN='(ghp_|gho_|ghu_|ghs_|github_pat_|AKIA[0-9A-Z]{16}|xox[baprs]-|sk-[A-Za-z0-9]{20,}|-----BEGIN [A-Z ]*PRIVATE KEY-----)'
WIFI='(ssid|psk|passphrase|wpa_passwd)[[:space:]]*[=:][[:space:]]*["'"'"'][^"'"'"']+'
PASSWORD_VALUE='"password"[[:space:]]*:[[:space:]]*"[^"]+"'

# Values that are documentation, test fixtures or code identifiers.
ALLOWED='192\.0\.2\.|198\.51\.|203\.0\.113\.|aa:bb:cc:dd:ee:(ff|00)|AA:BB:CC:DD:EE:(FF|00)|aa-bb-cc-dd-ee-ff|AA-BB-CC-DD-EE-FF|@example\.(com|org|net)|password123|password456|hunter2hunter2|"\*\*\*"|"password": "p"|"password": password|"password": kasa_creds|\{.*\}'

scan_working_tree() {
    local files
    files=$(git ls-files 2>/dev/null || find . -type f -not -path './.git/*' -not -path './.venv/*')
    local hits

    hits=$(printf '%s\n' "$files" | xargs -r grep -nEI "$PRIVATE_IP" -- 2>/dev/null | grep -v "^$self:")
    [ -n "$hits" ] && report "private IP address:"$'\n'"$hits"

    hits=$(printf '%s\n' "$files" | xargs -r grep -nEI "$MAC" -- 2>/dev/null \
        | grep -viE "$ALLOWED" | grep -v "^$self:")
    [ -n "$hits" ] && report "MAC address:"$'\n'"$hits"

    hits=$(printf '%s\n' "$files" | xargs -r grep -nEI "$EMAIL" -- 2>/dev/null \
        | grep -viE "$ALLOWED|security@codecov\.io|noreply@|git@github\.com" | grep -v "^$self:")
    [ -n "$hits" ] && report "email address:"$'\n'"$hits"

    hits=$(printf '%s\n' "$files" | xargs -r grep -nEI "$TOKEN" -- 2>/dev/null | grep -v "^$self:")
    [ -n "$hits" ] && report "credential or token:"$'\n'"$hits"

    hits=$(printf '%s\n' "$files" | xargs -r grep -nEI "$WIFI" -- 2>/dev/null | grep -v "^$self:")
    [ -n "$hits" ] && report "WiFi network name or key:"$'\n'"$hits"

    hits=$(printf '%s\n' "$files" | xargs -r grep -nEI "$PASSWORD_VALUE" -- 2>/dev/null \
        | grep -viE "$ALLOWED" | grep -v "^$self:")
    [ -n "$hits" ] && report "stored password:"$'\n'"$hits"

    local tracked
    for name in credentials.json devices.json protected.json; do
        tracked=$(git ls-files -- "*$name" 2>/dev/null)
        [ -n "$tracked" ] && report "local state file is tracked: $tracked"
    done
}

scan_history() {
    local added hits
    # Strip the leading '+' of each added line so diff markers cannot become
    # part of a match.
    added=$(git log -p --all --no-color 2>/dev/null | grep -E '^\+' | grep -v '^+++' | sed 's/^+//')
    [ -z "$added" ] && return

    for pattern_name in "private IP address:$PRIVATE_IP" "MAC address:$MAC" \
                        "email address:$EMAIL" "credential or token:$TOKEN" \
                        "WiFi network name or key:$WIFI" "stored password:$PASSWORD_VALUE"; do
        local label="${pattern_name%%:*}"
        local pattern="${pattern_name#*:}"
        hits=$(printf '%s\n' "$added" | grep -EI "$pattern" \
            | grep -viE "$ALLOWED|security@codecov\.io|noreply@|git@github\.com|grep -|ALLOWED=|PRIVATE_IP=|MAC=|EMAIL=|TOKEN=|WIFI=|PASSWORD_VALUE=" \
            | head -5)
        [ -n "$hits" ] && report "$label (in git history)"$'\n'"$hits"
    done
}

scan_working_tree
[ "${1:-}" = "--history" ] && scan_history

if [ "$status" -eq 0 ]; then
    echo "repo clean: no private addresses, MACs, emails, credentials, WiFi details or local state files"
fi
exit "$status"
