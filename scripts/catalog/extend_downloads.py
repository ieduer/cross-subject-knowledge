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

r52 (with --details, the SmartEdu details JSON of each row, read-only and cached):
  * a row listed under the platform's own name (智慧中小学) or none gets the publisher the record names
    (provider_list), the listed value kept as `publisher_listed`; size, update time and the subject
    tag are filled in from the same record;
  * a record the platform marks OFFLINE loses its platform link (rows of the parent list stay only for a
    file held on this site); among the added rows such a record is left out, and
    of the records for one book — same 学段, 学科, publisher and title — only
    the platform's current record is listed (same title once the revision marker is removed): a title
    revised to a newer curriculum standard first, then the later online / update time. The left-out ids are named in RELEASE.json.

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
import unicodedata
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
PLATFORM_PUBLISHERS = {"智慧中小学", ""}
REVISION_RE = re.compile(r"根据(\d{4})年版课程标准修订")
NOTATION_RE = re.compile(r"五线谱|简谱")


def details_facts(details_dir: Path | None, sid: str) -> dict | None:
    if details_dir is None:
        return None
    f = details_dir / f"{sid}.json"
    if not f.exists():
        return None
    body = read_json(f).get("body") or {}
    tags = {t.get("tag_dimension_id"): t.get("tag_name") for t in body.get("tag_list") or [] if t.get("tag_dimension_id")}
    size = next((int(t["ti_size"]) for t in body.get("ti_items") or []
                 if (t.get("ti_format") or "").lower() == "pdf" and t.get("ti_file_flag") == "source" and t.get("ti_size")), None)
    provider = next((x.get("name") for x in body.get("provider_list") or [] if x.get("name")), None)
    return {"status": body.get("status"), "update_time": body.get("update_time"), "online_time": body.get("online_time"),
            "create_time": body.get("create_time"), "size": size, "provider": provider, "tags": tags}


def apply_facts(row: dict, facts: dict | None, *, status: bool) -> None:
    if not facts:
        return
    if (row.get("publisher") or "") in PLATFORM_PUBLISHERS and facts["provider"] and facts["provider"] not in PLATFORM_PUBLISHERS:
        row["publisher_listed"] = row.get("publisher")
        row["publisher"] = facts["provider"]
    if row.get("size_bytes") is None and facts["size"]:
        row["size_bytes"] = facts["size"]
    row["subject_tag"] = row.get("subject_tag") or facts["tags"].get("zxxxk")
    if status:
        row["update_time"] = facts["update_time"]
        row["upstream_status"] = facts["status"]


def work_key(row: dict, facts: dict | None) -> tuple:
    """One book = same 学段, 学科, publisher and title once the curriculum-revision marker and punctuation are
    removed. The platform's tags are not used: they do not tell 舞蹈 / 影视 / 戏剧 or 五•四学制 editions apart."""
    title = REVISION_RE.sub("", unicodedata.normalize("NFKC", row.get("title") or ""))
    title = re.sub(r"[\s·•・()（）_]", "", title)
    return (row["stage"], row["subject"], title, row.get("publisher") or "")


def recency(row: dict, facts: dict | None) -> tuple:
    revision = max((int(y) for y in REVISION_RE.findall(row.get("title") or "")), default=0)
    f = facts or {}
    return (revision, f.get("online_time") or "", f.get("update_time") or "", f.get("create_time") or "", row["source_id"])
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
    details_dir = Path(args.details) if getattr(args, "details", "") else None
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
    publisher_fixed = 0
    offline = []
    for row in parent_downloads["rows"]:
        row = dict(row)
        row["subject"] = clean_subject(row)
        facts = details_facts(details_dir, row["source_id"])
        apply_facts(row, facts, status=False)
        publisher_fixed += "publisher_listed" in row
        if facts and facts["status"] == "OFFLINE":
            # the platform no longer serves this record: no upstream link; the row stays only for a file held here
            row["upstream_status"] = "OFFLINE"
            row["sources"] = [x for x in row["sources"] if x["kind"] != "upstream"]
            if not row["sources"]:
                offline.append({"source_id": row["source_id"], "title": row["title"], "listed_before": True})
                continue
        rows.append(row)
    added = []
    for sid, entry in found.items():
        r = entry["r"]
        title = r.get("title") or ""
        subject = pick_subject(title, entry["groups"])
        added.append({
            "source_id": sid, "stage": entry["stage"], "subject": subject,
            "subject_group": subject if subject in entry["groups"] else sorted(set(entry["groups"]), key=SUBJECT_PRIORITY.index)[0],
            "subject_tag": None, "title": title or None,
            "edition_label": r.get("edition") or None, "publisher": r.get("publisher") or None,
            "cover": r.get("cover_url") or None, "size_bytes": None, "update_time": None,
            "sources": [{"kind": "upstream", "url": f"https://jks-ai.bdfz.net/pdf/{sid}",
                         "edition_id": None, "platform_current": True}],
            "upstream_status": None, "edition_id": None, "held_other_edition_ids": [],
        })
    # r52: details facts, OFFLINE records, one current record per book
    groups_by_work, facts_of = {}, {}
    for row in added:
        facts = facts_of[row["source_id"]] = details_facts(details_dir, row["source_id"])
        apply_facts(row, facts, status=True)
        publisher_fixed += "publisher_listed" in row
        if row["upstream_status"] == "OFFLINE":
            offline.append({"source_id": row["source_id"], "title": row["title"]})
            continue
        groups_by_work.setdefault(work_key(row, facts), []).append(row)
    superseded, kept_added = [], []
    for members in groups_by_work.values():
        members.sort(key=lambda r: recency(r, facts_of[r["source_id"]]), reverse=True)
        rows.append(members[0])
        kept_added.append(members[0])
        for r in members[1:]:
            superseded.append({"source_id": r["source_id"], "title": r["title"], "publisher": r["publisher"],
                               "kept": members[0]["source_id"], "kept_title": members[0]["title"]})
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
    if details_dir:
        inputs["details/*"] = sha256_text("\n".join(f"{f.name}:{file_hashes(f, ('sha256',))['sha256']}"
                                                    for f in sorted(details_dir.glob("*-*-*-*-*.json"))))
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
                                         "metadata only, source links only; subject names 道德与法治 / 生物学 corrected"
                                         + ("; with the platform's details records: publishers named, OFFLINE records and "
                                            "superseded records of the same book left out" if details_dir else "")}
    release["extension"] = {"tool": "extend_downloads.py", "tool_sha256": tool, "inputs": inputs, "input_hash": input_hash,
                            "added_rows": len(kept_added), "special_education_rows_left_out": len(special),
                            "details_dir_files": len(list(details_dir.glob("*-*-*-*-*.json"))) if details_dir else 0,
                            "publisher_corrected_rows": publisher_fixed,
                            "offline_left_out": offline, "superseded_left_out": sorted(superseded, key=lambda x: x["source_id"]),
                            "added_by_stage_subject": dict(Counter(f"{r['stage']}/{r['subject']}" for r in kept_added))}
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
    print(f"built {release_id} files={len(files)} rows={len(rows)} added={len(kept_added)} "
          f"special_left_out={len(special)} offline={len(offline)} superseded={len(superseded)} publisher_fixed={publisher_fixed}")
    print(target)
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--parent", required=True)
    p.add_argument("--extra", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--details", default="", help="directory of cached SmartEdu details JSON (fetch_metadata.py details)")
    return build(p.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
