#!/usr/bin/env bash
# Copy the canonical skill to the directories each agent tool looks in.
#
# The skill is written once, in skills/powerctl/SKILL.md, which is where Claude
# Code plugins expect it. GitHub Copilot looks in .github/skills or .agents/skills
# instead, and neither tool follows symlinks reliably across platforms, so the
# file is copied and CI checks that the copies match.
#
#   sync-agent-skills.sh            update the copies
#   sync-agent-skills.sh --check    fail if a copy is out of date
set -euo pipefail

cd "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

source_file="skills/powerctl/SKILL.md"
targets=(".github/skills/powerctl/SKILL.md" ".agents/skills/powerctl/SKILL.md")
check_only=false
[ "${1:-}" = "--check" ] && check_only=true
status=0

for target in "${targets[@]}"; do
    if $check_only; then
        if ! cmp -s "$source_file" "$target"; then
            echo "OUT OF DATE: $target differs from $source_file" >&2
            echo "Run scripts/sync-agent-skills.sh to update it." >&2
            status=1
        fi
    else
        mkdir -p "$(dirname "$target")"
        cp "$source_file" "$target"
        echo "wrote $target"
    fi
done

$check_only && [ "$status" -eq 0 ] && echo "agent skill copies are in sync"
exit "$status"
