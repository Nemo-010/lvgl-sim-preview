/*
 * lvgl_preview.c - LVGL preview capture sink (device side).
 *
 * SPDX-License-Identifier: Apache-2.0
 *
 * This is a display driver. It sits in front of the project's real display,
 * captures every buffer LVGL hands to the display API, publishes it to a
 * shared mapping, and forwards the very same bytes to the real panel.
 *
 * Why a display driver and not a hook into LVGL:
 *   - it works for every LVGL revision and every Zephyr LVGL integration,
 *     because it only uses the stable display driver API
 *   - it needs no source change in the project beyond pointing
 *     `zephyr,display` at this node, which the drop-in overlay does
 *   - what it captures is exactly what the panel would have received, so the
 *     preview is evidence about the real hardware path rather than a parallel
 *     rendering path that could disagree with it
 *
 * The panel is found at runtime, by scanning the enabled nodes of the `display`
 * device class for one that is not this sink. That is what lets the overlay
 * stay generic: there is no phandle, width or height to keep in sync with the
 * board, because the sink asks the panel instead of being told about it.
 */

#define DT_DRV_COMPAT lvgl_preview_sink

#include <zephyr/device.h>
#include <zephyr/devicetree.h>
#include <zephyr/drivers/display.h>
#include <zephyr/dt-bindings/display/panel.h>
#include <zephyr/kernel.h>
#include <zephyr/logging/log.h>
#include <zephyr/sys/util.h>
#include <zephyr/version.h>

#include <stdarg.h>
#include <string.h>

#include "lvgl_preview_bottom.h"
#include "lvgl_preview_proto.h"

LOG_MODULE_REGISTER(lvgl_preview, CONFIG_LVGL_PREVIEW_LOG_LEVEL);

/*
 * Diagnostics go through printk rather than the logging subsystem.
 *
 * The applications this sink is pointed at frequently do not enable CONFIG_LOG,
 * in which case LOG_* expands to nothing and a failure to render produces a
 * completely silent run. A preview that fails silently is the one thing this
 * tool exists to prevent, so the messages that explain a blank screen are
 * unconditional.
 */
#define LVP_ERR(fmt, ...)  printk("lvgl-preview: " fmt "\n", ##__VA_ARGS__)
#define LVP_INF(fmt, ...)  printk("lvgl-preview: " fmt "\n", ##__VA_ARGS__)

struct lvp_frame {
	void *handle;
	struct lvp_frame_header *hdr;
	uint8_t *pixels;
	uint32_t capacity;
};

struct lvp_config {
	/* 0 means "ask the panel". */
	uint16_t width;
	uint16_t height;
	/* Describes the panel to emulate when no driver is attached. */
	enum display_pixel_format pixel_format;
	uint32_t screen_info;
	/* Explicit panel, when the overlay named one. */
	const struct device *panel_override;
};

struct lvp_data {
	struct display_capabilities cap;
	const struct device *panel;
	bool blanked;

	struct lvp_frame frame;

	atomic_t flushes;
	atomic_t frames;
	atomic_t last_flush_us;
	atomic_t dirty_x1;
	atomic_t dirty_y1;
	atomic_t dirty_x2;
	atomic_t dirty_y2;
	atomic_t dirty_valid;
	uint32_t last_publish_ms;
};

/*
 * Every enabled node of the display class, this sink included. The panel is
 * whichever of them is not this device.
 */
#define LVP_DISPLAY_CANDIDATE(node) DEVICE_DT_GET(node),

static const struct device *lvp_find_panel(const struct device *self)
{
	static const struct device *const candidates[] = {
		DT_FOREACH_CLASS_STATUS_OKAY(display, LVP_DISPLAY_CANDIDATE)
		NULL,
	};

	for (size_t i = 0; i < ARRAY_SIZE(candidates); i++) {
		if (candidates[i] == NULL) {
			break;
		}
		if (candidates[i] != self) {
			return candidates[i];
		}
	}

	return NULL;
}

/*
 * Bytes per row of panel-bound data for a rectangle `width` pixels wide.
 *
 * The display API is inconsistent here: for monochrome and 16bpp formats
 * `pitch` is expressed in pixels, for 8bpp in bytes. Rather than trust the
 * descriptor we recompute from the format and screen info, and fall back to the
 * descriptor's own pitch only for formats we do not know.
 */
static uint32_t lvp_row_bytes(const struct display_capabilities *cap, uint16_t width,
			      uint16_t pitch)
{
	switch (cap->current_pixel_format) {
	case PIXEL_FORMAT_MONO01:
	case PIXEL_FORMAT_MONO10:
		if (cap->screen_info & SCREEN_INFO_MONO_VTILED) {
			/* One byte per column, one run per page of 8 rows. */
			return width;
		}
		return DIV_ROUND_UP(width, 8U);
	case PIXEL_FORMAT_L_8:
		return pitch;
	case PIXEL_FORMAT_RGB_565:
	case PIXEL_FORMAT_RGB_565X:
	case PIXEL_FORMAT_AL_88:
		return (uint32_t)width * 2U;
	case PIXEL_FORMAT_RGB_888:
	case PIXEL_FORMAT_BGR_888:
		return (uint32_t)width * 3U;
	case PIXEL_FORMAT_ARGB_8888:
	case PIXEL_FORMAT_XRGB_8888:
	case PIXEL_FORMAT_ABGR_8888:
	case PIXEL_FORMAT_RGBA_8888:
	case PIXEL_FORMAT_BGRA_8888:
		return (uint32_t)width * 4U;
	default:
		return pitch;
	}
}

static enum lvp_format lvp_map_format(enum display_pixel_format pf)
{
	switch (pf) {
	case PIXEL_FORMAT_MONO01:
	case PIXEL_FORMAT_MONO10:
		return LVP_FMT_I1;
	case PIXEL_FORMAT_L_8:
		return LVP_FMT_L8;
	case PIXEL_FORMAT_AL_88:
		return LVP_FMT_AL88;
	case PIXEL_FORMAT_RGB_565:
		return LVP_FMT_RGB565;
	case PIXEL_FORMAT_RGB_565X:
		return LVP_FMT_RGB565_SWAPPED;
	case PIXEL_FORMAT_RGB_888:
	case PIXEL_FORMAT_BGR_888:
		return LVP_FMT_RGB888;
	case PIXEL_FORMAT_ARGB_8888:
		return LVP_FMT_ARGB8888;
	case PIXEL_FORMAT_XRGB_8888:
		return LVP_FMT_XRGB8888;
	default:
		return LVP_FMT_UNKNOWN;
	}
}

static const char *lvp_format_name(enum lvp_format fmt)
{
	switch (fmt) {
	case LVP_FMT_I1:
		return "I1";
	case LVP_FMT_L8:
		return "L8";
	case LVP_FMT_AL88:
		return "AL88";
	case LVP_FMT_RGB565:
		return "RGB565";
	case LVP_FMT_RGB565_SWAPPED:
		return "RGB565_SWAPPED";
	case LVP_FMT_RGB888:
		return "RGB888";
	case LVP_FMT_ARGB8888:
		return "ARGB8888";
	case LVP_FMT_XRGB8888:
		return "XRGB8888";
	default:
		return "UNKNOWN";
	}
}

static uint32_t lvp_capacity_for(const struct display_capabilities *cap)
{
	uint32_t w = cap->x_resolution;
	uint32_t h = cap->y_resolution;

	switch (cap->current_pixel_format) {
	case PIXEL_FORMAT_MONO01:
	case PIXEL_FORMAT_MONO10:
		if (cap->screen_info & SCREEN_INFO_MONO_VTILED) {
			return w * DIV_ROUND_UP(h, 8U);
		}
		return DIV_ROUND_UP(w, 8U) * h;
	case PIXEL_FORMAT_L_8:
		return w * h;
	case PIXEL_FORMAT_RGB_565:
	case PIXEL_FORMAT_RGB_565X:
	case PIXEL_FORMAT_AL_88:
		return w * h * 2U;
	case PIXEL_FORMAT_RGB_888:
	case PIXEL_FORMAT_BGR_888:
		return w * h * 3U;
	case PIXEL_FORMAT_ARGB_8888:
	case PIXEL_FORMAT_XRGB_8888:
	case PIXEL_FORMAT_ABGR_8888:
	case PIXEL_FORMAT_RGBA_8888:
	case PIXEL_FORMAT_BGRA_8888:
		return w * h * 4U;
	default:
		return 0U;
	}
}

static bool lvp_is_mono(const struct display_capabilities *cap)
{
	return (cap->current_pixel_format == PIXEL_FORMAT_MONO01) ||
	       (cap->current_pixel_format == PIXEL_FORMAT_MONO10);
}

/*
 * Composite one driver buffer into the capture buffer, in the panel's own
 * byte layout.
 *
 * Monochrome vertically tiled panels index pages rather than rows, and the
 * driver guarantees page alignment, so the copy stays a run of bytes per page.
 */
static void lvp_composite(const struct device *dev, const struct display_buffer_descriptor *desc,
			  const uint8_t *buf, uint16_t x, uint16_t y)
{
	struct lvp_data *data = dev->data;
	const struct display_capabilities *cap = &data->cap;
	uint32_t dst_row = lvp_row_bytes(cap, cap->x_resolution, cap->x_resolution);
	uint32_t src_row = lvp_row_bytes(cap, desc->width, desc->pitch);
	uint32_t bytes_per_px;

	if ((src_row == 0U) || (dst_row == 0U)) {
		return;
	}

	if (lvp_is_mono(cap) && (cap->screen_info & SCREEN_INFO_MONO_VTILED)) {
		uint32_t pages = DIV_ROUND_UP(desc->height, 8U);

		for (uint32_t p = 0; p < pages; p++) {
			uint32_t dst_off = (((uint32_t)y / 8U) + p) * dst_row + x;
			uint32_t src_off = p * src_row;

			if ((dst_off + src_row) > data->frame.capacity) {
				break;
			}
			if ((src_off + src_row) > (uint32_t)desc->buf_size) {
				break;
			}
			memcpy(data->frame.pixels + dst_off, buf + src_off, src_row);
		}
		return;
	}

	/*
	 * Row-wise formats. `desc->width` counts pixels while `src_row` counts
	 * bytes, so the horizontal byte offset comes from the ratio rather than
	 * being assumed to be one byte per pixel.
	 */
	bytes_per_px = (desc->width == 0U) ? 1U : (src_row / desc->width);
	if (bytes_per_px == 0U) {
		bytes_per_px = 1U;
	}

	for (uint32_t r = 0; r < desc->height; r++) {
		uint32_t dst_off = ((uint32_t)y + r) * dst_row + ((uint32_t)x * bytes_per_px);
		uint32_t src_off = r * src_row;

		if ((src_off + src_row) > (uint32_t)desc->buf_size) {
			break;
		}
		if ((dst_off + src_row) > data->frame.capacity) {
			break;
		}
		memcpy(data->frame.pixels + dst_off, buf + src_off, src_row);
	}
}

static void lvp_mark_dirty(struct lvp_data *data, uint16_t x, uint16_t y, uint16_t w, uint16_t h)
{
	int32_t x2 = (int32_t)x + (int32_t)w - 1;
	int32_t y2 = (int32_t)y + (int32_t)h - 1;

	if (atomic_get(&data->dirty_valid) == 0) {
		atomic_set(&data->dirty_x1, x);
		atomic_set(&data->dirty_y1, y);
		atomic_set(&data->dirty_x2, x2);
		atomic_set(&data->dirty_y2, y2);
		atomic_set(&data->dirty_valid, 1);
		return;
	}

	if (x < (uint16_t)atomic_get(&data->dirty_x1)) {
		atomic_set(&data->dirty_x1, x);
	}
	if (y < (uint16_t)atomic_get(&data->dirty_y1)) {
		atomic_set(&data->dirty_y1, y);
	}
	if (x2 > atomic_get(&data->dirty_x2)) {
		atomic_set(&data->dirty_x2, x2);
	}
	if (y2 > atomic_get(&data->dirty_y2)) {
		atomic_set(&data->dirty_y2, y2);
	}
}

/*
 * Publish the capture buffer to the host.
 *
 * `seq` is written last and is the only field that tells the host a frame is
 * complete; every other field is filled in before it.
 */
static void lvp_publish(struct lvp_data *data)
{
	struct lvp_frame_header *hdr = data->frame.hdr;
	uint32_t seq;

	if (hdr == NULL) {
		return;
	}

	seq = hdr->seq + 1;
	hdr->magic = LVP_MAGIC;
	hdr->version = LVP_VERSION;
	hdr->header_size = sizeof(*hdr);
	hdr->width = data->cap.x_resolution;
	hdr->height = data->cap.y_resolution;
	hdr->format = lvp_map_format(data->cap.current_pixel_format);
	hdr->stride = lvp_row_bytes(&data->cap, data->cap.x_resolution, data->cap.x_resolution);
	hdr->frame_size = data->frame.capacity;
	hdr->pixel_capacity = data->frame.capacity;

	hdr->flags = 0;
	if (data->cap.screen_info & SCREEN_INFO_MONO_VTILED) {
		hdr->flags |= LVP_FLAG_MONO_VTILED;
	}
	if (data->cap.screen_info & SCREEN_INFO_MONO_MSB_FIRST) {
		hdr->flags |= LVP_FLAG_MONO_MSB_FIRST;
	}
	if (IS_ENABLED(CONFIG_LV_Z_COLOR_MONO_HW_INVERSION)) {
		hdr->flags |= LVP_FLAG_MONO_INVERTED;
	}

	/*
	 * Resolve the bit polarity rather than leaving it to the host.
	 *
	 * LVGL writes MONO01 as "1 = black, 0 = white" and MONO10 as the
	 * opposite, and CONFIG_LV_Z_COLOR_MONO_HW_INVERSION flips the result once
	 * more. That is three inputs for one bit of meaning, and getting it wrong
	 * produces a perfectly plausible inverted image, so the device states the
	 * answer explicitly.
	 */
	if (lvp_is_mono(&data->cap)) {
		bool one_is_white = (data->cap.current_pixel_format == PIXEL_FORMAT_MONO01);

		if (IS_ENABLED(CONFIG_LV_Z_COLOR_MONO_HW_INVERSION)) {
			one_is_white = !one_is_white;
		}
		if (one_is_white) {
			hdr->flags |= LVP_FLAG_MONO_ONE_IS_WHITE;
		}
	}
	if (atomic_get(&data->dirty_valid) != 0) {
		hdr->flags |= LVP_FLAG_PARTIAL;
	}

	hdr->hdr_flags = data->blanked ? 0U : LVP_HDR_FLAG_DISPLAY_ON;
	hdr->dirty_x1 = atomic_get(&data->dirty_x1);
	hdr->dirty_y1 = atomic_get(&data->dirty_y1);
	hdr->dirty_x2 = atomic_get(&data->dirty_x2);
	hdr->dirty_y2 = atomic_get(&data->dirty_y2);
	hdr->flushes = atomic_get(&data->flushes);
	hdr->frames = seq;
	hdr->last_flush_us = atomic_get(&data->last_flush_us);
	hdr->tick_ms = k_uptime_get_32();
	hdr->heartbeat_ms = hdr->tick_ms;
	hdr->app_ready = 1;

	atomic_set(&data->dirty_valid, 0);
	data->last_publish_ms = k_uptime_get_32();

	/* Publish. */
	hdr->seq = seq;
}

static int lvp_write(const struct device *dev, const uint16_t x, const uint16_t y,
		     const struct display_buffer_descriptor *desc, const void *buf)
{
	struct lvp_data *data = dev->data;
	uint32_t start = k_uptime_get_32();

	if ((desc == NULL) || (buf == NULL)) {
		return -EINVAL;
	}

	atomic_inc(&data->flushes);

	lvp_composite(dev, desc, buf, x, y);
	lvp_mark_dirty(data, x, y, desc->width, desc->height);

	/*
	 * `frame_incomplete` stays set while more chunks of the same frame are
	 * still coming, so only the final chunk publishes.
	 */
	if (!desc->frame_incomplete) {
		atomic_inc(&data->frames);
		atomic_set(&data->last_flush_us, k_uptime_get_32() - start);
		lvp_publish(data);
	}

	if ((data->panel != NULL) && device_is_ready(data->panel)) {
		int ret = display_write(data->panel, x, y, desc, buf);

		if (ret < 0) {
			LVP_ERR("panel write failed (%d)", ret);
		}
	}

	return 0;
}

static void lvp_get_capabilities(const struct device *dev, struct display_capabilities *caps)
{
	struct lvp_data *data = dev->data;

	*caps = data->cap;
}

static int lvp_set_pixel_format(const struct device *dev, const enum display_pixel_format pf)
{
	struct lvp_data *data = dev->data;

	if (!(data->cap.supported_pixel_formats & pf)) {
		return -ENOTSUP;
	}

	if ((data->panel != NULL) && device_is_ready(data->panel)) {
		int ret = display_set_pixel_format(data->panel, pf);

		if (ret < 0) {
			return ret;
		}
	}

	data->cap.current_pixel_format = pf;
	LVP_INF("pixel format now %s", lvp_format_name(lvp_map_format(pf)));

	return 0;
}

static int lvp_set_orientation(const struct device *dev, const enum display_orientation o)
{
	struct lvp_data *data = dev->data;

	if ((data->panel != NULL) && device_is_ready(data->panel)) {
		int ret = display_set_orientation(data->panel, o);

		if (ret < 0) {
			return ret;
		}
	}

	data->cap.current_orientation = o;

	return 0;
}

static int lvp_blanking_on(const struct device *dev)
{
	struct lvp_data *data = dev->data;

	data->blanked = true;

	if ((data->panel != NULL) && device_is_ready(data->panel)) {
		return display_blanking_on(data->panel);
	}

	return 0;
}

static int lvp_blanking_off(const struct device *dev)
{
	struct lvp_data *data = dev->data;

	data->blanked = false;

	if ((data->panel != NULL) && device_is_ready(data->panel)) {
		return display_blanking_off(data->panel);
	}

	return 0;
}

static DEVICE_API(display, lvp_driver_api) = {
	.blanking_on = lvp_blanking_on,
	.blanking_off = lvp_blanking_off,
	.write = lvp_write,
	.get_capabilities = lvp_get_capabilities,
	.set_pixel_format = lvp_set_pixel_format,
	.set_orientation = lvp_set_orientation,
};

/* ---------------------------------------------------------------- manifest */

struct lvp_manifest {
	char *buf;
	size_t len;
	size_t used;
};

static void mf_puts(struct lvp_manifest *m, const char *s)
{
	size_t n = strlen(s);

	if ((m->used + n) >= m->len) {
		return;
	}
	memcpy(m->buf + m->used, s, n);
	m->used += n;
	m->buf[m->used] = '\0';
}

static void mf_printf(struct lvp_manifest *m, const char *fmt, ...)
{
	va_list ap;
	int n;

	if (m->used >= (m->len - 1U)) {
		return;
	}

	va_start(ap, fmt);
	n = vsnprintk(m->buf + m->used, m->len - m->used, fmt, ap);
	va_end(ap);

	if (n > 0) {
		m->used += MIN((size_t)n, m->len - m->used - 1U);
	}
}

static void mf_json(struct lvp_manifest *m, const char *s)
{
	mf_puts(m, "\"");

	for (; *s != '\0'; s++) {
		char pair[3] = {'\\', *s, '\0'};

		if ((*s == '"') || (*s == '\\')) {
			mf_puts(m, pair);
		} else if ((unsigned char)*s < 0x20U) {
			mf_printf(m, "\\u%04x", (unsigned char)*s);
		} else {
			char one[2] = {*s, '\0'};

			mf_puts(m, one);
		}
	}

	mf_puts(m, "\"");
}

static void mf_trim_comma(struct lvp_manifest *m)
{
	/* Turn a trailing ",\n" into "\n" so lists can be emitted by a
	 * DT_FOREACH_* loop, which cannot know which element is last. */
	if (m->used >= 2U && m->buf[m->used - 2U] == ',') {
		m->buf[m->used - 2U] = '\n';
		m->buf[m->used - 1U] = '\0';
		m->used -= 1U;
	}
}

#define LVP_BUTTON_ENTRY(child, m)                                                                 \
	do {                                                                                       \
		mf_puts(m, "    {\"node\": ");                                                    \
		mf_json(m, DT_NODE_FULL_NAME(child));                                              \
		mf_puts(m, ", \"label\": ");                                                      \
		mf_json(m, DT_PROP_OR(child, label, ""));                                          \
		mf_printf(m, ", \"code\": %u},\n", (unsigned int)DT_PROP_OR(child, zephyr_code, 0)); \
	} while (0);

#define LVP_BUTTONS_PARENT(parent)                                                                 \
	DT_FOREACH_CHILD_STATUS_OKAY_VARGS(parent, LVP_BUTTON_ENTRY, &m)

#define LVP_TOUCH_ENTRY(node, m)                                                                   \
	do {                                                                                       \
		mf_puts(m, "    {\"node\": ");                                                     \
		mf_json(m, DT_NODE_FULL_NAME(node));                                               \
		mf_puts(m, ", \"label\": ");                                                       \
		mf_json(m, DT_PROP_OR(node, label, ""));                                           \
		mf_puts(m, "},\n");                                                                \
	} while (0);

static void lvp_write_manifest(const struct device *dev)
{
	static char buf[4096];
	struct lvp_manifest m = {.buf = buf, .len = sizeof(buf), .used = 0};
	struct lvp_data *data = dev->data;

	mf_puts(&m, "{\n");
	mf_puts(&m, "  \"schema\": \"lvgl-preview-manifest/1\",\n");
	mf_printf(&m, "  \"board\": \"%s\",\n", CONFIG_BOARD_TARGET);
	mf_printf(&m, "  \"zephyr\": \"%s\",\n", KERNEL_VERSION_STRING);
	mf_printf(&m, "  \"app\": \"%s\",\n", CONFIG_LVGL_PREVIEW_APP_NAME);
	mf_printf(&m, "  \"capture\": \"%s\",\n", CONFIG_LVGL_PREVIEW_CAPTURE_PATH);
	mf_printf(&m, "  \"input\": \"%s\",\n", CONFIG_LVGL_PREVIEW_INPUT_PATH);

	mf_puts(&m, "  \"display\": {\n");
	mf_printf(&m, "    \"width\": %u,\n", data->cap.x_resolution);
	mf_printf(&m, "    \"height\": %u,\n", data->cap.y_resolution);
	mf_printf(&m, "    \"format\": \"%s\",\n",
		  lvp_format_name(lvp_map_format(data->cap.current_pixel_format)));
	/*
	 * The monochrome screen-info bits are only meaningful for the monochrome
	 * formats; panels that support colour as well advertise them regardless,
	 * so reporting them unconditionally would mislead the host into treating
	 * a colour frame as packed 1bpp.
	 */
	mf_printf(&m, "    \"monochrome\": %s,\n", lvp_is_mono(&data->cap) ? "true" : "false");
	mf_printf(&m, "    \"mono_vtiled\": %s,\n",
		  (lvp_is_mono(&data->cap) && (data->cap.screen_info & SCREEN_INFO_MONO_VTILED))
			  ? "true"
			  : "false");
	mf_printf(&m, "    \"mono_msb_first\": %s,\n",
		  (lvp_is_mono(&data->cap) && (data->cap.screen_info & SCREEN_INFO_MONO_MSB_FIRST))
			  ? "true"
			  : "false");
	mf_printf(&m, "    \"mono_inverted\": %s,\n",
		  (lvp_is_mono(&data->cap) && IS_ENABLED(CONFIG_LV_Z_COLOR_MONO_HW_INVERSION))
			  ? "true"
			  : "false");
	mf_printf(&m, "    \"epd\": %s,\n",
		  (data->cap.screen_info & SCREEN_INFO_EPD) ? "true" : "false");
	mf_printf(&m, "    \"tee\": %s,\n", (data->panel != NULL) ? "true" : "false");
	mf_puts(&m, "    \"device\": ");
	mf_json(&m, dev->name);
	mf_puts(&m, ",\n    \"panel\": ");
	mf_json(&m, (data->panel != NULL) ? data->panel->name : "");
	mf_puts(&m, "\n  },\n");

	mf_puts(&m, "  \"buttons\": [\n");
	DT_FOREACH_STATUS_OKAY(gpio_keys, LVP_BUTTONS_PARENT)
	mf_trim_comma(&m);
	mf_puts(&m, "  ],\n");

	mf_puts(&m, "  \"touch\": [\n");
	COND_CODE_1(DT_HAS_CHOSEN(zephyr_touch), (LVP_TOUCH_ENTRY(DT_CHOSEN(zephyr_touch), &m)), ());
	mf_trim_comma(&m);
	mf_puts(&m, "  ]\n");
	mf_puts(&m, "}\n");

	if (lvp_host_write_manifest(CONFIG_LVGL_PREVIEW_MANIFEST_PATH, buf, m.used) != 0) {
		LVP_ERR("could not write manifest");
	} else {
		LVP_INF("manifest written (%u bytes)", (unsigned int)m.used);
	}
}

/* ------------------------------------------------------------------- init */

static int lvp_init(const struct device *dev)
{
	struct lvp_data *data = dev->data;
	const struct lvp_config *cfg = dev->config;
	uint32_t capacity;
	int ret;

	data->cap.current_orientation = DISPLAY_ORIENTATION_NORMAL;

	data->panel = cfg->panel_override;

	/*
	 * A declared geometry means "emulate this panel". That is what the
	 * generated overlay does, and in that case there is nothing to scan for:
	 * on the simulator the only other display-class node is the SDL window,
	 * which is not the panel the application targets and must not be adopted.
	 *
	 * With no declared geometry the sink is a pass-through, so it looks for
	 * the panel it was placed in front of.
	 */
	if ((data->panel == NULL) && ((cfg->width == 0U) || (cfg->height == 0U))) {
		data->panel = lvp_find_panel(dev);
	}

	if (data->panel != NULL) {
		struct display_capabilities wcap;

		if (!device_is_ready(data->panel)) {
			LVP_ERR("panel %s is not ready", data->panel->name);
			return -ENODEV;
		}

		/*
		 * Mirror the panel exactly. Being a transparent pass-through is
		 * what keeps this generic: whatever the panel driver declares is
		 * what LVGL sees, so the project's own configuration keeps
		 * working untouched.
		 */
		display_get_capabilities(data->panel, &wcap);
		data->cap = wcap;

		if (cfg->width != 0U) {
			data->cap.x_resolution = cfg->width;
		}
		if (cfg->height != 0U) {
			data->cap.y_resolution = cfg->height;
		}
	} else {
		/*
		 * Capture-only mode: no panel to mirror, so offer the formats
		 * this sink can composite and let LVGL choose. Monochrome
		 * vertical tiling is the SSD1306-family layout, which is also
		 * what the preview host renders most faithfully.
		 */
		if ((cfg->width == 0U) || (cfg->height == 0U)) {
			LVP_ERR("no panel found and no width/height on the sink node; "
				"set them on the lvgl,preview-sink node");
			return -EINVAL;
		}

		data->cap.x_resolution = cfg->width;
		data->cap.y_resolution = cfg->height;
		data->cap.supported_pixel_formats = PIXEL_FORMAT_MONO01 | PIXEL_FORMAT_MONO10 |
						    PIXEL_FORMAT_L_8 | PIXEL_FORMAT_RGB_565 |
						    PIXEL_FORMAT_RGB_888 | PIXEL_FORMAT_ARGB_8888;
		data->cap.current_pixel_format = PIXEL_FORMAT_MONO10;
		data->cap.screen_info = SCREEN_INFO_MONO_VTILED | SCREEN_INFO_MONO_MSB_FIRST;
	}

	capacity = lvp_capacity_for(&data->cap);
	if (capacity == 0U) {
		LVP_ERR("unsupported pixel format %d", data->cap.current_pixel_format);
		return -ENOTSUP;
	}

	ret = lvp_host_map_frame(CONFIG_LVGL_PREVIEW_CAPTURE_PATH, capacity, &data->frame.handle,
				 (void **)&data->frame.hdr, (void **)&data->frame.pixels);
	if (ret != 0) {
		LVP_ERR("could not map capture buffer");
		return -EIO;
	}

	data->frame.capacity = capacity;
	memset(data->frame.pixels, 0, capacity);

	/*
	 * Fill in the geometry immediately. The host uses this to size its window
	 * and to decide the panel is alive; waiting for the first flush would make
	 * a working sink look dead until the application happened to redraw.
	 */
	data->frame.hdr->width = data->cap.x_resolution;
	data->frame.hdr->height = data->cap.y_resolution;
	data->frame.hdr->format = lvp_map_format(data->cap.current_pixel_format);
	data->frame.hdr->stride =
		lvp_row_bytes(&data->cap, data->cap.x_resolution, data->cap.x_resolution);
	data->frame.hdr->frame_size = capacity;
	data->frame.hdr->heartbeat_ms = k_uptime_get_32();
	data->frame.hdr->pixel_capacity = capacity;
	data->frame.hdr->header_size = sizeof(struct lvp_frame_header);
	data->frame.hdr->version = LVP_VERSION;
	data->frame.hdr->magic = LVP_MAGIC;
	strncpy(data->frame.hdr->device_name, dev->name, sizeof(data->frame.hdr->device_name) - 1);
	strncpy(data->frame.hdr->display_name, CONFIG_BOARD_TARGET,
		sizeof(data->frame.hdr->display_name) - 1);
	strncpy(data->frame.hdr->app_name, CONFIG_LVGL_PREVIEW_APP_NAME,
		sizeof(data->frame.hdr->app_name) - 1);

	data->blanked = true;
	data->last_publish_ms = k_uptime_get_32();

	LVP_INF("display %ux%u %s, stride %u, panel %s", data->cap.x_resolution,
		data->cap.y_resolution,
		lvp_format_name(lvp_map_format(data->cap.current_pixel_format)),
		(unsigned int)lvp_row_bytes(&data->cap, data->cap.x_resolution,
					    data->cap.x_resolution),
		(data->panel != NULL) ? data->panel->name : "(none)");

	lvp_write_manifest(dev);

	return 0;
}

#define LVP_PANEL_OVERRIDE(inst)                                                                   \
	COND_CODE_1(DT_INST_NODE_HAS_PROP(inst, wrapped_display),                                  \
		    (DEVICE_DT_GET(DT_INST_PHANDLE(inst, wrapped_display))), (NULL))

#define LVP_INIT(inst)                                                                             \
	static const struct lvp_config lvp_config_##inst = {                                       \
		.width = DT_INST_PROP_OR(inst, width, 0),                                          \
		.height = DT_INST_PROP_OR(inst, height, 0),                                        \
		.pixel_format = (DT_INST_PROP_OR(inst, pixel_format,                             \
						 PANEL_PIXEL_FORMAT_MONO10) ==                          \
				 PANEL_PIXEL_FORMAT_MONO01)                                         \
					? PIXEL_FORMAT_MONO01                                          \
					: PIXEL_FORMAT_MONO10,                                    \
		/*                                                                                 \
		 * Default to the layout every 128x64 OLED controller uses: eight vertical         \
		 * pixels per byte, bit 7 first. The generated overlay states this                 \
		 * explicitly; the default only matters when somebody writes an overlay            \
		 * by hand.                                                                        \
		 */                                                                                 \
		.screen_info = (DT_INST_PROP_OR(inst, mono_vtiled, 1)                              \
					? SCREEN_INFO_MONO_VTILED : 0) |                            \
			       (DT_INST_PROP_OR(inst, mono_msb_first, 1)                           \
					? SCREEN_INFO_MONO_MSB_FIRST : 0),                         \
		.panel_override = LVP_PANEL_OVERRIDE(inst),                                        \
	};                                                                                         \
	static struct lvp_data lvp_data_##inst;                                                    \
	DEVICE_DT_INST_DEFINE(inst, lvp_init, NULL, &lvp_data_##inst, &lvp_config_##inst,          \
			      POST_KERNEL, CONFIG_LVGL_PREVIEW_INIT_PRIORITY, &lvp_driver_api);

DT_INST_FOREACH_STATUS_OKAY(LVP_INIT)
