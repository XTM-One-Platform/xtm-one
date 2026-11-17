"""Fill the organization profile from a connected integration with an agent (#11888, PR 2)."""

from __future__ import annotations

from app.services import organization_context_fill_agent as fill


def test_intune_preview_prompt_uses_the_server_mapping_and_never_applies():
    prompt = fill.fill_prompt("intune", "Contoso Intune", apply=False)
    assert "intune_list_devices" in prompt
    assert "rows_format intune_device" in prompt
    assert "apply false" in prompt
    assert "apply true" not in prompt
    assert "Apply only when I say so" in prompt
    # #11994: the person is told the context is shared before anything is applied.
    assert prompt.startswith(fill.SHARED_CONTEXT_PROMPT)
    assert "tell me first that the organization context is shared" in prompt


def test_scheduled_prompt_applies_and_removes_retired_devices():
    prompt = fill.fill_prompt("intune", "Contoso Intune", apply=True)
    assert "apply true" in prompt
    assert "remove_organization_rows" in prompt
    assert "source intune" in prompt
    assert prompt.startswith(fill.SHARED_CONTEXT_PROMPT)


def test_another_platform_maps_rows_itself_with_its_own_id():
    prompt = fill.fill_prompt("crowdstrike", "Falcon", apply=False)
    assert "rows_format profile" in prompt
    assert "external_id" in prompt
    assert "source crowdstrike" in prompt
    assert "intune" not in prompt


def test_the_tag_names_the_integration():
    assert fill.fill_agent_tag("abc") == "org-fill:abc"
    assert fill.DEFAULT_SYNC_CRON == "0 6 * * *"
