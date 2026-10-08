/*
 * XDMADriver.cpp
 * macOS DriverKit extension for Xilinx XDMA PCIe devices
 *
 * Implementation of the main driver class.
 */

#include <string.h>

#include <os/log.h>
#include <DriverKit/IOLib.h>
#include <DriverKit/IOUserServer.h>
#include <DriverKit/IOBufferMemoryDescriptor.h>
#include <DriverKit/IODMACommand.h>
#include <DriverKit/IOMemoryMap.h>
#include <DriverKit/OSData.h>
#include <PCIDriverKit/IOPCIDevice.h>

#include "XDMADriver.h"
#include "XDMAUserClient.h"
#include "XDMADMAEngine.h"

#define XDMA_SERVICE_NAME "com.dau-dev.xdma"
#define XDMA_USER_CLIENT_PROPERTIES "XDMAUserClientProperties"

// Engine offsets
#define XDMA_H2C_CHANNEL_BASE(n)  (0x0000 + (0x100 * (n)))
#define XDMA_C2H_CHANNEL_BASE(n)  (0x1000 + (0x100 * (n)))

// SGDMA descriptor-address register blocks
#define XDMA_H2C_SGDMA_BASE(n)    (0x4000 + (0x100 * (n)))
#define XDMA_C2H_SGDMA_BASE(n)    (0x5000 + (0x100 * (n)))

// Engine register offsets (relative to channel base)
#define ENGINE_ID_REG       0x00

// Engine identifier subsystem values (identifier reg [31:16])
#define XDMA_ID_H2C         0x1fc0
#define XDMA_ID_C2H         0x1fc1

#define XDMA_RING_DESC_COUNT    32
#define XDMA_MAX_TRANSFER_BYTES (64ULL << 20)
#define XDMA_POLL_TIMEOUT_MS    10000

#define XDMA_STAT_ERROR_MASK (XDMA_STAT_ALIGN_MISMATCH | XDMA_STAT_MAGIC_STOPPED | \
                              XDMA_STAT_INVALID_LEN | (0x1FUL << 9) | (0x1FUL << 19))

struct XDMADriver_IVars {
    IOPCIDevice *pciDevice;
    IOBufferMemoryDescriptor *dmaDescriptorRing;
    IODMACommand *ringDMACommand;
    uint64_t ringDeviceAddress;   // ring IOVA as seen by the device
    uint64_t ringCPUAddress;      // ring address in the dext's address space
    uint8_t configBarIndex;       // memoryIndex of the XDMA config BAR
    bool configBarValid;
    uint64_t barSize[6];          // mapped length per memoryIndex; 0 = not a memory BAR
};

bool XDMADriver::init()
{
    if (!super::init()) {
        return false;
    }

    ivars = IONewZero(XDMADriver_IVars, 1);
    if (ivars == nullptr) {
        return false;
    }

    os_log(OS_LOG_DEFAULT, "XDMADriver::init() complete");
    return true;
}

kern_return_t IMPL(XDMADriver, Start)
{
    kern_return_t ret;

    os_log(OS_LOG_DEFAULT, "XDMADriver::Start()");

    ret = Start(provider, SUPERDISPATCH);
    if (ret != kIOReturnSuccess) {
        os_log(OS_LOG_DEFAULT, "XDMADriver: super::Start failed: 0x%x", ret);
        return ret;
    }

    // Get the PCI device provider
    ivars->pciDevice = OSDynamicCast(IOPCIDevice, provider);
    if (ivars->pciDevice == nullptr) {
        os_log(OS_LOG_DEFAULT, "XDMADriver: Provider is not IOPCIDevice");
        return kIOReturnError;
    }
    ivars->pciDevice->retain();

    // Open the PCI device
    ret = ivars->pciDevice->Open(this, 0);
    if (ret != kIOReturnSuccess) {
        os_log(OS_LOG_DEFAULT, "XDMADriver: Failed to open PCI device: 0x%x", ret);
        return ret;
    }

    // Enable memory space and bus mastering for BAR access + DMA
    uint16_t cmdReg;
    ivars->pciDevice->ConfigurationRead16(0x04, &cmdReg);
    cmdReg |= 0x0006;  // Memory space enable | bus master enable
    ivars->pciDevice->ConfigurationWrite16(0x04, cmdReg);

    // Record each memory BAR's mapped length, so a user-client register
    // access is refused past the end of its BAR instead of reaching whatever
    // the PCI bridge answers there. memoryIndex, not BAR index, is what the
    // MemoryRead32/MemoryWrite32 calls take, so the table is keyed by it.
    for (uint8_t i = 0; i < 6; i++) {
        ivars->barSize[i] = 0;
    }
    for (uint8_t bar = 0; bar < 6; bar++) {
        uint8_t memoryIndex = 0;
        uint64_t barSize = 0;
        uint8_t barType = 0;
        if (ivars->pciDevice->GetBARInfo(bar, &memoryIndex, &barSize, &barType) == kIOReturnSuccess && memoryIndex < 6) {
            ivars->barSize[memoryIndex] = barSize;
        }
    }

    // Locate the XDMA config BAR: probe each mapped range for the H2C0
    // engine identifier (identifier reg [31:16] == 0x1fc0). The DAU user
    // BAR carries the register window instead, so it never matches.
    ivars->configBarValid = false;
    for (uint8_t bar = 0; bar < 2 && !ivars->configBarValid; bar++) {
        uint32_t h2c_id = 0xFFFFFFFF;
        uint32_t c2h_id = 0xFFFFFFFF;
        ivars->pciDevice->MemoryRead32(bar, XDMA_H2C_CHANNEL_BASE(0) + ENGINE_ID_REG, &h2c_id);
        ivars->pciDevice->MemoryRead32(bar, XDMA_C2H_CHANNEL_BASE(0) + ENGINE_ID_REG, &c2h_id);
        if ((h2c_id >> 16) == XDMA_ID_H2C && (c2h_id >> 16) == XDMA_ID_C2H) {
            ivars->configBarIndex = bar;
            ivars->configBarValid = true;
            os_log(OS_LOG_DEFAULT, "XDMADriver: XDMA config BAR = memoryIndex %u (H2C0 id 0x%x, C2H0 id 0x%x)",
                   bar, h2c_id, c2h_id);
        }
    }
    if (!ivars->configBarValid) {
        os_log(OS_LOG_DEFAULT, "XDMADriver: no XDMA config BAR found; DMA disabled");
    }

    // Allocate the DMA descriptor ring and pin its device address
    uint64_t descriptorRingSize = XDMA_RING_DESC_COUNT * sizeof(XDMADescriptor);
    ret = IOBufferMemoryDescriptor::Create(kIOMemoryDirectionInOut,
                                           descriptorRingSize,
                                           64,
                                           &ivars->dmaDescriptorRing);
    if (ret != kIOReturnSuccess || ivars->dmaDescriptorRing == nullptr) {
        os_log(OS_LOG_DEFAULT, "XDMADriver: Failed to allocate DMA descriptor ring: 0x%x", ret);
    } else {
        IOAddressSegment ringRange = {};
        ivars->dmaDescriptorRing->GetAddressRange(&ringRange);
        ivars->ringCPUAddress = ringRange.address;

        IODMACommandSpecification spec = {};
        spec.options = kIODMACommandSpecificationNoOptions;
        spec.maxAddressBits = 64;
        ret = IODMACommand::Create(ivars->pciDevice, kIODMACommandCreateNoOptions, &spec, &ivars->ringDMACommand);
        if (ret == kIOReturnSuccess) {
            uint64_t dmaFlags = 0;
            uint32_t segCount = 1;
            IOAddressSegment seg = {};
            ret = ivars->ringDMACommand->PrepareForDMA(kIODMACommandPrepareForDMANoOptions,
                                                       ivars->dmaDescriptorRing,
                                                       0, descriptorRingSize,
                                                       &dmaFlags, &segCount, &seg);
            if (ret == kIOReturnSuccess && segCount == 1) {
                ivars->ringDeviceAddress = seg.address;
                os_log(OS_LOG_DEFAULT, "XDMADriver: DMA descriptor ring ready (%u descriptors)", XDMA_RING_DESC_COUNT);
            } else {
                os_log(OS_LOG_DEFAULT, "XDMADriver: ring PrepareForDMA failed: 0x%x segs=%u", ret, segCount);
                OSSafeReleaseNULL(ivars->ringDMACommand);
            }
        } else {
            os_log(OS_LOG_DEFAULT, "XDMADriver: ring IODMACommand::Create failed: 0x%x", ret);
        }
    }

    ret = SetName(XDMA_SERVICE_NAME);
    if (ret != kIOReturnSuccess) {
        os_log(OS_LOG_DEFAULT, "XDMADriver: SetName(%s) failed: 0x%x", XDMA_SERVICE_NAME, ret);
        return ret;
    }

    // Register the service so UserClient can find us
    ret = RegisterService();

    os_log(OS_LOG_DEFAULT, "XDMADriver::Start() complete, ret = 0x%x", ret);
    return ret;
}

kern_return_t IMPL(XDMADriver, NewUserClient)
{
    if (userClient == nullptr) {
        return kIOReturnBadArgument;
    }

    *userClient = nullptr;
    if (type != 0) {
        return kIOReturnBadArgument;
    }

    IOService *createdService = nullptr;
    kern_return_t ret = Create(this, XDMA_USER_CLIENT_PROPERTIES, &createdService);
    if (ret != kIOReturnSuccess) {
        os_log(OS_LOG_DEFAULT, "XDMADriver: Create(%s) failed: 0x%x", XDMA_USER_CLIENT_PROPERTIES, ret);
        return ret;
    }

    XDMAUserClient *createdUserClient = OSDynamicCast(XDMAUserClient, createdService);
    if (createdUserClient == nullptr) {
        OSSafeReleaseNULL(createdService);
        os_log(OS_LOG_DEFAULT, "XDMADriver: Created service is not XDMAUserClient");
        return kIOReturnError;
    }

    *userClient = createdUserClient;
    return kIOReturnSuccess;
}

kern_return_t IMPL(XDMADriver, Stop)
{
    os_log(OS_LOG_DEFAULT, "XDMADriver::Stop()");

    if (ivars->ringDMACommand != nullptr) {
        ivars->ringDMACommand->CompleteDMA(kIODMACommandCompleteDMANoOptions);
        OSSafeReleaseNULL(ivars->ringDMACommand);
    }
    OSSafeReleaseNULL(ivars->dmaDescriptorRing);

    if (ivars->pciDevice != nullptr) {
        ivars->pciDevice->Close(this, 0);
        OSSafeReleaseNULL(ivars->pciDevice);
    }

    return Stop(provider, SUPERDISPATCH);
}

void XDMADriver::free()
{
    os_log(OS_LOG_DEFAULT, "XDMADriver::free()");
    IOSafeDeleteNULL(ivars, XDMADriver_IVars, 1);
    super::free();
}

kern_return_t XDMADriver::ReadBAR(uint32_t bar_index, uint64_t offset, uint32_t *value)
{
    if (ivars->pciDevice == nullptr || value == nullptr) {
        return kIOReturnBadArgument;
    }
    if (bar_index >= 6) {
        return kIOReturnBadArgument;
    }
    if (offset & 3) {
        return kIOReturnNotAligned;
    }
    if (ivars->barSize[bar_index] == 0 || offset > ivars->barSize[bar_index] - 4) {
        return kIOReturnBadArgument;
    }

    ivars->pciDevice->MemoryRead32(bar_index, offset, value);
    return kIOReturnSuccess;
}

kern_return_t XDMADriver::WriteBAR(uint32_t bar_index, uint64_t offset, uint32_t value)
{
    if (ivars->pciDevice == nullptr) {
        return kIOReturnBadArgument;
    }
    if (bar_index >= 6) {
        return kIOReturnBadArgument;
    }
    if (offset & 3) {
        return kIOReturnNotAligned;
    }
    if (ivars->barSize[bar_index] == 0 || offset > ivars->barSize[bar_index] - 4) {
        return kIOReturnBadArgument;
    }

    ivars->pciDevice->MemoryWrite32(bar_index, offset, value);
    return kIOReturnSuccess;
}

kern_return_t XDMADriver::GetDeviceInfo(uint16_t *vendor, uint16_t *device,
                                        uint16_t *subsystem_vendor, uint16_t *subsystem_device)
{
    if (ivars->pciDevice == nullptr) {
        return kIOReturnError;
    }

    ivars->pciDevice->ConfigurationRead16(0x00, vendor);
    ivars->pciDevice->ConfigurationRead16(0x02, device);
    ivars->pciDevice->ConfigurationRead16(0x2C, subsystem_vendor);
    ivars->pciDevice->ConfigurationRead16(0x2E, subsystem_device);

    return kIOReturnSuccess;
}

kern_return_t XDMADriver::DMATransfer(bool hostToCard, uint64_t device_offset, uint64_t length,
                                      IOUserClientMethodArguments *arguments)
{
    kern_return_t ret;

    if (ivars->pciDevice == nullptr || arguments == nullptr) {
        return kIOReturnBadArgument;
    }
    if (!ivars->configBarValid || ivars->ringDMACommand == nullptr) {
        return kIOReturnNoDevice;
    }
    if (length == 0 || length > XDMA_MAX_TRANSFER_BYTES) {
        return kIOReturnBadArgument;
    }

    // Bounce buffer: uniform path for OSData and descriptor-carried user buffers
    IOBufferMemoryDescriptor *bounce = nullptr;
    ret = IOBufferMemoryDescriptor::Create(kIOMemoryDirectionInOut, length, 64, &bounce);
    if (ret != kIOReturnSuccess || bounce == nullptr) {
        return kIOReturnNoMemory;
    }
    IOAddressSegment bounceRange = {};
    bounce->GetAddressRange(&bounceRange);
    uint8_t *bounceBytes = (uint8_t *)bounceRange.address;

    if (hostToCard) {
        if (arguments->structureInput != nullptr) {
            size_t inLen = arguments->structureInput->getLength();
            if (inLen != length) {
                OSSafeReleaseNULL(bounce);
                return kIOReturnBadArgument;
            }
            memcpy(bounceBytes, arguments->structureInput->getBytesNoCopy(), length);
        } else if (arguments->structureInputDescriptor != nullptr) {
            IOMemoryMap *inMap = nullptr;
            ret = arguments->structureInputDescriptor->CreateMapping(0, 0, 0, 0, 0, &inMap);
            if (ret != kIOReturnSuccess || inMap == nullptr) {
                OSSafeReleaseNULL(bounce);
                return kIOReturnVMError;
            }
            memcpy(bounceBytes, (const void *)inMap->GetAddress(), length);
            OSSafeReleaseNULL(inMap);
        } else {
            OSSafeReleaseNULL(bounce);
            return kIOReturnBadArgument;
        }
    }

    // Map the bounce buffer for device access
    IODMACommandSpecification spec = {};
    spec.options = kIODMACommandSpecificationNoOptions;
    spec.maxAddressBits = 64;
    IODMACommand *cmd = nullptr;
    ret = IODMACommand::Create(ivars->pciDevice, kIODMACommandCreateNoOptions, &spec, &cmd);
    if (ret != kIOReturnSuccess || cmd == nullptr) {
        OSSafeReleaseNULL(bounce);
        return kIOReturnNoMemory;
    }
    uint64_t dmaFlags = 0;
    uint32_t segCount = XDMA_RING_DESC_COUNT;
    IOAddressSegment segs[XDMA_RING_DESC_COUNT] = {};
    ret = cmd->PrepareForDMA(kIODMACommandPrepareForDMANoOptions, bounce, 0, length,
                             &dmaFlags, &segCount, segs);
    if (ret != kIOReturnSuccess || segCount == 0 || segCount > XDMA_RING_DESC_COUNT) {
        os_log(OS_LOG_DEFAULT, "XDMADriver: PrepareForDMA failed: 0x%x segs=%u", ret, segCount);
        OSSafeReleaseNULL(cmd);
        OSSafeReleaseNULL(bounce);
        return kIOReturnDMAError;
    }

    // Build the descriptor chain
    XDMADescriptor *ring = (XDMADescriptor *)ivars->ringCPUAddress;
    uint64_t dev = device_offset;
    for (uint32_t i = 0; i < segCount; i++) {
        bool last = (i == segCount - 1);
        uint64_t src = hostToCard ? segs[i].address : dev;
        uint64_t dst = hostToCard ? dev : segs[i].address;
        ring[i].control = (uint32_t)(DESC_MAGIC | (last ? DESC_CONTROL_STOP : 0));
        ring[i].bytes = (uint32_t)segs[i].length;
        ring[i].src_addr_lo = (uint32_t)src;
        ring[i].src_addr_hi = (uint32_t)(src >> 32);
        ring[i].dst_addr_lo = (uint32_t)dst;
        ring[i].dst_addr_hi = (uint32_t)(dst >> 32);
        uint64_t next = last ? 0 : ivars->ringDeviceAddress + ((uint64_t)(i + 1) * sizeof(XDMADescriptor));
        ring[i].next_lo = (uint32_t)next;
        ring[i].next_hi = (uint32_t)(next >> 32);
        dev += segs[i].length;
    }
    __sync_synchronize();

    // Program and start the engine
    uint8_t bar = ivars->configBarIndex;
    uint64_t chBase = hostToCard ? XDMA_H2C_CHANNEL_BASE(0) : XDMA_C2H_CHANNEL_BASE(0);
    uint64_t sgBase = hostToCard ? XDMA_H2C_SGDMA_BASE(0) : XDMA_C2H_SGDMA_BASE(0);
    uint32_t status = 0;
    ivars->pciDevice->MemoryRead32(bar, chBase + XDMA_ENGINE_STATUS_RC, &status);  // read-clear
    ivars->pciDevice->MemoryWrite32(bar, sgBase + XDMA_SGDMA_DESC_LO, (uint32_t)ivars->ringDeviceAddress);
    ivars->pciDevice->MemoryWrite32(bar, sgBase + XDMA_SGDMA_DESC_HI, (uint32_t)(ivars->ringDeviceAddress >> 32));
    ivars->pciDevice->MemoryWrite32(bar, sgBase + XDMA_SGDMA_DESC_ADJACENT, segCount - 1);
    ivars->pciDevice->MemoryWrite32(bar, chBase + XDMA_ENGINE_CONTROL, XDMA_CTRL_RUN_STOP);

    // Poll for completion
    uint32_t elapsed_ms = 0;
    for (;;) {
        ivars->pciDevice->MemoryRead32(bar, chBase + XDMA_ENGINE_STATUS, &status);
        if (!(status & XDMA_STAT_BUSY)) {
            break;
        }
        if (elapsed_ms >= XDMA_POLL_TIMEOUT_MS) {
            break;
        }
        IOSleep(1);
        elapsed_ms++;
    }
    ivars->pciDevice->MemoryWrite32(bar, chBase + XDMA_ENGINE_CONTROL, 0);  // stop

    uint32_t completed = 0;
    ivars->pciDevice->MemoryRead32(bar, chBase + XDMA_ENGINE_COMPLETED_DESC, &completed);
    bool ok = !(status & XDMA_STAT_BUSY) && !(status & XDMA_STAT_ERROR_MASK);
    if (!ok) {
        os_log(OS_LOG_DEFAULT, "XDMADriver: DMA %s failed status=0x%x completed=%u/%u len=%llu",
               hostToCard ? "H2C" : "C2H", status, completed, segCount, length);
    }

    if (ok && !hostToCard) {
        __sync_synchronize();
        if (arguments->structureOutputDescriptor != nullptr) {
            IOMemoryMap *outMap = nullptr;
            ret = arguments->structureOutputDescriptor->CreateMapping(0, 0, 0, 0, 0, &outMap);
            if (ret == kIOReturnSuccess && outMap != nullptr) {
                memcpy((void *)outMap->GetAddress(), bounceBytes, length);
                OSSafeReleaseNULL(outMap);
            } else {
                ok = false;
            }
        } else {
            arguments->structureOutput = OSData::withBytes(bounceBytes, (size_t)length);
            if (arguments->structureOutput == nullptr) {
                ok = false;
            }
        }
    }

    cmd->CompleteDMA(kIODMACommandCompleteDMANoOptions);
    OSSafeReleaseNULL(cmd);
    OSSafeReleaseNULL(bounce);
    return ok ? kIOReturnSuccess : kIOReturnIOError;
}
