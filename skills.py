"""Reusable "skill" discovery — the SKILL.md convention popularized by Claude
Code / OpenClaw.

A skill is a directory under ``<workspace>/skills/`` containing a ``SKILL.md``
file. The file starts with a small frontmatter block (``name`` / ``description``)
followed by free-form Markdown instructions. Extra files placed alongside
``SKILL.md`` (scripts, templates, sample data, ...) are treated as skill
resources: the agent reads them on demand with the existing ``read_file`` tool
once it has loaded the skill, instead of everything being crammed into the
system prompt up front (progressive disclosure).

This module has no framework dependencies (no PyYAML) — the frontmatter is a
flat ``key: value`` block, which is all the ``name``/``description`` fields need.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
import re

SKILLS_DIRNAME = "skills"
_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?(.*)\Z", re.S)


def _parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    match = _FRONTMATTER_RE.match(text)
    if not match:
        return {}, text.strip()
    meta: dict[str, str] = {}
    for line in match.group(1).splitlines():
        key, sep, value = line.partition(":")
        if sep:
            meta[key.strip()] = value.strip().strip("\"'")
    return meta, match.group(2).strip()


def list_skills(skills_root: Path) -> list[dict[str, str]]:
    """Return ``{name, description, dir}`` for every ``<dir>/SKILL.md`` under skills_root."""
    if not skills_root.is_dir():
        return []
    items: list[dict[str, str]] = []
    for skill_dir in sorted(p for p in skills_root.iterdir() if p.is_dir()):
        skill_file = skill_dir / "SKILL.md"
        if not skill_file.is_file():
            continue
        try:
            meta, _ = _parse_frontmatter(skill_file.read_text(encoding="utf-8"))
        except OSError:
            continue
        items.append({
            "name": meta.get("name") or skill_dir.name,
            "description": meta.get("description") or "(無說明)",
            "dir": skill_dir.name,
        })
    return items


def load_skill(skills_root: Path, name: str, allowed_root: Path) -> dict[str, Any]:
    """Return the full instructions + resource file list for one skill."""
    skills = list_skills(skills_root)
    match = next(
        (s for s in skills if name.strip().lower() in {s["name"].lower(), s["dir"].lower()}),
        None,
    )
    if not match:
        available = "、".join(s["name"] for s in skills) or "(尚無任何技能)"
        raise FileNotFoundError(f"找不到技能「{name}」。目前可用技能：{available}")
    skill_dir = skills_root / match["dir"]
    _, body = _parse_frontmatter((skill_dir / "SKILL.md").read_text(encoding="utf-8"))
    resources = sorted(
        str(path.relative_to(allowed_root))
        for path in skill_dir.rglob("*")
        if path.is_file() and path.name != "SKILL.md"
    )
    return {"name": match["name"], "description": match["description"], "instructions": body, "resources": resources}
