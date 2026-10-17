#!/usr/bin/env bash
# End-to-end smoke test: build, run, capture, and drive input.
#
# This is the test that actually proves the system works. It needs a Zephyr
# tree, which CI provides; locally, point ZEPHYR_BASE at yours.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
: "${ZEPHYR_BASE:?set ZEPHYR_BASE to a Zephyr tree}"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

export PYTHONPATH="$here/src"

echo "== monochrome target (frdm_mcxa156, 128x64 SSD1306) =="
python3 -m lvgl_preview.cli run "$here/examples/render-probe" \
  --build-dir "$work/mono" -t frdm_mcxa156 --timeout 180 \
  -o "$work/mono.png" --scale 4 | tee "$work/mono.log"

# The discovery must be real, not a fallback.
grep -q "128x64 I1" "$work/mono.log"
grep -q "User SW2 (code 11)" "$work/mono.log"

# The frame must not be blank: a blank preview is the failure this tool exists
# to prevent, so it has to be caught here.
python3 - "$work/mono.png" <<'PY'
import sys
from PIL import Image
img = Image.open(sys.argv[1]).convert("L")
colors = img.getcolors()
assert colors is not None
distinct = len(colors)
assert distinct >= 2, f"frame is a single flat colour: {colors}"
# A drawn frame has both black and white pixels.
black = sum(c for c, v in colors if v < 64)
white = sum(c for c, v in colors if v > 192)
total = img.width * img.height
assert black / total > 0.1, "almost nothing is dark"
assert white / total > 0.01, "almost nothing is lit"
print(f"frame ok: {distinct} shades, {white} lit pixels")
PY

echo "== input reaches the application =="
python3 -m lvgl_preview.cli drive "$here/examples/render-probe" \
  --build-dir "$work/drive" -t frdm_mcxa156 --timeout 180 \
  --keys 11,11 --settle 0.7 -o "$work/driven.png" --scale 4 | tee "$work/drive.log"

# The probe echoes the raw key. 11 is INPUT_KEY_0, ASCII '0' == 48.
python3 - "$work/driven.png" "$work/mono.png" <<'PY'
import sys
from PIL import Image
a = Image.open(sys.argv[1]).convert("L")
b = Image.open(sys.argv[2]).convert("L")
assert a.size == b.size
assert list(a.getdata()) != list(b.getdata()), \
    "input changed nothing: the driven frame is identical to the idle one"
print("input ok: driven frame differs from the idle frame")
PY

echo "== colour panel =="
python3 -m lvgl_preview.cli run "$here/examples/render-probe" \
  --build-dir "$work/colour" --timeout 180 \
  -o "$work/colour.png" --scale 2 | tee "$work/colour.log"

grep -q "320x240 ARGB8888" "$work/colour.log"

echo "all smoke tests passed"
