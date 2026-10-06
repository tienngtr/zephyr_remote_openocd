# SPDX-License-Identifier: Apache-2.0

"""OpenOCD config lookup through real staged and mapped search trees."""

import io
from pathlib import Path, PurePosixPath

import pytest
from zephyr_remote_openocd.config import PathMapping
from zephyr_remote_openocd.remote.model import (
    RemotePathCheck,
    RemoteProcess,
    StagedDirectory,
    StagedFile,
)
from zephyr_remote_openocd.remote.openocd_plan import plan_openocd_base, plan_support_paths
from zephyr_remote_openocd.remote.paths import PathPlanner, PathPlanningError
from zephyr_remote_openocd.remote.protocol import decode_message, write_start
from zephyr_remote_openocd.remote_helper import _decode_start, materialize_argv, materialize_path


@pytest.mark.parametrize("mapped", (False, True), ids=("staged", "mapped"))
def test_search_tree_token_text_remains_literal_through_helper_materialization(
    tmp_path: Path, mapped: bool
) -> None:
    root = tmp_path / "scripts"
    root.mkdir()
    config_name = "{workspace} {address}.cfg"
    (root / config_name).write_text("# fixture\n")
    remote_root = "/remote/{workspace}/{address}"
    mappings = (PathMapping(root, PurePosixPath(remote_root)),) if mapped else ()
    planner = PathPlanner(mappings)
    base = plan_openocd_base("openocd", None, (str(root),), (str(root / config_name),), planner, ())
    stream = io.BytesIO()
    write_start(
        stream,
        RemoteProcess(
            base.argv,
            required_paths=tuple(planner.remote_checks),
            literal_prefix=base.literal_prefix,
            argv_templates=base.argv_templates,
        ),
        (),
    )
    request = _decode_start(decode_message(stream.getvalue()))
    workspace = "/runtime/{address}/{workspace}/session"
    argv = materialize_argv(
        request.argv,
        workspace=workspace,
        address="127.64.0.1",
        literal_prefix=request.literal_prefix,
        argv_templates=request.argv_templates,
    )
    if not mapped:
        tree = next(item for item in planner.staged_files if isinstance(item, StagedDirectory))
        remote_root = f"{workspace}/staged/{tree.destination}"

    assert argv[argv.index("-s") + 1] == remote_root
    assert argv[argv.index("-f") + 1] == remote_root + "/" + config_name
    if mapped:
        assert tuple(
            materialize_path(check.path, workspace=workspace, address="127.64.0.1")
            for check in request.required_paths
        ) == (remote_root, remote_root + "/" + config_name)


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
    assert [path.remote for path in search] == expected_search
    assert [path.remote for path in configs] == [expected_search[0] + "/interface/example.cfg"]
    if mapped:
        assert RemotePathCheck(configs[0].remote, "file") in planner.remote_checks
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
    assert [path.remote for path in configs] == [search[2].remote + "/interface/example.cfg"]
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
        assert [path.remote for path in configs] == ["/remote/example.cfg"]
        assert RemotePathCheck("/remote/example.cfg", "file") in planner.remote_checks
    else:
        staged_configs = [
            item
            for item in planner.staged_files
            if isinstance(item, StagedFile) and item.source == config
        ]
        assert len(staged_configs) == 1
        assert [path.remote for path in configs] == [
            f"{{workspace}}/staged/{staged_configs[0].destination}"
        ]


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
