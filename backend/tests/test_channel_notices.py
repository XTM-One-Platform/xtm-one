"""Channel bot notices are written in the reader's language.

The catalog (``services/channel_notices.py``) holds every notice in the four
UI languages with the same placeholders; the language is the user's own, then
the platform default, then English; the channel modules send no notice of
their own; a notice a shared service writes in English is recognised on its
way out, and these tests fail the moment that service's wording drifts from
the catalog's.
"""

from __future__ import annotations

import ast
import re
from types import SimpleNamespace

import pytest

from app.services import channel_notices
from app.services.channel_notices import (
    BUDGET_REASONS,
    DEFAULT_LANGUAGE,
    LANGUAGES,
    NOTICES,
    REPLY_NOTICES,
    localized_reply,
    notice,
    notice_language,
    placeholders,
)
from tests import app_sources

_TRANSLATED = tuple(lang for lang in LANGUAGES if lang != DEFAULT_LANGUAGE)


def _platform_default(monkeypatch, language: str | None) -> None:
    import app.services.branding as branding

    monkeypatch.setattr(branding, "get_cached_branding", lambda: SimpleNamespace(language=language))


# --- The catalog -------------------------------------------------------------


def test_every_notice_exists_in_every_language():
    incomplete = {
        key: sorted(set(LANGUAGES) - {lang for lang, text in texts.items() if text.strip()})
        for key, texts in NOTICES.items()
        if {lang for lang, text in texts.items() if text.strip()} != set(LANGUAGES)
    }
    assert not incomplete, f"notices missing a language: {incomplete}"
    unknown = {key: sorted(set(texts) - set(LANGUAGES)) for key, texts in NOTICES.items() if set(texts) - set(LANGUAGES)}
    assert not unknown, f"notices in a language the platform does not ship: {unknown}"


def test_every_translation_has_the_english_placeholders():
    mismatched = {
        f"{key}.{lang}": sorted(placeholders(text) ^ placeholders(texts[DEFAULT_LANGUAGE]))
        for key, texts in NOTICES.items()
        for lang, text in texts.items()
        if placeholders(text) != placeholders(texts[DEFAULT_LANGUAGE])
    }
    assert not mismatched, mismatched


def test_every_reply_notice_is_a_catalog_entry():
    assert set(REPLY_NOTICES) <= set(NOTICES)
    assert len(set(REPLY_NOTICES)) == len(REPLY_NOTICES)


# --- The language ------------------------------------------------------------


@pytest.mark.parametrize(
    ("stored", "expected"), [("en-us", "en"), ("es-es", "es"), ("fr-fr", "fr"), ("ja-jp", "ja")]
)
def test_an_identified_user_gets_their_own_language(monkeypatch, stored, expected):
    _platform_default(monkeypatch, "ja-jp" if expected != "ja" else "fr-fr")
    assert notice_language(SimpleNamespace(language=stored)) == expected


@pytest.mark.parametrize("stored", ["auto", "", None, "de-de"])
def test_a_user_without_a_language_of_their_own_gets_the_platform_default(monkeypatch, stored):
    _platform_default(monkeypatch, "es-es")
    assert notice_language(SimpleNamespace(language=stored)) == "es"


def test_an_unidentified_sender_gets_the_platform_default(monkeypatch):
    _platform_default(monkeypatch, "fr-fr")
    assert notice_language(None) == "fr"
    assert notice_language() == "fr"


@pytest.mark.parametrize("platform", ["auto", None, ""])
def test_with_no_platform_default_it_is_english(monkeypatch, platform):
    _platform_default(monkeypatch, platform)
    assert notice_language(None) == "en"
    assert notice_language(SimpleNamespace(language="auto")) == "en"


async def test_a_user_known_by_id_is_read_once(monkeypatch):
    _platform_default(monkeypatch, "es-es")

    class _Db:
        queries = 0

        async def scalar(self, _stmt):
            self.queries += 1
            return "ja-jp"

    db = _Db()
    assert await channel_notices.user_notice_language(db, "00000000-0000-0000-0000-000000000001") == "ja"
    assert db.queries == 1
    assert await channel_notices.user_notice_language(db, None) == "es"
    assert await channel_notices.user_notice_language(db, "not-a-uuid") == "es"


# --- Rendering ---------------------------------------------------------------


@pytest.mark.parametrize("language", LANGUAGES)
def test_a_notice_renders_its_placeholders(language):
    text = notice("agent_not_on_channel", language, channel="google_chat")
    assert "google_chat" in text and "{" not in text


def test_a_missing_translation_falls_back_to_english(monkeypatch):
    monkeypatch.setitem(NOTICES, "_only_english", {"en": "Hello {name}."})
    assert notice("_only_english", "ja", name="Ada") == "Hello Ada."
    assert notice("agent_disabled", "de") == NOTICES["agent_disabled"]["en"]
    assert notice("agent_disabled", None) == NOTICES["agent_disabled"]["en"]


# --- Reply notices written by shared services ---------------------------------


def _service_texts() -> dict[str, str]:
    """What the services shared with the web chat actually write, rendered live."""
    from app.llm import router as llm_router
    from app.llm.base import overflow_friendly_message
    from app.services import agent_chat, nova_guard, prompt_guards, quota
    from app.tools import agent_loop
    from app.tools.approval import no_approval_channel_message

    gate = SimpleNamespace(proposals=[{"tool_name": "send_email"}, {"tool_name": "delete_repo"}])
    return {
        "quota_exceeded": agent_chat.QUOTA_EXCEEDED_MESSAGE,
        "quota_exceeded/quota": quota.QUOTA_EXCEEDED_MESSAGE,
        "quota_unavailable": quota.QUOTA_UNAVAILABLE_MESSAGE,
        "accountless_quota_exceeded": quota.ACCOUNTLESS_QUOTA_EXCEEDED_MESSAGE,
        "accountless_quota_unavailable": quota.ACCOUNTLESS_QUOTA_UNAVAILABLE_MESSAGE,
        "guard_refused": nova_guard.NOVA_REFUSAL_MESSAGE,
        "output_withheld": prompt_guards.MODEL_OUTPUT_WITHHELD_MESSAGE,
        "request_too_large": llm_router.REQUEST_TOO_LARGE_STREAMING_MESSAGE,
        "request_cancelled": agent_loop._CANCELLED_MESSAGE,
        "empty_response": agent_loop.EMPTY_RESPONSE_FALLBACK,
        "forced_answer_timeout": agent_loop.FORCED_ANSWER_TIMEOUT_MESSAGE,
        "forced_answer_error": agent_loop.FORCED_ANSWER_ERROR_MESSAGE,
        "partial_results": f"{agent_loop.FORCED_ANSWER_TIMEOUT_MESSAGE}\n\nPartial results:\n\n- 3 hosts",
        "context_too_many_tools": overflow_friendly_message(SimpleNamespace(is_tool_dominated=True)),
        "context_too_long": overflow_friendly_message(SimpleNamespace(is_tool_dominated=False)),
        "approval_needed": no_approval_channel_message(gate),
        "approval_needed_here": no_approval_channel_message(gate, mention_admin_setting=False),
    }


#: Reply notices written inline (no constant to read): the module whose string
#: literals must carry the catalog's English word for word.
_INLINE_SOURCES = {
    "approval_abandoned": ("services/agent_chat.py", "services/general_chat.py"),
    "turn_timeout": ("services/agent_chat.py", "services/general_chat.py"),
    "model_timeout": ("tools/agent_loop.py",),
}


@pytest.mark.parametrize("case", sorted(_service_texts()))
@pytest.mark.parametrize("language", _TRANSLATED)
def test_a_service_notice_goes_out_in_the_reader_language(case, language):
    key = case.split("/", 1)[0]
    params = {
        "approval_needed": {"names": "send_email, delete_repo"},
        "approval_needed_here": {"names": "send_email, delete_repo"},
        "partial_results": {"lead": notice("forced_answer_timeout", language), "partial": "- 3 hosts"},
    }.get(key, {})
    assert localized_reply(_service_texts()[case], language) == notice(key, language, **params), (
        f"{case}: the service's wording no longer matches the catalog"
    )


@pytest.mark.parametrize("key", sorted(_INLINE_SOURCES))
def test_an_inline_service_notice_matches_the_catalog_word_for_word(key):
    english = NOTICES[key][DEFAULT_LANGUAGE]
    for rel in _INLINE_SOURCES[key]:
        literals = {
            node.value
            for node in app_sources.walk(app_sources.tree(app_sources.APP_DIR / rel))
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        }
        assert english in literals, f"{rel} no longer writes the {key!r} notice the catalog translates"


def test_the_assignment_notifications_match_the_catalog():
    source = app_sources.source(app_sources.APP_DIR / "services" / "notify.py")
    assert "f\"Assignment '{assignment.name}' failed.\"" in source
    assert "f\"Assignment '{assignment.name}' completed.\"" in source
    assert localized_reply("Assignment 'Daily digest' failed.", "fr") == notice("assignment_failed", "fr", name="Daily digest")


# --- The per-run budget's notices (tools/agent_loop.py) -------------------------


def _violations():
    """One exhausted budget per dimension, with what its reason should read."""
    from app.tools.run_budget import BudgetDimension, BudgetViolation

    def _v(dimension, limit):
        return BudgetViolation(dimension=dimension, spent=limit, projected=limit, limit=limit, gate=limit)

    return [
        (_v(BudgetDimension.COST, 0.5), "budget_reason_cost", {"amount": "0.5000"}),
        (_v(BudgetDimension.TOKENS, 50000), "budget_reason_tokens", {"count": "50,000"}),
        (_v(BudgetDimension.TOOL_CALLS, 40), "budget_reason_tool_calls", {"count": "40"}),
        (_v(BudgetDimension.DURATION, 300), "budget_reason_time", {"seconds": "300"}),
    ]


@pytest.mark.parametrize("index", range(4), ids=["cost", "tokens", "tool_calls", "time"])
@pytest.mark.parametrize("language", _TRANSLATED)
def test_the_budget_notices_go_out_in_the_reader_language(index, language):
    from app.tools import agent_loop

    violation, reason_key, reason_params = _violations()[index]
    reason = notice(reason_key, language, **reason_params)
    stopped = notice("budget_exhausted", language, reason=reason)

    # A run whose synthesis the budget could not pay for, with and without a digest.
    assert localized_reply(agent_loop._budget_exhausted_text(violation, "- 3 hosts"), language) == notice(
        "partial_results", language, lead=stopped, partial="- 3 hosts"
    )
    assert localized_reply(agent_loop._budget_exhausted_text(violation, None), language) == notice(
        "budget_exhausted_no_answer", language, reason=reason
    )
    # An answer the budget cut: the answer as written, the notice translated.
    cut = "Here is what I found so far." + "\n\n" + agent_loop.BUDGET_EXHAUSTED_MESSAGE.format(
        reason=violation.user_message()
    )
    assert localized_reply(cut, language) == f"Here is what I found so far.\n\n{stopped}"
    # The cheaper-model line (the reasoning pane today; translated wherever it is delivered).
    downgrade = agent_loop.MODEL_DOWNGRADE_MESSAGE.format(to_model="gpt-mini", reason=violation.user_message())
    assert localized_reply(downgrade, language) == notice(
        "model_downgrade", language, to_model="gpt-mini", reason=reason
    )


def test_the_budget_cut_is_appended_after_a_blank_line():
    """What the trailing rule reads: ``_say_budget_cut`` adds a blank line, then the notice."""
    source = app_sources.source(app_sources.APP_DIR / "tools" / "agent_loop.py")
    assert 'note = f"\\n\\n{BUDGET_EXHAUSTED_MESSAGE.format(reason=' in source
    # Only a closing notice is translated: one inside the answer is the agent's prose.
    sentence = NOTICES["budget_exhausted"]["en"].format(reason="x")
    assert localized_reply(f"{sentence}\n\nMore words.", "fr") == f"{sentence}\n\nMore words."


def test_every_reply_notice_has_a_drift_guard():
    guarded = {case.split("/", 1)[0] for case in _service_texts()} | set(_INLINE_SOURCES)
    guarded |= {"assignment_failed", "assignment_completed"}
    guarded |= {"budget_exhausted", "budget_exhausted_no_answer", "model_downgrade"}
    assert set(REPLY_NOTICES) == guarded


def test_anything_else_goes_out_as_written():
    assert localized_reply("Here is your report.", "fr") == "Here is your report."
    assert localized_reply(NOTICES["quota_exceeded"]["en"], "en") == NOTICES["quota_exceeded"]["en"]
    assert localized_reply(NOTICES["quota_exceeded"]["en"] + " Thanks!", "fr").endswith("Thanks!")
    # The partial-results label is translated only after a notice of its own.
    composed = "My own words.\n\nPartial results:\n\nrows"
    assert localized_reply(composed, "fr") == composed
    assert localized_reply("", "fr") == ""


# --- The channel layer sends its notices through the catalog --------------------

#: Modules whose replies reach a person on a messaging channel.
_CHANNEL_MODULES = tuple(
    sorted(
        {
            *(p for p in app_sources.python_files(app_sources.APP_DIR / "channels")),
            app_sources.APP_DIR / "services" / "channel_continuation.py",
            app_sources.APP_DIR / "api" / "v1" / "chat_continuation.py",
        }
    )
)

#: Mirrors of a REST surface's own wording (``api/deps.py``): the drift test
#: below pins them, so the English lives in both places on purpose.
_MIRRORED = {"ai_temporarily_unavailable", "chat_unavailable_enterprise", "chat_unavailable_license"}


def _is_text(node: ast.AST | None) -> bool:
    """A sentence spelled in place: a non-empty string literal or an f-string."""
    if isinstance(node, ast.Constant):
        return isinstance(node.value, str) and bool(node.value.strip())
    return isinstance(node, ast.JoinedStr)


def _callee(call: ast.Call) -> str:
    func = call.func
    return func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else ""


def _spelled_notices(path) -> list[str]:
    found = []
    for node in app_sources.walk(app_sources.tree(path)):
        if not isinstance(node, ast.Call):
            continue
        name = _callee(node)
        if name in {"OutboundMessage", "_TurnRoute"}:
            text = next((k.value for k in node.keywords if k.arg == "text"), node.args[0] if node.args else None)
            if _is_text(text):
                found.append(f"{name}(text=...) at line {node.lineno}")
        if name == "JSONResponse" and node.args and isinstance(node.args[0], ast.Dict):
            for key, value in zip(node.args[0].keys, node.args[0].values, strict=True):
                if isinstance(key, ast.Constant) and key.value in {"text", "content"} and _is_text(value):
                    found.append(f"JSONResponse({{{key.value!r}: ...}}) at line {node.lineno}")
    return found


def test_no_channel_module_spells_a_notice_itself():
    offenders = {
        app_sources.relative(path): spelled
        for path in _CHANNEL_MODULES
        if (spelled := _spelled_notices(path))
    }
    assert not offenders, (
        "Send a notice to a channel through channel_notices.notice(key, notice_language(...)): "
        f"{offenders}"
    )


def _fragments(english: str) -> list[str]:
    """The fixed stretches of an English notice long enough to be unmistakable."""
    return [part for part in re.split(r"\{[a-z_]+\}", english) if len(part.strip()) >= 15]


#: Every module that sends a channel notice: the channel layer and the services
#: whose notices it posts (the web chat keeps its own wording).
_NOTICE_SENDERS = (
    *_CHANNEL_MODULES,
    app_sources.APP_DIR / "services" / "mention_dispatch.py",
    app_sources.APP_DIR / "services" / "sub_agent.py",
    app_sources.APP_DIR / "services" / "notify.py",
)


def test_a_channel_notice_has_no_english_copy_outside_the_catalog():
    owned = {
        key: texts
        for key, texts in NOTICES.items()
        if key not in REPLY_NOTICES and key not in _MIRRORED and key not in BUDGET_REASONS
    }
    copies = []
    for path in _NOTICE_SENDERS:
        literals = [
            node.value
            for node in app_sources.walk(app_sources.tree(path))
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        ]
        for key, texts in owned.items():
            for fragment in _fragments(texts[DEFAULT_LANGUAGE]):
                if any(fragment in literal for literal in literals):
                    copies.append(f"{app_sources.relative(path)}: {key}")
    assert not copies, f"a notice's English is spelled outside the catalog: {sorted(set(copies))}"


def test_the_mirrored_notices_match_their_rest_wording():
    from app.api import deps

    assert NOTICES["ai_temporarily_unavailable"]["en"] == deps.AI_TEMPORARILY_UNAVAILABLE_DETAIL
    assert NOTICES["chat_unavailable_enterprise"]["en"] == deps.CHAT_UNAVAILABLE_PREFIX + deps.ENTERPRISE_EDITION_REQUIRED_DETAIL
    assert NOTICES["chat_unavailable_license"]["en"] == deps.CHAT_UNAVAILABLE_PREFIX + deps.LICENSE_REQUIRED_DETAIL


def test_the_queue_notices_are_the_catalog_english():
    from app.services import channel_continuation

    assert notice("channel_queued") == channel_continuation.QUEUED_NOTICE
    assert notice("channel_queue_full") == channel_continuation.QUEUE_FULL_NOTICE
    assert notice("channel_turn_busy") == channel_continuation.TURN_BUSY_NOTICE
    for language in _TRANSLATED:
        assert localized_reply(channel_continuation.QUEUED_NOTICE, language) == channel_continuation.QUEUED_NOTICE


# --- What reaches the reader -----------------------------------------------------


def test_the_licence_refusal_is_in_the_reader_language(monkeypatch):
    from app.channels import router
    from app.channels.base import ChannelType, InboundMessage

    _platform_default(monkeypatch, "ja-jp")
    inbound = InboundMessage(
        channel=ChannelType.SLACK,
        channel_user_id="U1",
        channel_thread_id="D1:1",
        text="hi",
    )
    assert router._licence_refusal_outbound(inbound, "fr").text == channel_notices.chat_unavailable_notice("fr")
    assert router._licence_refusal_outbound(inbound).text == channel_notices.chat_unavailable_notice("ja")


def test_the_unreadable_state_reply_is_in_the_platform_default(monkeypatch):
    from app.channels import router
    from app.channels.base import ChannelType, InboundMessage

    _platform_default(monkeypatch, "es-es")
    inbound = InboundMessage(channel=ChannelType.WEB, channel_user_id="u", channel_thread_id="t", text="hi")
    assert router._unreadable_state_outbound(inbound).text == notice("ai_temporarily_unavailable", "es")


def test_the_continuation_opening_is_in_its_owner_language(monkeypatch):
    from app.api.v1.chat_continuation import _opening_text

    conv = SimpleNamespace(title="Q3 review", id="00000000-0000-0000-0000-000000000002")
    text = _opening_text(conv, "teams", "fr")
    assert text.startswith("**Q3 review** se poursuit ici depuis ")
    assert notice("continuation_where_teams", "fr") in text
    assert _opening_text(conv, "slack").startswith("**Q3 review** continues here from ")


def test_the_mention_notes_are_in_the_reader_language():
    from app.services.mention_dispatch import MentionTarget, _timeout_note

    targets = [MentionTarget(agent_id="a", name="Triage"), MentionTarget(agent_id="b", name="Intel", agent=object())]
    assert _timeout_note(targets, 0, "es") == (
        notice("mention_timeout", "es", name="Triage") + " " + notice("mention_not_asked", "es", names="Intel")
    )
    assert _timeout_note(targets, 0) == (
        "Triage did not answer within the time this turn allows its mentioned agents. Not asked: Intel."
    )


def test_the_background_task_notice_is_in_its_owner_language():
    from app.services.sub_agent import _format_completion_notice

    assert _format_completion_notice("Researcher", "", "ja") == (
        notice("task_completed", "ja", agent_name="Researcher") + "\n\n" + notice("task_no_output", "ja")
    )
    assert _format_completion_notice("Researcher", "").startswith("**Background task completed**")


# --- Link labels in an agent's answer (channels/base.py) -------------------------


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows


class _Session:
    def __init__(self, rows):
        self._rows = rows

    async def execute(self, _stmt):
        return _Rows(self._rows)


@pytest.mark.parametrize("language", LANGUAGES)
async def test_a_nameless_file_is_linked_in_the_reader_language(language):
    import uuid

    from app.channels.base import resolve_channel_file_markers

    file_id = uuid.uuid4()
    out = await resolve_channel_file_markers(
        _Session([SimpleNamespace(id=file_id, filename="")]),
        f"Here: [[FILE:{file_id}]]",
        "https://app.example.org",
        conversation_id=uuid.uuid4(),
        language=language,
    )
    assert out.startswith(f"Here: [{notice('link_file', language)}](https://app.example.org/api/v1/chat/files/")


@pytest.mark.parametrize("language", LANGUAGES)
@pytest.mark.parametrize(
    ("resolver", "marker", "key"),
    [
        ("resolve_channel_report_markers", "REPORT", "link_report"),
        ("resolve_channel_presentation_markers", "PRESENTATION", "link_presentation"),
        ("resolve_channel_video_markers", "VIDEO", "link_video"),
    ],
)
async def test_an_untitled_deck_or_report_is_linked_in_the_reader_language(language, resolver, marker, key):
    import uuid

    from app.channels import base

    row_id = uuid.uuid4()
    out = await getattr(base, resolver)(
        _Session([SimpleNamespace(id=row_id, title=None)]),
        f"See [[{marker}:{row_id}]]",
        "https://app.example.org",
        conversation_id=uuid.uuid4(),
        language=language,
    )
    assert out.startswith(f"See [{notice(key, language)}](https://app.example.org/")


def test_a_link_text_with_nothing_left_falls_back_to_the_platform_language(monkeypatch):
    from app.channels.base import sanitize_md_link_text

    _platform_default(monkeypatch, "es-es")
    assert sanitize_md_link_text("[]`") == notice("link_file", "es")
    assert sanitize_md_link_text("[]", "custom") == "custom"
    assert sanitize_md_link_text("report[1].pdf") == "report1.pdf"


def test_the_link_builders_spell_no_label_themselves():
    builders = {
        "sanitize_md_link_text",
        "resolve_channel_file_markers",
        "_resolve_channel_artifact_markers",
        "resolve_channel_presentation_markers",
        "resolve_channel_report_markers",
        "resolve_channel_video_markers",
    }
    labels = {NOTICES[key][DEFAULT_LANGUAGE] for key in ("link_file", "link_presentation", "link_report", "link_video")}
    tree = app_sources.tree(app_sources.APP_DIR / "channels" / "base.py")
    spelled = [
        f"{fn.name}: {node.value!r}"
        for fn in ast.walk(tree)
        if isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef) and fn.name in builders
        for node in ast.walk(fn)
        if isinstance(node, ast.Constant) and node.value in labels
    ]
    assert not spelled, f"a link label comes from channel_notices: {spelled}"
