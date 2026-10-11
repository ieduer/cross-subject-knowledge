#!/usr/bin/env python3
"""Reassemble a PDF from the legacy page images of a reader book (one image per page).

Used only for books whose platform file is another edition, so that every old page link keeps an
exact target. By construction PDF page N is legacy image index N-1.

Encoding (documented choice): each p<N>.webp is checked against the published SHA-256 manifest,
decoded losslessly with `dwebp`, and embedded as a baseline JPEG of quality JPEG_QUALITY at its
original pixel dimensions (PDF cannot embed WebP; a lossless Flate embedding of the decoded pixels
would be about six times larger). Page size = pixels x 72 / LEGACY_DPI points, i.e. the size at
which the images were rendered. The file carries no text layer and honest metadata. Output is
deterministic: fixed metadata, no document id, no timestamps other than the fixed ones.

Writes  <store>/<sha256>.pdf, <sidecar-dir>/<sha256>.json (per-page source image hashes) and
updates <state> ({jc_short_key: {...}}). Already reassembled books are skipped.
"""
from __future__ import annotations

import argparse
import hashlib
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import TOOL_VERSION, file_hashes, read_json, write_json  # noqa: E402
from page_images import load_manifest  # noqa: E402

JPEG_QUALITY = 80
LEGACY_DPI = 100
DWEBP = "/opt/homebrew/bin/dwebp"
PRODUCER = f"reassembled from legacy page images, sun-catalog {TOOL_VERSION}"
FIXED_DATE = "D:20260316000000Z"   # the legacy image set was published 2026-03-05 … 03-16


def decode_webp(path: Path, tmp: Path):
    """Lossless decode of one WebP file to a PyMuPDF pixmap (RGB, no alpha)."""
    import pymupdf

    png = tmp / (path.stem + ".png")
    subprocess.run([DWEBP, "-quiet", str(path), "-o", str(png)], check=True)
    pix = pymupdf.Pixmap(str(png))
    if pix.alpha:
        pix = pymupdf.Pixmap(pix, 0)
    if pix.n not in (1, 3):
        pix = pymupdf.Pixmap(pymupdf.csRGB, pix)
    png.unlink()
    return pix


def reassemble(key: str, pages: int, title: str, legacy_root: Path, manifest: dict[str, str], out_pdf: Path,
               decode=decode_webp) -> dict:
    import pymupdf

    doc = pymupdf.open()
    rows = []
    with tempfile.TemporaryDirectory(prefix=f"reassemble-{key}-", dir=str(out_pdf.parent)) as tmpdir:
        for i in range(pages):
            src = legacy_root / key / f"p{i}.webp"
            sha = hashlib.sha256(src.read_bytes()).hexdigest()
            if manifest.get(f"{key}/p{i}.webp") != sha:
                raise RuntimeError(f"{key}/p{i}.webp does not match the published manifest")
            pix = decode(src, Path(tmpdir))
            jpeg = pix.tobytes("jpg", jpg_quality=JPEG_QUALITY)
            w_pt, h_pt = pix.width * 72 / LEGACY_DPI, pix.height * 72 / LEGACY_DPI
            page = doc.new_page(width=w_pt, height=h_pt)
            page.insert_image(page.rect, stream=jpeg, keep_proportion=False)
            rows.append({"pdf_page": i + 1, "legacy_image_index": i, "source": f"pages/{key}/p{i}.webp",
                         "source_sha256": sha, "source_bytes": src.stat().st_size,
                         "width_px": pix.width, "height_px": pix.height,
                         "embedded_jpeg_sha256": hashlib.sha256(jpeg).hexdigest()})
    doc.set_metadata({"title": title, "producer": PRODUCER, "creator": "sun-catalog reassemble.py",
                      "creationDate": FIXED_DATE, "modDate": FIXED_DATE, "author": "", "subject":
                      "legacy 100-dpi page images of an earlier edition; no text layer", "keywords": ""})
    doc.save(str(out_pdf), garbage=3, deflate=True, no_new_id=True)
    doc.close()
    return {"jc_short_key": key, "pages": pages, "title": title, "producer": PRODUCER,
            "encoding": {"decode": "dwebp (lossless decode of the published WebP)", "embed": "JPEG (DCTDecode)",
                         "jpeg_quality": JPEG_QUALITY, "pixel_dimensions": "original",
                         "page_size": f"pixels * 72 / {LEGACY_DPI} pt", "text_layer": False},
            "source_image_bytes": sum(r["source_bytes"] for r in rows), "page_sources": rows}


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--jc-catalog", required=True)
    p.add_argument("--legacy-root", required=True)
    p.add_argument("--manifest", required=True)
    p.add_argument("--store", required=True)
    p.add_argument("--sidecars", required=True)
    p.add_argument("--state", required=True, help="reassembled.json")
    p.add_argument("keys", nargs="+", help="jc_short_key of each book to reassemble")
    args = p.parse_args(argv)
    jc = {b["shortKey"]: b for b in read_json(args.jc_catalog)["books"]}
    manifest = load_manifest(Path(args.manifest))
    state = read_json(args.state, default={})
    store, sidecars = Path(args.store), Path(args.sidecars)
    store.mkdir(parents=True, exist_ok=True)
    for key in args.keys:
        b = jc[key]
        done = state.get(key)
        if done and (store / f"{done['sha256']}.pdf").exists() and (sidecars / f"{done['sha256']}.json").exists():
            print(f"{key}: already reassembled {done['sha256'][:12]}")
            continue
        tmp = store / f".reassemble-{key}.pdf"
        title = f"{b['title']}（{b.get('edition') or '版别不详'}，由旧页图重组）"
        info = reassemble(key, b["pages"], title, Path(args.legacy_root), manifest, tmp)
        h = file_hashes(tmp)
        final = store / f"{h['sha256']}.pdf"
        tmp.replace(final)
        info.update({"sha256": h["sha256"], "md5": h["md5"], "bytes": h["bytes"]})
        write_json(sidecars / f"{h['sha256']}.json", info)
        state[key] = {"sha256": h["sha256"], "md5": h["md5"], "bytes": h["bytes"], "pages": info["pages"],
                      "title": title, "source_image_bytes": info["source_image_bytes"]}
        write_json(args.state, state)
        print(f"{key}: {info['pages']} pages, {h['bytes']} bytes (source images {info['source_image_bytes']}), "
              f"sha256 {h['sha256'][:12]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
