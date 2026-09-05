#!/usr/bin/env bash
# Refuse to publish local setup: no private network addresses, no real MAC
# addresses, no credential or registry files, no device aliases from a real home
# network. Run before every push; the pre-commit hook installed by install.sh
# runs it too.
#
# Documentation values are allowed on purpose: the RFC 5737 example ranges
# (192.0.2.0/24, 198.51.100.0/24, 203.0.113.0/24) and the example MAC
# AA:BB:CC:DD:EE:FF.
set -uo pipefail

cd "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
status=0

files=$(git ls-files 2>/dev/null || find . -type f -not -path './.git/*' -not -path './.venv/*')

report() {
    echo "LEAK: $1" >&2
    status=1
}

# Private IPv4 addresses (RFC 1918) and common home ranges.
hits=$(printf '%s\n' "$files" | xargs -r grep -nE \
    '\b(10\.[0-9]{1,3}|192\.168\.[0-9]{1,3}|172\.(1[6-9]|2[0-9]|3[01])\.[0-9]{1,3})\.[0-9]{1,3}\b' \
    -- 2>/dev/null | grep -v '^scripts/check-repo-clean.sh:')
[ -n "$hits" ] && report "private IP address found:"$'\n'"$hits"

# MAC addresses other than the documentation example.
hits=$(printf '%s\n' "$files" | xargs -r grep -niE '\b([0-9a-f]{2}:){5}[0-9a-f]{2}\b' -- 2>/dev/null \
    | grep -viE 'aa:bb:cc:dd:ee:(ff|00)' | grep -v '^scripts/check-repo-clean.sh:')
[ -n "$hits" ] && report "MAC address found:"$'\n'"$hits"

# Local state files must never be tracked.
for name in credentials.json devices.json; do
    tracked=$(git ls-files -- "*$name" 2>/dev/null)
    [ -n "$tracked" ] && report "local state file tracked: $tracked"
done

# Anything that looks like a stored password.
hits=$(printf '%s\n' "$files" | xargs -r grep -nE '"password"[[:space:]]*:[[:space:]]*"[^"]+"' -- 2>/dev/null \
    | grep -viE 'password123|hunter2hunter2|password456|"\*\*\*"|\{.*\}' | grep -v '^scripts/check-repo-clean.sh:')
[ -n "$hits" ] && report "possible stored password:"$'\n'"$hits"

if [ "$status" -eq 0 ]; then
    echo "repo clean: no private addresses, MACs, credentials or local state files"
fi
exit "$status"
