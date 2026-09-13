# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import re

from tests.support import ROOT


def test_user_documentation_does_not_expose_test_gate_identifiers():
    documents = [ROOT / "README.md", *sorted((ROOT / "docs" / "user").glob("*.md"))]
    leaked = [
        str(path.relative_to(ROOT)) for path in documents if re.search(r"PG-\d+", path.read_text())
    ]
    assert leaked == []


def test_hardware_inventory_terminology_is_clear() -> None:
    paths = (
        ROOT / "AGENTS.md",
        ROOT / "CONTRIBUTING.md",
        ROOT / "docs/development/README.md",
        ROOT / "docs/development/testing.md",
        ROOT / "docs/development/hardware_inventories.md",
        ROOT / "docs/validation/README.md",
        ROOT / "docs/architecture/SAD.md",
        ROOT / "scripts/release_validate.py",
        ROOT / "tests/conftest.py",
    )
    ambiguous = (
        "ignored " + "inventory",
        "ignored " + "fixture",
        "external " + "fixture",
    )
    matches = [
        f"{path.relative_to(ROOT)}: {phrase}"
        for path in paths
        for phrase in ambiguous
        if phrase in path.read_text(encoding="utf-8").lower()
    ]
    assert matches == []
