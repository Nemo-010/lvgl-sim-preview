"""The frame protocol shared with the device side.

The canonical definition lives in ``include/lvgl_preview_proto.h`` so that the C
and Python sides cannot drift apart. This module only decodes it.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from enum import IntEnum

MAGIC = 0x3150564C  # "LVP1"
VERSION = 2

# Field order and types of struct lvp_frame_header. `reserved[8]` is flattened.
_HEADER = struct.Struct(
    "<"
    "I"  # magic
    "I"  # version
    "I"  # header_size
    "I"  # seq
    "I"  # width
    "I"  # height
    "I"  # format
    "I"  # stride
    "I"  # frame_size
    "I"  # pixel_capacity
    "I"  # flags
    "I"  # hdr_flags
    "I"  # dirty_x1
    "I"  # dirty_y1
    "I"  # dirty_x2
    "I"  # dirty_y2
    "I"  # flushes
    "I"  # frames
    "I"  # last_flush_us
    "I"  # tick_ms
    "I"  # heartbeat_ms
    "I"  # app_ready
    "8I"  # reserved
    "32s"  # display_name
    "64s"  # device_name
    "64s"  # app_name
)

HEADER_SIZE = _HEADER.size

FLAG_MONO_VTILED = 1 << 0
FLAG_MONO_MSB_FIRST = 1 << 1
FLAG_MONO_INVERTED = 1 << 2
FLAG_TRUNCATED = 1 << 3
FLAG_PARTIAL = 1 << 4
FLAG_MONO_ONE_IS_WHITE = 1 << 5

HDR_FLAG_DISPLAY_ON = 1 << 0

INPUT_MAGIC = 0x3150494C  # "LIP1"
INPUT_CAPACITY = 64
INPUT_HEADER = struct.Struct("<6I4I")
INPUT_EVENT = struct.Struct("<IIiiII")  # type, seq, x, y, code, value


class Format(IntEnum):
    UNKNOWN = 0
    I1 = 1
    L8 = 2
    AL88 = 3
    RGB565 = 4
    RGB565_SWAPPED = 5
    RGB888 = 6
    ARGB8888 = 7
    XRGB8888 = 8


class InputType(IntEnum):
    NONE = 0
    POINTER = 1
    KEY = 2
    ENCODER = 3


@dataclass(frozen=True)
class Header:
    magic: int
    version: int
    header_size: int
    seq: int
    width: int
    height: int
    format: int
    stride: int
    frame_size: int
    pixel_capacity: int
    flags: int
    hdr_flags: int
    dirty_x1: int
    dirty_y1: int
    dirty_x2: int
    dirty_y2: int
    flushes: int
    frames: int
    last_flush_us: int
    tick_ms: int
    heartbeat_ms: int
    app_ready: int
    display_name: str
    device_name: str
    app_name: str

    @property
    def fmt(self) -> Format:
        try:
            return Format(self.format)
        except ValueError:
            return Format.UNKNOWN

    @property
    def mono_vtiled(self) -> bool:
        return bool(self.flags & FLAG_MONO_VTILED)

    @property
    def mono_msb_first(self) -> bool:
        return bool(self.flags & FLAG_MONO_MSB_FIRST)

    @property
    def mono_inverted(self) -> bool:
        return bool(self.flags & FLAG_MONO_INVERTED)

    @property
    def mono_one_is_white(self) -> bool:
        """True when a set bit means a lit pixel."""
        return bool(self.flags & FLAG_MONO_ONE_IS_WHITE)

    @property
    def truncated(self) -> bool:
        return bool(self.flags & FLAG_TRUNCATED)

    @property
    def display_on(self) -> bool:
        return bool(self.hdr_flags & HDR_FLAG_DISPLAY_ON)

    def describe(self) -> str:
        return (
            f"{self.width}x{self.height} {self.fmt.name} stride={self.stride} "
            f"frame={self.frame_size}B capacity={self.pixel_capacity}B"
        )


def decode_header(buf: bytes) -> Header:
    if len(buf) < HEADER_SIZE:
        raise ValueError(f"short header: {len(buf)} < {HEADER_SIZE}")

    fields = _HEADER.unpack_from(buf, 0)

    def text(raw: bytes) -> str:
        return raw.split(b"\0", 1)[0].decode("utf-8", "replace")

    return Header(
        *fields[:22],
        display_name=text(fields[30]),
        device_name=text(fields[31]),
        app_name=text(fields[32]),
    )
