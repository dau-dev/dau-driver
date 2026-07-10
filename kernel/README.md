# Kernel Driver Sources

- `linux-xdma/` — patched Xilinx XDMA reference driver (`xdma v2020.2.2`),
  builds and loads on Linux 6.8 (vector-I/O helpers removed for the DAU
  transfer path). Build on the hardware host with `make` and load with
  `insmod ./xdma.ko`; the module is host-built, never rsynced.
- `macos-xdma/` — macOS DriverKit scaffolding (compiles; DMA deferred until
  the Linux DPU path is done).

Moved here from the bring-up scratch tree (`projects/nite/sw`) when that tree
was retired — dau-driver owns driver code per the package ethos.
