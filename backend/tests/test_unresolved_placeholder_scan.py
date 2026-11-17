"""Which text an assignment run scans for unresolved ``{{...}}`` placeholders.

Only author-written templates are scanned, each under its own name: the
persona, guardrails, attached prompts and pinned skills of the system prompt,
and the assignment and activation prompts before their context is rendered.
Memory, identity and platform sections, and the trigger values a template
renders, are data that may quote ``{{...}}`` verbatim. The same finding is
logged once an hour per assignment and section, not on every scheduled run.
"""

from __future__ import annotations

import logging
import uuid
from unittest.mock import AsyncMock

import pytest

from app.models.agent import Agent
from app.models.assignment import Assignment
from app.services import prompt_budget, variable_resolver
from app.services.assignment_helpers import (
    _build_system_message,
    _warn_unresolved_placeholders,
)

_LOGGER = "app.services.assignment_helpers"
_RENDERED_ROOTS = ("trigger", "agent", "elements")


@pytest.fixture(autouse=True)
def _clean_debounce():
    prompt_budget.reset_warning_state()
    yield
    prompt_budget.reset_warning_state()


def _warnings(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]


async def _warn(
    text: str, *, where: str = "assignment prompt", assignment_id=None, resolved_as=None, **kwargs
):
    await _warn_unresolved_placeholders(
        object(),
        text,
        where=where,
        agent_name="QA Browser",
        assignment_name="Watcher TNR",
        assignment_id=assignment_id or uuid.UUID(int=1),
        resolved_as=resolved_as,
        **kwargs,
    )


# -- The sections _build_system_message hands over for the scan --------------


async def test_only_the_author_written_sections_are_handed_over_for_the_scan():
    hidden = str(uuid.uuid4())
    agent = Agent(
        name="QA Browser",
        persona=f"Log in with {{{{var_id.{hidden}}}}} on board {{{{BOARD}}}}.",
        guardrails="Never touch {{PROD}}.",
    )
    assignment = Assignment(
        agent_id=uuid.uuid4(),
        name="Watcher TNR",
        trigger_type="schedule",
        trigger_config={},
        prompt="Watch the runs.",
    )
    sections: list[tuple[str, str]] = []

    prompt = await _build_system_message(
        None,
        agent,
        assignment,
        None,
        "The user wrote {{USER_NOTE}}.",
        "Run manifest: the password is {{var_id}}.",
        {},
        variables={},
        templated_sections=sections,
    )

    assert sections == [
        ("persona", f"Log in with {{{{var_id.{hidden}}}}} on board {{{{BOARD}}}}."),
        ("guardrails", "\n\nGuardrails:\nNever touch {{PROD}}."),
    ]
    # The memory is in the prompt the LLM gets, but not in what is scanned.
    assert "{{var_id}}" in prompt and "{{USER_NOTE}}" in prompt


async def test_a_caller_that_asks_for_no_sections_gets_the_same_prompt():
    agent = Agent(name="QA Browser", persona="Board {{BOARD}}.")
    assignment = Assignment(
        agent_id=uuid.uuid4(), name="Watcher TNR", trigger_type="schedule", trigger_config={}, prompt="x"
    )
    args = (None, agent, assignment, None, "", "", {})

    sections: list[tuple[str, str]] = []
    with_sections = await _build_system_message(*args, templated_sections=sections)

    assert with_sections == await _build_system_message(*args)
    assert sections == [("persona", "Board {{BOARD}}.")]


# -- The warning ---------------------------------------------------------------


async def test_a_rendered_template_reports_only_what_its_context_does_not_render(caplog):
    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        await _warn(
            "Summarize {{trigger.note}} for {{ agent.name }} in {{elements.teams_channels}}, "
            "then open {{FOO}} with {{var_id}} and {{ triggers.x }}",
            rendered_roots=_RENDERED_ROOTS,
        )

    (warning,) = _warnings(caplog)
    assert warning.startswith(
        "Unresolved placeholder(s) in assignment prompt for agent 'QA Browser' / "
        "assignment 'Watcher TNR': {{FOO}}, {{var_id}}, {{triggers.x}} - "
    )


async def test_a_section_rendered_by_nothing_reports_every_placeholder(caplog):
    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        await _warn("Use {{trigger.note}} and {{FOO}}", where="persona")

    (warning,) = _warnings(caplog)
    assert "in persona for agent" in warning
    assert ": {{trigger.note}}, {{FOO}} - " in warning


@pytest.mark.parametrize("text", ["", "All resolved.", "{{trigger.note}} only"])
async def test_nothing_unresolved_logs_nothing(caplog, text):
    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        await _warn(text, rendered_roots=_RENDERED_ROOTS)

    assert _warnings(caplog) == []


async def test_the_same_finding_is_logged_once_per_assignment_and_section(monkeypatch, caplog):
    deleted = str(uuid.uuid4())
    lookup = AsyncMock(return_value={deleted})
    monkeypatch.setattr(variable_resolver, "missing_variable_ids", lookup)
    first, second = uuid.uuid4(), uuid.uuid4()

    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        for _ in range(3):
            await _warn(f"{{{{FOO}}}} on {{{{var_id.{deleted}}}}}", assignment_id=first)
    assert len(_warnings(caplog)) == 2  # one per kind, on the first run only
    assert lookup.await_count == 1  # the debounced runs do not query the variables

    caplog.clear()
    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        await _warn("{{FOO}}", assignment_id=first, where="activation prompt")
        await _warn("{{FOO}}", assignment_id=second)
        await _warn("{{BAR}}", assignment_id=first)
        await _warn("{{BAR}}", assignment_id=first)
    warnings = _warnings(caplog)
    assert len(warnings) == 3
    assert "in activation prompt" in warnings[0]
    assert ": {{BAR}} - " in warnings[2]


async def test_another_identity_is_told_about_variables_again_but_not_about_typos(
    monkeypatch, caplog
):
    hidden = str(uuid.uuid4())
    monkeypatch.setattr(variable_resolver, "missing_variable_ids", AsyncMock(return_value=set()))
    text = f"{{{{FOO}}}} on {{{{var_id.{hidden}}}}}"
    first_user, second_user = uuid.uuid4(), uuid.uuid4()

    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        await _warn(text, resolved_as=first_user)
        await _warn(text, resolved_as=second_user)

    first_variables, typos, second_variables = _warnings(caplog)
    assert f"resolved as user {first_user}" in first_variables
    assert "{{FOO}}" in typos
    assert f"resolved as user {second_user}" in second_variables


async def test_the_same_finding_is_logged_again_after_the_interval(monkeypatch, caplog):
    now = 1000.0
    monkeypatch.setattr(prompt_budget.time, "monotonic", lambda: now)

    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        await _warn("{{FOO}}")
        await _warn("{{FOO}}")
        now += 3601
        await _warn("{{FOO}}")

    assert len(_warnings(caplog)) == 2


# -- An assignment run ---------------------------------------------------------


def _run_objects(assignment_id: uuid.UUID | None = None):
    from tests.test_assignment_stale_reference import _engine_objects

    run, assignment, agent = _engine_objects()
    if assignment_id is not None:
        assignment.id = run.assignment_id = assignment_id
    agent.persona = "Open board {{BOARD}}."
    assignment.prompt = "Summarize {{trigger.note}} on {{FOO}}."
    run.trigger_payload = {"note": "the manifest says {{var_id}}"}
    return run, assignment, agent


async def test_a_scheduled_run_names_each_section_and_not_its_memory_or_trigger_data(
    monkeypatch, caplog
):
    from tests.agentic_gate_stub import stub_agentic_gate
    from tests.test_assignment_stale_reference import _drive_run

    stub_agentic_gate(monkeypatch)
    built: list[str] = []

    async def _build_without_db(_db, agent, assignment, _user_id, *args, **kwargs):
        prompt = await _build_system_message(None, agent, assignment, None, *args, **kwargs)
        built.append(prompt)
        return prompt

    patches = {
        "app.services.assignment_engine._build_system_message": _build_without_db,
        "app.services.assignment_engine._load_memory_context": AsyncMock(
            return_value=("", "Run manifest: the password is {{var_id}}.")
        ),
    }
    run, assignment, agent = _run_objects()

    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        await _drive_run(
            monkeypatch,
            report_integration_id=None,
            engine_objects=(run, assignment, agent),
            patches=patches,
        )

    assert run.status == "completed", run.error
    assert "{{var_id}}" in built[0]  # the memory reached the prompt
    placeholder_warnings = [w for w in _warnings(caplog) if "Unresolved" in w]
    assert sorted(placeholder_warnings) == sorted(
        [
            "Unresolved placeholder(s) in persona for agent 'OCTI SaaS Alerts -> Notion Sync' / "
            "assignment 'Sync Excel -> Notion prod-tracker (hourly)': {{BOARD}} - these are "
            "sent verbatim to the LLM and will likely cause tool errors. Use {{var_id.UUID}} "
            "for platform variables or {{trigger.*}} for trigger data.",
            "Unresolved placeholder(s) in assignment prompt for agent 'OCTI SaaS Alerts -> "
            "Notion Sync' / assignment 'Sync Excel -> Notion prod-tracker (hourly)': {{FOO}} - "
            "these are sent verbatim to the LLM and will likely cause tool errors. Use "
            "{{var_id.UUID}} for platform variables or {{trigger.*}} for trigger data.",
        ]
    )

    # The next scheduled run of the same assignment repeats none of it.
    caplog.clear()
    next_run, next_assignment, next_agent = _run_objects(assignment.id)
    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        await _drive_run(
            monkeypatch,
            report_integration_id=None,
            engine_objects=(next_run, next_assignment, next_agent),
            patches=patches,
        )

    assert next_run.status == "completed", next_run.error
    assert [w for w in _warnings(caplog) if "Unresolved" in w] == []
