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
    commands = static_check.commands(
        ("one.py", "two.py"),
        (".github/workflows/check.yml", "config.yaml.example"),
        ("README.md", "docs/guide.md"),
    )

    def tool(command):
        return command[2] if len(command) > 2 and command[1] == "-m" else Path(command[0]).name

    tools = [tool(command) for command in commands]
    assert {
        "actionlint",
        "check-jsonschema",
        "git",
        "mypy",
        "pylint",
        "ruff",
        "rumdl",
        "vermin",
        "yamllint",
    }.issubset(tools)

    for name in ("mypy", "pylint", "vermin"):
        command = next(command for command in commands if tool(command) == name)
        assert {"one.py", "two.py"}.issubset(command)

    yamllint = next(command for command in commands if tool(command) == "yamllint")
    assert {".github/workflows/check.yml", "config.yaml.example"}.issubset(yamllint)

    actionlint = next(command for command in commands if tool(command) == "actionlint")
    assert "-no-color" in actionlint
    assert ".github/workflows/check.yml" in actionlint
    assert "config.yaml.example" not in actionlint

    rumdl = next(command for command in commands if tool(command) == "rumdl")
    assert {"README.md", "docs/guide.md"}.issubset(rumdl)
    assert "one.py" not in rumdl
    assert "config.yaml.example" not in rumdl
    assert "MD001,MD025,MD041,MD051,MD057" in rumdl
    assert "gfm" in rumdl
    assert "--no-cache" in rumdl

    schema_checks = [command for command in commands if tool(command) == "check-jsonschema"]
    assert any("--check-metaschema" in command for command in schema_checks)
    example_check = next(command for command in schema_checks if "--schemafile" in command)
    assert "python/zephyr_remote_openocd/resources/configuration.schema.json" in example_check
    assert "resources/config.yaml.example" in example_check
    assert ("--force-filetype", "yaml") in tuple(
        zip(example_check, example_check[1:], strict=False)
    )

    pylint = next(command for command in commands if tool(command) == "pylint")
    single_job_options = (("-j", "1"), ("--jobs", "1"))
    assert (
        any(option in tuple(zip(pylint, pylint[1:], strict=False)) for option in single_job_options)
        or "--jobs=1" in pylint
    )


def test_source_files_are_selected_from_git(monkeypatch):
    class Result:
        stdout = "one.py\ntwo.py\n"

    monkeypatch.setattr(static_check.subprocess, "run", lambda *args, **kwargs: Result())
    assert static_check.source_files(ROOT) == ("one.py", "two.py")


def test_yaml_files_include_configuration_examples(monkeypatch):
    class Result:
        stdout = "workflow.yml\nconfig.yaml.example\n"

    monkeypatch.setattr(static_check.subprocess, "run", lambda *args, **kwargs: Result())
    assert static_check.yaml_files(ROOT) == ("workflow.yml", "config.yaml.example")


def test_markdown_files_are_selected_from_git(monkeypatch):
    class Result:
        stdout = "README.md\ndocs/guide.md\n"

    monkeypatch.setattr(static_check.subprocess, "run", lambda *args, **kwargs: Result())
    assert static_check.markdown_files(ROOT) == ("README.md", "docs/guide.md")


def test_repository_root_is_reported_by_git(monkeypatch, tmp_path):
    class Result:
        stdout = f"{tmp_path}\n"

    monkeypatch.setattr(static_check.subprocess, "run", lambda *args, **kwargs: Result())
    assert static_check.repository_root() == tmp_path.resolve()
