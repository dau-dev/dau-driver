# dau-driver

Host I/O for DAU devices: discovery, registers, DMA, and job execution over XDMA

[![Build Status](https://github.com/dau-dev/dau-driver/actions/workflows/build.yaml/badge.svg?branch=main&event=push)](https://github.com/dau-dev/dau-driver/actions/workflows/build.yaml)
[![codecov](https://codecov.io/gh/dau-dev/dau-driver/branch/main/graph/badge.svg)](https://codecov.io/gh/dau-dev/dau-driver)
[![License](https://img.shields.io/github/license/dau-dev/dau-driver)](https://github.com/dau-dev/dau-driver)
[![PyPI](https://img.shields.io/pypi/v/dau-driver.svg)](https://pypi.python.org/pypi/dau-driver)

`dau-driver` is the host side of a DAU device. It discovers DAU-compatible XDMA
endpoints, reads the canonical register block, decodes the device's capability
advertisement, moves data over H2C/C2H DMA, allocates device storage, and runs
the register-contract jobs the shells expose. All device semantics — register
offsets, stream framing, capability words — come from `dau-core`; this package
owns transport and execution only.

- **Linux** hosts use the Xilinx XDMA character devices under `/dev`.
- **macOS** hosts use a DriverKit-based XDMA system extension through IOKit
  user-client calls, for register access and full H2C/C2H DMA.

```python
from dau_driver import discover_devices

device = discover_devices()[0]
device.check_bar_health()                          # all-ones reads fail here, not later
print(device.name, device.backend)                 # xdma0 linux-xdma
print(device.capabilities.dma)                     # alignment / max-transfer constraints
print(device.read_register_block_snapshot())       # the whole identity block
print(device.read_status())                        # link, MIG, operator ready bits
```

Every execution path also runs against an in-memory fake transport, so the whole
surface can be exercised — and is tested — with no hardware present.

## Documentation

The documentation lives in [the wiki](https://github.com/dau-dev/dau-driver/wiki),
authored under [`docs/wiki`](docs/wiki) and published from there. It is in four
parts, each answering a different need.

**[Tutorial](https://github.com/dau-dev/dau-driver/wiki/First-Device)** — start here if dau-driver is new to
you. Two guided lessons drive a device end to end against the fake transport, no
board required:
[Talk to a device without any hardware](https://github.com/dau-dev/dau-driver/wiki/First-Device) and
[Run a scan over many batches](https://github.com/dau-dev/dau-driver/wiki/First-Scan-Run).

**[How-to guides](https://github.com/dau-dev/dau-driver/wiki/Find-a-Device)** — directions for the jobs you
will actually have. How to
[find and check a device](https://github.com/dau-dev/dau-driver/wiki/Find-a-Device),
[move data to and from it](https://github.com/dau-dev/dau-driver/wiki/Move-Data),
[run scan jobs](https://github.com/dau-dev/dau-driver/wiki/Run-Scan-Jobs) across batches and cards,
[allocate device storage](https://github.com/dau-dev/dau-driver/wiki/Allocate-Device-Storage), and
[fence a PCIe operation with the deadman](https://github.com/dau-dev/dau-driver/wiki/Arm-the-Deadman)
before touching the bus.

**[Reference](https://github.com/dau-dev/dau-driver/wiki/Device-and-Discovery)** — the technical
description, laid out to mirror the code: the
[device and discovery](https://github.com/dau-dev/dau-driver/wiki/Device-and-Discovery) surface including
transfers, storage and errors; the
[execution contracts](https://github.com/dau-dev/dau-driver/wiki/Execution-Contracts) for stream, scan,
pipeline and fleet jobs; and the
[stream codec](https://github.com/dau-dev/dau-driver/wiki/Stream-Codec).

**[Explanation](https://github.com/dau-dev/dau-driver/wiki/Transport-Boundary)** — the reasoning behind
the design. Why [the driver owns almost
nothing](https://github.com/dau-dev/dau-driver/wiki/Transport-Boundary) and where the `dau-core`
boundary sits, why [staging overlaps compute and when the runner
refuses](https://github.com/dau-dev/dau-driver/wiki/Overlapped-Staging), and why storage is handed out as
[generation-checked handles rather than
pointers](https://github.com/dau-dev/dau-driver/wiki/Storage-Handles).

The [examples/](examples/) directory holds end-to-end host flows — record-batch
round trip, stream aggregation, the OHLCV pipeline job — that run against real
hardware.

## License

Apache-2.0, except `kernel/linux-xdma/`, which is Xilinx's XDMA reference
driver under its own Dual BSD/GPL terms. See [kernel/README.md](kernel/README.md).
