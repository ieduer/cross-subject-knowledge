"""Synthetic `sun-catalog-v1` release + tiny PDFs for tests and the dev server.

Everything written here is fake data for development: titles are prefixed
"【样例】" and the page maps are invented.  Never copy it to production.
Requires PyMuPDF (dev/test only; not a runtime dependency of SUN).
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pymupdf as fitz  # PyMuPDF (dev/test only)

# A real SUN book_key so that the dev server's proxied /api/search returns hits
# for the in-book search box.  The legacy index -> pdf page rows below are
# synthetic and only cover the first pages.
REAL_SUN_KEY = "高中_语文_普通高中教科书_语文必修_上册"
SECOND_SUN_KEY = "初中_物理_样例_八年级上册"


def _pdf(path_dir: Path, title: str, pages: int, text: bool, first_printed_at: int = 0) -> tuple[str, int]:
    doc = fitz.open()
    for number in range(1, pages + 1):
        page = doc.new_page(width=420, height=595)
        page.draw_rect(fitz.Rect(24, 24, 396, 571), color=(0.47, 0.58, 0.58), width=1)
        if text:
            page.insert_text((48, 80), f"Sample page {number}", fontsize=20)
            page.insert_text((48, 120), f"{title} 第 {number} 页 沁园春 长沙 光合作用", fontsize=13, fontname="china-s")
            page.insert_text((48, 150), "独立寒秋，湘江北去，橘子洲头。", fontsize=13, fontname="china-s")
            if first_printed_at and number >= first_printed_at:
                page.insert_text((200, 560), str(number - first_printed_at + 1), fontsize=10)
        else:
            # No text layer: shapes only.
            for row in range(12):
                page.draw_line((48, 70 + row * 36), (372 - (row * 13 + number * 7) % 120, 70 + row * 36), color=(0.36, 0.33, 0.32))
            page.draw_circle((210, 500), 20 + number, color=(0.48, 0.33, 0.19))
    if text:
        doc.set_toc([[1, "第一单元", 1], [2, "第 2 页", 2], [1, "第二单元", max(1, pages // 2)]])
    data = doc.tobytes(garbage=3, deflate=True)
    doc.close()
    sha = hashlib.sha256(data).hexdigest()
    path_dir.mkdir(parents=True, exist_ok=True)
    (path_dir / f"{sha}.pdf").write_bytes(data)
    return sha, len(data)


def _big_pdf(path_dir: Path, pages: int = 40) -> tuple[str, int]:
    """A multi-megabyte scan-like PDF (noise images, no text layer) so that the
    dev server can show real HTTP Range behaviour.  Deterministic."""
    import random

    rng = random.Random(20261010)
    doc = fitz.open()
    for number in range(1, pages + 1):
        page = doc.new_page(width=420, height=595)
        pix = fitz.Pixmap(fitz.csRGB, 300, 425, rng.randbytes(300 * 425 * 3), False)
        page.insert_image(fitz.Rect(30, 40, 390, 550), pixmap=pix)
        page.draw_rect(fitz.Rect(150, 10, 270, 34), color=(1, 1, 1), fill=(1, 1, 1))
        for digit_index, digit in enumerate(str(number)):  # page number drawn as bars, not text
            for bar in range(int(digit) + 1):
                page.draw_line((160 + digit_index * 40 + bar * 3, 14), (160 + digit_index * 40 + bar * 3, 30), color=(0, 0, 0))
    data = doc.tobytes(garbage=3, deflate=True)
    doc.close()
    sha = hashlib.sha256(data).hexdigest()
    path_dir.mkdir(parents=True, exist_ok=True)
    (path_dir / f"{sha}.pdf").write_bytes(data)
    return sha, len(data)


def _edition(source_id, sha, size, pages, **over):
    record = {
        "edition_id": f"cn-{source_id}-{sha[:12]}",
        "work_id": over.pop("work_id", f"work-{source_id}"),
        "region": "CN",
        "stage": "高中", "subject": "语文", "title": "【样例】教材", "volume": None,
        "publisher": "人民教育出版社", "edition_label": "样例版 2026",
        "source": {"provider": "smartedu", "source_id": source_id, "catalog_module": "synthetic",
                   "online_time": "2026-01-01", "update_time": "2026-06-01", "ti_md5": "0" * 32, "ti_size": size,
                   "retrieved_at": "2026-10-10T00:00:00Z", "retrieved_via": "synthetic"},
        "pdf": {"sha256": sha, "bytes": size, "pages": pages, "has_text_layer": True, "producer": "PyMuPDF"},
        "storage": {"vps": {"path": f"pdf/{sha}.pdf"}, "drive": None, "upstream": {"available": True}},
        "legacy": {"sun_book_key": None, "jc_short_key": None, "jks_slug": None},
        "corpus": {"relation": "none", "basis": None, "coverage": None},
        "processing": {"ocr_version": None, "index_version": None, "page_map_version": "synthetic-1"},
        "status": "active",
    }
    for key, value in over.items():
        if isinstance(value, dict) and isinstance(record.get(key), dict):
            record[key] = {**record[key], **value}
        else:
            record[key] = value
    return record


def _page_map(edition, rows):
    return {"edition_id": edition["edition_id"], "pdf_sha256": edition["pdf"]["sha256"],
            "page_map_version": "synthetic-1", "pages": rows}


def _row(pdf_page, printed=None, verified=False, legacy=None, ocr=None):
    return {"pdf_page": pdf_page, "printed_label": printed, "printed_verified": verified,
            "legacy_image_index": legacy, "ocr_page": ocr}


def write_release(release_dir: Path, release_id: str, editions: list, downloads: list, page_maps: dict,
                  generated_at: str = "2026-10-10T00:00:00Z") -> None:
    """Write a complete release directory including SHA256SUMS."""
    (release_dir / "page-maps").mkdir(parents=True, exist_ok=True)
    files = {
        "catalog.json": {"schema": "sun-catalog-v1", "release_id": release_id, "editions": editions},
        "downloads.json": {"schema": "sun-catalog-v1", "release_id": release_id, "downloads": downloads},
        "RELEASE.json": {"schema": "sun-catalog-v1", "release_id": release_id, "generated_at": generated_at,
                         "inputs": {}, "tools": {"generator": "tests/library/synthetic.py"},
                         "stats": {"editions": len(editions), "downloads": len(downloads)}},
    }
    for edition_id, payload in page_maps.items():
        files[f"page-maps/{edition_id}.json"] = payload
    lines = []
    for rel, payload in files.items():
        data = json.dumps(payload, ensure_ascii=False, indent=1).encode("utf-8")
        (release_dir / rel).write_bytes(data)
        lines.append(f"{hashlib.sha256(data).hexdigest()}  {rel}")
    (release_dir / "SHA256SUMS").write_text("\n".join(lines) + "\n", encoding="utf-8")


def set_current(catalog_root: Path, release_id: str) -> None:
    """Atomic pointer switch: write a temp file, then rename over CURRENT."""
    tmp = catalog_root / "CURRENT.tmp"
    tmp.write_text(release_id + "\n", encoding="utf-8")
    tmp.replace(catalog_root / "CURRENT")


def build(catalog_root: Path, pdf_dir: Path, release_id: str = "r-synthetic-1", big: bool = False) -> dict:
    """Create PDFs + one release and point CURRENT at it.  Returns handy ids.
    ``big=True`` (dev server only) adds one ~15 MB image-only book."""
    catalog_root = Path(catalog_root)
    pdf_dir = Path(pdf_dir)

    sha1, size1 = _pdf(pdf_dir, "语文必修上册", 12, text=True, first_printed_at=3)
    sha2, size2 = _pdf(pdf_dir, "数学必修第一册", 8, text=False)
    sha3, size3 = _pdf(pdf_dir, "物理八年级上册", 6, text=True)
    sha4, size4 = _pdf(pdf_dir, "语文必修上册旧版", 10, text=True)
    sha7, size7 = _pdf(pdf_dir, "物理八年级上册另一版", 7, text=True)
    sha5 = hashlib.sha256(b"not-stored-on-site").hexdigest()

    e1 = _edition("11111111-aaaa-4bbb-8ccc-000000000001", sha1, size1, 12,
                  title="【样例】普通高中教科书·语文 必修 上册", volume="必修上册", work_id="work-gz-yuwen-bx-s",
                  legacy={"sun_book_key": REAL_SUN_KEY, "jc_short_key": hashlib.md5(REAL_SUN_KEY.encode()).hexdigest()[:12],
                          "jks_slug": "gzyw_bx_s", "jks_pages": "pdf_page"},
                  corpus={"relation": "aligned", "basis": "synthetic", "coverage": 1.0})
    e2 = _edition("22222222-aaaa-4bbb-8ccc-000000000002", sha2, size2, 8, subject="数学",
                  title="【样例】普通高中教科书·数学 必修 第一册", edition_label="A版",
                  pdf={"has_text_layer": False})
    e3 = _edition("33333333-aaaa-4bbb-8ccc-000000000003", sha3, size3, 6, stage="初中", subject="物理",
                  title="【样例】义务教育教科书·物理 八年级 上册", work_id="work-cz-wuli-8s",
                  legacy={"sun_book_key": SECOND_SUN_KEY}, corpus={"relation": "partial", "basis": "synthetic", "coverage": 0.5})
    e4 = _edition("44444444-aaaa-4bbb-8ccc-000000000004", sha4, size4, 10,
                  title="【样例】普通高中教科书·语文 必修 上册", edition_label="样例版 2019", work_id="work-gz-yuwen-bx-s",
                  status="superseded")
    e5 = _edition("55555555-aaaa-4bbb-8ccc-000000000005", sha5, 123456789, 200, subject="英语",
                  title="【样例】普通高中教科书·英语 必修 第一册", publisher="外语教学与研究出版社",
                  storage={"vps": None, "drive": {"file_id": "FILEID", "resource_key": None,
                                                   "view_url": "https://drive.google.com/file/d/FILEID/view",
                                                   "download_url": "https://drive.google.com/uc?export=download&id=FILEID",
                                                   "shared": True}})
    e7 = _edition("77777777-aaaa-4bbb-8ccc-000000000007", sha7, size7, 7, stage="初中", subject="物理",
                  title="【样例】义务教育教科书·物理 八年级 上册", edition_label="样例版 2024", work_id="work-cz-wuli-8s",
                  legacy={"sun_book_key": SECOND_SUN_KEY}, corpus={"relation": "partial", "basis": "synthetic", "coverage": 0.4})
    editions = [e1, e2, e3, e4, e5, e7]
    if big:
        sha8, size8 = _big_pdf(pdf_dir)
        editions.append(_edition("88888888-aaaa-4bbb-8ccc-000000000008", sha8, size8, 40, subject="物理",
                                 title="【样例】大文件扫描版·物理 必修 第一册", edition_label="扫描样例",
                                 pdf={"has_text_layer": False}))

    page_maps = {
        # e1: every page mapped (legacy index = pdf_page - 1); printed pages verified from page 3.
        e1["edition_id"]: _page_map(e1, [
            _row(n, printed=(str(n - 2) if n >= 3 else None), verified=n >= 3, legacy=n - 1, ocr=n) for n in range(1, 13)]),
        # e3: printed labels present but NOT verified (must never be shown); legacy only for pages 2-4.
        e3["edition_id"]: _page_map(e3, [
            _row(n, printed=str(n), verified=False, legacy=({2: 1, 3: 2, 4: 5}.get(n))) for n in range(1, 7)]),
        # e7 shares e3's book key and also claims legacy index 2 -> ambiguous, must not resolve.
        e7["edition_id"]: _page_map(e7, [_row(n, legacy=(2 if n == 5 else None)) for n in range(1, 8)]),
        # e2 has no page map on purpose.
    }

    def dl(edition=None, **over):
        row = {"source_id": None, "stage": "高中", "subject": "语文", "title": "【样例】教材", "edition_label": None,
               "publisher": "人民教育出版社", "cover": None, "size_bytes": None, "sources": [], "edition_id": None}
        if edition is not None:
            row.update({"source_id": edition["source"]["source_id"], "stage": edition["stage"], "subject": edition["subject"],
                        "title": edition["title"], "edition_label": edition["edition_label"], "publisher": edition["publisher"],
                        "size_bytes": edition["pdf"]["bytes"], "edition_id": edition["edition_id"],
                        "sources": [{"kind": "vps"}, {"kind": "upstream", "url": f"https://jks-ai.bdfz.net/pdf/{edition['source']['source_id']}"}]})
        row.update(over)
        return row

    downloads = [
        dl(e1, cover="https://r1-ndr.ykt.cbern.com.cn/edu_product/esp/assets/sample/cover.jpg"),
        dl(e2),
        dl(e3),
        dl(e4),
        dl(e5, sources=[{"kind": "drive"}, {"kind": "upstream", "url": "https://jks-ai.bdfz.net/pdf/55555555-aaaa-4bbb-8ccc-000000000005"}]),
        dl(e7),
        dl(title="【样例】坏来源教材", source_id="bad-host-1", cover="http://insecure.example/cover.jpg",
           sources=[{"kind": "upstream", "url": "https://evil.example/pdf/1"}, {"kind": "upstream", "url": "javascript:alert(1)"}]),
        dl(title="【样例】暂无来源教材", source_id="no-source-1", subject="历史"),
    ]
    if big:
        downloads.append(dl(editions[-1]))
    subjects = ["语文", "数学", "英语", "科学", "道德与法治"]
    for index in range(70):
        stage = "小学" if index < 50 else "初中"
        downloads.append(dl(title=f"【样例】{stage}教材 第 {index + 1:02d} 册", source_id=f"gen-{index:03d}", stage=stage,
                            subject=subjects[index % len(subjects)], publisher=("北京师范大学出版社" if index % 3 else "人民教育出版社"),
                            edition_label=f"样例版 {2020 + index % 5}", size_bytes=(1000000 + index * 4096 if index % 4 else None),
                            sources=[{"kind": "upstream", "url": f"https://jks-ai.bdfz.net/pdf/gen-{index:03d}"}]))

    release_dir = catalog_root / "releases" / release_id
    write_release(release_dir, release_id, editions, downloads, page_maps)
    set_current(catalog_root, release_id)
    return {
        "release_id": release_id, "release_dir": release_dir,
        "e1": e1, "e2": e2, "e3": e3, "e4": e4, "e5": e5, "e7": e7,
        "editions": editions, "downloads": downloads, "page_maps": page_maps,
        "sizes": {"e1": size1, "e2": size2},
    }


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Write a synthetic catalog release and PDFs (dev only)")
    parser.add_argument("--catalog-root", required=True)
    parser.add_argument("--pdf-dir", required=True)
    parser.add_argument("--release-id", default="r-synthetic-1")
    args = parser.parse_args()
    info = build(Path(args.catalog_root), Path(args.pdf_dir), args.release_id)
    print(f"release {info['release_id']} -> {info['release_dir']}")
