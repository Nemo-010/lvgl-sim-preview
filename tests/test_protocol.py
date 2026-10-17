"""Tests for the parts that can be checked without a Zephyr build."""

from __future__ import annotations

import struct

import pytest

from lvgl_preview import protocol
from lvgl_preview.dts import parse_target, generate_overlay
from lvgl_preview.render import RenderError, decode


def make_header(**overrides) -> protocol.Header:
    fields = dict(
        magic=protocol.MAGIC,
        version=protocol.VERSION,
        header_size=protocol.HEADER_SIZE,
        seq=1,
        width=8,
        height=8,
        format=protocol.Format.I1,
        stride=8,
        frame_size=8,
        pixel_capacity=8,
        flags=protocol.FLAG_MONO_VTILED | protocol.FLAG_MONO_MSB_FIRST,
        hdr_flags=protocol.HDR_FLAG_DISPLAY_ON,
        dirty_x1=0,
        dirty_y1=0,
        dirty_x2=7,
        dirty_y2=7,
        flushes=1,
        frames=1,
        last_flush_us=10,
        tick_ms=100,
        heartbeat_ms=100,
        app_ready=1,
        display_name="t",
        device_name="d",
        app_name="a",
    )
    fields.update(overrides)
    return protocol.Header(**fields)


def test_header_round_trip():
    raw = struct.pack(
        "<III", protocol.MAGIC, protocol.VERSION, protocol.HEADER_SIZE
    ).ljust(protocol.HEADER_SIZE, b"\0")
    header = protocol.decode_header(raw)
    assert header.magic == protocol.MAGIC
    assert header.header_size == protocol.HEADER_SIZE


def test_header_size_matches_c_struct():
    # The C side asserts sizeof(struct lvp_frame_header) == 280. If this fails,
    # the two sides have drifted and every frame will be misread.
    assert protocol.HEADER_SIZE == 280


def test_input_event_size_matches_c_struct():
    # The C side asserts sizeof(struct lvp_input_event) == 24.
    assert protocol.INPUT_EVENT.size == 24


def test_mono_vtiled_msb_first_renders_pixels():
    """A set bit with bit 7 first, in the top-left page, lights the top pixel."""
    header = make_header(flags=protocol.FLAG_MONO_VTILED
                         | protocol.FLAG_MONO_MSB_FIRST
                         | protocol.FLAG_MONO_ONE_IS_WHITE)
    # One byte per column per page: byte 0 covers column 0, rows 0..7.
    data = bytearray(8)
    data[0] = 0x80  # bit 7 -> row 0
    image = decode(bytes(data), header)

    assert image.pixel(0, 0) == (255, 255, 255)
    assert image.pixel(0, 1) == (0, 0, 0)


def test_mono_polarity_is_honoured():
    """The same bytes invert when a set bit means black."""
    inverted = make_header(flags=protocol.FLAG_MONO_VTILED | protocol.FLAG_MONO_MSB_FIRST)
    data = bytearray(8)
    data[0] = 0x80
    image = decode(bytes(data), inverted)

    assert image.pixel(0, 0) == (0, 0, 0)
    assert image.pixel(0, 1) == (255, 255, 255)


def test_row_tiled_mono_uses_one_bit_per_pixel():
    header = make_header(
        width=8,
        height=1,
        stride=1,
        frame_size=1,
        flags=protocol.FLAG_MONO_MSB_FIRST | protocol.FLAG_MONO_ONE_IS_WHITE,
    )
    image = decode(b"\xaa", header)

    # 0b10101010 -> alternating, starting lit.
    assert image.pixel(0, 0) == (255, 255, 255)
    assert image.pixel(1, 0) == (0, 0, 0)


def test_short_frame_is_reported_not_guessed():
    header = make_header(frame_size=8)
    with pytest.raises(RenderError):
        decode(b"\0\0", header)


def test_unknown_format_is_reported():
    header = make_header(format=protocol.Format.UNKNOWN, frame_size=1)
    with pytest.raises(RenderError):
        decode(b"\0", header)


SAMPLE_DTS = """
/ {
    chosen {
        zephyr,display = &ssd1306_128x64;
    };

    gpio_keys: gpio-keys {
        compatible = "gpio-keys";

        user_button_2: button_2 {
            label = "User SW2";
            zephyr,code = < 0xb >;
        };

        user_button_3: button_3 {
            label = "User SW3";
            zephyr,code = < 0x2 >;
        };
    };

    ssd1306_128x64: ssd1306@3c {
        compatible = "solomon,ssd1306";
        width = < 0x80 >;
        height = < 0x40 >;
    };
};
"""


def test_parse_target_reads_panel_and_buttons():
    info = parse_target(SAMPLE_DTS, "frdm_mcxa156")

    assert info.display is not None
    assert (info.display.width, info.display.height) == (128, 64)
    assert info.display.monochrome
    assert [b.code for b in info.buttons] == [11, 2]
    assert [b.label for b in info.buttons] == ["User SW2", "User SW3"]


def test_generated_overlay_reproduces_the_target():
    info = parse_target(SAMPLE_DTS, "frdm_mcxa156")
    text = generate_overlay(info, None)

    assert "width = <128>" in text
    assert "height = <64>" in text
    # MONO10 is PANEL_PIXEL_FORMAT_MONO10 == 4.
    assert "pixel-format = <4>" in text
    assert "zephyr,code = <11>" in text
    assert "zephyr,code = <2>" in text
    assert "zephyr,display = &lvgl_preview_sink" in text
