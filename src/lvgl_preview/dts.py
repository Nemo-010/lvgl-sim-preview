"""Derive the preview overlay from the *target* board's devicetree.

This is what makes the preview faithful without hardcoding anything.

A Zephyr application's panel usually exists only for the board it targets: the
SSD1306 node lives in ``boards/frdm_mcxa156.overlay``, and the native simulator
has never heard of it. Rendering for native_sim alone would therefore show a
320x240 SDL window that has nothing to do with the 128x64 OLED the application
was written for.

So the tooling asks Zephyr twice:

1. configure for the real board, which produces ``zephyr.dts``: the panel's
   geometry, its pixel format, the buttons, the touch controller. This needs no
   cross compiler, because the devicetree is resolved before any code is built.
2. configure for native_sim with an overlay generated from that devicetree, so
   the emulated panel has exactly the real panel's geometry and the real
   buttons' codes.

The overlay is generated, never written by hand, so it cannot drift.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

# PANEL_PIXEL_FORMAT_* values, from zephyr/dt-bindings/display/panel.h.
# Copied deliberately: the devicetree is a text artifact we parse, and these
# numbers are part of the devicetree ABI rather than an internal enum.
PANEL_FORMATS = {
    "MONO01": 2,
    "MONO10": 4,
    "RGB888": 1,
    "ARGB8888": 8,
    "RGB565": 16,
    "RGB565X": 32,
    "L8": 64,
    "AL88": 128,
    "XRGB8888": 256,
    "BGR888": 512,
    "I4": 8192,
}

# Displays whose driver declares MONO01/MONO10, mapped to what we should emulate.
MONO_COMPATIBLES = (
    "solomon,ssd1306",
    "solomon,ssd1306b",
    "sinowealth,sh1106",
    "sinowealth,sh1107",
    "sinowealth,sh1108",
    "sinowealth,sh1122",
    "philips,pcd8544",
    "nxp,pcF85363",
    "solomon,ssd1309",
    "solomon,ssd1315",
    "ilitek,ili9341",
)

# 128x64 OLED panels are vertically tiled with bit 7 first. This is the layout
# the SSD1306 family and its clones use.
MONO_VTILED = True
MONO_MSB_FIRST = True


class DtsError(RuntimeError):
    """Raised when the target devicetree cannot be produced or parsed."""


@dataclass
class TargetButton:
    node: str
    label: str
    code: int


@dataclass
class TargetDisplay:
    node: str
    label: str
    compatible: str
    width: int
    height: int
    monochrome: bool
    pixel_format: int
    inverted: bool = False


@dataclass
class TargetInfo:
    board: str
    display: TargetDisplay | None = None
    buttons: list[TargetButton] = field(default_factory=list)
    touch: list[str] = field(default_factory=list)
    raw_dts: str = ""

    def summary(self) -> str:
        if self.display is None:
            return f"{self.board}: no display selected"
        d = self.display
        kind = "monochrome" if d.monochrome else "colour"
        return (
            f"{self.board}: {d.width}x{d.height} {kind} ({d.compatible}) "
            f"with {len(self.buttons)} button(s)"
        )


def _configure_for_dts(
    app: Path, board: str, zephyr_base: Path, out: Path, env: dict[str, str]
) -> str:
    """Run a configure that stops after devicetree generation.

    Only the devicetree is wanted, so this deliberately does not need a cross
    toolchain. ``cmake`` will still try to identify a compiler; the tree is
    written before that matters, and a failure after ``zephyr.dts`` exists is
    tolerated.
    """
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True, exist_ok=True)

    cmake = shutil.which("cmake")
    if cmake is None:
        raise DtsError("cmake was not found on PATH")

    cmd = [
        cmake,
        "-GNinja",
        f"-B{out}",
        f"-S{app}",
        f"-DBOARD={board}",
        f"-DZEPHYR_BASE={zephyr_base}",
    ]

    proc = subprocess.run(
        cmd, cwd=str(out), env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT
    )

    dts = out / "zephyr" / "zephyr.dts"
    if not dts.is_file():
        raise DtsError(
            f"could not generate a devicetree for board '{board}'. Is the board "
            f"name right, and is the project's overlay present?\n\n{proc.stdout}"
        )
    return dts.read_text(encoding="utf-8", errors="replace")


def _body_from_open(text: str, open_index: int) -> str:
    """Return the text between the brace at open_index and its match."""
    depth = 0
    for index in range(open_index, len(text)):
        char = text[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[open_index + 1 : index]
    return ""


def _node_body(dts: str, label: str) -> str:
    """Extract the body of a node by its devicetree label."""
    match = re.search(rf"^\s*{re.escape(label)}\s*:\s*[\w@\-]+\s*\{{", dts, re.MULTILINE)
    if match is None:
        return ""
    return _body_from_open(dts, match.end() - 1)


def _prop(body: str, name: str) -> int | None:
    match = re.search(rf"^\s*{re.escape(name)}\s*=\s*<\s*(0x[0-9a-fA-F]+|\d+)\s*>", body, re.MULTILINE)
    if match is None:
        return None
    return int(match.group(1), 0)


def _string_prop(body: str, name: str) -> str | None:
    match = re.search(rf'^\s*{re.escape(name)}\s*=\s*"([^"]*)"', body, re.MULTILINE)
    return match.group(1) if match else None


def _bool_prop(body: str, name: str) -> bool:
    return re.search(rf"^\s*{re.escape(name)}\s*;", body, re.MULTILINE) is not None


def parse_target(dts: str, board: str) -> TargetInfo:
    """Read the panel, buttons and touch device out of a resolved devicetree."""
    info = TargetInfo(board=board, raw_dts=dts)

    chosen = re.search(r"zephyr,display\s*=\s*&([\w@\-]+)\s*;", dts)
    if chosen is not None:
        label = chosen.group(1)
        body = _node_body(dts, label)
        compatible = _string_prop(body, "compatible") or ""
        width = _prop(body, "width")
        height = _prop(body, "height")

        if width is not None and height is not None:
            monochrome = any(c in compatible for c in MONO_COMPATIBLES)
            # MONO10 is "1 = black, 0 = white", which is what these panels want
            # with LV_Z_COLOR_MONO_HW_INVERSION. MONO01 is the other way round.
            fmt = PANEL_FORMATS["MONO10"] if monochrome else PANEL_FORMATS["ARGB8888"]
            info.display = TargetDisplay(
                node=label,
                label=_string_prop(body, "label") or label,
                compatible=compatible,
                width=width,
                height=height,
                monochrome=monochrome,
                pixel_format=fmt,
            )

    # Buttons: children of every gpio-keys node.
    for match in re.finditer(r'compatible\s*=\s*"gpio-keys"', dts):
        # Walk back to the opening brace of the enclosing node.
        open_index = dts.rfind("{", 0, match.start())
        if open_index < 0:
            continue
        parent_body = _body_from_open(dts, open_index)

        # Children are the labelled nodes inside it.
        for child in re.finditer(r"^\s*([\w@\-]+)\s*:\s*[\w@\-]+\s*\{", parent_body, re.MULTILINE):
            child_open = parent_body.find("{", child.end() - 1)
            child_body = _body_from_open(parent_body, child_open)

            code = _prop(child_body, "zephyr,code")
            if code is None:
                continue

            info.buttons.append(
                TargetButton(
                    node=child.group(1),
                    label=_string_prop(child_body, "label") or child.group(1),
                    code=code,
                )
            )

    touch = re.search(r"zephyr,touch\s*=\s*&([\w@\-]+)\s*;", dts)
    if touch is not None:
        info.touch.append(touch.group(1))

    return info


def generate_overlay(info: TargetInfo, source_overlay: Path) -> str:
    """Build an overlay that reproduces the target's panel and buttons."""
    lines = [
        "/*",
        " * Generated by lvgl-preview. Do not edit.",
        " *",
        f" * Reproduces the devicetree of board '{info.board}' on the native",
        " * simulator: the same panel geometry, the same pixel format, and the same",
        " * button codes. Everything here came from that board's own devicetree.",
        " */",
        "",
        "#include <zephyr/dt-bindings/input/input-event-codes.h>",
        "#include <zephyr/dt-bindings/gpio/gpio.h>",
        "#include <zephyr/dt-bindings/display/panel.h>",
        "",
        "/ {",
    ]

    if info.display is not None:
        d = info.display
        lines += [
            "\tlvgl_preview_sink: lvgl_preview_sink {",
            '\t\tcompatible = "lvgl,preview-sink";',
            '\t\tstatus = "okay";',
            f"\t\twidth = <{d.width}>;",
            f"\t\theight = <{d.height}>;",
            f"\t\tpixel-format = <{d.pixel_format}>;",
        ]
        if d.monochrome:
            lines += [
                "\t\tmono-vtiled;",
                "\t\tmono-msb-first;",
            ]
        lines += ["\t};", ""]
    else:
        lines += [
            "\t/*",
            "\t * The target board selects no display, so the preview runs on the",
            "\t * simulator's own panel.",
            "\t */",
            "",
        ]

    if info.buttons:
        lines.append("\tbuttons {")
        lines.append('\t\tcompatible = "gpio-keys";')
        lines.append("\t\tpolling-mode;")
        lines.append("\t\tdebounce-interval-ms = <10>;")
        lines.append("")
        for index, button in enumerate(info.buttons):
            lines += [
                f"\t\tpreview_button_{index}: preview_button_{index} {{",
                # The simulator's emulated GPIO controller has no interrupts, so
                # polling is the only thing that works, and every pin is safe to
                # use because the keys are virtual.
                f"\t\t\tgpios = <&gpio0 {index} (GPIO_ACTIVE_LOW | GPIO_PULL_UP)>;",
                f"\t\t\tzephyr,code = <{button.code}>;",
                f'\t\t\tlabel = "{button.label}";',
                "\t\t};",
            ]
        lines += ["\t};", ""]

    lines += ["};", ""]

    if info.display is not None:
        lines += [
            "/ {",
            "\tchosen {",
            "\t\tzephyr,display = &lvgl_preview_sink;",
            "\t};",
            "};",
            "",
        ]

    return "\n".join(lines)


def read_target(
    app: Path, board: str, zephyr_base: Path, work_dir: Path, env: dict[str, str]
) -> TargetInfo:
    """Configure for ``board`` and return what its devicetree says."""
    dts = _configure_for_dts(app, board, zephyr_base, work_dir, env)
    return parse_target(dts, board)
