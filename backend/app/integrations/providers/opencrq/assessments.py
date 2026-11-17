"""OpenCRQ compliance assessment tools.

* ``list_opencrq_compliance_assessments``   - assessments of the tenant.
* ``get_opencrq_compliance_assessment``     - assessment detail, requirements, control links.
* ``create_opencrq_compliance_assessment``  - start an assessment on a catalog framework.
* ``manage_opencrq_compliance_assessment``  - rename, publish or reopen an assessment.
* ``update_opencrq_requirement_answer``     - answer one requirement of a draft assessment.
"""

from __future__ import annotations

import datetime
import logging
import uuid

from app.tools.base import CallableToolExecutor, ToolDefinition

from ._common import (
    OFFSET_PROP,
    TENANT_PROP,
    _client,
    _fmt_assessment,
    _one_line,
    _page,
    _quarantined,
    _resolve_tenant,
    _trpc,
)

logger = logging.getLogger(__name__)

_SOURCE = "integration:opencrq"

# Rename has a call of its own: it alone sends ``name``.
_ASSESSMENT_STATUS_PROCEDURES = {
    "publish": "complianceAssessments.publishAssessment",
    "reopen": "complianceAssessments.reopenAssessment",
}
_ASSESSMENT_ACTIONS = ["rename", *_ASSESSMENT_STATUS_PROCEDURES]
_NAME_MAX = 200


def _is_iso_date(value: str) -> bool:
    """``YYYY-MM-DD`` only: OpenCRQ's ``z.iso.date()`` refuses the other forms ``fromisoformat`` takes."""
    try:
        return len(value) == 10 and bool(datetime.date.fromisoformat(value))
    except ValueError:
        return False


def _fmt_requirement_answer(node: dict, answer: dict | None) -> str:
    level = (answer or {}).get("level")
    if (answer or {}).get("notApplicable"):
        state = "not applicable"
    elif level is not None:
        state = f"level {level}"
    else:
        state = "not answered"
    extras = []
    owner = (answer or {}).get("owner") or {}
    if owner:
        extras.append(f"owner {_one_line(owner.get('name'), 120) or _one_line(owner.get('email'), 120)}")
    if (answer or {}).get("dueDate"):
        extras.append(f"due {_one_line(answer['dueDate'], 30)}")
    if (answer or {}).get("comment"):
        extras.append(f"comment: {_one_line(answer['comment'], 80)}")
    title = _one_line(node.get("title"), 80)
    return (
        f"- {_one_line(node.get('code'), 40, '?')} {title}: {state}"
        + (f" ({'; '.join(extras)})" if extras else "")
        + f" (requirement id: {node.get('id')})"
    )


def register(
    base_url: str, headers: dict[str, str], executor: CallableToolExecutor
) -> list[ToolDefinition]:
    async def list_opencrq_compliance_assessments(offset: int = 0, tenant_id: str = "") -> str:
        if offset < 0:
            return "offset must be >= 0."
        tenant, err = await _resolve_tenant(base_url, headers, tenant_id)
        if err:
            return err
        assessments, err = await _trpc(
            base_url, headers, "complianceAssessments.getAssessments",
            "list compliance assessments", tenant_id=tenant,
        )
        if err:
            return err
        assessments = assessments or []
        if not assessments:
            return "No compliance assessments found in this tenant."
        shown, more = _page(assessments, offset, 20)
        return _quarantined(
            f"Compliance assessments ({len(assessments)}) in tenant {tenant}:",
            "\n\n".join(_fmt_assessment(a) for a in shown),
            "compliance assessments",
            more,
        )

    async def get_opencrq_compliance_assessment(
        assessment_id: str = "",
        include_requirements: bool = False,
        include_control_links: bool = False,
        offset: int = 0,
        tenant_id: str = "",
    ) -> str:
        if not assessment_id:
            return "assessment_id is required."
        if offset < 0:
            return "offset must be >= 0."
        tenant, err = await _resolve_tenant(base_url, headers, tenant_id)
        if err:
            return err
        detail, err = await _trpc(
            base_url, headers, "complianceAssessments.getAssessment",
            "get compliance assessment",
            input={"id": assessment_id}, tenant_id=tenant,
        )
        if err:
            return err
        if not detail:
            return f"Compliance assessment {assessment_id} not found."
        out = _fmt_assessment(detail)
        notes: list[str] = []
        nodes = detail.get("nodes") or []
        requirements = [n for n in nodes if n.get("isRequirement")]
        if include_requirements:
            answers = {
                a.get("requirementId"): a for a in (detail.get("answers") or [])
            }
            shown, more = _page(requirements, offset, 100)
            out += f"\n\nRequirements ({len(requirements)}):\n" + "\n".join(
                _fmt_requirement_answer(n, answers.get(n.get("id"))) for n in shown
            )
            notes.append(more and f"Requirements: {more}")
        if include_control_links:
            codes = {n.get("id"): n.get("code") for n in nodes}
            links = detail.get("controlLinks") or []
            shown, more = _page(links, offset, 100)
            out += f"\n\nControl links ({len(links)}):\n" + "\n".join(
                f"- {_one_line(codes.get(link.get('requirementId')), 40, '?')} -> "
                f"{_one_line(link.get('name'), 200, '?')} ({_one_line(link.get('externalId'), 40, '?')}, "
                f"axis {_one_line(link.get('axis'), 20, '?')}, "
                f"capability id: {link.get('capabilityId')})"
                for link in shown
            )
            notes.append(more and f"Control links: {more}")
        return _quarantined("", out, "compliance assessment", *notes)

    async def create_opencrq_compliance_assessment(
        framework_id: str = "", name: str = "", tenant_id: str = ""
    ) -> str:
        name = (name or "").strip()
        if not framework_id or not name:
            return "framework_id and name are required."
        if len(name) > _NAME_MAX:
            return f"name must be at most {_NAME_MAX} characters."
        tenant, err = await _resolve_tenant(base_url, headers, tenant_id)
        if err:
            return err
        assessment_id, err = await _trpc(
            base_url, headers, "complianceAssessments.createAssessment",
            "create compliance assessment",
            input={"name": name, "frameworkId": framework_id},
            mutation=True, tenant_id=tenant,
        )
        if err:
            return err
        return (
            f"Created compliance assessment '{name}' as a draft "
            f"(id: {assessment_id})."
        )

    async def manage_opencrq_compliance_assessment(
        action: str = "",
        assessment_id: str = "",
        name: str = "",
        tenant_id: str = "",
    ) -> str:
        action = (action or "").strip().lower()
        if action not in _ASSESSMENT_ACTIONS:
            return "action must be rename, publish or reopen."
        if not assessment_id:
            return "assessment_id is required."
        name = (name or "").strip()
        if action == "rename":
            if not name:
                return "name is required to rename an assessment."
            if len(name) > _NAME_MAX:
                return f"name must be at most {_NAME_MAX} characters."
        tenant, err = await _resolve_tenant(base_url, headers, tenant_id)
        if err:
            return err
        if action == "rename":
            _, err = await _trpc(
                base_url, headers, "complianceAssessments.renameAssessment",
                "rename compliance assessment",
                input={"id": assessment_id, "name": name}, mutation=True, tenant_id=tenant,
            )
        else:
            _, err = await _trpc(
                base_url, headers, _ASSESSMENT_STATUS_PROCEDURES[action],
                f"{action} compliance assessment",
                input={"id": assessment_id}, mutation=True, tenant_id=tenant,
            )
        if err:
            return err
        return {
            "rename": f"Renamed compliance assessment {assessment_id} to '{name}'.",
            "publish": f"Published compliance assessment {assessment_id}; its answers are frozen.",
            "reopen": f"Reopened compliance assessment {assessment_id}; it is a draft again.",
        }[action]

    async def update_opencrq_requirement_answer(
        assessment_id: str = "",
        requirement_id: str = "",
        level: int | None = None,
        not_applicable: bool = False,
        clear: bool = False,
        comment: str | None = None,
        owner_id: str | None = None,
        due_date: str | None = None,
        tenant_id: str = "",
    ) -> str:
        if not assessment_id or not requirement_id:
            return "assessment_id and requirement_id are required."
        if owner_id:
            try:
                uuid.UUID(owner_id)
            except ValueError:
                return "owner_id must be a user UUID, or empty to remove the owner."
        if due_date and not _is_iso_date(due_date):
            return "due_date must be a date as YYYY-MM-DD, or empty to remove it."
        if level is not None and (
            isinstance(level, bool) or not isinstance(level, int) or not 0 <= level <= 5
        ):
            return "level must be an integer between 0 and 5."
        if sum((level is not None, not_applicable, clear)) > 1:
            return "Provide only one of level, not_applicable=true and clear=true."
        if (
            level is None and not not_applicable and not clear
            and comment is None and owner_id is None and due_date is None
        ):
            return (
                "Provide level (0-5), not_applicable=true, clear=true, a comment, "
                "an owner_id or a due_date."
            )
        if comment is not None and len(comment) > 10_000:
            return "comment must be at most 10000 characters."
        # The upsert replaces the whole answer: omitted fields must be read
        # back so the owner, due date and previous answer survive.
        async with _client(headers, timeout=30) as client:
            tenant, err = await _resolve_tenant(
                base_url, headers, tenant_id, client=client
            )
            if err:
                return err
            detail, err = await _trpc(
                base_url, headers, "complianceAssessments.getAssessment",
                "get compliance assessment",
                input={"id": assessment_id}, tenant_id=tenant, client=client,
            )
            if err:
                return err
            if not detail:
                return f"Compliance assessment {assessment_id} not found."
            current = next(
                (
                    a
                    for a in (detail.get("answers") or [])
                    if a.get("requirementId") == requirement_id
                ),
                {},
            )
            if level is not None:
                new_level, new_na = level, False
            elif not_applicable:
                new_level, new_na = None, True
            elif clear:
                new_level, new_na = None, False
            else:
                new_level = current.get("level")
                new_na = bool(current.get("notApplicable"))
            new_comment = (
                current.get("comment") if comment is None else (comment or None)
            )
            _, err = await _trpc(
                base_url, headers, "complianceAssessments.answerRequirement",
                "answer requirement",
                input={
                    "assessmentId": assessment_id,
                    "requirementId": requirement_id,
                    "ownerId": (
                        (current.get("owner") or {}).get("id")
                        if owner_id is None
                        else owner_id or None
                    ),
                    "dueDate": current.get("dueDate") if due_date is None else due_date or None,
                    "level": new_level,
                    "notApplicable": new_na,
                    "comment": new_comment,
                    "updatedAt": current.get("updatedAt"),
                },
                mutation=True, tenant_id=tenant, client=client,
            )
            if getattr(err, "reason", None) == "STALE_WRITE":
                return (
                    f"Error: someone changed requirement {requirement_id} of "
                    f"compliance assessment {assessment_id} since it was read, "
                    "so nothing was saved. Read it again "
                    "(get_opencrq_compliance_assessment with "
                    "include_requirements) and, with the user's confirmation, "
                    "retry."
                )
            if getattr(err, "reason", None) == "ASSESSMENT_PUBLISHED":
                return (
                    f"Error: compliance assessment {assessment_id} is published, so "
                    "its answers can no longer change and nothing was saved. Reopen "
                    "it with manage_opencrq_compliance_assessment (action=reopen), "
                    "with the user's confirmation, before answering."
                )
            if err:
                return err
        answer = (
            "not applicable"
            if new_na
            else f"level {new_level}" if new_level is not None else "not answered"
        )
        return (
            f"Recorded requirement {requirement_id} of compliance assessment "
            f"{assessment_id} as {answer}"
            + (", with a comment" if new_comment else "")
            + (
                f", owner {owner_id or 'removed'}"
                if owner_id is not None
                else ""
            )
            + (
                f", due date {due_date or 'removed'}"
                if due_date is not None
                else ""
            )
            + "."
        )

    executor.register(
        "list_opencrq_compliance_assessments", list_opencrq_compliance_assessments
    )
    executor.register(
        "get_opencrq_compliance_assessment", get_opencrq_compliance_assessment
    )
    executor.register(
        "create_opencrq_compliance_assessment", create_opencrq_compliance_assessment
    )
    executor.register(
        "manage_opencrq_compliance_assessment", manage_opencrq_compliance_assessment
    )
    executor.register("update_opencrq_requirement_answer", update_opencrq_requirement_answer)

    _tenant_prop = TENANT_PROP

    return [
        ToolDefinition(
            name="list_opencrq_compliance_assessments",
            description=(
                "List the compliance assessments of an OpenCRQ tenant with "
                "their framework, draft or published status, how many "
                "requirements are answered on a level (0-5) or marked not "
                "applicable, and the mean level, 20 at a time."
            ),
            input_schema={"type": "object", "properties": {**OFFSET_PROP, **_tenant_prop}},
            source=_SOURCE,
        ),
        ToolDefinition(
            name="get_opencrq_compliance_assessment",
            description=(
                "Get an OpenCRQ compliance assessment's detail. Set "
                "include_requirements=true to list every requirement with "
                "its level (0-5), not applicable or unanswered state, owner "
                "and comment (useful for gap analysis), and "
                "include_control_links=true for the OpenCRQ control "
                "capabilities each requirement's answer feeds. Each list "
                "shows 100 items at a time, from offset."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "assessment_id": {
                        "type": "string",
                        "description": "Compliance assessment UUID.",
                    },
                    "include_requirements": {
                        "type": "boolean",
                        "description": "Include the per-requirement answers.",
                        "default": False,
                    },
                    "include_control_links": {
                        "type": "boolean",
                        "description": "Include the requirement-to-control links.",
                        "default": False,
                    },
                    **OFFSET_PROP,
                    **_tenant_prop,
                },
                "required": ["assessment_id"],
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="create_opencrq_compliance_assessment",
            description=(
                "Create a new draft compliance assessment of an OpenCRQ "
                "tenant against an assessment-catalog framework. Confirm "
                "with the user before creating."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "framework_id": {
                        "type": "string",
                        "description": (
                            "Assessment-catalog framework UUID "
                            "(list_opencrq_frameworks)."
                        ),
                    },
                    "name": {
                        "type": "string",
                        "description": "Assessment name (1-200 characters).",
                    },
                    **_tenant_prop,
                },
                "required": ["framework_id", "name"],
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="manage_opencrq_compliance_assessment",
            description=(
                "Rename a draft OpenCRQ compliance assessment, publish it "
                "(its answers can no longer change and feed the control "
                "capabilities) or reopen a published one as a draft. "
                "Confirm with the user before applying."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": _ASSESSMENT_ACTIONS,
                    },
                    "assessment_id": {
                        "type": "string",
                        "description": "Compliance assessment UUID.",
                    },
                    "name": {
                        "type": "string",
                        "description": "New name, required for rename.",
                    },
                    **_tenant_prop,
                },
                "required": ["action", "assessment_id"],
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="update_opencrq_requirement_answer",
            description=(
                "Answer one requirement of a draft OpenCRQ compliance "
                "assessment: a level from 0 to 5, or not applicable, with "
                "an optional comment, or clear=true to make it unanswered "
                "again; owner_id and due_date assign the requirement. Fields "
                "you omit keep the value read just before writing, and a change "
                "made by someone else in between is refused rather than "
                "overwritten. Confirm with the user before answering."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "assessment_id": {
                        "type": "string",
                        "description": "Compliance assessment UUID.",
                    },
                    "requirement_id": {
                        "type": "string",
                        "description": (
                            "Requirement UUID "
                            "(get_opencrq_compliance_assessment with "
                            "include_requirements)."
                        ),
                    },
                    "level": {
                        "type": "integer",
                        "minimum": 0,
                        "maximum": 5,
                        "description": "Level answered, 0 to 5.",
                    },
                    "not_applicable": {
                        "type": "boolean",
                        "description": "Mark the requirement not applicable (excludes level).",
                    },
                    "clear": {
                        "type": "boolean",
                        "description": (
                            "Remove the level or the not applicable mark, so the "
                            "requirement is unanswered again (excludes level and "
                            "not_applicable)."
                        ),
                    },
                    "comment": {
                        "type": "string",
                        "description": "Comment on the answer; an empty string clears it.",
                    },
                    "owner_id": {
                        "type": "string",
                        "description": (
                            "UUID of the user who owns the requirement "
                            "(list_opencrq_tenant_users); an empty string removes the owner."
                        ),
                    },
                    "due_date": {
                        "type": "string",
                        "description": "Due date as YYYY-MM-DD; an empty string removes it.",
                    },
                    **_tenant_prop,
                },
                "required": ["assessment_id", "requirement_id"],
            },
            source=_SOURCE,
        ),
    ]
