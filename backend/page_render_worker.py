"""Render ONE page of ONE PDF to a WebP file, then exit.

Run as a short-lived child process by backend/page_images.py:

    python -I page_render_worker.py <pdf> <page> <width> <out> <max_pixels> <quality> <mem_mb>

It is a separate process on purpose: the web process never imports PyMuPDF, a
runaway or crashing render cannot take the application down, the parent can
kill it on a time limit, and (on Linux) its address space is capped so a
pathological page fails here instead of pushing the host into the OOM killer.

Exit codes: 0 ok · 3 not a readable PDF · 4 page out of range · 5 page size
not acceptable · 6 out of memory · anything else = render error.
Stdout on success: one JSON line {"width","height","bytes"}.
"""
from __future__ import annotations

import json
import os
import sys

MAX_PAGE_POINTS = 14400.0     # 200 inches; anything larger is not a textbook page
MAX_ASPECT = 6.0


def main(argv: list[str]) -> int:
    if len(argv) != 7:
        return 2
    pdf_path, out_path = argv[0], argv[3]
    page_number, width, max_pixels, quality, mem_mb = (int(argv[1]), int(argv[2]), int(argv[4]), int(argv[5]), int(argv[6]))
    try:
        os.nice(10)
    except OSError:
        pass
    if mem_mb > 0 and sys.platform.startswith("linux"):
        try:
            import resource
            limit = mem_mb * 1024 * 1024
            resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
        except (ImportError, ValueError, OSError):
            pass
    try:
        import pymupdf
        from PIL import Image

        pymupdf.TOOLS.mupdf_display_errors(False)
        try:
            document = pymupdf.open(pdf_path, filetype="pdf")
        except Exception:
            return 3
        try:
            if document.needs_pass or not document.is_pdf:
                return 3
            if not 1 <= page_number <= document.page_count:
                return 4
            page = document.load_page(page_number - 1)
            rect = page.rect
            if not (0 < rect.width <= MAX_PAGE_POINTS and 0 < rect.height <= MAX_PAGE_POINTS):
                return 5
            if max(rect.width / rect.height, rect.height / rect.width) > MAX_ASPECT:
                return 5
            zoom = width / rect.width
            height = int(round(rect.height * zoom))
            if height < 1 or width * height > max_pixels:
                return 5
            pixmap = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False, colorspace=pymupdf.csRGB, annots=False)
            image = Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)
            size = (pixmap.width, pixmap.height)
            pixmap = None
            image.save(out_path, "WEBP", quality=quality, method=4)
        finally:
            document.close()
    except MemoryError:
        return 6
    sys.stdout.write(json.dumps({"width": size[0], "height": size[1], "bytes": os.path.getsize(out_path)}) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
