# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from tests.support import ROOT

SPEC = importlib.util.spec_from_file_location("static_check", ROOT / "scripts/static_check.py")
assert SPEC is not None and SPEC.loader is not None
static_check = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = static_check
SPEC.loader.exec_module(static_check)


def test_commands_cover_repository_static_checks():
    commands = static_check.commands(("one.py", "two.py"))

    assert [
        command[2] if len(command) > 2 and command[1] == "-m" else Path(command[0]).name
        for command in commands
    ] == [
        "ruff",
        "ruff",
        "pylint",
        "vermin",
        "git",
    ]
    jobs = commands[2].index("-j")
    assert commands[2][jobs + 1] == "1"
    assert commands[-1] == ("git", "diff", "--check", "HEAD")


def test_source_files_are_selected_from_git(monkeypatch):
    class Result:
        stdout = "one.py\ntwo.py\n"

    monkeypatch.setattr(static_check.subprocess, "run", lambda *args, **kwargs: Result())
    assert static_check.source_files(ROOT) == ("one.py", "two.py")


def test_repository_root_is_reported_by_git(monkeypatch, tmp_path):
    class Result:
        stdout = f"{tmp_path}\n"

    monkeypatch.setattr(static_check.subprocess, "run", lambda *args, **kwargs: Result())
    assert static_check.repository_root() == tmp_path.resolve()
