#!/usr/bin/env bash
#
# Render otp.md to PDF for the unit's PRINT MANUAL job.
#
# Done at image-build time, not on the device: the Pi then needs no pandoc,
# no LaTeX and no network, and the layout is fixed at build time rather than
# varying with whatever is installed on the unit.
#
# weasyprint rather than a TeX engine keeps roughly 2GB of texlive out of
# the build. The one thing to watch is the tabula recta -- it is 26 columns
# of monospace and has to stay unwrapped on A5.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT_DIR="$REPO_DIR/assets"

for tool in pandoc weasyprint; do
    command -v "$tool" >/dev/null || {
        echo "ERROR: $tool not found. Install with:" >&2
        echo "  sudo apt-get install -y pandoc python3-weasyprint" >&2
        exit 1
    }
done

mkdir -p "$OUT_DIR"

render() {
    local size="$1" out="$2"
    echo "Rendering $size -> $out"
    # Run from the repo root so human_bias.png resolves.
    (cd "$REPO_DIR" && pandoc otp.md \
        --from=gfm \
        --pdf-engine=weasyprint \
        --pdf-engine-opt=--encoding=utf-8 \
        --css="image/manual-${size}.css" \
        --metadata title="One-Time Pad Field Manual" \
        --toc --toc-depth=2 \
        --output="$out")
}

render a5 "$OUT_DIR/otp-manual-a5.pdf"
render a4 "$OUT_DIR/otp-manual-a4.pdf"

echo
echo "Done:"
ls -lh "$OUT_DIR"/otp-manual-*.pdf
echo
echo "Check the tabula recta in the Tools section: all 26 columns must sit"
echo "on one line. If they wrap, drop the pre font-size in image/manual-a5.css."
