# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import pytest
from zephyr_remote_openocd.controller._ssh import SshCommand
from zephyr_remote_openocd.controller.forwarding_spec import ForwardSpec
from zephyr_remote_openocd.controller.plan import SessionPlan
from zephyr_remote_openocd.protocol.arguments import (
    ArgumentTemplate,
    SessionValue,
    TclWord,
    log_spool_template,
)
from zephyr_remote_openocd.protocol.listeners import ListenerSpec
from zephyr_remote_openocd.protocol.process import ProcessSpec, RequiredPathSpec

TEST_PROCESS = ProcessSpec(("test-process",))


class TestSessionSpecifications:
    def test_reserved_listener_has_no_local_forward_requirement(self) -> None:
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
    def test_reserved_listener_cannot_conflict_with_forwarded_listener(
        self, reserved: ListenerSpec
    ) -> None:
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
    def test_request_auxiliary_services_require_initial_membership(
        self, auxiliary: tuple[ForwardSpec, ...]
    ) -> None:
        with pytest.raises(ValueError):
            SessionPlan(
                "host",
                SshCommand(),
                TEST_PROCESS,
                forwards=(ForwardSpec("gdb", 3333, 3333), ForwardSpec("tcl", 6333, 6333)),
                auxiliary_forwards=auxiliary,
            )

    def test_request_defaults_to_required_services(self) -> None:
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
    def test_remote_process_rejects_scalar_readiness_evidence_container(
        self, sentinels: str | bytes
    ) -> None:
        with pytest.raises(ValueError):
            # A deliberately ill-typed value reaches the runtime validation.
            ProcessSpec(("openocd",), readiness_evidence=sentinels)  # type: ignore[arg-type]

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
            pytest.param({"readiness_evidence": ("",)}, id="empty-evidence"),
            pytest.param({"readiness_evidence": ("start up",)}, id="space-in-evidence"),
            pytest.param({"readiness_evidence": ("init", "init")}, id="duplicate-evidence"),
            pytest.param({"readiness_timeout": True}, id="boolean-timeout"),
            pytest.param({"readiness_timeout": float("inf")}, id="infinite-timeout"),
            pytest.param({"readiness_timeout": 10**400}, id="unrepresentable-timeout"),
            pytest.param({"readiness_timeout": 0}, id="zero-timeout"),
            pytest.param({"literal_prefix": True}, id="boolean-prefix"),
            pytest.param({"literal_prefix": -1}, id="negative-prefix"),
            pytest.param({"literal_prefix": 2}, id="prefix-outside-argv"),
        ),
    )
    def test_remote_process_rejects_invalid_domain_values(self, changes: dict[str, object]) -> None:
        fields = {"argv": ("openocd",), **changes}
        with pytest.raises(ValueError):
            # Some values are deliberately ill-typed for runtime validation.
            ProcessSpec(**fields)  # type: ignore[arg-type]

    @pytest.mark.parametrize(
        "template",
        (
            pytest.param(ArgumentTemplate((SessionValue.LOG_SPOOL,)), id="plain-part"),
            pytest.param(
                ArgumentTemplate((TclWord((SessionValue.LOG_SPOOL, ".log")),)), id="word-suffix"
            ),
            pytest.param(
                ArgumentTemplate(("-l ", TclWord((SessionValue.LOG_SPOOL,)))), id="template-prefix"
            ),
        ),
    )
    def test_remote_process_rejects_log_spool_outside_its_one_form(
        self, template: ArgumentTemplate
    ) -> None:
        with pytest.raises(ValueError):
            ProcessSpec(
                ("openocd", template.preview()), literal_prefix=1, argv_templates=((1, template),)
            )

    def test_remote_process_rejects_a_second_log_spool_argument(self) -> None:
        template = log_spool_template()
        with pytest.raises(ValueError):
            ProcessSpec(
                ("openocd", "first", "second"),
                literal_prefix=1,
                argv_templates=((1, template), (2, template)),
            )

    @pytest.mark.parametrize(
        ("path", "kind"),
        (
            pytest.param("", "file", id="empty-path"),
            pytest.param("bad\0path", "file", id="nul-path"),
            pytest.param("path", "socket", id="invalid-kind"),
        ),
    )
    def test_remote_path_check_rejects_invalid_values(self, path: str, kind: str) -> None:
        with pytest.raises(ValueError):
            # An unsupported kind is deliberately outside the declared Literal.
            RequiredPathSpec(path, kind)  # type: ignore[arg-type]

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
    def test_service_rejects_invalid_values(self, arguments: tuple[str | int, int, int]) -> None:
        with pytest.raises(ValueError):
            # The integer name is deliberately ill-typed for runtime validation.
            ForwardSpec(*arguments)  # type: ignore[arg-type]
