# How to fence a PCIe operation with the deadman

This guide shows you how to run a sequence that touches the PCIe bus without
risking a host you cannot get back. It applies to anything that removes an
endpoint, resets a bridge, rescans, holds runtime power management, or
reprograms the fabric — not only to the obviously destructive steps.

The rule is short: **arm a cancellable auto-reboot guard before any
PCIe-touching sequence, and disarm it only after the health ladder passes.**

## Why it is not optional

A bus operation that wedges the host does not fail; it stops answering. The
machine is up, the session is gone, and the only remedy is physical. A
guard armed beforehand turns that from an unrecoverable state into a wait:
if nothing disarms it, the host reboots itself and comes back.

This is the failure the rule exists for, and it has happened often enough
that the sequences worth running by hand are none of them.

## Use a composed plan, not a script

The `dau-build` hardware plans arm the guard around the destructive span,
take the host lock, carry the board's access facts from its platform
definition, and support a dry run. A bare script does none of that.

The plans this package's smoke command is injected into cover the two common
cases:

```bash
dau-cfg task=tasks/hardware/hardware-plan plan=plans/dpv1-validate-bitstream \
  platform=platforms/<vendor>/<board> \
  model.work_root=/srv/vivado-shell model.bitstream=artifacts/candidate.bit \
  plan.dau_core_root=/srv/checkouts/dau-core \
  plan.dau_driver_root=/srv/checkouts/dau-driver
```

Without `model.execute=true` this prints the step ladder and touches nothing.
Read it before running it.

Endpoint removal, secondary-bus reset, rescan, and runtime-PM holds are
**steps of those plans**, not scripts in this repository. Reach for
`plan=plans/recovery` and the programming plans rather than reproducing the
steps yourself.

## Arm and disarm by hand only when you must

The guard is a `dau-utils` tool, not part of this package:

```bash
sudo python -m dau_utils.deadman arm --timeout <seconds>
sudo python -m dau_utils.deadman status
sudo python -m dau_utils.deadman disarm
```

`dau-driver` neither implements nor depends on it — `dau_utils` is an
out-of-repo prerequisite on the bench host, and nothing here will tell you it
is missing. Pair it with the host's own hardware watchdog so that a host
wedged below the point where the timer can fire still recovers.

Choose the timeout by what the span actually takes, with margin. Too short
reboots a healthy host mid-programming; too long is a long wait for a
wedge you already know happened.

## Disarm only after the device answers

The ordering matters more than the arming does. A programmed board is not
healthy because programming returned. It is healthy because the device it
produced identified itself afterwards.

So the ladder is: arm, do the destructive span, discover the device, read
the magic word, read back the identity block, compare it against what was
built — and only then disarm.

Verification steps are deliberately placed **before** the disarm, which has a
consequence worth stating: an uncaught exception in a verify step reboots
the host. That is the intended behaviour. The alternative is the incident
this ordering exists to prevent, where a plan saw an endpoint, disarmed, and
reported success while the board kept running the previous design.

Verify the advertisement against the manifest that produced the bitstream:

```bash
dau-driver-verify-advertisement --manifest <output_root>/shell-build.artifacts.yaml
```

Add `--require-build-id` to refuse a device whose build id does not match the
manifest's spec hash, rather than merely warning.

## Next

- [Find and check a device](Find-a-Device) — the health checks the ladder
  ends with.
- [Device and discovery](Device-and-Discovery) — `check_bar_health`, the
  register window, and the identity snapshot.
- [The transport boundary](Transport-Boundary) — why discovery refuses more
  than it accepts.
