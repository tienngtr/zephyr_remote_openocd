# SPDX-License-Identifier: Apache-2.0

"""Report each nonempty ``os.read`` as a READ event before returning it.

Tests prepend this file's text to the remote serial reader program. It observes
real descriptor reads without replacing the reader's accumulation or matching
logic. The acknowledgement precedes return from ``os.read``.
"""

import base64
import json
import os

_original_read = os.read


def _observe_read(descriptor: int, size: int) -> bytes:
    payload = _original_read(descriptor, size)
    if payload:
        event = {"type": "READ", "data": base64.b64encode(payload).decode()}
        print(json.dumps(event), flush=True)
    return payload


os.read = _observe_read
