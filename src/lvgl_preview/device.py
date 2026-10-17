"""The host side of the preview: run the device, show what it renders."""

from __future__ import annotations

import os
import struct
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from .capture import FIRST_FRAME_TIMEOUT, Capture, CaptureError
from .protocol import (
    INPUT_CAPACITY,
    INPUT_EVENT,
    INPUT_HEADER,
    INPUT_MAGIC,
    VERSION,
    InputType,
)

# The emulated display needs a video driver. `dummy` makes the device render
# into its own buffers without opening a window, which is what a headless
# preview wants: the pixels still travel the full display API path.
#
# A video driver alone is not enough. SDL also picks a *render* driver, and its
# default choice needs a GPU context, so in a container or on a CI runner it
# fails with "Couldn't find matching render driver" and the device exits before
# any application code runs. Naming the software renderer explicitly is what
# makes a headless run work anywhere.
DEFAULT_SDL_VIDEODRIVER = "dummy"
DEFAULT_SDL_RENDER_DRIVER = "software"

# How long to wait for the manifest before assuming there is not one.
MANIFEST_TIMEOUT = 10.0


@dataclass
class DeviceManifest:
    board: str = ""
    zephyr: str = ""
    app: str = ""
    width: int = 0
    height: int = 0
    format: str = "UNKNOWN"
    buttons: list[dict] | None = None
    touch: list[dict] | None = None
    raw: dict | None = None

    @property
    def button_codes(self) -> list[int]:
        return [int(b.get("code", 0)) for b in (self.buttons or [])]


class PreviewError(RuntimeError):
    """Raised when the device cannot be started or produces nothing usable."""


class Device:
    """A running preview device, plus the two files it shares with the host."""

    def __init__(
        self,
        executable: str | Path,
        *,
        work_dir: str | Path | None = None,
        capture: str = "lvgl_preview_frame.bin",
        input_ring: str = "lvgl_preview_input.bin",
        manifest: str = "lvgl_preview_manifest.json",
    ):
        self.executable = Path(executable).resolve()
        if not self.executable.is_file():
            raise PreviewError(f"{self.executable} does not exist")

        self.work_dir = Path(work_dir).resolve() if work_dir else self.executable.parent
        self.work_dir.mkdir(parents=True, exist_ok=True)

        self.capture_path = self.work_dir / capture
        self.input_path = self.work_dir / input_ring
        self.manifest_path = self.work_dir / manifest

        self._proc: subprocess.Popen | None = None
        self._input_fd: int | None = None
        self._input_seq = 0
        self.capture = Capture(self.capture_path)
        self.manifest = DeviceManifest()

    # -------------------------------------------------------------- lifecycle

    def start(self, extra_args: list[str] | None = None) -> None:
        """Start the device.

        The capture and input files are removed first so that a stale frame from
        an earlier run cannot be mistaken for output from this one.
        """
        for path in (self.capture_path, self.input_path, self.manifest_path):
            path.unlink(missing_ok=True)

        env = dict(os.environ)
        env.setdefault("SDL_VIDEODRIVER", DEFAULT_SDL_VIDEODRIVER)
        env.setdefault("SDL_RENDER_DRIVER", DEFAULT_SDL_RENDER_DRIVER)

        cmd = [str(self.executable), "-no-rt", *list(extra_args or [])]
        self._proc = subprocess.Popen(
            cmd,
            cwd=str(self.work_dir),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )

    def stop(self, timeout: float = 5.0) -> str:
        """Stop the device and return whatever it printed."""
        if self._proc is None:
            return ""

        self.close_input()

        proc = self._proc
        self._proc = None
        output = ""

        if proc.stdout is not None and not proc.stdout.closed:
            try:
                if proc.poll() is None:
                    proc.terminate()
                output = proc.communicate(timeout=timeout)[0] or ""
            except subprocess.TimeoutExpired:
                proc.kill()
                output = proc.communicate()[0] or ""
            except ValueError:
                # Already reaped elsewhere.
                output = ""

        return output

    def is_running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def captured_output(self) -> str:
        """Return output produced so far without stopping the device.

        Reading a live pipe would block, so the device is asked to stop and the
        caller is told to restart it if it still needs it.
        """
        return self.stop()

    def output(self) -> str:
        """Whatever the device has printed so far, without stopping it."""
        if self._proc is None or self._proc.stdout is None:
            return ""
        return ""

    def __enter__(self) -> "Device":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()

    # ---------------------------------------------------------------- reading

    def wait_ready(self, timeout: float = FIRST_FRAME_TIMEOUT) -> None:
        """Wait until the device has a geometry to report.

        This is the point at which the sink has mapped the capture file and read
        the panel's capabilities. It is not the same as "has drawn something".
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._proc is not None and self._proc.poll() is not None:
                raise PreviewError(
                    "the device exited before it initialised the preview sink. "
                    "Its own output is below.\n\n" + self.stop()
                )
            try:
                header = self.capture.read_header()
            except CaptureError:
                time.sleep(0.05)
                continue
            except OSError:
                # The file has not been created yet.
                time.sleep(0.05)
                continue

            if header.width > 0 and header.height > 0:
                return
            time.sleep(0.05)

        raise PreviewError(
            f"the device did not report a display geometry within {timeout:.0f}s. "
            f"It started, but the preview sink never initialised; check the "
            f"device output for 'lvgl-preview' lines."
        )

    def wait_first_frame(self, timeout: float = FIRST_FRAME_TIMEOUT) -> None:
        """Wait until the device has published at least one frame."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                header = self.capture.read_header()
            except (CaptureError, OSError):
                time.sleep(0.05)
                continue

            if header.frames > 0:
                return

            if self._proc is not None and self._proc.poll() is not None:
                raise PreviewError(
                    "the device exited without rendering a frame. The "
                    "application probably returned early; its own output is "
                    "below.\n\n" + self.stop()
                )

            time.sleep(0.02)

        try:
            header = self.capture.read_header()
        except (CaptureError, OSError):
            raise PreviewError(
                f"no capture file was created within {timeout:.0f}s. The device "
                f"never initialised the preview sink."
            ) from None

        raise PreviewError(
            f"no frame was published within {timeout:.0f}s. The sink is "
            f"initialised ({header.describe()}) but LVGL never flushed. This "
            f"usually means the application never calls lv_timer_handler() and "
            f"CONFIG_LV_Z_RUN_LVGL_ON_WORKQUEUE is not set, or the display was "
            f"never unblanked."
        )

    def read_manifest(self, timeout: float = MANIFEST_TIMEOUT) -> DeviceManifest:
        import json

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.manifest_path.is_file():
                try:
                    raw = json.loads(self.manifest_path.read_text(encoding="utf-8"))
                except json.JSONDecodeError:
                    time.sleep(0.05)
                    continue

                display = raw.get("display", {})
                self.manifest = DeviceManifest(
                    board=raw.get("board", ""),
                    zephyr=raw.get("zephyr", ""),
                    app=raw.get("app", ""),
                    width=int(display.get("width", 0)),
                    height=int(display.get("height", 0)),
                    format=display.get("format", "UNKNOWN"),
                    buttons=raw.get("buttons", []),
                    touch=raw.get("touch", []),
                    raw=raw,
                )
                return self.manifest
            time.sleep(0.05)

        return self.manifest

    # ---------------------------------------------------------------- input

    def open_input(self) -> None:
        """Open the input ring for writing, initialising it if needed."""
        if self._input_fd is not None:
            return

        if not self.input_path.is_file():
            raise PreviewError(
                f"{self.input_path} does not exist. The device creates it when "
                f"CONFIG_LVGL_PREVIEW_INPUT is enabled."
            )

        self._input_fd = os.open(self.input_path, os.O_RDWR)

        header = os.pread(self._input_fd, INPUT_HEADER.size, 0)
        magic, version, header_size, head, tail, capacity = struct.unpack_from(
            "<6I", header, 0
        )

        if magic != INPUT_MAGIC or header_size != INPUT_HEADER.size:
            raise PreviewError(
                f"{self.input_path} is not an input ring (magic {magic:#x})"
            )

        if version != VERSION:
            raise PreviewError(
                f"input ring is version {version}, this tool speaks {VERSION}"
            )

    def close_input(self) -> None:
        if self._input_fd is not None:
            os.close(self._input_fd)
            self._input_fd = None

    def send_pointer(self, x: int, y: int, pressed: bool) -> None:
        self._send(InputType.POINTER, x=x, y=y, value=1 if pressed else 0)

    def send_key(self, code: int, pressed: bool = True) -> None:
        self._send(InputType.KEY, code=code, value=1 if pressed else 0)

    def send_encoder(self, delta: int) -> None:
        self._send(InputType.ENCODER, value=delta)

    def _send(self, kind: InputType, *, x: int = 0, y: int = 0, code: int = 0, value: int = 0) -> None:
        self.open_input()
        assert self._input_fd is not None

        head = struct.unpack_from("<I", os.pread(self._input_fd, 4, 12), 0)[0]
        tail = struct.unpack_from("<I", os.pread(self._input_fd, 4, 16), 0)[0]

        if head - tail >= INPUT_CAPACITY:
            # The device has not drained the ring. Dropping the event is better
            # than blocking the UI thread, but the caller should know.
            raise PreviewError("the input ring is full; the device is not reading it")

        slot = head % INPUT_CAPACITY
        offset = INPUT_HEADER.size + slot * INPUT_EVENT.size
        self._input_seq += 1

        event = INPUT_EVENT.pack(kind.value, self._input_seq, x, y, code, value)
        os.pwrite(self._input_fd, event, offset)
        os.pwrite(self._input_fd, struct.pack("<I", head + 1), 12)


def dump_output(device: Device, label: str) -> str:
    """Format the device's own diagnostics for a terminal."""
    return f"--- {label} ---\n{device.output()}"
