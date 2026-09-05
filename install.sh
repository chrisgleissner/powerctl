#!/usr/bin/env bash
# Install the powerctl CLI and link the Claude Code skill so it is available in
# every project on this machine.
set -euo pipefail

repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
skills_dir="${CLAUDE_SKILLS_DIR:-$HOME/.claude/skills}"
link="$skills_dir/powerctl"

echo "==> Installing the powerctl command"
if command -v uv >/dev/null 2>&1; then
    uv tool install --force --editable "$repo_dir"
elif command -v pipx >/dev/null 2>&1; then
    pipx install --force -e "$repo_dir"
else
    echo "Neither uv nor pipx found. Install one of them, or run:" >&2
    echo "  python3 -m pip install --user -e $repo_dir" >&2
    exit 1
fi

echo "==> Linking the Claude Code skill into $skills_dir"
mkdir -p "$skills_dir"
if [ -e "$link" ] && [ ! -L "$link" ]; then
    echo "$link exists and is not a symlink; leaving it alone." >&2
    exit 1
fi
ln -sfn "$repo_dir/skills/powerctl" "$link"

echo "==> Installing the pre-commit leak check"
if [ -d "$repo_dir/.git" ]; then
    hook="$repo_dir/.git/hooks/pre-commit"
    printf '#!/usr/bin/env bash\nexec "%s/scripts/check-repo-clean.sh"\n' "$repo_dir" > "$hook"
    chmod +x "$hook"
    echo "    $hook"
fi

echo "==> Done"
echo "    CLI:   $(command -v powerctl || echo 'not on PATH yet; add ~/.local/bin')"
echo "    Skill: $link -> $repo_dir/skills/powerctl"
echo
echo "Next: run 'powerctl discover' to find the outlets on your network."
echo "Device names and credentials stay on this machine, under ~/.config/powerctl."
