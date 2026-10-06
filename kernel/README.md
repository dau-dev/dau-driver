# Kernel driver sources

- `linux-xdma/`: the Xilinx XDMA reference driver (`xdma v2020.2.2`),
  patched to build and load on Linux 6.8 (the vector-I/O helpers were
  removed for the DAU transfer path). Build it on the hardware host with
  `make` and load it with `insmod ./xdma.ko`. The module is built on the
  host it runs on, never copied between hosts.
- `macos-xdma/`: the macOS DriverKit system extension and its activator,
  written for this project.

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
