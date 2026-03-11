#!/Users/ylsuen/.venv/bin/python
"""Audit local source coverage for the shixuci exam-page redesign.

This script does not build runtime assets. It reports what the current local
repository can already support for:

- Beijing classical-Chinese exam coverage already normalized into runtime data
- National classical-Chinese raw coverage available from GAOKAO-Bench
- Explicit question-pattern counts for function-word / content-word extraction

Usage:
    /Users/ylsuen/.venv/bin/python platform/scripts/audit_shixuci_exam_sources.py
    /Users/ylsuen/.venv/bin/python platform/scripts/audit_shixuci_exam_sources.py --json
"""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]

GAOKAO_CHUNKS_PATH = REPO_ROOT / "data" / "index" / "gaokao_chunks.jsonl"
NATIONAL_CLASSICAL_PATH = (
    REPO_ROOT
    / "data"
    / "gaokao_raw"
    / "GAOKAO-Bench"
    / "Data"
    / "Subjective_Questions"
    / "2010-2022_Chinese_Language_Classical_Chinese_Reading.json"
)
GAOKAO_BENCH_UPDATES_DIR = (
    REPO_ROOT / "data" / "gaokao_raw" / "GAOKAO-Bench-Updates" / "Data"
)

PATTERN_RULES = {
    "xuci_compare_same": re.compile(
        r"加[点點]词.{0,8}意义和用法.{0,6}(都相同|相同)"
    ),
    "xuci_compare_diff": re.compile(
        r"加[点點]词.{0,8}意义和用法.{0,6}(不同|不相同)"
    ),
    "shici_explanation": re.compile(
        r"加[点點]词(?:语)?.{0,8}(解释|解说)"
    ),
}


def _load_jsonl(path: Path) -> list[dict]:
    rows: list[dict] = []
    if not path.exists():
        return rows
    with path.open(encoding="utf-8") as handle:
        for raw in handle:
            raw = raw.strip()
            if not raw:
                continue
            rows.append(json.loads(raw))
    return rows


def _pattern_counts(texts: list[str]) -> dict[str, int]:
    counts = Counter()
    for text in texts:
        for label, pattern in PATTERN_RULES.items():
            if pattern.search(text or ""):
                counts[label] += 1
    return dict(counts)


def _coerce_year(value) -> int | None:
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def audit_beijing_runtime() -> dict:
    rows = _load_jsonl(GAOKAO_CHUNKS_PATH)
    classical_rows = [
        row
        for row in rows
        if row.get("subject") == "语文" and row.get("question_type") == "古文"
    ]
    years = sorted(
        {
            year
            for row in classical_rows
            if (year := _coerce_year(row.get("year"))) is not None
        }
    )
    texts = [str(row.get("text", "")) for row in classical_rows]
    return {
        "source_path": str(GAOKAO_CHUNKS_PATH),
        "classical_rows": len(classical_rows),
        "year_range": [years[0], years[-1]] if years else None,
        "years": years,
        "regions": dict(Counter(str(row.get("region") or "未知") for row in classical_rows)),
        "pattern_counts": _pattern_counts(texts),
    }


def audit_national_raw() -> dict:
    if not NATIONAL_CLASSICAL_PATH.exists():
        return {
            "source_path": str(NATIONAL_CLASSICAL_PATH),
            "available": False,
            "rows": 0,
            "year_range": None,
            "pattern_counts": {},
        }
    payload = json.loads(NATIONAL_CLASSICAL_PATH.read_text(encoding="utf-8"))
    items = payload.get("example") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        items = []
    years = sorted(
        {
            year
            for item in items
            if isinstance(item, dict)
            and (year := _coerce_year(item.get("year"))) is not None
        }
    )
    texts = [str(item.get("question", "")) for item in items if isinstance(item, dict)]
    category_counts = Counter(
        str(item.get("category") or "未知")
        for item in items
        if isinstance(item, dict)
    )
    return {
        "source_path": str(NATIONAL_CLASSICAL_PATH),
        "available": True,
        "rows": len(items),
        "year_range": [years[0], years[-1]] if years else None,
        "years": years,
        "categories": dict(category_counts),
        "pattern_counts": _pattern_counts(texts),
    }


def audit_updates() -> dict:
    files = []
    if GAOKAO_BENCH_UPDATES_DIR.exists():
        for path in sorted(GAOKAO_BENCH_UPDATES_DIR.rglob("*")):
            if not path.is_file():
                continue
            if "Classical" in path.name or "古文" in path.name:
                files.append(str(path))
    return {
        "source_path": str(GAOKAO_BENCH_UPDATES_DIR),
        "classical_update_files": files,
        "count": len(files),
    }


def build_report() -> dict:
    beijing = audit_beijing_runtime()
    national = audit_national_raw()
    updates = audit_updates()

    gaps: list[str] = []
    beijing_regions = set(beijing.get("regions", {}).keys())
    if beijing_regions == {"北京"}:
        gaps.append(
            "当前运行时 gaokao_chunks 里的语文古文题仅覆盖北京卷，尚无全国卷标准化结果。"
        )
    if national.get("available"):
        gaps.append(
            "全国卷文言文题目前仍停留在原始 GAOKAO-Bench 文件，尚未进入运行时索引。"
        )
    if updates.get("count", 0) == 0:
        gaps.append(
            "GAOKAO-Bench-Updates 当前未见 Classical_Chinese_Reading 更新文件，2023-2025 全国卷需单独补源。"
        )

    return {
        "built_at": datetime.now(timezone.utc).isoformat(),
        "beijing_runtime": beijing,
        "national_raw": national,
        "update_inventory": updates,
        "gaps": gaps,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true", help="Print JSON only")
    args = parser.parse_args()

    report = build_report()
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    print("Shixuci Exam Source Audit")
    print(f"built_at: {report['built_at']}")
    print()

    beijing = report["beijing_runtime"]
    print("[Beijing Runtime]")
    print(f"source: {beijing['source_path']}")
    print(f"classical_rows: {beijing['classical_rows']}")
    print(f"year_range: {beijing['year_range']}")
    print(f"regions: {beijing['regions']}")
    print(f"pattern_counts: {beijing['pattern_counts']}")
    print()

    national = report["national_raw"]
    print("[National Raw]")
    print(f"source: {national['source_path']}")
    print(f"available: {national['available']}")
    print(f"rows: {national['rows']}")
    print(f"year_range: {national['year_range']}")
    print(f"pattern_counts: {national['pattern_counts']}")
    print()

    updates = report["update_inventory"]
    print("[Updates]")
    print(f"source: {updates['source_path']}")
    print(f"classical_update_files: {updates['count']}")
    print()

    print("[Gaps]")
    for item in report["gaps"]:
        print(f"- {item}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
