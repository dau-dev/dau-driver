# The transport boundary: why a driver owns almost nothing

dau-driver moves bytes and runs jobs. It does not know what a register means,
what a batch header contains, or what an opcode computes. All of that comes from
`dau-core`. This page is about where that line sits, why it is drawn there, and
what it makes possible.

## Two things that must not be the same package

A device's semantics and a host's transport change for completely different
reasons. Register offsets, stream framing, and capability words change when the
*hardware contract* changes: a new register map version, a new operator, a wider
capability advertisement. Transport code changes when the *host* changes: a new
OS, a new devnode layout, a DMA quirk on one bitstream.

Putting both in one package means every hardware-contract change touches the
driver, and every host quirk risks changing the contract. The contract would
also have one consumer, this driver, and a contract with one consumer is in
practice an implementation detail. The compiler, the
simulator, and the golden models all need the same offsets.

So `dau-core` owns the semantics and dau-driver imports them. In practice, a
hardware quirk gets fixed in the backend layer and stays there. When one bitstream's AXI path corrupted whole-transfer C2H reads above a
threshold, the fix was a chunking field on the device handle, applied inside
`read_dma`. It did not become a rule in the protocol contract, because it was
never true of the protocol, only of that image. It is also why the field could
be deleted once no such image remained, with nothing else to change.

## `DeviceTransport` is four methods

The entire device-touching surface is a `Protocol` with `read_u32`, `write_u32`,
`read_dma`, and `write_dma`. Everything above it (register decoding, transfer
validation, the job runners, the stream codec) is written against those four
calls.

The interface is kept that narrow on purpose. Two real transports implement it: Linux XDMA character devices via `os.pread` and
`os.pwrite`, and a macOS DriverKit system extension via IOKit user-client calls.
They share nothing but the protocol, and the layers above cannot tell them apart.

It also makes testing cheap: a fake transport is fifty lines. `FakeTransport`
records DMA traffic and register writes in dictionaries, and every Python
execution path in the package (the scan runner's ping-pong staging, the fleet's
demand-driven scheduling, the bar shell's DDR layout, capability checks) runs
against it in CI, on machines with no FPGA. What sits below the boundary (the kernel module, the DriverKit extension,
the native extension) is tested on hardware.

This is the package's testing policy stated as an architectural constraint:
**every Python execution path must run against a fake, and hardware-only
behaviour sits behind `DeviceTransport`.** If a code path cannot be reached without a board, it
is in the wrong layer.

The fake lives in `dau_driver.testing`, shipped rather than kept in the test
directory, because the driver's consumers test against it too. One definition
means the fake device's semantics cannot drift between packages, as they would if
each package wrote its own.

## Discovery skips anything that is not a DAU shell

`discover_devices` will not return an XDMA endpoint just because it is an XDMA
endpoint. It reads the register window, looks for the DAU magic word, and decodes
a capability advertisement. Anything else is skipped.

`discover_devices` and `identify_device` treat a mismatch differently on purpose.
Discovery scans a machine that may hold other people's cards, so a non-DAU
endpoint is not an error, just not a match, and raising would make a mixed
machine undiscoverable. `identify_device` takes a path you named, so a bad
answer there is an error, and it raises `WrongBitstreamError` or
`UnhealthyBarError` by name.

The all-ones case has its own exception because it does not look like a failure.
A dead AXI-lite path returns `0xFFFFFFFF` for every read, which is a valid 32-bit
number. It decodes as a version, a bitmap or a counter, all of them plausible. `UnhealthyBarError` exists so that a dead path is a
named failure rather than a strange number four layers later, and
`check_bar_health()` exists so you can assert it before and after anything you
care about.

The magic probe retries a few times, with a short delay. The first AXI-lite reads
after a PCIe rescan can transiently return stale data before the link settles, and
a single-shot probe would report a healthy DAU shell as the wrong bitstream. This
is the kind of fact that only transport code should know.

## Validate the shape before opening the channel

Every DMA transfer is checked against the device's *own advertised* constraints
(alignment, maximum size, channel availability) before a channel is opened. The
errors are typed and say which rule was broken.

Checking against the advertisement rather than a constant is what lets the
driver work on a new board. `dma.max_aligned_transfer_bytes` is the value to use
for a maximum-sized loopback, not a hard-coded descriptor limit, because the
limit is a property of the device in front of you.

Two related guards also stop rather than proceed.
`write_address` always writes both halves of an address pair, including a zero
high half, because a design whose master is wider than 32 bits keeps the high half
across a LOW write, so writing only LOW reuses whatever the previous job left there
and addresses the wrong region. And when the handle knows the device's
memory size, an address past it raises rather than wrapping to the bottom of
memory, which is what the hardware would otherwise do.

The `memory_bytes` field defaults to `None`, because a guard is only as good as
the fact behind it. Inventing a default size would either
refuse valid addresses on a larger board or give false confidence on a smaller
one.

## Where the model bends

Two places do not fit this picture.

`write_address` is a module-level function rather than a method on `DauDevice`,
because devices in this codebase are duck-typed. Test fakes and alternative
transports implement `write_register` without subclassing `DauDevice`, and making
this a method broke every one of them. The runners take `device` untyped for the
same reason.

`KeyedPipelineConfig` is a plain dataclass while the rest of the DAU stack uses
frozen pydantic models. It mirrors the `PIPELINE_*` register encodings (it is
wire packing, not configuration), and the rule the codebase follows is that
register mirrors and DMA structs stay dataclasses while anything authored or
composed follows the model patterns.

## Related

- [Find and check a device](../how-to/find-a-device.md): discovery and health in
  practice.
- [Talk to a device without any hardware](../tutorial/first-device.md): the fake
  transport as a working tool.
- [Device and discovery reference](../reference/device-and-discovery.md): the
  protocol and every implementation of it.
