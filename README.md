# dau-driver

PCIe Driver for dau

[![Build Status](https://github.com/dau-dev/dau-driver/actions/workflows/build.yaml/badge.svg?branch=main&event=push)](https://github.com/dau-dev/dau-driver/actions/workflows/build.yaml)
[![codecov](https://codecov.io/gh/dau-dev/dau-driver/branch/main/graph/badge.svg)](https://codecov.io/gh/dau-dev/dau-driver)
[![License](https://img.shields.io/github/license/dau-dev/dau-driver)](https://github.com/dau-dev/dau-driver)
[![PyPI](https://img.shields.io/pypi/v/dau-driver.svg)](https://pypi.python.org/pypi/dau-driver)

## Overview

`dau-driver` discovers DAU-compatible XDMA devices and reads the canonical DAU register block. Linux hosts use Xilinx XDMA character devices under `/dev`; macOS hosts can use the DriverKit-based XDMA system extension through IOKit user-client calls.

On macOS, validate the DriverKit BAR/register path with:

```bash
dau-xdma-macos-probe --verbose
```

or from an editable checkout:

```bash
PYTHONPATH=/path/to/dau-core:/path/to/dau-driver python -m dau_driver.macos_xdma --verbose
```

The probe now reports the stage that failed instead of collapsing everything to `devices=0`:

- `status=no-service`: no IOKit DriverKit service matched. Check that the `10ee:7011` PCI endpoint is visible, the `com.dau-dev.xdma` dext is signed/installed/approved, and the driver reached `RegisterService()`.
- `status=no-user-client`: the service matched, but `IOServiceOpen` did not create `XDMAUserClient`. Check `XDMADriver::NewUserClient`, `XDMAUserClientProperties`, and user-client access entitlements.
- `status=wrong-bitstream`: the user client opened, but BAR scanning did not find `DAU_MAGIC_WORD`.
- `status=unhealthy-bar`: BAR reads returned all ones, which usually means the endpoint or AXI-lite BAR is not responding.

The canonical DriverKit service name is `com.dau-dev.xdma`; the probe also tries the legacy `XDMADriver` name/class as a fallback.

The current macOS DriverKit adapter supports device info and 32-bit BAR register access. DMA transfers are reported unavailable in `dau-driver` until the DriverKit extension exposes buffer-carrying DMA methods instead of the current scalar-only stubs.

> [!NOTE]
> This library was generated using [copier](https://copier.readthedocs.io/en/stable/) from the [Base Python Project Template repository](https://github.com/python-project-templates/base).
