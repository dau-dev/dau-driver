# Kernel driver sources

- `linux-xdma/`: the Xilinx XDMA reference driver from
  [Xilinx/dma_ip_drivers](https://github.com/Xilinx/dma_ip_drivers/tree/master/XDMA/linux-kernel)
  at driver version `v2020.2.2` (its `version.h`), patched to build and
  load on Linux 6.8: the vector-I/O helpers were removed for the DAU
  transfer path. The upstream commit the import was taken from is not
  recorded. Build it on the hardware host with `make` and load it with
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
