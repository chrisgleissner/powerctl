Use the `powerctl` command to work with the smart plugs on this network.

Start by reading the usage guide, which is the authority on the command:
`~/.codex/skills/powerctl/SKILL.md` if installed, otherwise the copy in the powerctl
repository at `skills/powerctl/SKILL.md`.

Safety rules that apply to every session:

- `powerctl discover`, `list`, `status` and `probe` are read-only. Run them freely.
- Never run `powerctl off` or `powerctl cycle` without my explicit permission for that
  specific device in this conversation.
- To check what a command would do, add `--dry-run`. It runs every safety check and
  changes nothing. Never verify a guard by actually switching a device.
- Never pass `--force-protected`. Never edit `~/.config/powerctl/*.json`.
- Exit code 3 means a guard refused the action: report it, do not work around it.
- Exit code 5 means power could not be restored after a cycle: tell me at once and run
  `powerctl on <device>`.

Every command accepts `--json`; prefer it and parse the result.

$ARGUMENTS
