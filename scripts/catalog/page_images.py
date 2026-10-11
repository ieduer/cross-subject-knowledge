#!/usr/bin/env python3
"""Page-image evidence: compare rendered PDF pages with the legacy page images.

Both sides are reduced to small greyscale thumbnails (THUMB_W x THUMB_H, aspect ignored) and
compared with the normalised cross-correlation (NCC, -1..1). A legacy page is `image-matched`
to a PDF page only when that page scores >= the threshold AND beats every other PDF page in the
search window by the margin; near-uniform (blank) pages carry no information and never match.
Thresholds are not chosen by hand: `calibrate()` derives them from books whose page pairing is
known (the byte-proven originals).

Legacy thumbnails are made once per book from a directory of p<N>.webp files whose SHA-256 must
equal the published manifest; `sips` (macOS) does the WebP decoding. PDF thumbnails are rendered
with PyMuPDF. Both are cached as raw bytes and never recomputed.
"""
from __future__ import annotations

import argparse
import hashlib
import math
import shutil
import subprocess
import sys
import tempfile
from array import array
from operator import mul
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import read_json, write_atomic, write_json  # noqa: E402

THUMB_W, THUMB_H = 60, 84
THUMB_N = THUMB_W * THUMB_H
THUMB_VERSION = "thumb-v1-60x84-gray"
BLANK_STDDEV = 6.0          # a thumbnail with less contrast than this is treated as blank


# --- thumbnails ----------------------------------------------------------------------------

def load_manifest(path: Path) -> dict[str, str]:
    out = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if line.strip():
            sha, name = line.split(None, 1)
            out[name.strip().removeprefix("./")] = sha
    return out


def legacy_thumbs(src_root: Path, manifest: dict[str, str], out_dir: Path, key: str, pages: int) -> dict:
    """Thumbnails of p0..p<pages-1> of one legacy book. Cached in <out>/<key>.gray + .json."""
    import pymupdf

    meta_path, data_path = out_dir / f"{key}.json", out_dir / f"{key}.gray"
    if meta_path.exists() and data_path.exists():
        meta = read_json(meta_path)
        if meta.get("version") == THUMB_VERSION and meta.get("pages") == pages:
            return meta
    files = [src_root / key / f"p{i}.webp" for i in range(pages)]
    missing, bad = [], []
    for i, f in enumerate(files):
        if not f.exists():
            missing.append(i)
        elif hashlib.sha256(f.read_bytes()).hexdigest() != manifest.get(f"{key}/p{i}.webp"):
            bad.append(i)
    if missing or bad:
        raise RuntimeError(f"{key}: legacy images missing {missing[:5]} / not matching the manifest {bad[:5]}")
    out_dir.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix=f"thumb-{key}-", dir=str(out_dir)))
    try:
        for start in range(0, pages, 200):
            subprocess.run(["/usr/bin/sips", "-s", "format", "png", "-z", str(THUMB_H), str(THUMB_W),
                            *[str(f) for f in files[start:start + 200]], "--out", str(tmp)],
                           check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        buf = bytearray()
        for i in range(pages):
            pix = pymupdf.Pixmap(str(tmp / f"p{i}.png"))
            if pix.n != 1 or pix.alpha:
                pix = pymupdf.Pixmap(pymupdf.csGRAY, pymupdf.Pixmap(pix, 0) if pix.alpha else pix)
            if (pix.width, pix.height) != (THUMB_W, THUMB_H):
                raise RuntimeError(f"{key}: unexpected thumbnail size {pix.width}x{pix.height}")
            buf += pix.samples
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    out_dir.mkdir(parents=True, exist_ok=True)
    write_atomic(data_path, bytes(buf))
    meta = {"version": THUMB_VERSION, "key": key, "pages": pages, "manifest_verified": True,
            "source": "local copy of the published page images, each file equal to the manifest SHA-256"}
    write_json(meta_path, meta)
    return meta


def pdf_thumbs(pdf_path: Path, sha256: str, out_dir: Path) -> dict:
    """Thumbnails of every page of one PDF. Cached in <out>/<sha256>.gray + .json."""
    import pymupdf

    meta_path, data_path = out_dir / f"{sha256}.json", out_dir / f"{sha256}.gray"
    if meta_path.exists() and data_path.exists():
        meta = read_json(meta_path)
        if meta.get("version") == THUMB_VERSION:
            return meta
    buf = bytearray()
    with pymupdf.open(str(pdf_path)) as doc:
        n = doc.page_count
        for page in doc:
            r = page.rect
            zoom = max(2 * THUMB_W / max(r.width, 1), 2 * THUMB_H / max(r.height, 1))
            pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), colorspace=pymupdf.csGRAY, alpha=False)
            small = pymupdf.Pixmap(pix, THUMB_W, THUMB_H)
            buf += small.samples
    out_dir.mkdir(parents=True, exist_ok=True)
    write_atomic(data_path, bytes(buf))
    meta = {"version": THUMB_VERSION, "sha256": sha256, "pages": n, "pymupdf": pymupdf.__version__}
    write_json(meta_path, meta)
    return meta


def load_thumbs(out_dir: Path, name: str) -> list:
    """[(unit vector as array('d') | None when blank)] per page."""
    data = (Path(out_dir) / f"{name}.gray").read_bytes()
    return [normalise(data[i:i + THUMB_N]) for i in range(0, len(data), THUMB_N)]


def normalise(sample: bytes):
    n = len(sample)
    mean = sum(sample) / n
    var = sum((v - mean) ** 2 for v in sample) / n
    if math.sqrt(var) < BLANK_STDDEV:
        return None
    norm = math.sqrt(var * n)
    return array("d", [(v - mean) / norm for v in sample])


def ncc(a, b) -> float:
    return sum(map(mul, a, b))


# --- matching ------------------------------------------------------------------------------

def score_book(legacy: list, pdf: list, window: int) -> list[dict]:
    """For every legacy index: best PDF page in the window around the same position, its score and
    the best other score. Blank legacy pages get {"blank": True}."""
    out = []
    n_pdf = len(pdf)
    for li, lv in enumerate(legacy):
        if lv is None:
            out.append({"legacy_index": li, "blank": True})
            continue
        lo, hi = max(0, li - window), min(n_pdf - 1, li + window)
        scores = [(ncc(lv, pdf[p]), p + 1) for p in range(lo, hi + 1) if pdf[p] is not None]
        if not scores:
            out.append({"legacy_index": li, "blank": False, "pdf_page": None, "score": None, "second": None})
            continue
        scores.sort(key=lambda t: (-t[0], t[1]))
        best = scores[0]
        second = scores[1][0] if len(scores) > 1 else -1.0
        out.append({"legacy_index": li, "blank": False, "pdf_page": best[1],
                    "score": round(best[0], 4), "second": round(second, 4)})
    return out


MATCH_FLOOR = 0.80
MATCH_MARGIN = 0.10


def passes(r: dict, threshold: float, margin: float) -> bool:
    return r["score"] >= threshold and r["score"] - r["second"] >= margin


def calibrate(known: list[list[dict]]) -> dict:
    """Decision rule from books whose true pairing is pdf_page == legacy_index + 1.

    Rule: a legacy page is matched to its best-scoring PDF page when score >= match_threshold AND
    score - (best other page) >= margin. The threshold starts at MATCH_FLOOR and is raised until
    NO page of the calibration set is matched to a wrong page; the margin rejects pages that look
    alike (atlas plates, part-title pages). Recall on the known pairs is reported, not tuned.

    `same_threshold` is deliberately null: at this thumbnail size another printing of the same
    edition scores as high as the same file, so image scores prove page correspondence, not that two
    files are the same printing.
    """
    true, rival, wrong_best = [], [], []
    informative = []
    for rows in known:
        for r in rows:
            if r.get("blank") or r.get("score") is None:
                continue
            informative.append(r)
            if r["pdf_page"] == r["legacy_index"] + 1:
                true.append(r["score"])
                rival.append(r["second"])
            else:
                wrong_best.append(r["score"])
    if not true:
        raise ValueError("no informative page in the calibration set")
    threshold = MATCH_FLOOR
    false_accepts = [r for r in informative if r["pdf_page"] != r["legacy_index"] + 1
                     and passes(r, threshold, MATCH_MARGIN)]
    if false_accepts:
        threshold = round(max(r["score"] for r in false_accepts) + 0.02, 4)
    accepted = sum(1 for r in informative if r["pdf_page"] == r["legacy_index"] + 1
                   and passes(r, threshold, MATCH_MARGIN))
    true.sort()
    rival.sort()
    pct = lambda xs, q: xs[min(len(xs) - 1, max(0, int(q * len(xs))))]  # noqa: E731
    return {
        "thumb_version": THUMB_VERSION, "pages": len(true) + len(wrong_best),
        "best_page_is_the_true_page": len(true), "best_page_not_the_true_page": len(wrong_best),
        "wrong_best_max_score": max(wrong_best, default=None),
        "true_min": true[0], "true_p01": pct(true, 0.01), "true_p50": pct(true, 0.5),
        "rival_max": rival[-1], "rival_p99": pct(rival, 0.99), "rival_p50": pct(rival, 0.5),
        "match_threshold": threshold, "margin": MATCH_MARGIN, "same_threshold": None,
        "false_accepts_at_rule": 0,
        "true_pairs_accepted": accepted,
        "recall": round(accepted / (len(true) + len(wrong_best)), 4),
    }


def image_matches(rows: list[dict], cal: dict) -> dict[int, dict]:
    """legacy_index -> {pdf_page, score, second} for pages that pass the calibrated test."""
    out = {}
    for r in rows:
        if r.get("blank") or r.get("score") is None:
            continue
        if passes(r, cal["match_threshold"], cal["margin"]):
            out[r["legacy_index"]] = {"pdf_page": r["pdf_page"], "score": r["score"], "second": r["second"]}
    return out


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--plan", required=True)
    p.add_argument("--holdings", required=True)
    p.add_argument("--legacy-root", required=True, help="directory with <jc_short_key>/p<N>.webp")
    p.add_argument("--manifest", required=True, help="page-images.sha256")
    p.add_argument("--out", required=True, help="cache directory (legacy/ and pdf/ thumbnails, scores/)")
    args = p.parse_args(argv)
    plan, holdings = read_json(args.plan), read_json(args.holdings)
    out = Path(args.out)
    manifest = load_manifest(Path(args.manifest))
    done = 0
    for pair in plan["comparisons"]:
        key, sha = pair["jc_short_key"], pair["sha256"]
        score_path = out / "scores" / f"{key}__{sha[:16]}.json"
        if score_path.exists():
            continue
        legacy_thumbs(Path(args.legacy_root), manifest, out / "legacy", key, pair["legacy_pages"])
        pdf_thumbs(Path(holdings[sha]["path"]), sha, out / "pdf")
        lt, pt = load_thumbs(out / "legacy", key), load_thumbs(out / "pdf", sha)
        window = abs(len(pt) - len(lt)) + 8
        write_json(score_path, {"jc_short_key": key, "sha256": sha, "legacy_pages": len(lt), "pdf_pages": len(pt),
                                "window": window, "thumb_version": THUMB_VERSION,
                                "rows": score_book(lt, pt, window)})
        done += 1
        print(f"{key} {sha[:12]} legacy={len(lt)} pdf={len(pt)} window={window}", flush=True)
    # Calibration: only from pairs whose true pairing is known (byte-proven originals).
    known = [read_json(out / "scores" / f"{c['jc_short_key']}__{c['sha256'][:16]}.json")["rows"]
             for c in plan["comparisons"] if c.get("known_pairing")]
    if known:
        cal = calibrate(known)
        cal["calibration_books"] = len(known)
        cal["legacy_source"] = ("local copy of the published page images (every file equal to the manifest "
                                "SHA-256), reduced to 60x84 greyscale")
        write_json(out / "calibration.json", cal)
        print("calibration", {k: v for k, v in cal.items() if k not in ("legacy_source", "thumb_version")})
    print(f"scored={done}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
