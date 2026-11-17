"""Template-based assignment creation must wire timeout_seconds and max_iterations.

Regression guard for the class of timeout bugs where assignments created from
agent templates (e.g. Social Media Manager "Write LinkedIn Caption Templates for
Content Library") run indefinitely because the template-load path in
``create_agent`` was not forwarding ``max_iterations`` / ``timeout_seconds`` to
the ``Assignment`` constructor — unlike the import path which already did.
"""

import pathlib

from app.agents.templates import BUILTIN_TEMPLATES

# ---------------------------------------------------------------------------
# Source-level contract: the create_agent template-load path must wire
# max_iterations and timeout_seconds from wf_data, matching the import path.
# We read the source file directly to avoid triggering database initialisation
# on module import, following the pattern used by existing source-contract
# tests in this repository.
# ---------------------------------------------------------------------------

_AGENTS_SOURCE = (
    pathlib.Path(__file__).parent.parent / "app" / "api" / "v1" / "agents.py"
).read_text(encoding="utf-8")

# Slice down to the create_agent function body: the import_agent path also
# reads both fields from wf_data, so asserting against the whole module would
# keep passing even if the template-load path regressed again.
_CREATE_AGENT_START = _AGENTS_SOURCE.index("async def create_agent(")
_CREATE_AGENT_END = _AGENTS_SOURCE.index("\n@router.", _CREATE_AGENT_START)
_CREATE_AGENT_SOURCE = _AGENTS_SOURCE[_CREATE_AGENT_START:_CREATE_AGENT_END]


def test_create_agent_template_path_wires_max_iterations():
    """create_agent must forward max_iterations from template wf_data."""
    assert 'wf_data.get("max_iterations")' in _CREATE_AGENT_SOURCE, (
        "Template-based assignment creation must read max_iterations from "
        "wf_data — without it a template that caps iterations (e.g. Social "
        "Media Manager) is ignored and the run inherits an unconstrained "
        "agent/platform value, reproducing the timeout."
    )


def test_create_agent_template_path_wires_timeout_seconds():
    """create_agent must forward timeout_seconds from template wf_data."""
    assert 'wf_data.get("timeout_seconds"' in _CREATE_AGENT_SOURCE, (
        "Template-based assignment creation must read timeout_seconds from "
        "wf_data — without it the default 300 s floor is always used and "
        "any tighter template-configured budget is silently discarded."
    )


# ---------------------------------------------------------------------------
# Social Media Manager template: the LinkedIn Caption Templates assignment
# must carry explicit, tight iteration / time bounds to prevent the runaway
# loop that produced the 700 s / 35-iteration timeout.
# ---------------------------------------------------------------------------


def _social_media_template():
    matches = [t for t in BUILTIN_TEMPLATES if t.get("slug") == "social-media-manager"]
    assert matches, "social-media-manager template not found in BUILTIN_TEMPLATES"
    return matches[0]


def _linkedin_caption_assignment():
    tmpl = _social_media_template()
    assignments = tmpl.get("default_assignments", [])
    matches = [
        a for a in assignments
        if a.get("name") == "Write LinkedIn Caption Templates for Content Library"
    ]
    assert matches, (
        "social-media-manager template is missing the "
        "'Write LinkedIn Caption Templates for Content Library' assignment"
    )
    return matches[0]


def test_linkedin_caption_assignment_exists_in_social_media_template():
    """The assignment must be present as a default in the social-media-manager template."""
    _linkedin_caption_assignment()  # raises AssertionError if missing


def test_linkedin_caption_assignment_has_max_iterations():
    """The assignment must have an explicit max_iterations to cap the tool loop."""
    assignment = _linkedin_caption_assignment()
    assert assignment.get("max_iterations") is not None, (
        "'Write LinkedIn Caption Templates for Content Library' must declare "
        "max_iterations so the template-load path can set a tight bound."
    )


def test_linkedin_caption_assignment_max_iterations_is_tight():
    """max_iterations must be <= 15 — enough for a KB search + 8 stores + summary."""
    assignment = _linkedin_caption_assignment()
    mi = assignment["max_iterations"]
    assert mi <= 15, (
        f"max_iterations={mi} is too large for a bounded content-generation task; "
        "10 is enough (1 KB search + 8 store_knowledge calls + 1 final summary)."
    )


def test_linkedin_caption_assignment_has_timeout_seconds():
    """The assignment must carry an explicit timeout_seconds."""
    assignment = _linkedin_caption_assignment()
    assert "timeout_seconds" in assignment, (
        "'Write LinkedIn Caption Templates for Content Library' must declare "
        "timeout_seconds so the template-load path stores a concrete budget."
    )


def test_linkedin_caption_assignment_timeout_is_bounded():
    """timeout_seconds must be <= 600 s for a simple content-generation task."""
    assignment = _linkedin_caption_assignment()
    ts = assignment["timeout_seconds"]
    assert ts <= 600, (
        f"timeout_seconds={ts} is too large for this assignment; 300 s is "
        "sufficient and prevents the 700 s runaway seen in production."
    )


def test_linkedin_caption_assignment_inherits_kb_write_access():
    """The assignment must rely on inherited KB write access, not legacy flags.

    Runtime write access is resolved by ``resolve_assignment_kb_access``:
    an unset ``write_knowledge_base_ids`` (NULL) inherits ALL of the agent's
    writable KBs, while ``[]`` is an explicit opt-out. The deprecated
    ``write_knowledge_base_enabled`` flag is no longer consulted at run time,
    so built-in templates must not reintroduce it.
    """
    assignment = _linkedin_caption_assignment()
    assert assignment.get("write_knowledge_base_ids") != [], (
        "'Write LinkedIn Caption Templates for Content Library' must not opt "
        "out of KB writes (write_knowledge_base_ids=[]) — the task needs the "
        "store_knowledge tool, inherited from the agent's KB write permissions."
    )
    assert "write_knowledge_base_enabled" not in assignment, (
        "write_knowledge_base_enabled is deprecated and ignored at run time — "
        "do not reintroduce it in built-in templates; write access is derived "
        "from the agent's knowledge_base_permissions via "
        "write_knowledge_base_ids inheritance."
    )


def test_linkedin_caption_assignment_is_manual_trigger():
    """The assignment should be manually triggered (on-demand content generation)."""
    assignment = _linkedin_caption_assignment()
    assert assignment.get("trigger_type") == "manual", (
        "Caption template generation is an on-demand task — trigger_type "
        "must be 'manual', not a schedule that runs unattended."
    )


def test_linkedin_caption_prompt_has_stop_condition():
    """The prompt must include an explicit stop directive to prevent runaway loops."""
    assignment = _linkedin_caption_assignment()
    prompt = assignment.get("prompt", "")
    assert "Stop after storing" in prompt, (
        "The prompt for 'Write LinkedIn Caption Templates for Content Library' "
        "must contain the explicit stop directive ('Stop after storing all "
        "selected templates') to prevent the agent from continuing to generate "
        "content indefinitely and exhausting its iteration budget."
    )


def test_linkedin_caption_prompt_targets_kb_explicitly_when_multiple():
    """The prompt must direct explicit KB targeting for multi-KB agents.

    ``store_knowledge`` silently defaults to the FIRST writable KB when the
    optional ``knowledge_base`` argument is omitted (see
    ``build_kb_store_tool`` in ``services/builtin_tools/kb.py``). An agent
    with several writable KBs would otherwise pollute whichever KB happens to
    be listed first, so the prompt must instruct the agent to pass the
    argument explicitly whenever more than one writable KB exists.
    """
    assignment = _linkedin_caption_assignment()
    prompt = assignment.get("prompt", "")
    assert "`knowledge_base` argument" in prompt, (
        "The prompt for 'Write LinkedIn Caption Templates for Content Library' "
        "must instruct the agent to pass the `knowledge_base` argument to "
        "store_knowledge when multiple writable knowledge bases are available "
        "— the tool's implicit default (first writable KB) can silently write "
        "templates into an unrelated knowledge base."
    )
