#!/usr/bin/env python3
"""Inspect PDFs and cache their per-page text layer (no OCR, nothing invented).

Cache file: <cache>/<sha256>.<EXTRACTOR_VERSION>.json — written once per (hash, extractor
version); an existing cache file is never recomputed.

A page is `ocr_needed` when it carries fewer than MIN_PAGE_CHARS non-space characters of
text AND has raster content (an embedded image). A page with neither text nor image is
`blank`. `has_text_layer` is true when at least half of the non-blank pages have text.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import EXTRACTOR_VERSION, SHA256_RE, file_hashes, read_json, write_json  # noqa: E402

MIN_PAGE_CHARS = 8
MARGIN_FRACTION = 0.12  # top / bottom strips read position-true for folio detection


def margin_lines(page, pymupdf) -> dict:
    """Text of the top and bottom page strips, rebuilt from glyph positions.

    Folios are often stored out of reading order in the text stream (right-aligned numbers
    emitted digit by digit, e.g. "41" for page 14). Glyphs are therefore grouped into rows by
    their vertical centre and ordered left to right; a wide horizontal gap separates tokens.
    """
    r = page.rect
    h = r.height
    out = {}
    for name, clip in (
        ("top", pymupdf.Rect(r.x0, r.y0, r.x1, r.y0 + MARGIN_FRACTION * h)),
        ("bottom", pymupdf.Rect(r.x0, r.y1 - MARGIN_FRACTION * h, r.x1, r.y1)),
    ):
        chars = []
        for block in page.get_text("rawdict", clip=clip).get("blocks", []):
            if block.get("type") != 0:
                continue
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    for c in span.get("chars", []):
                        if c["c"].strip():
                            x0, y0, x1, y1 = c["bbox"]
                            chars.append(((y0 + y1) / 2, x0, x1, max(y1 - y0, 1.0), c["c"]))
        chars.sort()
        rows: list[list] = []
        for ch in chars:
            if rows and abs(ch[0] - rows[-1][0]) <= 0.6 * ch[3]:
                rows[-1][1].append(ch)
            else:
                rows.append([ch[0], [ch]])
        lines = []
        for _, cs in rows:
            cs.sort(key=lambda c: (c[1], c[4]))
            tokens = [cs[0][4]]
            for a, b in zip(cs, cs[1:]):
                if b[1] - a[2] > 0.6 * max(a[3], 4.0):
                    tokens.append(b[4])
                else:
                    tokens[-1] += b[4]
            lines.append(" ".join(tokens))
        out[name] = lines[:12] if name == "top" else lines[-12:]
    return out


def cache_path(cache_dir: Path, sha256: str) -> Path:
    return Path(cache_dir) / f"{sha256}.{EXTRACTOR_VERSION}.json"


def extract(pdf_path: Path, sha256: str) -> dict:
    import pymupdf  # imported lazily so the pure-logic modules stay importable without it

    doc = pymupdf.open(str(pdf_path))
    try:
        pages = []
        for i in range(doc.page_count):
            page = doc.load_page(i)
            text = page.get_text("text") or ""
            chars = sum(1 for ch in text if not ch.isspace())
            has_image = bool(page.get_images(full=False))
            if chars >= MIN_PAGE_CHARS:
                kind = "text"
            elif has_image:
                kind = "ocr_needed"
            else:
                kind = "blank"
            margin = margin_lines(page, pymupdf) if chars else {"top": [], "bottom": []}
            pages.append({"pdf_page": i + 1, "chars": chars, "kind": kind, "text": text,
                          "margin": margin})
        meta = doc.metadata or {}
        text_pages = sum(1 for p in pages if p["kind"] == "text")
        ocr_pages = sum(1 for p in pages if p["kind"] == "ocr_needed")
        blank_pages = sum(1 for p in pages if p["kind"] == "blank")
        nonblank = text_pages + ocr_pages
        first_text = next((p["text"] for p in pages if p["kind"] == "text"), "")
        return {
            "sha256": sha256,
            "extractor_version": EXTRACTOR_VERSION,
            "pymupdf_version": pymupdf.__version__,
            "pages": doc.page_count,
            "has_text_layer": bool(nonblank) and text_pages * 2 >= nonblank,
            "text_pages": text_pages,
            "ocr_needed_pages": ocr_pages,
            "blank_pages": blank_pages,
            "total_chars": sum(p["chars"] for p in pages),
            "producer": (meta.get("producer") or None),
            "creator": (meta.get("creator") or None),
            "creation_date": (meta.get("creationDate") or None),
            "mod_date": (meta.get("modDate") or None),
            "pdf_title": (meta.get("title") or None),
            "pdf_format": (meta.get("format") or None),
            "encrypted": bool(doc.is_encrypted),
            "first_page_text_sample": " ".join(pages[0]["text"].split())[:200] if pages else "",
            "first_text_sample": " ".join(first_text.split())[:200],
            "page_texts": pages,
        }
    finally:
        doc.close()


def inspect(pdf_path: Path, cache_dir: Path, sha256: str | None = None) -> tuple[dict, bool]:
    """Returns (record, cache_hit). Hashes the file when the hash is not supplied."""
    if sha256 is None:
        sha256 = file_hashes(pdf_path, ("sha256",))["sha256"]
    if not SHA256_RE.match(sha256):
        raise ValueError("sha256 expected")
    cp = cache_path(cache_dir, sha256)
    if cp.exists():
        return read_json(cp), True
    rec = extract(pdf_path, sha256)
    write_json(cp, rec)
    return rec, False


def summary(rec: dict) -> dict:
    return {k: v for k, v in rec.items() if k != "page_texts"}


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--holdings", required=True, help="JSON {sha256: {path, ...}} of PDFs to inspect")
    p.add_argument("--cache", required=True)
    p.add_argument("--summary-out", help="write {sha256: summary} here")
    args = p.parse_args(argv)
    holdings = read_json(args.holdings)
    out, hits, failed = {}, 0, {}
    for sha in sorted(holdings):
        try:
            rec, hit = inspect(Path(holdings[sha]["path"]), Path(args.cache), sha)
        except Exception as exc:  # noqa: BLE001 - a broken PDF must not stop the batch
            failed[sha] = f"{type(exc).__name__}: {exc}"
            continue
        hits += int(hit)
        out[sha] = summary(rec)
    if args.summary_out:
        write_json(args.summary_out, {"extractor_version": EXTRACTOR_VERSION, "pdfs": out, "failed": failed})
    print(f"inspected={len(out)} cache_hits={hits} extracted={len(out) - hits} failed={len(failed)}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
