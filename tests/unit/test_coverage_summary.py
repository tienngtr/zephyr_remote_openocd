# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import importlib.util
import sys

from tests.support import ROOT

SPEC = importlib.util.spec_from_file_location(
    "coverage_summary", ROOT / "scripts/coverage_summary.py"
)
assert SPEC is not None and SPEC.loader is not None
coverage_summary = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = coverage_summary
SPEC.loader.exec_module(coverage_summary)

REPORT = """\
| Name | Stmts | Miss | Cover | Missing |
|----- | ----: | ---: | ----: | ------: |
| python/package/module.py | 10 | 2 | 80% | 4-5 |
| python/package/with\\_underscore.py | 5 | 0 | 100% | |
| TOTAL | 15 | 2 | 87% | |
"""


def test_file_rows_link_to_tested_revision(monkeypatch):
    monkeypatch.setenv("GITHUB_SERVER_URL", "https://github.example")
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/project")

    result = coverage_summary.link_file_rows(REPORT, "revision")

    assert (
        "[python/package/module.py]"
        "(https://github.example/owner/project/blob/revision/python/package/module.py)" in result
    )
    assert "blob/revision/python/package/with_underscore.py" in result
    assert "| TOTAL |" in result
    assert "[TOTAL]" not in result


def test_file_rows_remain_plain_outside_github(monkeypatch):
    monkeypatch.delenv("GITHUB_SERVER_URL", raising=False)
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)

    assert coverage_summary.link_file_rows(REPORT, "revision") == REPORT


def test_main_adds_heading_and_appends_output(monkeypatch, tmp_path):
    output = tmp_path / "summary.md"
    output.write_text("existing\n", encoding="utf-8")
    monkeypatch.setattr(coverage_summary, "repository_root", lambda: tmp_path)
    monkeypatch.setattr(coverage_summary, "coverage_markdown", lambda _root: REPORT)

    assert coverage_summary.main(["--revision", "tip", "--output", str(output)]) == 0

    result = output.read_text(encoding="utf-8")
    assert result.startswith("existing\n## Coverage\n\n")
    assert "| Name | Stmts |" in result


def test_cli_defaults_to_head():
    args = coverage_summary.parse_args([])

    assert args.revision == "HEAD"
    assert args.output is None
