"""Tests for cron expression validation in assignment create/update schemas.

Regression coverage for the scheduler crashing on a malformed cron expression.
"""

import pytest
from pydantic import ValidationError

from app.api.v1.schemas.assignment import (
    CreateAssignmentRequest,
    UpdateAssignmentRequest,
)


def _payload(**overrides) -> dict:
    base = {
        "agent_id": "00000000-0000-0000-0000-000000000001",
        "name": "Scheduled run",
        "prompt": "Do the thing.",
        "trigger_type": "schedule",
        "trigger_config": {"cron": "0 9 * * MON-FRI"},
    }
    base.update(overrides)
    return base


def test_create_assignment_accepts_valid_cron():
    req = CreateAssignmentRequest(**_payload())
    assert req.trigger_config["cron"] == "0 9 * * MON-FRI"


def test_create_assignment_accepts_seconds_field_cron():
    # croniter supports 5, 6, and 7-column cron expressions; the 6-column
    # form starts with a `seconds` field.
    req = CreateAssignmentRequest(
        **_payload(trigger_config={"cron": "30 0 9 * * 1"})
    )
    assert req.trigger_config["cron"] == "30 0 9 * * 1"


def test_create_assignment_rejects_invalid_cron_columns():
    with pytest.raises(ValidationError) as exc_info:
        CreateAssignmentRequest(**_payload(trigger_config={"cron": "0 9"}))
    msg = str(exc_info.value)
    assert "Invalid cron expression" in msg


def test_create_assignment_rejects_missing_cron_for_schedule():
    with pytest.raises(ValidationError) as exc_info:
        CreateAssignmentRequest(**_payload(trigger_config={}))
    msg = str(exc_info.value)
    assert "Schedule trigger requires a non-empty 'cron'" in msg


def test_create_assignment_rejects_empty_cron_string():
    with pytest.raises(ValidationError):
        CreateAssignmentRequest(**_payload(trigger_config={"cron": "   "}))


def test_create_assignment_skips_validation_for_non_schedule_trigger():
    req = CreateAssignmentRequest(
        **_payload(trigger_type="webhook", trigger_config={"path": "github"})
    )
    assert req.trigger_type == "webhook"


def test_update_assignment_validates_cron_when_setting_schedule_trigger():
    with pytest.raises(ValidationError):
        UpdateAssignmentRequest(
            trigger_type="schedule",
            trigger_config={"cron": "invalid"},
        )


def test_update_assignment_accepts_partial_update_without_trigger_fields():
    req = UpdateAssignmentRequest(name="renamed")
    assert req.name == "renamed"
    assert req.trigger_type is None


def test_update_assignment_requires_trigger_config_when_setting_schedule():
    # Reverse of the previous "partial update is OK" assumption: if the
    # caller explicitly switches the trigger to "schedule", they MUST
    # also send a trigger_config with a valid cron — otherwise the
    # assignment would land in the DB with a non-schedule
    # trigger_config and the scheduler would crash on the next tick.
    with pytest.raises(ValidationError) as exc_info:
        UpdateAssignmentRequest(trigger_type="schedule")
    msg = str(exc_info.value)
    assert "trigger_config" in msg


def test_update_assignment_partial_update_without_trigger_type_is_ok():
    # Caller is only renaming the assignment — no trigger fields touched.
    req = UpdateAssignmentRequest(name="renamed-only")
    assert req.name == "renamed-only"
    assert req.trigger_type is None
    assert req.trigger_config is None


def test_create_assignment_rejects_non_dict_trigger_config():
    # Pydantic should reject a string or list payload for `trigger_config`
    # because the field is typed as `dict`. The cron validator therefore
    # never gets to see a non-dict value, but we lock in the type guard
    # so future loosening of the schema can't silently accept garbage.
    with pytest.raises(ValidationError):
        CreateAssignmentRequest(**_payload(trigger_config="not a dict"))
    with pytest.raises(ValidationError):
        CreateAssignmentRequest(**_payload(trigger_config=["a", "b"]))


def test_update_assignment_rejects_invalid_cron_when_only_trigger_config_set():
    # Bypass guard: PATCH with `trigger_config={"cron": "invalid"}` and no
    # `trigger_type` MUST still fail validation. The persisted assignment
    # might already have `trigger_type="schedule"`; without this check, the
    # invalid cron would be saved and the scheduler would crash on the
    # next tick.
    with pytest.raises(ValidationError) as exc_info:
        UpdateAssignmentRequest(trigger_config={"cron": "not a cron"})
    msg = str(exc_info.value)
    assert "Invalid cron expression" in msg


def test_update_assignment_rejects_empty_cron_when_only_trigger_config_set():
    # Same bypass guard: an empty cron string in a partial update is just
    # as dangerous as a malformed one — the scheduler would still fail.
    with pytest.raises(ValidationError):
        UpdateAssignmentRequest(trigger_config={"cron": "   "})
    with pytest.raises(ValidationError):
        UpdateAssignmentRequest(trigger_config={"cron": ""})


def test_update_assignment_rejects_non_string_cron_when_only_trigger_config_set():
    # A non-string `cron` value must also be rejected — Pydantic accepts
    # `dict[Any, Any]` for `trigger_config`, so the inner type guard has
    # to do its own work.
    with pytest.raises(ValidationError):
        UpdateAssignmentRequest(trigger_config={"cron": 12345})
    with pytest.raises(ValidationError):
        UpdateAssignmentRequest(trigger_config={"cron": None})


def test_update_assignment_accepts_valid_cron_when_only_trigger_config_set():
    # Counterpart to the bypass test: a valid cron in a partial update
    # passes through unchanged — this is the legitimate "tweak the cron
    # on an already-scheduled assignment" workflow.
    req = UpdateAssignmentRequest(trigger_config={"cron": "0 9 * * 1"})
    assert req.trigger_config == {"cron": "0 9 * * 1"}
    assert req.trigger_type is None


def test_update_assignment_skips_cron_check_for_non_schedule_trigger_config():
    # If the partial update changes `trigger_config` to a webhook-shaped
    # payload (no `cron` key), the cron validator must not fire. This is
    # the second leg of "trigger_type=webhook + trigger_config=..."
    # workflows where the caller updates the config in a separate patch.
    req = UpdateAssignmentRequest(trigger_config={"path": "github/push"})
    assert req.trigger_config == {"path": "github/push"}


_SCHEDULE_CONFIGS = [
    {"cron": "0 9 * * MON-FRI"},
    {"cron": "30 0 9 * * 1"},
    {"cron": "*/15 * * 1"},
    {"cron": "not a cron"},
    {"cron": ""},
    {"cron": "   "},
    {"cron": 42},
    {"cron": None},
    {"timezone": "UTC"},
    {},
]


@pytest.mark.parametrize("config", _SCHEDULE_CONFIGS)
def test_rest_schemas_and_builder_tools_share_one_schedule_rule(config):
    """The REST create / update schemas, agent import and the agent-builder
    assignment tools all apply ``validate_schedule_trigger_config``: same
    verdict, same message, for a schedule trigger whose whole config is known."""
    from app.api.v1 import agent_import
    from app.api.v1.schemas import assignment as schemas
    from app.core import utils
    from app.services.builtin_tools import agent_builder

    assert schemas.validate_schedule_trigger_config is utils.validate_schedule_trigger_config
    assert agent_import.validate_schedule_trigger_config is utils.validate_schedule_trigger_config
    assert agent_builder.schedule_trigger_error is utils.schedule_trigger_error

    try:
        utils.validate_schedule_trigger_config("schedule", config)
        expected = None
    except ValueError as exc:
        expected = str(exc)

    def _schema_error(build) -> str | None:
        try:
            build()
        except ValidationError as exc:
            return str(exc)
        return None

    create_error = _schema_error(
        lambda: CreateAssignmentRequest(**_payload(trigger_config=config))
    )
    update_error = _schema_error(
        lambda: UpdateAssignmentRequest(trigger_type="schedule", trigger_config=config)
    )
    tool_error = utils.schedule_trigger_error("schedule", config)

    if expected is None:
        assert (create_error, update_error, tool_error) == (None, None, None)
    else:
        for error in (create_error, update_error, tool_error):
            assert error is not None and expected in error


def test_update_assignment_allows_clearing_trigger_config_to_empty_dict():
    # An empty config dict has no `cron` key, so the schema skips: it cannot
    # see the stored trigger type. On a scheduled assignment the route carries
    # the stored cron forward, so clearing a schedule in two steps (config,
    # then type) keeps working (test_assignment_update_schedule_postgres.py).
    req = UpdateAssignmentRequest(trigger_config={})
    assert req.trigger_config == {}
