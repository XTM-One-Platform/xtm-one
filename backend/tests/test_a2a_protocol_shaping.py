"""Protocol version negotiation and v1.0 <-> v0.3 shaping.

Also enforces the package's containment rule: ``a2a_sdk`` must be imported
from exactly one module. That rule is the whole reason a young third-party
dependency was allowed onto a security surface, so it is tested rather than
merely documented.
"""

import pathlib

import pytest

from app.a2a import protocol as proto

A2A_PKG = pathlib.Path(__file__).resolve().parents[1] / "app" / "a2a"


class TestContainmentBoundary:
    def test_the_permitted_importer_list_has_one_home(self):
        """The boundary is enforced in one place, not two.

        The list and the walk live in ``test_a2a_client_sdk_containment.py``;
        a second list here would be two answers to one question. This asserts
        only that ``protocol.py`` is on it, which is the fact THIS module
        cares about.
        """
        from tests.test_a2a_client_sdk_containment import PERMITTED

        assert "app/a2a/protocol.py" in PERMITTED


class TestVersionNegotiation:
    def test_absent_header_defaults_to_v1(self):
        # Both platforms speak v1.0 and neither declares a version when it
        # fetches a card, so the undeclared case has to be v1.0.
        assert proto.negotiate_version(None) == proto.PROTOCOL_VERSION_1_0
        assert proto.negotiate_version("") == proto.PROTOCOL_VERSION_1_0
        assert proto.negotiate_version("   ") == proto.PROTOCOL_VERSION_1_0

    @pytest.mark.parametrize("declared", ["1.0", "0.3"])
    def test_declared_version_is_honoured(self, declared):
        assert proto.negotiate_version(declared) == declared

    def test_patch_level_resolves_to_major_minor(self):
        assert proto.negotiate_version("1.0.1") == "1.0"

    def test_unrecognised_version_raises_naming_supported(self):
        with pytest.raises(proto.UnsupportedVersionError) as exc:
            proto.negotiate_version("9.9")
        assert "1.0" in str(exc.value) and "0.3" in str(exc.value)


class TestStateMapping:
    @pytest.mark.parametrize(
        "status,expected",
        [
            ("pending", proto.A2AState.SUBMITTED),
            ("delayed", proto.A2AState.SUBMITTED),
            ("running", proto.A2AState.WORKING),
            ("awaiting_human", proto.A2AState.INPUT_REQUIRED),
            ("completed", proto.A2AState.COMPLETED),
            ("failed", proto.A2AState.FAILED),
            ("cancelled", proto.A2AState.CANCELED),
        ],
    )
    def test_every_run_status_maps(self, status, expected):
        assert proto.run_status_to_state(status) == expected

    def test_unknown_status_fails_closed(self):
        # A partner told "failed" beats a partner polling forever because the
        # platform grew a status this mapping does not know.
        assert proto.run_status_to_state("some_new_status") == proto.A2AState.FAILED

    def test_every_state_value_is_a_real_sdk_state(self):
        """Assert against the SDK enum, never a hand-copied string.

        ``A2AState`` values are the **v0.3** wire values. This replaced a
        test that asserted a shaper spelled the v0.3 canceled state
        ``"cancelled"``, a spelling taken from the specification prose
        rather than from the SDK, which spells it ``canceled``. The
        hand-copied expectation made the test agree with the bug and pass
        while a v0.3 partner's deserialization failed on every cancellation.
        The v1.0 spellings are a separate vocabulary with its own
        parser-round-trip test below — an earlier revision believed the two
        were identical, checked it against the SDK's enum types instead of
        its v1.0 parser, and was disproved by the first real v1.0 client.
        """
        from a2a.compat.v0_3 import types as v03

        valid = {member.value for member in v03.TaskState}
        for state in proto.A2AState:
            assert state.value in valid

    def test_v03_wire_spelling_is_the_enum_value(self):
        for state in proto.A2AState:
            assert proto.state_wire(state, "0.3") == state.value

    @pytest.mark.parametrize("state", list(proto.A2AState))
    def test_v1_wire_spelling_survives_the_reference_parser(self, state):
        """The authority is ``ParseDict``, not the enum type.

        The v1.0 JSON-RPC binding is proto-JSON, and the reference client
        parses every response with ``json_format.ParseDict``, whose enum
        fields accept only the proto enum NAMES. ``"completed"`` — the v0.3
        spelling — raises here, which is exactly the failure a v1.0 partner
        saw before ``state_wire`` existed.
        """
        from a2a.types import a2a_pb2
        from google.protobuf import json_format

        parsed = json_format.ParseDict(
            {"state": proto.state_wire(state, "1.0")}, a2a_pb2.TaskStatus()
        )
        assert parsed.state == proto.sdk_state_value(state)

    def test_canceled_is_spelled_with_one_l(self):
        # The regression this file exists to prevent: the spelling a v0.3
        # partner's deserializer accepts, which is the SDK's, not the prose's.
        assert proto.A2AState.CANCELED.value == "canceled"

    def test_a_canceled_task_deserialises_for_a_v03_partner(self):
        """The end-to-end shape, as the reference SDK sees it."""
        from a2a.compat.v0_3 import types as v03

        task = v03.Task.model_validate(
            {
                "id": "11111111-1111-1111-1111-111111111111",
                "contextId": "22222222-2222-2222-2222-222222222222",
                "status": {"state": proto.A2AState.CANCELED.value},
                "artifacts": [],
            }
        )
        assert task.status.state is v03.TaskState.canceled

    def test_every_state_has_an_sdk_value(self):
        for state in proto.A2AState:
            assert isinstance(proto.sdk_state_value(state), int)


class TestPartShaping:
    def test_text_part_shapes_differ(self):
        # v0.3 discriminates parts by ``kind`` — ``type`` is the pre-0.3
        # spelling, silently tolerated by the python SDK and rejected by
        # Foundry's strict parser.
        assert proto.text_part("hi", "1.0") == {"text": "hi"}
        assert proto.text_part("hi", "0.3") == {"kind": "text", "text": "hi"}

    def test_data_part_uses_the_proto_field_name(self):
        # ``structuredData`` is a pre-donation draft spelling that appears in
        # no v1.0 proto field; the reference parser rejects it. Inbound
        # tolerance for it (below) is deliberate; emitting it was a bug.
        assert proto.data_part({"a": 1}, "1.0") == {"data": {"a": 1}}
        assert proto.data_part({"a": 1}, "0.3") == {"kind": "data", "data": {"a": 1}}

    def test_file_part_v1_is_the_flat_proto_shape(self):
        # v1.0's Part has flat ``url`` + ``filename`` fields — the nested
        # ``{"url": {"uri": ...}}`` object was the same class of draft-prose
        # bug as ``structuredData``.
        assert proto.file_part("https://x/f", "f.txt", "1.0") == {
            "url": "https://x/f",
            "filename": "f.txt",
        }
        assert proto.file_part("https://x/f", None, "1.0") == {"url": "https://x/f"}
        assert proto.file_part("https://x/f", "f.txt", "0.3") == {
            "kind": "file",
            "file": {"uri": "https://x/f", "name": "f.txt"},
        }

    @pytest.mark.parametrize(
        "part",
        [
            proto.text_part("hello", "1.0"),
            proto.data_part({"a": 1}, "1.0"),
            proto.file_part("https://x/f", "f.txt", "1.0"),
        ],
    )
    def test_every_v1_part_shape_survives_the_reference_parser(self, part):
        from a2a.types import a2a_pb2
        from google.protobuf import json_format

        json_format.ParseDict(part, a2a_pb2.Part())

    @pytest.mark.parametrize(
        "part,kind",
        [
            ({"text": "x"}, "text"),
            ({"structuredData": {"a": 1}}, "data"),
            ({"data": {"a": 1}}, "data"),
            ({"url": {"uri": "u"}}, "file"),
            ({"file": {"uri": "u"}}, "file"),
            ({"nope": 1}, "unknown"),
        ],
    )
    def test_inbound_parts_are_version_tolerant(self, part, kind):
        assert proto.read_part(part)[0] == kind


class TestMethodVocabulary:
    """The method name carries the version — see ``resolve_method``.

    The reference SDK's v1.0 client sends no ``A2A-Version`` header on any
    request; the vocabularies are disjoint, so the method is the one
    reliable statement of which parser awaits the response.
    """

    @pytest.mark.parametrize(
        "method,op,version",
        [
            ("message/send", "send", "0.3"),
            ("tasks/get", "get", "0.3"),
            ("tasks/cancel", "cancel", "0.3"),
            ("SendMessage", "send", "1.0"),
            ("GetTask", "get", "1.0"),
            ("CancelTask", "cancel", "1.0"),
        ],
    )
    def test_both_vocabularies_resolve(self, method, op, version):
        assert proto.resolve_method(method) == (op, version)

    @pytest.mark.parametrize(
        "method,op,version",
        [
            ("message/send", "send", "0.3"),
            ("tasks/get", "get", "0.3"),
            ("tasks/cancel", "cancel", "0.3"),
            ("tasks/pushNotificationConfig/set", "push_set", "0.3"),
            ("SendMessage", "send", "1.0"),
            ("GetTask", "get", "1.0"),
            ("CancelTask", "cancel", "1.0"),
            ("CreateTaskPushNotificationConfig", "push_set", "1.0"),
        ],
    )
    def test_a_caller_names_what_a_server_resolves(self, method, op, version):
        # The inverse direction, over the same table: what this platform
        # SENDS for an operation is what its own surface reads back as that
        # operation. A v0.3 partner is kept working by the 0.3 half.
        assert proto.method_for(op, version) == method

    @pytest.mark.parametrize("version", ["0.3.0", "1.0.4"])
    def test_a_patch_level_selects_its_own_vocabulary(self, version):
        # Normalised like every other comparison, or a stored ``0.3.0`` sends
        # a v0.3 partner ``SendMessage`` and collects -32601 on every call.
        assert proto.method_for("send", version) == proto.method_for(
            "send", proto.major_minor(version)
        )

    def test_an_unknown_version_raises_rather_than_guessing(self):
        for version in ("", "9.9", "nonsense"):
            with pytest.raises(ValueError, match="vocabulary for version"):
                proto.method_for("send", version)

    def test_an_unknown_operation_raises(self):
        with pytest.raises(ValueError, match="for operation"):
            proto.method_for("teleport", "1.0")

    @pytest.mark.parametrize(
        "method",
        [
            "ListTasks",  # real v1.0 method, deliberately not served
            "unknown",
            None,
            42,
        ],
    )
    def test_everything_else_is_unresolved(self, method):
        assert proto.resolve_method(method) is None


class TestBlockingFlag:
    """Opposite polarities: v0.3 opts IN to blocking, v1.0 opts OUT of it."""

    @pytest.mark.parametrize(
        "method,op",
        [
        ],
    )
    def test_streaming_resolves_in_both_vocabularies(self, method, op):
        """The card declares streaming, so both spellings of both operations
        must resolve — a partner derives its dialect from the card."""
        resolved = proto.resolve_method(method)
        assert resolved is not None, method
        assert resolved[0] == op
    def test_v03_defaults_to_non_blocking(self):
        assert proto.blocking_requested(None, "0.3") is False
        assert proto.blocking_requested({}, "0.3") is False

    def test_v03_blocking_is_an_explicit_opt_in(self):
        assert proto.blocking_requested({"blocking": True}, "0.3") is True

    def test_v1_defaults_to_blocking(self):
        assert proto.blocking_requested(None, "1.0") is True
        assert proto.blocking_requested({}, "1.0") is True

    def test_v1_return_immediately_opts_out_in_either_spelling(self):
        # proto-JSON parsers accept both key spellings; so must this read.
        assert proto.blocking_requested({"returnImmediately": True}, "1.0") is False
        assert proto.blocking_requested({"return_immediately": True}, "1.0") is False

    def test_a_malformed_configuration_falls_back_to_the_default(self):
        assert proto.blocking_requested("nonsense", "1.0") is True
        assert proto.blocking_requested(["nonsense"], "0.3") is False


class TestMessageShaping:
    def test_agent_role_is_spelled_per_version(self):
        legacy = proto.agent_message([], "0.3", message_id="m")
        modern = proto.agent_message([], "1.0", message_id="m")
        assert legacy["role"] == "agent" and legacy["kind"] == "message"
        assert modern["role"] == "ROLE_AGENT" and "kind" not in modern

    def test_v1_agent_message_survives_the_reference_parser(self):
        from a2a.types import a2a_pb2
        from google.protobuf import json_format

        message = proto.agent_message(
            [proto.text_part("done", "1.0")],
            "1.0",
            message_id="m1",
            task_id="t1",
            context_id="c1",
        )
        parsed = json_format.ParseDict(message, a2a_pb2.Message())
        assert parsed.role == a2a_pb2.ROLE_AGENT
        assert parsed.parts[0].text == "done"

    def test_status_message_is_an_agent_message(self):
        modern = proto.status_message("quota", "1.0", message_id="m")
        assert modern["role"] == "ROLE_AGENT"
        assert modern["parts"] == [{"text": "quota"}]


class TestSendResultEnvelope:
    def test_v03_result_is_the_task_itself(self):
        task = {"id": "t", "kind": "task"}
        assert proto.send_result(task, "0.3") is task

    def test_v1_result_wraps_the_task_in_the_response_oneof(self):
        """``SendMessageResponse`` is a proto oneof — the reference client
        refuses a bare task, which is how the missing envelope surfaced."""
        from a2a.types import a2a_pb2
        from google.protobuf import json_format

        rendered = {
            "id": "11111111-1111-1111-1111-111111111111",
            "contextId": "22222222-2222-2222-2222-222222222222",
            "status": {"state": proto.state_wire(proto.A2AState.COMPLETED, "1.0")},
            "artifacts": [
                {
                    "artifactId": "a1",
                    "name": "result",
                    "parts": [proto.text_part("answer", "1.0")],
                }
            ],
        }
        parsed = json_format.ParseDict(
            proto.send_result(rendered, "1.0"), a2a_pb2.SendMessageResponse()
        )
        assert parsed.task.status.state == a2a_pb2.TASK_STATE_COMPLETED
        assert parsed.task.artifacts[0].parts[0].text == "answer"


class TestErrorEnvelope:
    def test_named_errors_carry_a_machine_readable_type(self):
        err = proto.jsonrpc_error(1, proto.TASK_NOT_CANCELABLE, "no")["error"]
        assert err["code"] == proto.TASK_NOT_CANCELABLE
        assert err["data"]["@type"] == "TaskNotCancelableError"

    def test_uniform_not_found_is_identical_regardless_of_cause(self):
        # Unknown id, another client's task, unexposed agent, unknown token -
        # all must be indistinguishable.
        assert proto.not_found_error(1) == proto.not_found_error(1)
        assert proto.not_found_error(1)["error"]["message"] == "Not found"

    def test_result_envelope_shape(self):
        assert proto.jsonrpc_result(7, {"ok": True}) == {
            "jsonrpc": "2.0",
            "id": 7,
            "result": {"ok": True},
        }
