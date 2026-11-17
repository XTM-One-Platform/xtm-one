import pytest
from pydantic import ValidationError

from app.api.v1.schemas.agent import UpdateAgentRequest
from app.api.v1.schemas.prompt import PromptUpdate
from app.api.v1.schemas.versioning import (
    DiffLineResponse,
    RollbackRequest,
    VersionDetailResponse,
    VersionDiffResponse,
    VersionListItemResponse,
    VersionListResponse,
)


def test_prompt_update_accepts_commit_message():
    payload = PromptUpdate(content="body", commit_message="msg")
    assert payload.commit_message == "msg"


def test_update_agent_request_accepts_commit_message():
    payload = UpdateAgentRequest(persona="persona", commit_message="agent msg")
    assert payload.commit_message == "agent msg"


def test_commit_message_max_length_validation():
    too_long = "x" * 501

    with pytest.raises(ValidationError):
        PromptUpdate(commit_message=too_long)

    with pytest.raises(ValidationError):
        UpdateAgentRequest(commit_message=too_long)

    with pytest.raises(ValidationError):
        RollbackRequest(commit_message=too_long)


def test_update_agent_request_temperature_reset_to_null_is_tracked_in_model_fields_set():
    """Regression test: when the UI clicks "Reset to default"
    on a numeric tracked field like temperature, it sends ``{"temperature": null}``.

    The update endpoint must distinguish "explicit null" from "field not provided"
    so the agent value gets cleared AND a config version snapshot is written for
    deterministic replay. That distinction relies on Pydantic's
    ``model_fields_set`` containing ``"temperature"`` only when the key is present
    in the payload, even with a ``null`` value.
    """
    explicit_null = UpdateAgentRequest(temperature=None)
    not_provided = UpdateAgentRequest()

    assert "temperature" in explicit_null.model_fields_set
    assert "temperature" not in not_provided.model_fields_set

    # Same contract for the other nullable tracked config fields (already wired up
    # via ``model_fields_set`` in the endpoint; pinned here to prevent regressions).
    for field in ("reasoning_effort", "max_iterations"):
        explicit_null = UpdateAgentRequest(**{field: None})
        not_provided = UpdateAgentRequest()

        assert field in explicit_null.model_fields_set
        assert field not in not_provided.model_fields_set


def test_versioning_response_schemas_shape():
    item = VersionListItemResponse(
        id="id-1",
        version_number=2,
        author_id="u-1",
        author_name="Alice",
        commit_message="commit",
        created_at="2026-05-09T00:00:00Z",
    )
    detail = VersionDetailResponse(**item.model_dump(), content="hello")
    diff = VersionDiffResponse(
        from_version=1,
        to_version=2,
        added=1,
        removed=1,
        lines=[
            DiffLineResponse(
                type="add",
                content="line",
                old_line=None,
                new_line=2,
            )
        ],
        unified_diff="--- v1\n+++ v2",
    )
    listing = VersionListResponse(items=[item], total=1)
    rollback = RollbackRequest(commit_message="rollback")

    assert detail.content == "hello"
    assert diff.to_version == 2
    assert listing.total == 1
    assert rollback.commit_message == "rollback"
