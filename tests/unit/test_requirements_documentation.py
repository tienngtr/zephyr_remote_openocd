# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SRS = ROOT / "docs" / "requirements" / "SRS.md"
TRACEABILITY = ROOT / "docs" / "traceability" / "v1.md"
IDENTIFIER = re.compile(r"^## ((?:REQ|AC)-[A-Z0-9-]+)$", re.MULTILINE)
TRACEABILITY_ROW = re.compile(r"^\| ((?:AC)-[A-Z0-9-]+) \|", re.MULTILINE)


class RequirementsDocumentationTests(unittest.TestCase):
    def test_srs_requirement_and_acceptance_identifiers_are_unique(self):
        identifiers = IDENTIFIER.findall(SRS.read_text())
        self.assertEqual(len(identifiers), len(set(identifiers)))
        self.assertTrue(any(identifier.startswith("REQ-") for identifier in identifiers))
        self.assertTrue(any(identifier.startswith("AC-") for identifier in identifiers))

    def test_every_srs_acceptance_criterion_has_traceability(self):
        srs_acceptance = {
            identifier
            for identifier in IDENTIFIER.findall(SRS.read_text())
            if identifier.startswith("AC-")
        }
        traceability = TRACEABILITY_ROW.findall(TRACEABILITY.read_text())
        self.assertEqual(len(traceability), len(set(traceability)))
        self.assertEqual(srs_acceptance, set(traceability))
