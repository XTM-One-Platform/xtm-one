"""Regression tests for versioned-prompt fragment building + variable
substitution on the evaluation-replay path.

Covers two correctness issues:

1. ``_build_versioned_prompts_fragment`` (in
   ``app.services.evaluation_engine``) must include the prompt
   description in the header so versioned evaluation replay produces
   the same structural system prompt that the live
   ``builtin_tools.load_agent_prompts`` path emits.  An earlier
   implementation only emitted ``### {name}`` and dropped the
   description entirely, which silently diverged the LLM input
   between non-versioned and versioned executions for any prompt
   whose description carries instructions.

2. The override ``prompt_fragment`` injected into
   ``app.services.agent_chat`` from
   ``resolve_run_snapshot_overrides`` must go through
   ``substitute_variables`` before being appended to the system
   prompt — otherwise any ``{{variables}}`` literally stored in the
   snapshotted prompt content stay unresolved and reach the LLM as
   placeholders.
"""

from __future__ import annotations

import pytest

from app.services.evaluation_engine import _build_versioned_prompts_fragment
from app.services.variable_resolver import substitute_variables


class TestBuildVersionedPromptsFragment:
    def test_empty_snapshots_returns_empty_string(self):
        assert _build_versioned_prompts_fragment([]) == ""

    def test_single_snapshot_with_description_renders_both_in_header(self):
        """The live ``load_agent_prompts`` path emits
        ``### {name}\\n{description}\\n{content}``.  The versioned
        fragment must match the same layout exactly.
        """
        fragment = _build_versioned_prompts_fragment(
            [("My Prompt", "Short description.", "Prompt body here.")]
        )

        assert "── Attached Prompts ──" in fragment
        assert "### My Prompt" in fragment
        assert "Short description." in fragment
        assert "Prompt body here." in fragment

        # The description MUST appear between the name and the body
        # so the LLM sees the instructions in the expected order.
        name_idx = fragment.index("### My Prompt")
        desc_idx = fragment.index("Short description.")
        body_idx = fragment.index("Prompt body here.")
        assert name_idx < desc_idx < body_idx

    def test_snapshot_with_no_description_omits_the_blank_line(self):
        """When the prompt has no description, the header MUST NOT emit
        a stray blank line — that would alter token positioning and
        diverge from the live path which also short-circuits the
        description line when empty.
        """
        fragment = _build_versioned_prompts_fragment(
            [("Bare Prompt", None, "Body without preamble.")]
        )

        assert "### Bare Prompt" in fragment
        assert "Body without preamble." in fragment

        # The header line should be exactly "### Bare Prompt" with no
        # trailing description-line — ``join("\n")`` of
        # ``["### Bare Prompt", "Body without preamble."]`` would
        # produce exactly one ``\n`` between them.
        assert "### Bare Prompt\nBody without preamble." in fragment

    def test_snapshot_with_empty_string_description_omits_description(self):
        """An empty-string description (vs. ``None``) must also be
        skipped — the live ``load_agent_prompts`` path guards with
        ``if prompt.description:`` which is falsy for both ``None``
        and ``""``.
        """
        fragment = _build_versioned_prompts_fragment(
            [("Empty Desc Prompt", "", "Body.")]
        )

        assert "### Empty Desc Prompt\nBody." in fragment

    def test_multiple_snapshots_render_in_listed_order(self):
        """Snapshot order MUST be preserved so the LLM sees the same
        prompt sequence between versioned and non-versioned runs.
        """
        fragment = _build_versioned_prompts_fragment(
            [
                ("First", "First description.", "First body."),
                ("Second", None, "Second body."),
                ("Third", "Third description.", "Third body."),
            ]
        )

        first_idx = fragment.index("### First")
        second_idx = fragment.index("### Second")
        third_idx = fragment.index("### Third")
        assert first_idx < second_idx < third_idx
        assert "First description." in fragment
        assert "Third description." in fragment


class TestOverridePromptFragmentSubstitutesVariables:
    """``agent_chat`` injects ``evaluation_overrides['prompt_fragment']``
    into ``system_parts`` when an evaluation run carries a versioned
    prompt snapshot.  That fragment can contain ``{{var_id.UUID}}``
    literals that the snapshotted prompt content preserved verbatim.
    Without an explicit ``substitute_variables`` call on the override
    path, the LLM would see the raw placeholders.

    These tests exercise the substitution semantics directly against
    ``substitute_variables`` — the same helper the live
    ``load_agent_prompts`` and the override path now both use — so a
    future refactor that strips the substitution from either path
    fails loudly.

    Note: the substitution helper recognises ``{{var_id.<UUID>}}``
    placeholders only (see ``variable_resolver.VAR_ID_PATTERN``);
    arbitrary ``{{name}}`` style templates are intentionally NOT
    a supported syntax because all variable references go through
    a canonical-UUID indirection so they survive rename/clone.
    """

    _UUID_A = "11111111-1111-4111-8111-111111111111"
    _UUID_B = "22222222-2222-4222-8222-222222222222"
    _UUID_UNKNOWN = "33333333-3333-4333-8333-333333333333"

    def test_substitutes_known_placeholder(self):
        out = substitute_variables(
            f"Today's user is {{{{var_id.{self._UUID_A}}}}}.",
            {self._UUID_A: "Alice"},
        )
        assert out == "Today's user is Alice."

    def test_leaves_unknown_placeholder_intact(self):
        """Unknown placeholders are left as-is (matches the live
        ``load_agent_prompts`` behaviour).  This is intentional — a
        prompt that references a not-yet-bound variable should not
        silently lose the placeholder.
        """
        template = (
            f"Hello {{{{var_id.{self._UUID_A}}}}}"
            f" and {{{{var_id.{self._UUID_UNKNOWN}}}}}."
        )
        out = substitute_variables(template, {self._UUID_A: "Bob"})
        assert "Bob" in out
        assert f"{{{{var_id.{self._UUID_UNKNOWN}}}}}" in out

    def test_handles_empty_variables_mapping(self):
        """``substitute_variables`` MUST tolerate an empty mapping —
        the override path passes ``_variables`` which may be empty
        when an evaluation run carries no user/agent context.  In
        that case the helper short-circuits and returns the input
        verbatim (it does NOT substitute against an empty map).
        """
        out = substitute_variables(
            f"Hello {{{{var_id.{self._UUID_A}}}}}.",
            {},
        )
        assert out == f"Hello {{{{var_id.{self._UUID_A}}}}}."

    def test_repeated_placeholder_is_replaced_at_every_occurrence(self):
        """Two references to the same ``var_id`` MUST both be
        substituted — a regression that only substitutes the first
        match would leak placeholder syntax into the LLM input.
        """
        template = (
            f"{{{{var_id.{self._UUID_A}}}}}-{{{{var_id.{self._UUID_A}}}}}"
        )
        out = substitute_variables(template, {self._UUID_A: "x"})
        assert out == "x-x"

    def test_multiple_distinct_placeholders_resolve_independently(self):
        template = (
            f"{{{{var_id.{self._UUID_A}}}}}-{{{{var_id.{self._UUID_B}}}}}"
        )
        out = substitute_variables(
            template,
            {self._UUID_A: "1", self._UUID_B: "2"},
        )
        assert out == "1-2"

    @pytest.mark.parametrize(
        "text",
        [
            "nothing-to-replace",
            "",  # empty input round-trips to empty
        ],
    )
    def test_text_without_placeholders_round_trips(self, text):
        out = substitute_variables(text, {"unused": "value"})
        assert out == text
