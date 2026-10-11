#!/usr/bin/env python3
"""Pre-render one cover per held PDF: page 1 as WebP at fixed widths.

  <out>/<pdf sha256>-<width>.webp      for width in WIDTHS

Rendering: PyMuPDF renders page 1 to RGB at exactly the target width (height by the page's aspect
ratio), written as PNG and encoded by `cwebp -q QUALITY` single-threaded, so the same PDF always
gives the same bytes on the same tool versions. A cover that exists is never rendered again.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import read_json, write_atomic  # noqa: E402

WIDTHS = (240, 480)
QUALITY = 80
CWEBP = "/opt/homebrew/bin/cwebp"


def render_cover(pdf_path: Path, width: int, encode=None) -> bytes:
    import pymupdf

    with pymupdf.open(str(pdf_path)) as doc:
        page = doc.load_page(0)
        zoom = width / page.rect.width
        pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), colorspace=pymupdf.csRGB, alpha=False)
        if pix.width != width:   # rounding: scale the pixmap to the exact width
            pix = pymupdf.Pixmap(pix, width, max(1, round(pix.height * width / pix.width)))
        png = pix.tobytes("png")
    return (encode or cwebp)(png)


def cwebp(png: bytes) -> bytes:
    with tempfile.TemporaryDirectory(prefix="cover-") as tmp:
        src, dst = Path(tmp) / "c.png", Path(tmp) / "c.webp"
        src.write_bytes(png)
        subprocess.run([CWEBP, "-quiet", "-q", str(QUALITY), "-m", "4", str(src), "-o", str(dst)], check=True)
        return dst.read_bytes()


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--holdings", required=True, help="JSON {sha256: {path}} of held PDFs")
    p.add_argument("--out", required=True)
    args = p.parse_args(argv)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    made = kept = total = 0
    for sha, h in sorted(read_json(args.holdings).items()):
        for w in WIDTHS:
            target = out / f"{sha}-{w}.webp"
            if target.exists():
                kept += 1
            else:
                write_atomic(target, render_cover(Path(h["path"]), w))
                made += 1
            total += target.stat().st_size
    print(f"covers rendered={made} kept={kept} total_bytes={total}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
