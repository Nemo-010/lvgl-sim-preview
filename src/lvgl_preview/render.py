"""Decode a captured frame into RGB pixels.

The panel layouts this handles are the ones the Zephyr display API actually
uses. Each format is decoded from the raw panel bytes, not from an LVGL buffer,
because what the preview promises to show is what the panel was sent.
"""

from __future__ import annotations

from dataclasses import dataclass

from .protocol import Format, Header


class RenderError(RuntimeError):
    """Raised when a frame cannot be decoded."""


@dataclass
class Image:
    width: int
    height: int
    # Row-major RGB888.
    rgb: bytearray

    def pixel(self, x: int, y: int) -> tuple[int, int, int]:
        off = (y * self.width + x) * 3
        return self.rgb[off], self.rgb[off + 1], self.rgb[off + 2]

    def to_png(self, path: str, scale: int = 1) -> None:
        """Write a PNG, upscaling with nearest-neighbour if asked."""
        from PIL import Image as PILImage

        img = PILImage.frombytes("RGB", (self.width, self.height), bytes(self.rgb))
        if scale != 1:
            img = img.resize(
                (self.width * scale, self.height * scale), PILImage.NEAREST
            )
        img.save(path)


def _decode_i1(data: bytes, header: Header) -> bytearray:
    """1bpp monochrome, either packed per row or per 8-row page."""
    out = bytearray(header.width * header.height * 3)
    stride = header.stride or (
        header.width if header.mono_vtiled else (header.width + 7) // 8
    )
    one_is_white = header.mono_one_is_white

    for y in range(header.height):
        for x in range(header.width):
            if header.mono_vtiled:
                # One byte per column, one page per 8 rows.
                index = (y // 8) * stride + x
                bit = y % 8
            else:
                index = y * stride + x // 8
                bit = x % 8

            if index >= len(data):
                raise RenderError(
                    f"frame is too small for {header.width}x{header.height} "
                    f"monochrome data (needed byte {index}, have {len(data)})"
                )

            byte = data[index]
            set_bit = bool(byte & (0x80 >> bit) if header.mono_msb_first else byte & (1 << bit))
            lit = set_bit if one_is_white else not set_bit

            off = (y * header.width + x) * 3
            value = 255 if lit else 0
            out[off] = out[off + 1] = out[off + 2] = value

    return out


def _decode_l8(data: bytes, header: Header) -> bytearray:
    out = bytearray(header.width * header.height * 3)
    stride = header.stride or header.width

    for y in range(header.height):
        row = y * stride
        for x in range(header.width):
            value = data[row + x]
            off = (y * header.width + x) * 3
            out[off] = out[off + 1] = out[off + 2] = value

    return out


def _decode_al88(data: bytes, header: Header) -> bytearray:
    out = bytearray(header.width * header.height * 3)
    stride = header.stride or header.width * 2

    for y in range(header.height):
        row = y * stride
        for x in range(header.width):
            base = row + x * 2
            luminance = data[base]
            off = (y * header.width + x) * 3
            out[off] = out[off + 1] = out[off + 2] = luminance

    return out


def _decode_rgb565(data: bytes, header: Header, swap: bool) -> bytearray:
    out = bytearray(header.width * header.height * 3)
    stride = header.stride or header.width * 2

    for y in range(header.height):
        row = y * stride
        for x in range(header.width):
            base = row + x * 2
            if swap:
                value = (data[base] << 8) | data[base + 1]
            else:
                value = data[base] | (data[base + 1] << 8)

            r = (value >> 11) & 0x1F
            g = (value >> 5) & 0x3F
            b = value & 0x1F

            off = (y * header.width + x) * 3
            out[off] = (r << 3) | (r >> 2)
            out[off + 1] = (g << 2) | (g >> 4)
            out[off + 2] = (b << 3) | (b >> 2)

    return out


def _decode_rgb888(data: bytes, header: Header) -> bytearray:
    """LVGL's RGB888 is stored BGR, and the display API keeps that order."""
    out = bytearray(header.width * header.height * 3)
    stride = header.stride or header.width * 3

    for y in range(header.height):
        row = y * stride
        for x in range(header.width):
            base = row + x * 3
            off = (y * header.width + x) * 3
            out[off] = data[base + 2]
            out[off + 1] = data[base + 1]
            out[off + 2] = data[base]

    return out


def _decode_xrgb8888(data: bytes, header: Header) -> bytearray:
    """32bpp, little-endian 0x00RRGGBB."""
    out = bytearray(header.width * header.height * 3)
    stride = header.stride or header.width * 4

    for y in range(header.height):
        row = y * stride
        for x in range(header.width):
            base = row + x * 4
            off = (y * header.width + x) * 3
            out[off] = data[base + 2]
            out[off + 1] = data[base + 1]
            out[off + 2] = data[base]

    return out


def decode(data: bytes, header: Header) -> Image:
    """Decode the pixel area of a frame into RGB888."""
    if header.width == 0 or header.height == 0:
        raise RenderError("frame has no geometry; the sink has not rendered yet")

    if header.frame_size > len(data):
        raise RenderError(
            f"frame claims {header.frame_size} bytes but only {len(data)} were "
            f"mapped; the capture file was probably truncated"
        )

    payload = data[: header.frame_size]

    if header.fmt == Format.I1:
        rgb = _decode_i1(payload, header)
    elif header.fmt == Format.L8:
        rgb = _decode_l8(payload, header)
    elif header.fmt == Format.AL88:
        rgb = _decode_al88(payload, header)
    elif header.fmt == Format.RGB565:
        rgb = _decode_rgb565(payload, header, swap=False)
    elif header.fmt == Format.RGB565_SWAPPED:
        rgb = _decode_rgb565(payload, header, swap=True)
    elif header.fmt == Format.RGB888:
        rgb = _decode_rgb888(payload, header)
    elif header.fmt in (Format.ARGB8888, Format.XRGB8888):
        rgb = _decode_xrgb8888(payload, header)
    else:
        raise RenderError(
            f"unsupported pixel format {header.fmt.name} ({header.format}); "
            f"the sink reported it but this renderer cannot decode it"
        )

    return Image(width=header.width, height=header.height, rgb=rgb)
