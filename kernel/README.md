# Kernel driver sources

- `linux-xdma/`: the Xilinx XDMA reference driver from
  [Xilinx/dma_ip_drivers](https://github.com/Xilinx/dma_ip_drivers/tree/master/XDMA/linux-kernel),
  `XDMA/linux-kernel/xdma` at upstream commit
  [`14c9f045bad9`](https://github.com/Xilinx/dma_ip_drivers/commit/14c9f045bad9)
  (2024-04-04, driver version `v2020.2.2` in its `version.h`), with
  `COPYING`, `LICENSE` and `include/libxdma_api.h` from the same tree.
  Sixteen of the twenty-one files are byte-identical to that commit; the
  other five (`cdev_ctrl.c`, `cdev_sgdma.c`, `libxdma.h`, `xdma_cdev.c`,
  `xdma_mod.c`) carry the changes in
  [`linux-xdma-dau.patch`](linux-xdma-dau.patch): kernel-version guards for
  the 6.3 `vm_flags_set`, the 6.4 `class_create` signature and the 6.5
  removal of the vector-I/O helpers (`read_iter`/`write_iter` return
  `-EOPNOTSUPP` there; the DAU transfer path uses the scalar calls), a
  flexible array member, and trailing whitespace. The provenance was
  established by matching every file's blob hash against upstream history:
  the import lies between that commit and the next upstream change to the
  directory (2025-03-04). Build it on the hardware host with `make` and load it with
  `insmod ./xdma.ko`. The module is built on the host it runs on, never
  copied between hosts.
- `macos-xdma/`: the macOS DriverKit system extension and its activator,
  written for this project.

## Access

Both drivers expose the card's memory and registers to whoever can open
the device: the `/dev/xdma*` character devices on Linux, the IOKit user
client on macOS. Neither driver checks what a transfer does to the card.
The operating system's device permissions (a udev rule on Linux) are the
access control, so grant the device nodes to the user who runs the host
software and to nobody else.

## Licensing

`linux-xdma/` is Xilinx's code and keeps Xilinx's terms: the source files
are GPL-2.0 and the module declares `Dual BSD/GPL`. The upstream
`LICENSE` (BSD) and `COPYING` (GPL-2.0) files are included alongside the
sources, exactly as they ship in
[Xilinx/dma_ip_drivers](https://github.com/Xilinx/dma_ip_drivers/tree/master/XDMA/linux-kernel).
The Apache-2.0 licence at the root of this repository does not apply to
that directory.

Everything else in this repository, including `macos-xdma/`, is
Apache-2.0.
