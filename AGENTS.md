# AGENTS.md

Instructions for coding agents working in this repository, and for agents that use the
`powerctl` command on a real network. Codex reads this file automatically; GitHub Copilot
reads `.github/copilot-instructions.md`, which points here; Claude Code loads the skill in
`skills/powerctl/SKILL.md`.

## What this project is

`powerctl` is a command line tool that discovers TP-Link Kasa and Tapo smart plugs, reads
their power state and energy figures, switches them, and power cycles machines that have
no reset line. `skills/powerctl/SKILL.md` is the agent-facing usage guide and is the
authority on how to drive the command.

## Using the command against real hardware

**Never cut power without the user's explicit permission for that specific device in the
current conversation.** `discover`, `list`, `status` and `probe` are read-only and safe to
run. `off` and `cycle` interrupt power to real hardware: they lose work, spoil food and
take networks down.

* Verify guard behaviour with `--dry-run`, never by switching a real device. `--dry-run`
  runs every safety check and reports the outcome without touching anything.
* Never pass `--force-protected`, and never edit or delete `~/.config/powerctl/*.json`.
* Exit code 3 means a safety guard refused the action. Report it; do not work around it.
* Exit code 5 means a power cycle could not restore power. Say so immediately and run
  `powerctl on <device>`.

## Working on the code

```bash
uv venv .venv && uv pip install --python .venv/bin/python -e ".[dev]"
.venv/bin/python -m pytest                  # coverage gate: 95%
.venv/bin/ruff check src tests && .venv/bin/ruff format --check src tests
./scripts/check-repo-clean.sh --history     # no private network details or secrets
./scripts/sync-agent-skills.sh --check      # skill copies match the canonical file
```

Conventions that matter here:

* Safety rules live in `powerctl.core`, never in the argument parser, so library callers
  get the same guards as the CLI. A change that weakens a guard needs a test that proves
  the new behaviour is intended.
* Tests run against stub devices only: no network traffic, no hardware, no real config
  directory. Never add a test that talks to a device.
* Vendor-specific code belongs in `src/powerctl/backends/`. Each adapter owns its device
  families exclusively, because these devices accept one session at a time.
* Nothing in this repository may contain a private IP address, a MAC address, an email
  address, a token, a WiFi name, or the local state files. Use the RFC 5737 documentation
  ranges, `AA:BB:CC:DD:EE:FF` and `example.com` in examples and tests.
* `skills/powerctl/SKILL.md` is canonical; run `scripts/sync-agent-skills.sh` after
  editing it so the Copilot copies stay identical.
