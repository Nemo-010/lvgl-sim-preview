/*
 * lvgl_preview_input.c - feed host input into LVGL.
 *
 * SPDX-License-Identifier: Apache-2.0
 *
 * The preview host writes events into a shared ring; this file drains that ring
 * and drives LVGL input devices with it, so a mouse, a touch screen or a
 * keyboard on the desktop reaches the application running on the target.
 *
 * Two LVGL input devices are created:
 *   - a pointer, for absolute position and press/release
 *   - a keypad, for buttons
 *
 * The keypad is given a group, and that group is made LVGL's default group, so
 * widgets the application creates afterwards are reachable without the
 * application knowing that a keypad exists.
 */

#include <zephyr/device.h>
#include <zephyr/dt-bindings/input/input-event-codes.h>
#include <zephyr/kernel.h>
#include <zephyr/logging/log.h>
#include <zephyr/sys/printk.h>
#include <zephyr/sys/util.h>

#include <stdlib.h>

#include <lvgl.h>

#include "lvgl_preview_bottom.h"
#include "lvgl_preview_proto.h"

LOG_MODULE_DECLARE(lvgl_preview, CONFIG_LVGL_PREVIEW_LOG_LEVEL);

#define LVP_ERR(fmt, ...) printk("lvgl-preview: " fmt "\n", ##__VA_ARGS__)
#define LVP_INF(fmt, ...) printk("lvgl-preview: " fmt "\n", ##__VA_ARGS__)

K_MSGQ_DEFINE(lvp_key_msgq, sizeof(uint32_t), CONFIG_LVGL_PREVIEW_INPUT_QUEUE_SIZE, 4);

struct lvp_input_state {
	struct lvp_input_header *ring;
	void *handle;

	lv_indev_t *pointer;
	lv_indev_t *keypad;
	lv_group_t *group;

	/* Latest absolute pointer position, in panel pixels. */
	int32_t x;
	int32_t y;
	bool pressed;

	atomic_t received;
	atomic_t dropped;
};

static struct lvp_input_state lvp_input;

/*
 * Map a Zephyr input key code onto the LVGL key the widgets expect.
 *
 * The wire code is whatever the host sends, which the preview host takes from
 * the manifest, so a project's own `zephyr,code` values arrive here unchanged.
 * Codes without a navigation meaning still reach LVGL as their raw value, which
 * is what a keypad-driven text field wants for digits.
 */
static uint32_t lvp_map_key(uint32_t code)
{
	switch (code) {
	case INPUT_KEY_UP:
	case INPUT_KEY_W:
		return LV_KEY_UP;
	case INPUT_KEY_DOWN:
	case INPUT_KEY_S:
		return LV_KEY_DOWN;
	case INPUT_KEY_LEFT:
	case INPUT_KEY_A:
		return LV_KEY_LEFT;
	case INPUT_KEY_RIGHT:
	case INPUT_KEY_D:
		return LV_KEY_RIGHT;
	case INPUT_KEY_ENTER:
	case INPUT_KEY_KPENTER:
	case INPUT_BTN_LEFT:
	case INPUT_BTN_SOUTH:
		return LV_KEY_ENTER;
	case INPUT_KEY_ESC:
	case INPUT_KEY_BACKSPACE:
	case INPUT_BTN_RIGHT:
	case INPUT_BTN_EAST:
		return LV_KEY_ESC;
	case INPUT_KEY_HOME:
		return LV_KEY_HOME;
	case INPUT_KEY_END:
		return LV_KEY_END;
	case INPUT_KEY_PAGEUP:
		return LV_KEY_PREV;
	case INPUT_KEY_PAGEDOWN:
		return LV_KEY_NEXT;
	case INPUT_BTN_NORTH:
	case INPUT_KEY_TAB:
		return LV_KEY_NEXT;
	case INPUT_BTN_WEST:
		return LV_KEY_PREV;
	default:
		break;
	}

	/* Digits and letters travel as themselves: LVGL keypads accept ASCII. */
	if (code >= INPUT_KEY_1 && code <= INPUT_KEY_0) {
		/* INPUT_KEY_1..INPUT_KEY_0 are not contiguous with ASCII. */
		static const char digits[] = "1234567890";

		return (uint32_t)digits[code - INPUT_KEY_1];
	}

	return code;
}

static void lvp_pointer_read(lv_indev_t *indev, lv_indev_data_t *data)
{
	ARG_UNUSED(indev);

	data->point.x = lvp_input.x;
	data->point.y = lvp_input.y;
	data->state = lvp_input.pressed ? LV_INDEV_STATE_PRESSED : LV_INDEV_STATE_RELEASED;
}

static void lvp_keypad_read(lv_indev_t *indev, lv_indev_data_t *data)
{
	uint32_t code;

	ARG_UNUSED(indev);

	if (k_msgq_get(&lvp_key_msgq, &code, K_NO_WAIT) != 0) {
		data->state = LV_INDEV_STATE_RELEASED;
		data->key = 0;
		return;
	}

	data->key = code;
	data->state = LV_INDEV_STATE_PRESSED;
}

static void lvp_drain_ring(void)
{
	struct lvp_input_header *ring = lvp_input.ring;

	if (ring == NULL) {
		return;
	}

	while (ring->tail != ring->head) {
		uint32_t idx = ring->tail % ring->capacity;
		struct lvp_input_event *ev = &ring->ring[idx];

		switch (ev->type) {
		case LVP_INPUT_POINTER:
			lvp_input.x = ev->x;
			lvp_input.y = ev->y;
			lvp_input.pressed = ev->value != 0U;
			break;
		case LVP_INPUT_KEY: {
			uint32_t key = lvp_map_key(ev->code);

			/*
			 * The queue carries presses; a release is synthesised by
			 * the read callback once the queue drains, which is how
			 * LVGL keypads are meant to be driven.
			 */
			if (ev->value != 0U) {
				if (k_msgq_put(&lvp_key_msgq, &key, K_NO_WAIT) != 0) {
					atomic_inc(&lvp_input.dropped);
				}
			}
			break;
		}
		case LVP_INPUT_ENCODER: {
			uint32_t key = (ev->value > 0) ? LV_KEY_NEXT : LV_KEY_PREV;
			int32_t steps = abs((int)ev->value);

			for (int32_t i = 0; i < steps; i++) {
				if (k_msgq_put(&lvp_key_msgq, &key, K_NO_WAIT) != 0) {
					atomic_inc(&lvp_input.dropped);
					break;
				}
			}
			break;
		}
		default:
			break;
		}

		atomic_inc(&lvp_input.received);
		ring->tail++;
	}
}

static void lvp_input_thread(void *a, void *b, void *c)
{
	ARG_UNUSED(a);
	ARG_UNUSED(b);
	ARG_UNUSED(c);

	for (;;) {
		lvp_drain_ring();
		k_msleep(10);
	}
}

K_THREAD_DEFINE(lvp_input_tid, CONFIG_LVGL_PREVIEW_INPUT_STACK_SIZE, lvp_input_thread, NULL, NULL,
		NULL, CONFIG_LVGL_PREVIEW_INPUT_PRIORITY, 0, 0);

static int lvp_input_init(void)
{
	if (lvp_host_map_input(CONFIG_LVGL_PREVIEW_INPUT_PATH, &lvp_input.handle,
			       (void **)&lvp_input.ring) != 0) {
		LVP_ERR("could not map input ring");
		return -EIO;
	}

	lvp_input.ring->magic = LVP_INPUT_MAGIC;
	lvp_input.ring->version = LVP_VERSION;
	lvp_input.ring->header_size = sizeof(struct lvp_input_header);
	lvp_input.ring->capacity = LVP_INPUT_CAPACITY;

	lvp_input.pointer = lv_indev_create();
	lv_indev_set_type(lvp_input.pointer, LV_INDEV_TYPE_POINTER);
	lv_indev_set_read_cb(lvp_input.pointer, lvp_pointer_read);

	lvp_input.keypad = lv_indev_create();
	lv_indev_set_type(lvp_input.keypad, LV_INDEV_TYPE_KEYPAD);
	lv_indev_set_read_cb(lvp_input.keypad, lvp_keypad_read);

	lvp_input.group = lv_group_create();
	lv_indev_set_group(lvp_input.keypad, lvp_input.group);
	lv_group_set_default(lvp_input.group);

	LVP_INF("preview input ready (pointer + keypad)");

	return 0;
}

/*
 * LVGL initializes at CONFIG_LV_Z_INIT_PRIORITY in the APPLICATION level, so
 * this has to run after it; a higher priority number in the same level does it.
 */
SYS_INIT(lvp_input_init, APPLICATION, CONFIG_LVGL_PREVIEW_INPUT_INIT_PRIORITY);
