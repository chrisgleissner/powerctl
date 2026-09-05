# powerctl

[![Build](https://github.com/chrisgleissner/powerctl/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/chrisgleissner/powerctl/actions/workflows/ci.yml)
[![codecov](https://codecov.io/gh/chrisgleissner/powerctl/graph/badge.svg)](https://codecov.io/gh/chrisgleissner/powerctl)
[![Python](https://img.shields.io/badge/python-3.11%20%7C%203.12-blue)](https://www.python.org/)
[![Ruff](https://img.shields.io/badge/lint-ruff-261230)](https://docs.astral.sh/ruff/)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)

Command line tool to discover, query and switch networked smart power outlets, plus a
Claude Code skill that drives it.

It reads the power state and energy figures of every switchable outlet on the network,
switches outlets on and off behind explicit safety gates, and power cycles a machine that
has no reset line: outlet off, wait, outlet on, then wait until the machine answers on the
network again.

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
```

## What it supports

| Device family | Adapter | Library | Notes |
| --- | --- | --- | --- |
| Kasa IOT: KP115, HS100/110, KP303, HS300 | `kasa` | [python-kasa](https://github.com/python-kasa/python-kasa) | No account needed |
| Tapo: P100/P105/P110/P115, P110M and other TPAP firmware | `tapo` | [plugp100](https://github.com/petretiandrea/plugp100) | Needs a TP-Link account |

Devices that answer TP-Link discovery but hold no relay, such as Deco mesh nodes and
cameras, are not listed: this tool is about power. Pass `--all-devices` to see them.

## Install

```bash
git clone git@github.com:chrisgleissner/powerctl.git
cd powerctl
./install.sh
```

`install.sh` installs the `powerctl` command with `uv tool install` (falling back to
`pipx`), links the Claude Code skill into `~/.claude/skills/powerctl` so it is available in
every project, and installs a pre-commit hook that keeps local network details out of the
repository.

## Quick start

```bash
powerctl discover                  # scan the network, write the device registry
powerctl list                      # what is in the registry, no network traffic
powerctl status <device>           # state plus power draw
powerctl status --all --json       # every known device, machine readable
powerctl on <device>               # switch on
powerctl off <device> --yes        # switch off, confirmation required
powerctl protect <device>          # refuse to switch this one off
powerctl protect <device> --critical   # refuse it under every flag
powerctl off <device> --yes --dry-run  # run the safety checks, change nothing
```

Devices are named by alias, IP address, host name, MAC address, device id, or a unique
alias prefix. Every command takes `--json`, and `--backend <name>` restricts it to one
adapter.

Exit codes: `0` success, `1` error, `2` bad arguments, `3` refused by a safety guard,
`4` the machine did not come back before `--wait-timeout` expired, `5` a power cycle could
not switch the outlet back on and the device is still without power.

## Safety model

Cutting power is the one operation that can destroy work or spoil food, so it is gated
in layers, all enforced in `powerctl.core`, not in the argument parser:

1. **`--yes` is required.** `off` and `cycle` refuse without it. Switching *on* is never
   gated.
2. **Protected devices are refused.** `powerctl protect <device>` stores the device's
   alias, address, MAC and device id. A match on any one of them protects it, so the
   device stays protected however it is addressed. `--force-protected` overrides this
   tier.
3. **Critical devices are refused under every flag.** `powerctl protect <device>
   --critical` is for a fridge, a router, a server. No command line option lifts it; the
   entry has to be removed from the protection file by hand.
4. **Identity is confirmed before power is cut.** A device that is not in the registry is
   queried first, and the guards run again against the identity it reports. If it cannot
   be identified, the power cut is refused rather than attempted.
5. **Protections live in their own file.** `~/.config/powerctl/protected.json` is separate
   from the device cache, so rebuilding or deleting the cache cannot drop a safety rule.
6. **`--dry-run`** runs every check and reports the outcome without touching the device,
   which is the only safe way to verify the guards against real hardware.
7. **A power cycle always tries to restore power.** If anything fails between switching
   off and switching on, including an interrupt, the switch-on is retried four times. If
   power still cannot be restored, the command exits with code 5 and says so plainly
   rather than reporting a normal result.

Layers 2 to 5 exist because an earlier version matched protections by display name only.
With the device cache deleted, a protected outlet addressed by its bare IP address did not
match its own protection and was switched off. `tests/test_core.py` and
`tests/test_registry.py` contain regression tests for exactly that sequence.

## Credentials

Kasa IOT devices need none. Tapo devices authenticate against a TP-Link account, which is
the same account for both product lines, so it is stored once under the scope `tplink`.

```bash
powerctl login --backend tapo     # prompts, never echoes, writes mode 0600
```

* Or set `POWERCTL_TPLINK_USERNAME` and `POWERCTL_TPLINK_PASSWORD`; the environment wins
  over the stored file.
* Passwords are never accepted as command line arguments: process arguments are readable
  by other local users and end up in shell history.
* A credential file readable by group or other is rejected rather than used.
* The device registry holds addressing and protocol data only. Every record passes through
  `powerctl.secrets.scrub`, and all output passes through a redactor that replaces known
  secret values with `***`.

`powerctl doctor` reports where credentials come from and flags a file with permissions
that are too wide. Neither the registry nor the credential file is in this repository.

## Power cycling a machine

For hardware that boots when power returns:

```bash
powerctl cycle <device> --yes \
  --off-seconds 8 \
  --wait-host <machine-ip> --wait-port 80 \
  --wait-timeout 180
```

The outlet goes off, stays off for `--off-seconds`, comes back on, and the command then
polls the machine until it answers. Exit code 4 means power was restored but the machine
did not return; exit code 5 means power itself could not be restored. A script can tell
all three outcomes apart. Without `--wait-port`, ICMP is used. Without `--wait-host`, the
command returns as soon as power is restored.

## When discovery finds nothing

Discovery is a UDP broadcast; it does not cross subnets and is blocked by routers that
isolate an IoT or guest network. Probe the address directly, which walks every supported
protocol:

```bash
powerctl probe 192.0.2.42          # one address
powerctl discover --sweep          # every address on the local subnet
powerctl discover --sweep 192.0.2.0/24
```

## Architecture

```
cli.py        argument parsing and output formatting only
core.py       actions and every safety rule (guards live here, not in the parser)
registry.py   device cache and the separate protection store
secrets.py    credential loading, file permissions, redaction
netutil.py    broadcast detection, subnet sweep, reachability waits
backends/
  base.py             Backend interface plus DeviceRecord / DeviceStatus / EnergyReading
  kasa_backend.py     Kasa IOT via python-kasa
  tapo_backend.py     Tapo via plugp100
```

Adapters are the only vendor-aware code. Each owns its device families exclusively,
because these devices accept one session at a time and two adapters talking to one outlet
make it reject both.

To add a vendor: subclass `Backend`, implement `discover`, `status`, `switch` (and
optionally `probe`), expose `get_backend()`, and add the module to `_LOADERS` in
`backends/__init__.py`. Adapters are imported lazily, so a missing dependency disables one
adapter rather than the CLI. Set `credential_scope` when the new devices share an account
with an existing adapter.

## Development

```bash
uv venv .venv && uv pip install --python .venv/bin/python -e ".[dev]"
.venv/bin/python -m pytest        # coverage gate: 95%
.venv/bin/ruff check src tests
./scripts/check-repo-clean.sh     # no private addresses, MACs or local state
```

The suite runs entirely against stub devices: no network traffic, no hardware, and a
temporary config directory, so it can never switch off anything real.

## License

MIT. See [LICENSE](LICENSE).
