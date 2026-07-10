/*
 * XDMADMAEngine.h
 * DMA engine management for XDMA driver
 *
 * Handles XDMA descriptor format, engine programming, and transfer operations.
 */

#ifndef XDMADMAEngine_h
#define XDMADMAEngine_h

#include <stdint.h>

// XDMA Descriptor format (32 bytes, from libxdma.h)
#pragma pack(push, 1)
struct XDMADescriptor {
    uint32_t control;       // Magic (0xAD4B) | stop | completed | count
    uint32_t bytes;         // Transfer length in bytes
    uint32_t src_addr_lo;   // Source address low 32 bits
    uint32_t src_addr_hi;   // Source address high 32 bits
    uint32_t dst_addr_lo;   // Destination address low 32 bits
    uint32_t dst_addr_hi;   // Destination address high 32 bits
    uint32_t next_lo;       // Next descriptor address low 32 bits
    uint32_t next_hi;       // Next descriptor address high 32 bits
};
#pragma pack(pop)

// XDMA Engine register offsets (relative to channel base)
#define XDMA_ENGINE_IDENTIFIER      0x00
#define XDMA_ENGINE_CONTROL         0x04
#define XDMA_ENGINE_CONTROL_W1S     0x08
#define XDMA_ENGINE_CONTROL_W1C     0x0C
#define XDMA_ENGINE_STATUS          0x40
#define XDMA_ENGINE_STATUS_RC       0x44
#define XDMA_ENGINE_COMPLETED_DESC  0x48
#define XDMA_ENGINE_ALIGNMENTS      0x4C

// Descriptor address registers
#define XDMA_SGDMA_DESC_LO          0x80
#define XDMA_SGDMA_DESC_HI          0x84
#define XDMA_SGDMA_DESC_ADJACENT    0x88
#define XDMA_SGDMA_DESC_CREDITS     0x8C

// Performance monitoring
#define XDMA_PERF_CTRL              0xC0
#define XDMA_PERF_CYC_LO            0xC4
#define XDMA_PERF_CYC_HI            0xC8
#define XDMA_PERF_DAT_LO            0xCC
#define XDMA_PERF_DAT_HI            0xD0

// H2C and C2H channel base addresses
#define XDMA_H2C_CHANNEL_OFFSET(n)  (0x0000 + (0x100 * (n)))
#define XDMA_C2H_CHANNEL_OFFSET(n)  (0x1000 + (0x100 * (n)))

// Control register bits
#define XDMA_CTRL_RUN_STOP                  (1UL << 0)
#define XDMA_CTRL_IE_DESC_STOPPED           (1UL << 1)
#define XDMA_CTRL_IE_DESC_COMPLETED         (1UL << 2)
#define XDMA_CTRL_IE_DESC_ALIGN_MISMATCH    (1UL << 3)
#define XDMA_CTRL_IE_MAGIC_STOPPED          (1UL << 4)
#define XDMA_CTRL_IE_IDLE_STOPPED           (1UL << 6)
#define XDMA_CTRL_IE_READ_ERROR             (0x1FUL << 9)
#define XDMA_CTRL_IE_DESC_ERROR             (0x1FUL << 19)
#define XDMA_CTRL_NON_INCR_ADDR             (1UL << 25)
#define XDMA_CTRL_POLL_MODE_WB              (1UL << 26)

// Status register bits
#define XDMA_STAT_BUSY              (1UL << 0)
#define XDMA_STAT_DESC_STOPPED      (1UL << 1)
#define XDMA_STAT_DESC_COMPLETED    (1UL << 2)
#define XDMA_STAT_ALIGN_MISMATCH    (1UL << 3)
#define XDMA_STAT_MAGIC_STOPPED     (1UL << 4)
#define XDMA_STAT_INVALID_LEN       (1UL << 5)
#define XDMA_STAT_IDLE_STOPPED      (1UL << 6)

// Descriptor control field bits
#define DESC_MAGIC                  0xAD4B0000UL
#define DESC_CONTROL_STOP           (1UL << 0)
#define DESC_CONTROL_COMPLETED      (1UL << 1)

// Maximum descriptor count
#define XDMA_MAX_DESC_COUNT         0x800

// Maximum bytes per descriptor
#define XDMA_DESC_BLEN_MAX          ((1 << 28) - 1)

/*
 * DMA Transfer Flow:
 * 1. Allocate descriptor ring in DMA-coherent memory
 * 2. For each data chunk, fill a descriptor:
 *    - Set src (host physical addr for H2C, device addr for C2H)
 *    - Set dst (device addr for H2C, host physical addr for C2H)
 *    - Set byte count
 * 3. Chain descriptors via next_lo/hi
 * 4. Write first descriptor address to engine's SGDMA descriptor register
 * 5. Set engine control register to start (XDMA_CTRL_RUN_STOP)
 * 6. Wait for completion (poll status register or wait for interrupt)
 * 7. Check status for errors
 */

#endif /* XDMADMAEngine_h */
