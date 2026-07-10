/*
 * XDMADMAEngine.cpp
 * DMA engine management implementation
 *
 * This file provides utility functions for DMA operations.
 * The actual DMA operations are implemented in XDMADriver.cpp.
 */

#include "XDMADMAEngine.h"

/*
 * This file primarily serves as documentation for the XDMA descriptor
 * format and engine register layout. The actual DMA transfer logic
 * is implemented in XDMADriver::DMAWrite() and XDMADriver::DMARead().
 *
 * Reference implementation (from libxdma.c):
 *
 * For H2C (Host-to-Card) transfer:
 * 1. Build descriptor with:
 *    - src = host physical address
 *    - dst = device offset
 *    - bytes = transfer size
 *    - control = DESC_MAGIC
 * 2. Write descriptor address to H2C engine SGDMA registers
 * 3. Start engine by setting XDMA_CTRL_RUN_STOP
 * 4. Poll status until XDMA_STAT_BUSY is clear
 *
 * For C2H (Card-to-Host) transfer:
 * 1. Build descriptor with:
 *    - src = device offset
 *    - dst = host physical address
 *    - bytes = transfer size
 *    - control = DESC_MAGIC
 * 2. Write descriptor address to C2H engine SGDMA registers
 * 3. Start engine by setting XDMA_CTRL_RUN_STOP
 * 4. Poll status until XDMA_STAT_BUSY is clear
 */
