# How to find and check a device

This guide shows you how to get from "there is a card in the machine" to a
validated `DauDevice` handle, and how to read the failure when you do not get
one.

Discovery is deliberately strict. A DAU device is not "an XDMA endpoint"; it is
an XDMA endpoint whose register window reports the DAU magic word and decodes a
capability advertisement. Anything else is skipped or refused by name.

## Discover every device on the host

```python
from dau_driver import discover_devices

devices = discover_devices()
for device in devices:
    print(device.name, device.backend, device.capabilities.platform_id)
```

On Linux this scans `/dev` for `xdma*_user` character devices, pairing each with
its `xdma*_h2c_0` and `xdma*_c2h_0` channels. On macOS, when the Linux scan finds
nothing, it falls back to the DriverKit transport over IOKit.

Point it somewhere else if your devnodes are not under `/dev`:

```python
devices = discover_devices("/some/other/dev")
```

An endpoint whose BAR reads all ones, or whose magic word is not DAU's, is
skipped rather than returned — broad discovery should not fail because one card
in the box is running someone else's bitstream.

## Identify one device by path

When you know which node you want, skip the scan:

```python
from dau_driver import identify_device

device = identify_device("/dev/xdma0_user")
```

This does raise, which is the point: you named this device, so a bad answer is
an error rather than an omission.

| Exception             | Meaning                                                                                              |
| --------------------- | ---------------------------------------------------------------------------------------------------- |
| `UnhealthyBarError`   | The magic word read all ones. The endpoint or its AXI-lite BAR is not responding.                    |
| `WrongBitstreamError` | The window responded, but with something other than the DAU magic word.                              |
| `RegisterAccessError` | A register read failed after the window was located — a short read, or the node could not be opened. |

A path that does not exist reads back as zero rather than the magic word, so it
surfaces as `WrongBitstreamError`, not as a missing-file error.

Pass `h2c_path=` and `c2h_path=` if the channels are not the conventional
siblings of the user node.

The register window is located during identification: the shell base is probed
first, then the stream-job contract window. The magic probe retries a few times,
because the first AXI-lite reads after a PCIe rescan can return stale data before
the link settles, and a single-shot probe would misread a healthy shell as the
wrong bitstream.

## Check the register path before you trust it

A dead AXI-lite path returns `0xFFFFFFFF`, which is a perfectly plausible
number. Check for it explicitly:

```python
device.check_bar_health()
```

Raises `UnhealthyBarError` if `MAGIC` or `STREAM_PROTOCOL_VERSION` reads all
ones. Call it before a sequence you care about and again afterwards — a
successful DMA that leaves the BAR dead is worth knowing about immediately.

To check other offsets, pass them:

```python
from dau_core.registers import RegisterOffset

device.check_bar_health(offsets=(RegisterOffset.MAGIC, RegisterOffset.STATUS))
```

## Read what the device advertises

```python
snapshot = device.read_register_block_snapshot()
print(snapshot.register_map_version, snapshot.platform_id, hex(snapshot.build_id))
print(device.read_status())
print(device.read_counters())
```

`read_register_block_snapshot()` reads the whole identity block in one pass:
versions, platform id, shell version, build id, status, DMA constraints, the
operator bitmap, and the counters. The composed-capability words — lane count,
host opcode bitmap, sort capacity — are only read on register map 0.2 and above,
because older shells do not decode those offsets.

`device.capabilities.operators` lists what the device executes.
`device.capabilities.host_operators` lists protocol-level opcodes the *host*
derives from hardware results, such as AVERAGE from SUM and COUNT. Those must
never be submitted to the device.

## Read the concrete capability contract

The register bitmap is a coarse advertisement. A design's shell-build manifest
carries the exact composed contract — per-tile opcodes and input types, with no
bitmap-times-types expansion:

```python
from dau_driver.capability_decode import contract_from_manifest_metadata, operators_from_contract

contract = contract_from_manifest_metadata(manifest.metadata)
hardware_operators, host_operators = operators_from_contract(contract)
```

Returns `None` when the manifest carries no `capability_contract` key.

## Diagnose a macOS DriverKit path

The macOS transport has more ways to fail than "no device", so it ships a probe
that reports the failing stage:

```bash
dau-xdma-macos-probe --verbose
```

```text
macos-xdma	status=no-service devices=0 message=No macOS XDMA DriverKit service found for com.dau-dev.xdma...
```

| `status`                  | What to check                                                                                                                        |
| ------------------------- | ------------------------------------------------------------------------------------------------------------------------------------ |
| `found`                   | Devices were identified; nothing to fix.                                                                                             |
| `no-service`              | No IOKit DriverKit service matched. Is the PCI endpoint visible, and is the `com.dau-dev.xdma` dext signed, installed, and approved? |
| `no-user-client`          | The service matched but `IOServiceOpen` created no user client. Check the user-client entitlements.                                  |
| `no-compatible-bitstream` | The user client opened, but no device passed identification.                                                                         |
| `wrong-bitstream`         | BAR scanning did not find the DAU magic word.                                                                                        |
| `unhealthy-bar`           | BAR reads returned all ones; the endpoint or AXI-lite BAR is not responding.                                                         |
| `register-access-failed`  | A register read failed outright.                                                                                                     |

Add `--service-name` (repeatable) to probe a service name other than the
default.

## Check whether the native extension is loaded

```python
from dau_driver import native_info

print(native_info())
```

```text
NativeInfo(backend='nanobind', abi_version=1, package_version='0.1.0', extension_available=True)
```

`backend='python'` with `extension_available=False` means the compiled extension
is not present and the pure-Python fallback is in use. Everything still works;
hot paths are slower.

## Next

- [Move data to and from a device](Move-Data) — DMA and the stream codec.
- [Device and discovery reference](Device-and-Discovery) — every
  field and signature.
- [The transport boundary](Transport-Boundary) — why discovery
  refuses so much.
