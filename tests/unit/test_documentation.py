# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MARKDOWN_LINK = re.compile(r"\[[^]]*\]\(([^)]+)\)")
EXCLUDED_PARTS = {".git", ".scratch", ".venv", ".mypy_cache", ".ruff_cache"}


def markdown_files() -> list[Path]:
    return sorted(
        path for path in ROOT.rglob("*.md") if not EXCLUDED_PARTS.intersection(path.parts)
    )


def test_repository_relative_markdown_links_resolve():
    missing = []
    for document in markdown_files():
        for target in MARKDOWN_LINK.findall(document.read_text()):
            if "://" in target or target.startswith(("#", "mailto:")):
                continue
            relative_target = target.split("#", maxsplit=1)[0]
            if not relative_target:
                continue
            resolved = (document.parent / relative_target).resolve()
            if not resolved.exists():
                missing.append(f"{document.relative_to(ROOT)} -> {target}")
    assert missing == []


def test_removed_document_paths_are_not_referenced():
    stale = []
    for document in markdown_files():
        text = document.read_text()
        for path in ("doc/SRS.md", "doc/SAD.md", "doc/startup-overhead"):
            if path in text:
                stale.append(f"{document.relative_to(ROOT)} contains {path}")
    assert stale == []
