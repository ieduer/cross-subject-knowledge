#!/usr/bin/env python3
"""Rebuild the download list of a catalog release from the platform's own catalog parts.

  refresh_downloads.py --parent <releases>/<id> --parts <dir> --out <releases> --stamp <YYYY-MM-DDTHH:MM:SSZ>

`--parts` holds `data_version.json` (module, module_version, urls) and the part files it names, saved as
`part_<n>.json` in the order of `urls`. The parts list every record the platform currently offers
(tch_material, ONLINE only); a withdrawn record is simply absent. Metadata only: no PDF is fetched.

Carried from the parent byte for byte: editions, page maps, covers, reassembly records (only
`release_id` / `generated_at` change in catalog.json). The download list is rebuilt:

  * scope: records whose 学段 tag is 小学 / 初中 / 高中 (五•四学制 folded into 小学 / 初中); records tagged
    特殊教育 or without a 学段 / 学科 tag, and 教师用书, left out; 盲校 / 聋校 / 培智 books the platform tags
    as 小学 / 初中 / 高中 stay in the subjects the first catalog listed them under (CORE_SUBJECTS) and are
    not added elsewhere (no 培智-only subjects such as 律动 or 沟通与交往);
  * 学科 from the platform tag with the list's display names (SUBJECT_MAP; a 艺术 record titled 音乐 /
    美术 is listed there); `subject_group` / `subject_tag` keep the raw tag;
  * publisher from `provider_list`, 版本 from the zxxbb tag, size from the PDF source item;
  * one row per book among rows without a file held on this site: same 学段, 学科, publisher, the
    platform's 版本 / 年级 / 册次 tags and title once the curriculum-revision marker and punctuation are
    removed (titles alone are not enough: the platform shortens them, e.g. without 上册 / 下册); a revised
    title first, then the later online / update time (extend_downloads.recency);
  * every row links the platform's current file (`jks-ai.bdfz.net/pdf/<id>`, resolved at click time);
    a row whose id has a file held here also offers that file, exactly as build_catalog.build_downloads;
  * a parent row with a file held here whose id the platform no longer offers keeps the held file only.

RELEASE.json records the parent, the module version, the part hashes, the rules, the counts and the
diff against the parent (added / withdrawn / changed rows). Deterministic for the same inputs.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_catalog import PHASE_ORDER, counts  # noqa: E402
from common import dumps, file_hashes, read_json, sha256_bytes, sha256_text, write_atomic  # noqa: E402
from extend_downloads import recency, work_key as title_key  # noqa: E402

UPSTREAM_PDF = "https://jks-ai.bdfz.net/pdf/"
COVER = "https://jks-ai.bdfz.net/cover/"
STAGES = {"小学": "小学", "初中": "初中", "高中": "高中", "小学（五•四学制）": "小学", "初中（五•四学制）": "初中"}
SUBJECT_MAP = {"艺术·音乐": "音乐", "艺术·美术": "美术", "艺术·舞蹈/影视/戏剧": "艺术", "语文·书法练习指导": "语文",
               "地理图册": "地理", "英语（三年级起点）": "英语", "中国历史": "历史", "世界历史": "历史", "生物": "生物学"}
TEACHER_RE = re.compile(r"教师用书")
SPECIAL_RE = re.compile(r"盲校|聋校|培智")
CORE_SUBJECTS = {"语文", "数学", "英语", "物理", "化学", "生物学", "历史", "地理", "思想政治", "道德与法治"}
MIN_RECORDS = 1000


def tag(item: dict, dim: str) -> str | None:
    return next((t.get("tag_name") for t in item.get("tag_list") or [] if t.get("tag_dimension_id") == dim), None)


def display_subject(raw: str, title: str) -> str:
    subject = SUBJECT_MAP.get(raw, raw)
    if subject == "艺术":
        for name in ("音乐", "美术"):
            if name in title:
                return name
    return subject


def read_parts(parts_dir: Path) -> tuple[dict, list[dict], dict]:
    version = read_json(parts_dir / "data_version.json")
    urls = version["urls"].split(",") if isinstance(version.get("urls"), str) else list(version.get("urls") or [])
    if not urls:
        raise SystemExit("data_version.json names no parts")
    items, hashes = [], {"data_version.json": file_hashes(parts_dir / "data_version.json", ("sha256",))["sha256"]}
    for n in range(1, len(urls) + 1):
        f = parts_dir / f"part_{n}.json"
        body = read_json(f)
        if not isinstance(body, list):
            raise SystemExit(f"{f.name} is not a list")
        items += body
        hashes[f.name] = file_hashes(f, ("sha256",))["sha256"]
    if len(items) < MIN_RECORDS:
        raise SystemExit(f"parts hold {len(items)} records, fewer than {MIN_RECORDS}")
    if len({x.get("id") for x in items}) != len(items):
        raise SystemExit("parts repeat an id")
    return version, items, hashes


def held_maps(editions: list[dict]):
    """The same source attribution as build_catalog.build_downloads."""
    current, pending, other = {}, {}, {}
    for e in editions:
        sid = e["source"]["source_id"]
        if not sid or e.get("holding") == "learn-only":
            continue
        if not e.get("pdf"):
            pending[sid] = e["edition_id"]
        elif e["platform_current"]:
            current[sid] = e["edition_id"]
        else:
            other.setdefault(sid, []).append(e["edition_id"])
    return current, pending, other


def build(args) -> int:
    parent_dir, parts_dir, out_root = Path(args.parent), Path(args.parts), Path(args.out)
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", args.stamp):
        raise SystemExit("--stamp must be YYYY-MM-DDTHH:MM:SSZ")
    parent_release = read_json(parent_dir / "RELEASE.json")
    parent_downloads = read_json(parent_dir / "downloads.json")
    catalog = read_json(parent_dir / "catalog.json")
    editions = catalog["editions"]
    by_eid = {e["edition_id"]: e for e in editions}
    current, pending, other = held_maps(editions)
    version, items, part_hashes = read_parts(parts_dir)

    left_out = Counter()
    rows = []
    for item in items:
        sid, title = item.get("id"), (item.get("title") or "").strip()
        stage_raw, subject_raw = tag(item, "zxxxd"), tag(item, "zxxxk")
        if item.get("status") != "ONLINE":
            left_out["not ONLINE"] += 1
            continue
        if stage_raw not in STAGES:
            left_out["no school 学段 tag (特殊教育, untagged, other)"] += 1
            continue
        if TEACHER_RE.search(title):
            left_out["教师用书"] += 1
            continue
        if not subject_raw or not sid or not title:
            left_out["no 学科 tag, id or title"] += 1
            continue
        if SPECIAL_RE.search(title) and display_subject(subject_raw, title) not in CORE_SUBJECTS:
            left_out["特殊教育 title outside the first catalog's subjects"] += 1
            continue
        size = next((int(t["ti_size"]) for t in item.get("ti_items") or []
                     if (t.get("ti_format") or "").lower() == "pdf" and t.get("ti_file_flag") == "source" and t.get("ti_size")), None)
        sources = [{"kind": "upstream", "url": UPSTREAM_PDF + sid, "edition_id": current.get(sid) or pending.get(sid),
                    "platform_current": True}]
        for eid in [current.get(sid)] + sorted(other.get(sid, [])):
            vps = by_eid[eid]["storage"]["vps"] if eid else None
            if vps:
                sources.append({"kind": "vps", "path": vps["path"], "edition_id": eid,
                                "platform_current": by_eid[eid]["platform_current"]})
        provider = next((x.get("name") for x in item.get("provider_list") or [] if x.get("name")), None)
        rows.append({
            "source_id": sid, "stage": STAGES[stage_raw], "subject": display_subject(subject_raw, title),
            "subject_group": subject_raw, "subject_tag": subject_raw, "title": title,
            "edition_label": tag(item, "zxxbb"), "publisher": provider, "cover": COVER + sid,
            "size_bytes": size, "update_time": item.get("update_time"), "upstream_status": "ONLINE",
            "sources": sources, "edition_id": current.get(sid), "held_other_edition_ids": sorted(other.get(sid, [])),
            "_facts": {"online_time": item.get("online_time"), "update_time": item.get("update_time"),
                       "create_time": item.get("create_time")},
            "_tags": (tag(item, "zxxbb") or "", tag(item, "zxxnj") or "", tag(item, "zxxcc") or ""),
        })

    # one row per book among rows without a held file
    kept, superseded, groups_by_work = [], [], {}
    for row in rows:
        if row["edition_id"] or any(s["kind"] == "vps" for s in row["sources"]):
            kept.append(row)
            continue
        groups_by_work.setdefault(title_key(row, None) + row["_tags"], []).append(row)
    for members in groups_by_work.values():
        members.sort(key=lambda r: recency(r, r["_facts"]), reverse=True)
        kept.append(members[0])
        for r in members[1:]:
            superseded.append({"source_id": r["source_id"], "title": r["title"], "publisher": r["publisher"],
                               "kept": members[0]["source_id"]})
    for row in kept:
        row.pop("_facts"); row.pop("_tags")

    # parent rows with a held file whose id the platform no longer offers keep that file only
    listed = {r["source_id"] for r in kept}
    online = {x.get("id") for x in items}
    carried_held = 0
    for row in parent_downloads["rows"]:
        if row["source_id"] in listed:
            continue
        held = [s for s in row["sources"] if s["kind"] == "vps"]
        if held:
            row = {**row, "sources": held}
            if row["source_id"] not in online:
                row["upstream_status"] = "OFFLINE"
            kept.append(row)
            carried_held += 1
    kept.sort(key=lambda r: (PHASE_ORDER.get(r["stage"], 9), r["subject"], r["title"] or "", r["source_id"]))

    # diff against the parent
    before = {r["source_id"]: r for r in parent_downloads["rows"]}
    after = {r["source_id"]: r for r in kept}
    added = sorted(set(after) - set(before))
    withdrawn = sorted(set(before) - set(after))
    changed = Counter()
    for sid in set(before) & set(after):
        for field in ("publisher", "subject", "stage", "title"):
            if (before[sid].get(field) or "") != (after[sid].get(field) or ""):
                changed[field] += 1
    by = lambda ids, src: dict(Counter(f"{src[i]['stage']}/{src[i]['subject']}" for i in ids))
    diff = {"added": len(added), "withdrawn": len(withdrawn), "changed": dict(changed),
            "added_by_stage_subject": by(added, after), "withdrawn_by_stage_subject": by(withdrawn, before),
            "added_ids": added, "withdrawn_ids": withdrawn,
            "subjects_before": sorted({r["subject"] for r in before.values()}),
            "subjects_after": sorted({r["subject"] for r in after.values()})}

    module_version = version.get("module_version")
    inputs = {"parent/SHA256SUMS": file_hashes(parent_dir / "SHA256SUMS", ("sha256",))["sha256"],
              **{"parts/" + k: v for k, v in sorted(part_hashes.items())}}
    tool = {Path(p).name: file_hashes(Path(p), ("sha256",))["sha256"] for p in
            (__file__, Path(__file__).with_name("extend_downloads.py"), Path(__file__).with_name("build_catalog.py"))}
    input_hash = sha256_text(dumps({"inputs": inputs, "tool": tool}))
    release_id = args.stamp.replace("-", "").replace(":", "") + "-" + input_hash[:8]

    groups = sorted(({"stage": s, "subject": j, "rows": n} for (s, j), n in Counter((r["stage"], r["subject"]) for r in kept).items()),
                    key=lambda g: (PHASE_ORDER.get(g["stage"], 9), g["subject"]))
    downloads = {"contract": parent_downloads["contract"], "release_id": release_id, "rows": kept, "groups": groups,
                 "upstream_catalog_versions": [str(module_version)],
                 "not_enumerated": [{"group": k, "rows": v, "reason": "left out by the refresh rules"} for k, v in sorted(left_out.items())]}
    catalog["release_id"] = release_id
    catalog["generated_at"] = args.stamp
    page_maps = {p.stem: read_json(p) for p in sorted((parent_dir / "page-maps").glob("*.json"))}
    release = {k: v for k, v in parent_release.items() if k not in ("extension", "derived_from", "refresh")}
    release.update({"release_id": release_id, "generated_at": args.stamp,
                    "derived_from": {"release_id": parent_release["release_id"], "input_hash": parent_release["input_hash"],
                                     "generated_at": parent_release["generated_at"],
                                     "change": "download list rebuilt from the platform's catalog parts (refresh_downloads.py)"},
                    "refresh": {"tool": tool, "inputs": inputs, "input_hash": input_hash, "module": version.get("module"),
                                "module_version": module_version, "records": len(items), "left_out": dict(left_out),
                                "rows": len(kept), "carried_held_rows": carried_held,
                                "superseded_left_out": sorted(superseded, key=lambda x: x["source_id"]), "diff": diff}})
    release["counts"] = counts(editions, page_maps, downloads,
                               {"newer_platform": release.get("newer_platform"),
                                "indeterminate_platform": release.get("indeterminate_platform")})

    files: dict[str, bytes] = {"catalog.json": dumps(catalog).encode(), "downloads.json": dumps(downloads).encode(),
                               "RELEASE.json": dumps(release).encode()}
    for line in (parent_dir / "SHA256SUMS").read_text().splitlines():
        name = line.split("  ", 1)[1]
        if name not in files and name != "SHA256SUMS":
            files[name] = (parent_dir / name).read_bytes()
    files["SHA256SUMS"] = "".join(f"{sha256_bytes(d)}  {n}\n" for n, d in sorted(files.items())).encode()

    target = out_root / release_id
    if target.exists():
        if (target / "SHA256SUMS").read_bytes() == files["SHA256SUMS"]:
            print(json.dumps({"release_id": release_id, "unchanged": True, **{k: diff[k] for k in ("added", "withdrawn")}}))
            return 0
        raise SystemExit(f"release {release_id} exists with different content; refusing to overwrite")
    tmp = out_root / f".{release_id}.tmp"
    if tmp.exists():
        shutil.rmtree(tmp)
    for name, data in files.items():
        write_atomic(tmp / name, data)
    tmp.rename(target)
    print(json.dumps({"release_id": release_id, "rows": len(kept), "added": diff["added"], "withdrawn": diff["withdrawn"],
                      "changed": diff["changed"], "superseded": len(superseded), "module_version": module_version}, ensure_ascii=False))
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--parent", required=True)
    p.add_argument("--parts", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--stamp", required=True, help="retrieval time of the parts, YYYY-MM-DDTHH:MM:SSZ")
    return build(p.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
