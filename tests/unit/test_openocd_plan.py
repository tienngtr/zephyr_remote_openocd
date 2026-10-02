# SPDX-License-Identifier: Apache-2.0

"""OpenOCD config lookup through real staged and mapped search trees."""

from pathlib import Path, PurePosixPath

import pytest
from zephyr_remote_openocd.config import PathMapping
from zephyr_remote_openocd.remote.model import RemotePathCheck, StagedDirectory, StagedFile
from zephyr_remote_openocd.remote.openocd_plan import plan_support_paths
from zephyr_remote_openocd.remote.paths import PathPlanner, PathPlanningError


@pytest.mark.parametrize("mapped", (False, True), ids=("staged", "mapped"))
@pytest.mark.parametrize("nested_first", (False, True), ids=("parent-first", "nested-first"))
def test_relative_config_uses_first_search_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mapped: bool, nested_first: bool
) -> None:
    parent = tmp_path / "scripts"
    nested = parent / "nested"
    for root in (parent, nested):
        (root / "interface").mkdir(parents=True)
        (root / "interface" / "example.cfg").write_text(f"# {root.name}\n")
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    roots = (nested, parent) if nested_first else (parent, nested)
    mappings = (PathMapping(parent, PurePosixPath("/remote/scripts")),) if mapped else ()
    planner = PathPlanner(mappings)

    search, configs = plan_support_paths(
        tuple(str(root) for root in roots), ("interface/example.cfg",), planner
    )

    if mapped:
        remote_parent = "/remote/scripts"
    else:
        staged_parent = next(
            item
            for item in planner.staged_files
            if isinstance(item, StagedDirectory) and item.source == parent
        )
        remote_parent = f"{{workspace}}/staged/{staged_parent.destination}"
    expected_search = [remote_parent, remote_parent + "/nested"]
    if nested_first:
        expected_search.reverse()
    assert search == expected_search
    assert configs == [expected_search[0] + "/interface/example.cfg"]
    if mapped:
        assert RemotePathCheck(configs[0], "file") in planner.remote_checks
        assert not planner.staged_files
    else:
        staged = [item for item in planner.staged_files if isinstance(item, StagedFile)]
        assert {item.source for item in staged} == {
            parent / "interface" / "example.cfg",
            nested / "interface" / "example.cfg",
        }
        assert len(staged) == 2


def test_relative_config_skips_search_roots_without_a_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    roots = tuple(tmp_path / name for name in ("empty", "directory", "file"))
    roots[0].mkdir()
    (roots[1] / "interface" / "example.cfg").mkdir(parents=True)
    (roots[2] / "interface").mkdir(parents=True)
    config = roots[2] / "interface" / "example.cfg"
    config.write_text("# config\n")
    monkeypatch.chdir(tmp_path)
    planner = PathPlanner(())

    search, configs = plan_support_paths(
        tuple(str(root) for root in roots), ("interface/example.cfg",), planner
    )

    assert len(search) == 3
    assert configs == [search[2] + "/interface/example.cfg"]
    assert len([item for item in planner.staged_files if item.source == config]) == 1


@pytest.mark.parametrize("absolute", (False, True), ids=("cwd-relative", "absolute"))
@pytest.mark.parametrize("mapped", (False, True), ids=("staged", "mapped"))
def test_direct_config_takes_precedence_over_search_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, absolute: bool, mapped: bool
) -> None:
    search_root = tmp_path / "scripts"
    search_root.mkdir()
    (search_root / "example.cfg").write_text("# search copy\n")
    config = tmp_path / "example.cfg"
    config.write_text("# direct copy\n")
    monkeypatch.chdir(tmp_path)
    mappings = (PathMapping(tmp_path, PurePosixPath("/remote")),) if mapped else ()
    planner = PathPlanner(mappings)

    _, configs = plan_support_paths(
        (str(search_root),), (str(config) if absolute else "example.cfg",), planner
    )

    if mapped:
        assert configs == ["/remote/example.cfg"]
        assert RemotePathCheck("/remote/example.cfg", "file") in planner.remote_checks
    else:
        staged_configs = [
            item
            for item in planner.staged_files
            if isinstance(item, StagedFile) and item.source == config
        ]
        assert len(staged_configs) == 1
        assert configs == [f"{{workspace}}/staged/{staged_configs[0].destination}"]


@pytest.mark.parametrize("absolute", (False, True), ids=("relative", "absolute"))
def test_unresolvable_config_fails_during_planning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, absolute: bool
) -> None:
    search_root = tmp_path / "scripts"
    search_root.mkdir()
    (search_root / "example.cfg").write_text("# search copy\n")
    monkeypatch.chdir(tmp_path)
    config = str(tmp_path / "missing" / "example.cfg") if absolute else "missing.cfg"

    with pytest.raises(PathPlanningError):
        plan_support_paths((str(search_root),), (config,), PathPlanner(()))
