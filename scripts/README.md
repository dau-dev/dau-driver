# Hardware Host Operations Scripts

Run these on the Linux hardware host (see dau-docs GUIDELINES "Hardware
Rules" for the mandatory ordering):

- `sudo python -m dau_utils.deadman arm --timeout N` (`disarm` | `status`)
  — cancellable auto-reboot guard; arm before ANY PCIe/JTAG-touching
  sequence, disarm only after the health ladder passes. Pairs with the
  systemd hardware watchdog (`RuntimeWatchdogSec=60` → `wdat_wdt` on the
  NUC13 host).
- `thunderbolt_pcie_hold.sh [--rescan] [--detect]` — Thunderbolt runtime-PM
  hold; never `--rescan` while a known-dead image is resident.
- `pcie_hot_reset.sh` — secondary-bus reset of the endpoint's upstream
  bridge.
- `dau-xdma-poc.py` — DMA write/read/compare bandwidth validation runner
  (H2C and C2H whole-transfer reads).
