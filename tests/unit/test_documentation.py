# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MARKDOWN_LINK = re.compile(r"\[[^]]*\]\(([^)]+)\)")
EXCLUDED_PARTS = {".agents", ".git", ".scratch", ".venv", ".mypy_cache", ".ruff_cache"}


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


def test_user_documentation_does_not_expose_test_gate_identifiers():
    documents = [ROOT / "README.md", *sorted((ROOT / "docs" / "user").glob("*.md"))]
    leaked = [
        str(path.relative_to(ROOT)) for path in documents if re.search(r"PG-\d+", path.read_text())
    ]
    assert leaked == []


def test_documents_do_not_present_development_contracts_as_v1_or_frozen():
    misleading = re.compile(r"\bV1\b|\bfrozen\b|\bfreeze(?:d|s|ing)?\b", re.IGNORECASE)
    occurrences = []
    for document in markdown_files():
        for line_number, line in enumerate(document.read_text().splitlines(), 1):
            if misleading.search(line):
                occurrences.append(f"{document.relative_to(ROOT)}:{line_number}: {line.strip()}")
    assert occurrences == []


def test_agent_guidance_preserves_contract_change_control():
    guidance = (ROOT / "AGENTS.md").read_text()
    assert "explicitly authorizes a configuration-contract" in guidance
    assert "explicitly authorizes a protocol-contract" in guidance
    assert guidance.count("Do not invent a new") == 2
