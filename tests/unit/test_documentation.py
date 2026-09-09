# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import re
from pathlib import Path

from tests.support import ROOT

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


def test_user_documentation_does_not_expose_test_gate_identifiers():
    documents = [ROOT / "README.md", *sorted((ROOT / "docs" / "user").glob("*.md"))]
    leaked = [
        str(path.relative_to(ROOT)) for path in documents if re.search(r"PG-\d+", path.read_text())
    ]
    assert leaked == []
