# lvgl-preview

Preview a Zephyr LVGL application on your desktop, faithfully, without touching
the application.

It builds your project for the native simulator, attaches a capture sink in
front of your display, and shows exactly the bytes your panel would have
received. The emulated panel has the same geometry and pixel format as the one
your board really has, because those are read from that board's devicetree.

```
lvgl-preview run path/to/app --target-board frdm_mcxa156 -o frame.png
```

```
device    render-probe on native_sim/native/64 (zephyr 4.5.0-rc2)
display   128x64 I1
buttons   User SW2 (code 11), User SW3 (code 2)
frame     frame.png (seq 158)
```

## Why a display driver

The sink is a Zephyr display controller, not a hook into LVGL. That choice is
what makes it generic and what makes it trustworthy:

- **Generic.** It uses only the stable display driver API, so it does not care
  which LVGL version, which LVGL integration, or which panel you use.
- **No changes to your project.** Point `zephyr,display` at the sink and LVGL,
  the LVGL input glue and your application behave exactly as before.
- **Faithful.** What it captures is what `display_write()` was handed, so the
  preview is evidence about your real hardware path, not a parallel rendering
  path that could disagree with it.

## How the panel is discovered

Nothing about your board is hardcoded. The tooling configures Zephyr twice:

1. for your **target board**, which produces `zephyr.dts` — the panel's
   geometry, its pixel format, the buttons and their codes, the touch
   controller. No cross compiler is needed, because the devicetree is resolved
   before any code is built.
2. for the **native simulator**, with an overlay generated from that devicetree.

So the emulated panel is 128x64 monochrome with the same vertical tiling and bit
order as your SSD1306, and the preview's buttons carry the same codes as the
real ones. The overlay is generated, never written by hand, so it cannot drift.

On the MCXA156 example this finds:

```
128x64 monochrome (solomon,ssd1306)
buttons: User SW2 (INPUT_KEY_0), User SW3 (INPUT_KEY_1)
```

## Install

Requires a Zephyr tree and a host C compiler. No Zephyr SDK is needed: the
native simulator builds with your host toolchain.

```bash
python3 -m pip install -e .
```

`cmake`, `ninja` and `dtc` must be on `PATH`. If you use pixi, `pixi install`
in this repository provides all of them.

## Usage

```bash
# build only, and report what was discovered
lvgl-preview build path/to/app --target-board frdm_mcxa156

# build, run, write the first frame
lvgl-preview run path/to/app --target-board frdm_mcxa156 -o frame.png --scale 4

# press the target board's buttons and capture the result
lvgl-preview drive path/to/app --target-board frdm_mcxa156 --keys 11,11 -o after.png

# inspect an existing capture
lvgl-preview info frame.bin
lvgl-preview render frame.bin -o frame.png
```

Without `--target-board` the preview uses the simulator's own panel, which is
useful for LVGL applications that are not tied to a board.

## When the preview is blank

This is the case the tool exists for, so it is designed to explain itself rather
than show an empty window. In order of likelihood:

1. **Your application returned early.** If it checks for a sensor, an EEPROM or
   any other device that the simulator does not have, it exits before drawing.
   The tool detects this and prints the application's own output.
2. **LVGL is never ticked.** `lv_timer_handler()` has to be called. The preview
   sets `CONFIG_LV_Z_RUN_LVGL_ON_WORKQUEUE=y` so Zephyr drives LVGL for you; if
   your project forces it off, nothing will ever render.
3. **The display is still blanked.** Call `display_blanking_off()`.
4. **Static rendering buffers.** The sink has no compile-time geometry, so LVGL
   must size its buffers at runtime. If your project sets
   `CONFIG_LV_Z_BUFFER_ALLOC_STATIC`, either switch to dynamic allocation or
   state `width`/`height` on the sink node.

`lvgl-preview info` reports the geometry, frame count and blanking state, which
separates "the sink never initialised" from "the application never drew".

## Monochrome specifics

1bpp panels have three separate things that can be wrong, so the sink resolves
them once and states the answer in the frame header instead of leaving the host
to guess:

- **packing**: eight vertical pixels per byte, or eight horizontal
- **bit order**: whether bit 7 or bit 0 is the first pixel
- **polarity**: whether a set bit means a lit pixel

The last one is not derivable from the pixel format alone: `MONO01` and `MONO10`
write opposite bits, and `CONFIG_LV_Z_COLOR_MONO_HW_INVERSION` flips it again.
Getting it wrong produces a perfectly plausible inverted image, so the device
states it explicitly.

## Layout

```
include/lvgl_preview_proto.h   wire format, shared by the device and the host
zephyr/                        the capture sink (a display driver) and input bridge
overlay/lvgl_preview.overlay   drop-in overlay for projects without a target board
dts/bindings/                  the lvgl,preview-sink binding
src/lvgl_preview/              host side: build, capture, render, CLI
examples/render-probe/         a small LVGL app used to test the system itself
```

The device publishes frames into a file-backed mapping. `seq` is written last
and is the only field that marks a frame complete, so a viewer never shows a
torn frame during a partial refresh.

## Licence

Apache-2.0.
