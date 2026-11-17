"""A gated (skipped) run must still surface the prompt it sent to the LLM.

The run-detail endpoint rebuilds the "Full Rendered Prompt" panel from the
stored ``AssignmentRun.messages`` (first ``user``-role message). When the
activation gate answers ``NO`` / ``SKIP`` the engine finalizes and returns
*before* the main agentic loop runs, so ``run.messages`` would stay ``NULL``
and the panel would read "No rendered prompt available for this run." - even
though the activation-gate prompt WAS evaluated by the LLM for that run.

The fix persists the activation-gate exchange (rendered activation prompt +
gate answer) to ``run.messages`` in the skip branch so the prompt that was
actually sent to the LLM is visible, explaining *why* the run was gated.

Pinned via source inspection (same convention as
``test_activation_gate_trigger_context.py``) because the full execution path is
too heavy to drive in a unit test.
"""

from __future__ import annotations

import inspect

from app.services import assignment_engine


def _skip_branch_source() -> str:
    source = inspect.getsource(assignment_engine.execute_assignment_run)
    skip_idx = source.index('answer.startswith("NO")')
    # Bound the branch on the comment that immediately follows the gate block
    # (the start of the main-prompt rendering). Searching for the bare token
    # ``return`` is unsafe here because the f-string above it contains the
    # substring "returned".
    end_idx = source.index("# Render the main prompt", skip_idx)
    return source[skip_idx:end_idx]


def test_skipped_run_persists_activation_messages():
    """The skip branch must store the activation-gate exchange on the run."""
    branch = _skip_branch_source()
    assert "run.messages = [" in branch, (
        "A gated run must persist run.messages so the 'Full Rendered Prompt' "
        "panel isn't empty for skipped runs."
    )


def test_skipped_run_stores_rendered_activation_as_prompt():
    """The persisted user message must be the rendered activation prompt.

    The run-detail endpoint derives ``rendered_prompt`` from the first
    ``user``-role message, so the gate prompt that was sent to the LLM must be
    stored under the ``user`` role.
    """
    branch = _skip_branch_source()
    assert '"role": "user", "content": rendered_activation' in branch, (
        "The skipped run's first user message must be the rendered activation "
        "prompt (what was actually sent to the gate LLM)."
    )
    assert '"role": "assistant", "content": answer' in branch, (
        "The gate's YES/NO/SKIP answer should be stored as the assistant turn "
        "so it surfaces as an agentic step."
    )


def test_skip_branch_persists_before_commit():
    """run.messages must be assigned before the commit that ends the run."""
    branch = _skip_branch_source()
    messages_idx = branch.index("run.messages = [")
    commit_idx = branch.index("await db.commit()")
    assert messages_idx < commit_idx, (
        "run.messages must be set before db.commit() so the activation prompt "
        "is durably persisted for the skipped run."
    )


def test_skip_branch_also_handles_skip_answer():
    """The skip branch must treat ``SKIP`` as a skip condition, not only ``NO``.

    The gate's system prompt advertises YES/NO/SKIP and the engine skips on
    ``answer.startswith("NO") or answer.startswith("SKIP")``. The other tests
    here anchor on the ``NO`` predicate, so pin the ``SKIP`` predicate too -
    otherwise a future refactor could drop ``SKIP`` handling (regressing the
    skip-and-persist behaviour for SKIP answers) while still passing them.
    """
    branch = _skip_branch_source()
    assert 'answer.startswith("SKIP")' in branch, (
        "The activation-gate skip branch must also treat a 'SKIP' answer as a "
        "skip condition (gate answers are YES/NO/SKIP), not only 'NO'."
    )
