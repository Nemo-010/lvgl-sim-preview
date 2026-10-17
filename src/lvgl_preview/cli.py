"""Command line entry points for the preview system."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from . import __version__
from .build import BuildError, build
from .capture import Capture, CaptureError
from .device import Device, PreviewError
from .render import RenderError, decode
from .protocol import decode_header


def _cmd_build(args: argparse.Namespace) -> int:
    try:
        result = build(
            args.app,
            build_dir=args.build_dir,
            board=args.board,
            target_board=args.target_board,
            zephyr_base=args.zephyr_base,
            extra_config=args.config,
            pristine=args.pristine,
        )
    except BuildError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(f"built {result.executable}")
    if result.target is not None:
        print(f"  target    {result.target.summary()}")
        for button in result.target.buttons:
            print(f"            button {button.label!r} code {button.code}")
    print(f"  capture   {result.capture}")
    print(f"  manifest  {result.manifest}")
    return 0


def _cmd_drive(args: argparse.Namespace) -> int:
    """Exercise the input path and show the result.

    This exists so the claim "buttons work" can be checked by a script rather
    than by eye: it presses the codes the manifest reports and then captures the
    frame the application drew in response.
    """
    try:
        result = build(
            args.app,
            build_dir=args.build_dir,
            board=args.board,
            target_board=args.target_board,
            zephyr_base=args.zephyr_base,
            extra_config=args.config,
            pristine=args.pristine,
        )
    except BuildError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    device = Device(
        result.executable,
        work_dir=args.work_dir or result.build_dir,
        capture=str(result.capture),
        input_ring=str(result.input_ring),
        manifest=str(result.manifest),
    )

    device.start()
    status = 0
    try:
        device.wait_ready(timeout=args.timeout)
        manifest = device.read_manifest(timeout=min(args.timeout, 10.0))
        device.wait_first_frame(timeout=args.timeout)

        print(f"display   {manifest.width}x{manifest.height} {manifest.format}")
        if manifest.buttons:
            print("buttons   " + ", ".join(
                f"{b.get('label') or b.get('node')}={b.get('code')}" for b in manifest.buttons
            ))

        codes = [int(part) for part in args.keys.split(",") if part.strip()]
        for code in codes:
            print(f"press     {code}")
            device.send_key(code, pressed=True)
            time.sleep(args.settle)
            device.send_key(code, pressed=False)
            time.sleep(args.settle)

        if args.tap:
            x_text, _, y_text = args.tap.partition(",")
            print(f"tap       {x_text},{y_text}")
            device.send_pointer(int(x_text), int(y_text), pressed=True)
            time.sleep(args.settle)
            device.send_pointer(int(x_text), int(y_text), pressed=False)
            time.sleep(args.settle)

        if args.output:
            capture = Capture(device.capture_path)
            frame = capture.read_frame()
            frame.image.to_png(args.output, scale=args.scale)
            print(f"frame     {args.output} (seq {frame.header.seq})")
    except (PreviewError, CaptureError, RenderError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        status = 1
    finally:
        out = device.stop()
        if out.strip() and status != 0:
            print("\n--- device output ---")
            print(out.rstrip())

    return status


def _cmd_info(args: argparse.Namespace) -> int:
    capture = Capture(args.capture)
    try:
        capture.wait_for_file(args.timeout)
        header = capture.read_header()
    except CaptureError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(f"capture   {capture.path}")
    print(f"  device   {header.device_name}")
    print(f"  app      {header.app_name}")
    print(f"  geometry {header.describe()}")
    print(f"  frames   {header.frames} ({header.flushes} flushes)")
    print(f"  display  {'on' if header.display_on else 'blanked'}")
    print(f"  uptime   {header.tick_ms} ms")
    if header.mono_inverted:
        print("  mono     hardware inverted")
    return 0


def _cmd_render(args: argparse.Namespace) -> int:
    capture = Capture(args.capture)
    try:
        capture.wait_for_file(args.timeout)
        if args.wait:
            header = capture.read_header()
            capture.wait_for_seq_change(header.seq, args.timeout)
        result = capture.read_frame()
    except (CaptureError, RenderError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    result.image.to_png(args.output, scale=args.scale)
    print(f"wrote {args.output} ({result.image.width}x{result.image.height}, seq {result.header.seq})")
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    try:
        result = build(
            args.app,
            build_dir=args.build_dir,
            board=args.board,
            target_board=args.target_board,
            zephyr_base=args.zephyr_base,
            extra_config=args.config,
            pristine=args.pristine,
        )
    except BuildError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    device = Device(
        result.executable,
        work_dir=args.work_dir or result.build_dir,
        capture=str(result.capture),
        input_ring=str(result.input_ring),
        manifest=str(result.manifest),
    )

    device.start(args.sim_arg)
    status = 0
    try:
        device.wait_ready(timeout=args.timeout)
        manifest = device.read_manifest(timeout=min(args.timeout, 10.0))
        device.wait_first_frame(timeout=args.timeout)

        print(f"device    {manifest.app or '?'} on {manifest.board or '?'} (zephyr {manifest.zephyr or '?'})")
        print(f"display   {manifest.width}x{manifest.height} {manifest.format}")
        if manifest.buttons:
            print("buttons   " + ", ".join(
                f"{b.get('label') or b.get('node')} (code {b.get('code')})" for b in manifest.buttons
            ))
        else:
            print("buttons   none found in devicetree")

        if args.output:
            capture = Capture(device.capture_path)
            frame = capture.read_frame()
            frame.image.to_png(args.output, scale=args.scale)
            print(f"frame     {args.output} (seq {frame.header.seq})")

        if args.hold:
            deadline = time.monotonic() + args.hold
            while time.monotonic() < deadline and device.is_running():
                time.sleep(0.1)
    except (PreviewError, CaptureError, RenderError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        status = 1
    finally:
        out = device.stop()
        if out.strip() and (status != 0 or args.verbose):
            print("\n--- device output ---")
            print(out.rstrip())

    return status


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="lvgl-preview",
        description="Preview a Zephyr LVGL application on the desktop.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("app", help="path to the Zephyr application")
        p.add_argument("-b", "--build-dir", default=None)
        p.add_argument("--board", default="native_sim/native/64")
        p.add_argument("-t", "--target-board", default=None,
                       help="the board the application really targets, e.g. frdm_mcxa156. "
                            "Its devicetree decides the emulated panel and buttons.")
        p.add_argument("--zephyr-base", default=None)
        p.add_argument("-c", "--config", action="append", default=[],
                       help="extra Kconfig fragment, repeatable")
        p.add_argument("-p", "--pristine", action="store_true")
        p.add_argument("--timeout", type=float, default=20.0)

    p = sub.add_parser("build", help="build the application with the preview attached")
    common(p)
    p.set_defaults(func=_cmd_build)

    p = sub.add_parser("run", help="build, run and report what the device rendered")
    common(p)
    p.add_argument("-o", "--output", default=None, help="write the first frame as PNG")
    p.add_argument("--scale", type=int, default=1)
    p.add_argument("--hold", type=float, default=0.0, help="keep running for N seconds")
    p.add_argument("--work-dir", default=None)
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--sim-arg", action="append", default=[],
                   help="extra argument passed to the simulator, repeatable")
    p.set_defaults(func=_cmd_run)

    p = sub.add_parser("drive", help="build, run and send scripted input to the device")
    common(p)
    p.add_argument("-o", "--output", default=None, help="write a PNG after the input")
    p.add_argument("--scale", type=int, default=1)
    p.add_argument("--work-dir", default=None)
    p.add_argument("--keys", default="",
                   help="comma separated input key codes to press, e.g. 11,11,2")
    p.add_argument("--tap", default=None,
                   help="tap at X,Y, e.g. 64,32")
    p.add_argument("--settle", type=float, default=0.6,
                   help="seconds to wait after each event")
    p.set_defaults(func=_cmd_drive)

    p = sub.add_parser("info", help="describe a capture file")
    p.add_argument("capture")
    p.add_argument("--timeout", type=float, default=5.0)
    p.set_defaults(func=_cmd_info)

    p = sub.add_parser("render", help="render a capture to PNG")
    p.add_argument("capture")
    p.add_argument("-o", "--output", default="frame.png")
    p.add_argument("--scale", type=int, default=1)
    p.add_argument("--wait", action="store_true", help="wait for the next frame")
    p.add_argument("--timeout", type=float, default=10.0)
    p.set_defaults(func=_cmd_render)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
