# powerctl

[![Build](https://github.com/chrisgleissner/powerctl/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/chrisgleissner/powerctl/actions/workflows/ci.yml)
[![codecov](https://codecov.io/gh/chrisgleissner/powerctl/graph/badge.svg)](https://codecov.io/gh/chrisgleissner/powerctl)
[![Coverage gate](https://img.shields.io/badge/coverage%20gate-95%25-brightgreen)](pyproject.toml)
[![Python](https://img.shields.io/badge/python-3.11%20%7C%203.12-blue)](https://www.python.org/)
[![Ruff](https://img.shields.io/badge/lint-ruff-261230)](https://docs.astral.sh/ruff/)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)

Find every smart outlet on your network, read what it is drawing, and switch it, from one
command. Ships with an agent skill for Claude Code, Codex and GitHub Copilot, so an agent
can do the same under the same safety rules.

Its main job is rebooting hardware that has no reset line: cut the outlet, wait, restore
it, then keep waiting until the machine answers on the network again. Cutting power is
also the one thing here that can spoil food or take a household offline, so every power
cut passes explicit guards, and a fridge or router can be marked so that no flag switches
it off.

```console
$ powerctl discover
HOST         ALIAS       MODEL  TYPE  ENERGY  FLAGS
-----------  ----------  -----  ----  ------  --------
192.0.2.10   Bench Plug  KP115  plug  yes     -
192.0.2.24   Lab Server  P110M  plug  yes     critical

$ powerctl status "Bench Plug"
Bench Plug  [on]
  host=192.0.2.10  model=KP115  type=plug  mac=AA:BB:CC:DD:EE:FF
  power=73.5 W  voltage=243.2 V  current=0.426 A
  today=0.912 kWh  month=5.349 kWh  total=56.040 kWh

$ powerctl off "Lab Server" --yes
powerctl: 'Lab Server' is protected as critical and will not be switched off by
powerctl under any flag.
```

## Supported devices

| Device family | Adapter | Library | Credentials |
| --- | --- | --- | --- |
| Kasa IOT: KP115, HS100/110, KP303, HS300 | `kasa` | [python-kasa](https://github.com/python-kasa/python-kasa) | None |
| Tapo: P100/P105/P110/P115, and P110M and newer TPAP firmware | `tapo` | [plugp100](https://github.com/petretiandrea/plugp100) | TP-Link account |

Power strips are supported through `--child <socket>`. The Tapo adapter reports watts and
kilowatt hours but not voltage or current.

Mesh nodes and cameras answer the same TP-Link discovery but have no relay, so they are
not listed. Pass `--all-devices` if you want to see them.

## Install

[![Release](https://img.shields.io/github/v/release/chrisgleissner/powerctl?display_name=tag&sort=semver)](https://github.com/chrisgleissner/powerctl/releases/latest)

There is no one-click install for agent skills on any of these tools; the closest is a
single command, given for each below. Requires Python 3.11 or newer.

### The command line tool

Install the released version straight from the repository, no clone needed:

```bash
uv tool install "git+https://github.com/chrisgleissner/powerctl@v0.1.0"   # or: pipx install
powerctl --version
```

### With the agent skill

Clone and run the installer. It installs the command, then registers the skill with every
agent tool it finds on the machine:

```bash
git clone https://github.com/chrisgleissner/powerctl.git
cd powerctl
./install.sh
```

| Flag | Effect |
| --- | --- |
| *(none)* | CLI, plus every agent tool found on the machine |
| `--cli-only` | Just the `powerctl` command |
| `--claude` | Symlinks the skill into `~/.claude/skills/powerctl` |
| `--codex` | Symlinks `~/.codex/skills/powerctl` and adds the `/powerctl` prompt |
| `--copilot` | Symlinks the skill into `~/.copilot/skills/powerctl` |
| `--uninstall` | Removes the skill links; leaves the command installed |

The skill is symlinked rather than copied, so `git pull` updates every agent at once.

### Claude Code, as a plugin

The repository is also a plugin marketplace, which gives versioned installs and updates:

```
/plugin marketplace add chrisgleissner/powerctl
/plugin install powerctl@powerctl
```

Install the `powerctl` command separately, as above: the plugin ships the skill, not the
binary. Skills load as `/powerctl:powerctl`.

### Codex

`./install.sh --codex` places the skill in `~/.codex/skills/powerctl` and a prompt in
`~/.codex/prompts/powerctl.md`, so `/powerctl` works in any session. In a repository,
Codex also reads [AGENTS.md](AGENTS.md).

### GitHub Copilot

`./install.sh --copilot` places the skill in `~/.copilot/skills/powerctl`; run
`/skills reload` in an open session, then `/skills info powerctl` to confirm. For a single
repository, copy `skills/powerctl` into that repository's `.github/skills/` instead. This
repository carries its own copies in `.github/skills/` and `.agents/skills/`, kept in sync
by `scripts/sync-agent-skills.sh`.

## Commands

| Command | What it does |
| --- | --- |
| `discover` | Broadcast scan; writes the device registry |
| `list` | Print the registry, no network traffic |
| `status [device ...]` | Power state and energy readings; `--all` for every device |
| `on <device>` | Switch on. Never needs a confirmation flag |
| `off <device> --yes` | Switch off |
| `cycle <device> --yes` | Off, wait, on, then wait for a host to return |
| `probe <host> ...` | Identify a device by address when discovery cannot reach it |
| `protect` / `unprotect` / `protected` | Manage devices that may not be switched off |
| `login` / `logout` | Store or remove the TP-Link account |
| `backends` | List the adapters this build supports |
| `doctor` | Check registry, credential sources and file permissions |

Name a device by alias, IP address, host name, MAC address, device id, or a unique alias
prefix. Every command takes `--json`; `--backend <name>` restricts it to one adapter.

| Exit code | Meaning |
| --- | --- |
| 0 | Success |
| 1 | Error, such as an unreachable device or a failed login |
| 2 | Bad arguments |
| 3 | Refused by a safety guard |
| 4 | Power was restored, but the machine did not answer before `--wait-timeout` |
| 5 | A power cycle could not switch the outlet back on. The device is still off |

## Rebooting a machine

```bash
powerctl cycle <device> --yes \
  --off-seconds 8 \
  --wait-host <machine-ip> --wait-port 80 \
  --wait-timeout 180
```

The outlet goes off, stays off for `--off-seconds`, comes back on, and the command then
polls the machine until it answers. Exit code 4 means power came back but the machine did
not; exit code 5 means power itself could not be restored, so a script can tell the three
outcomes apart. Without `--wait-port` the check is ICMP. Without `--wait-host` the command
returns as soon as power is restored.

## Safety model

Every rule is enforced in `powerctl.core`, not in the argument parser, so a caller using
the library gets the same guards as the CLI.

1. **`--yes` is required** for `off` and `cycle`. Switching on is never gated.
2. **Protected devices are refused.** `powerctl protect <device>` records the device's
   alias, address, MAC and device id; a match on any one of them protects it, so the
   device stays protected however it is addressed. `--force-protected` overrides this
   tier.
3. **Critical devices are refused under every flag.** `powerctl protect <device>
   --critical` is for a fridge, a router, a server. No option lifts it; the entry has to
   be removed from the protection file by hand.
4. **Identity is confirmed first.** A device the registry cannot fully identify is queried
   before anything is switched, and the guards run again on the identity it reports. A
   device that cannot be identified is refused rather than switched.
5. **Protections have their own file.** `~/.config/powerctl/protected.json` is separate
   from the device cache, so rebuilding or deleting the cache cannot drop a safety rule.
6. **`--dry-run`** runs every check and reports the outcome without touching the device.
   It is the only safe way to verify the guards against real hardware.
7. **A power cycle always tries to restore power.** If anything fails between off and on,
   including an interrupt, the switch-on is retried four times; if power still cannot be
   restored the command exits 5 and says so instead of reporting a normal result.

Rules 2 to 5 exist because an earlier version matched protections by display name only.
With the device cache deleted, a protected outlet addressed by its bare IP address did not
match its own protection and was switched off. `tests/test_core.py` and
`tests/test_registry.py` replay that exact sequence.

## Credentials

Kasa IOT devices need no account. Tapo devices authenticate against a TP-Link account,
which is the same account for both product lines, so it is stored once under the scope
`tplink`:

```bash
powerctl login --backend tapo     # prompts, never echoes, writes mode 0600
```

Alternatively set `POWERCTL_TPLINK_USERNAME` and `POWERCTL_TPLINK_PASSWORD`; the
environment wins over the stored file.

* Passwords are never accepted as command line arguments, because process arguments are
  readable by other local users and end up in shell history.
* A credential file readable by group or other is rejected rather than used.
* The registry holds addressing and protocol data only. Records pass through
  `powerctl.secrets.scrub`, and all output passes through a redactor that replaces known
  secret values with `***`.

`powerctl doctor` reports where credentials come from and flags a file whose permissions
are too wide.

## When discovery finds nothing

Discovery is a UDP broadcast. It does not cross subnets and is blocked by routers that
isolate an IoT or guest network. Address the device directly instead:

```bash
powerctl probe 192.0.2.42           # try every protocol against one address
powerctl discover --sweep           # probe every address on the local subnet
powerctl discover --sweep 192.0.2.0/24
```

## Architecture

```
cli.py        argument parsing and output formatting only
core.py       actions and every safety rule
registry.py   device cache and the separate protection store
secrets.py    credential loading, file permissions, redaction
netutil.py    broadcast detection, subnet sweep, reachability waits
backends/
  base.py             Backend interface, DeviceRecord / DeviceStatus / EnergyReading
  kasa_backend.py     Kasa IOT via python-kasa
  tapo_backend.py     Tapo via plugp100
```

Adapters hold all vendor-specific code, and each owns its device families exclusively:
these devices accept one session at a time, so two adapters talking to one outlet make it
reject both.

Two adapters rather than one library is deliberate. The Kasa IOT protocol has been stable
for years, while Tapo firmware keeps changing: recent devices speak TPAP, which no
python-kasa release supports and plugp100 shipped in 6.0.1. The half of the estate that
moves fastest therefore depends on the library that releases most often.

To add a vendor, subclass `Backend`, implement `discover`, `status` and `switch` (and
optionally `probe`), expose `get_backend()`, and add the module to `_LOADERS` in
`backends/__init__.py`. Adapters are imported lazily, so a missing dependency disables one
adapter rather than the whole CLI. Set `credential_scope` when the new devices share an
account with an existing adapter.

## Development

```bash
uv venv .venv && uv pip install --python .venv/bin/python -e ".[dev]"
.venv/bin/python -m pytest            # coverage gate: 95%
.venv/bin/ruff check src tests
./scripts/check-repo-clean.sh --history
```

The suite runs entirely against stub devices: no network traffic, no hardware, and a
temporary config directory, so it can never switch off anything real.

Every push runs the tests on Python 3.11 and 3.12, lints and format-checks with ruff,
smoke tests the command, and scans for secrets with gitleaks. Coverage is gated twice, so
it cannot fall below 95% unnoticed: `pytest --cov-fail-under` fails the build with no
external service, and Codecov enforces a 95% project and 90% patch target from
`codecov.yml`.

The skill in `skills/powerctl/SKILL.md` is canonical. `scripts/sync-agent-skills.sh`
copies it to `.github/skills/` and `.agents/skills/` for Copilot, and CI fails if the
copies drift.

`scripts/check-repo-clean.sh` keeps the repository publishable. It fails if a private IPv4
address, a MAC address, an email address, a token, a WiFi name or key, or one of the local
state files appears in the working tree or, with `--history`, in any commit. Only
documentation values are allowed: the RFC 5737 ranges, `AA:BB:CC:DD:EE:FF` and
`example.com`. It runs as a pre-commit hook and in CI over the full history.

## License

MIT. See [LICENSE](LICENSE).
