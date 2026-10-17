"""Watch a capture file and serve it to viewers.

The device publishes frames by bumping ``seq``. This reads the file the same way
any other viewer would, which keeps the transport honest: the preview never
depends on private knowledge of the device's state.
"""

from __future__ import annotations

import struct
import time
from dataclasses import dataclass
from pathlib import Path

from .protocol import HEADER_SIZE, MAGIC, VERSION, Header, decode_header
from .render import Image, decode

# How long to wait for a frame before deciding the device is not going to send
# one. Generous, because a fresh build can take a moment to reach lv_timer.
FIRST_FRAME_TIMEOUT = 20.0


class CaptureError(RuntimeError):
    """Raised when the capture file cannot be used."""


@dataclass
class WatchResult:
    header: Header
    image: Image


class Capture:
    """A reader for one capture file."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._size = 0

    def wait_for_file(self, timeout: float = FIRST_FRAME_TIMEOUT) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.path.is_file() and self.path.stat().st_size >= HEADER_SIZE:
                return
            time.sleep(0.05)
        raise CaptureError(
            f"{self.path} did not appear within {timeout:.0f}s. The device "
            f"never started, or it is writing somewhere else."
        )

    def read_header(self) -> Header:
        try:
            with self.path.open("rb") as f:
                raw = f.read(HEADER_SIZE)
        except OSError as exc:
            raise CaptureError(f"{self.path} cannot be read: {exc}") from None

        if len(raw) < HEADER_SIZE:
            raise CaptureError(
                f"{self.path} is only {len(raw)} bytes; the sink did not finish "
                f"initialising"
            )

        header = decode_header(raw)

        if header.magic != MAGIC:
            raise CaptureError(
                f"{self.path} is not a preview capture (magic {header.magic:#x}, "
                f"expected {MAGIC:#x})"
            )
        if header.version != VERSION:
            raise CaptureError(
                f"capture was written by protocol version {header.version}, this "
                f"tool speaks {VERSION}"
            )
        if header.header_size != HEADER_SIZE:
            raise CaptureError(
                f"capture header is {header.header_size} bytes, this tool expects "
                f"{HEADER_SIZE}"
            )

        return header

    def read_frame(self, header: Header | None = None) -> WatchResult:
        """Read one consistent frame.

        The device writes the header, then the pixels, then bumps ``seq``. So a
        frame is only trusted when ``seq`` is unchanged across the whole read.
        Without this a viewer can show a torn frame during a partial refresh.
        """
        header = header or self.read_header()

        for _ in range(50):
            with self.path.open("rb") as f:
                before = f.read(HEADER_SIZE)
                f.seek(header.header_size)
                pixels = f.read(header.pixel_capacity)
                f.seek(0)
                after = f.read(HEADER_SIZE)

            if before == after:
                current = decode_header(after)
                if current.seq != header.seq or current.width != header.width:
                    # The device moved on; re-read with the newer header.
                    header = current
                    continue
                return WatchResult(header=current, image=decode(pixels, current))

        raise CaptureError("could not get a stable frame; the device is refreshing too fast")

    def wait_for_seq_change(self, last_seq: int, timeout: float) -> Header | None:
        """Block until the device publishes a new frame, or time out."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                header = self.read_header()
            except CaptureError:
                time.sleep(0.05)
                continue

            if header.seq != last_seq:
                return header
            if header.heartbeat_ms == 0:
                time.sleep(0.05)
                continue
            time.sleep(0.01)

        return None

    def device_alive(self, timeout: float = 5.0) -> bool:
        """True while the device keeps bumping its heartbeat."""
        try:
            first = self.read_header()
        except CaptureError:
            return False

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                if self.read_header().heartbeat_ms != first.heartbeat_ms:
                    return True
            except CaptureError:
                return False
            time.sleep(0.05)

        return False
