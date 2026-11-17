"""The XTM One Relay wire protocol and its pure rules.

Everything here runs without Redis or a database: the frame codec, the
encryption a frame gets in Redis, the error a caller sees, what a hello and a
response header may carry, and the destination patterns of a relay group.
The bus itself runs against a real Redis in ``test_relay_bus_redis.py``.
"""

from __future__ import annotations

import json
import struct

import httpx
import pytest
from cryptography.fernet import InvalidToken

from app.integrations.http import format_network_error
from app.models.relay import RELAY_PROTOCOL_VERSION
from app.services import relay_transport as rt
from app.services.relays import (
    DestinationPatternError,
    allows_all,
    destination_allowed,
    normalize_destination_patterns,
)

# -- Frame codec ---------------------------------------------------------------


def test_a_frame_round_trips_and_carries_its_payload_length():
    raw = rt.encode_frame({"type": "data", "sid": "a" * 32}, b"hello")
    (header_len,) = struct.unpack_from(">I", raw)
    header = json.loads(raw[4 : 4 + header_len])
    assert header == {"type": "data", "sid": "a" * 32, "len": 5}
    frame, end = rt.decode_frame(raw)
    assert frame.type == "data"
    assert frame.sid == "a" * 32
    assert frame.payload == b"hello"
    assert end == len(raw)


def test_a_frame_without_payload_has_no_length_and_a_caller_len_is_ignored():
    raw = rt.encode_frame({"type": "heartbeat", "len": 999})
    (header_len,) = struct.unpack_from(">I", raw)
    assert json.loads(raw[4 : 4 + header_len]) == {"type": "heartbeat"}


def test_a_header_without_len_owns_the_rest_of_the_message():
    head = json.dumps({"type": "data", "sid": "b" * 32}).encode()
    raw = struct.pack(">I", len(head)) + head + b"the whole websocket message"
    frame, end = rt.decode_frame(raw)
    assert frame.payload == b"the whole websocket message"
    assert end == len(raw)


def test_concatenated_frames_each_carry_len_even_when_empty():
    body = rt.encode_frames(
        [
            rt.Frame({"type": "open", "sid": "c" * 32}, b"abc"),
            rt.Frame({"type": "end", "sid": "c" * 32}),
            rt.Frame({"type": "data", "sid": "d" * 32}, b"x" * 70_000),
        ]
    )
    frames = rt.decode_frames(body)
    assert [f.type for f in frames] == ["open", "end", "data"]
    assert frames[0].payload == b"abc"
    assert frames[1].payload == b""
    assert frames[1].header["len"] == 0
    assert len(frames[2].payload) == 70_000


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"\x00\x00",
        struct.pack(">I", 0),
        struct.pack(">I", rt.MAX_HEADER_BYTES + 1) + b"{}",
        struct.pack(">I", 50) + b'{"type":"data"}',
        struct.pack(">I", 9) + b"not json!",
        struct.pack(">I", 2) + b"[]",
        struct.pack(">I", 15) + b'{"sid":"abcdef"}'[:15],
    ],
)
def test_bytes_that_are_not_a_frame_are_refused(raw):
    with pytest.raises(rt.FrameError):
        rt.decode_frame(raw)


@pytest.mark.parametrize("length", [-1, True, "3", 1.5, 100])
def test_a_bad_payload_length_is_refused(length):
    head = json.dumps({"type": "data", "len": length}).encode()
    with pytest.raises(rt.FrameError):
        rt.decode_frame(struct.pack(">I", len(head)) + head + b"abc")


def test_a_header_past_the_bound_is_refused_on_encode():
    with pytest.raises(rt.FrameError):
        rt.encode_frame({"type": "open", "url": "x" * rt.MAX_HEADER_BYTES})
    with pytest.raises(rt.FrameError):
        rt.encode_frames([rt.Frame({"type": "open", "url": "x" * rt.MAX_HEADER_BYTES})])


def test_a_truncated_body_of_frames_is_refused_whole():
    body = rt.encode_frames([rt.Frame({"type": "data", "sid": "e" * 32}, b"abcdef")])
    with pytest.raises(rt.FrameError):
        rt.decode_frames(body[:-2])


# -- Sealing -------------------------------------------------------------------


def test_a_sealed_frame_is_not_readable_and_unseals_to_the_same_bytes():
    raw = rt.encode_frame({"type": "data", "sid": "f" * 32}, b"Authorization: secret")
    token = rt.seal(raw)
    assert "secret" not in token
    assert rt.unseal(token) == raw


def test_a_tampered_sealed_frame_is_refused():
    token = rt.seal(rt.encode_frame({"type": "end", "sid": "g" * 32}))
    tampered = token[:-4] + ("AAAA" if not token.endswith("AAAA") else "BBBB")
    with pytest.raises(InvalidToken):
        rt.unseal(tampered)


@pytest.mark.parametrize("fields", [{}, {"f": 3}, {"f": "garbage"}, None])
def test_an_unreadable_entry_is_dropped(fields):
    assert rt._unseal_entry(fields) is None


def test_a_readable_entry_is_its_frame():
    entry = rt._sealed_frame({"type": "data", "sid": "h" * 32}, b"xyz")
    frame = rt._unseal_entry(entry)
    assert frame is not None
    assert frame.payload == b"xyz"


# -- Errors --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("code", "httpx_class"),
    [
        ("dns_failed", httpx.ConnectError),
        ("connect_failed", httpx.ConnectError),
        ("tls_failed", httpx.ConnectError),
        ("no_relay_online", httpx.ConnectError),
        ("timeout", httpx.TimeoutException),
        ("relay_disconnected", httpx.RemoteProtocolError),
        ("protocol_error", httpx.RemoteProtocolError),
        ("destination_denied", httpx.TransportError),
        ("too_large", httpx.TransportError),
    ],
)
def test_a_relay_error_is_the_httpx_error_integrations_already_handle(code, httpx_class):
    exc = rt.relay_error(code)
    assert isinstance(exc, httpx_class)
    assert isinstance(exc, rt.RelayError)
    assert exc.code == code
    assert str(exc) == rt.RELAY_ERROR_MESSAGES[code]


def test_every_error_code_has_a_message():
    assert set(rt.RELAY_ERROR_CODES) == set(rt.RELAY_ERROR_MESSAGES)
    assert all(rt.RELAY_ERROR_MESSAGES[c] for c in rt.RELAY_ERROR_CODES)


def test_an_unknown_code_is_a_protocol_error():
    exc = rt.relay_error("not_a_code")
    assert exc.code == "protocol_error"


def test_the_message_names_the_group_and_a_bounded_detail():
    exc = rt.relay_error("connect_failed", "x" * 5000, group_name="Paris DC")
    text = str(exc)
    assert "relay group 'Paris DC'" in text
    assert len(text) < 700


def test_an_integration_reports_the_relay_cause_not_an_unreachable_service():
    exc = rt.relay_error("no_relay_online", group_name="Paris DC")
    text = format_network_error(exc, "Splunk", "search events")
    assert "No relay of this group is online" in text
    assert "Paris DC" in text
    assert "may be unreachable" not in text
    plain = format_network_error(httpx.ConnectError("boom"), "Splunk", "search events")
    assert "may be unreachable" in plain


# -- Hello and response headers -------------------------------------------------


def _hello(**overrides):
    header = {"type": "hello", "protocol": RELAY_PROTOCOL_VERSION, "capabilities": ["http"]}
    header.update(overrides)
    return header


def test_a_valid_hello_is_accepted():
    assert rt.check_hello(_hello()) is None


@pytest.mark.parametrize(
    "header",
    [
        _hello(type="heartbeat"),
        _hello(protocol=RELAY_PROTOCOL_VERSION + 1),
        _hello(protocol=None),
        _hello(capabilities=["tcp"]),
        _hello(capabilities="http"),
    ],
)
def test_a_hello_the_platform_cannot_serve_is_refused(header):
    assert rt.check_hello(header)


def test_a_hello_writes_bounded_typed_values_only():
    values = rt._clean_hello(
        _hello(
            capabilities=["http", "tcp", "bogus", 3],
            destinations=["splunk.corp.local:8089", "", 7, "*"],
            max_concurrent=10_000,
            host_facts={"hostname": "relay-01", "os": "linux"},
            version="  1.2.3  ",
            protocol=RELAY_PROTOCOL_VERSION,
        )
    )
    assert values["capabilities"] == ["http", "tcp"]
    assert values["declared_destinations"] == ["splunk.corp.local:8089", "*"]
    assert values["allows_all_destinations"] is True
    assert values["max_concurrent_requests"] == 256
    assert values["agent_version"] == "1.2.3"
    assert values["hostname"] == "relay-01"


def test_oversized_host_facts_are_not_stored():
    values = rt._clean_hello(_hello(host_facts={"blob": "x" * (rt.MAX_HOST_FACTS_BYTES + 10)}))
    assert "host_facts" not in values


def test_a_response_head_is_rebuilt_from_known_keys():
    header = rt._clean_response_header(
        rt.Frame(
            {
                "type": "response_head",
                "sid": "i" * 32,
                "status": 200,
                "headers": [["Content-Type", "application/json"], ["bad"], [1, "x"], ["X", "y" * 20_000]],
                "extra": "dropped",
            }
        )
    )
    assert header == {
        "type": "response_head",
        "sid": "i" * 32,
        "status": 200,
        "headers": [["Content-Type", "application/json"]],
    }


@pytest.mark.parametrize("status", [None, "200", True, 99, 1000])
def test_a_response_head_without_a_status_is_refused(status):
    assert rt._clean_response_header(rt.Frame({"type": "response_head", "status": status})) is None


def test_an_error_frame_keeps_known_codes_only():
    header = rt._clean_response_header(
        rt.Frame({"type": "error", "sid": "j" * 32, "code": "rm -rf", "message": "m" * 2000})
    )
    assert header["code"] == "protocol_error"
    assert len(header["message"]) == rt.MAX_ERROR_MESSAGE_CHARS


# -- Request shaping -----------------------------------------------------------


def test_hop_by_hop_headers_and_the_ones_the_relay_sets_are_not_forwarded():
    request = httpx.Request(
        "POST",
        "https://splunk.corp.local:8089/services/search/jobs?q=1",
        headers={
            "Authorization": "Bearer token",
            "Connection": "keep-alive",
            "Transfer-Encoding": "chunked",
            "Host": "evil.example",
            "X-Custom": "kept",
        },
        content=b"search=index%3Dmain",
    )
    names = {name.lower() for name, _ in rt._forwarded_request_headers(request)}
    assert {"authorization", "x-custom"} <= names
    assert not names & {"connection", "transfer-encoding", "host", "content-length"}


def test_request_timeouts_follow_the_client_and_are_clamped():
    request = httpx.Request(
        "GET",
        "https://a.example",
        extensions={"timeout": {"connect": 5.0, "read": 30.0, "write": 10.0, "pool": 1.0}},
    )
    assert rt._request_timeouts(request) == (30.0, 5.0)
    huge = httpx.Request("GET", "https://a.example", extensions={"timeout": {"read": 10_000}})
    assert rt._request_timeouts(huge) == (rt.MAX_REQUEST_TIMEOUT_SECONDS, None)
    none = httpx.Request("GET", "https://a.example")
    assert rt._request_timeouts(none) == (rt.DEFAULT_REQUEST_TIMEOUT_SECONDS, None)


# -- Destination patterns --------------------------------------------------------


def test_patterns_are_canonicalised_and_deduplicated():
    assert normalize_destination_patterns(
        ["Splunk.Corp.Local:8089", "splunk.corp.local:8089", "*.corp.local", "10.0.0.0/8", "[fd00::1]:443"]
    ) == ["splunk.corp.local:8089", "*.corp.local", "10.0.0.0/8", "[fd00::1]:443"]
    assert normalize_destination_patterns("a.example, b.example\n10.1.2.3") == [
        "a.example",
        "b.example",
        "10.1.2.3",
    ]
    assert normalize_destination_patterns(None) is None


@pytest.mark.parametrize(
    "pattern",
    ["", "https://splunk.corp.local", "host:0", "host:70000", "a*.corp.local", "*.", "10.0.0.0/33", "[fd00::1"],
)
def test_a_pattern_the_grammar_refuses_is_named(pattern):
    with pytest.raises(DestinationPatternError):
        normalize_destination_patterns([pattern])


def test_none_allows_everything_and_an_empty_list_nothing():
    assert destination_allowed(None, "anything.example", 443)
    assert not destination_allowed([], "anything.example", 443)


@pytest.mark.parametrize(
    ("host", "port", "allowed"),
    [
        ("splunk.corp.local", 8089, True),
        ("SPLUNK.corp.local.", 8089, True),
        ("splunk.corp.local", 9000, False),
        ("tanium.corp.local", 443, True),
        ("corp.local", 443, False),
        ("evilcorp.local", 443, False),
        ("10.20.30.40", 9200, True),
        ("11.0.0.1", 9200, False),
        ("fd00::1", 443, True),
        ("[fd00::1]", 443, True),
        ("fd00::1", 80, False),
    ],
)
def test_a_destination_matches_host_suffix_cidr_and_port(host, port, allowed):
    patterns = ["splunk.corp.local:8089", "*.corp.local:443", "10.0.0.0/8", "[fd00::1]:443"]
    assert destination_allowed(patterns, host, port) is allowed


def test_a_stored_pattern_that_no_longer_parses_matches_nothing():
    assert not destination_allowed(["https://old.example"], "old.example", 443)


def test_allows_all_spots_the_wildcard_with_or_without_a_port():
    assert allows_all(["*"])
    assert allows_all(["a.example", "*:443"])
    assert not allows_all(["*.corp.local"])
    assert not allows_all(None)
