# powerctl

Command line tool to discover, query and switch networked smart power outlets, plus a
Claude Code skill that drives it.

The first backend covers TP-Link Kasa and Tapo devices through
[python-kasa](https://python-kasa.readthedocs.io/). The backend interface is separate
from the CLI, so another vendor can be added without changing any command.

The main use case it was built for: power cycling a machine that has no reset line. The
`cycle` command switches an outlet off, waits, switches it back on, and then waits until
the machine answers on the network again.

## Install

```bash
git clone git@github.com:<owner>/powerctl.git
cd powerctl
./install.sh
```

`install.sh` installs the `powerctl` command with `uv tool install` (falling back to
`pipx`) and links the Claude Code skill into `~/.claude/skills/powerctl`, so the skill is
available in every project on this machine.

Manual install without the skill:

```bash
uv tool install --editable .    # or: pipx install -e .
```

## Quick start

```bash
powerctl discover               # scan the network, write the device registry
powerctl list                   # what is in the registry
powerctl status lab-plug       # state plus power draw
powerctl status --all --json    # every known device, machine readable
powerctl on lab-plug            # switch on
powerctl off lab-plug --yes     # switch off, confirmation required
powerctl cycle lab-plug --yes --off-seconds 8 --wait-host 192.0.2.60 --wait-port 80
```

Devices can be named by alias, IP address, host name, MAC address or device id. Aliases
are matched case-insensitively, and a unique prefix is enough (`powerctl status lab`).

### When discovery finds nothing

Discovery is a UDP broadcast. It does not cross subnets, and many routers block
broadcast between a guest or IoT network and the main one. If a device is missing, probe
its address directly; this walks every supported protocol and encryption scheme instead
of relying on broadcast:

```bash
powerctl probe 192.0.2.42
```

Get the address from the vendor app (in the Tapo or Kasa app: the device, then Settings,
then Device Info). If the probe also fails, the device is on a network this machine
cannot reach, and the fix is a router or SSID change rather than a powerctl option.

## Commands

| Command | What it does |
| --- | --- |
| `discover` | UDP broadcast scan; writes every device found to the registry |
| `probe <host> ...` | Identify a device by address when broadcast discovery cannot reach it |
| `list` | Print the registry without touching the network |
| `status [device ...]` | Power state, model, and energy readings where supported |
| `on <device>` | Switch on. Never needs a confirmation flag |
| `off <device> --yes` | Switch off. Refuses without `--yes` |
| `cycle <device> --yes` | Off, wait, on, then optionally wait for a host to return |
| `protect` / `unprotect` / `protected` | Manage the list of devices that may not be switched off |
| `login` / `logout` | Store or remove credentials for a backend |
| `backends` | List the device families this build supports |
| `doctor` | Check registry state, credential sources and file permissions |

Every command takes `--json` for scripted use, and `--backend <name>` to restrict the
operation to one device family.

Exit codes: `0` success, `1` error, `2` bad arguments, `3` refused by a safety guard,
`4` the host did not come back before `--wait-timeout` expired.

### Power strips

A strip such as the HS300 reports each socket as a child. `powerctl status <strip>` shows
all sockets; `--child <alias|index>` addresses one of them:

```bash
powerctl status bench-strip --child 2
powerctl off bench-strip --child "Soldering Iron" --yes
```

## Safety model

Cutting power is the one operation that can destroy work or damage hardware, so it is
guarded twice:

1. **`--yes` is required.** `powerctl off` and `powerctl cycle` refuse to run without it
   and exit with code 3. Switching a device *on* never needs a flag.
2. **A protected device is refused even with `--yes`.** `powerctl protect fridge-plug` adds a
   device to the protected list in the registry. Overriding it takes a second, explicit
   flag, `--force-protected`.

The rules are enforced in `powerctl.core.check_switch_allowed`, not in the argument
parser, so a caller using the library directly gets the same guards.

## Credentials and what is written to disk

Older Kasa devices (HS100, HS110, KP115, KP303, HS300 and similar) need no credentials.
Newer Kasa and all Tapo devices authenticate against a TP-Link cloud account.

* Store them with `powerctl login --backend kasa`. The password is read with `getpass`,
  never echoed, and written to `~/.config/powerctl/credentials.json` with mode `0600`. A
  credential file that is readable by group or other is rejected rather than used.
* Or set `POWERCTL_KASA_USERNAME` and `POWERCTL_KASA_PASSWORD` in the environment. The
  environment takes precedence over the stored file.
* Passwords are never accepted as command line arguments. Process arguments are readable
  by other local users through `/proc` and end up in shell history.
* The device registry (`~/.config/powerctl/devices.json`) holds addressing and protocol
  data only. Every record passes through `powerctl.secrets.scrub`, which replaces
  `credentials`, `credentials_hash`, `aes_keys` and `token` keys with `***`. A test
  asserts that a password put into a record does not reach the file.
* All output goes through a process-wide redactor that replaces known secret values with
  `***`, including error messages from the library.

`powerctl doctor` reports where credentials are coming from and flags a credential file
with permissions that are too wide.

Neither the registry nor the credential file is in this repository, and both file names
are in `.gitignore`.

## Power cycling a machine (the c64u case)

The Ultimate 64 restarts when it gets power again, so a full reboot is a power cycle of
its outlet:

```bash
powerctl cycle c64u-plug --yes \
  --off-seconds 8 \
  --wait-host 192.0.2.60 --wait-port 80 \
  --wait-timeout 180
```

The command switches the outlet off, keeps it off for 8 seconds, switches it back on, and
then polls `192.0.2.60:80` until the device answers. It exits with code 4 if the machine
does not come back within the timeout, so a script can tell "power restored" apart from
"machine actually returned". With `--wait-port` omitted, ICMP ping is used instead. With
`--wait-host` omitted, the command returns as soon as power is restored.

## Adding another backend

1. Write a module in `src/powerctl/backends/` that subclasses `Backend` and implements
   `discover`, `status` and `switch`, returning `DeviceRecord` and `DeviceStatus`.
2. Expose a `get_backend()` function in that module.
3. Add the module path to `_LOADERS` in `src/powerctl/backends/__init__.py`.

Backends are imported lazily, so a backend whose dependency is missing does not break the
rest of the CLI. Credentials for a backend named `foo` are read from
`POWERCTL_FOO_USERNAME` and `POWERCTL_FOO_PASSWORD`, or from the `foo` entry of the
credential file.

## Development

```bash
uv venv .venv && uv pip install --python .venv/bin/python -e ".[dev]"
.venv/bin/python -m pytest
```

The test suite uses an in-memory fake backend and a temporary config directory. It makes
no network calls and never touches real hardware or `~/.config/powerctl`.
