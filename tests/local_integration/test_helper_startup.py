# SPDX-License-Identifier: Apache-2.0

"""Regression coverage for remote workspace adoption and pre-spawn publication."""

from __future__ import annotations

import io
import json
import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest
from zephyr_remote_openocd.remote.arguments import ArgumentTemplate
from zephyr_remote_openocd.remote.model import RemoteProcess
from zephyr_remote_openocd.remote.protocol import EventOrder, decode_single_frame, write_start

from tests.process_support import managed_process, read_line
from tests.support import ROOT


@pytest.mark.parametrize(
    ("boundary", "action"),
    [
        (boundary, action)
        for boundary in ("allocated", "constructor", "loop")
        for action in (signal.SIGINT, signal.SIGTERM, "exception")
        if (boundary, action) != ("allocated", "exception")
    ],
)
def test_helper_startup_retains_workspace_cleanup_ownership(tmp_path, boundary, action):
    gate_read, gate_write = os.pipe()
    try:
        with managed_process(
            [
                sys.executable,
                str(ROOT / "tests/fixtures/helper_startup.py"),
                boundary,
                "exception" if action == "exception" else "signal",
                str(gate_read),
            ],
            env={**os.environ, "XDG_RUNTIME_DIR": str(tmp_path)},
            pass_fds=(gate_read,),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        ) as owner:
            process = owner.process
            assert process.stderr is not None
            checkpoint = json.loads(read_line(process.stderr))
            workspace = Path(checkpoint["workspace"])
            if action != "exception":
                assert workspace.is_dir()
                process.send_signal(action)
                # Release only after the actual installed handler observes
                # the signal, not after a scheduling delay.
                assert json.loads(read_line(process.stderr)) == {"signal": action}
                os.write(gate_write, b"x")
            _stdout, stderr = process.communicate(timeout=30)
            records = [json.loads(line) for line in stderr.splitlines() if line.startswith(b"{")]
            final = records[-1]
            assert process.returncode == 1
            assert final["lock_closed"] and final["handlers_restored"]
            assert not final["workspace_remaining"] and not final["lease_remaining"]
            assert not any(workspace.parent.iterdir())
            if action == "exception":
                assert records[0]["failure"] == "RuntimeError"
    finally:
        os.close(gate_read)
        os.close(gate_write)


def test_empty_materialized_executable_fails_without_invalid_start_event(tmp_path):
    request = RemoteProcess(
        (sys.executable,),
        literal_prefix=0,
        argv_templates=((0, ArgumentTemplate(("",))),),
    )
    wire = io.BytesIO()
    write_start(wire, request, ())
    with managed_process(
        [sys.executable, str(ROOT / "python/zephyr_remote_openocd/remote_helper.py"), "control"],
        env={**os.environ, "XDG_RUNTIME_DIR": str(tmp_path)},
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ) as owner:
        stdout, _stderr = owner.process.communicate(input=wire.getvalue(), timeout=30)
    events = [decode_single_frame(line) for line in stdout.splitlines(keepends=True)]
    order = EventOrder()
    for event in events:
        order.accept(event)
    assert [event["type"] for event in events] == ["SESSION_CREATED", "SESSION_ENDED"]
    assert not any(Path(events[0]["remote_workspace"]).parent.iterdir())
