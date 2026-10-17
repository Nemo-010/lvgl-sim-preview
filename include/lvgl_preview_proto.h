/*
 * lvgl_preview_proto.h - shared frame protocol between the Zephyr preview
 * module (device side) and the Qt preview host (desktop side).
 *
 * This header is deliberately free of Zephyr, LVGL and Qt dependencies so it
 * can be included from both the RTOS side and the host side, and from C++.
 *
 * SPDX-License-Identifier: Apache-2.0
 */

#ifndef LVGL_PREVIEW_PROTO_H_
#define LVGL_PREVIEW_PROTO_H_

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define LVP_MAGIC       0x3150564cU /* "LVP1" */
#define LVP_VERSION     2U

/*
 * Canonical pixel formats published in the frame header.
 *
 * These are intentionally our own stable identifiers rather than LVGL's
 * LV_COLOR_FORMAT_* values: the preview host must be able to decode captures
 * produced by any LVGL revision, so the wire format cannot depend on an enum
 * that LVGL is free to renumber.
 */
enum lvp_format {
	LVP_FMT_UNKNOWN = 0,
	LVP_FMT_I1 = 1,          /* 1bpp, MSB-first, row-major, 8px per byte */
	LVP_FMT_L8 = 2,          /* 8bpp luminance */
	LVP_FMT_AL88 = 3,        /* 8bpp luminance + 8bpp alpha, little-endian pair */
	LVP_FMT_RGB565 = 4,      /* 16bpp, native endian */
	LVP_FMT_RGB565_SWAPPED = 5, /* 16bpp, byte swapped */
	LVP_FMT_RGB888 = 6,      /* 24bpp, BGR byte order (LVGL RGB888) */
	LVP_FMT_ARGB8888 = 7,    /* 32bpp */
	LVP_FMT_XRGB8888 = 8,    /* 32bpp, alpha ignored */
};

/* Frame flags. */
#define LVP_FLAG_MONO_VTILED      (1U << 0) /* panel packs 8 vertical pixels per byte */
#define LVP_FLAG_MONO_MSB_FIRST   (1U << 1) /* panel treats bit 7 as the first pixel */
#define LVP_FLAG_MONO_INVERTED    (1U << 2) /* panel inverts monochrome pixels in hw */
#define LVP_FLAG_TRUNCATED        (1U << 3) /* frame was cut short by the sink */
#define LVP_FLAG_PARTIAL          (1U << 4) /* only the dirty rectangle is valid */
/*
 * A set bit means a lit (white) pixel. When clear, a set bit means black.
 *
 * This is not derivable from the format alone: the same panel can be driven in
 * MONO01 or MONO10, and LVGL writes the opposite bit in each case. The sink
 * resolves it and states the answer, so the host never has to guess.
 */
#define LVP_FLAG_MONO_ONE_IS_WHITE (1U << 5)

/* Header flags. */
#define LVP_HDR_FLAG_DISPLAY_ON   (1U << 0) /* blanking has been turned off */

/*
 * Frame header.
 *
 * Layout rules, so that the host can validate what it maps:
 *   - the header is the first `header_size` bytes of the capture file
 *   - pixel data starts at offset `header_size`
 *   - `pixel_capacity` is the number of bytes reserved for pixels
 *   - `frame_size` is how many of those bytes are valid for this frame
 *   - `seq` is written last and read first; the host must treat a frame as
 *     stable only when two consecutive reads of `seq` agree
 *
 * All multi-byte fields are little-endian.
 */
struct lvp_frame_header {
	uint32_t magic;        /* LVP_MAGIC */
	uint32_t version;      /* LVP_VERSION */
	uint32_t header_size;  /* sizeof(struct lvp_frame_header) */
	uint32_t seq;          /* incremented once per published frame */
	uint32_t width;        /* visible panel width in pixels */
	uint32_t height;       /* visible panel height in pixels */
	uint32_t format;       /* enum lvp_format */
	uint32_t stride;       /* bytes per row of the pixel buffer */
	uint32_t frame_size;   /* valid bytes in the pixel buffer */
	uint32_t pixel_capacity; /* bytes reserved after the header */
	uint32_t flags;        /* LVP_FLAG_* */
	uint32_t hdr_flags;    /* LVP_HDR_FLAG_* */
	uint32_t dirty_x1;
	uint32_t dirty_y1;
	uint32_t dirty_x2;     /* inclusive */
	uint32_t dirty_y2;     /* inclusive */
	uint32_t flushes;      /* total flush callbacks observed */
	uint32_t frames;       /* total frames published */
	uint32_t last_flush_us; /* duration of the last composite, microseconds */
	uint32_t tick_ms;      /* k_uptime at publish time */
	uint32_t heartbeat_ms; /* updated even when no frame is published */
	uint32_t app_ready;    /* 1 once the preview module finished init */
	uint32_t reserved[8];
	char display_name[32]; /* LVGL display name / Zephyr device name */
	char device_name[64];  /* Zephyr display device name */
	char app_name[64];     /* build-provided application name */
};

/*
 * Input events travel the other way: the host writes them into a second
 * mapping that the device side polls. The two directions never share memory.
 */
#define LVP_INPUT_MAGIC 0x3150494cU /* "LIP1" */

enum lvp_input_type {
	LVP_INPUT_NONE = 0,
	LVP_INPUT_POINTER = 1, /* absolute press/release at x,y */
	LVP_INPUT_KEY = 2,     /* key press/release */
	LVP_INPUT_ENCODER = 3, /* relative rotation + optional button */
};

struct lvp_input_event {
	uint32_t type;  /* enum lvp_input_type */
	uint32_t seq;   /* host-side sequence number */
	int32_t x;      /* pointer position, in panel pixels */
	int32_t y;
	uint32_t code;  /* key code, for LVP_INPUT_KEY */
	uint32_t value; /* 0 = release, 1 = press; encoder delta for LVP_INPUT_ENCODER */
};

_Static_assert(sizeof(struct lvp_input_event) == 24,
	       "the host packs this as <IIiiII>; keep the two in step");

struct lvp_input_header {
	uint32_t magic;   /* LVP_INPUT_MAGIC */
	uint32_t version; /* LVP_VERSION */
	uint32_t header_size;
	uint32_t head;    /* write index, advanced by the host */
	uint32_t tail;    /* read index, advanced by the device */
	uint32_t capacity; /* number of slots in the ring */
	uint32_t reserved[4];
	struct lvp_input_event ring[];
};

#define LVP_INPUT_CAPACITY 64U

#define LVP_INPUT_FILE_SIZE \
	(sizeof(struct lvp_input_header) + (LVP_INPUT_CAPACITY * sizeof(struct lvp_input_event)))

#ifdef __cplusplus
}
#endif

#endif /* LVGL_PREVIEW_PROTO_H_ */
