# dau-driver

Host I/O for DAU devices: discovery, registers, DMA, and job execution over XDMA

[![Build Status](https://github.com/dau-dev/dau-driver/actions/workflows/build.yaml/badge.svg?branch=main&event=push)](https://github.com/dau-dev/dau-driver/actions/workflows/build.yaml)
[![codecov](https://codecov.io/gh/dau-dev/dau-driver/branch/main/graph/badge.svg)](https://codecov.io/gh/dau-dev/dau-driver)
[![License](https://img.shields.io/github/license/dau-dev/dau-driver)](https://github.com/dau-dev/dau-driver)
[![PyPI](https://img.shields.io/pypi/v/dau-driver.svg)](https://pypi.python.org/pypi/dau-driver)

## Overview

`dau-driver` is the host side of a DAU device: it discovers
DAU-compatible XDMA endpoints, reads the canonical register block, decodes
the device's capability advertisement, moves data over H2C/C2H DMA, and
runs the register-contract jobs the shells expose. All device semantics
(register offsets, stream framing, capability words) come from
`dau-core` — this package owns transport and execution only.

- **Linux** hosts use the Xilinx XDMA character devices under `/dev`.
- **macOS** hosts use a DriverKit-based XDMA system extension through
  IOKit user-client calls (register access and full H2C/C2H DMA).

```python
from dau_driver import discover_devices

device = discover_devices()[0]
print(device.info())            # identity registers: magic, versions, capabilities
print(device.capabilities.dma)  # alignment / max-transfer constraints
```

## Package map

- `dau_driver.discovery` / `dau_driver.device` — `discover_devices`,
  `identify_device`, and `DauDevice`: a validated handle bundling the
  register window and DMA channel paths behind a `DeviceTransport`
  (swappable for fakes in tests).
- `dau_driver.status` — decoded device status and counter registers.
- `dau_driver.capability_decode` — the composed capability advertisement
  read back from the device, as `dau-core` capability models.
- `dau_driver.stream_execution` — the single-job register contract: stage
  a stream batch, trigger, poll status, read the result back.
- `dau_driver.scan_execution` — `run_scan_batches`: multi-batch scans
  with overlapped staging (the next batch's H2C DMA overlaps the current
  job's compute), returning per-batch results.
- `dau_driver.pipeline_execution` — the keyed-pipeline and bar-job
  register contracts, including two-phase (load-then-probe) jobs and
  DDR-staged inputs.
- `dau_driver.stream_codec` — decode packed result streams and record
  batches into typed columns.
- `dau_driver.native` — the optional C extension for hot paths, with a
  pure-Python fallback (`NativeInfo` reports which is active).
- `dau_driver.linux_xdma` / `dau_driver.macos_xdma` — the two transports.

The [examples/](examples/) directory holds end-to-end host flows
(record-batch round trip, stream aggregation, the OHLCV pipeline job),
each runnable against a fake transport where no device is present.

## macOS probe

Validate the DriverKit BAR/register path stage by stage:

```bash
dau-xdma-macos-probe --verbose
```

The probe reports the failing stage rather than collapsing to
`devices=0`:

- `status=no-service` — no IOKit DriverKit service matched: check that
  the PCI endpoint is visible and the `com.dau-dev.xdma` dext is
  signed, installed, and approved.
- `status=no-user-client` — the service matched but `IOServiceOpen` did
  not create `XDMAUserClient`: check the user-client entitlements.
- `status=wrong-bitstream` — the user client opened, but BAR scanning did
  not find the DAU magic word.
- `status=unhealthy-bar` — BAR reads returned all ones; the endpoint or
  AXI-lite BAR is not responding.

## Development

```bash
pip install -e .[develop]
python -m pytest                 # fake-transport tests; no hardware needed
python -m ruff check dau_driver
```

> [!NOTE]
> This library was generated using [copier](https://copier.readthedocs.io/en/stable/) from the [Base Python Project Template repository](https://github.com/python-project-templates/base).
