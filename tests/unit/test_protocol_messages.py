# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import io

import pytest
from zephyr_remote_openocd.protocol import commands as protocol_commands
from zephyr_remote_openocd.protocol.commands import write_start
from zephyr_remote_openocd.protocol.outcome import CompletionPolicy
from zephyr_remote_openocd.protocol.process import ProcessSpec, RequiredPathSpec
from zephyr_remote_openocd.protocol.responses import (
    validate_deployment_response,
    validate_openocd_version_response,
    validate_staged_response,
)
from zephyr_remote_openocd.protocol.wire import (
    ProtocolError,
    decode_frame,
    encode_frame,
    read_message,
    validate_event,
)
from zephyr_remote_openocd.remote.model import Service


class TestProtocol:
    @pytest.mark.parametrize("frame_size", (1_048_576, 1_048_577))
    def test_start_control_frame_size_includes_lf(self, frame_size):
        template = io.BytesIO()
        write_start(template, ProcessSpec(("child", "")), ())
        argument = "x" * (frame_size - len(template.getvalue()))
        process = ProcessSpec(("child", argument))
        stream = io.BytesIO()

        if frame_size == 1_048_576:
            write_start(stream, process, ())
            assert len(stream.getvalue()) == frame_size
            assert stream.getvalue().endswith(b"\n")
            assert decode_frame(stream.getvalue())["argv"] == ["child", argument]
        else:
            with pytest.raises(ProtocolError):
                write_start(stream, process, ())
            assert stream.getvalue() == b""

    def test_read_message_consumes_one_frame_at_a_time_and_reports_eof(self):
        stream = io.BytesIO(encode_frame("HELLO", value=3) + encode_frame("HELLO", value=4))
        assert read_message(stream)["value"] == 3
        assert read_message(stream)["value"] == 4
        with pytest.raises(EOFError):
            read_message(stream)

    def test_read_message_rejects_frame_without_lf(self):
        frame = encode_frame("HELLO", value=3).rstrip(b"\n")

        with pytest.raises(ProtocolError):
            read_message(io.BytesIO(frame))

    @pytest.mark.parametrize("argv", (None, [], [""], "openocd", ["openocd", None], [1]))
    def test_attempt_rejects_invalid_argv(self, argv):
        with pytest.raises(ProtocolError):
            validate_event(decode_frame(encode_frame("ATTEMPT", argv=argv)))

    def test_start_serializers_use_validated_domain_models(self):
        from zephyr_remote_openocd.protocol.commands import decode_command

        stream = io.BytesIO()
        process = ProcessSpec(
            ("openocd", "--fixed", ""),
            environment=(("ZRO_TEST", "value"),),
            required_paths=(RequiredPathSpec("/firmware.elf", "file"),),
            required_output_sentinels=("INIT COMPLETE", "READY FOR START"),
            readiness_timeout=47.5,
            literal_prefix=2,
            completion_policy=CompletionPolicy.PROCESS_EXIT,
        )
        forwards = (Service("gdb", 12345, 3333), Service("rtt", 12346, 5566))
        write_start(
            stream,
            process,
            (forward.listener for forward in forwards),
            preferred_address="127.64.0.7",
        )
        request = decode_command(decode_frame(stream.getvalue()))
        assert request.argv == process.argv
        assert request.environment == process.environment
        assert [(check.path, check.kind) for check in request.required_paths] == [
            (check.path, check.kind) for check in process.required_paths
        ]
        assert [(listener.name, listener.remote_port) for listener in request.listeners] == [
            (forward.name, forward.remote_port) for forward in forwards
        ]
        assert request.literal_prefix == process.literal_prefix
        assert request.completion_policy == process.completion_policy
        assert request.required_output_sentinels == process.required_output_sentinels
        assert request.readiness_timeout == process.readiness_timeout
        assert request.preferred_address == "127.64.0.7"

    def test_start_preferred_address_reaches_helper_validation(self):
        stream = io.BytesIO()
        write_start(stream, ProcessSpec(("child",)), (), preferred_address="127.64.0.7")
        request = protocol_commands.decode_command(decode_frame(stream.getvalue()))
        assert request.preferred_address == "127.64.0.7"

    @pytest.mark.parametrize(
        "address", ("127.0.0.1", "127.64.0.0", "127.127.255.255", "::1", "bad")
    )
    def test_start_rejects_invalid_preference_before_writing(self, address: str):
        stream = io.BytesIO()
        with pytest.raises(ValueError):
            write_start(stream, ProcessSpec(("child",)), (), preferred_address=address)
        assert not stream.getvalue()

    def test_helper_event_unknown_fields_are_rejected(self):
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
        self, validator, valid, mistyped_field
    ):
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

    @pytest.mark.parametrize("payload", ("line\nbreak", "line\n"))
    def test_child_output_payload_rejects_embedded_line_delimiters(self, payload):
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

    def test_child_output_requires_boundary_metadata(self):
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

    def test_child_output_boundary_states_are_unambiguous(self):
        with pytest.raises(ProtocolError):
            validate_event(
                decode_frame(
                    encode_frame("CHILD_OUTPUT", stream="stdout", payload="", line_end=False)
                )
            )
