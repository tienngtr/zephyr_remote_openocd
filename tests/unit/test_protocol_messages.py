# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from collections.abc import Callable

import pytest
from zephyr_remote_openocd.controller.forwarding_spec import ForwardSpec
from zephyr_remote_openocd.protocol.arguments import (
    ArgumentTemplate,
    SessionValue,
    materialize_path,
)
from zephyr_remote_openocd.protocol.commands import decode_command, encode_start
from zephyr_remote_openocd.protocol.outcome import CompletionPolicy
from zephyr_remote_openocd.protocol.process import ProcessSpec, RequiredPathSpec
from zephyr_remote_openocd.protocol.responses import (
    validate_deployment_response,
    validate_error_response,
    validate_openocd_version_response,
    validate_staged_response,
)
from zephyr_remote_openocd.protocol.wire import (
    ControlFrameBuffer,
    ProtocolError,
    decode_frame,
    encode_frame,
    validate_event,
)


class TestProtocol:
    @pytest.mark.parametrize("frame_size", (1_048_576, 1_048_577))
    def test_start_control_frame_size_includes_lf(self, frame_size: int) -> None:
        template = encode_start(ProcessSpec(("child", "")), ())
        argument = "x" * (frame_size - len(template))
        process = ProcessSpec(("child", argument))

        if frame_size == 1_048_576:
            frame = encode_start(process, ())
            assert len(frame) == frame_size
            assert frame.endswith(b"\n")
            # The helper's incremental framing admits the same boundary frame.
            frames = ControlFrameBuffer()
            frames.feed(frame)
            assert frames.pop_frame() == frame
            assert decode_command(frame).argv == ("child", argument)
        else:
            with pytest.raises(ProtocolError):
                encode_start(process, ())

    @pytest.mark.parametrize(
        "argv", (None, [], [""], "openocd", ["openocd", None], [1], ["open\0ocd"])
    )
    def test_attempt_rejects_invalid_argv(self, argv: object) -> None:
        with pytest.raises(ProtocolError):
            validate_event(decode_frame(encode_frame("ATTEMPT", argv=argv)))

    @pytest.mark.parametrize(
        "policy, preferred_address",
        ((CompletionPolicy.LIVE_SERVER, None), (CompletionPolicy.PROCESS_EXIT, "127.64.0.7")),
    )
    def test_start_serializers_use_validated_domain_models(
        self, policy: CompletionPolicy, preferred_address: str | None
    ) -> None:
        path_template = ArgumentTemplate((SessionValue.WORKSPACE, "/staged"))
        process = ProcessSpec(
            ("openocd", "--fixed", ""),
            environment=(("ZRO_TEST", "value"),),
            required_paths=(
                RequiredPathSpec("/firmware.elf", "file"),
                RequiredPathSpec(path_template.preview(), "directory", template=path_template),
            ),
            required_output_sentinels=("INIT COMPLETE", "READY FOR START"),
            readiness_timeout=0.5,
            literal_prefix=2,
            completion_policy=policy,
        )
        forwards = (ForwardSpec("gdb", 12345, 1), ForwardSpec("rtt", 12346, 65535))
        frame = encode_start(
            process,
            (forward.listener for forward in forwards),
            preferred_address=preferred_address,
        )
        request = decode_command(frame)
        assert request.argv == process.argv
        assert request.environment == process.environment
        assert [
            (
                materialize_path(check.path, workspace="/allocated", address="127.64.0.7"),
                check.kind,
            )
            for check in request.required_paths
        ] == [("/firmware.elf", "file"), ("/allocated/staged", "directory")]
        assert [(listener.name, listener.remote_port) for listener in request.listeners] == [
            (forward.name, forward.remote_port) for forward in forwards
        ]
        assert request.literal_prefix == process.literal_prefix
        assert request.completion_policy == process.completion_policy
        assert request.required_output_sentinels == process.required_output_sentinels
        assert request.readiness_timeout == process.readiness_timeout
        assert request.preferred_address == preferred_address

    @pytest.mark.parametrize(
        "address", ("127.0.0.1", "127.64.0.0", "127.127.255.255", "::1", "bad")
    )
    def test_start_rejects_invalid_preference_before_encoding(self, address: str) -> None:
        with pytest.raises(ValueError):
            encode_start(ProcessSpec(("child",)), (), preferred_address=address)

    def test_helper_event_unknown_fields_are_rejected(self) -> None:
        with pytest.raises(ProtocolError):
            validate_event(
                decode_frame(
                    encode_frame(
                        "SESSION_CREATED",
                        helper="helper",
                        session_id="id",
                        remote_workspace="/work",
                        future=True,
                    )
                )
            )

    @pytest.mark.parametrize(
        ("validator", "valid", "mistyped_field"),
        (
            pytest.param(
                validate_error_response,
                {"version": 1, "type": "ERROR", "code": "FAILED", "message": ""},
                {"code": ""},
                id="standalone-error",
            ),
            pytest.param(
                validate_staged_response,
                {
                    "version": 1,
                    "type": "STAGED",
                    "byte_count": 0,
                    "sha256": "0" * 64,
                    "files": [],
                    "directories": [],
                },
                {"byte_count": "0"},
                id="staged",
            ),
            pytest.param(
                validate_openocd_version_response,
                {"version": 1, "type": "OPENOCD_VERSION", "output": "OpenOCD 0.12.0"},
                {"output": None},
                id="openocd-version",
            ),
            pytest.param(
                validate_deployment_response,
                {
                    "version": 1,
                    "type": "DEPLOYED",
                    "status": "deployed",
                    "path": "/tmp/helper.py",
                    "sha256": "0" * 64,
                },
                {"sha256": 0},
                id="deployment",
            ),
        ),
    )
    def test_one_shot_response_validators_require_exact_typed_fields(
        self,
        validator: Callable[[dict[str, object]], None],
        valid: dict[str, object],
        mistyped_field: dict[str, object],
    ) -> None:
        validator(valid)

        missing_type = dict(valid)
        missing_type.pop("type")
        malformed = (
            missing_type,
            {**valid, "unexpected": True},
            {**valid, **mistyped_field},
        )
        for response in malformed:
            with pytest.raises(ProtocolError):
                validator(response)

    @pytest.mark.parametrize(
        "manifest",
        (
            pytest.param({"directories": ["."]}, id="dot"),
            pytest.param({"files": ["./f"]}, id="dot-component"),
            pytest.param({"files": ["d/../f"]}, id="dotdot-component"),
            pytest.param({"files": ["d//f"]}, id="empty-component"),
            pytest.param({"files": ["/f"]}, id="absolute"),
            pytest.param({"directories": ["d/"]}, id="trailing-slash"),
            pytest.param({"files": [""]}, id="empty"),
            pytest.param({"files": ["f\0"]}, id="nul"),
            pytest.param({"files": [1]}, id="non-string"),
            pytest.param({"files": ["f", "f"]}, id="duplicate-file"),
            pytest.param({"files": ["d"], "directories": ["d"]}, id="file-and-directory"),
            pytest.param({"files": ["d"], "directories": ["d/e"]}, id="file-ancestor"),
        ),
    )
    def test_staged_response_requires_normalized_disjoint_manifest(
        self, manifest: dict[str, object]
    ) -> None:
        valid = {
            "version": 1,
            "type": "STAGED",
            "byte_count": 0,
            "sha256": "0" * 64,
            "files": ["d/f"],
            "directories": ["d"],
        }
        validate_staged_response(valid)
        with pytest.raises(ProtocolError):
            validate_staged_response({**valid, **manifest})

    @pytest.mark.parametrize("payload", ("line\nbreak", "line\n"))
    def test_child_output_payload_rejects_embedded_line_delimiters(self, payload: str) -> None:
        with pytest.raises(ProtocolError):
            validate_event(
                decode_frame(
                    encode_frame(
                        "CHILD_OUTPUT",
                        stream="stdout",
                        payload=payload,
                        line_end=False,
                    )
                )
            )

    def test_child_output_requires_boundary_metadata(self) -> None:
        valid = encode_frame(
            "CHILD_OUTPUT",
            stream="stdout",
            payload="fragment",
            line_end=False,
        )
        validate_event(decode_frame(valid))
        for fields in (
            {},
            {"line_end": 1},
            {"line_end": False, "unexpected": True},
        ):
            with pytest.raises(ProtocolError):
                validate_event(
                    decode_frame(
                        encode_frame(
                            "CHILD_OUTPUT",
                            stream="stdout",
                            payload="fragment",
                            **fields,
                        )
                    )
                )

    def test_child_output_stream_is_stdout_or_stderr(self) -> None:
        with pytest.raises(ProtocolError):
            validate_event(
                decode_frame(
                    encode_frame("CHILD_OUTPUT", stream="console", payload="x", line_end=False)
                )
            )

    def test_child_output_boundary_states_are_unambiguous(self) -> None:
        validate_event(
            decode_frame(encode_frame("CHILD_OUTPUT", stream="stdout", payload="", line_end=True))
        )
        with pytest.raises(ProtocolError):
            validate_event(
                decode_frame(
                    encode_frame("CHILD_OUTPUT", stream="stdout", payload="", line_end=False)
                )
            )
