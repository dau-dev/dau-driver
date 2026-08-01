# Hardware Host Operations Scripts

Run these on the Linux hardware host (see dau-docs GUIDELINES "Hardware
Rules" for the mandatory ordering):

- `sudo python -m dau_utils.deadman arm --timeout N` (`disarm` | `status`)
  — cancellable auto-reboot guard; arm before ANY PCIe/JTAG-touching
  sequence, disarm only after the health ladder passes. Pairs with the
  systemd hardware watchdog (`RuntimeWatchdogSec=60` → `wdat_wdt` on the
  NUC13 host).

Runtime-PM holds, endpoint removal, secondary-bus reset and rescan are NOT
scripts here: they are steps of the composed dau-build hardware plans
(`plan=plans/thunderbolt-hold`, `plans/thunderbolt-release`,
`plans/recovery`, `plans/sram-program`). Use those. They arm the deadman
around the destructive span, take the host lock, carry the platform's
host-access facts, and support a dry run — none of which a bare script
does. A hand-run bus reset with no deadman is how the host wedges.
- `dau-xdma-poc.py` — DMA write/read/compare bandwidth validation runner
  (H2C and C2H whole-transfer reads).
