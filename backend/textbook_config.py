#!/usr/bin/env python3
"""
Unified textbook configuration — single source of truth for subject,
edition, phase, and visibility policies.

Canonical location: scripts/textbook_config.py
Synced copy:        platform/backend/textbook_config.py  (via sync_shared_config.sh)

Both copies are version-controlled.  Release/deploy scripts fail-fast
if they diverge.
"""

from __future__ import annotations

# ── Subject three-layer model ─────────────────────────────────────────────

# Layer 1: aliases → canonical subject name
SUBJECT_ALIASES: dict[str, str] = {
    "生物": "生物学",
    "政治": "思想政治",
    "外语": "英语",
    "道法": "道德与法治",
    "道德与法治": "道德与法治",  # identity — keeps canonical explicit
}

# Layer 2: canonical subject → icon / color
CANONICAL_SUBJECT_META: dict[str, dict[str, str]] = {
    "语文":     {"icon": "📖", "color": "#e74c3c"},
    "数学":     {"icon": "📐", "color": "#3498db"},
    "英语":     {"icon": "🌍", "color": "#2ecc71"},
    "物理":     {"icon": "⚛️", "color": "#9b59b6"},
    "化学":     {"icon": "🧪", "color": "#e67e22"},
    "生物学":   {"icon": "🧬", "color": "#1abc9c"},
    "历史":     {"icon": "📜", "color": "#f39c12"},
    "地理":     {"icon": "🗺️", "color": "#16a085"},
    "思想政治": {"icon": "⚖️", "color": "#c0392b"},
    "道德与法治": {"icon": "⚖️", "color": "#c0392b"},
}

# Layer 3: subject family (for cross-subject mapping)
SUBJECT_FAMILY: dict[str, str] = {
    "生物学":   "生命科学",
    "思想政治": "德育",
    "道德与法治": "德育",
}

# Phase-dependent display name overrides
# Key: (phase, canonical_subject) → display_subject
# If absent, display_subject == canonical_subject
PHASE_DISPLAY_SUBJECT: dict[tuple[str, str], str] = {
    ("初中", "生物学"):   "生物",
    ("初中", "道德与法治"): "道德与法治",
    ("高中", "思想政治"):  "思想政治",
}


def normalize_subject(raw: str) -> str:
    """Map any alias to canonical subject name.  Returns raw if not aliased."""
    s = raw.strip()
    return SUBJECT_ALIASES.get(s, s)


def display_subject(phase: str, canonical: str) -> str:
    """Phase-aware display name for a canonical subject."""
    return PHASE_DISPLAY_SUBJECT.get((phase, canonical), canonical)


def subject_family(canonical: str) -> str:
    """Return the subject family, or the canonical name itself."""
    return SUBJECT_FAMILY.get(canonical, canonical)


def subject_meta(phase: str) -> dict[str, dict[str, str]]:
    """Return full subject metadata for a phase, keyed by canonical subject.

    Each value contains: icon, color, display_subject.
    """
    result: dict[str, dict[str, str]] = {}
    for subj, meta in CANONICAL_SUBJECT_META.items():
        # Filter out subjects not used in this phase
        if phase == "高中" and subj == "道德与法治":
            continue
        if phase == "初中" and subj == "思想政治":
            continue
        result[subj] = {
            "icon": meta["icon"],
            "color": meta["color"],
            "display_subject": display_subject(phase, subj),
        }
    return result


# ── Edition detection ─────────────────────────────────────────────────────

EDITION_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("A版", ("（A版）", "(A版)", " A版", "_A版_", " A 版", "人民教育出版社 ·北京· A版", "人民教育出版社A版")),
    ("B版", ("（B版）", "(B版)", " B版", "_B版_", " B 版", "中学数学教材实验研究组", "数学（B版）", "数学(B版)")),
    ("北师大版", ("北师大版", "北京师范大学出版社", "北京师范大学出版社高中数学编辑室", "王尚志", "保继光", "主编王蔷")),
    ("冀教版", ("冀教版", "河北教育出版社")),
    ("外研社版", ("外语教学与研究出版社", "外研社", "Foreign Language Teaching and Research Press", "陈琳")),
    ("上外教版", ("上海外语教育出版社", "束定芳", "上海外国语大学")),
    ("重大版", ("重庆大学出版社", "杨晓钰")),
    ("沪教版", ("上海教育出版社", "上海教育出版社有限公司", "牛津大学出版社", "华东师范大学", "上海市中小学（幼儿园）课程改革委员会组织编写")),
    ("沪科版", ("上海科学技术出版社", "上海科技教育出版社", "上海世纪出版", "麻生明", "陈寅", "束炳如", "何润伟")),
    ("苏教版", ("苏教版", "江苏凤凰教育出版社", "江苏凤凰出版传媒", "葛军", "李善良", "王祖浩")),
    ("鄂教版", ("湖北教育出版社", "武汉中远印务有限公司", "彭双阶", "胡典顺")),
    ("湘教版", ("湖南教育出版社", "湖南出版中心", "张景中", "黄步高", "邹楚林", "邹伟华")),
    ("鲁科版", ("鲁科版", "山东科学技术出版社", "总主编王磊陈光巨", "陈光巨")),
    ("人教版", ("人民教育出版社", "人民教出版社", "人民都育出版社", "课程教材研究所", "人教版")),
    ("中图版", ("中国地图出版社",)),
    ("人民出版社版", ("人民出版社",)),
)

# Series label patterns — used to infer phase, NOT edition
SERIES_PATTERNS: tuple[tuple[str, str], ...] = (
    ("义务教育教科书", "初中"),
    ("义务教育课程标准实验教科书", "初中"),
    ("普通高中教科书", "高中"),
    ("普通高中课程标准实验教科书", "高中"),
)


# ── Scope / visibility policies ──────────────────────────────────────────

def edition_ok(phase: str, subject: str, edition: str) -> bool:
    """Is this edition allowed for the given phase+subject?"""
    if phase == "高中":
        return (
            edition == "人教版"
            or (subject == "英语" and edition == "北师大版")
            or (subject == "化学" and edition == "鲁科版")
        )
    elif phase == "初中":
        return edition == "人教版"
    return False


def catalog_visible(
    phase: str,
    subject: str,
    edition: str,
    source_role: str,
    has_page_images: bool,
) -> bool:
    """Should this book appear in /api/books?

    Decision uses only five orthogonal dimensions.
    The legacy ``supported`` field is NOT an input — it is a derived/debug field.
    """
    if not edition_ok(phase, subject, edition):
        return False
    if source_role in ("primary", "primary_bound"):
        return True
    if source_role == "supplemental_only":
        return has_page_images
    return False


def search_enabled(phase: str, subject: str, edition: str, source_role: str) -> bool:
    """Should chunks from this book be searchable?"""
    return edition_ok(phase, subject, edition)


def page_image_enabled(
    phase: str,
    subject: str,
    edition: str,
    source_role: str,
    has_page_images: bool,
) -> bool:
    """Can page images be served for this book?"""
    return has_page_images and edition_ok(phase, subject, edition)
