# How to move data to and from a device

This guide shows you how to get bytes across the host link: raw DMA in both
directions, the shape rules the device enforces, and the stream codec that turns
typed columns into the batch format the shells read.

Two address spaces are in play and confusing them is the classic mistake. DMA
device addresses are **device memory** addresses. Register offsets are
**AXI-lite** offsets in the register window. `read_dma(device_address=0x1000)`
and `read_register(0x1000)` have nothing to do with each other.

## Write and read raw bytes

```python
transfer = device.write_dma(device_address=0x0000_0000, data=payload)
readback = device.read_dma(device_address=0x0000_0000, byte_count=len(payload))
assert readback == payload
```

`write_dma` returns the `DmaTransfer` it validated and performed; `read_dma`
returns the bytes. Both take an optional `timeout_seconds` (default 5.0).

## Get the shape right the first time

Every transfer is validated against the device's own advertised DMA constraints
before a channel is opened, so a bad shape costs you nothing:

```python
from dau_driver import DmaDirection

transfer = device.prepare_transfer(DmaDirection.H2C, device_address=0x40, byte_count=128)
```

`TransferValidationError` is raised when the byte count is not positive, the
device address is negative, the byte count exceeds `max_transfer_bytes`, the
address or byte count is not a multiple of `alignment_bytes`, or the direction's
channel is not available.

Read the constraints from the device rather than hard-coding them:

```python
dma = device.capabilities.dma
dma.alignment_bytes
dma.max_transfer_bytes
```

For a maximum-sized loopback, use `dma.max_aligned_transfer_bytes` rather than
computing the descriptor limit yourself.

## Guard against addressing past the end of memory

An address beyond the device's memory does not fail — it wraps to the bottom, and
you silently read or overwrite the wrong region. Tell the handle how big its
memory is, then program addresses through the method form:

```python
import dataclasses

from dau_core.registers import RegisterOffset

device = dataclasses.replace(device, memory_bytes=platform.memory.size_bytes)
device.write_address_register(RegisterOffset.INPUT_ADDRESS_LOW, RegisterOffset.INPUT_ADDRESS_HIGH, address)
```

`RegisterAccessError` is then raised for an address at or past the limit. Left as
`None`, the caller is trusted — the guard is only as good as the fact, and
inventing a default would either refuse valid addresses or give false confidence.

The module-level `write_address` function reads `memory_bytes` off the device by
default. Pass an integer to override the limit, or `None` to disable the check
entirely — but disable it deliberately, because the default is what makes the
guard apply to callers who never thought about it.

## Program a 64-bit address into a register pair

Device addresses are wider than a register, so they are written as a LOW/HIGH
pair:

```python
from dau_core.registers import RegisterOffset
from dau_driver.device import write_address

write_address(device, RegisterOffset.INPUT_ADDRESS_LOW, RegisterOffset.INPUT_ADDRESS_HIGH, address)
```

Always write both halves, which is what `write_address` does even when the high
half is zero. A design whose master is wider than 32 bits keeps the high half
across a LOW write, so writing only LOW inherits whatever the previous job left
and quietly addresses the wrong region.

Pass the offsets explicitly rather than deriving `low + 4`. The job pair is
adjacent, but a NoC lane's is not — its high half is at `0x14`, because `0x04` is
the result length.

`write_address` is a function, not a method, because devices here are duck-typed:
test fakes and alternative transports implement `write_register` without
subclassing `DauDevice`.

## Encode typed columns as a stream batch

The shells read a DAU stream batch: a header, one descriptor per column, optional
operation descriptors, and 64-byte-aligned payloads. Build one from fixed-width,
null-free columns:

```python
import struct

from dau_core.stream import LogicalType, OperationCode, OperationDescriptor, output_type_for
from dau_driver.stream_codec import RecordBatchColumn, StreamEncoder

symbols = [1, 1, 2]
prices = [1050, 1075, 990]

columns = (
    RecordBatchColumn(field_id=0, logical_type=LogicalType.INT32, payload=struct.pack(f"<{len(symbols)}i", *symbols)),
    RecordBatchColumn(field_id=1, logical_type=LogicalType.INT32, payload=struct.pack(f"<{len(prices)}i", *prices)),
)

operation = OperationDescriptor(
    operation_id=0,
    opcode=OperationCode.SUM,
    input_column_id=1,
    output_field_id=0,
    output_type=output_type_for(OperationCode.SUM),
    accumulator_width_bits=64,
)

encoded = StreamEncoder.encode_record_batch(columns, row_count=len(prices), operations=(operation,))
```

For a single column, `StreamEncoder.encode_column(logical_type, payload, row_count=..., operations=...)` is the one-column form.

Nulls are rejected: the format has no validity bitmap, so `null_count` must be
zero. Drop or fill upstream.

## Encode straight from PyArrow

```python
import pyarrow as pa
from dau_driver.stream_codec import StreamDecoder, StreamEncoder

batch = pa.RecordBatch.from_arrays(
    [pa.array([1, 1, 2], type=pa.int32()), pa.array([1050, 1075, 990], type=pa.int32())],
    names=["symbol", "price"],
)

encoded = StreamEncoder.encode_arrow_batch(batch)
restored = StreamDecoder.decode_record_batch_to_arrow(encoded, names=["symbol", "price"])
assert restored.equals(batch)
```

Column data is taken as a zero-copy, offset-aware view of the primitive buffer,
so slicing an array before encoding is cheap. A column with nulls raises
`InvalidStreamError` naming the column.

## Decode what comes back

```python
decoded = StreamDecoder.decode_record_batch(encoded)
decoded.header.row_count
decoded.column_payload(1)          # by field_id, not by position
```

For a single-column batch use `decode_column`; for the one-row result of a
scalar aggregation use `decode_scalar_result`, which returns the typed `value`
directly.

Decoding validates as it goes — version, header length, alignment, payload
windows, per-column widths and row counts, and the operation descriptors — and
raises `InvalidStreamError` on any mismatch. A truncated or misaligned read
surfaces there rather than as a plausible wrong number.

## Run one aggregation as a single call

```python
from dau_driver import execute_scalar_operation

result = execute_scalar_operation(
    device,
    LogicalType.INT32,
    struct.pack(f"<{len(prices)}i", *prices),
    row_count=len(prices),
    operation=operation,
    input_device_address=0x0000_0000,
    output_device_address=0x0010_0000,
    output_byte_count=192,
    trigger=my_trigger,
)
print(result.value)
```

The operation is checked against the device's advertised capabilities *before*
any DMA — an unsupported opcode or a mismatched output type raises
`StreamExecutionError` with nothing staged. `trigger` is the hook that starts the
job between the H2C write and the C2H read; supply the register sequence your
shell expects, or use `dau_driver.testing.golden_aggregation_trigger` against a
fake.

## Next

- [Run scan jobs](Run-Scan-Jobs) — many batches, with staging overlapped
  against compute.
- [Stream codec reference](Stream-Codec) — every encoder and
  decoder entry point.
- [Allocate device storage](Allocate-Device-Storage) — handing out device
  addresses safely.
