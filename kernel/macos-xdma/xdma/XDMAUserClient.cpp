/*
 * XDMAUserClient.cpp
 * IOUserClient implementation for XDMA driver
 */

#include <os/log.h>
#include <DriverKit/IOLib.h>
#include <DriverKit/IOUserServer.h>
#include <DriverKit/IOBufferMemoryDescriptor.h>
#include <DriverKit/IOMemoryDescriptor.h>
#include <DriverKit/OSData.h>

#include "XDMAUserClient.h"
#include "XDMADriver.h"

// External method selectors
enum {
    kMethodReadBAR = 0,
    kMethodWriteBAR = 1,
    kMethodDMAWrite = 2,
    kMethodDMARead = 3,
    kMethodGetInfo = 4,
    kNumMethods
};

struct XDMAUserClient_IVars {
    XDMADriver *driver;
};

// Forward declarations of static dispatch functions
static kern_return_t StaticReadBAR(OSObject *target, void *reference,
                                   IOUserClientMethodArguments *arguments);
static kern_return_t StaticWriteBAR(OSObject *target, void *reference,
                                    IOUserClientMethodArguments *arguments);
static kern_return_t StaticDMAWrite(OSObject *target, void *reference,
                                    IOUserClientMethodArguments *arguments);
static kern_return_t StaticDMARead(OSObject *target, void *reference,
                                   IOUserClientMethodArguments *arguments);
static kern_return_t StaticGetInfo(OSObject *target, void *reference,
                                   IOUserClientMethodArguments *arguments);

// Method dispatch table
static const IOUserClientMethodDispatch sMethodDispatch[kNumMethods] = {
    /* kMethodReadBAR */ {
        .function = StaticReadBAR,
        .checkCompletionExists = false,
        .checkScalarInputCount = 2,
        .checkStructureInputSize = 0,
        .checkScalarOutputCount = 1,
        .checkStructureOutputSize = 0,
    },
    /* kMethodWriteBAR */ {
        .function = StaticWriteBAR,
        .checkCompletionExists = false,
        .checkScalarInputCount = 3,
        .checkStructureInputSize = 0,
        .checkScalarOutputCount = 0,
        .checkStructureOutputSize = 0,
    },
    /* kMethodDMAWrite */ {
        .function = StaticDMAWrite,
        .checkCompletionExists = false,
        .checkScalarInputCount = 1,
        .checkStructureInputSize = kIOUserClientVariableStructureSize,
        .checkScalarOutputCount = 0,
        .checkStructureOutputSize = 0,
    },
    /* kMethodDMARead */ {
        .function = StaticDMARead,
        .checkCompletionExists = false,
        .checkScalarInputCount = 2,
        .checkStructureInputSize = 0,
        .checkScalarOutputCount = 0,
        .checkStructureOutputSize = kIOUserClientVariableStructureSize,
    },
    /* kMethodGetInfo */ {
        .function = StaticGetInfo,
        .checkCompletionExists = false,
        .checkScalarInputCount = 0,
        .checkStructureInputSize = 0,
        .checkScalarOutputCount = 4,
        .checkStructureOutputSize = 0,
    }
};

bool XDMAUserClient::init()
{
    if (!super::init()) {
        return false;
    }

    ivars = IONewZero(XDMAUserClient_IVars, 1);
    if (ivars == nullptr) {
        return false;
    }

    os_log(OS_LOG_DEFAULT, "XDMAUserClient::init()");
    return true;
}

kern_return_t IMPL(XDMAUserClient, Start)
{
    kern_return_t ret;

    os_log(OS_LOG_DEFAULT, "XDMAUserClient::Start()");

    ret = Start(provider, SUPERDISPATCH);
    if (ret != kIOReturnSuccess) {
        return ret;
    }

    ivars->driver = OSDynamicCast(XDMADriver, provider);
    if (ivars->driver == nullptr) {
        os_log(OS_LOG_DEFAULT, "XDMAUserClient: Provider is not XDMADriver");
        return kIOReturnError;
    }
    ivars->driver->retain();

    return kIOReturnSuccess;
}

kern_return_t IMPL(XDMAUserClient, Stop)
{
    os_log(OS_LOG_DEFAULT, "XDMAUserClient::Stop()");

    OSSafeReleaseNULL(ivars->driver);

    return Stop(provider, SUPERDISPATCH);
}

void XDMAUserClient::free()
{
    os_log(OS_LOG_DEFAULT, "XDMAUserClient::free()");
    IOSafeDeleteNULL(ivars, XDMAUserClient_IVars, 1);
    super::free();
}

kern_return_t XDMAUserClient::ExternalMethod(uint64_t selector,
                                              IOUserClientMethodArguments *arguments,
                                              const IOUserClientMethodDispatch *dispatch,
                                              OSObject *target,
                                              void *reference)
{
    if (selector >= kNumMethods) {
        return kIOReturnBadArgument;
    }

    return super::ExternalMethod(selector, arguments,
                                 &sMethodDispatch[selector], this, nullptr);
}

// Static dispatch functions

static kern_return_t StaticReadBAR(OSObject *target, void *reference,
                                   IOUserClientMethodArguments *arguments)
{
    XDMAUserClient *me = OSDynamicCast(XDMAUserClient, target);
    if (me == nullptr || me->ivars->driver == nullptr) {
        return kIOReturnError;
    }

    uint32_t bar_index = (uint32_t)arguments->scalarInput[0];
    uint64_t offset = arguments->scalarInput[1];
    uint32_t value = 0;

    kern_return_t ret = me->ivars->driver->ReadBAR(bar_index, offset, &value);
    if (ret == kIOReturnSuccess) {
        arguments->scalarOutput[0] = value;
    }

    return ret;
}

static kern_return_t StaticWriteBAR(OSObject *target, void *reference,
                                    IOUserClientMethodArguments *arguments)
{
    XDMAUserClient *me = OSDynamicCast(XDMAUserClient, target);
    if (me == nullptr || me->ivars->driver == nullptr) {
        return kIOReturnError;
    }

    uint32_t bar_index = (uint32_t)arguments->scalarInput[0];
    uint64_t offset = arguments->scalarInput[1];
    uint32_t value = (uint32_t)arguments->scalarInput[2];

    return me->ivars->driver->WriteBAR(bar_index, offset, value);
}

static kern_return_t StaticDMAWrite(OSObject *target, void *reference,
                                    IOUserClientMethodArguments *arguments)
{
    XDMAUserClient *me = OSDynamicCast(XDMAUserClient, target);
    if (me == nullptr || me->ivars->driver == nullptr) {
        return kIOReturnError;
    }

    uint64_t device_offset = arguments->scalarInput[0];
    uint64_t length = 0;
    if (arguments->structureInput != nullptr) {
        length = arguments->structureInput->getLength();
    } else if (arguments->structureInputDescriptor != nullptr) {
        arguments->structureInputDescriptor->GetLength(&length);
    } else {
        return kIOReturnBadArgument;
    }

    return me->ivars->driver->DMATransfer(true, device_offset, length, arguments);
}

static kern_return_t StaticDMARead(OSObject *target, void *reference,
                                   IOUserClientMethodArguments *arguments)
{
    XDMAUserClient *me = OSDynamicCast(XDMAUserClient, target);
    if (me == nullptr || me->ivars->driver == nullptr) {
        return kIOReturnError;
    }

    uint64_t device_offset = arguments->scalarInput[0];
    uint64_t length = arguments->scalarInput[1];

    return me->ivars->driver->DMATransfer(false, device_offset, length, arguments);
}

static kern_return_t StaticGetInfo(OSObject *target, void *reference,
                                   IOUserClientMethodArguments *arguments)
{
    XDMAUserClient *me = OSDynamicCast(XDMAUserClient, target);
    if (me == nullptr || me->ivars->driver == nullptr) {
        return kIOReturnError;
    }

    uint16_t vendor, device, subsystem_vendor, subsystem_device;

    kern_return_t ret = me->ivars->driver->GetDeviceInfo(&vendor, &device,
                                                         &subsystem_vendor, &subsystem_device);
    if (ret == kIOReturnSuccess) {
        arguments->scalarOutput[0] = vendor;
        arguments->scalarOutput[1] = device;
        arguments->scalarOutput[2] = subsystem_vendor;
        arguments->scalarOutput[3] = subsystem_device;
    }

    return ret;
}
