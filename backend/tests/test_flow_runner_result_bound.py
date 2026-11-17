"""What a flow reads of a runner command's result.

The result upload takes up to 16 MiB, but a flow never reads more than 1 MiB
of a result. ``bound_runner_result`` keeps it so: a result within the limit is
returned as it is, a larger one is cut to fit, its fields kept.
"""

import json

import pytest

from app.services.flow_runner import (
    FLOW_RUNNER_RESULT_MAX_BYTES,
    FLOW_RUNNER_RESULT_TRUNCATED_KEY,
    _json_bytes,
    bound_runner_result,
)

LIMIT = FLOW_RUNNER_RESULT_MAX_BYTES


def test_a_result_within_the_limit_is_the_same_object():
    result = {"exit_code": 0, "stdout": "a" * (LIMIT - 64), "stderr": "é"}
    assert _json_bytes(result) <= LIMIT

    assert bound_runner_result(result) is result


def test_a_large_output_keeps_its_head_and_says_it_was_cut():
    result = {"exit_code": 1, "stdout": "a" * (5 * LIMIT), "stderr": "boom"}

    bounded = bound_runner_result(result)

    assert _json_bytes(bounded) <= LIMIT
    assert bounded[FLOW_RUNNER_RESULT_TRUNCATED_KEY] == {
        "original_bytes": _json_bytes(result),
        "limit_bytes": LIMIT,
    }
    assert (bounded["exit_code"], bounded["stderr"]) == (1, "boom")
    assert bounded["stdout"].startswith("a" * (LIMIT - 4096))
    assert bounded["stdout"].endswith("for a flow.] ...")
    # The command's own copy is untouched.
    assert len(result["stdout"]) == 5 * LIMIT


def test_two_long_streams_each_keep_a_head_measured_as_written():
    # A control character is six bytes of JSON: the cut is by the encoding,
    # and neither stream is given up for the other.
    result = {"stdout": "\x01" * LIMIT, "stderr": "\x02" * LIMIT}

    bounded = bound_runner_result(result)

    assert _json_bytes(bounded) <= LIMIT
    for field, char in (("stdout", "\x01"), ("stderr", "\x02")):
        head = bounded[field].split("\n\n... [TRUNCATED", 1)[0]
        assert head == char * len(head) and len(head) > LIMIT // 6 // 4, (field, len(head))


def test_a_head_denser_than_the_rest_is_kept_to_the_limit():
    # Escaped characters first, plain text after: the head is cut on its own
    # encoding, not the text's average one.
    result = {"stdout": "\x01" * 1_000_000 + "a" * 9_000_000}

    bounded = bound_runner_result(result)

    assert _json_bytes(bounded) <= LIMIT
    head = bounded["stdout"].split("\n\n... [TRUNCATED", 1)[0]
    assert head == "\x01" * len(head)
    # Nearly all of the room is used, not handed back as a bare marker.
    assert _json_bytes(bounded) > LIMIT - 1024


@pytest.mark.parametrize(
    "result",
    [
        {"path": "/data", "entries": [{"name": f"f{i:07}", "is_dir": False} for i in range(80_000)]},
        {"content": "é" * LIMIT, "url": "https://example.com"},
    ],
    ids=["folder-listing", "multibyte-page"],
)
def test_other_results_are_cut_to_the_limit_keeping_their_shape(result):
    bounded = bound_runner_result(result)

    assert _json_bytes(bounded) <= LIMIT
    assert set(result) <= set(bounded)
    assert FLOW_RUNNER_RESULT_TRUNCATED_KEY in bounded
    if "entries" in result:
        assert 0 < len(bounded["entries"]) < len(result["entries"])
        assert bounded["entries"] == result["entries"][: len(bounded["entries"])]


def test_a_result_nothing_can_be_cut_from_is_reduced_to_its_note():
    result = {f"k{i:07}": i for i in range(200_000)}

    bounded = bound_runner_result(result)

    assert bounded == {
        FLOW_RUNNER_RESULT_TRUNCATED_KEY: {"original_bytes": _json_bytes(result), "limit_bytes": LIMIT}
    }
    json.dumps(bounded)
