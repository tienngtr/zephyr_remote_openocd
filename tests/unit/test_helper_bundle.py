# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import zipfile
from importlib.resources import files
from pathlib import Path

from zephyr_remote_openocd.controller._deployment import build_helper_bundle
from zephyr_remote_openocd.protocol.wire import VERSION


def test_helper_bundle_is_deterministic_and_runs_without_local_package(tmp_path):
    content = build_helper_bundle()
    assert content == build_helper_bundle()
    helper = tmp_path / "helper-digest.py"
    helper.write_bytes(content)
    result = subprocess.run(
        [sys.executable, "-I", str(helper), "openocd-version", "--", sys.executable],
        cwd=tmp_path,
        capture_output=True,
        timeout=30,
        check=True,
    )
    response = json.loads(result.stdout)
    assert response["version"] == VERSION == 1
    assert response["type"] == "OPENOCD_VERSION"
    assert "Python" in response["output"]

    control = subprocess.run(
        [sys.executable, "-I", str(helper), "control"],
        cwd=tmp_path,
        env={**os.environ, "XDG_RUNTIME_DIR": str(tmp_path)},
        input=b"",
        capture_output=True,
        timeout=30,
        check=True,
    )
    created, ended = [json.loads(line) for line in control.stdout.splitlines()]
    assert created["type"] == "SESSION_CREATED"
    assert ended["type"] == "SESSION_ENDED"
    assert ended["cleanup"]["workspace_disposal"] == "confirmed"
    assert not Path(created["remote_workspace"]).exists()


def test_helper_bundle_covers_canonical_sources_without_controller_dependencies():
    package = files("zephyr_remote_openocd")
    expected = {
        "__main__.py": package.joinpath("helper", "__main__.py"),
        "zephyr_remote_openocd/__init__.py": package.joinpath("__init__.py"),
    }
    for directory in ("helper", "protocol"):
        for module in package.joinpath(directory).iterdir():
            if module.name.endswith(".py"):
                expected[f"zephyr_remote_openocd/{directory}/{module.name}"] = module
    with zipfile.ZipFile(io.BytesIO(build_helper_bundle())) as bundle:
        assert set(bundle.namelist()) == set(expected)
        assert all(bundle.read(name) == source.read_bytes() for name, source in expected.items())
