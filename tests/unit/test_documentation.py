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
