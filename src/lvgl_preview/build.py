"""Configure and build a Zephyr application for the native simulator.

The sink is attached by pointing ``ZEPHYR_EXTRA_MODULES`` at this repository and
passing the drop-in overlay through ``EXTRA_DTC_OVERLAY_FILE``, which Zephyr
applies after the application's own overlays. Nothing in the application is
modified.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .dts import PANEL_FORMATS, TargetInfo, generate_overlay, read_target

# Repository root: src/lvgl_preview/build.py -> ../../
ROOT = Path(__file__).resolve().parents[2]
OVERLAY = ROOT / "overlay" / "lvgl_preview.overlay"

# The sink has no compile-time geometry, so LVGL must size its buffers at
# runtime.
#
# LV_Z_RUN_LVGL_ON_WORKQUEUE is forced because most LVGL applications either
# call lv_timer_handler() themselves or, more often, forget to. An application
# that never calls it renders nothing at all, which would look exactly like a
# broken preview. Letting Zephyr drive LVGL is the one setting that makes the
# preview work regardless of what the application does, and it is safe when the
# application also calls lv_timer_handler(): LVGL is mutex-protected and both
# callers simply advance the same timers.
FORCED_CONFIG = (
    "CONFIG_LVGL_PREVIEW=y",
    "CONFIG_LV_Z_BUFFER_ALLOC_DYNAMIC=y",
    "CONFIG_LVGL_PREVIEW_INPUT=y",
    "CONFIG_LV_Z_RUN_LVGL_ON_WORKQUEUE=y",
    # The native simulator's default UART is a pseudoterminal. Creating one
    # fails in containers and CI runners that have no /dev/pts, which aborts the
    # run before any application code executes. Routing the console through the
    # process's own stdin/stdout removes that dependency entirely, and it also
    # means the device's diagnostics land in the same pipe the preview reads.
    "CONFIG_UART_NATIVE_PTY_0_ON_STDINOUT=y",
)

# A blank preview is almost always one of these. They are checked before the
# build is trusted, so the failure is reported with a reason instead of an
# empty window.
STATIC_BUFFER_MARKERS = (
    "CONFIG_LV_Z_BUFFER_ALLOC_STATIC=y",
)


class BuildError(RuntimeError):
    """Raised when the application cannot be configured or built."""


@dataclass
class BuildResult:
    build_dir: Path
    executable: Path
    manifest: Path
    capture: Path
    input_ring: Path
    config: dict[str, str] = field(default_factory=dict)
    log: str = ""
    target: TargetInfo | None = None
    overlay: Path | None = None


def find_zephyr_base(explicit: str | os.PathLike | None = None) -> Path:
    """Locate the Zephyr tree.

    Checked in order: the argument, ``ZEPHYR_BASE``, then a west workspace
    discovered by walking up from the current directory.
    """
    if explicit:
        path = Path(explicit).resolve()
        if not (path / "CMakeLists.txt").is_file():
            raise BuildError(f"{path} does not look like a Zephyr tree")
        return path

    env = os.environ.get("ZEPHYR_BASE")
    if env:
        return Path(env).resolve()

    here = Path.cwd().resolve()
    for parent in (here, *here.parents):
        if (parent / ".west" / "config").is_file():
            manifest = parent / "zephyr"
            if (manifest / "CMakeLists.txt").is_file():
                return manifest
    raise BuildError(
        "could not find Zephyr. Set ZEPHYR_BASE, or run from inside a west "
        "workspace, or pass --zephyr-base."
    )


def find_tool(name: str) -> str:
    path = shutil.which(name)
    if path is None:
        raise BuildError(
            f"{name} was not found on PATH. The native simulator builds with "
            f"the host compiler, so cmake, ninja and a C compiler are needed."
        )
    return path


# The LVGL colour format the build ended up with, as a short name.
_COLOR_FORMATS = {
    "CONFIG_LV_COLOR_FORMAT_I1": "MONO10",
    "CONFIG_LV_COLOR_FORMAT_L8": "L8",
    "CONFIG_LV_COLOR_FORMAT_RGB565": "RGB565",
    "CONFIG_LV_COLOR_FORMAT_RGB565_SWAPPED": "RGB565_SWAPPED",
    "CONFIG_LV_COLOR_FORMAT_RGB888": "RGB888",
    "CONFIG_LV_COLOR_FORMAT_XRGB8888": "ARGB8888",
    "CONFIG_LV_COLOR_FORMAT_ARGB8888": "ARGB8888",
}


def _lvgl_color_format(config: dict[str, str]) -> str | None:
    for key, name in _COLOR_FORMATS.items():
        if config.get(key) == "y":
            return name
    return None


def read_kconfig(dot_config: Path) -> dict[str, str]:
    """Parse a Zephyr .config into a dict."""
    values: dict[str, str] = {}
    if not dot_config.is_file():
        return values
    for line in dot_config.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line.startswith("CONFIG_"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key] = value.strip().strip('"')
    return values


def _run(cmd: list[str], cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        cwd=str(cwd),
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )


def build(
    app_dir: str | os.PathLike,
    *,
    build_dir: str | os.PathLike | None = None,
    board: str = "native_sim/native/64",
    target_board: str | None = None,
    zephyr_base: str | os.PathLike | None = None,
    extra_config: list[str] | None = None,
    pristine: bool = False,
    overlay: str | os.PathLike | None = None,
) -> BuildResult:
    """Configure and build ``app_dir`` with the preview sink attached.

    When ``target_board`` is given, that board's devicetree is read first and an
    overlay is generated from it, so the emulated panel has the same geometry and
    pixel format as the real one. Without it, the simulator's own panel is used.
    """
    app = Path(app_dir).resolve()
    if not (app / "CMakeLists.txt").is_file():
        raise BuildError(f"{app} has no CMakeLists.txt; is it a Zephyr application?")

    zb = find_zephyr_base(zephyr_base)
    out = Path(build_dir).resolve() if build_dir else (app / "build-preview")

    if pristine and out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True, exist_ok=True)

    env = dict(os.environ)
    env["ZEPHYR_BASE"] = str(zb)
    # The native simulator uses the host compiler, so no Zephyr SDK is needed.
    env.setdefault("ZEPHYR_TOOLCHAIN_VARIANT", "host")
    env["ZEPHYR_EXTRA_MODULES"] = str(ROOT)
    # Keep generated caches out of the user's home when a workspace-local
    # directory is available, which is what makes this reproducible in CI.
    env.setdefault("ZEPHYR_BASE", str(zb))

    cmake = find_tool("cmake")
    ninja = find_tool("ninja")

    # Resolve the target board's devicetree before configuring the simulator, so
    # the panel the preview renders is the panel the application was written for.
    target: TargetInfo | None = None
    overlay_path = Path(overlay).resolve() if overlay else OVERLAY

    if target_board:
        try:
            target = read_target(app, target_board, zb, out / f"target-dts-{target_board}", env)
        except Exception as exc:  # noqa: BLE001 - surfaced as a build error
            raise BuildError(
                f"could not read the devicetree of target board "
                f"'{target_board}': {exc}"
            ) from exc
    else:
        # No target board: emulate the simulator's own panel. Its geometry is
        # just as much a hardware fact as a real panel's, so it is read the same
        # way, and the same overlay generator runs.
        sim_board = board.split("/", 1)[0]

        try:
            target = read_target(app, sim_board, zb, out / f"target-dts-{sim_board}", env)
        except Exception:  # noqa: BLE001 - fall back to the plain overlay
            target = None

        if target is not None and target.display is None:
            # native_sim declares its display in the board's devicetree but with
            # no width/height, so fill in what the board actually advertises.
            from .dts import PANEL_FORMATS, TargetDisplay

            target.display = TargetDisplay(
                node="sdl_dc",
                label="simulator display",
                compatible="zephyr,sdl-dc",
                width=320,
                height=240,
                monochrome=False,
                pixel_format=PANEL_FORMATS["ARGB8888"],
            )

    generated = out / "lvgl_preview_generated.overlay"

    if target is not None:
        generated.write_text(generate_overlay(target, overlay_path), encoding="utf-8")
        overlay_path = generated
    else:
        # Remove a stale overlay so a reused build directory cannot silently
        # keep emulating the previous panel.
        generated.unlink(missing_ok=True)

    # LVGL's colour depth has to match the panel, or it renders in one format
    # and the sink composites in another. It is a property of the hardware, so
    # it is derived from the target's devicetree rather than asked of the user.
    config_args = list(FORCED_CONFIG) + list(extra_config or [])

    # Work out the pixel format to emulate.
    #
    # With a target board, its devicetree decides. Without one, the sink wraps
    # the simulator's own display, whose format is not known until the build
    # has run, so the application's own choice is left alone and simply checked
    # afterwards.
    panel_monochrome: bool | None = None

    if target is not None and target.display is not None:
        panel_monochrome = target.display.monochrome
    elif target is None:
        # No target board: the sink wraps the simulator's own display, which
        # native_sim defines as ARGB8888. LVGL has to render in that format or
        # the flush callback it picks will not match the data it is handed, and
        # nothing is ever drawn.
        panel_monochrome = False

    if panel_monochrome is not None:
        bits = 1 if panel_monochrome else 32
        config_args.append(f"CONFIG_LV_Z_BITS_PER_PIXEL={bits}")
        config_args.append(
            "CONFIG_LV_COLOR_FORMAT_I1=y" if bits == 1
            else "CONFIG_LV_COLOR_FORMAT_ARGB8888=y"
        )

    # LVGL's rendering buffer comes out of its memory pool. A full-refresh
    # ARGB8888 320x240 buffer is 300 KB, so an application that sized its pool
    # for a 1 KB monochrome panel (which is what every OLED application does)
    # fails to allocate and then never draws anything, silently, unless it
    # happened to enable LVGL logging. That is indistinguishable from "LVGL is
    # broken", so the preview sizes the pool from the panel it already read.
    if target is not None and target.display is not None:
        bytes_per_pixel = 1 if target.display.monochrome else 4
        frame_bytes = target.display.width * target.display.height * bytes_per_pixel
        # Two buffers plus room for the widget tree and font caches.
        pool = max(32768, frame_bytes * 2 + 32768)
        config_args.append(f"CONFIG_LV_Z_MEM_POOL_SIZE={pool}")
    config_fragment = out / "preview.conf"
    config_fragment.write_text("\n".join(config_args) + "\n", encoding="utf-8")

    configure = [
        cmake,
        "-GNinja",
        f"-DCMAKE_MAKE_PROGRAM={ninja}",
        f"-B{out}",
        f"-S{app}",
        f"-DBOARD={board}",
        f"-DZEPHYR_BASE={zb}",
        f"-DEXTRA_DTC_OVERLAY_FILE={overlay_path}",
        # EXTRA_CONF_FILE, not CONF_FILE: CONF_FILE replaces the application's
        # own prj.conf, which would silently drop every setting the project
        # relies on. EXTRA_CONF_FILE is applied on top of it.
        f"-DEXTRA_CONF_FILE={config_fragment}",
        f"-DZEPHYR_EXTRA_MODULES={ROOT}",
    ]

    # LVGL 9.6 deprecates the LV_COLOR_DEPTH choice in favour of
    # LV_COLOR_FORMAT_DEFAULT, and CONFIG_LV_COLOR_DEPTH_* no longer selects the
    # format in Zephyr (see the 4.5 migration guide). LV_Z_COLOR_FORMAT is the
    # board-level hook that provides the choice's default, so it is the one that
    # can be set from a .conf fragment.

    proc = _run(configure, out, env)
    if proc.returncode != 0:
        raise BuildError(
            "cmake configure failed. This usually means the application's "
            "devicetree has no display, or LVGL is not enabled.\n\n"
            + proc.stdout
        )

    dot_config = out / "zephyr" / ".config"
    config = read_kconfig(dot_config)

    for marker in STATIC_BUFFER_MARKERS:
        if marker in config:
            raise BuildError(
                f"the application forces {marker}, but the preview sink has no "
                "compile-time geometry. Remove that setting, or state width and "
                "height on the lvgl,preview-sink node in a project overlay."
            )

    # LVGL renders in the format the application asked for, and the sink
    # composites in the format the *panel* wants. If those disagree, LVGL draws
    # 16bpp into a 32bpp buffer and every frame is silently wrong: the classic
    # "the preview is blank / garbled" failure. Refuse to run instead.
    rendered = _lvgl_color_format(config)

    if panel_monochrome is not None and rendered is not None:
        expected = "MONO10" if panel_monochrome else "ARGB8888"
        if rendered != expected:
            raise BuildError(
                f"the application asks LVGL to render in {rendered}, but the panel "
                f"is {expected}. The sink captures what the panel would receive, so "
                f"these have to agree.\n\n"
                f"Either remove the colour-format setting from the project's "
                f"prj.conf (it belongs in Kconfig.defconfig next to the panel it "
                f"targets, not in the application), or pass the matching "
                f"--config.\n\n"
                f"Found in the build: "
                + ", ".join(
                    f"{k}=y" for k in config
                    if k.startswith("CONFIG_LV_COLOR_FORMAT_") and config[k] == "y"
                )
            )

    if config.get("CONFIG_LVGL_PREVIEW") != "y":
        raise BuildError(
            "CONFIG_LVGL_PREVIEW did not end up enabled. The devicetree has no "
            "enabled lvgl,preview-sink node, which means the overlay was not "
            "applied."
        )

    if "CONFIG_LVGL" not in config:
        raise BuildError(
            "LVGL is not enabled in this application. The preview needs "
            "CONFIG_LVGL=y, and a display selected through zephyr,display."
        )

    proc = _run([cmake, "--build", str(out)], out, env)
    if proc.returncode != 0:
        raise BuildError("build failed:\n\n" + proc.stdout)

    executable = out / "zephyr" / "zephyr.exe"
    if not executable.is_file():
        raise BuildError(f"build reported success but {executable} is missing")

    return BuildResult(
        build_dir=out,
        executable=executable,
        manifest=Path(config.get("CONFIG_LVGL_PREVIEW_MANIFEST_PATH", "lvgl_preview_manifest.json")),
        capture=Path(config.get("CONFIG_LVGL_PREVIEW_CAPTURE_PATH", "lvgl_preview_frame.bin")),
        input_ring=Path(config.get("CONFIG_LVGL_PREVIEW_INPUT_PATH", "lvgl_preview_input.bin")),
        config=config,
        log=proc.stdout,
        target=target,
        overlay=overlay_path,
    )
