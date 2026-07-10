#!/usr/bin/env bash
# deadman.sh — auto-reboot guard around risky PCIe/JTAG operations.
#
# Usage:
#   sudo ./deadman.sh arm [minutes]   # schedule reboot (default 5 min)
#   sudo ./deadman.sh disarm          # cancel after the op verifies healthy
#   sudo ./deadman.sh status
#
# Rule (GUIDELINES): arm BEFORE any rescan/remove/BAR-probe/flash sequence;
# disarm ONLY after config space + LnkSta + bounded identity read pass.
# This catches "kernel alive, network/Thunderbolt dead" wedges. Full kernel
# hangs are covered separately by the hardware watchdog (systemd
# RuntimeWatchdogSec -> iTCO), which this script does not manage.
set -euo pipefail

case "${1:-}" in
  arm)
    minutes="${2:-5}"
    shutdown -r "+${minutes}" "deadman: auto-reboot in ${minutes}m unless disarmed" 2>&1
    echo "deadman armed: reboot in ${minutes} minutes unless disarmed"
    ;;
  disarm)
    shutdown -c 2>&1 || true
    echo "deadman disarmed"
    ;;
  status)
    if [ -f /run/systemd/shutdown/scheduled ]; then
      cat /run/systemd/shutdown/scheduled
    else
      echo "no shutdown scheduled"
    fi
    ;;
  *)
    echo "usage: deadman.sh arm [minutes] | disarm | status" >&2
    exit 2
    ;;
esac
