# Device and discovery reference

`dau_driver.discovery`, `dau_driver.device`, `dau_driver.transfer`,
`dau_driver.status`, `dau_driver.capability_decode`, `dau_driver.storage`, and
`dau_driver.errors`. Register offsets, capability models, and stream types come
from `dau-core`; this package owns transport and execution.

## Discovery

### `discover_devices`

```python
discover_devices(root: str | Path = Path("/dev")) -> list[DauDevice]
```

Every DAU-compatible device under `root`. Scans for `xdma*_user` nodes paired
with `xdma*_h2c_0` and `xdma*_c2h_0`. When the Linux scan finds nothing, `root`
is the default, and the platform is macOS, falls back to the DriverKit transport.
Endpoints raising `UnhealthyBarError` or `WrongBitstreamError` are skipped.

### `identify_device`

```python
identify_device(user_path, *, h2c_path=None, c2h_path=None) -> DauDevice
```

Identify one device by its user node. Channel paths default to the conventional
siblings. Raises `UnhealthyBarError` when the magic word reads all ones,
`WrongBitstreamError` when it reads anything other than `DAU_MAGIC_WORD`, and
`RegisterAccessError` on a failed register read.

The register window is located by probing the shell base and then the stream-job
contract window, retried a few times so a transient after a PCIe rescan does not
masquerade as a wrong bitstream.

## `DauDevice`

A frozen dataclass bundling the register window and DMA channel paths behind a
transport.

| Field                    | Type                      | Default | Description                                                                                            |
| ------------------------ | ------------------------- | ------- | ------------------------------------------------------------------------------------------------------ |
| `name`                   | `str`                     | —       | Device name, e.g. `xdma0`.                                                                             |
| `backend`                | `str`                     | —       | Transport backend, e.g. `linux-xdma`.                                                                  |
| `user_path`              | `Path`                    | —       | Register window node.                                                                                  |
| `h2c_path`               | `Path`                    | —       | Host-to-card channel.                                                                                  |
| `c2h_path`               | `Path`                    | —       | Card-to-host channel.                                                                                  |
| `capabilities`           | `DeviceCapabilities`      | —       | The decoded advertisement.                                                                             |
| `register_window_offset` | `int`                     | `0`     | Added to every register offset.                                                                        |
| `transport`              | `DeviceTransport \| None` | `None`  | Overrides the path-based transport.                                                                    |
| `memory_bytes`           | `int \| None`             | `None`  | The addressable size of device memory, when known. Enables the wrap guard in `write_address_register`. |

`resolved_transport` (property) returns `transport`, or a `PathDeviceTransport`
over the three paths.

### Registers

| Method                                                          | Behaviour                                                                                  |
| --------------------------------------------------------------- | ------------------------------------------------------------------------------------------ |
| `read_register(offset)`                                         | Read one 32-bit register at `register_window_offset + offset`.                             |
| `write_register(offset, value)`                                 | Write one 32-bit register. Raises `RegisterAccessError` when `value` does not fit 32 bits. |
| `write_address_register(low_offset, high_offset, address)`      | Program a 64-bit address across a register pair, applying the `memory_bytes` guard.        |
| `check_bar_health(*, offsets=(MAGIC, STREAM_PROTOCOL_VERSION))` | Raise `UnhealthyBarError` if any listed offset reads all ones.                             |
| `read_status()`                                                 | A `DeviceStatus`.                                                                          |
| `read_counters()`                                               | A `DeviceCounters`.                                                                        |
| `read_register_block_snapshot()`                                | A `dau_core.registers.RegisterBlockSnapshot` of the whole identity block.                  |
| `close()`                                                       | Close the transport, if it has a `close`.                                                  |

**Register offsets are window-relative.** `read_register` and `write_register`
add `register_window_offset` themselves, so a `RegisterOffset` is passed as
declared and never pre-biased. The module-level `read_u32(path, offset)` and
`read_u64(path, offset)` are the exception: they address the node absolutely,
and a caller using them composes the window itself.

The window is discovered rather than configured.
`capability_decode.REGISTER_WINDOW_OFFSETS` is the candidate list —
`(0x0, 0x1000)`, the shell base first and the stream-job contract window
second — and discovery returns the first offset whose `MAGIC` reads back as
the DAU magic word, retrying a few times to let a freshly programmed fabric
settle. A composition top decodes only the low twelve address bits and returns
all ones for anything unmapped, so a health probe must stay on contract
registers inside the 4 KiB window.

64-bit registers are read as two separate 32-bit accesses, deliberately: the
user BAR can short-read an eight-byte access.

### DMA

| Method                                                       | Behaviour                                                                                           |
| ------------------------------------------------------------ | --------------------------------------------------------------------------------------------------- |
| `prepare_transfer(direction, *, device_address, byte_count)` | Validate and return a `DmaTransfer` without performing it.                                          |
| `read_register_block_snapshot() -> RegisterBlockSnapshot`    | The identity/capability words the resident shell advertises, including `lane_count` and `build_id`. |
| `read_dma(*, device_address, byte_count) -> bytes`           | Validate, then read.                                                                                |
| `write_dma(*, device_address, data) -> DmaTransfer`          | Validate, then write.                                                                               |

DMA device addresses are device memory addresses, not AXI-lite register offsets.

## `DeviceTransport`

A `Protocol`. Implement it to swap the backend — the fakes in the test suite do
exactly this.

```python
class DeviceTransport(Protocol):
    def read_u32(self, offset: int) -> int: ...
    def write_u32(self, offset: int, value: int) -> None: ...
    def read_dma(self, device_address: int, byte_count: int) -> bytes: ...
    def write_dma(self, device_address: int, data: bytes | bytearray | memoryview) -> int: ...
```

`PathDeviceTransport(user_path, h2c_path, c2h_path)` is the file-backed
implementation, using `os.pread` / `os.pwrite` through `dau_driver.xdma_io`.

## `write_address`

```python
write_address(device, low_offset, high_offset, address: int, *, memory_bytes=<from device>) -> None
```

Program a 64-bit address across its LOW/HIGH register pair on any object with a
`write_register` method. Both halves are always written, including a zero high
half. Raises `RegisterAccessError` when the address does not fit 64 bits, or when
a memory limit is known and the address is at or past it.

`memory_bytes` defaults to a sentinel, not to `None`, and the sentinel means
"read `device.memory_bytes`". Passing `None` explicitly disables the bound
check; passing an integer overrides it. A plain `None` default would have made
the guard silently inactive for every caller who did not know to opt in.

Offsets are passed explicitly; register pairs are not always adjacent. The job
pair is, but a NoC lane's high half is at `0x14` — `0x04` is its result length.

Module-level helpers `read_u32(path, offset)` and `read_u64(path, offset)` read
registers directly from a node path.

## Transfers

### `DmaDirection`

A `StrEnum`: `H2C` (`"h2c"`), `C2H` (`"c2h"`).

### `DmaTransfer`

| Field            | Type           | Default |
| ---------------- | -------------- | ------- |
| `direction`      | `DmaDirection` | —       |
| `device_address` | `int`          | —       |
| `byte_count`     | `int`          | —       |
| `channel_path`   | `Path`         | —       |

### `validate_transfer`

```python
validate_transfer(direction, device_address, byte_count, constraints: DmaConstraints, *, memory_bytes=None) -> None
```

Raises `TransferValidationError` when `byte_count` is not positive,
`device_address` is negative, the span `device_address + byte_count` runs past
64 bits or past `memory_bytes` when one is given, `byte_count` exceeds
`max_transfer_bytes`, either value is not aligned to `alignment_bytes`, or the
direction's channel count is zero. `DauDevice` passes its own `memory_bytes`.

## Status

### `DeviceStatus`

| Field        | Type  |
| ------------ | ----- |
| `raw_status` | `int` |
| `last_error` | `int` |

`raw_status` is the STATUS word as read. No shell assigns its bits yet, so it
is not decoded into named flags.

### `DeviceCounters`

`h2c_bytes`, `c2h_bytes`, `rows`, `batches`, `stalls` — all `int`.

## Capability decode

Shared register-block decode semantics for every transport backend.

| Function                                      | Returns                                                                                                                                         |
| --------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------- |
| `decode_platform_id(value)`                   | The ASCII platform id, or `""` when the word is not printable ASCII.                                                                            |
| `decode_register_map_version(value)`          | The `RegisterMapVersion`. An all-ones word is a dead aperture, not a version, and decodes as `NOT_ADVERTISED_REGISTER_MAP_VERSION` (0.0).       |
| `read_register_block_snapshot(read_register)` | The discovery-time identity snapshot. `read_register` abstracts the backend's window access.                                                    |
| `operators_from_snapshot(snapshot)`           | `(hardware operators, host-computed operators)` decoded by register map version.                                                                |
| `contract_from_manifest_metadata(metadata)`   | The `CapabilityContract` from a shell-build manifest's `capability_contract` key, or `None`.                                                    |
| `operators_from_contract(contract)`           | `(hardware operators, host-computed operators)` from the concrete composition contract — per-tile opcodes and input types, no bitmap expansion. |

Register map 0.2 and above advertises the composed truth: `OPERATOR_BITMAP`
carries only HDL-executed opcodes, typed INT32 at register level, and
`HOST_OPCODE_BITMAP` carries protocol-level opcodes the host derives from
hardware results. Host opcodes must never be submitted to the device. The
composed-capability words (`LANE_COUNT`, `HOST_OPCODE_BITMAP`, `SORT_CAPACITY`)
are only read on 0.2 and above; all-ones is normalized to not-advertised. The
job-capacity words (`INPUT_ROW_CAPACITY`, `OUTPUT_RECORD_CAPACITY`) need 0.3 and
above. Both gates, `CAPABILITY_WORDS_REGISTER_MAP_VERSION` and
`JOB_CAPACITY_REGISTER_MAP_VERSION`, are defined in `dau_core.registers`.

## Storage

### `StorageAllocator`

```python
StorageAllocator(*, tier: str, capacity_bytes: int, base: int = 0,
                 reserved: tuple[tuple[int, int], ...] = (), align: int = 64)
```

First-fit allocator over one tier's `[base, base + capacity_bytes)`, minus the
reserved regions. Raises `ValueError` for a non-positive capacity, an `align`
that is not a positive power of two, or a reserved region with non-positive size
or lying outside the tier.

| Method                              | Behaviour                                                                                                                                                                     |
| ----------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `allocate(nbytes) -> StorageHandle` | First-fit, aligned. Raises `StorageExhaustedError` when no span fits, `ValueError` for a non-positive size.                                                                   |
| `free(handle)`                      | Bump the handle's generation and return the live span to the free list, coalesced. Raises `StaleHandleError` on a stale, forged, or foreign handle — so a double free faults. |
| `resolve(handle) -> int`            | The handle's device address, generation-checked. Raises `StaleHandleError`.                                                                                                   |
| `stage(device, handle, data)`       | Resolve, check `data` against the live grant, and `device.write_dma`. Raises `ValueError` when the payload exceeds the grant.                                                 |

### `StorageHandle`

| Field        | Type  | Description                                                |
| ------------ | ----- | ---------------------------------------------------------- |
| `id`         | `int` | Handle id; recycled after a free.                          |
| `generation` | `int` | Incremented each time the id is reissued.                  |
| `base`       | `int` | Device address at allocation time.                         |
| `capacity`   | `int` | Granted size.                                              |
| `tier`       | `str` | The issuing tier's name.                                   |
| `allocator`  | `int` | Binds the handle to the allocator instance that issued it. |

Every operation trusts the allocator's live record, never the handle's own copy
of `base` or `capacity`.

### Handle table

```python
HandleTable(device, allocator: StorageAllocator, *, capacity: int)
```

One allocator's slots mirrored into one device's handle table, so a job can name
`(id, generation)` where it would otherwise name an address. `capacity` is the
built shell's `handle_table_capacity`: a table is hardware, planned into the
design and priced with it, and no register advertises it, so it is passed in
rather than discovered. A non-positive capacity raises `ValueError`.

| Method                                                                                         | Behaviour                                                                                                                                                                                                                              |
| ---------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `install(handle)`                                                                              | Program the handle's grant into slot `handle.id` from the allocator's **live** record. Raises `StaleHandleError` when the allocator will not resolve it, `HandleTableError` for a slot past `capacity` or a base off the 64-byte grid. |
| `free(handle)`                                                                                 | Drop the slot out of liveness. Unconditional on the allocator — freeing can only make the table refuse more, so it is safe whether or not the host has released the span yet.                                                          |
| `arm(handle)`                                                                                  | Name the handle the next job resolves, in place of an input address.                                                                                                                                                                   |
| `run_resident_scan(handle, *, row_count, row_bytes, collect, repeats=1, timeout_seconds=60.0)` | `scan_execution.run_resident_scan` with the raw address replaced by the handle: arm once, then run the job `repeats` times. Returns one `collect(device)` per repeat.                                                                  |

Two authorities, deliberately different. `install` trusts the host — the live
record is what a slot is programmed from, and a handle the host no longer
believes in must never become a grant the device does. `arm` trusts nothing: a
stale handle has to reach the device, or the table is never the thing that
refused it.

### `HandleFaultError`

The shell refused to start the job on the handle it was armed with. A
`PipelineJobError`, so a caller catching every job failure still catches it,
with its own type because the whole value of the table is that this case is
distinguishable: the job did not run, nothing was read, nothing was written, and
no result register moved. Results for repeats that did run are dropped with it.

`code` is a `dau_core.registers.HandleFaultCode`:

| Code            | Meaning                                                                                     |
| --------------- | ------------------------------------------------------------------------------------------- |
| `BAD_ID`        | The table has no such slot; capacity is planned at build time, not grown at run time.       |
| `STALE`         | The slot is freed, or live at another generation — that region belongs to someone else now. |
| `OUT_OF_BOUNDS` | The job asks for more bytes than the grant.                                                 |

A code outside that set came from the datapath, and the datapath ran; it is
raised as a plain `PipelineJobError`.

### Errors

`StaleHandleError(RuntimeError)`, `StorageExhaustedError(RuntimeError)`,
`HandleTableError(DauDriverError)`, `HandleFaultError(PipelineJobError)`.

## Native extension

```python
native_info() -> NativeInfo
```

`NativeInfo` fields: `backend` (`"nanobind"` or `"python"`), `abi_version`,
`package_version`, `extension_available`. The pure-Python fallback is used when
`dau_driver.extension` is not importable; source-tree imports work before the
extension is built.

## Exceptions

All derive from `DauDriverError(Exception)`.

| Exception                 | Raised when                                                       |
| ------------------------- | ----------------------------------------------------------------- |
| `DeviceNotFoundError`     | No device matched.                                                |
| `RegisterAccessError`     | A register read or write failed, short-read, or was out of range. |
| `UnhealthyBarError`       | A register read returned all ones.                                |
| `TransferValidationError` | A transfer's shape violates the device's DMA constraints.         |
| `TransferIoError`         | A transfer's I/O failed.                                          |
| `WrongBitstreamError`     | The register window did not report the DAU magic word.            |

`StreamExecutionError` derives from `ValueError`, and `PipelineJobError` from
`RegisterAccessError`.

## Test doubles

`dau_driver.testing` is shared across the DAU test suites so the fake device's
semantics cannot drift between packages. Not imported by production code.

| Name                                                                     | Purpose                                                                                                                                            |
| ------------------------------------------------------------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------- |
| `FakeTransport()`                                                        | In-memory `DeviceTransport` recording `h2c_writes`, `c2h_reads`, `register_reads`, and exposing writable `registers` and `c2h_payloads` dicts.     |
| `default_int32_capabilities(operators=None, *, max_transfer_bytes=4096)` | The standard fake INT32 aggregation capability set.                                                                                                |
| `fake_device(tmp_path, transport, *, capabilities=None)`                 | A `DauDevice` over a `FakeTransport`; the paths are never opened.                                                                                  |
| `golden_aggregation_trigger(transport, output_address)`                  | A `StreamTrigger` emulating the aggregation hardware: decode the input batch, compute the golden scalar, stage the result stream for the C2H read. |
