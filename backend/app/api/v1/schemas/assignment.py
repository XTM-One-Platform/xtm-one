# Copyright (c) 2025-2026 Filigran SAS
#
# This file is part of the XTM One Enterprise Edition ("EE") and is
# licensed under the XTM One Enterprise Edition License (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.

"""Pydantic schemas for assignment endpoints."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import (
    BaseModel,
    Field,
    SerializerFunctionWrapHandler,
    ValidationInfo,
    field_serializer,
    field_validator,
    model_validator,
)

from app.api.v1.schemas.column_limits import lone_surrogate_validator
from app.core.utils import validate_cron_expression, validate_schedule_trigger_config
from app.models.assignment import (
    ASSIGNMENT_BOUNDED_TEXT_FIELDS,
    Assignment,
    assignment_text_length_error,
)
from app.services.element_scope import parse_scope_enforcement
from app.services.run_outcome import run_outcome


def _validate_cron_in_trigger_config(trigger_config: dict | None) -> None:
    """Validate a `cron` key in a partial trigger_config update.

    When a caller updates only `trigger_config` (no `trigger_type`) on an
    already-scheduled assignment, we cannot know the persisted `trigger_type`
    at the schema layer — but if the new config carries a `cron` key, that
    value still has to be a parseable cron. Otherwise an update like
    `{"trigger_config": {"cron": "invalid"}}` silently persists a malformed
    cron and crashes the scheduler on the next tick.

    Skips silently when the config has no `cron` key (legitimate
    non-schedule trigger configs).
    """
    if not isinstance(trigger_config, dict):
        return  # type-coerced by Pydantic — handled separately
    if "cron" not in trigger_config:
        return
    validate_cron_expression(trigger_config["cron"])


def _normalize_kb_compat(req: BaseModel) -> None:
    """Map deprecated WRITE KB inputs onto the new write refinement field.

    Mutates *req* in place. The deprecated write field is mapped only when it
    was *explicitly* set on the request AND ``write_knowledge_base_ids`` was
    not, so modern clients (sending the new field, or relying on its ``NULL`` =
    inherit-all-writable default) are untouched. Without it, a legacy
    ``write_knowledge_base_enabled = False`` with no ``write_knowledge_base_ids``
    would leave ``NULL`` = "inherit all writable" and silently grant writes the
    caller never intended - so write keeps a meaningful explicit "none" (``[]``).

    Read is deliberately NOT normalized here: read has no "none" state - ``NULL``
    and ``[]`` both mean "inherit all of the agent's KBs" - so the legacy
    ``use_knowledge_bases`` flag has nothing to map onto and is simply no longer
    consulted (an assignment with bound agent KBs always remains searchable).
    """
    fields_set = req.model_fields_set
    # Write: legacy ``write_knowledge_base_enabled`` (+ optional target) with no
    # explicit write selection -> [target] when enabled with a target, else [].
    if (
        "write_knowledge_base_ids" not in fields_set
        and "write_knowledge_base_enabled" in fields_set
    ):
        if req.write_knowledge_base_enabled and req.target_knowledge_base_id:
            req.write_knowledge_base_ids = [req.target_knowledge_base_id]
        else:
            req.write_knowledge_base_ids = []


def _normalize_blank_activation_prompt(req: BaseModel) -> None:
    """Collapse a blank / whitespace-only activation prompt to ``None``.

    The activation prompt is the optional "should I run?" gate: ``NULL`` means
    "no gate, always run". A caller clearing the field from the UI sends ``""``
    (and a caller who typed a stray space sends ``"   "``) — both mean "remove
    the gate", but a truthy ``"   "`` would otherwise be persisted and make the
    engine issue a real gate LLM call with a blank question, letting its answer
    decide whether the assignment runs at all.

    Mutates *req* in place, and only when the field was actually provided as a
    string: an update that never mentions ``activation_prompt`` must stay out of
    ``model_fields_set`` so the "omitted means unchanged" update loop doesn't
    clear an existing gate.
    """
    if isinstance(req.activation_prompt, str) and not req.activation_prompt.strip():
        req.activation_prompt = None


def _parsed_activation_check(value: Any) -> dict | None:
    """The tool-capable gate as stored, refused with its reason when malformed."""
    from app.services.assignment_activation_check import parse_activation_check

    parsed, error, _provided = parse_activation_check(value)
    if error is not None:
        raise ValueError(error)
    return parsed


def _refuse_over_long_text(value: str | None, info: ValidationInfo) -> str | None:
    """Refuse a value its ``String(n)`` column cannot hold, before the route runs.

    Both routes write these fields as sent, and PostgreSQL refusing the write
    was a 500. Any shorter string is kept: a trigger type is not an allow-list.
    """
    error = assignment_text_length_error(info.field_name, value)
    if error is not None:
        raise ValueError(error)
    return value


class AssignmentResponse(BaseModel):
    id: str
    agent_id: str
    user_id: str | None = None
    is_company_managed: bool = False
    run_as_user_id: str | None = None
    run_as_all_users: bool = False
    # A seeded built-in assignment an administrator turns on and points at a
    # run-as account: only ``enabled`` and ``run_as_user_id`` are
    # writable on it (``builtin_agents.ADMIN_OPT_IN_WRITABLE_FIELDS``).
    builtin_admin_opt_in: bool = False
    hide_runs: bool | None = None
    name: str
    description: str | None = None
    trigger_type: str
    trigger_config: dict
    # How an integration-event / webhook trigger matches event keys:
    # "exact", or "legacy" for a trigger saved before exact matching that
    # keeps its prefix match. None for the other trigger types.
    event_matching: str | None = None
    activation_prompt: str | None = None
    # The tool-capable gate evaluated before the LLM gate; null = none.
    activation_check: dict | None = None
    prompt: str
    delay: str | None = None
    tool_sources: list[str] | None = None
    integration_bindings: dict[str, list[str]] | None = None
    integration_allowed_tools: dict[str, list[str] | None] | None = None
    # Built-in group id -> function names; intersected with the agent's.
    builtin_allowed_tools: dict[str, list[str] | None] | None = None
    integration_element_bindings: dict[str, list[dict]] | None = None
    # The element types whose selection is a boundary on the tools;
    # null = none. A type the agent enforces is a boundary too.
    integration_element_enforcement: list[str] | None = None
    channel_config: dict[str, dict] | None = None
    use_knowledge_bases: bool
    read_knowledge_base_ids: list[str] | None = None
    write_knowledge_base_ids: list[str] | None = None
    # Runner refinement (native OS-control): null = inherit the agent's runner
    # selection, [] = no runners, subset = refine (same pattern as tool/KB scoping).
    runner_ids: list[str] | None = None
    # Deprecated — superseded by the agent KB permission model + write_knowledge_base_ids.
    write_knowledge_base_enabled: bool = False
    target_knowledge_base_id: str | None = None
    ai_label: bool
    ai_label_prompt: str | None = None
    notify_on_complete: bool
    show_in_digest: bool
    human_in_the_loop: bool | None = None
    human_in_the_loop_prompt: str | None = None
    # null = inherit (agent override -> platform default); a value overrides the agent.
    max_iterations: int | None = None
    # Per-assignment ``wait`` policy override: {per_call_seconds,
    # total_seconds, unbounded_with_until}, any subset; null = inherit.
    wait_policy: dict | None = None
    # Organization summary in the run's system prompt (#11533): inherit | always | off.
    organization_context_mode: Literal["inherit", "always", "off"] | None = None
    # Categories preloaded after the basic block (#11533): null = inherit, a list wins.
    organization_context_categories: list[str] | None = None
    timeout_seconds: int = 300
    dry_run: bool = False
    ignore_self_triggered_events: bool = False
    retry_on_failure: bool = False
    max_retries: int = 3
    manual_trigger_parameters: list[dict] | None = None
    enabled: bool
    # --- Pause provenance ---
    # Describes the CURRENT pause only; all NULL while the assignment is
    # enabled, and all NULL on an assignment paused before this was tracked
    # (the UI falls back to "reason unknown" rather than inventing one).
    paused_at: str | None = None
    #: ``user_toggle`` | ``agent_tool`` | ``auth_quarantine`` |
    #: ``invalid_schedule`` | ``created_disabled``
    paused_reason: str | None = None
    paused_by_user_id: str | None = None
    #: Denormalized actor label (user email, or the system component).
    paused_by_label: str | None = None
    paused_details: str | None = None
    #: Not company-managed and its owner's account is deactivated: nothing starts
    #: it, whatever ``enabled`` says. Derived at read time, never stored, so it
    #: clears the moment the account is reactivated.
    paused_by_owner_deactivation: bool = False
    execution_count: int
    #: Integration events the trigger's filter dropped before a run was created.
    events_filtered_count: int = 0
    last_triggered_at: str | None = None
    webhook_url: str | None = (
        None  # computed: per-assignment webhook URL (for webhook + integration_event triggers)
    )
    integration_webhook_url: str | None = (
        None  # computed: generic integration-level webhook URL (fans out to all matching assignments)
    )
    created_at: str
    updated_at: str

    model_config = {"from_attributes": True}


class AssignmentScheduleResponse(BaseModel):
    """Trigger semantics + computed next/pending run times for one assignment.

    Powers the Agentic Flow "Schedule" tab ("what will trigger, and when?").
    Read-only and computed on the fly: ``next_run_at`` comes from the cron
    expression + ``last_triggered_at`` via the shared ``core.utils.next_cron_run``
    helper (same logic the scheduler uses), and ``pending_run_at`` is the
    earliest queued/delayed orchestration execution time. Nothing here is
    persisted.
    """

    assignment_id: str
    assignment_name: str
    agent_id: str
    agent_name: str
    agent_icon: str
    trigger_type: str
    enabled: bool
    # schedule triggers
    cron: str | None = None
    next_run_at: str | None = None
    # The zone the cron is read in (``trigger_config.timezone``, IANA; "UTC"
    # when absent or not recognised), the next run as a wall time in that zone
    # (ISO 8601 with its offset; ``next_run_at`` stays UTC), and the warning
    # "timezone '<value>' not recognised, UTC used" when the configured zone
    # is unknown, so a wrong zone is visible.
    timezone: str | None = None
    next_run_local: str | None = None
    timezone_warning: str | None = None
    # all triggers
    delay_seconds: int = 0
    last_triggered_at: str | None = None
    # The latest finished run the caller can open: its status
    # (completed / failed / skipped / cancelled) and when it finished, None
    # without one. The Schedule row shows them together, outcome dot included.
    last_run_status: str | None = None
    last_run_finished_at: str | None = None
    has_active_run: bool = False
    pending_run_at: str | None = None
    # Accepted webhook deliveries waiting behind the active run. A
    # non-zero value is a backlog forming: the assignment is receiving
    # deliveries faster than it can run them. Operators need to SEE this —
    # before the queue existed the same situation was an INFO log and a
    # silently discarded delivery.
    queued_run_count: int = 0
    # integration_event triggers
    provider: str | None = None
    event: str | None = None
    poll_interval_seconds: int | None = None
    # webhook + integration_event triggers
    webhook_url: str | None = None


class CreateAssignmentRequest(BaseModel):
    agent_id: str
    name: str
    description: str | None = None
    trigger_type: str  # schedule | webhook | integration_event | manual | work_item | platform_event
    trigger_config: dict = {}

    # Stored as sent, a lone surrogate committed a row every read failed on.
    _no_lone_surrogates = lone_surrogate_validator(Assignment)

    @field_validator(*ASSIGNMENT_BOUNDED_TEXT_FIELDS)
    @classmethod
    def _fit_the_column(cls, value: str | None, info: ValidationInfo) -> str | None:
        return _refuse_over_long_text(value, info)

    @field_validator("integration_element_enforcement")
    @classmethod
    def _only_enforceable_types(cls, value: list[str] | None) -> list[str] | None:
        # Only the types a provider enforces: a switch that does nothing is
        # refused instead of stored.
        return parse_scope_enforcement(value)

    @model_validator(mode="after")
    def _validate_cron(self) -> CreateAssignmentRequest:
        validate_schedule_trigger_config(self.trigger_type, self.trigger_config)
        return self

    @model_validator(mode="after")
    def _validate_platform_event(self) -> CreateAssignmentRequest:
        # A platform_event trigger names known events.
        from app.services.platform_events import trigger_config_error

        error = trigger_config_error(self.trigger_type, self.trigger_config)
        if error is not None:
            raise ValueError(error)
        return self

    @model_validator(mode="after")
    def _validate_batch_window(self) -> CreateAssignmentRequest:
        # A batch window on an event trigger only, 0 to 600 seconds.
        from app.services.event_batching import window_error

        error = window_error(self.trigger_type, self.trigger_config)
        if error is not None:
            raise ValueError(error)
        return self

    @model_validator(mode="after")
    def _validate_work_item_concurrency(self) -> CreateAssignmentRequest:
        # A work_item inbox runs 1 to 10 items in parallel.
        from app.services.assignment_dispatch import work_item_concurrency_error

        if self.trigger_type == "work_item":
            error = work_item_concurrency_error(self.trigger_config)
            if error is not None:
                raise ValueError(error)
        return self

    @model_validator(mode="after")
    def _validate_trigger_filter(self) -> CreateAssignmentRequest:
        # A filter expression the payload filter can evaluate.
        from app.services.integration_event_filters import trigger_filter_error

        error = trigger_filter_error(self.trigger_type, self.trigger_config)
        if error is not None:
            raise ValueError(error)
        return self

    @model_validator(mode="after")
    def _normalize_deprecated_kb_fields(self) -> CreateAssignmentRequest:
        # Map the deprecated KB fields onto the new read/write refinement fields
        # for legacy clients that still send only the old fields. Only fires
        # when the deprecated field was *explicitly* provided and its new
        # counterpart was not, so modern clients keep the new NULL = inherit-all
        # default. Without this a legacy ``write_knowledge_base_enabled=False``
        # (with no ``write_knowledge_base_ids``) would leave NULL = "inherit all
        # writable" and silently grant writes the caller never intended.
        _normalize_kb_compat(self)
        return self

    @model_validator(mode="after")
    def _normalize_activation_prompt(self) -> CreateAssignmentRequest:
        # An empty / whitespace-only gate means "no gate" — normalize on
        # write so the engine never runs a gate LLM call with a blank question.
        _normalize_blank_activation_prompt(self)
        return self

    @field_validator("activation_check", mode="before")
    @classmethod
    def _valid_activation_check(cls, value: Any) -> dict | None:
        return _parsed_activation_check(value)

    activation_prompt: str | None = None  # optional "should I run?" gate
    # Optional tool-capable gate: a read-only tool call and a condition.
    activation_check: dict | None = None
    prompt: str
    delay: str | None = None
    # Optional "run as" user (company-managed agents only; admin-only).
    run_as_user_id: str | None = None
    # When true, a scheduled trigger fans out to one run per active user.
    run_as_all_users: bool = False
    # null = inherit the agent's hide_runs default; True/False overrides it.
    hide_runs: bool | None = None
    tool_sources: list[str] | None = None
    integration_bindings: dict[str, list[str]] | None = None
    integration_allowed_tools: dict[str, list[str] | None] | None = None
    # Built-in group id -> function names; intersected with the agent's.
    builtin_allowed_tools: dict[str, list[str] | None] | None = None
    integration_element_bindings: dict[str, list[dict]] | None = None
    # The element types whose selection is a boundary on the tools;
    # null = none. A type the agent enforces is a boundary too.
    integration_element_enforcement: list[str] | None = None
    channel_config: dict[str, dict] | None = None
    use_knowledge_bases: bool = True
    read_knowledge_base_ids: list[str] | None = None
    write_knowledge_base_ids: list[str] | None = None
    # Runner refinement (native OS-control): null = inherit the agent's runner
    # selection, [] = no runners, subset = refine (same pattern as tool/KB scoping).
    runner_ids: list[str] | None = None
    # Deprecated — kept for backward-compatible imports/clients.
    write_knowledge_base_enabled: bool = False
    target_knowledge_base_id: str | None = None
    ai_label: bool = True
    ai_label_prompt: str | None = None
    notify_on_complete: bool = False
    show_in_digest: bool = False
    human_in_the_loop: bool | None = None
    human_in_the_loop_prompt: str | None = None
    # null (default) = inherit (agent override -> platform default); a value overrides the agent.
    max_iterations: int | None = None
    # Per-assignment ``wait`` policy override: {per_call_seconds,
    # total_seconds, unbounded_with_until}, any subset; null = inherit.
    wait_policy: dict | None = None
    # Organization summary in the run's system prompt (#11533): inherit | always | off.
    organization_context_mode: Literal["inherit", "always", "off"] | None = None
    # Categories preloaded after the basic block (#11533): null = inherit, a list wins.
    organization_context_categories: list[str] | None = None
    timeout_seconds: int = 300
    dry_run: bool = False
    ignore_self_triggered_events: bool = False
    retry_on_failure: bool = False
    max_retries: int = 3
    manual_trigger_parameters: list[dict] | None = None
    enabled: bool = True


class UpdateAssignmentRequest(BaseModel):
    name: str | None = None
    description: str | None = None
    trigger_type: str | None = None
    trigger_config: dict | None = None

    _no_lone_surrogates = lone_surrogate_validator(Assignment)

    @field_validator(*ASSIGNMENT_BOUNDED_TEXT_FIELDS)
    @classmethod
    def _fit_the_column(cls, value: str | None, info: ValidationInfo) -> str | None:
        return _refuse_over_long_text(value, info)

    @field_validator("integration_element_enforcement")
    @classmethod
    def _only_enforceable_types(cls, value: list[str] | None) -> list[str] | None:
        # Only the types a provider enforces: a switch that does nothing is
        # refused instead of stored.
        return parse_scope_enforcement(value)

    @model_validator(mode="after")
    def _validate_cron(self) -> UpdateAssignmentRequest:
        # Validation has to cover three partial-update shapes because the
        # schema layer has no access to the persisted assignment row:
        #
        # 1) Caller explicitly switches `trigger_type` to `"schedule"`:
        #    require a fresh `trigger_config` carrying a valid cron.
        #    Otherwise the row would be persisted as a schedule trigger
        #    bound to the **previous** (possibly non-schedule)
        #    `trigger_config`, and the scheduler would crash on the next
        #    tick.
        #
        # 2) Caller leaves `trigger_type` unset but updates
        #    `trigger_config` (i.e. partial update on an
        #    already-scheduled assignment): if the new config carries a
        #    `cron` key, validate it. This closes the bypass where
        #    `PATCH {"trigger_config": {"cron": "invalid"}}` would
        #    otherwise persist a malformed cron and crash the scheduler.
        #    Non-schedule configs (no `cron` key, e.g. webhook payloads)
        #    are passed through untouched.
        #
        # 3) Caller leaves both unset (rename, prompt change, etc.):
        #    skip — nothing to validate.
        if self.trigger_type == "schedule":
            if self.trigger_config is None:
                raise ValueError(
                    "trigger_config with a valid 'cron' is required when "
                    "setting trigger_type to 'schedule'."
                )
            validate_schedule_trigger_config(self.trigger_type, self.trigger_config)
        elif self.trigger_config is not None:
            _validate_cron_in_trigger_config(self.trigger_config)
        if self.trigger_type == "platform_event":
            # Switching to platform_event needs a config naming known events.
            from app.services.platform_events import trigger_config_error

            error = trigger_config_error(self.trigger_type, self.trigger_config)
            if error is not None:
                raise ValueError(error)
        if self.trigger_config is not None:
            # A filter expression the payload filter can evaluate; a
            # partial update keeps the stored type, so its filter is checked too.
            from app.services.integration_event_filters import trigger_filter_error

            error = trigger_filter_error(self.trigger_type, self.trigger_config)
            if error is not None:
                raise ValueError(error)
        return self

    @model_validator(mode="after")
    def _normalize_deprecated_kb_fields(self) -> UpdateAssignmentRequest:
        # Same backward-compat normalization as create: a partial update that
        # touches only the deprecated KB fields is mapped onto the new
        # refinement fields (gated on ``model_fields_set``), so legacy clients
        # can't silently flip KB read/write via the NULL-vs-empty semantics.
        # An explicit ``null`` on the deprecated flag is "no change" on update:
        # mapping it would rewrite an inherit-all write scope to "none".
        if (
            "write_knowledge_base_enabled" in self.model_fields_set
            and self.write_knowledge_base_enabled is None
        ):
            return self
        _normalize_kb_compat(self)
        return self

    @model_validator(mode="after")
    def _normalize_activation_prompt(self) -> UpdateAssignmentRequest:
        # Same normalization as create: an explicitly blank gate clears the
        # column instead of being stored as a truthy whitespace gate.
        # ``activation_prompt`` stays in ``model_fields_set``, so the update
        # loop writes the NULL through rather than skipping the field.
        _normalize_blank_activation_prompt(self)
        return self

    @field_validator("activation_check", mode="before")
    @classmethod
    def _valid_activation_check(cls, value: Any) -> dict | None:
        # ``null`` (or ``{}``) clears the check; omitted leaves it unchanged.
        return _parsed_activation_check(value)

    activation_prompt: str | None = None
    activation_check: dict | None = None
    prompt: str | None = None
    delay: str | None = None
    run_as_user_id: str | None = None
    run_as_all_users: bool | None = None
    hide_runs: bool | None = None
    tool_sources: list[str] | None = None
    integration_bindings: dict[str, list[str]] | None = None
    integration_allowed_tools: dict[str, list[str] | None] | None = None
    # Built-in group id -> function names; intersected with the agent's.
    builtin_allowed_tools: dict[str, list[str] | None] | None = None
    integration_element_bindings: dict[str, list[dict]] | None = None
    # The element types whose selection is a boundary on the tools;
    # null = none. A type the agent enforces is a boundary too.
    integration_element_enforcement: list[str] | None = None
    channel_config: dict[str, dict] | None = None
    use_knowledge_bases: bool | None = None
    read_knowledge_base_ids: list[str] | None = None
    write_knowledge_base_ids: list[str] | None = None
    # Runner refinement (native OS-control): null = inherit / not-provided, [] =
    # no runners, subset = refine. "Not provided" vs explicit null is
    # distinguished via model_fields_set in the route (same as the KB fields).
    runner_ids: list[str] | None = None
    # Deprecated — kept for backward-compatible clients.
    write_knowledge_base_enabled: bool | None = None
    target_knowledge_base_id: str | None = None
    ai_label: bool | None = None
    ai_label_prompt: str | None = None
    notify_on_complete: bool | None = None
    show_in_digest: bool | None = None
    human_in_the_loop: bool | None = None
    human_in_the_loop_prompt: str | None = None
    # null = inherit (agent override -> platform default); a value overrides the
    # agent. Distinguished from "not provided" via model_fields_set in the route.
    max_iterations: int | None = None
    # Per-assignment ``wait`` policy override: {per_call_seconds,
    # total_seconds, unbounded_with_until}, any subset; null = inherit.
    wait_policy: dict | None = None
    # Organization summary in the run's system prompt (#11533): inherit | always | off.
    organization_context_mode: Literal["inherit", "always", "off"] | None = None
    # Categories preloaded after the basic block (#11533): null = inherit, a list wins.
    organization_context_categories: list[str] | None = None
    timeout_seconds: int | None = None
    dry_run: bool | None = None
    ignore_self_triggered_events: bool | None = None
    retry_on_failure: bool | None = None
    max_retries: int | None = None
    manual_trigger_parameters: list[dict] | None = None
    enabled: bool | None = None


class _ShownAsRead(BaseModel):
    """A run as a person reads it: a stored secret variable marker is ``[secret]``, never restorable."""

    @field_serializer("*", mode="wrap")
    def _secret_markers_shown(self, value: Any, handler: SerializerFunctionWrapHandler):
        from app.core.redaction import redact_known_secrets_for_display

        return redact_known_secrets_for_display(handler(value))


class _RunOutcome(BaseModel):
    """``outcome``: what the run's end says about its agent
    (``run_outcome.run_outcome``): ``interrupted`` for a run the
    platform ended (a rollout cut, a restart), which keeps its ``failed``
    status and is listed, but never counts as the agent failing."""

    outcome: str | None = None

    @model_validator(mode="after")
    def _derive_outcome(self):
        if self.outcome is None:
            self.outcome = run_outcome(getattr(self, "status", None), getattr(self, "error", None))
        return self


class AssignmentRunResponse(_RunOutcome, _ShownAsRead):
    id: str
    assignment_id: str | None = None
    agent_id: str
    status: str
    is_company_managed: bool = False
    notification_sent: bool = False
    parent_run_id: str | None = None
    child_run_count: int = 0
    trigger_payload: dict | None = None
    trigger_context: dict | None = None
    actions_taken: list | None = None
    result_summary: str | None = None
    error: str | None = None
    # Structured proposals when the pause came from the per-tool gate rather
    # than from ``request_human_input``. Carried on the run itself, like
    # ``human_input_question`` beside it, because the reviewer decides from the
    # run card — without it the card cannot tell a gated pause from a free-text
    # one and falls back to a text box, which cannot express approve/reject.
    pending_approvals: list | None = None
    #: The reviewer has decided; the run is waiting on the worker, not on them.
    #: ``resume_assignment_run`` emits an outbox event without changing the
    #: status, so the run stays ``awaiting_human`` until the queue drains — a
    #: client that offered it again would overwrite the recorded decision and
    #: emit a second resume. Derived rather than the decisions themselves:
    #: whether it was answered is all a list needs to know.
    approval_answered: bool = False
    # Structured failure attribution. ``error`` stays the free text a
    # human reads; these two are what a query groups by. Both NULL on a run
    # that succeeded — and on a failed run recorded before the classifier
    # existed, which is why the UI treats "no cause" as its own state rather
    # than as ``unknown``.
    failure_cause: str | None = None
    failure_determination: str | None = None
    # Why a ``completed`` run stopped before its task was done: the
    # iteration cap, the wall clock, the per-run budget, ... NULL when it
    # ended on its own terms (``AssignmentRun.stop_reason``).
    stop_reason: str | None = None
    # Integrations whose calls failed authentication in this run (#1454):
    # ``[{integration, status, first_tool, tools}]``; NULL when none did.
    integration_auth_failures: list[dict[str, Any]] | None = None
    human_input_question: str | None = None
    human_input_response: str | None = None
    human_input_history: list | None = None
    langfuse_trace_id: str | None = None
    llm_trace_id: str | None = None
    total_input_tokens: int | None = None
    total_output_tokens: int | None = None
    started_at: str | None = None
    completed_at: str | None = None
    created_at: str


class TriggerAssignmentRequest(BaseModel):
    payload: dict | None = None


class ApprovalDecision(BaseModel):
    """One reviewer decision about one proposed tool call.

    Keyed on ``tool_call_id``, never on the tool name: a single turn can call
    the same tool more than once with different arguments, and those are
    separate decisions.
    """

    tool_call_id: str
    decision: Literal["approve", "approve_always", "reject"]
    # No arguments here on purpose: the reviewer answers the call the agent
    # proposed. An agent that proposed the wrong one is corrected by a reject
    # carrying a reason it can act on, not by a human rewriting the call under
    # its name — which would leave the transcript claiming the agent chose
    # arguments it never chose.
    rejection_reason: str | None = None


class BatchApprovalDecision(BaseModel):
    """One decision inside a batch, carrying the run it belongs to.

    ``approve_always`` is accepted here (FR-032, revised 2026-09-02). It was
    refused at first on the grounds that "a group has no single tool FR-015c's
    disclosure could be about" — which only holds if the disclosure has to name
    ONE tool. A disclosure that **enumerates** every tool the click would
    permanently un-gate answers the objection directly, and is strictly more
    informative than the per-proposal one it replaces. The obligation therefore
    moves rather than disappearing: the caller must show that enumeration
    before submitting, and the batch must never be the only place a reviewer
    can reach the durable choice.
    """

    run_id: str
    tool_call_id: str
    decision: Literal["approve", "approve_always", "reject"]
    rejection_reason: str | None = None


class PendingToolSummary(BaseModel):
    """How much is waiting on one tool, across everything the caller may answer."""

    tool_name: str
    #: Runs whose pending calls are ALL this tool, and so are answerable by a
    #: decision about it alone.
    run_count: int
    call_count: int


class PendingMixedGroup(BaseModel):
    """Runs whose pending calls span several tools, grouped by that combination.

    Answerable once every name in ``tool_names`` is selected, which is the rule
    ``approve_by_tool`` applies. Grouped by combination rather than listed per
    run so the payload is bounded by how many distinct combinations are paused,
    not by how many runs are.
    """

    #: Sorted, so one combination has one identity across requests.
    tool_names: list[str]
    run_count: int
    call_count: int


class PendingByToolResponse(BaseModel):
    tools: list[PendingToolSummary]
    #: The mixed runs themselves, not merely how many. A selection covering a
    #: combination answers its runs too, so a reviewer shown only a count was
    #: consenting against a total that understated its own reach.
    mixed: list[PendingMixedGroup]
    #: ``sum(g.run_count for g in mixed)``. Kept because several surfaces want
    #: only "how much is waiting" and should not have to add the list up.
    mixed_run_count: int


class ApproveByToolRequest(BaseModel):
    """Answer every paused run whose calls are all among ``tool_names``.

    A decision about tools rather than about rows, which is what lets it reach
    past the page the reviewer has loaded: the selection is derived from the
    tools they named, not from what happened to be on screen.
    """

    tool_names: list[str]
    #: ``approve_always`` is accepted here, and more clearly consented than in a
    #: batch: FR-032b asks that the disclosure enumerate every tool the click
    #: un-gates, and here the reviewer named them. The caller still owes the
    #: disclosure — it now covers two reaches, the calls waiting and every
    #: future run (FR-040e).
    decision: Literal["approve", "approve_always", "reject"]
    rejection_reason: str | None = None
    #: Narrows the sweep to one agent's runs, for the copy of this control that
    #: sits on an agent's activity list. The reviewer named the tools while
    #: looking at one agent, so the reach must stop where the page does.
    agent_id: uuid.UUID | None = None
    #: Narrows the sweep to the caller's own runs, for the offer that follows
    #: switching allow-all on (FR-043). That switch reaches exactly the runs
    #: carrying their identity, and an admin may answer other people's — so the
    #: sweep beside it must not go further than the switch does.
    own_only: bool = False


class BatchApprovalRequest(BaseModel):
    decisions: list[BatchApprovalDecision]


class ReleaseCoveredRequest(BaseModel):
    """Release the runs among ``run_ids`` that a standing approval fully covers.

    Deliberately carries no decisions. The reviewer is not answering calls here
    — they are asking the platform to apply exceptions they already granted, so
    which calls get approved is the server's to determine (FR-039b). A body of
    per-call decisions would put that arithmetic back on the caller, which is
    what let a partially covered run through.

    ``run_ids`` is the scope the surface can act on, not a filter over
    everything waiting: the offer states what it will release (FR-039d), and it
    must not release more than it showed.
    """

    run_ids: list[str]


class BatchApprovalResult(BaseModel):
    run_id: str
    tool_call_id: str
    status: Literal["applied", "refused"]
    #: Present only when refused: ``already_answered``, ``run_not_awaiting``,
    #: ``run_not_found``, ``forbidden``, ``incomplete_decisions``,
    #: ``rejected_by_applier``, ``not_fully_covered``.
    reason: str | None = None
    detail: str | None = None


class BatchApprovalResponse(BaseModel):
    """Per-member results. Partial success is a success (FR-033)."""

    results: list[BatchApprovalResult]


class RespondToHumanInputRequest(BaseModel):
    """Resolve a paused run — free-text answer, or structured approvals.

    Exactly one of ``response`` / ``decisions`` is expected. ``response`` stays
    optional-but-unchanged so existing clients answering a
    ``request_human_input`` prompt keep working untouched.
    """

    response: str | None = None
    decisions: list[ApprovalDecision] | None = None

    @model_validator(mode="before")
    @classmethod
    def _free_text_answer_without_decisions(cls, data: Any) -> Any:
        # Without ``decisions`` this is the free-text answer, validated as it
        # always was, so a client keyed on the 422 gets the same body.
        if isinstance(data, dict) and data.get("decisions") is None:
            _FreeTextAnswer.model_validate(data)
        return data


class _FreeTextAnswer(BaseModel):
    response: str


class RunActionRequestResponse(BaseModel):
    """An open request to act on a paused run, made from the card of its pasted link."""

    id: str
    requester_name: str
    source: str
    created_at: str


class FollowupRunRequest(BaseModel):
    prompt: str


class FollowupChatRequest(BaseModel):
    """Open an interactive chat seeded from a terminal run.

    The follow-up prompt is optional here — opening the seeded chat is useful on
    its own (the run history is the value), and the user can type their question
    once the conversation is open.
    """

    prompt: str | None = None


class FollowupChatResponse(BaseModel):
    conversation_id: str
    agent_id: str


class AssignmentRunAttachment(BaseModel):
    file_id: str
    filename: str
    content_type: str
    size: int
    download_url: str
    # Display intent mirrored from the file's provenance ``source`` (see
    # ``attachment_meta_from_file_attachment`` in ``chat.py``): the
    # ``generate_file`` tool persists ``agent_working`` / ``agent_download``,
    # mapped here to the ``working_file`` (compact chip) vs ``download_file``
    # (full download card) render the frontend ``FileChip`` expects. Without
    # this the run-detail UI had to hardcode ``download_file`` and rendered
    # scratch/working artifacts as prominent download cards.
    file_tag: Literal["working_file", "download_file"] | None = None
    # As on a chat attachment: when the file was written (ISO 8601) and the
    # hex SHA-256 of its bytes, what tells two same-name files apart.
    created_at: str | None = None
    sha256: str | None = None


class AssignmentRunDetailResponse(_RunOutcome, _ShownAsRead):
    id: str
    assignment_id: str | None = None
    assignment_name: str | None = None
    agent_id: str
    agent_name: str
    agent_icon: str
    status: str
    is_company_managed: bool = False
    notification_sent: bool = False
    notify_on_complete: bool = False
    show_in_digest: bool = False
    parent_run_id: str | None = None
    child_run_count: int = 0
    parent_agent_name: str | None = None
    parent_agent_icon: str | None = None
    trigger_payload: dict | None = None
    trigger_context: dict | None = None
    #: The A2A provider that submitted this run, resolved from what its calling
    #: registration is owned by (``a2a.providers.resolve_submitter_providers``).
    #: NULL on internal runs, and on external ones whose registration cannot be
    #: attributed — a legacy per-exposure credential served both credential
    #: forms. Resolved server-side so every surface labels a run identically,
    #: instead of each guessing from the registration id's shape and disagreeing.
    a2a_provider: str | None = None
    #: Where an external run stands in its task's exchange with the caller
    #: (``a2a.run_exchange.run_exchanges``): ``asked`` when it asked its caller
    #: a question (waiting, answered or withdrawn), ``answers`` when it
    #: continues the task after the answer. NULL on any other run.
    a2a_exchange: dict | None = None
    actions_taken: list | None = None
    result_summary: str | None = None
    rendered_prompt: str | None = None
    agentic_steps: list[dict] | None = None
    attachments: list[AssignmentRunAttachment] | None = None
    #: The friction ledger of the run (``services/frictions.run_frictions``):
    #: what it struggled with and what carries each fix, oldest first,
    #: bounded. NULL for a run that recorded none.
    frictions: list[dict] | None = None
    #: The frictions the run left with no disposition at its end (NULL: none).
    frictions_undisposed: int | None = None
    #: The run's ROI record as the outcome review row reads it
    #: (``roi_records.review_block``: record id, unit, outcome, review minutes,
    #: note, ``can_review``); NULL when the run recorded no unit.
    roi: dict | None = None
    error: str | None = None
    # Structured proposals when the pause came from the per-tool gate rather
    # than from ``request_human_input``. Carried on the run itself, like
    # ``human_input_question`` beside it, because the reviewer decides from the
    # run card — without it the card cannot tell a gated pause from a free-text
    # one and falls back to a text box, which cannot express approve/reject.
    pending_approvals: list | None = None
    #: The reviewer has decided; the run is waiting on the worker, not on them.
    #: ``resume_assignment_run`` emits an outbox event without changing the
    #: status, so the run stays ``awaiting_human`` until the queue drains — a
    #: client that offered it again would overwrite the recorded decision and
    #: emit a second resume. Derived rather than the decisions themselves:
    #: whether it was answered is all a list needs to know.
    approval_answered: bool = False
    # Structured failure attribution — see AssignmentRunResponse.
    failure_cause: str | None = None
    failure_determination: str | None = None
    # Why a completed run stopped early — see AssignmentRunResponse.
    stop_reason: str | None = None
    # Integration auth failures of the run — see AssignmentRunResponse (#1454).
    integration_auth_failures: list[dict[str, Any]] | None = None
    human_input_question: str | None = None
    human_input_response: str | None = None
    human_input_history: list | None = None
    langfuse_trace_id: str | None = None
    llm_trace_id: str | None = None
    total_input_tokens: int | None = None
    total_output_tokens: int | None = None
    started_at: str | None = None
    completed_at: str | None = None
    created_at: str


class RunCountResponse(BaseModel):
    count: int


class RunStatusCountsResponse(BaseModel):
    """How many runs each status holds under a feed's other filters (the Status pill's counts)."""

    counts: dict[str, int]


class BulkDeleteRunsRequest(BaseModel):
    run_ids: list[str] | None = None  # delete specific runs by ID
    all_matching_filters: bool = False  # delete all matching the filters below
    status: str | None = None
    search: str | None = None
    notifications: bool = False
    digests: bool = False
    ownership: str | None = None
    vis: str | None = None
    agent_id: str | None = None
    # The feed the filters were read from: "all" is an admin's entity Activity
    # tab (every run of the agent), "visible" the own + company-managed feed.
    scope: Literal["visible", "all"] = "visible"


class BulkDeleteRunsResponse(BaseModel):
    deleted: int


class BulkCancelRunsRequest(BaseModel):
    """Which waiting runs ``POST /runs/bulk-cancel`` cancels.

    At least one of ``agent_id``, ``assignment_id`` or ``created_before``;
    ``status`` lists only statuses of runs that have not started.
    """

    agent_id: str | None = None
    assignment_id: str | None = None
    status: list[Literal["pending", "delayed", "queued"]] = Field(
        default_factory=lambda: ["pending", "delayed", "queued"]
    )
    created_before: datetime | None = None
    # True: count what would be cancelled and change nothing (the
    # confirmation the Operations page shows before the real call).
    dry_run: bool = False


class BulkCancelRunsResponse(BaseModel):
    #: Runs cancelled, or with ``dry_run`` the runs that would be.
    count: int
    dry_run: bool = False
