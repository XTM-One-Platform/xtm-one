# SPDX-License-Identifier: Apache-2.0

"""OpenAEV atomic testing + payload management tools.

* ``manage_openaev_atomic_testing`` - one-off inject tests: search/get/create/
  update/delete and launch/relaunch/stop/duplicate (``AtomicTestingApi``).
  Launch and relaunch EXECUTE against the targeted assets; stop ends a
  running test (``POST /api/atomic-testings/{id}/stop``).
* ``manage_openaev_payload`` - payload management gaps: list/get/update/delete/
  duplicate/upsert (``PayloadApi``). Payload creation with injector-contract
  polling stays in the core ``create_openaev_payload`` tool.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from app.tools.base import CallableToolExecutor, ToolDefinition

from ._common import (
    FINDINGS_KEPT_AS_HISTORY,
    _api,
    _normalize_output_parsers,
    _relation_ids,
    _search_body,
    inject_targets_line,
    json_answer,
    mirror_content_targets,
    stored_targets_line,
)
from .read_scope import area_scope, count_text, with_scope

logger = logging.getLogger(__name__)

_SOURCE = "integration:openaev"

# OpenAEV parses a recurrence with cron-utils CronType.SPRING53 (6 fields,
# seconds first, no year) or as an ISO 8601 period, and computes the next run
# in UTC (CronService.getNextOccurrence). PUT /recurrence stores any non-blank
# string, so a 5-field Unix or 7-field Quartz expression is saved and never
# fires: it is refused here before the write (xtm-one #242).
_SPRING_CRON_FIELDS = 6


def recurrence_refusal(recurrence: str) -> str | None:
    """Why OpenAEV would never fire ``recurrence``, or None when it is usable."""
    value = recurrence.strip()
    if value.startswith(("P", "@")):
        return None
    fields = value.split()
    if len(fields) == _SPRING_CRON_FIELDS:
        return None
    shape = {5: "a 5-field Unix cron", 7: "a 7-field Quartz cron"}.get(len(fields), f"{len(fields)} field(s)")
    return (
        f"Refused: recurrence '{value}' is {shape}. OpenAEV stores it without "
        "error but never fires it. Send a 6-field Spring cron, seconds first "
        "(sec min hour day-of-month month day-of-week, e.g. '0 0 2 * * *' for "
        "every day at 02:00 UTC), or an ISO 8601 period such as 'P1D'. The "
        "schedule is evaluated in UTC. Nothing was written."
    )


def recurrence_answer(inject_id: str, recurrence: str, start: str, end: str) -> str:
    """What the recurrence call stored, and what it cleared."""
    cleared = [
        name
        for name, value in (("recurrence", recurrence), ("recurrence_start", start), ("recurrence_end", end))
        if not value
    ]
    if not recurrence:
        head = f"Cleared the schedule of atomic test {inject_id}: no recurrence was sent."
    else:
        head = f"Set the recurrence of atomic test {inject_id} to '{recurrence}' (UTC)"
        head += f", start {start}" if start else ""
        head += f", end {end}" if end else ""
        head += "."
    if cleared:
        head += (
            " The call replaces the three fields together; cleared: "
            + ", ".join(cleared)
            + ". Send all three to keep a field."
        )
    return (
        head + " Every scheduled run is a relaunch: previous results are archived, "
        "new expectations are created for all targets and the atomic test gets "
        "a NEW inject id, so find it again by title with 'list' after a run."
    )


#: The relations of a stored inject an atomic-test update carries over, as
#: (AtomicTestingInput key, id key of an element when it is an object).
_ATOMIC_RELATIONS: tuple[tuple[str, str], ...] = (
    ("inject_assets", "asset_id"),
    ("inject_asset_groups", "asset_group_id"),
    ("inject_teams", "team_id"),
    ("inject_tags", "tag_id"),
)

#: Relations newer OpenAEV versions carry: kept when the stored inject names
#: them, left out when the answer has no such key (a version whose input
#: has no such field either).
_ATOMIC_OPTIONAL_RELATIONS: tuple[tuple[str, str], ...] = (("inject_secret_references", "secret_reference_id"),)


def _contract_id(raw: Any) -> str:
    if isinstance(raw, dict):
        return str(raw.get("injector_contract_id") or "")
    return raw if isinstance(raw, str) else ""


def _stored_documents(raw: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for doc in raw if isinstance(raw, list) else []:
        if isinstance(doc, dict) and doc.get("document_id"):
            out.append(
                {
                    "document_id": str(doc["document_id"]),
                    "document_attached": bool(doc.get("document_attached", True)),
                }
            )
    return out


def stored_atomic_input(stored: Any, changes: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """The full AtomicTestingInput of an update: the stored inject, ``changes`` laid over it.

    OpenAEV's PUT /api/atomic-testings/{id} replaces every field: a title left
    out is refused, a contract left out fails its lookup, a list left out is
    stored empty. Every field the caller did not change is therefore read
    back from the stored inject (GET /api/injects/{id}). Returns
    ``(body, refusal)``; a refusal names what the stored answer lacks so the
    update never wipes it. Nothing is written when it is not empty.
    """
    if not isinstance(stored, dict):
        return {}, (
            "Refused: the stored atomic test could not be read as an object, so "
            "an update would wipe its fields. Nothing was written."
        )
    body: dict[str, Any] = {
        "inject_title": stored.get("inject_title") or "",
        "inject_description": stored.get("inject_description"),
        "inject_injector_contract": _contract_id(stored.get("inject_injector_contract")),
        "inject_content": stored.get("inject_content"),
        "inject_all_teams": bool(stored.get("inject_all_teams", False)),
    }
    # The stored injector belongs to the stored contract: a new contract lets
    # OpenAEV resolve the injector from it.
    injector = stored.get("inject_injector")
    if isinstance(injector, str) and injector and "inject_injector_contract" not in changes:
        body["inject_injector"] = injector
    missing: list[str] = []
    for key, id_key in _ATOMIC_RELATIONS:
        if key in changes:
            continue
        if key not in stored:
            missing.append(key)
            continue
        body[key] = _relation_ids(stored.get(key), id_key)
    for key, id_key in _ATOMIC_OPTIONAL_RELATIONS:
        if key in stored:
            body[key] = _relation_ids(stored.get(key), id_key)
    if "inject_documents" in stored:
        raw_documents = stored.get("inject_documents")
        documents = _stored_documents(raw_documents)
        if isinstance(raw_documents, list) and len(documents) < len(raw_documents):
            missing.append("the document ids of inject_documents")
        body["inject_documents"] = documents
    body.update(changes)
    if not body["inject_title"]:
        missing.append("inject_title (pass title)")
    if not body["inject_injector_contract"]:
        missing.append("inject_injector_contract (pass injector_contract_id)")
    if missing:
        return {}, (
            "Refused: OpenAEV replaces every field of an atomic test on update, "
            "and the stored atomic test as read lacks "
            + ", ".join(missing)
            + ", so the update would wipe it. Nothing was written."
        )
    return body, ""


def readiness_line(answer: Any) -> str:
    """Why OpenAEV holds an atomic test back from launch, from its read or write answer.

    OpenAEV (from openaev #9569) lists the contract fields left empty under
    ``inject_missing_mandatory_fields`` and refuses the launch with a 400
    naming them; an older version only answers ``inject_ready`` false. A ready
    test, or an answer without ``inject_ready``, adds no line.
    """
    if not isinstance(answer, dict) or answer.get("inject_ready") is not False:
        return ""
    missing = answer.get("inject_missing_mandatory_fields")
    fields = [str(f) for f in missing if f] if isinstance(missing, list) else []
    if fields:
        return (
            "NOT READY: mandatory field(s) "
            + ", ".join(fields)
            + " of its injector contract are empty, so a launch is refused (it "
            "would run and reach no target). Set them with 'update' (content "
            "JSON, or asset_ids / asset_group_ids for the targets)."
        )
    return (
        "NOT READY: OpenAEV reports this atomic test not ready to launch and "
        "this version does not name the field; compare its content with the "
        "mandatory fields of its injector contract."
    )


def contract_mandatory_fields(contract: Any) -> list[str]:
    """``key [type]`` of each mandatory field an injector contract declares.

    Read from ``injector_contract_content`` (a JSON string or an object); a
    field mandatory only under a condition, or in a group of alternatives, is
    marked so. Absent or malformed content answers an empty list.
    """
    content = contract.get("injector_contract_content") if isinstance(contract, dict) else None
    if isinstance(content, str):
        try:
            content = json.loads(content)
        except (TypeError, ValueError):
            return []
    if not isinstance(content, dict) or not isinstance(content.get("fields"), list):
        return []
    out: list[str] = []
    for field in content["fields"]:
        if not isinstance(field, dict) or not field.get("key"):
            continue
        conditional = bool(field.get("mandatoryConditionFields"))
        groups = field.get("mandatoryGroups")
        if not (field.get("mandatory") or conditional or (isinstance(groups, list) and groups)):
            continue
        item = f"{field['key']} [{field.get('type', '?')}]"
        if conditional:
            item += " (when its condition holds)"
        elif isinstance(groups, list) and groups and not field.get("mandatory"):
            item += " (one of: " + ", ".join(str(g) for g in groups) + ")"
        out.append(item)
    return out


def register(
    base_url: str, headers: dict[str, str], executor: CallableToolExecutor
) -> list[ToolDefinition]:
    async def manage_openaev_atomic_testing(
        action: str = "",
        inject_id: str = "",
        title: str = "",
        description: str = "",
        injector_contract_id: str = "",
        asset_ids: list[str] | None = None,
        asset_group_ids: list[str] | None = None,
        content: str = "",
        recurrence: str = "",
        recurrence_start: str = "",
        recurrence_end: str = "",
        search: str = "",
        limit: int = 20,
    ) -> str:
        action = (action or "").strip().lower()

        async def _stored_inject(iid: str) -> object:
            # The atomic-testing routes answer InjectResultOverviewOutput, which
            # carries no inject_assets / inject_asset_groups: the stored
            # relations are read from the inject itself (a read, never a write).
            if not iid:
                return None
            resp, err = await _api(
                base_url,
                headers,
                "get",
                f"/api/injects/{iid}",
                "read the stored targets of the atomic test",
                retry_transient=True,
            )
            if err or resp is None:
                return None
            try:
                return resp.json()
            except ValueError:
                return None

        def _atomic_body(for_create: bool) -> dict:
            # The fields the caller provided. On 'create' the full atomic-test
            # shape the API expects is filled in (atomic tests have no teams);
            # on 'update' these fields are laid over the stored inject by
            # stored_atomic_input, since OpenAEV's PUT replaces every field.
            body: dict = {}
            if title:
                body["inject_title"] = title
            if description:
                body["inject_description"] = description
            if injector_contract_id:
                body["inject_injector_contract"] = injector_contract_id
            if asset_ids is not None:
                body["inject_assets"] = asset_ids
            if asset_group_ids is not None:
                body["inject_asset_groups"] = asset_group_ids
            if content:
                try:
                    body["inject_content"] = json.loads(content)
                except json.JSONDecodeError:
                    body["inject_content"] = {"content": content}
            if for_create:
                body.setdefault("inject_assets", [])
                body.setdefault("inject_asset_groups", [])
                body.setdefault("inject_content", {})
                body["inject_teams"] = []
                body["inject_all_teams"] = False
            return body

        if action == "list":
            resp, err = await _api(
                base_url, headers, "post", "/api/atomic-testings/search",
                "search atomic tests",
                json=_search_body(size=min(limit, 50), text_search=search or None),
            )
            if err:
                return err
            data = resp.json()
            items = data.get("content", data) if isinstance(data, dict) else data
            if not items:
                # OpenAEV answers an empty page to a grant-limited identity (#1444).
                scope, cause = await area_scope(headers, "assessment")
                return with_scope(
                    "No atomic tests found" + (": " + count_text(0, scope) if scope != "full" else "") + ".",
                    scope,
                    cause,
                )
            lines = [f"Atomic tests ({len(items)}):"]
            for i in items:
                lines.append(
                    f"- {i.get('inject_title', '?')} (id: {i.get('inject_id')})"
                )
            return "\n".join(lines)

        if action == "get":
            if not inject_id:
                return "inject_id is required for action 'get'."
            resp, err = await _api(
                base_url, headers, "get", f"/api/atomic-testings/{inject_id}",
                "get atomic test",
            )
            if err:
                return err
            answer = resp.json() if resp is not None else {}
            stored = await _stored_inject(inject_id)
            # A read adds the line only from relations it could read: no claim
            # when the inject read failed.
            head = [t for t in (readiness_line(answer), inject_targets_line(stored)) if t]
            return "\n".join([*head, json_answer(answer)])

        if action in ("create", "update"):
            if action == "create" and (not title or not injector_contract_id):
                return "title and injector_contract_id are required for 'create'."
            if action == "update" and not inject_id:
                return "inject_id is required for 'update'."
            body = _atomic_body(for_create=action == "create")
            targets_note = ""
            if content:
                assets, groups, targets_note, targets_err = mirror_content_targets(
                    body.get("inject_content"), asset_ids, asset_group_ids
                )
                if targets_err:
                    return targets_err
                if assets is not None:
                    body["inject_assets"] = assets
                if groups is not None:
                    body["inject_asset_groups"] = groups
            if action == "update" and not body:
                return (
                    "Provide at least one field to update (title, description, "
                    "injector_contract_id, asset_ids, asset_group_ids, or content)."
                )
            if action == "update":
                # OpenAEV's PUT /api/atomic-testings/{id} replaces every field
                # (AtomicTestingInput: a blank title is refused, an omitted list
                # is stored empty), so the stored inject is read first and the
                # caller's fields are laid over it. GET /api/injects/{id} carries
                # the relations; the atomic-testing read does not.
                stored, err = await _api(
                    base_url,
                    headers,
                    "get",
                    f"/api/injects/{inject_id}",
                    "read the stored atomic test before the update",
                )
                if err:
                    return err
                body, refusal = stored_atomic_input(stored.json() if stored is not None else None, body)
                if refusal:
                    return refusal
            if action == "create":
                resp, err = await _api(
                    base_url, headers, "post", "/api/atomic-testings",
                    "create atomic test", json=body,
                )
            else:
                resp, err = await _api(
                    base_url, headers, "put", f"/api/atomic-testings/{inject_id}",
                    "update atomic test", json=body,
                )
            if err:
                return err
            i = resp.json()
            if not isinstance(i, dict):
                i = {}
            verb = "Created" if action == "create" else "Updated"
            lines = [f"{verb} atomic test '{i.get('inject_title')}' (id: {i.get('inject_id')})."]
            stored = await _stored_inject(str(i.get("inject_id") or inject_id or ""))
            targets = stored_targets_line(i, stored, body.get("inject_assets"), body.get("inject_asset_groups"))
            lines += [t for t in (targets_note, targets, readiness_line(i)) if t]
            if action == "create":
                contract_id = str(body.get("inject_injector_contract") or "")
                read, read_err = await _api(
                    base_url,
                    headers,
                    "get",
                    f"/api/injector_contracts/{contract_id}",
                    "read the mandatory fields of the injector contract",
                    retry_transient=True,
                )
                contract: Any = None
                if not read_err and read is not None:
                    try:
                        contract = read.json()
                    except ValueError:
                        contract = None
                mandatory = contract_mandatory_fields(contract)
                if mandatory:
                    lines.append("Mandatory fields of its injector contract: " + "; ".join(mandatory) + ".")
                elif contract is None:
                    lines.append(
                        "Mandatory fields of its injector contract: not readable "
                        "(the contract read failed); read them with "
                        "get_openaev_injector_contract."
                    )
            return "\n".join(lines)

        if action == "delete":
            if not inject_id:
                return "inject_id is required for action 'delete'."
            _, err = await _api(
                base_url, headers, "delete", f"/api/atomic-testings/{inject_id}",
                "delete atomic test",
            )
            if err:
                return err
            return (
                f"Deleted atomic test {inject_id}. Its queued agent jobs went with "
                "it, so a job not yet fetched never runs; an execution an agent or "
                "injector already picked up is NOT stopped by the delete. "
                + FINDINGS_KEPT_AS_HISTORY
            )

        if action in ("launch", "relaunch", "duplicate"):
            if not inject_id:
                return f"inject_id is required for action '{action}'."
            resp, err = await _api(
                base_url, headers, "post",
                f"/api/atomic-testings/{inject_id}/{action}",
                f"{action} atomic test",
            )
            if err:
                # OpenAEV's own reason is part of err (a not-ready test is
                # refused with a 400 naming the empty mandatory fields).
                return err
            i = resp.json() if resp.content else {}
            if action == "duplicate":
                return f"Duplicated atomic test - new id: {i.get('inject_id')}."
            note = (
                " Each execution archives the previous results and creates fresh "
                "expectations for every target, so read the results again "
                "afterwards rather than reusing earlier verdicts."
            )
            if action == "relaunch":
                new_id = i.get("inject_id") if isinstance(i, dict) else None
                moved = (
                    f" OpenAEV relaunches a copy and deletes the original: the test "
                    f"is now inject {new_id}, use that id from here on."
                    if new_id and new_id != inject_id
                    else ""
                )
                return (
                    f"Relaunched atomic test {inject_id}.{moved} An execution of the "
                    "previous run that an agent or injector already picked up is "
                    f"NOT stopped.{note}"
                )
            return f"Launched atomic test {inject_id}.{note}"

        if action == "recurrence":
            if not inject_id:
                return "inject_id is required for action 'recurrence'."
            recurrence = (recurrence or "").strip()
            body: dict = {}
            if recurrence:
                refusal = recurrence_refusal(recurrence)
                if refusal:
                    return refusal
                body["inject_recurrence"] = recurrence
            if recurrence_start:
                body["inject_recurrence_start"] = recurrence_start
            if recurrence_end:
                body["inject_recurrence_end"] = recurrence_end
            if not body:
                return (
                    "Provide at least one field for 'recurrence': recurrence (a "
                    "6-field Spring cron, seconds first, or an ISO 8601 period, "
                    "evaluated in UTC), recurrence_start or recurrence_end (ISO "
                    "timestamps). The call replaces the three together: a field "
                    "left out is cleared, so a call without recurrence clears "
                    "the schedule."
                )
            resp, err = await _api(
                base_url, headers, "put",
                f"/api/atomic-testings/{inject_id}/recurrence",
                "set atomic test recurrence", json=body,
            )
            if err:
                return err
            return recurrence_answer(inject_id, recurrence, recurrence_start, recurrence_end)

        if action in ("stop", "cancel", "abort"):
            if not inject_id:
                return f"inject_id is required for action '{action}'."
            _, err = await _api(
                base_url,
                headers,
                "post",
                f"/api/atomic-testings/{inject_id}/stop",
                "stop atomic test",
            )
            if err:
                # OpenAEV answers 400 with its reason when the test is not
                # running (queuing, executing or pending); an instance older
                # than the stop route answers 404/405.
                return (
                    f"{err}\nOnly a queuing, executing or pending atomic test can be "
                    "stopped. On an OpenAEV without the stop route, 'delete' the "
                    "test to keep a job not yet fetched from running."
                )
            return (
                f"Stopped atomic test {inject_id}: the agent jobs not picked up yet "
                "are withdrawn and the test ends in ERROR with a trace naming who "
                "stopped it. A command an agent already started is NOT interrupted "
                "on the endpoint."
            )

        return (
            f"Unknown action '{action}'. Valid actions: list, get, create, update, "
            "delete, launch, relaunch, stop, duplicate, recurrence."
        )

    async def manage_openaev_payload(
        action: str = "",
        payload_id: str = "",
        name: str = "",
        description: str = "",
        config: str = "",
        search: str = "",
        limit: int = 20,
    ) -> str:
        action = (action or "").strip().lower()

        extra: dict = {}
        if config:
            try:
                extra = json.loads(config)
            except ValueError as exc:
                return f"config must be a valid JSON object: {exc}"
            if not isinstance(extra, dict):
                return "config must be a JSON object of field->value pairs."

        if action == "list":
            resp, err = await _api(
                base_url, headers, "post", "/api/payloads/search", "search payloads",
                json=_search_body(size=min(limit, 50), text_search=search or None),
            )
            if err:
                return err
            data = resp.json()
            items = data.get("content", data) if isinstance(data, dict) else data
            if not items:
                return "No payloads found."
            lines = [f"Payloads ({len(items)}):"]
            for p in items:
                lines.append(
                    f"- {p.get('payload_name', '?')} [{p.get('payload_type', '?')}] "
                    f"(id: {p.get('payload_id')})"
                )
            return "\n".join(lines)

        if action == "get":
            if not payload_id:
                return "payload_id is required for action 'get'."
            resp, err = await _api(
                base_url, headers, "get", f"/api/payloads/{payload_id}", "get payload"
            )
            if err:
                return err
            return json_answer(resp.json())

        if action == "update":
            if not payload_id:
                return "payload_id is required for action 'update'."
            body = dict(extra)
            if name:
                body["payload_name"] = name
            if description:
                body["payload_description"] = description
            if not body:
                return "Provide name, description, or config fields to update."
            parser_err = _normalize_output_parsers(body)
            if parser_err:
                return parser_err
            # The payload PUT is a full replace: any field the caller omits is nulled. Platforms
            # are mandatory (a payload with no platform cannot run), and omitting them used to null
            # them and break contract rebuilding, so carry the payload's current platforms forward
            # whenever the update did not set them explicitly.
            if not body.get("payload_platforms"):
                current, cur_err = await _api(
                    base_url, headers, "get", f"/api/payloads/{payload_id}",
                    "get payload",
                )
                if cur_err:
                    return cur_err
                existing = current.json().get("payload_platforms") or []
                if existing:
                    body["payload_platforms"] = existing
            resp, err = await _api(
                base_url, headers, "put", f"/api/payloads/{payload_id}",
                "update payload", json=body,
            )
            if err:
                return err
            return f"Updated payload {payload_id}."

        if action == "delete":
            if not payload_id:
                return "payload_id is required for action 'delete'."
            _, err = await _api(
                base_url, headers, "delete", f"/api/payloads/{payload_id}",
                "delete payload",
            )
            if err:
                return err
            return f"Deleted payload {payload_id}."

        if action == "duplicate":
            if not payload_id:
                return "payload_id is required for action 'duplicate'."
            resp, err = await _api(
                base_url, headers, "post", f"/api/payloads/{payload_id}/duplicate",
                "duplicate payload",
            )
            if err:
                return err
            p = resp.json()
            return f"Duplicated payload - new id: {p.get('payload_id')}."

        if action == "upsert":
            body = dict(extra)
            if name:
                body["payload_name"] = name
            if description:
                body["payload_description"] = description
            if not body:
                return "Provide config (and/or name) for 'upsert'."
            parser_err = _normalize_output_parsers(body)
            if parser_err:
                return parser_err
            resp, err = await _api(
                base_url, headers, "post", "/api/payloads/upsert", "upsert payload",
                json=body,
            )
            if err:
                return err
            p = resp.json()
            return f"Upserted payload '{p.get('payload_name')}' (id: {p.get('payload_id')})."

        return (
            f"Unknown action '{action}'. Valid actions: list, get, update, delete, "
            "duplicate, upsert. (To create a payload use create_openaev_payload.)"
        )

    executor.register("manage_openaev_atomic_testing", manage_openaev_atomic_testing)
    executor.register("manage_openaev_payload", manage_openaev_payload)

    return [
        ToolDefinition(
            name="manage_openaev_atomic_testing",
            description=(
                "Manage OpenAEV atomic tests (one-off inject executions). Actions: "
                "'list' (search), 'get' (inject_id), 'create' (title + "
                "injector_contract_id, optional description/asset_ids/asset_group_ids/"
                "content JSON), 'update' (inject_id + the fields to change; every "
                "field left out keeps its stored value, asset_ids=[] clears the "
                "assets), 'delete' (inject_id; its findings stay as history), "
                "'launch' / 'relaunch' (inject_id - EXECUTES the test against the "
                "targeted assets), 'duplicate' (inject_id), 'recurrence' "
                "(inject_id + recurrence, recurrence_start, recurrence_end: send "
                "all three, a field left out is cleared; each scheduled run gives "
                "the test a NEW inject id). A test whose mandatory contract "
                "fields are empty is NOT READY: create, update and get name the "
                "fields and launch is refused. Every launch, "
                "relaunch or scheduled run ARCHIVES the previous results and "
                "creates fresh expectations for all targets, so re-read the "
                "results afterwards instead of reusing earlier verdicts. "
                "'stop' (inject_id) ends a queuing, executing or pending test: "
                "jobs not yet fetched are withdrawn, a command already started "
                "is not interrupted. 'relaunch' runs a copy under a NEW inject "
                "id and deletes the original."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": [
                            "list", "get", "create", "update", "delete", "launch",
                            "relaunch", "stop", "duplicate", "recurrence",
                        ],
                    },
                    "inject_id": {"type": "string"},
                    "title": {"type": "string"},
                    "description": {"type": "string"},
                    "injector_contract_id": {"type": "string"},
                    "asset_ids": {"type": "array", "items": {"type": "string"}},
                    "asset_group_ids": {"type": "array", "items": {"type": "string"}},
                    "content": {"type": "string", "description": "Inject content as a JSON object string."},
                    "recurrence": {
                        "type": "string",
                        "description": "6-field Spring cron, seconds first ('0 0 2 * * *' = daily 02:00 UTC), or ISO 8601 period ('P1D'); UTC. Empty clears the schedule.",
                    },
                    "recurrence_start": {
                        "type": "string",
                        "description": "ISO timestamp when the recurrence starts (cleared when left out).",
                    },
                    "recurrence_end": {
                        "type": "string",
                        "description": "ISO timestamp when the recurrence ends (cleared when left out).",
                    },
                    "search": {"type": "string"},
                    "limit": {"type": "integer", "default": 20},
                },
                "required": ["action"],
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="manage_openaev_payload",
            description=(
                "Manage OpenAEV payloads directly (the raw payload record). "
                "PREFER manage_openaev_threat_arsenal: OpenAEV v3 manages attack "
                "content as Threat Arsenal 'actions' (payload + injector contract) "
                "and this payload API is deprecated for removal - use this tool "
                "only for payload-level fields the arsenal tool does not expose. "
                "Actions: 'list' (search), 'get' (payload_id), 'update' "
                "(payload_id + name/description and/or config JSON of payload "
                "fields), 'delete' (payload_id), 'duplicate' (payload_id), "
                "'upsert' (config JSON, optional name/description). Type-specific "
                "payload fields go in the 'config' JSON object (e.g. "
                "command_content, executable_file, dns_resolution_hostname, "
                "payload_platforms, payload_tags, payload_expectations)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["list", "get", "update", "delete", "duplicate", "upsert"],
                    },
                    "payload_id": {"type": "string"},
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "config": {"type": "string", "description": "JSON object of payload fields (update/upsert)."},
                    "search": {"type": "string"},
                    "limit": {"type": "integer", "default": 20},
                },
                "required": ["action"],
            },
            source=_SOURCE,
        ),
    ]
