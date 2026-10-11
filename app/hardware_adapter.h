#ifndef STUDIO_HARDWARE_ADAPTER_H
#define STUDIO_HARDWARE_ADAPTER_H
#include <stdint.h>
/* Studio adapter ABI, NOT MORPHS SDK definitions. An adapter built using the
 * licensed vendor headers must translate the real SDK calls into this ABI.
 * All functions use cdecl. Open/capture return 0 on success. Capture blocks up
 * to timeout_ms, writes exactly width*height bytes, and never retains pixels.
 * The SDK owns its internal resources; studio_close must release them.
 * Dimensions: 1600x1500, stride 1600, grayscale 8-bit, 500 DPI, white background.
 * Canonical platen orientation: right4 index->little; left4 little->index;
 * thumbs left->right. The adapter must rotate/mirror vendor output if needed.
 */
typedef struct { uint32_t size, width, height, dpi; } StudioInitialization;
typedef struct {
    uint32_t size, width, height, stride, capacity;
    uint8_t *pixels;
} StudioFrame;
#ifdef __cplusplus
extern "C" {
#endif
uint32_t studio_abi_version(void);
int studio_open(const StudioInitialization *config);
int studio_capture(uint32_t fgp, StudioFrame *frame, uint32_t timeout_ms);
void studio_close(void);
#ifdef __cplusplus
}
#endif
#endif
