#!/usr/bin/env python3
"""Derive a catalog release whose download list also covers the subject groups the first build
never enumerated (信息技术, 通用技术, 艺术, 音乐, 美术, 体育与健康, 科学, 日语, 俄语 …).

  extend_downloads.py --parent <releases>/<id> --extra <dir of jks-ai group caches> --out <releases>

Metadata only (owner 2026-11-26: the new subjects are listed and downloaded from the source site;
no PDF of them is fetched or stored). Every added row has exactly one source, the platform's
current file through jks-ai (`kind: upstream`), and no edition id. The parent's editions, page
maps, covers and reassembly records are carried over byte for byte (only `release_id` and
`generated_at` change in catalog.json); its download rows are kept, with two subject-name corrections:

  * a 思想政治-group row whose title is a 道德与法治 book is listed under 道德与法治;
  * the service group 生物 is listed under the curriculum name 生物学 (as in the search corpus).

`subject_group` always keeps the service's group name. Rows of special-education editions
(盲校 / 聋校 / 培智) in the added groups are left out and counted in `not_enumerated`, as the
first build did for those rows outside its groups.

A row found in several added groups gets the most specific subject named in its title
(SUBJECT_PRIORITY), else the first of its groups in that order. Deterministic: release id =
latest group retrieval time + hash of (parent SHA256SUMS, added group files, this script).
"""
from __future__ import annotations

import argparse
import re
import shutil
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_catalog import PHASE_ORDER, counts  # noqa: E402
from common import dumps, file_hashes, read_json, sha256_bytes, sha256_text, write_atomic  # noqa: E402

SUBJECT_PRIORITY = ["信息技术", "信息科技", "通用技术", "体育与健康", "音乐", "美术", "日语", "俄语", "法语",
                    "德语", "西班牙语", "劳动", "艺术", "科学"]
SPECIAL_RE = re.compile(r"盲校|聋校|培智")
SUBJECT_RENAME = {"生物": "生物学"}
PARENT_GROUP_SUBJECTS = {"语文", "数学", "英语", "物理", "化学", "生物", "历史", "地理", "思想政治"}


def clean_subject(row: dict) -> str:
    group = row["subject_group"]
    if group == "思想政治" and "道德与法治" in (row.get("title") or ""):
        return "道德与法治"
    return SUBJECT_RENAME.get(group, group)


def pick_subject(title: str, groups: list[str]) -> str:
    ordered = sorted(set(groups), key=SUBJECT_PRIORITY.index)
    for subject in ordered:
        if subject in title:
            return subject
    return ordered[0]


def build(args) -> int:
    parent_dir, extra_dir, out_root = Path(args.parent), Path(args.extra), Path(args.out)
    parent_release = read_json(parent_dir / "RELEASE.json")
    parent_downloads = read_json(parent_dir / "downloads.json")
    parent_id = parent_release["release_id"]
    known = {r["source_id"] for r in parent_downloads["rows"]}

    group_files = sorted(extra_dir.glob("*__*.json"))
    if not group_files:
        raise SystemExit("no group caches in --extra")
    found: dict[str, dict] = {}
    groups, versions, stamps = [], set(), []
    special = Counter()
    for f in group_files:
        stage, subject = f.stem.split("__")
        if subject not in SUBJECT_PRIORITY:
            raise SystemExit(f"{f.name}: subject {subject!r} is not one of the added groups")
        wrapped = read_json(f)
        versions.add((wrapped.get("headers") or {}).get("x-jks-catalog-version"))
        stamps.append(wrapped["retrieved_at"])
        groups.append({"stage": stage, "subject": subject, "rows": len(wrapped["body"]),
                       "retrieved_at": wrapped.get("retrieved_at")})
        for r in wrapped["body"]:
            sid = r["id"]
            if sid in known:
                continue                  # already listed under one of the first build's groups
            if SPECIAL_RE.search(r.get("title") or ""):
                special[sid] = 1
                continue
            entry = found.setdefault(sid, {"r": r, "stage": stage, "groups": []})
            if entry["stage"] != stage:
                raise SystemExit(f"{sid} appears in two stages")
            entry["groups"].append(subject)

    rows = []
    for row in parent_downloads["rows"]:
        row = dict(row)
        row["subject"] = clean_subject(row)
        rows.append(row)
    for sid, entry in found.items():
        r = entry["r"]
        title = r.get("title") or ""
        subject = pick_subject(title, entry["groups"])
        rows.append({
            "source_id": sid, "stage": entry["stage"], "subject": subject,
            "subject_group": subject if subject in entry["groups"] else sorted(set(entry["groups"]), key=SUBJECT_PRIORITY.index)[0],
            "subject_tag": None, "title": title or None,
            "edition_label": r.get("edition") or None, "publisher": r.get("publisher") or None,
            "cover": r.get("cover_url") or None, "size_bytes": None, "update_time": None,
            "sources": [{"kind": "upstream", "url": f"https://jks-ai.bdfz.net/pdf/{sid}",
                         "edition_id": None, "platform_current": True}],
            "upstream_status": None, "edition_id": None, "held_other_edition_ids": [],
        })
    rows.sort(key=lambda r: (PHASE_ORDER.get(r["stage"], 9), r["subject"], r["title"] or "", r["source_id"]))

    all_groups = parent_downloads["groups"] + groups
    all_groups.sort(key=lambda g: (PHASE_ORDER.get(g["stage"], 9), g["subject"]))
    not_enumerated = [g for g in parent_downloads["not_enumerated"] if "not in the 27 UI groups" not in g["reason"]
                      or g["group"].startswith("特殊教育")]
    not_enumerated.append({"group": "特殊教育 editions (盲校 / 聋校 / 培智) in the groups added in r51",
                           "rows": len(special), "reason": "left out of the download list"})

    inputs = {"parent/SHA256SUMS": file_hashes(parent_dir / "SHA256SUMS", ("sha256",))["sha256"]}
    for f in group_files:
        inputs[f"downloads-extra/{f.name}"] = file_hashes(f, ("sha256",))["sha256"]
    tool = file_hashes(Path(__file__), ("sha256",))["sha256"]
    input_hash = sha256_text(dumps({"inputs": inputs, "tool": tool}))
    stamp = max(stamps)
    release_id = stamp.replace("-", "").replace(":", "") + "-" + input_hash[:8]

    downloads = {"contract": parent_downloads["contract"],
                 "upstream_catalog_versions": sorted(v for v in (set(parent_downloads["upstream_catalog_versions"]) | versions) if v),
                 "groups": all_groups, "not_enumerated": not_enumerated, "rows": rows, "release_id": release_id}
    catalog = read_json(parent_dir / "catalog.json")
    catalog["release_id"] = release_id
    catalog["generated_at"] = stamp
    page_maps = {p.stem: read_json(p) for p in sorted((parent_dir / "page-maps").glob("*.json"))}
    release = dict(parent_release)
    release["release_id"] = release_id
    release["generated_at"] = stamp
    release["derived_from"] = {"release_id": parent_id, "input_hash": parent_release["input_hash"],
                               "generated_at": parent_release["generated_at"],
                               "change": "download list extended with the subject groups the parent did not enumerate; "
                                         "metadata only, source links only; subject names 道德与法治 / 生物学 corrected"}
    release["extension"] = {"tool": "extend_downloads.py", "tool_sha256": tool, "inputs": inputs, "input_hash": input_hash,
                            "added_rows": len(found), "special_education_rows_left_out": len(special),
                            "added_by_stage_subject": dict(Counter(f"{e['stage']}/{pick_subject(e['r'].get('title') or '', e['groups'])}"
                                                                   for e in found.values()))}
    release["counts"] = counts(catalog["editions"], page_maps, downloads,
                               {"newer_platform": release.get("newer_platform"),
                                "indeterminate_platform": release.get("indeterminate_platform")})

    files: dict[str, bytes] = {"catalog.json": dumps(catalog).encode(), "downloads.json": dumps(downloads).encode(),
                               "RELEASE.json": dumps(release).encode()}
    for line in (parent_dir / "SHA256SUMS").read_text().splitlines():
        name = line.split("  ", 1)[1]
        if name in files or name == "SHA256SUMS":
            continue
        files[name] = (parent_dir / name).read_bytes()
    sums = "".join(f"{sha256_bytes(data)}  {name}\n" for name, data in sorted(files.items()))
    files["SHA256SUMS"] = sums.encode()

    target = out_root / release_id
    if target.exists():
        if (target / "SHA256SUMS").read_bytes() == files["SHA256SUMS"]:
            print(f"unchanged {release_id}")
            return 0
        raise SystemExit(f"release {release_id} exists with different content; refusing to overwrite")
    tmp = out_root / f".{release_id}.tmp"
    if tmp.exists():
        shutil.rmtree(tmp)
    for name, data in files.items():
        write_atomic(tmp / name, data)
    tmp.rename(target)
    print(f"built {release_id} files={len(files)} rows={len(rows)} added={len(found)} special_left_out={len(special)}")
    print(target)
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--parent", required=True)
    p.add_argument("--extra", required=True)
    p.add_argument("--out", required=True)
    return build(p.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
