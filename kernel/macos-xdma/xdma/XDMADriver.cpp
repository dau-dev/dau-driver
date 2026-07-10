/*
 * XDMADriver.cpp
 * macOS DriverKit extension for Xilinx XDMA PCIe devices
 *
 * Implementation of the main driver class.
 */

#include <os/log.h>
#include <DriverKit/IOLib.h>
#include <DriverKit/IOUserServer.h>
#include <DriverKit/IOBufferMemoryDescriptor.h>
#include <PCIDriverKit/IOPCIDevice.h>

#include "XDMADriver.h"
#include "XDMAUserClient.h"
#include "XDMADMAEngine.h"

#define XDMA_SERVICE_NAME "com.dau-dev.xdma"
#define XDMA_USER_CLIENT_PROPERTIES "XDMAUserClientProperties"

// Engine offsets
#define XDMA_H2C_CHANNEL_BASE(n)  (0x0000 + (0x100 * (n)))
#define XDMA_C2H_CHANNEL_BASE(n)  (0x1000 + (0x100 * (n)))

// Engine register offsets (relative to channel base)
#define ENGINE_ID_REG       0x00

struct XDMADriver_IVars {
    IOPCIDevice *pciDevice;
    IOBufferMemoryDescriptor *dmaDescriptorRing;
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

    // Enable bus mastering for DMA
    uint16_t cmdReg;
    ivars->pciDevice->ConfigurationRead16(0x04, &cmdReg);
    cmdReg |= 0x0004;  // Set bus master enable bit
    ivars->pciDevice->ConfigurationWrite16(0x04, cmdReg);

    // Discover XDMA engines
    for (int i = 0; i < 4; i++) {
        uint32_t engine_id = 0;
        uint64_t offset = XDMA_H2C_CHANNEL_BASE(i) + ENGINE_ID_REG;
        ivars->pciDevice->MemoryRead32(0, offset, &engine_id);
        if (engine_id != 0 && engine_id != 0xFFFFFFFF) {
            os_log(OS_LOG_DEFAULT, "XDMADriver: Found H2C engine %d, ID = 0x%x", i, engine_id);
        }
    }
    for (int i = 0; i < 4; i++) {
        uint32_t engine_id = 0;
        uint64_t offset = XDMA_C2H_CHANNEL_BASE(i) + ENGINE_ID_REG;
        ivars->pciDevice->MemoryRead32(0, offset, &engine_id);
        if (engine_id != 0 && engine_id != 0xFFFFFFFF) {
            os_log(OS_LOG_DEFAULT, "XDMADriver: Found C2H engine %d, ID = 0x%x", i, engine_id);
        }
    }

    // Allocate DMA descriptor ring (32 descriptors * 32 bytes each)
    uint64_t descriptorRingSize = 32 * 32;
    ret = IOBufferMemoryDescriptor::Create(kIOMemoryDirectionInOut,
                                           descriptorRingSize,
                                           0,
                                           &ivars->dmaDescriptorRing);
    if (ret != kIOReturnSuccess || ivars->dmaDescriptorRing == nullptr) {
        os_log(OS_LOG_DEFAULT, "XDMADriver: Failed to allocate DMA descriptor ring: 0x%x", ret);
    } else {
        os_log(OS_LOG_DEFAULT, "XDMADriver: DMA descriptor ring allocated, size = %llu", descriptorRingSize);
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

kern_return_t XDMADriver::DMAWrite(uint64_t device_offset, uint64_t length)
{
    os_log(OS_LOG_DEFAULT, "XDMADriver::DMAWrite(offset=0x%llx, length=%llu)", device_offset, length);
    // Full implementation requires descriptor chaining and engine programming
    return kIOReturnUnsupported;
}

kern_return_t XDMADriver::DMARead(uint64_t device_offset, uint64_t length)
{
    os_log(OS_LOG_DEFAULT, "XDMADriver::DMARead(offset=0x%llx, length=%llu)", device_offset, length);
    // Full implementation requires descriptor chaining and engine programming
    return kIOReturnUnsupported;
}
