# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import pytest
from zephyr_remote_openocd.controller.forwarding_spec import ForwardSpec
from zephyr_remote_openocd.controller.plan import SessionPlan
from zephyr_remote_openocd.controller.ssh import SshCommand
from zephyr_remote_openocd.protocol.listeners import ListenerSpec
from zephyr_remote_openocd.protocol.process import ProcessSpec, RequiredPathSpec

TEST_PROCESS = ProcessSpec(("test-process",))


class TestSessionSpecifications:
    def test_reserved_listener_has_no_local_forward_requirement(self):
        forward = ForwardSpec("gdb", 12345, 3333)
        reserved = ListenerSpec("rtt", 12345)
        plan = SessionPlan(
            "host",
            SshCommand(),
            TEST_PROCESS,
            forwards=(forward,),
            reserved_listeners=(reserved,),
        )
        assert plan.forwards == (forward,)
        assert plan.listeners == (forward.listener, reserved)

    @pytest.mark.parametrize("reserved", (ListenerSpec("gdb", 5555), ListenerSpec("rtt", 3333)))
    def test_reserved_listener_cannot_conflict_with_forwarded_listener(self, reserved):
        with pytest.raises(ValueError):
            SessionPlan(
                "host",
                SshCommand(),
                TEST_PROCESS,
                forwards=(ForwardSpec("gdb", 12345, 3333),),
                reserved_listeners=(reserved,),
            )

    @pytest.mark.parametrize(
        "auxiliary",
        (
            (ForwardSpec("unknown", 5555, 5555),),
            (ForwardSpec("tcl", 6334, 6333),),
            (ForwardSpec("tcl", 6333, 6333), ForwardSpec("tcl", 6333, 6333)),
        ),
        ids=("unknown", "different-port", "duplicate"),
    )
    def test_request_auxiliary_services_require_initial_membership(self, auxiliary):
        with pytest.raises(ValueError):
            SessionPlan(
                "host",
                SshCommand(),
                TEST_PROCESS,
                forwards=(ForwardSpec("gdb", 3333, 3333), ForwardSpec("tcl", 6333, 6333)),
                auxiliary_forwards=auxiliary,
            )

    def test_request_defaults_to_required_services(self):
        forward = ForwardSpec("tcl", 6333, 6333)
        assert (
            SessionPlan("host", SshCommand(), TEST_PROCESS, forwards=(forward,)).auxiliary_forwards
            == ()
        )

    @pytest.mark.parametrize(
        "sentinels",
        ("READY", b"READY"),
        ids=("string", "bytes"),
    )
    def test_remote_process_rejects_scalar_output_sentinel_container(self, sentinels):
        with pytest.raises(ValueError):
            ProcessSpec(("openocd",), required_output_sentinels=sentinels)

    @pytest.mark.parametrize(
        "changes",
        (
            pytest.param({"argv": ()}, id="empty-argv"),
            pytest.param({"argv": ("",)}, id="empty-executable"),
            pytest.param({"argv": ("openocd", 1)}, id="non-string-argument"),
            pytest.param({"environment": (("NAME", "1"), ("NAME", "2"))}, id="duplicate-env"),
            pytest.param({"environment": (("BAD=NAME", "value"),)}, id="invalid-env-name"),
            pytest.param({"environment": (("NAME", "bad\0value"),)}, id="nul-env-value"),
            pytest.param({"required_paths": (object(),)}, id="invalid-path-check"),
            pytest.param({"required_output_sentinels": ("",)}, id="empty-marker"),
            pytest.param({"required_output_sentinels": (" READY ",)}, id="untrimmed-marker"),
            pytest.param({"required_output_sentinels": ("READY", "READY")}, id="duplicate-marker"),
            pytest.param({"readiness_timeout": True}, id="boolean-timeout"),
            pytest.param({"readiness_timeout": float("inf")}, id="infinite-timeout"),
            pytest.param({"readiness_timeout": 0}, id="zero-timeout"),
            pytest.param({"literal_prefix": True}, id="boolean-prefix"),
            pytest.param({"literal_prefix": -1}, id="negative-prefix"),
            pytest.param({"literal_prefix": 2}, id="prefix-outside-argv"),
        ),
    )
    def test_remote_process_rejects_invalid_domain_values(self, changes):
        fields = {"argv": ("openocd",), **changes}
        with pytest.raises(ValueError):
            ProcessSpec(**fields)

    @pytest.mark.parametrize(
        ("path", "kind"),
        (
            pytest.param("", "file", id="empty-path"),
            pytest.param("bad\0path", "file", id="nul-path"),
            pytest.param("path", "socket", id="invalid-kind"),
        ),
    )
    def test_remote_path_check_rejects_invalid_values(self, path, kind):
        with pytest.raises(ValueError):
            RequiredPathSpec(path, kind)

    @pytest.mark.parametrize(
        "arguments",
        (
            pytest.param(("", 1234, 3333), id="empty-name"),
            pytest.param((1, 1234, 3333), id="integer-name"),
            pytest.param(("gdb", True, 3333), id="boolean-port"),
            pytest.param(("gdb", 0, 3333), id="zero-port"),
            pytest.param(("gdb", 1234, 65536), id="out-of-range-port"),
        ),
    )
    def test_service_rejects_invalid_values(self, arguments):
        with pytest.raises(ValueError):
            ForwardSpec(*arguments)
