#!/usr/bin/env bash
# Install the powerctl command and register its agent skill with the coding
# agents you use.
#
#   ./install.sh                 install the CLI, then every agent that is present
#   ./install.sh --cli-only      install just the command
#   ./install.sh --claude        install the CLI and the Claude Code skill
#   ./install.sh --codex         install the CLI and the Codex prompt and skill
#   ./install.sh --copilot       install the CLI and the Copilot skill
#   ./install.sh --uninstall     remove the agent skills (leaves the CLI in place)
set -euo pipefail

repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
skill_src="$repo_dir/skills/powerctl"

want_cli=true
want_claude=false
want_codex=false
want_copilot=false
uninstall=false
explicit=false

for arg in "$@"; do
    case "$arg" in
        --cli-only) explicit=true ;;
        --claude) want_claude=true; explicit=true ;;
        --codex) want_codex=true; explicit=true ;;
        --copilot) want_copilot=true; explicit=true ;;
        --all) want_claude=true; want_codex=true; want_copilot=true; explicit=true ;;
        --uninstall) uninstall=true; want_cli=false ;;
        -h|--help) sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "Unknown option: $arg" >&2; exit 2 ;;
    esac
done

# With no options, install for whichever agents are already set up on this machine.
if ! $explicit && ! $uninstall; then
    [ -d "$HOME/.claude" ] && want_claude=true
    [ -d "$HOME/.codex" ] && want_codex=true
    { [ -d "$HOME/.copilot" ] || [ -d "$HOME/.agents" ]; } && want_copilot=true
fi

link_skill() {
    # $1 destination directory for the skill folder, $2 label
    local dest="$1" label="$2"
    mkdir -p "$(dirname "$dest")"
    if [ -e "$dest" ] && [ ! -L "$dest" ]; then
        echo "  $label: $dest exists and is not a symlink, leaving it alone" >&2
        return
    fi
    ln -sfn "$skill_src" "$dest"
    echo "  $label: $dest -> $skill_src"
}

if $uninstall; then
    echo "==> Removing agent skills"
    for path in "$HOME/.claude/skills/powerctl" "$HOME/.codex/skills/powerctl" \
                "$HOME/.copilot/skills/powerctl" "$HOME/.agents/skills/powerctl"; do
        if [ -L "$path" ]; then rm -f "$path"; echo "  removed $path"; fi
    done
    rm -f "$HOME/.codex/prompts/powerctl.md" 2>/dev/null && echo "  removed $HOME/.codex/prompts/powerctl.md"
    echo "The powerctl command itself is untouched; remove it with 'uv tool uninstall powerctl'."
    exit 0
fi

if $want_cli; then
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
fi

if $want_claude; then
    echo "==> Claude Code"
    link_skill "$HOME/.claude/skills/powerctl" "skill"
fi

if $want_codex; then
    echo "==> Codex"
    link_skill "$HOME/.codex/skills/powerctl" "skill"
    mkdir -p "$HOME/.codex/prompts"
    cp "$repo_dir/prompts/powerctl.md" "$HOME/.codex/prompts/powerctl.md"
    echo "  prompt: $HOME/.codex/prompts/powerctl.md (use /powerctl in Codex)"
fi

if $want_copilot; then
    echo "==> GitHub Copilot"
    if [ -d "$HOME/.copilot" ] || ! [ -d "$HOME/.agents" ]; then
        link_skill "$HOME/.copilot/skills/powerctl" "skill"
    else
        link_skill "$HOME/.agents/skills/powerctl" "skill"
    fi
    echo "  reload with '/skills reload' in an existing session"
fi

if [ -d "$repo_dir/.git" ]; then
    hook="$repo_dir/.git/hooks/pre-commit"
    printf '#!/usr/bin/env bash\nexec "%s/scripts/check-repo-clean.sh" --history\n' "$repo_dir" > "$hook"
    chmod +x "$hook"
fi

echo
echo "==> Done"
$want_cli && echo "    CLI: $(command -v powerctl || echo 'not on PATH yet; add ~/.local/bin')"
echo "    Next: run 'powerctl discover' to find the outlets on your network."
echo "    Device names and credentials stay on this machine, under ~/.config/powerctl."
