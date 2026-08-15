# Talk to a device without any hardware

In this tutorial we will drive a DAU device end to end — read its identity
registers, check the health of its register path, validate a DMA transfer, and
run one aggregation job — without a board, a bitstream, or a driver install.
Everything runs against the fake transport that ships with the package for
exactly this purpose.

Work in a Python session with `dau-driver` installed. Each step shows the code
to run and an **Output** block with what it prints.

## Step 1 — Open a device

A `DauDevice` is a validated handle. On a real host, `discover_devices()`
produces one; here we build one over an in-memory transport so nothing is
opened. Run:

```python
from pathlib import Path
from dau_driver.testing import FakeTransport, fake_device

transport = FakeTransport()
device = fake_device(Path("/tmp"), transport)

print("name:    ", device.name)
print("backend: ", device.backend)
print("platform:", device.capabilities.platform_id)
```

**Output:**

```text
name:     xdma0
backend:  linux-xdma
platform: DPV1
```

The paths passed to `fake_device` are never opened — the transport intercepts
every read and write. Anything you do below would work identically against a real
device.

## Step 2 — Read what the device says it can do

Capabilities are decoded from the device's own register block, so they describe
the bitstream that is loaded, not the driver. Run:

```python
dma = device.capabilities.dma
print("alignment_bytes:   ", dma.alignment_bytes)
print("max_transfer_bytes:", dma.max_transfer_bytes)

for operator in device.capabilities.operators:
    print(operator.opcode.name, [t.name for t in operator.input_types], "->", operator.output_type.name)
```

**Output:**

```text
alignment_bytes:    64
max_transfer_bytes: 4096

SUM ['INT32'] -> INT64
MIN ['INT32'] -> INT64
MAX ['INT32'] -> INT64
COUNT ['INT32'] -> UINT64
```

Two things are worth noticing. The DMA constraints are facts you will have to
respect in Step 4. And the operator list is typed: `SUM` over `INT32` produces
`INT64`, and asking for any other output type is an error the driver catches
before touching the bus.

## Step 3 — Read the identity and status registers

Register access goes through the same transport. Preload a few values so the fake
has something to report, then read them back. Run:

```python
from dau_core.registers import DAU_MAGIC_WORD, RegisterOffset

transport.registers[int(RegisterOffset.MAGIC)] = DAU_MAGIC_WORD
transport.registers[int(RegisterOffset.STATUS)] = 0b0111
transport.registers[int(RegisterOffset.ROWS_LOW)] = 1_000_000
transport.registers[int(RegisterOffset.BATCHES)] = 12

print("magic:", hex(device.read_register(RegisterOffset.MAGIC)))

status = device.read_status()
print("link_ready:    ", status.link_ready)
print("mig_ready:     ", status.mig_ready)
print("operator_ready:", status.operator_ready)
print("reset_active:  ", status.reset_active)

print(device.read_counters())
```

**Output:**

```text
magic: 0x44415531
link_ready:     True
mig_ready:      True
operator_ready: True
reset_active:   False
DeviceCounters(h2c_bytes=0, c2h_bytes=0, rows=1000000, batches=12, stalls=0)
```

`0x44415531` is the DAU magic word — it is how discovery tells a DAU shell from
any other XDMA bitstream. `DeviceStatus` decodes the status word into named bits,
and the counters are 64-bit values read as two 32-bit register accesses.

## Step 4 — Check the register path is healthy

A dead AXI-lite path does not fail; it returns all ones, which looks like a
perfectly good number. `check_bar_health()` is the check for that. Run:

```python
device.check_bar_health()
print("BAR health: ok")
```

**Output:**

```text
BAR health: ok
```

Now break it and watch what happens:

```python
from dau_driver import UnhealthyBarError

transport.registers[int(RegisterOffset.MAGIC)] = 0xFFFF_FFFF
try:
    device.check_bar_health()
except UnhealthyBarError as exc:
    print("UnhealthyBarError:", exc)

transport.registers[int(RegisterOffset.MAGIC)] = DAU_MAGIC_WORD
```

**Output:**

```text
UnhealthyBarError: MAGIC read all ones from /tmp/xdma0_user; BAR/AXI-lite path may be unhealthy
```

Call this before and after any sequence you care about. It is cheap and it turns
a wrong number into a named failure.

## Step 5 — Validate a transfer before opening a channel

Transfers are shape-checked against the device's own DMA constraints, before any
channel is opened. Run:

```python
from dau_driver import DmaDirection, TransferValidationError

try:
    device.prepare_transfer(DmaDirection.C2H, device_address=0x40, byte_count=100)
except TransferValidationError as exc:
    print("TransferValidationError:", exc)

transfer = device.prepare_transfer(DmaDirection.H2C, device_address=0x40, byte_count=128)
print(transfer.direction, hex(transfer.device_address), transfer.byte_count, transfer.channel_path.name)
```

**Output:**

```text
TransferValidationError: byte_count must be aligned to 64 bytes
h2c 0x40 128 xdma0_h2c_0
```

100 bytes is not a multiple of the device's 64-byte alignment, so it never
reaches the bus. The valid transfer resolves to the H2C channel path — one
`DmaTransfer` describing exactly what is about to happen.

## Step 6 — Run an aggregation job

Now the whole round trip: encode a column into a DAU stream batch, DMA it in,
let the device compute, and decode the scalar result. The fake device needs
something to play the part of the hardware, which is what
`golden_aggregation_trigger` does — it decodes the input batch and stages the
golden answer where the C2H read will find it. Run:

```python
import struct

from dau_core.stream import LogicalType, OperationCode, OperationDescriptor, output_type_for
from dau_driver import execute_scalar_operation
from dau_driver.testing import golden_aggregation_trigger

values = [3, 1, 4, 1, 5, 9, 2, 6]

operation = OperationDescriptor(
    operation_id=0,
    opcode=OperationCode.SUM,
    input_column_id=0,
    output_field_id=0,
    output_type=output_type_for(OperationCode.SUM),
    accumulator_width_bits=64,
)

result = execute_scalar_operation(
    device,
    LogicalType.INT32,
    struct.pack(f"<{len(values)}i", *values),
    row_count=len(values),
    operation=operation,
    input_device_address=0x0000,
    output_device_address=0x1000,
    output_byte_count=192,
    trigger=golden_aggregation_trigger(transport, 0x1000),
)

print("device:", result.value)
print("python:", sum(values))
```

**Output:**

```text
device: 31
python: 31
```

Look at what actually crossed the bus:

```python
print("h2c writes:", [(hex(address), len(payload)) for address, payload in transport.h2c_writes])
print("c2h reads: ", [(hex(address), count) for address, count in transport.c2h_reads])
```

**Output:**

```text
h2c writes: [('0x0', 256)]
c2h reads:  [('0x1000', 192)]
```

Eight int32 values became a 256-byte stream batch: a header, a column descriptor,
an operation descriptor, and the payload, all padded onto the 64-byte grid the
DMA engine requires.

## Step 7 — Watch a capability check refuse

The driver checks the requested operation against the device's advertised
capabilities *before* any DMA. Run:

```python
from dau_driver import StreamExecutionError

try:
    execute_scalar_operation(
        device,
        LogicalType.INT32,
        struct.pack("<2i", 1, 2),
        row_count=2,
        operation=OperationDescriptor(
            operation_id=0,
            opcode=OperationCode.AVERAGE,
            input_column_id=0,
            output_field_id=0,
            output_type=LogicalType.INT64,
            accumulator_width_bits=64,
        ),
        input_device_address=0x0000,
        output_device_address=0x1000,
        output_byte_count=192,
    )
except StreamExecutionError as exc:
    print("StreamExecutionError:", exc)

print("h2c writes since:", len(transport.h2c_writes))
```

**Output:**

```text
StreamExecutionError: device does not support average for int32
h2c writes since: 1
```

The write count did not go up. Nothing was staged, because the refusal happened
first.

## What you have done

You have exercised the whole host surface — discovery handle, capabilities,
registers, status, BAR health, transfer validation, and a full job round trip —
against a transport that needs no hardware. This is also how the package's own
test suite runs: every execution path has to work against a fake, so hardware-only
behaviour stays behind the `DeviceTransport` boundary.

From here:

- To run many batches with staging overlapped against compute, see
  [Run a scan over many batches](First-Scan-Run).
- To find real devices on a real host, see
  [Find and check a device](Find-a-Device).
- For why the fake exists at all, read
  [The transport boundary](Transport-Boundary).
