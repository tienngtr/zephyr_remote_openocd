# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import re

from tests.unit.test_documentation import ROOT

SRS = ROOT / "docs" / "requirements" / "SRS.md"
TRACEABILITY = ROOT / "docs" / "traceability" / "v1.md"
IDENTIFIER = re.compile(r"^## ((?:REQ|AC)-[A-Z0-9-]+)$", re.MULTILINE)
TRACEABILITY_ROW = re.compile(r"^\| ((?:AC)-[A-Z0-9-]+) \|", re.MULTILINE)


def test_srs_requirement_and_acceptance_identifiers_are_unique():
    identifiers = IDENTIFIER.findall(SRS.read_text())
    assert len(identifiers) == len(set(identifiers))
    assert any(identifier.startswith("REQ-") for identifier in identifiers)
    assert any(identifier.startswith("AC-") for identifier in identifiers)


def test_every_srs_acceptance_criterion_has_traceability():
    srs_acceptance = {
        identifier
        for identifier in IDENTIFIER.findall(SRS.read_text())
        if identifier.startswith("AC-")
    }
    traceability = TRACEABILITY_ROW.findall(TRACEABILITY.read_text())
    assert len(traceability) == len(set(traceability))
    assert srs_acceptance == set(traceability)
