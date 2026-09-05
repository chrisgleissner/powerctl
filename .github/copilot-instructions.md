# Copilot instructions

This repository builds `powerctl`, a command line tool that discovers TP-Link Kasa and
Tapo smart plugs, reads their power draw, switches them, and power cycles machines that
have no reset line.

Read [AGENTS.md](../AGENTS.md) for the full working agreement, and
[.github/skills/powerctl/SKILL.md](skills/powerctl/SKILL.md) for how to drive the command.

The rule that overrides everything else: **never cut power to a device without the user's
explicit permission for that specific device in the current conversation.** Use
`powerctl ... --dry-run` to check what a command would do; it runs every safety check and
changes nothing. Never pass `--force-protected`, and never edit `~/.config/powerctl/*.json`.

Before proposing changes, run the checks listed in AGENTS.md: pytest with its 95% coverage
gate, ruff, `scripts/check-repo-clean.sh --history`, and
`scripts/sync-agent-skills.sh --check`.
