# SPDX-License-Identifier: Apache-2.0

"""Small purpose-built hardware inventory documents for behavioral tests."""

from __future__ import annotations

from typing import Any


def inventory_document(*, zephyr_base: str = "/zephyr", west: str = "/west") -> dict[str, Any]:
    return {
        "build_environments": {
            "environment": {"zephyr_base": zephyr_base, "west": west},
        },
        "toolchains": {"toolchain": {"gdb": "/gdb"}},
        "hosts": {
            "host": {
                "ssh_host": "ssh-host",
                "ssh_command": ["ssh"],
                "openocd_command": ["/openocd"],
                "forward_env": ["CHANNEL"],
                "path_mappings": {"/local": "/remote"},
            }
        },
        "targets": {
            "target": {
                "host": "host",
                "build_environment": "environment",
                "toolchain": "toolchain",
                "board": "board",
                "builds": {
                    "application": {"application": "app"},
                    "precondition": {"application": "before"},
                },
                "serial": {
                    "console": {
                        "device": "/dev/console",
                        "baud": 115200,
                        "data_bits": 8,
                        "parity": "none",
                        "stop_bits": 1,
                        "flow_control": "none",
                    }
                },
                "profiles": {
                    "profile": {
                        "build": "application",
                        "probe_serial": "probe",
                        "runner_args": ["--cmd-pre-init=bindto"],
                        "environment": {"CHANNEL": "0"},
                        "operations": {
                            "flash": {
                                "precondition_build": "precondition",
                                "quiescence_timeout": 2,
                                "serial": {
                                    "endpoint": "console",
                                    "pattern": "ready",
                                    "timeout": 30,
                                },
                            },
                            "debug": {"breakpoint": "main"},
                            "attach": {"precondition_build": "precondition"},
                            "debugserver": {},
                        },
                    },
                    "rtt_profile": {
                        "build": "application",
                        "operations": {
                            "rtt": {
                                "port": 19021,
                                "input": "help\n",
                                "response": "commands",
                                "timeout": 30,
                                "program_survives_reset": True,
                                "breakpoint": "main",
                            }
                        },
                    },
                },
            }
        },
    }
