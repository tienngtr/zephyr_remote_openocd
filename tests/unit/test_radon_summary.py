# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import importlib.util
import io
import sys
import tarfile
from types import SimpleNamespace

import pytest

from tests.support import ROOT

SPEC = importlib.util.spec_from_file_location("radon_summary", ROOT / "scripts/radon_summary.py")
assert SPEC is not None and SPEC.loader is not None
radon_summary = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = radon_summary
SPEC.loader.exec_module(radon_summary)


def metrics(source: str, *, test: bool = False):
    prefix = "tests/" if test else "python/"
    prefixes = radon_summary.TEST_PREFIXES if test else radon_summary.PRODUCTION_PREFIXES
    return radon_summary.analyze_sources({f"{prefix}example.py": source}, prefixes)


def test_analysis_excludes_test_assertions_from_complexity():
    source = "def check(values):\n    for value in values:\n        assert value\n"
    production = metrics(source)
    tests = metrics(source, test=True)

    assert production.blocks[0].complexity == 3
    assert tests.blocks[0].complexity == 2
    assert tests.maximum_complexity == 2
    assert tests.concerning_blocks == 0
    assert tests.files[0].effort >= 0


def test_callable_changes_match_qualified_names_after_lines_move():
    base = {
        "Production": metrics("class Worker:\n    def run(self, item):\n        return item\n"),
    }
    current = {
        "Production": metrics(
            "\n\nclass Worker:\n"
            "    def run(self, item):\n"
            "        if item:\n"
            "            return item\n"
            "        return None\n"
        ),
    }

    changes = radon_summary._changed_blocks(base, current)

    method = next(change for change in changes if (change[2] or change[1]).name == "Worker.run")
    assert method[1].line == 2
    assert method[2].line == 4
    assert method[1].complexity == 1
    assert method[2].complexity == 2


def test_comparison_renders_points_and_signed_deltas(monkeypatch):
    monkeypatch.setenv("GITHUB_SERVER_URL", "https://github.example")
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/project")
    base_scope = metrics("def work(value):\n    return value\n")
    current_scope = metrics(
        "def work(value):\n    if value:\n        return value\n    return None\n"
    )
    base = {"Production": base_scope}
    current = {"Production": current_scope}

    report = radon_summary.render_comparison(base, current, "base", "current")

    assert "Code complexity changes" in report
    assert "python/example.py#L1" in report
    assert "`work`" in report
    assert "| A (1) | A (2) | +1 |" in report
    assert "File metric changes" in report
    assert (
        radon_summary._location("python/example.py", None, "current")
        == "[python/example.py](https://github.example/owner/project/blob/current/python/example.py)"
    )
    assert "Minimum MI" not in report
    assert "quality threshold" not in report


def test_push_renders_callable_and_file_hotspots():
    branches = "".join(f"    if value == {number}:\n        return value\n" for number in range(11))
    current = {"Production": metrics(f"def complex_work(value):\n{branches}    return None\n")}

    report = radon_summary.render_push(current, "tip")

    assert "Tip revision: `tip`" in report
    assert "Current C-F callable hotspots" in report
    assert "Includes callables with CC 11 or higher" in report
    assert "`complex_work`" in report
    assert "Current file hotspots" in report
    assert "Includes the five lowest-MI and five highest-effort files in each scope" in report
    assert "ordered by lower MI, then higher Halstead effort, then path." in report
    assert "Minimum MI" not in report
    assert "quality threshold" not in report


def test_file_hotspots_order_by_mi_effort_then_path():
    block = radon_summary.BlockMetrics("python/example.py", "F", "work", 1, 1)
    current = {
        "Production": radon_summary.ScopeMetrics(
            (block,),
            (
                radon_summary.FileMetrics("python/b.py", 10.0, 100.0),
                radon_summary.FileMetrics("python/a.py", 10.0, 200.0),
                radon_summary.FileMetrics("python/z.py", 5.0, 1.0),
            ),
        )
    }

    report = "\n".join(radon_summary._file_hotspots(current, "tip"))

    assert report.index("python/z.py") < report.index("python/a.py")
    assert report.index("python/a.py") < report.index("python/b.py")


def test_detail_lists_are_limited():
    assert radon_summary._limited(list(range(25))) == (list(range(20)), 5)


def test_revision_sources_reads_archive_and_reports_git_failure(monkeypatch, tmp_path):
    archive_bytes = io.BytesIO()
    with tarfile.open(fileobj=archive_bytes, mode="w") as archive:
        payload = b"value = 1\n"
        member = tarfile.TarInfo("python/example.py")
        member.size = len(payload)
        archive.addfile(member, io.BytesIO(payload))

    monkeypatch.setattr(
        radon_summary.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0, stdout=archive_bytes.getvalue(), stderr=b""
        ),
    )
    assert radon_summary.revision_sources(tmp_path, "tip") == {"python/example.py": "value = 1\n"}

    monkeypatch.setattr(
        radon_summary.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=128, stdout=b"", stderr=b"unknown revision"
        ),
    )
    with pytest.raises(RuntimeError, match="unknown revision"):
        radon_summary.revision_sources(tmp_path, "missing")
