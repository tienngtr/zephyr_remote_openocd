# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
import subprocess
import sys

from zephyr_remote_openocd.remote.deploy import _helper_source
from zephyr_remote_openocd.remote.protocol import PROTOCOL_VERSION


def test_helper_bundle_is_deterministic_and_runs_without_local_package(tmp_path):
    content = _helper_source()
    assert content == _helper_source()
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
    assert response["version"] == PROTOCOL_VERSION
    assert response["type"] == "OPENOCD_VERSION"
    assert "Python" in response["output"]
