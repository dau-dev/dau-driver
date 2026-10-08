# XDMA macOS DriverKit Extension

This directory contains a macOS DriverKit system extension (dext) that adapts the Xilinx XDMA PCIe driver model for macOS: device matching, BAR register access, and H2C/C2H DMA through the XDMA engines, driven from Python over IOKit user-client calls.

## Overview

The XDMA driver provides:
- **PCI Device Matching**: Automatically detects Xilinx XDMA PCIe devices (vendor 0x10ee and 0x1d0f)
- **BAR Register Access**: Read/write access to PCIe Base Address Registers for device control
- **DMA Transfers**: Host-to-Card (H2C) and Card-to-Host (C2H) transfers through the XDMA descriptor engines
- **Device Information**: Query vendor, device, and subsystem IDs

The DriverKit service is registered as `com.dau-dev.xdma` — the one name user-space tools match. (`XDMADriver` survives only as the dext C++ class name.)

## Architecture

### Components

1. **XDMADriver** (`XDMADriver.h/cpp`)
   - Main driver class, subclass of `IOService`
   - Handles PCI device attachment and initialization
   - Maps BARs into kernel address space
   - Discovers XDMA engines
   - Sets up DMA descriptor rings
   - Manages MSI/MSI-X interrupts

2. **XDMAUserClient** (`XDMAUserClient.h/cpp`)
   - IOUserClient subclass for userspace communication
   - Provides external methods for:
     - `kMethodReadBAR`: Read 32-bit register from BAR
     - `kMethodWriteBAR`: Write 32-bit register to BAR
     - `kMethodDMAWrite`: H2C DMA transfer
     - `kMethodDMARead`: C2H DMA transfer
     - `kMethodGetInfo`: Get device information

3. **XDMADMAEngine** (`XDMADMAEngine.h/cpp`)
   - DMA descriptor format definitions
   - Engine register offsets and constants
   - Reference implementation notes

### Supported Devices

The driver matches the following PCI device IDs:

**Xilinx (0x10ee):**
- 0x9048, 0x9044, 0x9042, 0x9041, 0x903f, 0x9038, 0x9028, 0x9018
- 0x9034, 0x9024, 0x9014, 0x9032, 0x9022, 0x9012, 0x9031, 0x9021, 0x9011
- 0x8011-0x8038 (in steps matching Linux driver)
- 0x7011-0x7038 (in steps matching Linux driver)
- 0x6828, 0x6830, 0x6928, 0x6930, 0x6A28, 0x6A30, 0x6D30
- 0x4808, 0x4828, 0x4908, 0x4A28, 0x4B28
- 0x2808

**AWS (0x1d0f):**
- 0xf000, 0xf001

## Prerequisites

### Development Environment

1. **macOS 26.2 or later**
   - the dext's DriverKit deployment target is 25.2, which ships with macOS 26.2

2. **Xcode 26 or later**
   - Install from Mac App Store
   - Includes DriverKit SDK and required tools

3. **Apple Developer Account**
   - Required for code signing
   - Free accounts can sign for local development
   - Paid account required for distribution

4. **System Integrity Protection (SIP)**
   - Reduced SIP required for loading third-party dexts
   - See "Installation" section below

## Building

### Using Command Line

For source validation without provisioning/signing:

```bash
DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer make compile-unsigned
```

For a signed local development build, pass your Apple Developer team ID at build time. Do not commit a personal `DEVELOPMENT_TEAM` value into the Xcode project.

```bash
DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer DEVELOPMENT_TEAM=<team-id> make compile
```

To let Xcode create or refresh local signing profiles for a configured account:

```bash
DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer DEVELOPMENT_TEAM=<team-id> make compile-provision
```

### Signing With An Apple Development Account

DriverKit dext signing has three local prerequisites:

- Xcode is signed in to an Apple Developer account with a usable team.
- The login keychain has an `Apple Development` code-signing identity for that team.
- Xcode has or can create a DriverKit App Development provisioning profile for the dext bundle identifier, `com.dau-dev.xdma`, with the DriverKit PCI entitlement.

First, add the account in Xcode: Settings -> Accounts -> Apple ID. Select the team, open Manage Certificates, and create an Apple Development certificate if one is missing. The Team ID is shown in that account/team view. You can also usually recover it from the certificate common name:

```bash
security find-identity -v -p codesigning | grep 'Apple Development'
security find-identity -v -p codesigning \
   | sed -n 's/.*Apple Development:.*(\([A-Z0-9]\{10\}\)).*/\1/p'
```

Check whether local provisioning profiles already exist:

```bash
find "$HOME/Library/MobileDevice/Provisioning Profiles" \
   -maxdepth 1 -name '*.mobileprovision' -print
```

To inspect a profile without installing extra tools:

```bash
PROFILE="/path/to/profile.mobileprovision"
security cms -D -i "$PROFILE" | plutil -extract Name raw -o - -
security cms -D -i "$PROFILE" | plutil -extract TeamIdentifier.0 raw -o - -
security cms -D -i "$PROFILE" | plutil -extract Entitlements.application-identifier raw -o - -
security cms -D -i "$PROFILE" | plutil -extract Entitlements.com.apple.developer.driverkit.transport.pci raw -o - -
```

The profile must correspond to `com.dau-dev.xdma`. If automatic provisioning cannot create one, create an explicit DriverKit App Development profile in the Apple Developer portal for that bundle identifier and install it by opening the downloaded `.mobileprovision` file.

Build once with provisioning updates enabled:

```bash
DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer \
   DEVELOPMENT_TEAM=<team-id> \
   make compile-provision
```

After the profile exists locally, normal signed builds can use:

```bash
DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer \
   DEVELOPMENT_TEAM=<team-id> \
   make compile
```

Verify the signed dext:

```bash
DEXT=build/Debug-driverkit/com.dau-dev.xdma.dext
codesign -dv --verbose=4 "$DEXT"
codesign -d --entitlements - "$DEXT"
plutil -p "$DEXT/Info.plist" | grep -E 'CFBundleIdentifier|IOUserServerName|IOUserClass'
```

Common signing failures:

- `Signing for "XDMADriver" requires a development team`: pass `DEVELOPMENT_TEAM=<team-id>` or select a team in Xcode for a local-only build.
- `No Accounts`: add your Apple ID in Xcode Settings -> Accounts.
- `No profiles for 'com.dau-dev.xdma' were found`: let Xcode provision with `make compile-provision`, or install a DriverKit App Development profile for `com.dau-dev.xdma`.
- Missing DriverKit PCI entitlement: ensure the team/profile has `com.apple.developer.driverkit.transport.pci`; without it the dext may build incorrectly or fail activation.

## Installation

### 1. Prepare Your System

DriverKit extensions require reduced System Integrity Protection (SIP):

1. **Boot into Recovery Mode:**
   - Intel Mac: Restart and hold `Cmd+R`
   - Apple Silicon: Shut down, hold power button until "Loading startup options" appears

2. **Open Terminal from Recovery Mode:**
   - Utilities → Terminal

3. **Reduce SIP for third-party extensions:**
   ```bash
   csrutil enable --without kext
   ```

4. **Reboot into normal macOS**

### 2. Activate the Extension

System extensions are activated by a signed containing application through `OSSystemExtensionManager`; `systemextensionsctl` does not provide an install verb. For development, use a signed Xcode run action or a small signed host app that embeds `com.dau-dev.xdma.dext` under `Contents/Library/SystemExtensions/` and requests activation.

After activation is requested, macOS may prompt for approval in System Settings → Privacy & Security. Development machines can use:

```bash
systemextensionsctl developer on
```

Use `systemextensionsctl reset` only when intentionally clearing system-extension state.

### 3. Verify Installation

Check if the driver loaded:

```bash
# List loaded dexts
systemextensionsctl list

# Check IOKit registry for the canonical service
ioreg -l -w0 | grep -E 'com\.dau-dev\.xdma|XDMADriver|XDMAUserClient'

# View system logs
log show --predicate 'eventMessage CONTAINS[c] "XDMADriver" OR eventMessage CONTAINS[c] "com.dau-dev.xdma"' --last 5m
```

Then run the Python BAR/register probe:

```bash
dau-xdma-macos-probe --verbose
```

## Usage

### From Python

Use `dau-driver` from the DAU checkout or installed environment:

```python
from dau_driver import discover_devices
from dau_core.registers import RegisterOffset

devices = discover_devices()
dev = devices[0]
magic = dev.read_register(RegisterOffset.MAGIC)
snapshot = dev.read_register_block_snapshot()
```

To probe the DriverKit path directly:

```bash
PYTHONPATH=/path/to/dau-core:/path/to/dau-driver python -m dau_driver.macos_xdma --verbose
```

### From C/Swift (Direct IOKit Access)

```c
#include <IOKit/IOKitLib.h>

// Find the driver
CFMutableDictionaryRef matchingDict = IOServiceNameMatching("com.dau-dev.xdma");
io_service_t service = IOServiceGetMatchingService(kIOMasterPortDefault, matchingDict);

// Open connection
io_connect_t connection;
IOServiceOpen(service, mach_task_self(), 0, &connection);

// Call methods
uint64_t scalarInput[2] = {0, 0x100};  // bar_index=0, offset=0x100
uint32_t scalarOutputCount = 1;
uint64_t scalarOutput[1];

IOConnectCallScalarMethod(connection, kMethodReadBAR,
                         scalarInput, 2,
                         scalarOutput, &scalarOutputCount);

// Close
IOServiceClose(connection);
```

## Trust model

The dext sets no user-client entitlement and declares `allow-any-userclient-access`, so any process on the host that can open the service can read and write the card's registers and run DMA against host memory it supplies. That is deliberate: the client is an unsigned Python process, and it matches what the Linux character devices give under a permissive udev rule. Whoever can run code on the host can drive the card. Do not install the dext on a machine where that is not acceptable.

What the dext does enforce is the shape of a request: the BAR index is one of the six, the offset is 32-bit aligned, and the access lies inside the mapped length of that BAR (recorded at start from `GetBARInfo`), so a register access cannot reach past the device's own ranges; DMA lengths and buffers are checked the same way.

## Device Matching

The driver automatically matches PCI devices based on vendor and device IDs specified in `Info.plist`. When a supported device is detected:

1. macOS loads the driver
2. Driver's `Start()` method is called
3. Driver opens the PCI device and maps BARs
4. Driver discovers XDMA engines
5. Driver calls `SetName("com.dau-dev.xdma")` and `RegisterService()`, making it visible to userspace
6. Userspace opens `XDMAUserClient` through `XDMADriver::NewUserClient`

## Register Map

Based on the Xilinx XDMA IP:

### BAR0 - User Registers
- User-defined registers for FPGA logic
- Typical usage: Control, status, data registers

### BAR1 - XDMA Configuration (optional)
- **H2C Channels**: 0x0000, 0x0100, 0x0200, 0x0300
- **C2H Channels**: 0x1000, 0x1100, 0x1200, 0x1300
- **Interrupt Block**: 0x2000
- **Config Block**: 0x3000

Each DMA channel has:
- Identifier (0x00)
- Control (0x04)
- Status (0x40)
- Completed descriptors (0x48)
- SGDMA descriptor address (0x80, 0x84)

## DMA Transfer Flow

### H2C (Host to Card)

1. Create DMA descriptor with:
   - `src_addr` = host physical address
   - `dst_addr` = device offset
   - `bytes` = transfer size
   - `control` = 0xAD4B0000 (DESC_MAGIC)

2. Write descriptor address to engine's SGDMA registers
3. Start engine (set XDMA_CTRL_RUN_STOP in control register)
4. Wait for completion (poll status or interrupt)

### C2H (Card to Host)

Same as H2C but with reversed addresses:
- `src_addr` = device offset
- `dst_addr` = host physical address

## Troubleshooting

### Driver doesn't load

1. **Check system logs:**
   ```bash
   log stream --predicate 'processImagePath contains "kernel"' | grep -i xdma
   ```

2. **Verify code signing:**
   ```bash
   codesign -dv XDMADriver.dext
   ```

3. **Check entitlements:**
   ```bash
   codesign -d --entitlements - XDMADriver.dext
   ```

### Device not found

1. **Check PCI devices:**
   ```bash
   system_profiler SPPCIDataType | grep -Ei -A 10 '10ee|7011|Xilinx|XDMA'
   ```

2. **Verify device ID in Info.plist:**
   - Device must be in IOPCIPrimaryMatch string

3. **Check IOKit registry:**
   ```bash
   ioreg -p IOService -l -w0 -r -c IOPCIDevice | grep -Ei -A 20 -B 5 'pci10ee|10ee|7011|XDMADriver|com\.dau-dev\.xdma'
   ```

4. **Run the staged probe:**
   ```bash
   dau-xdma-macos-probe --verbose
   ```

### Permission denied

- Ensure SIP is reduced: `csrutil status`
- Approve extension in Security & Privacy preferences
- Check file permissions on `.dext` bundle

### DMA transfers fail

- Verify BAR mapping succeeded (check logs)
- Ensure FPGA bitstream is loaded
- Check XDMA IP configuration in FPGA design
- Verify DMA descriptor format matches XDMA IP version

## Differences from Linux Driver

| Feature | Linux Driver | macOS Driver |
|---------|-------------|--------------|
| Interface | Character devices (`/dev/xdma*`) | IOUserClient methods |
| BAR Access | `pread`/`pwrite` on `/dev/xdma0_user` | `kMethodReadBAR`/`kMethodWriteBAR` |
| DMA | `pread`/`pwrite` on `/dev/xdma0_h2c_0` | `kMethodDMAWrite`/`kMethodDMARead` through the descriptor engines |
| Interrupts | Linux IRQ handlers | IOInterruptDispatchSource |
| Memory | `dma_alloc_coherent` | IOBufferMemoryDescriptor |

## References

- [Apple DriverKit Documentation](https://developer.apple.com/documentation/driverkit)
- [Xilinx XDMA IP Product Guide (PG195)](https://www.xilinx.com/support/documentation/ip_documentation/xdma/v4_1/pg195-pcie-dma.pdf)
- [Linux XDMA Driver](../xdma/)

## License

This driver is provided as-is for use with Xilinx XDMA PCIe devices.

## Support

For issues or questions:
- Check system logs for error messages
- Verify FPGA bitstream and XDMA IP configuration
- Review Xilinx XDMA documentation
- Compare behavior with Linux driver
