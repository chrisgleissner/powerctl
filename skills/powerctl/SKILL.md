---
name: powerctl
description: Discover, query and switch smart power outlets (TP-Link Kasa/Tapo) from the command line, read their power draw, and power cycle a machine that has no reset line by cutting and restoring outlet power. Use when asked to list smart plugs on the network, check whether something is powered, read watts or kWh, switch an outlet on or off, or reboot a device by power cycling it (for example an Ultimate 64 that restarts when power returns).
---

# powerctl

`powerctl` is a CLI that talks to smart outlets on the local network. Every command
accepts `--json`, which is the form to use here: parse the JSON, do not screen-scrape the
human output.

## The one rule that matters

**Never cut power without explicit permission from the user in the current
conversation.** Discovery, `list` and `status` are read-only and can be run freely.
`off` and `cycle` interrupt power to real hardware and can lose the user's work.

* Ask before running `off` or `cycle`, naming the exact device you are about to switch.
* Do not pass `--yes` unless the user has just agreed to that specific action.
* Do not pass `--force-protected`. It overrides the user's own protected list. If a
  device is protected, report that and stop.
* `on` is not guarded, but still say which device you switched on.
* If unsure which outlet feeds a machine, ask. Do not guess from an alias.

## Finding devices

```bash
powerctl discover --json          # broadcast scan, updates the registry (read-only for devices)
powerctl list --json              # registry only, no network traffic
```

Each record has `host`, `alias`, `model`, `device_type`, `mac`, `supports_switching`,
`supports_energy`, `needs_credentials` and `children` (sockets of a power strip).

If a device the user expects is missing, do not re-run discovery repeatedly. Discovery is
a UDP broadcast and does not cross subnets or a router that blocks broadcast between an
IoT/guest network and the main one. Ask the user for the address from the vendor app
(device, Settings, Device Info) and probe it directly:

```bash
powerctl probe <ip> --json
```

`probe` tries every supported protocol against that one address and adds the device to
the registry if it answers. If probe also fails, the device is not reachable from this
machine's network, which is a router or SSID problem, not a powerctl option.

`needs_credentials: true` means the device is a newer Kasa or a Tapo model and needs a
TP-Link account. Tell the user to run `powerctl login --backend kasa` themselves; it
prompts for the password. Never ask for the password in chat, never put it on a command
line, and never write it into a file yourself.

## Reading state and power

```bash
powerctl status <device> --json
powerctl status --all --json
powerctl status <strip> --child 2 --json
```

`state` is `"on"`, `"off"` or `null` (device has no switch). `energy` is `null` when the
model has no meter; otherwise it carries `power_w`, `voltage_v`, `current_a`,
`today_kwh`, `this_month_kwh` and `total_kwh`. Any single field can be `null`.

A device can be named by alias, IP address, host name, MAC, device id or a unique alias
prefix.

## Switching

```bash
powerctl on <device> --json
powerctl off <device> --yes --json          # only after the user agreed
```

## Power cycling a machine

For hardware that boots when power returns (such as an Ultimate 64 / c64u):

```bash
powerctl cycle <device> --yes --json \
  --off-seconds 8 \
  --wait-host <machine-ip> --wait-port 80 \
  --wait-timeout 180
```

This switches the outlet off, holds it off for `--off-seconds`, switches it back on, and
then polls the machine until it answers. Use `--wait-host` whenever the machine has a
reachable service, so the result says whether the machine actually came back rather than
only that power was restored. `--wait-port` picks a TCP port to probe; without it, ICMP
ping is used.

Before running it:

1. Confirm with the user which outlet feeds the machine.
2. Check that nothing important is mid-flight on that machine.
3. Get the user's explicit go-ahead for the power cycle.

Report afterwards: the events list with timestamps, `final_state`, and `wait_seconds`
(how long the machine took to answer again).

## Exit codes

| Code | Meaning |
| --- | --- |
| 0 | Success |
| 1 | Error (device unreachable, authentication failed) |
| 2 | Bad arguments |
| 3 | Refused by a safety guard: `--yes` missing, or the device is protected |
| 4 | Power was restored, but `--wait-host` did not answer before the timeout |

Code 3 is never something to work around. Report it and ask the user what to do.

## Protection list

```bash
powerctl protected --json          # devices that may not be switched off
powerctl protect <device>          # add one
powerctl unprotect <device>        # remove one, only when the user asks
```

Check `powerctl protected --json` before proposing any power cut.

## Diagnosing setup problems

`powerctl doctor` reports the registry location, the number of known devices, where
credentials come from, and whether the credential file permissions are too wide. It
prints no secret values.
