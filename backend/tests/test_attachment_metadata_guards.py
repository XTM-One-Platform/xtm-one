"""Regression tests for the malformed-metadata guards on the chat /
agent-chats attachment decoders.

Round-N picked up two Copilot threads against ``_parse_msg_attachments``
(``backend/app/api/v1/agent_chats.py``) and an independent senior-reviewer
pass turned up the same shape of bug in ``parse_message_metadata`` /
``parse_attachments`` / ``_build_message_response`` over in
``backend/app/api/v1/chat.py``.  Three failure modes were possible at
every site:

1.  ``json.loads(metadata_json)`` returns a non-dict (``json.loads("[]")``
    → ``[]``).  All downstream code calls ``.get(...)`` directly so this
    raises ``AttributeError`` and either drops every attachment via the
    ``except Exception`` rescue (``agent_chats.py``) or propagates a
    500 up through the message-serialisation endpoint (``chat.py``,
    where there is no rescue).

2.  ``meta["attachments"]`` is set to a non-list value (string, dict,
    int).  Iterating it would either iterate over chars / keys instead
    of attachment dicts, or raise ``TypeError`` outright.

3.  Individual entries in ``attachments`` are not dicts.  ``att.get(...)``
    raises ``AttributeError`` and (via the broad ``except``) drops the
    entire list — meaning a single bad row poisons every otherwise-valid
    attachment in the same message.

Both sites now guard each step with ``isinstance(...)`` checks and skip
malformed entries instead of erasing the whole list, matching the
existing gold-standard pattern in ``backend/app/channels/router.py``.
These tests pin every failure mode at every site so a regression that
strips any guard trips loudly.
"""

from __future__ import annotations

import json

import pytest

from app.api.v1.agent_chats import _parse_msg_attachments
from app.api.v1.chat import _build_message_response
from app.services.chat_messages import parse_attachments, parse_message_metadata

# ── chat_messages.py::parse_message_metadata ─────────────────────────────────────────────


@pytest.mark.parametrize(
    "value",
    [
        None,
        "",
        "not json",
        "[]",           # valid JSON but not a dict
        '"a string"',   # valid JSON but a scalar
        "123",          # valid JSON but a scalar
        "true",         # valid JSON but a bool
        "null",         # valid JSON but None
    ],
)
def test_parse_metadata_returns_empty_dict_for_non_dict_input(value):
    """``parse_message_metadata`` MUST always return a ``dict`` so every caller
    can rely on ``.get(...)`` without re-checking the type.  This used
    to silently propagate non-dict JSON results, causing
    ``AttributeError: 'list' object has no attribute 'get'`` deep in the
    message serialisation path.  Parametrised across the four shapes
    of "valid JSON, wrong type" plus the two "no JSON at all" inputs
    so any regression to ``return parsed`` (without the
    ``isinstance(parsed, dict)`` guard) trips on every variant.
    """
    result = parse_message_metadata(value)
    assert isinstance(result, dict)
    assert result == {}


def test_parse_metadata_returns_dict_unchanged_for_well_formed_input():
    """Happy-path symmetry guard so the dict-guard fix above can't
    accidentally also collapse legitimate metadata dicts to ``{}``."""
    result = parse_message_metadata(json.dumps({"attachments": [], "tool_names": ["x"]}))
    assert result == {"attachments": [], "tool_names": ["x"]}


# ── chat_messages.py::parse_attachments ──────────────────────────────────────────


@pytest.mark.parametrize(
    "raw_meta",
    [
        None,
        "",
        "{}",
        json.dumps({"attachments": None}),
        json.dumps({"attachments": []}),
        json.dumps({"attachments": "a string"}),
        json.dumps({"attachments": {"file_id": "x"}}),  # dict not list
        json.dumps({"attachments": 42}),
        json.dumps({}),
    ],
)
def test_parse_attachments_returns_none_for_missing_or_malformed(raw_meta):
    """``parse_attachments`` must return ``None`` (never raise) when
    metadata is missing, attachments is not a list, or the list is
    empty.  Pins the ``isinstance(attachments, list)`` guard against a
    regression to the previous shape that iterated ``meta["attachments"]``
    unconditionally and raised on non-list values.
    """
    assert (
        parse_attachments(raw_meta, conversation_id="c", message_id="m") is None
    )


def test_parse_attachments_skips_non_dict_entries():
    """A list mixing dict entries with stray scalars / nulls must yield
    just the valid dicts — NOT drop the entire list and NOT raise.  The
    previous shape called ``att.get("file_id")`` on every entry and the
    AttributeError from a string entry would propagate up through
    ``_build_message_response``.
    """
    raw = json.dumps(
        {
            "attachments": [
                {"filename": "a.txt", "file_id": "f-1"},
                "stray string",           # bad
                None,                       # bad
                42,                          # bad
                {"filename": "b.txt", "file_id": "f-2"},
            ]
        }
    )
    parsed = parse_attachments(raw, conversation_id="c-1", message_id="m-1")
    assert parsed is not None
    assert [a["filename"] for a in parsed] == ["a.txt", "b.txt"]
    # Download URLs still wired for the valid entries.
    assert parsed[0]["download_url"].endswith("/files/f-1/download")
    assert parsed[1]["download_url"].endswith("/files/f-2/download")


def test_parse_attachments_returns_none_when_only_invalid_entries():
    """All-bad list must collapse to ``None`` (not an empty list, which
    Pydantic would happily serialise as ``attachments: []``)."""
    raw = json.dumps({"attachments": ["a", 1, None]})
    assert (
        parse_attachments(raw, conversation_id="c", message_id="m") is None
    )


# ── chat.py::_build_message_response (inline attachment normalisation) ──


class _StubMessage:
    """Minimal duck-typed stand-in for ``Message`` covering only the
    attributes ``_build_message_response`` reads.  A real Message would
    require an SQLAlchemy session and a DB row; the metadata-decode path
    we are pinning here has no DB interaction, so the stub is enough.
    """

    def __init__(self, metadata_json: str | None):
        import datetime
        import uuid as _uuid

        self.id = _uuid.uuid4()
        self.conversation_id = _uuid.uuid4()
        self.role = "assistant"
        self.content = "hello"
        self.channel = "web"
        self.channel_message_id = None
        self.sequence = 1
        self.created_at = datetime.datetime.now(datetime.UTC)
        self.metadata_json = metadata_json
        self.llm_trace_id = None


def test_build_message_response_handles_non_list_attachments():
    """Mirror guard for ``_build_message_response``: a string / dict in
    place of a list of attachments must yield ``attachments=None`` on
    the response, not raise."""
    m = _StubMessage(json.dumps({"attachments": "not a list"}))
    resp = _build_message_response(m)
    assert resp.attachments is None


def test_build_message_response_skips_non_dict_entries():
    """Same mixed-list guard as ``parse_attachments``: dict entries
    survive, non-dict entries are dropped, the response carries the
    valid set.  ``MessageAttachment`` requires ``filename`` / ``type`` /
    ``size`` so the valid entries in the fixture carry those fields —
    the dropped entries are the stray non-dicts that the new guards
    must filter out before pydantic validation runs."""
    m = _StubMessage(
        json.dumps(
            {
                "attachments": [
                    "junk",
                    {
                        "filename": "ok.txt",
                        "type": "text",
                        "size": 4,
                        "file_id": "f-9",
                    },
                    None,
                ]
            }
        )
    )
    resp = _build_message_response(m)
    assert resp.attachments is not None
    assert len(resp.attachments) == 1
    assert resp.attachments[0].filename == "ok.txt"
    assert (resp.attachments[0].download_url or "").endswith(
        "/files/f-9/download"
    )


# ── agent_chats.py::_parse_msg_attachments ───────────────────────────────


@pytest.mark.parametrize(
    "raw_meta",
    [
        None,
        "",
        "not json",
        "[]",                                              # non-dict JSON
        json.dumps({}),
        json.dumps({"attachments": None}),
        json.dumps({"attachments": "string"}),
        json.dumps({"attachments": {"file_id": "x"}}),
        json.dumps({"attachments": []}),
        json.dumps({"attachments": [None, "x", 42]}),  # all-invalid entries
    ],
)
def test_parse_msg_attachments_returns_none_for_missing_or_malformed(raw_meta):
    """The Copilot-flagged site (``agent_chats.py::_parse_msg_attachments``)
    used to wrap the whole decode in ``try/except Exception: return None``.
    That mode dropped EVERY attachment from the response on the first
    bad row.  The fix narrows the rescue to ``json.loads`` only and adds
    explicit ``isinstance`` guards for the dict / list / dict-entry
    layers so legitimate rows survive a sibling bad row.  Same parametric
    sweep as ``parse_attachments`` above so both sites have the same
    contract.
    """
    assert (
        _parse_msg_attachments(
            raw_meta, conversation_id="c", message_id="m"
        )
        is None
    )


def test_parse_msg_attachments_skips_non_dict_entries_but_keeps_valid():
    """Single bad row in an otherwise valid list must NOT drop the rest
    — this is the actual production-bug-resolving assertion (a stray
    legacy entry used to blank the entire Chats tab for a conversation).
    """
    raw = json.dumps(
        {
            "attachments": [
                "stray-string",
                {"filename": "a.txt", "file_id": "f-1"},
                None,
                {"filename": "b.txt", "file_id": "f-2"},
            ]
        }
    )
    parsed = _parse_msg_attachments(
        raw, conversation_id="c-1", message_id="m-1"
    )
    assert parsed is not None
    assert [a["filename"] for a in parsed] == ["a.txt", "b.txt"]
    assert parsed[0]["download_url"].endswith("/files/f-1/download")
    assert parsed[1]["download_url"].endswith("/files/f-2/download")


def test_parse_msg_attachments_happy_path_unchanged():
    """Symmetry guard so the new guards cannot accidentally drop
    well-formed attachments — pins the existing happy-path contract."""
    raw = json.dumps(
        {
            "attachments": [
                {"filename": "a.txt", "file_id": "f-1"},
                {"filename": "b.txt", "file_id": "f-2"},
            ]
        }
    )
    parsed = _parse_msg_attachments(
        raw, conversation_id="c-1", message_id="m-1"
    )
    assert parsed is not None
    assert len(parsed) == 2
    assert all("download_url" in a for a in parsed)
