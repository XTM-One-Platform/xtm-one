# SPDX-License-Identifier: Apache-2.0

"""Hard object-type boundary for the HubSpot tools.

The CRM object types an operator selects in an agent's or an assignment's
Scope(s) (``integration_element_bindings["hubspot:crm_object_type"]``) used to
be prompt text only: the HubSpot tools reached every object type the private
app token could. With an object-type scope in the run's effective scope
(``services.element_scope``), ``build_tools`` passes the tools through
:func:`apply_object_type_scope`, the same shape as the GitHub repository
guard (``providers/github/scope.py``):

* the tools bound to one object type (contacts, companies, deals, tickets) are
  withdrawn when that type is not selected - they cannot be used on anything
  else;
* a deal's line items go with the deal type;
* the generic tools that name object types - associations (both ends),
  ``delete_hubspot_record`` and the record attachment tools - and the
  ``associations`` argument of the record reads are refused before any
  request when a type is outside the selection (or not named), naming the
  allowed types. ``contacts``, ``contact`` and ``0-1`` are the same type
  (likewise for every standard object); a custom object is named by its
  ``objectTypeId`` (``2-...``), as the Scope(s) picker keys it;
* marketing email campaigns and the file manager (file search and upload)
  are not a CRM object type and are withdrawn; a file is downloaded through
  the record of an allowed type it is attached to (``download_hubspot_file``
  checks the attachment itself);
* engagements (emails, notes, calls, meetings, tasks, communications) are the
  activity of a record: they are read through a record of an allowed type -
  its timeline, an engagement search filtered on it, or its associations of an
  engagement type - and ``get_hubspot_engagement`` refuses one associated with
  no record of an allowed type;
* forms and their submissions, sequences and the inbound report are contact
  data and go with the contact type.

Owners, teams, pipeline and property definitions stay available: they
describe the account, not the records of any object type. A selection with no
usable type allows nothing.
"""

from __future__ import annotations

import functools
import logging
from collections.abc import Mapping
from typing import Any

from app.integrations.providers.hubspot_records import engagement_type
from app.tools.base import CallableToolExecutor, ToolDefinition, ToolResult

logger = logging.getLogger(__name__)

#: Element-binding key of a HubSpot object-type scope (``integration_elements``).
HUBSPOT_OBJECT_TYPE_SCOPE_KEY = "hubspot:crm_object_type"

#: Standard CRM objects: the element ID the picker writes (singular) and the
#: spellings the HubSpot API accepts for it (plural name, ``objectTypeId``).
_STANDARD_TYPES: dict[str, tuple[str, ...]] = {
    "contact": ("contacts", "0-1"),
    "company": ("companies", "0-2"),
    "deal": ("deals", "0-3"),
    "ticket": ("tickets", "0-5"),
    "product": ("products", "0-7"),
    "line_item": ("line_items", "0-8"),
    "quote": ("quotes", "0-14"),
}
_ALIASES = {alias: canonical for canonical, aliases in _STANDARD_TYPES.items() for alias in (canonical, *aliases)}

#: Tools bound to one object type.
TOOL_OBJECT_TYPE = {
    "get_hubspot_contact": "contact",
    "search_hubspot_contacts": "contact",
    "create_hubspot_contact": "contact",
    "update_hubspot_contact": "contact",
    "get_hubspot_company": "company",
    "search_hubspot_companies": "company",
    "create_hubspot_company": "company",
    "update_hubspot_company": "company",
    "list_hubspot_deals": "deal",
    "get_hubspot_deal": "deal",
    "search_hubspot_deals": "deal",
    "create_hubspot_deal": "deal",
    "update_hubspot_deal": "deal",
    # A deal's line items go with the deal type: they are its products and make its ACV and TCV.
    "list_hubspot_line_items": "deal",
    "create_hubspot_line_item": "deal",
    "update_hubspot_line_item": "deal",
    "delete_hubspot_line_item": "deal",
    "search_hubspot_tickets": "ticket",
    "get_hubspot_ticket": "ticket",
    "create_hubspot_ticket": "ticket",
    "update_hubspot_ticket": "ticket",
    "list_hubspot_forms": "contact",
    "list_hubspot_form_submissions": "contact",
    "get_hubspot_contact_form_submissions": "contact",
    "list_hubspot_sequences": "contact",
    "get_hubspot_sequence_enrollments": "contact",
    "get_hubspot_inbound_engagement_report": "contact",
}

#: Tools outside any object-type selection.
DROPPED_WHEN_SCOPED = frozenset(
    {"list_hubspot_campaigns", "get_hubspot_campaign", "search_hubspot_files", "upload_hubspot_file"}
)

#: Arguments naming object types, per generic tool.
_TYPE_ARGUMENTS = {
    "get_hubspot_associations": ("object_type", "to_object_type"),
    "create_hubspot_association": ("from_type", "to_type"),
    "delete_hubspot_association": ("from_type", "to_type"),
    "delete_hubspot_record": ("object_type",),
    "list_hubspot_attachments": ("object_type",),
    "download_hubspot_file": ("object_type",),
    "upload_hubspot_attachment": ("object_type",),
    "create_hubspot_note": ("object_type",),
    "create_hubspot_task": ("object_type",),
    "get_hubspot_record_timeline": ("object_type",),
    "search_hubspot_engagements": ("associated_object_type",),
    "batch_get_hubspot_records": ("object_type",),
    "batch_get_hubspot_associations": ("object_type", "to_object_type"),
}

#: Arguments that may name an engagement type: the activity of the record the
#: tool reads, reachable when that record's type is allowed.
_ACTIVITY_ARGUMENTS = frozenset(
    {
        ("get_hubspot_associations", "to_object_type"),
        ("batch_get_hubspot_associations", "to_object_type"),
    }
)


def _is_engagement_type(value: Any) -> bool:
    return bool(engagement_type(value))


def canonical_object_type(value: Any) -> str:
    """The object type *value* names (``contacts`` -> ``contact``), or ``""``."""
    if not isinstance(value, str):
        return ""
    raw = value.strip().lower()
    return _ALIASES.get(raw, raw)


def api_object_type(value: Any) -> str:
    """The name a CRM API path takes for *value* (``contact`` / ``0-1`` -> ``contacts``)."""
    aliases = _STANDARD_TYPES.get(canonical_object_type(value))
    return aliases[0] if aliases else str(value or "").strip()


def object_type_scope_from_bindings(element_scope: Mapping[str, Any] | None) -> frozenset[str] | None:
    """The selected object types (canonical), or ``None``.

    *element_scope* is a run's EFFECTIVE scope (``services.element_scope``):
    ``None`` means no object-type scope was set. A selection with no usable
    entry - including the empty list a narrowing produces - is an EMPTY set
    that allows nothing, never no boundary at all.
    """
    if not isinstance(element_scope, Mapping):
        return None
    items = element_scope.get(HUBSPOT_OBJECT_TYPE_SCOPE_KEY)
    if items is None:
        return None
    if not isinstance(items, list):
        return frozenset()
    types = {
        canonical_object_type(item.get("id"))
        for item in items
        if isinstance(item, Mapping) and isinstance(item.get("id"), str) and item["id"].strip()
    }
    return frozenset(types - {""})


def allowed_text(scope: frozenset[str]) -> str:
    return ", ".join(sorted(scope)) if scope else "none (no valid object type is selected)"


def _outside(what: str, scope: frozenset[str]) -> str:
    return (
        f"Refused: HubSpot object type '{what}' is outside the Scope(s) set for this agent or "
        f"assignment. Allowed object types: {allowed_text(scope)}. Work only on those - do not "
        "retry with another object type."
    )


def _unnamed(argument: str, scope: frozenset[str]) -> str:
    return (
        f"Refused: pass {argument} - the Scope(s) set for this agent or assignment only allows "
        f"HubSpot object types: {allowed_text(scope)}."
    )


def _refusal(tool: str, kwargs: dict[str, Any], scope: frozenset[str]) -> str | None:
    for argument in _TYPE_ARGUMENTS.get(tool, ()):
        value = kwargs.get(argument)
        if not str(value or "").strip():
            return _unnamed(argument, scope)
        if (tool, argument) in _ACTIVITY_ARGUMENTS and _is_engagement_type(value):
            continue
        if canonical_object_type(value) not in scope:
            return _outside(str(value or "").strip(), scope)
    associations = kwargs.get("associations")
    if isinstance(associations, str) and associations.strip():
        for name in associations.split(","):
            if name.strip() and not _is_engagement_type(name) and canonical_object_type(name) not in scope:
                return _outside(name.strip(), scope)
    return None


def _guarded(tool: str, handler: Any, scope: frozenset[str]) -> Any:
    @functools.wraps(handler)
    async def guarded(*args: Any, **kwargs: Any) -> Any:
        refusal = _refusal(tool, kwargs, scope)
        if refusal:
            logger.info("HubSpot Scope(s): refused a %s call", tool)
            return ToolResult(tool_call_id="", content=refusal, is_error=True)
        return await handler(*args, **kwargs)

    return guarded


def apply_object_type_scope(
    definitions: list[ToolDefinition],
    executor: CallableToolExecutor,
    scope: frozenset[str],
) -> list[ToolDefinition]:
    """Enforce *scope* on the HubSpot tools just registered on *executor*."""
    kept: list[ToolDefinition] = []
    withdrawn = 0
    for definition in definitions:
        name = definition.name
        bound_type = TOOL_OBJECT_TYPE.get(name)
        if name in DROPPED_WHEN_SCOPED or (bound_type is not None and bound_type not in scope):
            executor.unregister(name)
            withdrawn += 1
            continue
        handler = executor.handler(name)
        if handler is not None and (name in _TYPE_ARGUMENTS or bound_type is not None):
            executor.register(name, _guarded(name, handler, scope))
        kept.append(definition)
    logger.info(
        "HubSpot Scope(s) enforced on object types %s: %d tool(s) withdrawn",
        allowed_text(scope),
        withdrawn,
    )
    return kept
