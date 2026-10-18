# SPDX-License-Identifier: Apache-2.0

"""Standard-library serial observation support for external tests."""

from __future__ import annotations

import base64
import json
import shlex
from pathlib import Path

from zephyr_remote_openocd.controller._ssh import ManagedSshProcess, stop_process

from tests.inventory import SerialEndpoint
from tests.process_support import read_line

# Sent as remote ``python3 -c`` source text; see that module for its events.
SERIAL_READER_SOURCE = (Path(__file__).parent / "fixtures/remote_serial_reader.py").read_text(
    encoding="utf-8"
)


def read_event(process: ManagedSshProcess, timeout: float) -> dict[str, object]:
    """Read one JSON event from the remote reader."""
    if process.stdout is None:
        raise AssertionError("remote serial reader has no stdout")
    line = read_line(process.stdout, timeout)
    if not line:
        diagnostic = process.stderr_tail()
        raise AssertionError("remote serial reader exited: " + diagnostic.decode(errors="replace"))
    return json.loads(line)


def stop_reader(process: ManagedSshProcess) -> None:
    """Terminate a reader and close all of its pipes."""
    stop_process(process)


def remote_serial_reader_command(
    device: str,
    baud: int,
    pattern: str,
    timeout: float,
    *,
    data_bits: int = 8,
    parity: str = "none",
    stop_bits: int = 1,
    flow_control: str = "none",
) -> str:
    """Return a shell-safe remote ``python3 -c`` serial-reader command."""
    encoded = base64.b64encode(SERIAL_READER_SOURCE.encode()).decode("ascii")
    args = (
        device,
        str(baud),
        str(data_bits),
        parity,
        str(stop_bits),
        flow_control,
        pattern,
        str(timeout),
    )
    return " ".join(
        (
            "python3",
            "-c",
            shlex.quote(f"import base64;exec(base64.b64decode('{encoded}'))"),
            *(shlex.quote(value) for value in args),
        )
    )


def endpoint_reader_command(endpoint: SerialEndpoint, pattern: str, timeout: float) -> str:
    """Return the remote reader command for an inventory serial endpoint."""
    return remote_serial_reader_command(
        endpoint.device,
        endpoint.baud,
        pattern,
        timeout,
        data_bits=endpoint.data_bits,
        parity=endpoint.parity,
        stop_bits=endpoint.stop_bits,
        flow_control=endpoint.flow_control,
    )


def captured_text(event: dict[str, object]) -> str:
    """Decode the serial data carried by a MATCH or TIMEOUT event."""
    encoded = event.get("data", "")
    assert isinstance(encoded, str), f"invalid serial reader event: {event}"
    return base64.b64decode(encoded).decode("utf-8", "replace")
