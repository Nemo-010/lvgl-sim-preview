/*
 * lvgl_preview_bottom.h - host-side half of the preview transport.
 *
 * SPDX-License-Identifier: Apache-2.0
 *
 * This header is included from two contexts:
 *   - the embedded side, which only ever sees opaque handles
 *   - the native simulator runner side, which owns the real mappings
 *
 * The split mirrors Zephyr's own "bottom" drivers (display_sdl_bottom.h,
 * can_native_linux_adapt.h): the embedded side must not be exposed to host
 * headers, so every function here takes and returns plain integers.
 */

#ifndef LVGL_PREVIEW_BOTTOM_H_
#define LVGL_PREVIEW_BOTTOM_H_

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/*
 * Frame transport.
 *
 * `lvp_host_map_frame` returns an opaque handle. The embedded side keeps that
 * handle and passes it back on every publish. `header` is the device's view of
 * the frame header, `pixels` the device's view of the pixel area; both are
 * valid until the handle is unmapped.
 */
int lvp_host_map_frame(const char *path, uint32_t pixel_capacity, void **handle,
		       void **header, void **pixels);

void lvp_host_unmap_frame(void *handle);

/*
 * Input transport. The ring lives in a file so that a host that is not this
 * process (a viewer, a script, a CI step) can also feed events.
 */
int lvp_host_map_input(const char *path, void **handle, void **ring);

void lvp_host_unmap_input(void *handle);

/*
 * Device manifest.
 *
 * The manifest is produced from devicetree at startup. It is written by the
 * embedded side because only it can see the devicetree, but it is written
 * through the host side so that the embedded side does not need host headers.
 */
int lvp_host_write_manifest(const char *path, const char *text, uint32_t length);

#ifdef __cplusplus
}
#endif

#endif /* LVGL_PREVIEW_BOTTOM_H_ */
