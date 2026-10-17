/*
 * render-probe: a small LVGL application used to exercise the preview system.
 *
 * SPDX-License-Identifier: Apache-2.0
 *
 * It draws a known, deterministic pattern so a captured frame can be checked
 * rather than merely looked at:
 *
 *   - a full border, so the exact panel geometry is verifiable
 *   - a large label, so text rendering and the default font are exercised
 *   - a bar that sweeps, so partial refreshes actually happen
 *   - a focusable list, so a keypad can be driven from the preview host
 *   - a press counter, so button events are visibly delivered
 *
 * Everything is sized in fractions of the panel, so the same source looks right
 * on a 128x64 OLED and on a 320x240 colour panel.
 *
 * Nothing here is preview-specific: it is an ordinary LVGL application, which is
 * the point. The preview attaches from outside.
 */

#include <zephyr/device.h>
#include <zephyr/devicetree.h>
#include <zephyr/drivers/display.h>
#include <zephyr/kernel.h>

#include <lvgl.h>

static lv_obj_t *g_status;
static lv_obj_t *g_bar;
static int g_presses;

static void bump(const char *why)
{
	g_presses++;
	lv_label_set_text_fmt(g_status, "%s: %d", why, g_presses);
}

static void on_click(lv_event_t *e)
{
	ARG_UNUSED(e);

	bump("clicks");
}

/*
 * Report every key that arrives, not only the ones LVGL maps to a widget action.
 *
 * A real board's buttons are whatever codes its devicetree gives them: on the
 * MCXA156 they are INPUT_KEY_0 and INPUT_KEY_1, i.e. the characters '0' and '1',
 * which no widget treats as ENTER. Handling the raw key event here is how the
 * preview proves the whole input path works regardless of what the panel's
 * buttons happen to be.
 */
static void on_key(lv_event_t *e)
{
	uint32_t key = lv_event_get_key(e);

	if (key == 0) {
		return;
	}

	lv_label_set_text_fmt(g_status, "key %u", (unsigned)key);
	g_presses++;
}

int main(void)
{
	const struct device *display = DEVICE_DT_GET(DT_CHOSEN(zephyr_display));
	lv_obj_t *screen;
	lv_obj_t *label;
	lv_obj_t *list;
	int32_t width;
	int32_t height;
	int ret;

	if (!device_is_ready(display)) {
		printk("render-probe: display %s is not ready\n", display->name);
		return 0;
	}

	/*
	 * Blanking matters more than it looks. The sink reports it in the frame
	 * header, and a display left blanked is exactly what a "my preview is
	 * empty" report usually turns out to be. Print the result so a failure is
	 * visible instead of silent.
	 */
	ret = display_blanking_off(display);
	printk("render-probe: blanking off -> %d\n", ret);
	if (ret < 0 && ret != -ENOSYS) {
		printk("render-probe: blanking off failed (%d)\n", ret);
	}

	screen = lv_screen_active();
	width = lv_display_get_horizontal_resolution(NULL);
	height = lv_display_get_vertical_resolution(NULL);

	lv_obj_set_style_bg_color(screen, lv_color_black(), 0);
	lv_obj_set_style_bg_opa(screen, LV_OPA_COVER, 0);
	lv_obj_set_style_text_color(screen, lv_color_white(), 0);

	/* A 1px border makes the captured geometry verifiable at a glance. */
	lv_obj_set_style_border_color(screen, lv_color_white(), 0);
	lv_obj_set_style_border_width(screen, 1, 0);

	/* Receive key events on the screen itself. */
	lv_obj_add_flag(screen, LV_OBJ_FLAG_CLICKABLE);
	lv_group_t *group = lv_group_get_default();

	if (group != NULL) {
		lv_group_add_obj(group, screen);
	}
	lv_obj_add_event_cb(screen, on_key, LV_EVENT_KEY, NULL);

	/*
	 * Layout in panel fractions, with a floor of one text line (8 px with the
	 * UNSCII 8 font). On a 128x64 OLED that gives title, bar, list and status
	 * without overlap, and it still looks right on a larger colour panel.
	 */
	const int32_t line = 10;
	const int32_t margin = 3;

	label = lv_label_create(screen);
	lv_label_set_text(label, "PREVIEW");
	lv_obj_set_pos(label, margin, margin);

	g_bar = lv_bar_create(screen);
	lv_obj_set_size(g_bar, width - 2 * margin, MAX(line, height / 8));
	lv_bar_set_range(g_bar, 0, 100);
	lv_bar_set_value(g_bar, 50, LV_ANIM_OFF);
	lv_obj_set_pos(g_bar, margin, margin + line + 2);

	const int32_t list_y = margin + 2 * line + 4;
	const int32_t status_h = line + margin;
	const int32_t list_h = MAX(line * 2, height - list_y - status_h - 2);

	/* A focusable list proves the keypad reaches the application. */
	list = lv_list_create(screen);
	lv_obj_set_size(list, width - 2 * margin, list_h);
	lv_obj_set_pos(list, margin, list_y);

	/*
	 * On a 1bpp panel the theme's defaults are not helpful: a list item is drawn
	 * with a light background and dark text, which on monochrome reads as a solid
	 * white block. Every colour is therefore set explicitly, which is also what a
	 * real OLED application has to do.
	 */
	lv_obj_set_style_bg_color(list, lv_color_black(), 0);
	lv_obj_set_style_bg_opa(list, LV_OPA_COVER, 0);
	lv_obj_set_style_border_color(list, lv_color_white(), 0);
	lv_obj_set_style_border_width(list, 1, 0);
	lv_obj_set_style_text_color(list, lv_color_white(), 0);
	lv_obj_set_style_pad_all(list, 0, 0);
	lv_obj_set_style_pad_row(list, 0, 0);

	lv_obj_t *item = lv_list_add_button(list, NULL, "one");

	lv_obj_add_event_cb(item, on_click, LV_EVENT_CLICKED, NULL);
	item = lv_list_add_button(list, NULL, "two");
	lv_obj_add_event_cb(item, on_click, LV_EVENT_CLICKED, NULL);

	/* Same reasoning as the list itself: state the colours for the buttons. */
	uint32_t count = lv_obj_get_child_count(list);

	for (uint32_t i = 0; i < count; i++) {
		lv_obj_t *child = lv_obj_get_child(list, (int32_t)i);

		lv_obj_set_style_bg_color(child, lv_color_black(), 0);
		lv_obj_set_style_bg_opa(child, LV_OPA_COVER, 0);
		lv_obj_set_style_text_color(child, lv_color_white(), 0);
		lv_obj_set_style_border_width(child, 0, 0);
		lv_obj_set_style_pad_all(child, 1, 0);
		lv_obj_set_style_radius(child, 0, 0);
	}

	g_status = lv_label_create(screen);
	lv_label_set_text(g_status, "presses: 0");
	lv_obj_set_pos(g_status, margin, height - status_h);

	printk("render-probe: drawing on %dx%d\n", (int)width, (int)height);

	while (1) {
		/* Sweep the bar so partial refreshes actually happen. */
		static int value = 50;
		static int step = 7;

		value += step;
		if (value >= 100) {
			value = 100;
			step = -step;
		} else if (value <= 0) {
			value = 0;
			step = -step;
		}
		lv_bar_set_value(g_bar, value, LV_ANIM_OFF);

		/*
		 * Drive LVGL from the application, which is the ordinary LVGL
		 * pattern. The preview also works when an application does not do
		 * this (it enables CONFIG_LV_Z_RUN_LVGL_ON_WORKQUEUE), but doing it
		 * here keeps the example honest and independent of that setting.
		 */
		lv_timer_handler();

		k_sleep(K_MSEC(200));
	}

	return 0;
}
