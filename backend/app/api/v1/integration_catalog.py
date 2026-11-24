# SPDX-License-Identifier: Apache-2.0

"""Provider catalog – describes what fields each integration provider type needs.

Extracted from ``integrations.py`` to keep the route file focused on HTTP handlers.
Everything here is pure data + lightweight helpers.

Exports:
    PROVIDER_CATALOG    – dict of provider key → configuration
    REGISTRATION_ONLY_PROVIDERS – providers only a platform registration creates
    CERTIFIED_PROVIDERS – providers XTM One qualifies as Certified
    is_registration_only_provider – whether a provider is registration-only here
    supported_auth_types – the auth types a catalog entry accepts (#8386)
    platform_base_url   – the base_url a platform_managed integration needs (#8386)
    ProviderTool        – Pydantic schema for tool metadata
    build_dummy_token   – dummy credential for provider tool introspection
    build_provider_tools – build ProviderTool lists from provider tool builders

Each tool's read / write ``tool_type`` comes from ``services/tool_classification.py``.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from app.api.v1.schemas.integration import ProviderTool
from app.core.config import settings
from app.integrations.providers.aikido.webhooks import EVENT_TYPES as AIKIDO_EVENT_TYPES
from app.integrations.providers.aikido.webhooks import TRIGGER_EVENTS as AIKIDO_TRIGGER_EVENTS
from app.integrations.providers.airwallex.webhooks import EVENT_TYPES as AIRWALLEX_EVENT_TYPES
from app.integrations.providers.airwallex.webhooks import TRIGGER_EVENTS as AIRWALLEX_TRIGGER_EVENTS
from app.integrations.providers.rydoo.webhooks import EVENT_TYPES as RYDOO_EVENT_TYPES
from app.integrations.providers.rydoo.webhooks import TRIGGER_EVENTS as RYDOO_TRIGGER_EVENTS
from app.integrations.providers.spendesk.webhooks import EVENT_TYPES as SPENDESK_EVENT_TYPES
from app.integrations.providers.spendesk.webhooks import TRIGGER_EVENTS as SPENDESK_TRIGGER_EVENTS
from app.services.dealhub_events import catalog_events as _dealhub_catalog_events
from app.services.opencrq_job_events import FILTER_KEYS as _OPENCRQ_FILTER_KEYS
from app.services.tool_classification import definition_tool_type
from app.services.wordsmith_webhooks import EVENTS as _WORDSMITH_EVENTS

logger = logging.getLogger(__name__)

#: The filter keys of every Pennylane event (services/pennylane_events.py).
_PENNYLANE_FILTER_KEYS: dict[str, str] = {
    "min_amount": "Only records of at least this amount in euros, tax included",
    "max_amount": "Only records of at most this amount in euros, tax included",
    "supplier_id": "Only records of these Pennylane supplier ids",
    "customer_id": "Only records of these Pennylane customer ids",
    "journal_id": "Only transactions of these journal ids",
    "bank_account_id": "Only transactions of these bank account ids",
    "status": "Only invoices in these statuses (payment_status for supplier invoices)",
    "currency": "Only records in these currencies (EUR, USD...)",
}
#: Each Pennylane event: (event, label, description).
_PENNYLANE_EVENTS: tuple[tuple[str, str, str], ...] = (
    (
        "supplier_invoice_created",
        "Supplier invoice created",
        "Triggers when a supplier invoice is added in Pennylane. Uses polling (every 5 minutes); "
        "the first poll starts from now.",
    ),
    (
        "supplier_invoice_updated",
        "Supplier invoice updated",
        "Triggers when a supplier invoice changes in Pennylane (one run per change). Uses polling "
        "(every 5 minutes); the first poll starts from now.",
    ),
    (
        "customer_invoice_created",
        "Customer invoice created",
        "Triggers when a customer invoice is created in Pennylane. Uses polling (every 5 minutes); "
        "the first poll starts from now.",
    ),
    (
        "customer_invoice_updated",
        "Customer invoice updated",
        "Triggers when a customer invoice changes in Pennylane (one run per change). Uses polling "
        "(every 5 minutes); the first poll starts from now.",
    ),
    (
        "customer_invoice_paid",
        "Customer invoice paid",
        "Triggers once when a customer invoice is paid in Pennylane. Uses polling (every 5 minutes); "
        "the first poll starts from now.",
    ),
    (
        "transaction_created",
        "Bank transaction created",
        "Triggers when a bank transaction is added in Pennylane. Uses polling (every 5 minutes); "
        "the first poll starts from now.",
    ),
    (
        "transaction_matched",
        "Bank transaction matched",
        "Triggers when a bank transaction is matched to invoices in Pennylane, again when the "
        "matched invoices change. Uses polling (every 5 minutes); the first poll starts from now.",
    ),
)
#: The filter keys of every Wordsmith event (the event's ``data`` and envelope, lifted).
_WORDSMITH_FILTER_KEYS: dict[str, str] = {
    "type": "The Wordsmith event type",
    "status": "The status the event carries (task, review, report, input request, operation)",
    "agent_id": "The Wordsmith agent the event names",
    "assignee_id": "The task's assignee (task.created, task.assigned)",
    "task_id": "The task the event names",
    "resource_type": "The type of the object the event is about",
    "actor_kind": "Who caused the event: user, agent or system",
}
#: The filter keys of every Spendesk webhook event (the delivery envelope).
_SPENDESK_FILTER_KEYS: dict[str, str] = {
    "event.entity": "The Spendesk entity type of the event: payables or settlements",
    "event.eventType": "The event type: created or prepared",
    "companyId": "The Spendesk company (entity) that sent the event",
    "data.id": "The id of the payable or settlement the event names",
}
#: The filter keys of the NetSuite transaction change events (services/netsuite_events.py).
_NETSUITE_TRANSACTION_FILTER_KEYS: dict[str, str] = {
    "subsidiary_id": "Only records of these subsidiaries (internal ids)",
    "transaction_types": "Only these types among the event's (e.g. credit_memo, vendor_payment)",
    "status": "Only records in one of these statuses (names or codes)",
    "min_amount": "Only records whose absolute amount is at least this, in the transaction currency",
    "change_kinds": "Only created or only updated records",
}

#: The NetSuite transaction change events: event, label, description.
_NETSUITE_TRANSACTION_EVENTS: tuple[tuple[str, str, str], ...] = (
    (
        "vendor_bill_changed",
        "Vendor bill created or changed",
        "Triggers when a vendor bill is created or changed. Uses polling (every 5 minutes); "
        "the first poll starts from now.",
    ),
    (
        "invoice_changed",
        "Invoice created or changed",
        "Triggers when an invoice or a credit memo is created or changed. Uses polling (every 5 minutes); "
        "the first poll starts from now.",
    ),
    (
        "payment_changed",
        "Payment created or changed",
        "Triggers when a customer or vendor payment is created or changed. Uses polling (every 5 minutes); "
        "the first poll starts from now.",
    ),
    (
        "journal_entry_changed",
        "Journal entry created or changed",
        "Triggers when a journal entry or an intercompany journal is created or changed. Uses polling "
        "(every 5 minutes); the first poll starts from now.",
    ),
)

#: The filter keys of every Aikido webhook event (the delivery envelope and its payload).
_AIKIDO_FILTER_KEYS: dict[str, str] = {
    "event_type": "The Aikido event type (issue.open.created, ci.gate.failed, zen.attack...)",
    "workspace_id": "The Aikido workspace that sent the event",
    "payload.severity": "The issue severity: critical, high, medium or low (issue events)",
    "payload.severity_score": "The issue severity score, 1 to 100 (issue events)",
    "payload.type": "The issue type: open_source, leaked_secret, cloud, iac, sast, surface_monitoring, malware",
    "payload.status": "The issue status the event carries",
    "payload.issue_id": "The issue the event names",
    "payload.code_repo_id": "The code repository the event names, when it carries one",
    "payload.code_repo_name": "The code repository name, when the event carries one",
    "payload.team_id": "The responsible team, when the event carries one",
}
#: The filter keys of every Airwallex webhook event (the Event object it delivers).
_AIRWALLEX_FILTER_KEYS: dict[str, str] = {
    "name": "The Airwallex event type (payout.transfer.paid, deposit.settled...)",
    "account_id": "The Airwallex account (entity) the event belongs to",
    "data.object.currency": "The currency of the object, when it carries one",
    "data.object.amount": "The amount of the object, when it carries one",
    "data.object.status": "The status of the object the event is about",
}
#: The filter keys of every Rydoo expense webhook event (the delivery is flat).
_RYDOO_FILTER_KEYS: dict[str, str] = {
    "eventType": "The Rydoo event type (ExpenseSubmitted, ExpenseApproved, ExpenseRejected...)",
    "expenseId": "The expense the event names",
    "userId": "The employee who owns the expense",
    "companyId": "The Rydoo company that sent the event",
}
#: The label of each Wordsmith event type in the trigger picker.
_WORDSMITH_EVENT_LABELS: dict[str, str] = {
    "operation.succeeded": "Wordsmith operation succeeded",
    "operation.failed": "Wordsmith operation failed",
    "operation.cancelled": "Wordsmith operation cancelled",
    "task.created": "Wordsmith task created",
    "task.assigned": "Wordsmith task assigned",
    "task.status_changed": "Wordsmith task status changed",
    "task.comment_added": "Wordsmith task comment added",
    "review.completed": "Wordsmith review completed",
    "input_request.created": "Wordsmith input request created",
    "input_request.resolved": "Wordsmith input request resolved",
    "repository.documents_added": "Wordsmith repository documents added",
    "repository.indexing_completed": "Wordsmith repository indexing completed",
    "report.run_completed": "Wordsmith report run completed",
    "alert.triggered": "Wordsmith alert triggered",
    "webhook_endpoint.test": "Wordsmith webhook test event",
}
#: One event-specific sentence per Wordsmith event type, as the GitHub and Jira pickers give.
_WORDSMITH_EVENT_DESCRIPTIONS: dict[str, str] = {
    "operation.succeeded": "Triggers when a Wordsmith operation succeeds.",
    "operation.failed": "Triggers when a Wordsmith operation fails.",
    "operation.cancelled": "Triggers when a Wordsmith operation is cancelled.",
    "task.created": "Triggers when a Wordsmith task is created.",
    "task.assigned": "Triggers when a Wordsmith task is assigned.",
    "task.status_changed": "Triggers when a Wordsmith task changes status.",
    "task.comment_added": "Triggers when a comment is added to a Wordsmith task.",
    "review.completed": "Triggers when a Wordsmith review is completed.",
    "input_request.created": "Triggers when a Wordsmith agent asks for input.",
    "input_request.resolved": "Triggers when a Wordsmith input request is resolved.",
    "repository.documents_added": "Triggers when documents are added to a Wordsmith repository.",
    "repository.indexing_completed": "Triggers when a Wordsmith repository finishes indexing.",
    "report.run_completed": "Triggers when a Wordsmith report run is completed.",
    "alert.triggered": (
        "Triggers when a Wordsmith audit alert rule fires. The Wordsmith key must be allowed to manage "
        "the organisation's webhooks."
    ),
    "webhook_endpoint.test": "Triggers when Wordsmith sends a test event to the webhook.",
}

#: Providers an XTM One instance only ever gets through a platform
#: registration, which creates and rewrites them: never created by hand, and
#: read-only on the integration page. The one list the provider catalog, the
#: create paths (the REST route and the agent builder) and every integration
#: response read. It lives here, not in the router, so the agent
#: builder reads it without importing the integrations router.
REGISTRATION_ONLY_PROVIDERS: frozenset[str] = frozenset({"opencti", "openaev", "opencrq"})


#: The providers XTM One qualifies as Certified: the Filigran products and the
#: integrations its own agent fleets run on every day in production. The one
#: list the catalog serves as ``is_certified`` (the card's corner ribbon);
#: every other first-party provider is Preview and a custom provider is
#: neither. ``opencrq`` is the catalog key of the product marketed as OpenCRQ.
CERTIFIED_PROVIDERS: frozenset[str] = frozenset(
    {
        # Filigran products and services.
        "opencti",
        "openaev",
        "opencrq",
        "xtm_hub",
        # Business, collaboration and development integrations.
        "ashby",
        "concord",
        "docusign",
        "drata",
        "figma",
        "github",
        "google",
        "google_search",
        "grafana",
        "hibob",
        "hubspot",
        "jira",
        "letsignit",
        "linkedin",
        "metabase",
        "microsoft",
        "notion",
        "openapi",
        "perk",
        "planhat",
        "slack",
        "whatsapp",
        # Security integrations.
        "defender",
        "entra",
        "hackerone",
        "intune",
        "purview",
    }
)


def is_certified_provider(provider: str) -> bool:
    """Whether *provider* carries the Certified qualification in the catalog."""
    return provider in CERTIFIED_PROVIDERS


def is_registration_only_provider(provider: str) -> bool:
    """Whether *provider* is registration-only on this deployment (XTM One mode)."""
    return settings.is_xtm_one and provider in REGISTRATION_ONLY_PROVIDERS


def supported_auth_types(entry: dict | None) -> list[str]:
    """The auth types a catalog entry accepts (#8386): its ``supported_auth_types``,
    else exactly its ``auth_type``. Empty only for an entry that declares neither,
    which then accepts any type. The one rule the REST create / update routes and
    the agent builder tools apply, so an unsupported type (``platform_managed`` on
    an OAuth-only provider) can never be stored."""
    entry = entry or {}
    declared = entry.get("supported_auth_types")
    if declared:
        return list(declared)
    default = entry.get("auth_type")
    return [default] if default else []


def unsupported_auth_type_message(auth_type: str, provider: str, supported: list[str]) -> str:
    """The refusal every create / update path answers for an unsupported type."""
    return (
        f"Auth type '{auth_type}' is not supported for provider '{provider}'. "
        f"Supported: {', '.join(supported)}"
    )


PLATFORM_MANAGED_NEEDS_BASE_URL = (
    "A platform-managed integration needs the platform's base_url in extra_config."
)


def platform_base_url(extra: dict | None) -> str:
    """The base_url a platform_managed integration signs its JWT for (#8386):
    without one it has no credential at all, so it is never stored as connected.
    The one rule the REST routes and the agent builder tools apply."""
    value = (extra or {}).get("base_url") if isinstance(extra, dict) else None
    return value.strip() if isinstance(value, str) else ""


def missing_required_extra_fields(provider: str, extra: dict | None) -> list[str]:
    """The extra-config keys *extra* lacks for *provider* to work (#9898), sorted.

    Read from the catalog's ``extra_field_required_when``: ``{field: {selector:
    [values]}}``, the field required while every selector's stored value (``""``
    when absent) is one of its values; an empty condition requires the field
    always. A key counts as present when its value is not blank. The one rule the
    REST create / update routes and the agent builder tools apply, so a write that
    would leave an integration unable to build its credential is refused with the
    key named instead of stored (a WhatsApp integration that lost its ``backend``
    resolved as a Cloud API one without ``phone_number_id`` and lost every tool).
    """
    entry = PROVIDER_CATALOG.get(provider) or {}
    rules = entry.get("extra_field_required_when") or {}
    values = extra if isinstance(extra, dict) else {}

    def _text(key: str) -> str:
        value = values.get(key)
        return value.strip() if isinstance(value, str) else ("" if value is None else str(value))

    missing = []
    for field, condition in rules.items():
        applies = all(_text(selector) in allowed for selector, allowed in (condition or {}).items())
        if applies and not _text(field):
            missing.append(field)
    return sorted(missing)


def missing_required_extra_fields_message(provider: str, missing: list[str]) -> str:
    """The refusal every create / update path answers for missing required keys."""
    keys = ", ".join(f"'{key}'" for key in missing)
    return (
        f"The {provider} integration needs {keys} in extra_config for its current "
        "settings; nothing was saved. Set the key, or change the setting that requires it."
    )


_DOMAIN_NAME = re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)+$")


def invalid_extra_fields(provider: str, extra: dict | None) -> list[str]:
    """The problems of *extra*'s typed values for *provider*, one sentence each.

    Read from the catalog's ``extra_field_inputs``: a ``domains`` field holds
    domain names only, as text (one per line) or a JSON list (a leading ``@``
    and a trailing dot are tolerated, as the provider reads them), an ``integer`` field a whole number within its
    ``minimum`` and ``maximum``. A blank value is not judged: the default
    applies. The create and update routes answer 422 with these sentences.
    """
    entry = PROVIDER_CATALOG.get(provider) or {}
    specs = entry.get("extra_field_inputs") or {}
    values = extra if isinstance(extra, dict) else {}
    problems: list[str] = []
    for key, spec in specs.items():
        raw = values.get(key)
        # A domain list may arrive as a JSON list; it is judged entry by entry,
        # never as the text of the list.
        listed = _listed_entries(raw)
        text = "\n".join(listed) if listed is not None else ("" if raw is None else str(raw).strip())
        if not text:
            continue
        if listed is not None and spec.get("kind") != "domains":
            problems.append(f"'{key}' takes one value, not a list.")
            continue
        if spec.get("kind") == "domains":
            entries = listed if listed is not None else re.split(r"[\s,;]+", text)
            bad = [item for item in entries if item and not _DOMAIN_NAME.match(item.lower().lstrip("@").rstrip("."))]
            if bad:
                problems.append(f"'{key}' holds values that are not domain names: {', '.join(bad[:5])}.")
        elif spec.get("kind") == "integer":
            low, high = spec.get("minimum"), spec.get("maximum")
            try:
                number: int | None = int(text)
            except ValueError:
                number = None
            if number is None or (low is not None and number < low) or (high is not None and number > high):
                problems.append(f"'{key}' is a whole number from {low} to {high}.")
    return problems


def _listed_entries(value: object) -> list[str] | None:
    """The trimmed, non-empty entries of a JSON list value; ``None`` when *value* is no list."""
    if not isinstance(value, (list, tuple)):
        return None
    return [str(item).strip() for item in value if item is not None and str(item).strip()]


def normalize_typed_extra_fields(provider: str, extra: dict[str, Any]) -> dict[str, Any]:
    """*extra* with each ``domains`` field given as a JSON list stored as text.

    The form stores a domain list one entry per line and reads it back that
    way; a list an API or tool caller sends is stored in the same form, so the
    form shows its entries and every reader sees one shape. Other values are
    left as given; a dict without such a list is returned as is.
    """
    entry: Any = PROVIDER_CATALOG.get(provider) or {}
    specs: dict[str, dict[str, Any]] = entry.get("extra_field_inputs") or {}
    normalized: dict[str, Any] = {}
    for key, value in extra.items():
        listed = _listed_entries(value) if (specs.get(key) or {}).get("kind") == "domains" else None
        normalized[key] = "\n".join(listed) if listed is not None else value
    return normalized if normalized != extra else extra


def newly_missing_required_extra_fields(
    provider: str, stored: dict | None, merged: dict | None
) -> list[str]:
    """The required keys an update would newly leave missing (#9898).

    A key already missing before the write is not held against it, so an edit of
    an integration saved before the rule existed is never refused for a gap it
    did not open; a write that drops a required key, or switches a selector to a
    value whose required key is absent, is.
    """
    before = set(missing_required_extra_fields(provider, stored))
    return [key for key in missing_required_extra_fields(provider, merged) if key not in before]


def registration_editable_extra_fields(provider: str) -> frozenset[str]:
    """Extra-config keys an admin sets on a registered integration (catalog
    ``registration_editable_extra_fields``): edited on the integration page,
    kept when the platform registers again. Every other key is the
    registration's own."""
    entry = PROVIDER_CATALOG.get(provider) or {}
    return frozenset(entry.get("registration_editable_extra_fields") or ())


# The outbound opt-in of #708, shared by the three A2A providers: whether
# every message sent to the partner carries this instance's identity (Instance
# ID and display name) in its metadata. Off by default, first option.
_A2A_SEND_INSTANCE_IDENTITY_LABEL = (
    "Send this instance's identity (Instance ID and platform name) in the metadata of every message to the partner"
)
_A2A_SEND_INSTANCE_IDENTITY_OPTIONS = [
    {"value": "off", "label": "Off: the message text only (default)"},
    {"value": "on", "label": "On: tell the partner which instance is calling"},
]


# The complete set of JSON-RPC methods a federated partner may invoke on an
# exposed agent's task surface — the protocol's whole vocabulary, since
# everything else is refused (see ``app.a2a.protocol``). Shared by BOTH A2A
# providers: the operations belong to the protocol, not to either credential
# mechanism, so a partner reads the same surface whichever it authenticates
# with. Named by their v0.3 spelling with the v1.0 alias in the description,
# because both are served and the method name decides the response dialect.
# ``write`` marks the two that change platform state (a submission starts a
# billable run; a cancel stops one).
_A2A_OPERATIONS = [
    {
        "name": "message/send",
        "description": (
            "v1.0: SendMessage — submit a task. Runs the agent as "
            "the exposure's run-as user and returns the task id. "
            "Deduplicated per (client, message id), so a retry never "
            "starts a second run."
        ),
        "tool_type": "write",
    },
    {
        "name": "tasks/get",
        "description": (
            "v1.0: GetTask — poll one task's state and artifacts. "
            "Scoped to the calling registration: a partner can never "
            "read another's task."
        ),
        "tool_type": "read",
    },
    {
        "name": "tasks/cancel",
        "description": (
            "v1.0: CancelTask — request cancellation of the "
            "underlying run. Crosses replicas, so it works even when "
            "the request lands away from the executing worker."
        ),
        "tool_type": "write",
    },
    {
        "name": "tasks/pushNotificationConfig/set",
        "description": (
            "v1.0: CreateTaskPushNotificationConfig — register where to "
            "be told this task finished, instead of polling for it. "
            "The credential is encrypted at rest and delivery is "
            "bounded; polling remains the floor under it."
        ),
        "tool_type": "write",
    },
    {
        "name": "tasks/pushNotificationConfig/get",
        "description": "v1.0: GetTaskPushNotificationConfig — read one registered callback.",
        "tool_type": "read",
    },
    {
        "name": "tasks/pushNotificationConfig/list",
        "description": "v1.0: ListTaskPushNotificationConfigs — every callback on one task.",
        "tool_type": "read",
    },
    {
        "name": "tasks/pushNotificationConfig/delete",
        "description": "v1.0: DeleteTaskPushNotificationConfig — stop being told.",
        "tool_type": "write",
    },
]

#: What an INBOUND binding gives the agents it exposes. One tool, offered
#: only while an external task is running: A2A blocks a task on its calling
#: AGENT, so this asks a machine that answers in its own turn.
_A2A_INBOUND_AGENT_TOOLS = [
    {
        "name": "ask_the_caller",
        "description": (
            "Ask the agent that sent this task for a detail it did not "
            "give. The task reports input-required with the question, the "
            "caller answers, and the same task continues. No person is "
            "involved. Offered only inside an external task, and bounded."
        ),
        "tool_type": "write",
    },
]

#: What an OUTBOUND binding exposes. One route, and only where an operator
#: allows it: registering a callback tells a partner where this deployment
#: lives, which in an outbound-only relationship it would never learn.
_A2A_OUTBOUND_EXPOSED = [
    {
        "name": "POST /a2a/callbacks/{delegation_id}",
        "description": (
            "Where a partner reports that a task it was given finished. "
            "HTTPS, pre-auth rate limited, a per-delegation secret compared "
            "in constant time, and one uniform not-found for every refusal. "
            "The body is never the answer — it names a task, which is then "
            "read over the authenticated channel."
        ),
        "tool_type": "write",
    },
]

#: The ``trigger_config.filter`` keys of the mail events, evaluated before any
#: run or gate (``services/integration_event_filters.py``).
_MAIL_FILTER_KEYS: dict[str, str] = {
    "from": 'Sender allow / deny lists: {"allow": [...], "deny": [...]}, addresses or domains (a deny entry wins)',
    "to_me": "Only mail where the mailbox owner is a To recipient",
    "cc_me": "Only mail where the mailbox owner is a Cc recipient (with to_me: either one)",
    "subject_contains": "Only mail whose subject holds this text (or one of a list), any case",
    "exclude_automated": (
        "Drop mail no person wrote: Auto-Submitted / bulk Precedence / mailing-list headers, "
        "delivery and read reports, calendar responses, no-reply senders"
    ),
    "exclude_meeting_messages": "Drop meeting requests, cancellations and answers (the payload's message_type)",
    "from_contains": "Only mail whose sender address holds this text",
}

#: The ``trigger_config.filter`` keys of the Teams channel events.
_TEAMS_CHANNEL_FILTER_KEYS: dict[str, str] = {
    "scope": 'The channels, as {"teams_channel": ["team_id:channel_id", ...]} (the Scope picker)',
    "exclude_authors": "Drop messages written by these people (display names or user ids)",
}

#: The ``trigger_config.filter`` of a webhook-delivered GitHub event: matched
#: against the delivered body (``integration_event_filters.payload_filter_reason``).
_GITHUB_PAYLOAD_FILTER_KEYS: dict[str, str] = {
    "any.payload.field": (
        "Only events whose payload field equals this value: any field of GitHub's body, dotted paths "
        "allowed (labels[].name maps a list), a list means any of its values; e.g. repository.full_name, "
        "action, workflow_run.conclusion, workflow_run.head_branch"
    ),
    "operators": (
        'Instead of a value, an object of operators, all holding: {"ne": v}, {"in": [...]}, '
        '{"not_in": [...]}, {"starts_with": "feat/" or [...]}, {"ends_with": "[bot]" or [...]}, '
        '{"contains": v}, {"matches": "anchored regex"}, {"exists": true|false}, {"eq": v}; '
        'e.g. {"sender.login": {"ne": "me", "not_in": ["dependabot[bot]"]}}'
    ),
    "all / any / not": (
        'Combinators: {"all": [filter, ...]}, {"any": [filter, ...]}, {"not": filter}, nesting; '
        'e.g. {"not": {"sender.login": {"ends_with": "[bot]"}}}'
    ),
}

#: A HubSpot CRM event: the object-type scope plus the payload filter on the
#: delivered event (``webhooks._hubspot_filter_reason``).
_HUBSPOT_FILTER_KEYS: dict[str, str] = {
    "scope": 'The CRM entity types, as {"crm_object_type": ["deal", ...]} (the Scope picker)',
    "propertyName": 'entity_updated: only changes of this property (a list means any), e.g. "dealstage"',
    "propertyValue": "entity_updated: only this new value; operators allowed",
    "changeSource": 'Only changes from this source, e.g. {"ne": "INTEGRATION"}',
    "operators": _GITHUB_PAYLOAD_FILTER_KEYS["operators"],
    "all / any / not": _GITHUB_PAYLOAD_FILTER_KEYS["all / any / not"],
}

#: A GitHub CI or push event: the payload filter plus the open pull request
#: its head commit belongs to (``providers/github/event_enrichment.py``).
_GITHUB_HEAD_FILTER_KEYS: dict[str, str] = {
    **_GITHUB_PAYLOAD_FILTER_KEYS,
    "enriched.pull_request.user.login": (
        "The author of the open pull request whose head is the event's commit (read from GitHub with "
        "the integration's token, cached 10 min); also .number, .draft, .state, .author_association, "
        ".labels[].name, .base.ref, .head.ref, .head.repo.full_name"
    ),
    "enriched.pull_request": (
        'null when the commit heads no open pull request: {"enriched.pull_request": {"exists": false}}; '
        "every one of them under enriched.pull_requests"
    ),
    "enriched.head.checks.all_green": (
        "workflow_run, check_suite, check_run and status events: true when every check run on the event's "
        "head commit completed and none failed, and no GitHub Actions workflow run of the head is still "
        "queued or running (a re-run of failed jobs counts as pending; read from GitHub, never from a cache "
        "taken before a completion). The event that turns it true is the last check to finish, whatever "
        'its workflow name, so {"action": "completed", "workflow_run.conclusion": "success", '
        '"enriched.head.checks.all_green": true} wakes once per fully green head with no list of workflow names'
    ),
    "enriched.head.checks": (
        "The check state of the head commit: .total, .completed, .pending (queued or in progress), .failed "
        "(failure, cancelled, timed_out, action_required, stale), .skipped, .neutral, .all_completed, "
        ".all_green, .failed_names[], .first_failed (.name, .id, .completed_at), .names[] (.name, .status, "
        '.conclusion); e.g. {"enriched.head.checks.pending": 0}. enriched.head.sha is the commit'
    ),
    "enriched.head.checks.first_failure": (
        "check_run events: true on the completion of the head's earliest failed check, false otherwise. A "
        "workflow run completes only when its last job ends, so a failed job beside a slow one is seen late "
        'on workflow_run; {"action": "completed", "enriched.head.checks.first_failure": true} on check_run '
        "wakes once per red head, on its first failed job, while the other jobs still run"
    ),
    "enriched.head.checks.last_to_complete": (
        "CI completion events: true when no check run of the head finished after this delivery's completion "
        "(ties allowed), false otherwise. all_green stays true on late or redelivered completions; add "
        '{"enriched.head.checks.last_to_complete": true} to wake only on the one that turned the head green'
    ),
    "enriched.head.required.all_passed": (
        "CI events: true when every status check the open pull request's base branch requires passed on the "
        "head (skipped or neutral pass), whatever the other checks; false with no open PR or no required check. "
        "Also .total, .passed, .failed, .pending, .missing, .*_names[], .base, .last_to_complete"
    ),
    "enriched.head.parent.unchanged_red": (
        "CI events: true when every counted red check of the head was already red on its first parent "
        '(a red the head did not cause); {"ne": true} drops such wakes. Also .sha, .failed_names[], '
        ".new_failed_names[]"
    ),
}


PROVIDER_CATALOG = {
    # --- Direct LLM-query integrations (query/evaluate external LLMs as tools) ---
    "anthropic": {
        "name": "Anthropic",
        "description": "Query Anthropic Claude models directly as a tool - prompt, compare model versions, evaluate responses (LLM-as-a-judge), and run web-grounded queries for brand monitoring and LLM evaluation",
        "logo": "/logos/anthropic.svg",
        "category": "integration",
        "integration_category": "ai_models",
        "integration_types": ["llm"],
        "auth_type": "api_key",
        "fields": ["api_key"],
        "setup_guide": {
            "title": "Anthropic API Key Setup",
            "url": "https://console.anthropic.com/settings/keys",
            "steps": [
                "Sign in to the Anthropic Console at console.anthropic.com.",
                "Open Settings > API Keys.",
                "Click 'Create Key', name it, and copy the key (starts with 'sk-ant-').",
                "Paste the API key in the field below.",
                "Make sure the workspace has billing/credit enabled so the Messages API and web search can run.",
            ],
            "permissions": [
                {
                    "scope": "Messages API",
                    "description": "Send prompts to Claude models and read their responses",
                },
                {
                    "scope": "Models API",
                    "description": "List the Claude models available to the key",
                },
                {
                    "scope": "Web Search",
                    "description": "Run server-side web searches to ground answers in live web content (billed per search)",
                },
            ],
            "notes": "This integration queries Claude as an external model the agent can prompt and evaluate - it is independent of the platform's own LLM provider configuration. Web search is billed by Anthropic per search in addition to token usage.",
        },
    },
    "openai": {
        "name": "OpenAI",
        "description": "Query OpenAI GPT models directly as a tool - prompt, compare model versions, evaluate responses (LLM-as-a-judge), and run web-grounded queries for brand monitoring and LLM evaluation",
        "logo": "/logos/openai.svg",
        "category": "integration",
        "integration_category": "ai_models",
        "integration_types": ["llm"],
        "auth_type": "api_key",
        "fields": ["api_key"],
        "setup_guide": {
            "title": "OpenAI API Key Setup",
            "url": "https://platform.openai.com/api-keys",
            "steps": [
                "Sign in to the OpenAI platform at platform.openai.com.",
                "Open the API keys page (platform.openai.com/api-keys).",
                "Click 'Create new secret key', name it, and copy the key (starts with 'sk-').",
                "Paste the API key in the field below.",
                "Make sure the account/project has billing set up so the Chat Completions, Responses, and Models APIs can run.",
            ],
            "permissions": [
                {
                    "scope": "Chat Completions API",
                    "description": "Send prompts to GPT models and read their responses",
                },
                {
                    "scope": "Responses API (web search)",
                    "description": "Run the built-in web search tool to ground answers in live web content",
                },
                {
                    "scope": "Models API",
                    "description": "List the models available to the key",
                },
            ],
            "notes": "This integration queries OpenAI as an external model the agent can prompt and evaluate - it is independent of the platform's own LLM provider configuration. Web search via the Responses API is billed by OpenAI in addition to token usage.",
        },
    },
    "gemini": {
        "name": "Google Gemini",
        "description": "Query Google Gemini models directly as a tool - prompt, compare model versions, evaluate responses (LLM-as-a-judge), and run Google-Search-grounded queries for brand monitoring and LLM evaluation",
        "logo": "/logos/gemini.svg",
        "category": "integration",
        "integration_category": "ai_models",
        "integration_types": ["llm"],
        "auth_type": "api_key",
        "fields": ["api_key"],
        "setup_guide": {
            "title": "Google Gemini API Key Setup",
            "url": "https://aistudio.google.com/apikey",
            "steps": [
                "Sign in to Google AI Studio at aistudio.google.com.",
                "Open the API keys page (aistudio.google.com/apikey).",
                "Click 'Create API key' (new keys are created as restricted auth keys) and copy it (starts with 'AIza').",
                "Paste the API key in the field below.",
                "The Gemini API has a free tier; for higher rate limits enable billing on the linked Google Cloud project.",
            ],
            "permissions": [
                {
                    "scope": "generateContent",
                    "description": "Send prompts to Gemini models and read their responses",
                },
                {
                    "scope": "Google Search grounding",
                    "description": "Ground answers in live web results with cited sources",
                },
                {
                    "scope": "Models list",
                    "description": "List the Gemini models available to the key",
                },
            ],
            "notes": "This integration queries Gemini as an external model the agent can prompt and evaluate - it is independent of the platform's own LLM provider configuration. Google Search grounding may incur additional Gemini API charges.",
        },
    },
    "mistral": {
        "name": "Mistral AI",
        "description": "Query Mistral AI models directly as a tool - prompt, compare model versions, evaluate responses (LLM-as-a-judge), and run web-grounded queries for brand monitoring and LLM evaluation",
        "logo": "/logos/mistral.svg",
        "category": "integration",
        "integration_category": "ai_models",
        "integration_types": ["llm"],
        "auth_type": "api_key",
        "fields": ["api_key"],
        "setup_guide": {
            "title": "Mistral AI API Key Setup",
            "url": "https://console.mistral.ai/api-keys",
            "steps": [
                "Sign in to La Plateforme at console.mistral.ai.",
                "Open the API keys page (console.mistral.ai/api-keys).",
                "Click 'Create new key', name it, and copy the key.",
                "Paste the API key in the field below.",
                "Make sure the workspace has an active plan / billing so the Chat Completions, Models, and web search connector can run.",
            ],
            "permissions": [
                {
                    "scope": "Chat Completions API",
                    "description": "Send prompts to Mistral models and read their responses",
                },
                {
                    "scope": "Web search connector",
                    "description": "Ground answers in live web results with cited sources",
                },
                {
                    "scope": "Models API",
                    "description": "List the models available to the key",
                },
            ],
            "notes": "This integration queries Mistral as an external model the agent can prompt and evaluate - it is independent of the platform's own LLM provider configuration. The built-in web search connector may incur additional Mistral charges.",
        },
    },
    # --- Model gateway: the account behind the platform's OpenRouter LLM provider ---
    "openrouter": {
        "name": "OpenRouter",
        "description": (
            "One API key for hundreds of models (Claude, GPT, Gemini, Llama, Mistral...). "
            "Track the key's spend, credits and daily activity per model, browse the "
            "catalog with each model's providers and prices, manage the account's API "
            "keys, and feed the platform's OpenRouter LLM provider with the models you pick."
        ),
        "logo": "/logos/openrouter.svg",
        "category": "integration",
        "integration_category": "ai_models",
        "integration_types": ["llm"],
        "auth_type": "api_key",
        "fields": ["api_key"],
        "extra_fields": {
            "management_key": ("Management key (optional - credits, activity and API key management)"),
        },
        "setup_guide": {
            "title": "OpenRouter Setup",
            "url": "https://openrouter.ai/settings/keys",
            "steps": [
                "Sign in at openrouter.ai and open Settings > API Keys (openrouter.ai/settings/keys).",
                "Create a key, optionally with a credit limit, and copy it (starts with 'sk-or-'). Paste it as the API key.",
                "Optional: open Settings > Management keys, create a management key and paste it in the Management key field. It unlocks the credits, the activity per model and the API key management of this integration; OpenRouter refuses it for model calls.",
                "To let the whole platform run on OpenRouter, make the integration company-managed, then open its Models tab and create the OpenRouter LLM provider from it.",
            ],
            "permissions": [
                {
                    "scope": "API key",
                    "description": "Read the key's own limit and spend, read the model catalog, and run the models the platform LLM provider offers",
                },
                {
                    "scope": "Management key (optional)",
                    "description": "Read the account's credits and activity, and create, edit, disable or delete its API keys",
                },
            ],
            "notes": (
                "The platform LLM provider fed by this integration gets a copy of the API key, "
                "replaced whenever the key is replaced here. Deleting the integration, or making "
                "it private again, leaves the provider running on the key it holds. A key the "
                "platform runs on cannot be disabled or deleted from the API Keys tab."
            ),
        },
    },
    # --- Model server on the customer's network, reached through a relay ---
    "lmstudio": {
        "name": "LM Studio",
        "description": (
            "Run the platform on models served by LM Studio inside your network, reached "
            "through an XTM One Relay. See the models on the server, load and unload them, "
            "download new ones from the LM Studio catalog or Hugging Face, and feed the "
            "platform's LM Studio LLM provider with the models you pick."
        ),
        "logo": "/logos/lmstudio.svg",
        "category": "integration",
        "integration_category": "ai_models",
        "integration_types": ["llm"],
        # Reached only from the relay installed next to the server.
        "requires_relay": True,
        # The token is optional (LM Studio asks for one only when Require
        # Authentication is on): an extra field, so the integration is
        # connected as soon as its server URL is set.
        "auth_type": "none",
        "fields": [],
        "extra_fields": {
            "base_url": "LM Studio server URL, as the relay reaches it (e.g. http://gpu-host:1234)",
            "api_token": "API token (optional: only when Require Authentication is on in LM Studio)",
        },
        "setup_guide": {
            "title": "LM Studio Setup",
            "url": "https://lmstudio.ai/docs/developer/rest",
            "steps": [
                "Install LM Studio 0.4 or later on the machine that serves the models, open Developer and start the server. Turn on Serve on Local Network so the relay can reach it.",
                "Install an XTM One Relay in the same network (Settings > Relays), and allow the server's host and port in the relay's destinations.",
                "Choose that relay group in Route through relay, and enter the server URL as the relay reaches it (for example http://gpu-host:1234).",
                "Optional: when Require Authentication is on in LM Studio's server settings, create a token (Manage Tokens) and paste it in the API token field. Leave it empty otherwise.",
                "To let the platform run on LM Studio, make the integration company-managed, then open its Models tab and create the LM Studio LLM provider from it.",
            ],
            "permissions": [
                {
                    "scope": "Server (REST API v1)",
                    "description": "List the models on disk and their loaded instances, load and unload models, download models",
                },
                {
                    "scope": "OpenAI-compatible API (/v1)",
                    "description": "Run the models the platform LLM provider offers: chat, tools, embeddings",
                },
            ],
            "notes": (
                "Every request leaves from the relay, never from the platform: the integration "
                "refuses to call the server without a relay group. The platform LLM provider fed "
                "by this integration holds no URL or token of its own; it reads them, and the "
                "relay group, from the integration on every request, so an edit here applies to it. "
                "Unlinking the provider, deleting the integration or making it private stops it."
            ),
        },
    },
    # --- Agent integrations (used by agents to act on behalf of users) ---
    "google": {
        "name": "Google",
        "description": "Gmail, Calendar, Drive, Docs, Sheets, Chat",
        "logo": "/logos/google.svg",
        "category": "integration",
        "integration_category": "communication",
        "integration_types": [
            "email",
            "calendar",
            "messaging",
            "storage",
            "documentation",
        ],
        "available_events": [
            {
                "event": "new_email",
                "label": "New email received",
                "description": "Triggers when a new email arrives in Gmail. Uses polling (every 5 min).",
                "mechanism": "polling",
                "filter_keys": {**_MAIL_FILTER_KEYS, "folder": "Only mail carrying this Gmail label"},
            },
        ],
        "auth_type": "oauth",
        "fields": ["client_id", "client_secret"],
        "scopes": "https://www.googleapis.com/auth/gmail.readonly https://www.googleapis.com/auth/gmail.compose https://www.googleapis.com/auth/gmail.modify https://www.googleapis.com/auth/calendar.readonly https://www.googleapis.com/auth/calendar.events https://www.googleapis.com/auth/drive.readonly https://www.googleapis.com/auth/drive.file https://www.googleapis.com/auth/drive https://www.googleapis.com/auth/documents https://www.googleapis.com/auth/spreadsheets https://www.googleapis.com/auth/chat.messages.readonly https://www.googleapis.com/auth/chat.messages.create openid email profile",
        "authorize_url": "https://accounts.google.com/o/oauth2/v2/auth",
        "token_url": "https://oauth2.googleapis.com/token",
        "setup_guide": {
            "title": "Google Cloud Console Setup",
            "url": "https://console.cloud.google.com/apis/credentials",
            "steps": [
                "Go to Google Cloud Console > APIs & Services > Credentials.",
                "Click 'Create Credentials' > 'OAuth client ID'.",
                "Select 'Web application' as the application type.",
                "Add your redirect URI: {base_url}/api/oauth/callback",
                "Copy the Client ID and Client Secret into the fields below.",
                "Enable each API listed below in 'Enabled APIs & Services'.",
            ],
            "permissions": [
                {
                    "scope": "gmail.readonly",
                    "description": "Read email messages and metadata",
                },
                {
                    "scope": "gmail.compose",
                    "description": "Create and send email drafts",
                },
                {
                    "scope": "gmail.modify",
                    "description": "Star, label, mark read or unread and move email to the trash",
                },
                {"scope": "calendar.readonly", "description": "Read calendar events"},
                {
                    "scope": "calendar.events",
                    "description": "Create, update and delete calendar events, with their attachments",
                },
                {
                    "scope": "drive.readonly",
                    "description": "Read files from Google Drive",
                },
                {
                    "scope": "drive.file",
                    "description": "Create the files the agent uploads (a calendar event's attachments are copied to a folder of the connected Drive and shared read-only with the attendees)",
                },
                {
                    "scope": "drive",
                    "description": "Comment on, reply to and resolve comments of files shared with the user (drive.file only reaches the files the app created)",
                },
                {
                    "scope": "documents",
                    "description": "Attach comments to a passage of a Google Doc, and write suggestions or edits to its text",
                },
                {
                    "scope": "spreadsheets",
                    "description": "Read Google Sheets, and append, update, clear or delete their rows",
                },
                {
                    "scope": "chat.messages.readonly",
                    "description": "Read Google Chat messages",
                },
                {
                    "scope": "chat.messages.create",
                    "description": "Send Google Chat messages",
                },
                {"scope": "openid email profile", "description": "Basic user identity"},
            ],
            "apis_to_enable": [
                "Gmail API",
                "Google Calendar API",
                "Google Drive API",
                "Google Docs API",
                "Google Sheets API",
                "Google Chat API",
            ],
            "notes": "Make sure each API is enabled in the 'Enabled APIs & Services' section before authorizing. Google Chat API requires a Google Workspace account. An integration authorized before calendar.events and drive.file were requested shows a scope change: re-authorize it to let the agent write calendar events and attach files to them. Likewise for drive and documents: re-authorize it to let the agent comment on, suggest edits to and edit Google Docs. The Google Sheets tools also work with the drive scope alone; re-authorizing adds the spreadsheets scope. An integration authorized before gmail.modify was requested shows a scope change: re-authorize it to let the agent star, label and trash email.",
        },
    },
    "microsoft": {
        "name": "Microsoft",
        "description": "Outlook, Teams, SharePoint, OneDrive",
        "logo": "/logos/microsoft.svg",
        "category": "integration",
        "integration_category": "communication",
        "integration_types": ["email", "calendar", "messaging", "storage"],
        #: The channel its messaging tools reach (a channel bot of that type is the same platform).
        "messaging_channel": "teams",
        "available_events": [
            {
                "event": "new_email",
                "label": "New email received",
                "description": (
                    "Triggers when a new email arrives in Outlook. Delivered by a Microsoft Graph "
                    "subscription when the platform has a public URL (inbox, or the filter's folder), "
                    "else polled every 5 min."
                ),
                "mechanism": "hybrid",
                "filter_keys": {
                    **_MAIL_FILTER_KEYS,
                    "folder": "Only mail of this folder (a well-known name such as inbox, or a folder id)",
                },
            },
            {
                "event": "new_teams_message",
                "label": "New Teams message",
                "description": (
                    "Triggers when a new message (a thread root) appears in monitored Teams channels. "
                    "Delivered by a Graph subscription per selected channel when the platform has a "
                    "public URL, else polled every 5 min. Configure channel filter in trigger config."
                ),
                "mechanism": "hybrid",
                "filter_keys": _TEAMS_CHANNEL_FILTER_KEYS,
            },
            {
                "event": "new_teams_reply",
                "label": "New reply in a Teams thread",
                "description": (
                    "Triggers when someone replies in a thread of a monitored Teams channel. Carries "
                    "parent_message_id (the thread root) besides the message fields. Delivered by a "
                    "Graph subscription per selected channel, else polled every 5 min (the latest 50 "
                    "threads of each channel)."
                ),
                "mechanism": "hybrid",
                "filter_keys": _TEAMS_CHANNEL_FILTER_KEYS,
            },
            {
                "event": "new_teams_chat_message",
                "label": "New Teams chat message",
                "description": (
                    "Triggers on a new message in a 1:1 or group chat of the connected user. Carries "
                    "chat_id, chat_topic and chat_type. Delivered by a Graph subscription per chat "
                    "listed in chat_ids, else polled every 5 min."
                ),
                "mechanism": "hybrid",
                "filter_keys": {
                    "chat_ids": "Only these chats (ids as list_microsoft_chats returns them)",
                    "exclude_authors": "Drop messages written by these people (display names or user ids)",
                },
            },
            {
                "event": "calendar_event_changed",
                "label": "Calendar event created, updated or cancelled",
                "description": (
                    "Triggers when an event of the mailbox's calendar is created, moved or otherwise "
                    "updated, or cancelled. Carries event_id and change_kind (created / updated / "
                    "cancelled). Delivered by a Graph subscription, else polled every 5 min (a poll "
                    "cannot see an event deleted outright)."
                ),
                "mechanism": "hybrid",
                "filter_keys": {"change_kinds": "Only these change kinds: created, updated, cancelled"},
            },
            {
                "event": "email_flag_changed",
                "label": "Email flag changed",
                "description": (
                    "Triggers when the follow-up flag of a mail is set, completed or cleared. Carries "
                    "mail_id, flag_status and previous_flag_status besides the mail fields. Delivered by "
                    "a Graph subscription (inbox, or the filter's folder), else polled every 5 min."
                ),
                "mechanism": "hybrid",
                "filter_keys": {
                    **_MAIL_FILTER_KEYS,
                    "flag_status": "Only these new statuses: flagged, complete, notFlagged",
                    "folder": "Only mail of this folder (a well-known name such as inbox, or a folder id)",
                },
            },
            {
                "event": "todo_task_changed",
                "label": "To Do task created, updated, completed or deleted",
                "description": (
                    "Triggers when a Microsoft To Do task is created, updated, completed or deleted. "
                    "Carries task_id, list_id, list_name, title, body, status, importance, due_date, "
                    "created_at, last_modified_at and change_kind (created / updated / completed / "
                    "deleted). Delivered by a Graph subscription when lists names every list by id, "
                    "else polled every minute (a poll cannot see a task deleted outright). A task the "
                    "assignment's own run wrote never triggers it."
                ),
                "mechanism": "hybrid",
                "filter_keys": {
                    "lists": "Only these task lists (names or ids; Tasks is the default list)",
                    "exclude_lists": "Never these task lists (names or ids, e.g. Flagged Emails)",
                    "change_kinds": "Only these change kinds: created, updated, completed, deleted",
                    "exclude_self": "Drop every change made through the platform's own To Do tools",
                },
            },
        ],
        "element_types": [
            {
                "type": "teams_channel",
                "name": "Teams Channel",
                "description": "Select specific Microsoft Teams channels for this assignment to focus on",
                #: The metadata keys that name the element to its tools.
                "identifiers": ["team_id", "channel_id"],
                "multiple": True,
            },
        ],
        "auth_type": "oauth",
        "fields": ["client_id", "client_secret"],
        "scopes": (
            "openid profile email offline_access "
            "User.Read.All User.ReadBasic.All User.ReadWrite "
            "Mail.Read Mail.ReadBasic Mail.ReadWrite Mail.Send "
            "Mail.Read.Shared Mail.ReadBasic.Shared Mail.ReadWrite.Shared Mail.Send.Shared "
            "Calendars.Read Calendars.ReadBasic Calendars.ReadWrite "
            "Calendars.Read.Shared Calendars.ReadWrite.Shared "
            "Chat.Create Chat.Read Chat.ReadBasic Chat.ReadWrite Chat.ReadWrite.All Chat.ManageDeletion.All "
            "ChatMember.Read ChatMember.ReadWrite ChatMessage.Read ChatMessage.Send "
            "Team.ReadBasic.All TeamMember.ReadWrite.All "
            "Channel.Create Channel.Delete.All Channel.ReadBasic.All "
            "ChannelMember.Read.All ChannelMember.ReadWrite.All "
            "ChannelMessage.Read.All ChannelMessage.ReadWrite ChannelMessage.Edit ChannelMessage.Send "
            "Files.Read Files.Read.All Files.Read.Selected "
            "Files.ReadWrite Files.ReadWrite.All Files.ReadWrite.AppFolder Files.ReadWrite.Selected "
            "Files.SelectedOperations.Selected "
            "Sites.Read.All Sites.ReadWrite.All Sites.Manage.All Sites.Create.All Sites.FullControl.All Sites.Selected "
            "GroupMember.Read.All "
            "Tasks.Read Tasks.ReadWrite Tasks.Read.Shared Tasks.ReadWrite.Shared "
            "OnlineMeetings.Read OnlineMeetings.ReadWrite "
            "OnlineMeetingTranscript.Read.All OnlineMeetingRecording.Read.All "
            "OnlineMeetingArtifact.Read.All OnlineMeetingAiInsight.Read.All "
            "CallTranscripts.Read.All"
        ),
        # Team member writes need admin consent in most tenants: a sign-in that
        # does not obtain it keeps the integration usable (#1829).
        "optional_scopes": "TeamMember.ReadWrite.All",
        "authorize_url": "https://login.microsoftonline.com/common/oauth2/v2.0/authorize",
        "token_url": "https://login.microsoftonline.com/common/oauth2/v2.0/token",
        "extra_fields": {
            "tenant_id": "Azure AD tenant ID (optional, for single-tenant apps)",
            "compose_style": (
                "Compose style of the emails the agents write (optional CSS; empty: Outlook's own, "
                "font-family:Aptos,Calibri,Arial,sans-serif;font-size:11pt;color:rgb(0,0,0); none: no style)"
            ),
        },
        "setup_guide": {
            "title": "Microsoft Entra ID (Azure AD) Setup",
            "url": "https://entra.microsoft.com/#view/Microsoft_AAD_RegisteredApps/ApplicationsListBlade",
            "steps": [
                "Go to Microsoft Entra admin center > App registrations > New registration (direct link: https://entra.microsoft.com/#view/Microsoft_AAD_RegisteredApps/ApplicationsListBlade).",
                "Name: '{APP_NAME}' (or any name you prefer).",
                "Supported account types: choose 'Single tenant' or 'Multi-tenant' based on your needs.",
                "Redirect URI: select 'Web' and enter {base_url}/api/oauth/callback",
                "After creating, copy the Application (client) ID.",
                "Go to 'Certificates & secrets' > 'New client secret'. Copy the secret value immediately.",
                "Go to 'API permissions' > 'Add a permission' > 'Microsoft Graph' > 'Delegated permissions'. Add each permission listed below.",
                "If you are a tenant admin, click 'Grant admin consent' to pre-approve for all users.",
            ],
            "permissions": [
                {
                    "scope": "Mail.Read",
                    "description": "Read the signed-in user's email",
                },
                {
                    "scope": "Mail.ReadWrite",
                    "description": "Read and write the user's email (drafts, flags)",
                },
                {
                    "scope": "Mail.Send",
                    "description": "Send email on behalf of the user",
                },
                {
                    "scope": "Chat.Create",
                    "description": "Create new Teams 1:1 and group chats",
                },
                {
                    "scope": "Chat.Read",
                    "description": "Read the user's Teams chat list",
                },
                {
                    "scope": "Chat.ReadWrite",
                    "description": "Read and manage Teams chats, and edit or delete the user's chat messages",
                },
                {
                    "scope": "ChatMember.ReadWrite",
                    "description": "List, add and remove the members of the user's Teams group chats",
                },
                {
                    "scope": "ChatMessage.Read",
                    "description": "Read messages in Teams chats",
                },
                {
                    "scope": "ChatMessage.Send",
                    "description": "Send messages in Teams chats",
                },
                {
                    "scope": "Team.ReadBasic.All",
                    "description": "List the user's joined Teams teams",
                },
                {
                    "scope": "Channel.Create",
                    "description": "Create new channels in Teams teams",
                },
                {
                    "scope": "Channel.ReadBasic.All",
                    "description": "List channels within Teams teams",
                },
                {
                    "scope": "ChannelMember.Read.All",
                    "description": "List members of Teams channels",
                },
                {
                    "scope": "ChannelMember.ReadWrite.All",
                    "description": "Add and remove members of private and shared Teams channels",
                },
                {
                    "scope": "TeamMember.ReadWrite.All",
                    "description": "Add and remove members of Teams teams (admin consent)",
                },
                {
                    "scope": "ChannelMessage.Read.All",
                    "description": "Read Teams channel messages",
                },
                {
                    "scope": "ChannelMessage.Send",
                    "description": "Send messages and reactions in Teams channels",
                },
                {
                    "scope": "ChannelMessage.ReadWrite",
                    "description": "Read, send, edit, and delete the user's Teams channel messages",
                },
                {
                    "scope": "ChannelMessage.Edit",
                    "description": "Edit Teams channel messages sent by the user",
                },
                {
                    "scope": "Calendars.Read",
                    "description": "Read the user's calendar events",
                },
                {
                    "scope": "Calendars.ReadWrite",
                    "description": "Create, update, and delete calendar events, and respond to meeting invitations",
                },
                {
                    "scope": "Calendars.Read.Shared",
                    "description": "Read calendars that other people have shared with the user (delegate access)",
                },
                {
                    "scope": "Calendars.ReadWrite.Shared",
                    "description": "Read and write calendars that other people have shared with the user (delegate access)",
                },
                {
                    "scope": "Files.Read.All",
                    "description": "Read files in OneDrive and SharePoint",
                },
                {
                    "scope": "Files.ReadWrite.All",
                    "description": "Read and write files in OneDrive and SharePoint",
                },
                {
                    "scope": "Sites.Read.All",
                    "description": "Read SharePoint sites and lists",
                },
                {
                    "scope": "Sites.ReadWrite.All",
                    "description": "Read and write SharePoint sites and lists",
                },
                {
                    "scope": "GroupMember.Read.All",
                    "description": "Enumerate the SharePoint sites backing the user's Microsoft 365 groups and Teams",
                },
                {
                    "scope": "Tasks.Read",
                    "description": "Read the user's tasks in To Do and Planner",
                },
                {
                    "scope": "Tasks.ReadWrite",
                    "description": "Read and write the user's tasks",
                },
                {
                    "scope": "OnlineMeetings.ReadWrite",
                    "description": "Read and change the options of the Teams meetings the user organizes (record and transcribe automatically, allow transcription, allow recording)",
                },
                {
                    "scope": "openid profile email",
                    "description": "Sign in and read the user's basic profile",
                },
            ],
            "notes": "For single-tenant apps, paste your Directory (tenant) ID in the optional 'Tenant ID' field. For multi-tenant apps, leave it empty. All permissions are Delegated — the user must consent via OAuth. Some scopes (Team.ReadBasic.All, Channel.ReadBasic.All, ChannelMessage.Read.All, ChannelMember.Read.All, Sites.Read.All, GroupMember.Read.All) require admin consent in many tenants; if Teams channel loading or SharePoint site enumeration is incomplete, verify admin consent then reconnect the integration. GroupMember.Read.All lets the agent enumerate the SharePoint sites backing the Microsoft 365 groups / Teams you belong to (list_microsoft_sharepoint_sites) — without it, only search-indexed and followed sites appear, though you can still read any file directly with get_microsoft_file_by_url, and list a folder shared with you (on any site, a member or not) with list_microsoft_folder_by_url and its link.\n\nMeeting transcripts (all delegated — no app-only/Application Access Policy needed): the agent reads transcripts of meetings the connected user ORGANIZED *and* meetings they were INVITED to, because it derives the meeting ID from the join URL and the Graph transcript API accepts an invited attendee's token. Listing a meeting's transcripts uses the delegated OnlineMeetingTranscript.Read.All: a 403 is reported as a permission refusal naming it, with Graph's message, and a 404 as no transcript exposed. When Graph lists none, get_microsoft_meeting_transcript_by_join_url reads the transcript embedded in the meeting's recording in the connected user's OneDrive Recordings folder (where an automatic recording of a meeting they organized lands), and get_microsoft_recording_transcript reads it from a recording's OneDrive or SharePoint URL; the video is never downloaded. For Teams CHANNEL meetings (where the meeting ID can't be derived), the agent reads the transcript embedded in the channel's SharePoint recording via get_microsoft_channel_meeting_transcript. These recording paths use the SharePoint Stream API, which needs a SharePoint-audience token for the recording's host (the OneDrive host {tenant}-my.sharepoint.com included) — so for them also add the delegated 'Office 365 SharePoint Online' permission (e.g. AllSites.Read) to the app registration, grant admin consent, and reconnect the integration. Recording files themselves are listable via get_microsoft_channel_meeting_recordings using your existing Files/Sites scopes and channel membership.\n\nTeams meeting options (delegated, no Application Access Policy needed): turning on 'Record and transcribe automatically' (and allowing transcription or recording) on a calendar event's Teams meeting uses OnlineMeetings.ReadWrite, and only works for meetings the connected account organizes — connect the integration with the account that organizes the meetings. If a calendar tool answers that the token lacks OnlineMeetings.ReadWrite, add it to the app registration's delegated permissions, grant admin consent if your tenant requires it, then re-authorize the integration.",
        },
    },
    "github": {
        "name": "GitHub",
        "description": "Repositories, Issues & Sub-issues, Pull Requests, Projects v2 (backlog), Actions (CI/CD), Checks, Security alerts, Releases, Organizations & Teams (members + roles), Enterprise governance (orgs, members, owners), Branch protection & Rulesets (change-control), Git operations, Search & GraphQL",
        "logo": "/logos/github.svg",
        "category": "integration",
        "integration_category": "development",
        "integration_types": ["repository"],
        "available_events": [
            # ── Repository & Code ─────────────────────────────────────────
            {
                "event": "push",
                "label": "Push to repository",
                "description": "Triggers on git push events to any branch or tag.",
                "mechanism": "webhook",
                "filter_keys": _GITHUB_HEAD_FILTER_KEYS,
            },
            {
                "event": "create",
                "label": "Branch or tag created",
                "description": "Triggers when a branch or tag is created.",
                "mechanism": "webhook",
            },
            {
                "event": "delete",
                "label": "Branch or tag deleted",
                "description": "Triggers when a branch or tag is deleted.",
                "mechanism": "webhook",
            },
            {
                "event": "fork",
                "label": "Repository forked",
                "description": "Triggers when a repository is forked.",
                "mechanism": "webhook",
            },
            {
                "event": "repository",
                "label": "Repository event",
                "description": "Triggers when a repository is created, deleted, archived, unarchived, publicized, privatized, edited, renamed, or transferred.",
                "mechanism": "webhook",
            },
            {
                "event": "public",
                "label": "Repository made public",
                "description": "Triggers when a private repository is made public.",
                "mechanism": "webhook",
            },
            {
                "event": "repository_dispatch",
                "label": "Repository dispatch",
                "description": "Triggers when a custom repository_dispatch event is sent via the API.",
                "mechanism": "webhook",
            },
            {
                "event": "repository_ruleset",
                "label": "Repository ruleset",
                "description": "Triggers when a repository ruleset is created, edited, or deleted.",
                "mechanism": "webhook",
            },
            {
                "event": "commit_comment",
                "label": "Commit comment",
                "description": "Triggers when a commit or diff is commented on.",
                "mechanism": "webhook",
            },
            {
                "event": "gollum",
                "label": "Wiki page updated",
                "description": "Triggers when a wiki page is created or updated.",
                "mechanism": "webhook",
            },
            {
                "event": "star",
                "label": "Repository starred",
                "description": "Triggers when a star is added to or removed from a repository.",
                "mechanism": "webhook",
            },
            {
                "event": "watch",
                "label": "Repository watched",
                "description": "Triggers when a user starts watching a repository.",
                "mechanism": "webhook",
            },
            # ── Issues ────────────────────────────────────────────────────
            {
                "event": "issues",
                "label": "Issue event",
                "description": "Triggers on issue opened, closed, edited, labeled, assigned, milestoned, typed, field_added, field_removed, etc.",
                "mechanism": "webhook",
                "filter_keys": _GITHUB_PAYLOAD_FILTER_KEYS,
            },
            {
                "event": "issue_comment",
                "label": "Issue comment",
                "description": "Triggers when an issue comment is created, edited, or deleted.",
                "mechanism": "webhook",
            },
            {
                "event": "label",
                "label": "Label event",
                "description": "Triggers when a label is created, edited, or deleted.",
                "mechanism": "webhook",
            },
            {
                "event": "milestone",
                "label": "Milestone event",
                "description": "Triggers when a milestone is created, closed, opened, edited, or deleted.",
                "mechanism": "webhook",
            },
            {
                "event": "sub_issues",
                "label": "Sub-issues event",
                "description": "Triggers when sub-issues are added or removed, or parent issues are added or removed.",
                "mechanism": "webhook",
            },
            # ── Pull Requests ─────────────────────────────────────────────
            {
                "event": "pull_request",
                "label": "Pull request event",
                "description": "Triggers on PR opened, closed, merged, edited, review requested, labeled, locked, enqueued, dequeued, etc.",
                "mechanism": "webhook",
                "filter_keys": _GITHUB_PAYLOAD_FILTER_KEYS,
            },
            {
                "event": "pull_request_review",
                "label": "Pull request review",
                "description": "Triggers when a review is submitted, edited, or dismissed on a PR.",
                "mechanism": "webhook",
            },
            {
                "event": "pull_request_review_comment",
                "label": "Pull request review comment",
                "description": "Triggers when a pull request diff comment is created, edited, or deleted.",
                "mechanism": "webhook",
            },
            {
                "event": "pull_request_review_thread",
                "label": "Pull request review thread",
                "description": "Triggers when a pull request review thread is resolved or unresolved.",
                "mechanism": "webhook",
            },
            {
                "event": "merge_queue_entry",
                "label": "Merge queue entry",
                "description": "Triggers when a merge queue entry is added.",
                "mechanism": "webhook",
            },
            # ── CI/CD & Checks ────────────────────────────────────────────
            {
                "event": "check_run",
                "label": "Check run event",
                "description": "Triggers when a CI check run is created, completed, or re-requested.",
                "mechanism": "webhook",
                "filter_keys": _GITHUB_HEAD_FILTER_KEYS,
            },
            {
                "event": "check_suite",
                "label": "Check suite event",
                "description": "Triggers when a check suite is requested or completed.",
                "mechanism": "webhook",
                "filter_keys": _GITHUB_HEAD_FILTER_KEYS,
            },
            {
                "event": "status",
                "label": "Commit status",
                "description": "Triggers when the combined status of a commit changes (e.g. CI passes or fails).",
                "mechanism": "webhook",
                "filter_keys": _GITHUB_HEAD_FILTER_KEYS,
            },
            {
                "event": "workflow_run",
                "label": "Workflow run event",
                "description": "Triggers when a GitHub Actions workflow run is requested or completed.",
                "mechanism": "webhook",
                "filter_keys": _GITHUB_HEAD_FILTER_KEYS,
            },
            {
                "event": "workflow_job",
                "label": "Workflow job event",
                "description": "Triggers when a workflow job is queued, waiting, in progress, or completed.",
                "mechanism": "webhook",
                "filter_keys": _GITHUB_PAYLOAD_FILTER_KEYS,
            },
            {
                "event": "workflow_dispatch",
                "label": "Workflow dispatch",
                "description": "Triggers when a manual workflow run is requested.",
                "mechanism": "webhook",
            },
            # ── Releases ──────────────────────────────────────────────────
            {
                "event": "release",
                "label": "Release event",
                "description": "Triggers when a release is published, edited, unpublished, or deleted.",
                "mechanism": "webhook",
            },
            # ── Security ──────────────────────────────────────────────────
            {
                "event": "code_scanning_alert",
                "label": "Code scanning alert",
                "description": "Triggers when a code scanning alert is created, fixed, closed, or reopened.",
                "mechanism": "webhook",
            },
            {
                "event": "dependabot_alert",
                "label": "Dependabot alert",
                "description": "Triggers when a Dependabot vulnerability alert is created, dismissed, fixed, or reopened.",
                "mechanism": "webhook",
            },
            {
                "event": "repository_advisory",
                "label": "Repository security advisory",
                "description": "Triggers when a repository security advisory is published or privately reported.",
                "mechanism": "webhook",
            },
            {
                "event": "security_advisory",
                "label": "Global security advisory",
                "description": "Triggers when a GitHub-reviewed global security advisory is published, updated, or withdrawn.",
                "mechanism": "webhook",
            },
            {
                "event": "security_and_analysis",
                "label": "Security and analysis",
                "description": "Triggers when code security features are enabled or disabled for a repository.",
                "mechanism": "webhook",
            },
            {
                "event": "branch_protection_configuration",
                "label": "Branch protection configuration",
                "description": "Triggers when all branch protections are disabled or enabled for a repository.",
                "mechanism": "webhook",
            },
            {
                "event": "branch_protection_rule",
                "label": "Branch protection rule",
                "description": "Triggers when a branch protection rule is created, edited, or deleted.",
                "mechanism": "webhook",
            },
            # ── Projects v2 ───────────────────────────────────────────────
            {
                "event": "projects_v2",
                "label": "Project event",
                "description": "Triggers when a GitHub Project (v2) is created, updated, deleted, closed, or reopened.",
                "mechanism": "webhook",
            },
            {
                "event": "projects_v2_item",
                "label": "Project item event",
                "description": "Triggers when a project item is created, edited, deleted, archived, restored, converted, or reordered. Includes custom field changes (Status, Priority, etc.).",
                "mechanism": "webhook",
            },
            {
                "event": "projects_v2_status_update",
                "label": "Project status update",
                "description": "Triggers when a project status update is created, edited, or deleted.",
                "mechanism": "webhook",
            },
            # ── Collaboration ─────────────────────────────────────────────
            {
                "event": "member",
                "label": "Collaborator event",
                "description": "Triggers when a collaborator is added to, removed from, or has changed permissions for a repository.",
                "mechanism": "webhook",
            },
            {
                "event": "org_block",
                "label": "Organization block",
                "description": "Triggers when a user is blocked or unblocked by the organization.",
                "mechanism": "webhook",
            },
            # ── Meta ──────────────────────────────────────────────────────
            {
                "event": "meta",
                "label": "App meta event",
                "description": "Triggers when the GitHub App is deleted and the associated hook is removed.",
                "mechanism": "webhook",
            },
            {
                "event": "installation_target",
                "label": "Installation target renamed",
                "description": "Triggers when a GitHub App installation target (user or org) is renamed.",
                "mechanism": "webhook",
            },
        ],
        "element_types": [
            {
                "type": "organization",
                "name": "Organization",
                "description": "Select GitHub organizations to scope agent/assignment access",
                #: The metadata keys that name the element to its tools.
                "identifiers": ["login"],
                "multiple": True,
            },
            {
                "type": "repository",
                "name": "Repository",
                "description": "Select specific GitHub repositories to focus on (private repos listed, type public repo names manually)",
                #: The metadata keys that name the element to its tools.
                "identifiers": ["full_name"],
                "multiple": True,
                "allow_manual_input": True,
            },
        ],
        "needs_shared_webhook": True,
        "supports_assignment_webhooks": True,
        "supports_webhook_management": True,
        "auth_type": "oauth",
        # OAuth app or a personal access token in the API Key field, as the
        # provider class declares (#8386 makes the list binding).
        "supported_auth_types": ["oauth", "api_key"],
        # What its sign-in learns about the account: a person's own credential
        # never reads the integration's.
        "account_config_keys": ["installations", "installation_id"],
        "fields": ["client_id", "client_secret"],
        "extra_fields": {"webhook_secret": "Webhook secret (optional — for signature verification of inbound events)"},
        "scopes": "",
        "authorize_url": "https://github.com/login/oauth/authorize",
        "token_url": "https://github.com/login/oauth/access_token",
        "setup_guide": {
            "title": "GitHub App Setup",
            "url": "https://github.com/settings/apps",
            "steps": [
                "Go to GitHub > Settings > Developer settings > GitHub Apps > New GitHub App.",
                "App name: '{APP_NAME}' (or any unique name).",
                "Homepage URL: your {APP_NAME} instance URL.",
                "Callback URL: {base_url}/api/oauth/callback",
                "Check 'Request user authorization (OAuth) during installation'.",
                "Webhook: Active. Webhook URL: use the Webhook URL shown below this form (pre-generated).",
                "Webhook secret: (optional) set a secret and add it in the 'Webhook Secret' field on this form.",
                "Under 'Repository permissions', set each repository permission listed below.",
                "Under 'Organization permissions', set each organization permission listed below (required for Projects v2 custom fields).",
                "Under 'Subscribe to events', check the events you want to receive. Repository advisory delivers only published and reported actions; use a scheduled Assignment to reconcile later status changes.",
                "Set 'Where can this GitHub App be installed?' based on your needs (only this account, or any account).",
                "After creating the app, copy the Client ID (not the App ID — look for 'Client ID' specifically).",
                "Click 'Generate a client secret' and copy it immediately (shown only once).",
                "Go to 'Install App' in the sidebar and install it on your repositories (all repos or selected repos).",
                "Back in {APP_NAME}, click Connect on the integration to authorize it with your GitHub account: an install started from GitHub does not connect an integration.",
            ],
            "permissions": [
                {
                    "scope": "Repository — Actions: Read & Write",
                    "description": "Workflow runs, artifacts, and re-run workflows (GitHub Actions)",
                },
                {
                    "scope": "Repository — Administration: Read & Write",
                    "description": "Repository settings, teams, collaborators, branch protection, and Rulesets (branch governance / change-control)",
                },
                {
                    "scope": "Repository — Webhooks: Read & Write",
                    "description": "Automatic repository webhook management (Managed Repository Webhooks)",
                },
                {
                    "scope": "Repository — Checks: Read & Write",
                    "description": "Read and create check runs and check suites on commits",
                },
                {
                    "scope": "Repository — Code scanning alerts: Read & Write",
                    "description": "View and manage code scanning (SAST) alerts",
                },
                {
                    "scope": "Repository — Secret scanning alerts: Read & Write",
                    "description": "View and resolve secret scanning alerts (leaked credentials)",
                },
                {
                    "scope": "Repository — Commit statuses: Read & Write",
                    "description": "Read combined commit status and create status checks",
                },
                {
                    "scope": "Repository — Contents: Read & Write",
                    "description": "Repository contents, commits, branches, downloads, releases, and merges",
                },
                {
                    "scope": "Repository — Dependabot alerts: Read",
                    "description": "View Dependabot vulnerability alerts and affected packages",
                },
                {
                    "scope": "Repository — Repository security advisories: Read",
                    "description": "Read repository security advisories (unpublished ones also need the authorizing user to be a repository admin or security manager) and receive repository_advisory events; the App must also be installed on each selected repository",
                },
                {
                    "scope": "Repository — Issues: Read & Write",
                    "description": "Issues, comments, labels, assignees, and milestones",
                },
                {
                    "scope": "Repository — Metadata: Read (mandatory)",
                    "description": "Search repositories, list collaborators, and access repository metadata",
                },
                {
                    "scope": "Repository — Projects: Read & Write",
                    "description": "Manage classic projects within a repository",
                },
                {
                    "scope": "Repository — Pull requests: Read & Write",
                    "description": "Pull requests, reviews, comments, diffs, merges, and reviewer assignments",
                },
                {
                    "scope": "Organization — Administration: Read",
                    "description": "Manage access to an organization",
                },
                {
                    "scope": "Organization — Members: Read",
                    "description": "List organization members WITH their role (admin/member), teams, and team membership (maintainers labelled)",
                },
                {
                    "scope": "Organization — Issue Fields: Read & Write",
                    "description": "Manage issue fields for an organization",
                },
                {
                    "scope": "Organization — Issue Types: Read & Write",
                    "description": "Manage issue types for an organization",
                },
                {
                    "scope": "Organization — Projects: Read & Write",
                    "description": "Manage Projects v2 for an organization (custom fields like Status, Priority, Bug Criticity)",
                },
            ],
            "notes": "GitHub Apps use fine-grained repository permissions (not OAuth scopes). Permissions are configured on the app itself — no scopes are needed in the OAuth URL. The app must be installed on the repositories it should access. For organization repos, an org admin may need to approve the app installation. Each integration instance gets a pre-generated Webhook URL; configure it on the GitHub App to receive events. Organization-level Projects permissions are required to access Projects v2 custom fields via the GraphQL API. The 'Organization — Members: Read' permission is what lets the agent enumerate members with their roles and team membership (without it those reads return 'Resource not accessible by integration'). Enterprise-level governance (the enterprise organization roster, member roster, and owners/billing managers) is NOT available to GitHub App tokens — to read it, connect this integration with the API key (Personal Access Token) auth type using a classic PAT that has the 'read:enterprise' (or 'admin:enterprise') scope. Branch governance increasingly uses Rulesets (the modern replacement for classic branch protection); reading them needs the 'Administration: Read' permission (repository rulesets) and 'Organization — Administration: Read' (org-level rulesets). A branch governed only by a ruleset shows as 'not protected' via classic branch protection — use get_github_branch_rules / list_github_rulesets for change-control evidence (SOC 2 CC8.1, FedRAMP CM-3).",
        },
    },
    "grafana": {
        "name": "Grafana",
        "description": "Dashboards, Panel images, Data Sources, Alerts, Logs, Annotations",
        "logo": "/logos/grafana.svg",
        "category": "integration",
        "integration_category": "observability",
        "integration_types": ["observability"],
        "auth_type": "api_key",
        "fields": ["api_key"],
        "extra_fields": {"base_url": "Grafana instance URL (e.g. https://grafana.company.com)"},
        "setup_guide": {
            "title": "Grafana API Setup",
            "url": "https://grafana.com/docs/grafana/latest/http_api/",
            "steps": [
                "Log in to Grafana as an admin.",
                "Go to Configuration > Service Accounts (recommended for Grafana 9.5+) "
                + "or Settings > API Keys (legacy).",
                "For Service Accounts: click 'Add service account', name it '{APP_NAME}', "
                + "assign the 'Viewer' role for read-only access or 'Editor' for write access "
                + "(required for creating annotations). Then go to the service account's "
                + "'Tokens' tab and click 'Add service account token', name it, and copy the token.",
                "For API Keys (legacy): click 'Add API Key', name it (e.g. '{APP_NAME}'), "
                + "assign the 'Viewer' or 'Editor' role, set an expiry, and click 'Copy token'.",
                "Copy the generated token immediately — it will not be shown again.",
                "Enter your Grafana instance URL in the 'Base URL' field below.",
                "Paste the token in the 'API Key' field.",
            ],
            "permissions": [
                {
                    "scope": "Viewer",
                    "description": "Read dashboards, panels, data sources, alerts, annotations, and search. Run queries. Render panels as images.",
                },
                {
                    "scope": "Editor",
                    "description": "All Viewer permissions plus create annotations, silences and alert rules, and manage dashboard tags.",
                },
            ],
            "notes": (
                "Both API keys and service account tokens are supported — "
                "they use the same Bearer token auth mechanism. "
                "Service accounts are recommended for Grafana 9.5+ because they can be scoped "
                "to specific folders/dashboards and are easier to manage across multiple instances. "
                "Tokens are scoped to the role assigned during creation (Viewer, Editor, or Admin). "
                "For Grafana Cloud, use tokens from Grafana.com or from your Grafana Cloud instance. "
                "Rendering a panel as an image needs the Grafana Image Renderer service set up on "
                "the Grafana side (https://grafana.com/docs/grafana/latest/setup-grafana/image-rendering/); "
                "without it the panel image tool says so and every other tool works."
            ),
        },
    },
    "slack": {
        "name": "Slack",
        "description": "Channels, Messages, Files, Bookmarks, Search",
        "logo": "/logos/slack.svg",
        "category": "integration",
        "integration_category": "communication",
        "integration_types": ["messaging"],
        #: The channel its messaging tools reach (a channel bot of that type is the same platform).
        "messaging_channel": "slack",
        "available_events": [
            {
                "event": "new_slack_message",
                "label": "New Slack message",
                "description": "Triggers when a new message appears in monitored Slack channels. Supports Slack Events API (real-time webhook): configure Event Subscriptions in your Slack App settings with the Request URL given in the setup guide, and store the app's Signing Secret on the integration (or on the Slack OAuth default when your administrator provides the app). Once it is stored, events it does not sign are refused, and an event reaches only the integrations of the workspace it comes from. Without a stored Signing Secret, events are still accepted unverified (the Security Audit reports it) unless INTEGRATION_NOTIFICATIONS_REQUIRE_VERIFICATION is set. Without Events API, new messages are polled every 5 min instead.",
                "mechanism": "hybrid",
            },
        ],
        "element_types": [
            {
                "type": "slack_channel",
                "name": "Slack Channel",
                "description": "Select specific Slack channels for this assignment to focus on",
                #: The metadata keys that name the element to its tools.
                "identifiers": ["channel_id"],
                "multiple": True,
            },
        ],
        "needs_shared_webhook": True,
        "auth_type": "oauth",
        # OAuth app or a bot token, as the provider class declares (#8386).
        "supported_auth_types": ["oauth", "api_key"],
        "fields": ["client_id", "client_secret"],
        # ``webhook_secret`` is the key every Slack signature check reads: the Slack
        # events endpoint, and the integration and provider webhook routes.
        "extra_fields": {
            "webhook_secret": "Signing secret (Slack app > Basic Information; verifies real-time events - without it they are accepted unverified, or polled every 5 minutes when verification is required)"
        },
        "scopes": "bookmarks:read bookmarks:write channels:history channels:read channels:write chat:write files:read files:write groups:history groups:read groups:write im:history im:read im:write reactions:read reactions:write search:read search:read.im search:read.private search:read.public search:read.users users:read users.profile:write",
        "authorize_url": "https://slack.com/oauth/v2/authorize",
        "token_url": "https://slack.com/api/oauth.v2.access",
        "setup_guide": {
            "title": "Slack App Setup (User Token)",
            "url": "https://api.slack.com/apps",
            "steps": [
                "Go to api.slack.com/apps > Create New App > From scratch.",
                "App Name: '{APP_NAME}'. Pick your workspace.",
                "Go to 'OAuth & Permissions' in the sidebar.",
                "Under 'Redirect URLs', add: {base_url}/api/oauth/callback",
                "Under 'User Token Scopes', add each scope listed below.",
                "Go to 'Basic Information' and copy the Client ID and Client Secret.",
                "For real-time message events, go to 'Event Subscriptions' in the sidebar:",
                "  - In 'Basic Information', copy the Signing Secret into the 'Signing secret' field of this form (or of the Slack OAuth default when your administrator provides the app) BEFORE entering the Request URL: Slack verifies the URL with a signed request, and once the secret is stored anything it does not sign is refused.",
                "  - Toggle 'Enable Events' to On.",
                "  - Request URL: {base_url}/api/v1/integrations/notifications/slack",
                "  - Under 'Subscribe to events on behalf of users', add message.channels, message.groups and message.im (the integration connects with a user token). Messages outside public channels reach only the integration of their author or of the user Slack authorized the event for; the others still get them by polling.",
                "  - If not using Event Subscriptions, messages will be polled every 5 minutes instead.",
            ],
            "permissions": [
                {
                    "scope": "bookmarks:read",
                    "description": "List bookmarks in channels",
                },
                {
                    "scope": "bookmarks:write",
                    "description": "Create, edit, and remove bookmarks",
                },
                {
                    "scope": "channels:history",
                    "description": "Read messages in public channels",
                },
                {
                    "scope": "channels:read",
                    "description": "View basic information about public channels",
                },
                {
                    "scope": "channels:write",
                    "description": "Manage and create public channels on the user's behalf",
                },
                {
                    "scope": "chat:write",
                    "description": "Send messages on behalf of the user",
                },
                {
                    "scope": "files:read",
                    "description": "View files shared in channels and conversations",
                },
                {
                    "scope": "files:write",
                    "description": "Upload, edit, and delete files on the user's behalf",
                },
                {
                    "scope": "groups:history",
                    "description": "Read messages in private channels",
                },
                {
                    "scope": "groups:read",
                    "description": "View basic information about private channels",
                },
                {
                    "scope": "groups:write",
                    "description": "Manage private channels and create new ones on the user's behalf",
                },
                {"scope": "im:history", "description": "Read direct message history"},
                {"scope": "im:read", "description": "View basic DM information"},
                {
                    "scope": "im:write",
                    "description": "Start direct messages on the user's behalf",
                },
                {
                    "scope": "reactions:read",
                    "description": "View emoji reactions and their associated content",
                },
                {
                    "scope": "reactions:write",
                    "description": "Add and edit emoji reactions on the user's behalf",
                },
                {
                    "scope": "search:read",
                    "description": "Search workspace content",
                },
                {
                    "scope": "search:read.im",
                    "description": "Search content in direct messages",
                },
                {
                    "scope": "search:read.private",
                    "description": "Search content in private channels",
                },
                {
                    "scope": "search:read.public",
                    "description": "Search content in public channels",
                },
                {
                    "scope": "search:read.users",
                    "description": "Search workspace users",
                },
                {
                    "scope": "users:read",
                    "description": "View people in the workspace",
                },
                {
                    "scope": "users.profile:write",
                    "description": "Edit a user's profile information and status",
                },
            ],
            "notes": "These are User Token Scopes (acting as the user), not Bot Token Scopes. If your workspace requires admin approval for apps, ask a Slack admin to approve it. The Webhook URL is per-integration — all agents using this integration will receive events automatically.",
        },
    },
    "perk": {
        "name": "Perk",
        "description": "Trips, Bookings, Invoices (with PDFs), Users, Cost Centers, Webhooks",
        "logo": "/logos/perk.svg",
        "category": "integration",
        "integration_category": "business_operations",
        "integration_types": ["travel"],
        "supports_assignment_webhooks": True,
        "available_events": [
            {
                "event": "trip_created",
                "label": "Trip created",
                "description": "Triggers when a new trip is created in Perk. Configure a webhook in Perk pointing to the webhook URL shown below.",
                "mechanism": "webhook",
            },
            {
                "event": "trip_updated",
                "label": "Trip updated",
                "description": "Triggers when a trip is modified (status change, dates, travelers).",
                "mechanism": "webhook",
            },
            {
                "event": "booking_created",
                "label": "Booking created",
                "description": "Triggers when a new booking (flight, hotel, car, train) is created.",
                "mechanism": "webhook",
            },
            {
                "event": "booking_updated",
                "label": "Booking updated",
                "description": "Triggers when a booking is modified or cancelled.",
                "mechanism": "webhook",
            },
            {
                "event": "invoice_created",
                "label": "Invoice created",
                "description": "Triggers when a new invoice is issued.",
                "mechanism": "webhook",
            },
            {
                "event": "invoice_updated",
                "label": "Invoice status changed",
                "description": "Triggers when an invoice status changes (e.g. paid).",
                "mechanism": "webhook",
            },
        ],
        "auth_type": "api_key",
        "fields": ["api_key"],
        "extra_fields": {"webhook_secret": "Webhook shared secret (for Tk-Signature verification)"},
        "setup_guide": {
            "title": "Perk (formerly TravelPerk) API Key Setup",
            "url": "https://developers.perk.com/reference/authentication",
            "steps": [
                "Log in to your Perk account at app.perk.com.",
                "Go to Settings > Integrations > API Keys (or visit developers.perk.com).",
                "Generate a new API key. API keys have full admin access to your account data.",
                "Copy the API key and paste it in the 'API Key' field below.",
                "To receive real-time event notifications, configure webhooks in Perk:",
                "  - Go to your Perk developer settings > Webhooks.",
                "  - Create a new webhook endpoint pointing to the Webhook URL shown below.",
                "  - Set a shared secret — paste the same value in the 'Webhook shared secret' field on this form.",
                "  - Subscribe to the events you want (trip changes, booking updates, invoices, etc.).",
                "  - Each assignment can have its own dedicated webhook URL — create multiple webhook endpoints in Perk, each pointing to a different assignment's URL.",
            ],
            "permissions": [
                {
                    "scope": "Trips",
                    "description": "Read trip itineraries, details, travelers, and locations",
                },
                {
                    "scope": "Bookings",
                    "description": "Read flight, hotel, car, and train bookings",
                },
                {
                    "scope": "Invoices",
                    "description": "Read invoices, invoice lines, and invoice profiles, and download invoice PDFs",
                },
                {
                    "scope": "Users",
                    "description": "Read traveler profiles and employee information",
                },
                {
                    "scope": "Suppliers",
                    "description": "Read airline, hotel, and car rental supplier info",
                },
                {
                    "scope": "Cost Centers",
                    "description": "Read and manage cost centers for expense allocation",
                },
                {
                    "scope": "Webhooks",
                    "description": "Create and manage webhook subscriptions for real-time events",
                },
                {
                    "scope": "Custom Fields",
                    "description": "Read custom fields defined for trips",
                },
            ],
            "notes": "Perk (formerly TravelPerk) uses API key authentication for accessing your own account data. The API key provides full admin access — treat it like a password. OAuth 2.0 is available for partners accessing other accounts, but for self-service use the API key approach. Perk supports per-assignment webhook URLs: create multiple webhooks in Perk, each pointing to a different assignment's dedicated URL shown in the {APP_NAME} UI.",
        },
    },
    "notion": {
        "name": "Notion",
        "description": "Pages, Databases, Comments, File uploads",
        "logo": "/logos/notion.svg",
        "category": "integration",
        "integration_category": "business_operations",
        "integration_types": ["documentation"],
        # OAuth (public integration) or an internal integration token in the
        # API Key field: both are accepted (#8386 makes the list binding).
        "auth_type": "oauth",
        "supported_auth_types": ["oauth", "api_key"],
        "fields": ["client_id", "client_secret", "api_key"],
        "scopes": "",
        "authorize_url": "https://api.notion.com/v1/oauth/authorize",
        "token_url": "https://api.notion.com/v1/oauth/token",
        "setup_guide": {
            "title": "Notion Integration Setup",
            "url": "https://www.notion.so/my-integrations",
            "steps": [
                "Go to notion.so/my-integrations > New integration.",
                "Name: '{APP_NAME}'. Select the workspace.",
                "For Internal integration (simpler): choose 'Internal', then copy the 'Internal Integration Secret' and paste it as the API Key below. No OAuth needed.",
                "For Public/OAuth integration: choose 'Public', set the redirect URI to {base_url}/api/oauth/callback, and copy the Client ID + Client Secret.",
                "Select capabilities: Read content, Update content, Insert content, Read comments (`comments:read`), and Create comments (`comments:write`). Under User capabilities, choose 'Read user information without email addresses' so agents can read the names of people @mentioned or assigned in pages.",
                "Share the pages/databases you want the agent to access with the integration in Notion.",
            ],
            "permissions": [
                {
                    "scope": "Read content",
                    "description": "Read pages, databases, and their content",
                },
                {
                    "scope": "Update content",
                    "description": "Create and edit pages and database entries",
                },
                {
                    "scope": "Insert content",
                    "description": "Create new pages and blocks",
                },
                {
                    "scope": "Read comments (`comments:read`)",
                    "description": "Read page-level and inline comments",
                },
                {
                    "scope": "Create comments (`comments:write`)",
                    "description": "Add page-level comments",
                },
                {
                    "scope": "Read user information without email addresses",
                    "description": "Read the names of people mentioned in pages, assigned in people properties, and who wrote comments or edited pages",
                },
            ],
            "notes": 'Two modes: (1) Internal integration — paste the secret as API Key, no OAuth needed. (2) Public/OAuth — use Client ID + Secret and authorize via OAuth. Internal integrations only work within the workspace where they were created. Notion capabilities are configured in the Notion integration settings (not requested dynamically in the OAuth URL), so enable `comments:read` and `comments:write` there before reconnecting if comment tools fail. Without a user information capability, Notion replaces every @mention of a person with "@Anonymous" and hides the names in people properties and comment authors; the connection test still passes, so enable it and reconnect if agents report people as anonymous. Uploading files to pages (the `upload_notion_file` tool) requires the `Insert content` capability. Remember to share pages with the integration in Notion.',
        },
    },
    "hubspot": {
        "name": "HubSpot",
        "description": (
            "CRM — Contacts, Companies, Deals, Tickets, Campaigns, Files and record attachments, record activity "
            "timelines (emails, calls, meetings, notes, tasks), forms and submissions, sequences, teams, batch "
            "reads and an inbound engagement report"
        ),
        "logo": "/logos/hubspot.svg",
        "category": "integration",
        "integration_category": "business_operations",
        "integration_types": ["crm"],
        "available_events": [
            {
                "event": "entity_created",
                "label": "Entity created",
                "description": "Triggers when a contact, company, deal, or other CRM entity is created in HubSpot.",
                "mechanism": "webhook",
                "filter_keys": _HUBSPOT_FILTER_KEYS,
            },
            {
                "event": "entity_updated",
                "label": "Entity property updated",
                "description": "Triggers when a property on a CRM entity is changed in HubSpot (e.g. email, stage, amount).",
                "mechanism": "webhook",
                "filter_keys": _HUBSPOT_FILTER_KEYS,
            },
            {
                "event": "entity_deleted",
                "label": "Entity deleted",
                "description": "Triggers when a contact, company, deal, or other CRM entity is deleted in HubSpot.",
                "mechanism": "webhook",
                "filter_keys": _HUBSPOT_FILTER_KEYS,
            },
        ],
        "element_types": [
            {
                "type": "crm_object_type",
                "name": "CRM entity type",
                "description": "Scope the trigger to specific HubSpot entity types (Contact, Company, Deal, Ticket, ...). Leave empty to react to every entity type.",
                #: The metadata keys that name the element to its tools.
                "identifiers": ["object_type"],
                "multiple": True,
            },
        ],
        "auth_type": "api_key",
        "fields": ["api_key"],
        "extra_fields": {"client_secret": "Client secret (for webhook signature validation)"},
        "setup_guide": {
            "title": "HubSpot Private App Setup",
            "url": "https://developers.hubspot.com/docs/api/private-apps",
            "steps": [
                "Log in to your HubSpot account as a Super Admin.",
                "Go to Settings > Integrations > Private Apps.",
                "Click 'Create a private app'.",
                "Name it '{APP_NAME}' and add a description.",
                "Go to the 'Scopes' tab and enable the scopes listed below.",
                "Click 'Create app', then copy the Access Token and Client Secret.",
                "Paste the Access Token in the 'API Key' field and the Client Secret in the 'Client secret' field below.",
                "To receive real-time CRM event notifications, configure a webhook:",
                "  - Go to the 'Webhooks' tab in your Private App settings.",
                "  - Set the Target URL to: {base_url}/api/hooks/integrations/<integration-id>",
                "  - Subscribe to the object events you want (e.g. contact.creation, contact.propertyChange, contact.deletion).",
                "  - HubSpot will POST event batches to the webhook URL when CRM objects change.",
            ],
            "permissions": [
                {
                    "scope": "crm.objects.contacts.read",
                    "description": "Read contacts and their properties",
                },
                {
                    "scope": "crm.objects.contacts.write",
                    "description": "Create and update contacts",
                },
                {
                    "scope": "crm.objects.companies.read",
                    "description": "Read company records",
                },
                {
                    "scope": "crm.objects.companies.write",
                    "description": "Create and update companies",
                },
                {
                    "scope": "crm.objects.deals.read",
                    "description": "Read deals and pipeline stages",
                },
                {
                    "scope": "crm.objects.deals.write",
                    "description": "Create and update deals",
                },
                {
                    "scope": "crm.objects.line_items.read",
                    "description": "Read deal line items (price, quantity, term, ACV, TCV)",
                },
                {
                    "scope": "crm.objects.line_items.write",
                    "description": "Create, update and delete deal line items",
                },
                {
                    "scope": "crm.objects.owners.read",
                    "description": "Read owner (team member) records",
                },
                {
                    "scope": "content",
                    "description": "Read marketing emails and campaigns",
                },
                {"scope": "tickets", "description": "Read and manage support tickets"},
                {
                    "scope": "files",
                    "description": "Search, download and upload files in the file manager",
                },
                {
                    "scope": "files.ui_hidden.read",
                    "description": "Read the files attached to CRM records (notes, emails, calls, meetings, tasks)",
                },
                {
                    "scope": "sales-email-read",
                    "description": "Read the sales emails logged on records and their attachments",
                },
                {
                    "scope": "crm.objects.emails.read",
                    "description": "Read email engagements (sender, recipients, subject, body, opens)",
                },
                {
                    "scope": "crm.objects.notes.read",
                    "description": "Read the notes logged on records",
                },
                {
                    "scope": "crm.objects.notes.write",
                    "description": "Write notes on records (create_hubspot_note)",
                },
                {
                    "scope": "crm.objects.calls.read",
                    "description": "Read logged calls (direction, outcome, duration, notes)",
                },
                {
                    "scope": "crm.objects.meetings.read",
                    "description": "Read meetings (time, outcome, notes)",
                },
                {
                    "scope": "crm.objects.tasks.read",
                    "description": "Read tasks (type, status, notes)",
                },
                {
                    "scope": "crm.objects.tasks.write",
                    "description": "Create tasks on records (create_hubspot_task)",
                },
                {
                    "scope": "forms",
                    "description": "List forms and read their submissions (values, page, time)",
                },
                {
                    "scope": "automation.sequences.read",
                    "description": "List sequences and read a contact's sequence enrollment",
                },
                {
                    "scope": "crm.schemas.contacts.read",
                    "description": "Read contact property definitions (labels of lifecycle stages, lead statuses and other options)",
                },
                {
                    "scope": "crm.schemas.companies.read",
                    "description": "Read company property definitions (option labels)",
                },
                {
                    "scope": "crm.schemas.deals.read",
                    "description": "Read deal property definitions (option labels)",
                },
                {
                    "scope": "settings.users.teams.read",
                    "description": "List teams and their members (owner regions)",
                },
            ],
            "notes": "HubSpot Private Apps provide both an Access Token (for API calls) and a Client Secret (for webhook signature validation). The token is scoped to the permissions you select when creating the app. Super Admin access is required to create private apps. To receive webhook events, configure subscriptions in the Private App's Webhooks tab. Each tool states the scopes it needs in its description, and a tool that meets a missing scope names the scope and itself in its error: add the scope to the private app (the token keeps working) and retry. The activity tools (timeline, engagements, forms, sequences, teams, inbound report) need the engagement, forms, sequences, schema and teams scopes above; without one, the rest still works.",
        },
    },
    "jira": {
        "name": "Atlassian Jira",
        "description": "Issues (with full custom-field support), JQL search, fields & metadata, comments, worklogs, attachments, boards, sprints, backlog, epics, projects, versions, components, filters, dashboards, users, and Service Management (requests, queues, SLA, approvals)",
        "logo": "/logos/jira.svg",
        "category": "integration",
        "integration_category": "development",
        # Issues and Service Management: Jira fills the Ticketing capability
        # slot of a fleet (services.fleet_capability_slots). Not a code host:
        # never typed "repository", the Source code capability's type (#1434).
        "integration_types": ["ticketing", "itsm"],
        "auth_type": "oauth",
        # What its sign-in learns about the account: a person's own credential
        # never reads the integration's.
        "account_config_keys": ["sites", "cloud_id"],
        "fields": ["client_id", "client_secret"],
        # Polled from the site's issues (services/jira_events.py, every 60 s):
        # one JQL search of the issues changed since the last poll, with their
        # changelog and comments.
        "available_events": [
            {
                "event": event,
                "label": label,
                "description": (
                    f"Triggers when {when}. Carries issue_key, summary, status, issue_type, project, "
                    "assignee, previous_assignee (issue_assigned), reporter, actor, the changelog items "
                    "and the comment (comment_created). Uses polling (every minute); the first poll "
                    "starts from now. Each change runs once."
                ),
                "mechanism": "polling",
                "filter_keys": {
                    "assignee_account_id": (
                        "Only issues assigned to these Atlassian account ids; 'self' is the account "
                        "the integration is connected as"
                    ),
                    "project_key": "Only issues of these project keys",
                    "issue_type": "Only issues of these types (Task, Bug, ...)",
                    "status": "Only issues in these statuses (the new status for issue_transitioned)",
                    "jql": "Free JQL ANDed into the search (e.g. labels = urgent)",
                },
            }
            for event, label, when in (
                ("issue_assigned", "Issue assigned", "the assignee of an issue changes"),
                ("issue_created", "Issue created", "an issue is created"),
                ("issue_updated", "Issue updated", "an issue changes (one run per change)"),
                ("issue_transitioned", "Issue transitioned", "the status of an issue changes"),
                ("comment_created", "Comment added", "a comment is added to an issue"),
            )
        ],
        "scopes": (
            "read:me "
            "read:jira-work write:jira-work read:jira-user "
            "manage:jira-project manage:jira-configuration manage:jira-webhook "
            "manage:jira-data-provider "
            "read:servicedesk-request write:servicedesk-request "
            "manage:servicedesk-customer "
            "offline_access"
        ),
        "authorize_url": "https://auth.atlassian.com/authorize",
        "token_url": "https://auth.atlassian.com/oauth/token",
        "setup_guide": {
            "title": "Atlassian OAuth App Setup",
            "url": "https://developer.atlassian.com/console/myapps/",
            "steps": [
                "Go to developer.atlassian.com > Console > Create > OAuth 2.0 integration.",
                "App name: '{APP_NAME}'.",
                "Under 'Authorization', add a callback URL: {base_url}/api/oauth/callback",
                "Under 'Permissions', add all Jira Platform REST API and Jira Service Management API scopes listed below.",
                "Go to 'Settings' and copy the Client ID and Client Secret.",
                "The user will authorize access to specific Atlassian sites during the OAuth flow.",
            ],
            "permissions": [
                {
                    "scope": "read:me",
                    "description": "Read the authenticated user's Atlassian profile (required for /me endpoint)",
                },
                {
                    "scope": "read:jira-work",
                    "description": "View Jira issue data — read projects, issues, attachments, worklogs",
                },
                {
                    "scope": "write:jira-work",
                    "description": "Create and manage issues — create/edit issues, post comments, create worklogs, delete issues",
                },
                {
                    "scope": "read:jira-user",
                    "description": "View user profiles — usernames, email addresses, and avatars",
                },
                {
                    "scope": "manage:jira-project",
                    "description": "Manage project settings — create/edit project settings, versions, components",
                },
                {
                    "scope": "manage:jira-configuration",
                    "description": "Manage Jira global settings — create projects, custom fields, manage assignments",
                },
                {
                    "scope": "manage:jira-webhook",
                    "description": "Manage Jira webhooks — register, refresh, and delete dynamic webhooks",
                },
                {
                    "scope": "manage:jira-data-provider",
                    "description": "Manage development and release information for third parties",
                },
                {
                    "scope": "read:servicedesk-request",
                    "description": "View JSM request data — customer requests, approvals, comments, service desks",
                },
                {
                    "scope": "write:servicedesk-request",
                    "description": "Create and manage JSM requests — create/edit requests, add comments, approve, transition",
                },
                {
                    "scope": "manage:servicedesk-customer",
                    "description": "Manage JSM customers and organizations",
                },
            ],
            "notes": "Atlassian OAuth uses 3LO (3-legged OAuth). The user picks which Atlassian site(s) to grant access to during authorization. The app requires both Jira Platform and Jira Service Management scopes for full functionality. Admin consent may be required for some organizations.",
        },
        "element_types": [
            {
                "type": "jira_project",
                "name": "Jira Project",
                "description": "Select specific Jira projects for this agent/assignment to focus on",
                #: The metadata keys that name the element to its tools.
                "identifiers": ["key"],
                "multiple": True,
            },
        ],
    },
    "drata": {
        "name": "Drata",
        "description": "Compliance GRC — Policies, Controls, Risks, Personnel, Vendors, Evidence, Assets, Frameworks, Audits, Monitoring Tests, Version Control identities (full read + write)",
        "logo": "/logos/drata.svg",
        "category": "integration",
        "integration_category": "grc",
        "integration_types": ["compliance"],
        "auth_type": "api_key",
        "fields": ["api_key"],
        "extra_fields": {"region": "API Region"},
        "extra_field_options": {
            "region": [
                {"value": "us", "label": "US — public-api.drata.com"},
                {"value": "eu", "label": "EU — public-api.eu.drata.com"},
                {"value": "apac", "label": "APAC — public-api.apac.drata.com"},
            ],
        },
        "setup_guide": {
            "title": "Drata API Key Setup",
            "url": "https://app.drata.com/account-settings/api-keys",
            "steps": [
                "Go to Drata > Account Settings > API Keys.",
                "Click 'Generate New API Key'.",
                "Give it a descriptive name like '{APP_NAME}'.",
                "Grant the required permissions (see list below). For full agentic control select both List/Read **and** Create/Update/Delete permissions for every resource you want the agent to manage.",
                "Copy the API key and paste it below.",
            ],
            "permissions": [
                {
                    "scope": "Policies (read + acknowledge)",
                    "description": "Read policies, versions, and their content; acknowledge assigned policies for users",
                },
                {
                    "scope": "Controls (read + write) + Map/Delete External Evidence",
                    "description": "Read, create, update controls. Manage control notes and owners. The 'Controls: Map External Evidence' and 'Controls: Delete Mapped External Evidence' permissions are required to list, upload, and remove evidence mapped directly to controls (a separate object family from the Evidence Library).",
                },
                {
                    "scope": "Risk Management (read + write)",
                    "description": "Read, create, update risks and risk categories across the risk register",
                },
                {
                    "scope": "Personnel (read + write)",
                    "description": "Read personnel records and compliance training status; create/update personnel; log background checks",
                },
                {
                    "scope": "Vendors (read + write)",
                    "description": "Read, create, update, archive vendors. Send security questionnaires and review vendor documents.",
                },
                {
                    "scope": "Evidence (read + write)",
                    "description": "Read, create, update and delete items in the evidence library and link them to controls",
                },
                {
                    "scope": "Assets (read + write)",
                    "description": "Read, create, update and delete registered assets; inspect device compliance",
                },
                {
                    "scope": "Frameworks & Audits (read)",
                    "description": "Read compliance frameworks, requirements, and audits",
                },
                {
                    "scope": "Monitoring Tests (read) + Trigger Monitor Test",
                    "description": "Read monitoring tests, their failures, and exclusions. The 'Monitoring: Trigger Monitor Test' permission is required to re-run a test on demand.",
                },
                {
                    "scope": "Connections: Manage Version Control User Identities",
                    "description": "List Version Control user identities (GitHub, GitLab, Bitbucket, Azure Repos, AWS CodeCommit), link them to Drata users, set their Write access / Merge to default branch flags, and mark service accounts.",
                },
                {
                    "scope": "Users, Roles, Connections, Events (read)",
                    "description": "Read users, roles, custom connections, and the audit-log event stream",
                },
            ],
            "notes": "API keys in Drata inherit the permissions of the user who created them. To unlock the full write surface (create/update/delete across controls, risks, vendors, evidence, assets, personnel) the creating account must have admin-level access in Drata. The Drata Public API v2 is supported across all regions.",
        },
    },
    "infoblox": {
        "name": "Infoblox",
        "description": (
            "DDI + DNS security (Infoblox BloxOne / Cloud Services Portal) - IPAM "
            "(IP spaces, subnets with utilization, addresses, ranges, hosts), DNS "
            "(authoritative zones, resource records, views), and BloxOne Threat "
            "Defense (TIDE threat-intel IOC lookup by host/ip/url, security "
            "policies, and custom/named lists to block indicators at the DNS layer)"
        ),
        "logo": "/logos/infoblox.svg",
        "category": "integration",
        "integration_category": "threat_intelligence",
        "integration_types": ["threat_intel", "ddi"],
        # Infoblox CSP authenticates with a single user/service API key sent as
        # "Authorization: Token <key>" - there is no OAuth, so the only supported
        # auth mode is "api_key".
        "auth_type": "api_key",
        "supported_auth_types": ["api_key"],
        "fields": ["api_key"],
        "extra_fields": {
            "base_url": "Infoblox CSP URL (default https://csp.infoblox.com)",
        },
        "setup_guide": {
            "title": "Infoblox CSP API Key Setup",
            "url": "https://www.infoblox.com/developer-portal/getting-started/",
            "steps": [
                "Log in to the Infoblox Portal (Cloud Services Portal) at csp.infoblox.com.",
                "We recommend creating a dedicated service user (Administration > User Access) with a role granting only the access the agent needs - a read-only role for DDI/DNS/threat visibility, plus Threat Defense custom-list write access if you want the agent to manage blocking lists.",
                "Sign in as that user, then open User Preferences (click the account menu, bottom-left) and scroll to the 'User API Keys' section.",
                "Click 'Create', give the key a descriptive name like '{APP_NAME}', set an expiration, then copy the generated key (it is shown only once).",
                "Paste the key into the 'API Key' field below.",
                "Leave 'base_url' as https://csp.infoblox.com unless you use a dedicated CSP tenant URL.",
                "Click 'Test Connection' to verify the key.",
            ],
            "permissions": [
                {
                    "scope": "DDI / IPAM (read)",
                    "description": "List IP spaces, subnets, addresses, ranges, and hosts",
                },
                {
                    "scope": "DNS (read)",
                    "description": "List authoritative zones, resource records, and views",
                },
                {
                    "scope": "Threat Defense / TIDE (read)",
                    "description": "Look up IOCs in TIDE threat intelligence and read security policies",
                },
                {
                    "scope": "Threat Defense custom lists (read + write)",
                    "description": "List custom/named lists and add/remove IOCs for DNS-layer blocking",
                },
            ],
            "notes": (
                "Infoblox CSP authenticates with a user API key sent in the "
                "Authorization header with the 'Token' prefix (Authorization: "
                "Token <key>) - NOT Bearer. API keys inherit the permissions of "
                "the user that created them, so use a dedicated service user with "
                "a scoped role. DDI/DNS calls target /api/ddi/v1, BloxOne Threat "
                "Defense firewall /api/atcfw/v1, and TIDE threat intel "
                "/tide/api/data."
            ),
        },
    },
    "ses": {
        "name": "Amazon SES",
        "description": "Email distribution at scale — send simple, templated, and bulk emails; manage verified identities, templates, configuration sets, the suppression list, and contact lists (Amazon SES v2 API)",
        "logo": "/logos/ses.svg",
        "category": "integration",
        "integration_category": "communication",
        "integration_types": ["email"],
        # SES authenticates with AWS IAM credentials (an access key) and signs
        # every request with AWS Signature V4 — there is no OAuth flow, so the
        # only supported auth mode is "api_key".
        "auth_type": "api_key",
        "supported_auth_types": ["api_key"],
        # The Secret Access Key (the credential) is stored as the API key; the
        # Access Key ID, region, and optional default configuration set live in
        # extra config.
        "fields": ["api_key"],
        "extra_fields": {
            "access_key_id": "AWS Access Key ID",
            "region": "AWS Region",
            "configuration_set": "Default Configuration Set (optional)",
        },
        "extra_field_options": {
            "region": [
                {"value": "us-east-1", "label": "US East (N. Virginia) - us-east-1"},
                {"value": "us-east-2", "label": "US East (Ohio) - us-east-2"},
                {"value": "us-west-1", "label": "US West (N. California) - us-west-1"},
                {"value": "us-west-2", "label": "US West (Oregon) - us-west-2"},
                {"value": "ca-central-1", "label": "Canada (Central) - ca-central-1"},
                {"value": "eu-west-1", "label": "Europe (Ireland) - eu-west-1"},
                {"value": "eu-west-2", "label": "Europe (London) - eu-west-2"},
                {"value": "eu-west-3", "label": "Europe (Paris) - eu-west-3"},
                {"value": "eu-central-1", "label": "Europe (Frankfurt) - eu-central-1"},
                {"value": "eu-north-1", "label": "Europe (Stockholm) - eu-north-1"},
                {"value": "eu-south-1", "label": "Europe (Milan) - eu-south-1"},
                {"value": "ap-south-1", "label": "Asia Pacific (Mumbai) - ap-south-1"},
                {"value": "ap-southeast-1", "label": "Asia Pacific (Singapore) - ap-southeast-1"},
                {"value": "ap-southeast-2", "label": "Asia Pacific (Sydney) - ap-southeast-2"},
                {"value": "ap-northeast-1", "label": "Asia Pacific (Tokyo) - ap-northeast-1"},
                {"value": "ap-northeast-2", "label": "Asia Pacific (Seoul) - ap-northeast-2"},
                {"value": "sa-east-1", "label": "South America (Sao Paulo) - sa-east-1"},
            ],
        },
        "setup_guide": {
            "title": "Amazon SES IAM Access Key Setup",
            "url": "https://docs.aws.amazon.com/ses/latest/dg/setting-up.html",
            "steps": [
                "In the AWS console, open Amazon SES and select the AWS Region you want to send from (SES resources are region-scoped). Note the region code (e.g. us-east-1).",
                "Verify at least one sender identity (an email address or a domain) in SES > Configuration > Identities. You can also do this later with the create_ses_identity tool.",
                "If your account is still in the SES sandbox, request production access (SES > Account dashboard) to send to unverified recipients and raise the sending quota.",
                "Open the IAM console > Users > Create user (e.g. '{APP_NAME}-ses'). Do NOT enable console access.",
                "Attach a permissions policy: the AWS-managed 'AmazonSESFullAccess' for the full tool set, or a custom policy granting the granular ses:* actions listed below (least privilege).",
                "Open the new user > Security credentials > Create access key > 'Application running outside AWS'. Copy the Access Key ID and Secret Access Key (the secret is shown only once).",
                "Paste the Secret Access Key into the 'API Key' field above, the Access Key ID into the 'AWS Access Key ID' field, and select your AWS Region below.",
                "Optionally set a default Configuration Set so all sends are tracked together (open/click/bounce/complaint event publishing).",
                "Click 'Test Connection' to verify the credentials and region (calls GetAccount).",
            ],
            "permissions": [
                {
                    "scope": "ses:SendEmail, ses:SendBulkEmail",
                    "description": "Send simple, templated, and bulk emails",
                },
                {
                    "scope": "ses:GetAccount",
                    "description": "Read sending quota, send rate, reputation, and sandbox/production status",
                },
                {
                    "scope": "ses:ListEmailIdentities, ses:GetEmailIdentity, ses:CreateEmailIdentity, ses:DeleteEmailIdentity",
                    "description": "List, inspect, verify, and delete sender identities (email addresses and domains)",
                },
                {
                    "scope": "ses:ListEmailTemplates, ses:GetEmailTemplate, ses:CreateEmailTemplate, ses:UpdateEmailTemplate, ses:DeleteEmailTemplate",
                    "description": "Manage reusable email templates",
                },
                {
                    "scope": "ses:ListConfigurationSets, ses:GetConfigurationSet, ses:CreateConfigurationSet, ses:DeleteConfigurationSet",
                    "description": "Manage configuration sets (sending rules and event publishing)",
                },
                {
                    "scope": "ses:ListSuppressedDestinations, ses:GetSuppressedDestination, ses:PutSuppressedDestination, ses:DeleteSuppressedDestination",
                    "description": "Manage the account-level suppression list",
                },
                {
                    "scope": "ses:ListContactLists, ses:GetContactList, ses:CreateContactList, ses:DeleteContactList, ses:ListContacts, ses:GetContact, ses:CreateContact, ses:UpdateContact, ses:DeleteContact",
                    "description": "Manage contact lists and contacts (topic subscriptions for unsubscribe handling)",
                },
            ],
            "notes": "Amazon SES resources (identities, templates, configuration sets, contact lists, the suppression list) are scoped to a single AWS Region, so pick the region where you verified your sender. New accounts start in the SES sandbox (can only send to verified addresses, low quota) until you request production access. For best deliverability, verify a domain with DKIM and configure a custom MAIL FROM domain. Credentials are stored encrypted; rotate the IAM access key periodically.",
        },
    },
    "letsignit": {
        "name": "Letsignit",
        "description": "Email signatures (Letsignit): what agents send carries each person's current signature and banner - the replies of an Outlook mailbox agent account, and the drafts and emails of the Outlook mail tools of an agent this integration is bound to - and Letsignit users and their signature variables can be read and managed",
        "logo": "/logos/letsignit.png",
        "category": "integration",
        "integration_category": "communication",
        "integration_types": ["email"],
        # The Letsignit public API authenticates with a single API key (scope
        # "api") sent as "Authorization: Bearer <key>"; there is no OAuth.
        "auth_type": "api_key",
        "supported_auth_types": ["api_key"],
        "fields": ["api_key"],
        "setup_guide": {
            "title": "Letsignit API Key Setup",
            "url": "https://help.letsignit.com/en/articles/496032-api-documentation-signature-retrieval",
            "steps": [
                "Sign in to the Letsignit administration platform as an administrator.",
                "Open Settings > API keys and create a key with the Public API ('api') scope.",
                "Copy the key and paste it into the 'API Key' field below.",
                "Click 'Test Connection': it reads the signature Letsignit composes for the integration's owner (a person Letsignit holds no signature for still proves the key works).",
                "To sign the replies of an Outlook mailbox agent account, pick this integration as the account's signature (Replies > Signature). To sign what an agent writes with its Outlook mail tools, bind this integration to the agent next to its Microsoft integration.",
            ],
            "permissions": [
                {
                    "scope": "api",
                    "description": "Read the signature of a person; read, create, update the variables of and delete Letsignit users",
                },
            ],
            "notes": "The signature is read by the email address of the mailbox that sends, when each email is sent, and kept for 10 minutes. If your tenant signs mail on the server side (the Letsignit SMTP connector), every outgoing email is already signed in transit: choose 'No signature' on the mailbox agent accounts instead, so their replies are not signed twice.",
        },
    },
    "aws_security": {
        "name": "AWS Security",
        "description": "AWS-native security operations — Amazon GuardDuty threat-detection findings (list/get/statistics/archive), AWS Security Hub aggregated findings, insights, and workflow triage (NEW/NOTIFIED/RESOLVED/SUPPRESSED), and AWS CloudTrail audit-event lookup for investigations",
        "logo": "/logos/aws_security.svg",
        "category": "integration",
        "integration_category": "detection_response",
        "integration_types": ["siem", "threat_intel"],
        # AWS authenticates with IAM credentials (an access key) and signs
        # every request with AWS Signature V4 — there is no OAuth flow, so the
        # only supported auth mode is "api_key".
        "auth_type": "api_key",
        "supported_auth_types": ["api_key"],
        # The Secret Access Key (the credential) is stored as the API key; the
        # Access Key ID and region live in extra config.
        "fields": ["api_key"],
        "extra_fields": {
            "access_key_id": "AWS Access Key ID",
            "region": "AWS Region",
        },
        "extra_field_options": {
            "region": [
                {"value": "us-east-1", "label": "US East (N. Virginia) - us-east-1"},
                {"value": "us-east-2", "label": "US East (Ohio) - us-east-2"},
                {"value": "us-west-1", "label": "US West (N. California) - us-west-1"},
                {"value": "us-west-2", "label": "US West (Oregon) - us-west-2"},
                {"value": "ca-central-1", "label": "Canada (Central) - ca-central-1"},
                {"value": "eu-west-1", "label": "Europe (Ireland) - eu-west-1"},
                {"value": "eu-west-2", "label": "Europe (London) - eu-west-2"},
                {"value": "eu-west-3", "label": "Europe (Paris) - eu-west-3"},
                {"value": "eu-central-1", "label": "Europe (Frankfurt) - eu-central-1"},
                {"value": "eu-north-1", "label": "Europe (Stockholm) - eu-north-1"},
                {"value": "eu-south-1", "label": "Europe (Milan) - eu-south-1"},
                {"value": "ap-south-1", "label": "Asia Pacific (Mumbai) - ap-south-1"},
                {"value": "ap-southeast-1", "label": "Asia Pacific (Singapore) - ap-southeast-1"},
                {"value": "ap-southeast-2", "label": "Asia Pacific (Sydney) - ap-southeast-2"},
                {"value": "ap-northeast-1", "label": "Asia Pacific (Tokyo) - ap-northeast-1"},
                {"value": "ap-northeast-2", "label": "Asia Pacific (Seoul) - ap-northeast-2"},
                {"value": "sa-east-1", "label": "South America (Sao Paulo) - sa-east-1"},
            ],
        },
        "setup_guide": {
            "title": "AWS Security IAM Access Key Setup",
            "url": "https://docs.aws.amazon.com/IAM/latest/UserGuide/id_credentials_access-keys.html",
            "steps": [
                "Pick the AWS Region where GuardDuty and Security Hub are enabled (both are region-scoped) and note the region code (e.g. us-east-1).",
                "Ensure Amazon GuardDuty is enabled (GuardDuty console > Get started) and AWS Security Hub is enabled (Security Hub console) in that region. CloudTrail event history requires no setup.",
                "Open the IAM console > Users > Create user (e.g. '{APP_NAME}-security'). Do NOT enable console access.",
                "Attach a permissions policy: for reads, the AWS-managed 'AmazonGuardDutyReadOnlyAccess', 'AWSSecurityHubReadOnlyAccess', and 'AWSCloudTrail_ReadOnlyAccess'; add the granular write actions listed below (guardduty:ArchiveFindings, guardduty:UnarchiveFindings, securityhub:BatchUpdateFindings) only if the agent should triage findings.",
                "Open the new user > Security credentials > Create access key > 'Application running outside AWS'. Copy the Access Key ID and Secret Access Key (the secret is shown only once).",
                "Paste the Secret Access Key into the 'API Key' field above, the Access Key ID into the 'AWS Access Key ID' field, and select your AWS Region below.",
                "Click 'Test Connection' to verify the credentials (calls STS GetCallerIdentity).",
            ],
            "permissions": [
                {
                    "scope": "sts:GetCallerIdentity",
                    "description": "Verify the credentials and report the account/principal (allowed for every IAM principal)",
                },
                {
                    "scope": "guardduty:ListDetectors, guardduty:GetDetector",
                    "description": "Discover the GuardDuty detector in the region",
                },
                {
                    "scope": "guardduty:ListFindings, guardduty:GetFindings, guardduty:GetFindingsStatistics",
                    "description": "List, read, and aggregate GuardDuty threat-detection findings",
                },
                {
                    "scope": "guardduty:ArchiveFindings, guardduty:UnarchiveFindings",
                    "description": "Archive and reopen GuardDuty findings (triage; administrator account only)",
                },
                {
                    "scope": "securityhub:DescribeHub, securityhub:GetEnabledStandards",
                    "description": "Read the Security Hub status and enabled security standards",
                },
                {
                    "scope": "securityhub:GetFindings, securityhub:GetInsights, securityhub:GetInsightResults",
                    "description": "List and read aggregated findings and insights",
                },
                {
                    "scope": "securityhub:BatchUpdateFindings",
                    "description": "Update finding workflow status (NEW/NOTIFIED/RESOLVED/SUPPRESSED) and notes",
                },
                {
                    "scope": "cloudtrail:LookupEvents",
                    "description": "Look up management events for audit investigations (90-day window)",
                },
            ],
            "notes": "GuardDuty and Security Hub are region-scoped — the integration talks to a single AWS Region, so configure one integration per region you monitor. CloudTrail LookupEvents covers the last 90 days of management events and is rate-limited by AWS to 2 requests per second. Archiving GuardDuty findings requires the GuardDuty administrator account (member accounts cannot archive). Credentials are stored encrypted; rotate the IAM access key periodically and prefer read-only policies unless the agent should triage findings.",
        },
    },
    "rapid7": {
        "name": "Rapid7",
        "description": "Rapid7 Insight Platform - detection & response (InsightIDR investigations, alerts, comments, and LEQL log search), vulnerability management (InsightVM assets and vulnerability definitions with CVSS/CVEs), and threat intelligence (Community Threat IOC management)",
        "logo": "/logos/rapid7.svg",
        "category": "integration",
        "integration_category": "exposure_management",
        "integration_types": ["vulnerability_management", "siem", "threat_intel"],
        # The Insight Platform authenticates with a single API key sent in the
        # X-Api-Key header - there is no OAuth, so the only supported auth mode
        # is "api_key".
        "auth_type": "api_key",
        "supported_auth_types": ["api_key"],
        "fields": ["api_key"],
        "extra_fields": {
            "base_url": "Insight Platform region API host (default https://us.api.insight.rapid7.com)",
        },
        "extra_field_options": {
            "base_url": [
                {
                    "value": "https://us.api.insight.rapid7.com",
                    "label": "United States - 1 (us)",
                },
                {
                    "value": "https://us2.api.insight.rapid7.com",
                    "label": "United States - 2 (us2)",
                },
                {
                    "value": "https://us3.api.insight.rapid7.com",
                    "label": "United States - 3 (us3)",
                },
                {
                    "value": "https://eu.api.insight.rapid7.com",
                    "label": "Europe (eu)",
                },
                {
                    "value": "https://ca.api.insight.rapid7.com",
                    "label": "Canada (ca)",
                },
                {
                    "value": "https://au.api.insight.rapid7.com",
                    "label": "Australia (au)",
                },
                {
                    "value": "https://ap.api.insight.rapid7.com",
                    "label": "Japan / Asia Pacific (ap)",
                },
            ],
        },
        "setup_guide": {
            "title": "Rapid7 Insight Platform API Key Setup",
            "url": "https://docs.rapid7.com/insight/managing-platform-api-keys/",
            "steps": [
                "Log in to the Rapid7 Insight Platform at insight.rapid7.com.",
                "Determine your data region: open a product (e.g. InsightIDR) and read the full region prefix from the host in the URL - one of us, us2, us3, eu, ca, au, or ap (us2 and us3 are distinct regions from us, so use the whole prefix, not just the first two letters). Select the matching API host in the 'base_url' field below.",
                "We recommend a User API Key scoped to a dedicated service account whose product role grants only the access the agent needs (read-only for visibility and hunting; investigation-management for closing/assigning).",
                "Go to your profile menu > Platform Home > API Keys (or 'Manage Platform API Keys'). Under 'User API Keys' click 'New User Key', name it, and click Create.",
                "Copy the generated key (it is shown only once) and paste it into the 'API Key' field above.",
                "Click 'Test Connection' to verify the key (calls GET {base_url}/validate, which returns 'Authorized').",
            ],
            "permissions": [
                {
                    "scope": "InsightIDR - Investigations & Comments",
                    "description": "List/search/get investigations, set status/priority/disposition/assignee, add comments, and list associated alerts",
                },
                {
                    "scope": "InsightIDR - Log Search",
                    "description": "List logs/logsets and run LEQL queries for threat hunting",
                },
                {
                    "scope": "InsightVM - Vulnerability Management",
                    "description": "Search assets and vulnerability definitions and read their CVSS/CVE detail",
                },
                {
                    "scope": "InsightIDR - Community Threats",
                    "description": "Create custom threats and add/replace their indicators (IOCs)",
                },
            ],
            "notes": (
                "The Insight Platform authenticates with a single API key sent in the "
                "X-Api-Key header (no OAuth). The platform is region-scoped: every "
                "product API lives under https://{region}.api.insight.rapid7.com "
                "(regions us, us2, us3, eu, ca, au, ap) - set the region host as the "
                "base URL. A User API Key inherits the owner's product entitlements "
                "and role, which bound what the agent can do; an Organization Key "
                "(Platform Admin only) grants broader access. Keys are shown only once "
                "on creation, so store them securely - they are kept encrypted here."
            ),
        },
    },
    "misp": {
        "name": "MISP",
        "description": (
            "Open Source Threat Intelligence Platform (MISP) - search and "
            "manage threat events and attributes (IOCs), tag with taxonomies "
            "and galaxies (threat actors, malware, ATT&CK), record sightings "
            "and false positives, check indicators against warninglists, "
            "browse structured objects, and manage OSINT feeds"
        ),
        "logo": "/logos/misp.svg",
        "category": "integration",
        "integration_category": "threat_intelligence",
        "integration_types": ["threat_intel"],
        # MISP authenticates with a single automation key sent RAW in the
        # Authorization header (no Bearer/Token prefix) - there is no OAuth,
        # so the only supported auth mode is "api_key".
        "auth_type": "api_key",
        "supported_auth_types": ["api_key"],
        "fields": ["api_key"],
        "extra_fields": {
            "base_url": "MISP instance URL (e.g. https://misp.company.com)",
        },
        "setup_guide": {
            "title": "MISP Automation Key Setup",
            "url": "https://www.misp-project.org/openapi/",
            "steps": [
                "Log in to your MISP instance.",
                "We recommend creating a dedicated service user (Administration > Add User) with a role granting only the access the agent needs - 'Read Only' for pure lookups, 'User' for event/attribute/sighting management, or an admin role if the agent should list users.",
                "Sign in as that user, then go to Global Actions > My Profile and open the 'Auth keys' tab.",
                "Click 'Add authentication key', add a comment like '{APP_NAME}', optionally pin allowed IPs and an expiration, and decide whether the key should be read-only.",
                "Copy the generated key (shown only once) and paste it into the 'API Key' field below.",
                "Set 'base_url' to your MISP instance URL (e.g. https://misp.company.com).",
                "Click 'Test Connection' to verify the key.",
            ],
            "permissions": [
                {
                    "scope": "Events & Attributes (read + write)",
                    "description": "Search, read, create, update, publish, and delete threat events and their IOC attributes",
                },
                {
                    "scope": "Tags & Sightings (read + write)",
                    "description": "Attach/remove taxonomy and galaxy tags; record sightings, false positives, and expirations",
                },
                {
                    "scope": "Reference data (read)",
                    "description": "Galaxies and clusters, warninglists, objects, organisations",
                },
                {
                    "scope": "Feeds (read + manage)",
                    "description": "List feeds and enable/disable/fetch/cache them",
                },
                {
                    "scope": "Users (read, optional)",
                    "description": "List user accounts - requires the key to belong to an admin user",
                },
            ],
            "notes": (
                "MISP authenticates with an automation key sent RAW in the "
                "Authorization header (Authorization: <key> - no Bearer/Token "
                "prefix). Keys inherit the role and permissions of the user "
                "that created them, so use a dedicated service user with a "
                "scoped role; keys can also be made read-only, IP-pinned, and "
                "expiring. The API lives at the instance web UI host (no /api "
                "prefix). Note that MISP returns HTTP 403 (not 401) for an "
                "invalid key. The key is stored encrypted here."
            ),
        },
    },
    "mitre_atlas": {
        "name": "MITRE ATLAS",
        "description": (
            "MITRE ATLAS (Adversarial Threat Landscape for AI Systems) - "
            "look up and search AI/ML attack tactics, techniques, "
            "mitigations, and real-world case studies, traverse the "
            "relationship graph, and cross-walk ATT&CK ids to their ATLAS "
            "counterparts. Open data published by MITRE - no API key needed"
        ),
        "logo": "/logos/mitre_atlas.svg",
        "category": "integration",
        "integration_category": "threat_intelligence",
        "integration_types": ["threat_intel"],
        # ATLAS has no API and no credentials: the knowledge base is
        # published openly by MITRE in the mitre-atlas/atlas-data GitHub
        # repository and fetched over plain HTTPS.
        "auth_type": "none",
        # Every extra field is a tuning knob with a working default — the
        # integration is fully functional with all of them blank, so it is
        # marked "connected" at creation time.
        "extra_fields_optional": True,
        "fields": [],
        "extra_fields": {
            "source_url": (
                "Optional ATLAS.yaml source URL override for mirrors / "
                "air-gapped hosts (default: the official "
                "mitre-atlas/atlas-data dist/v6/ATLAS-latest.yaml release)"
            ),
        },
        "setup_guide": {
            "title": "MITRE ATLAS Setup",
            "url": "https://github.com/mitre-atlas/atlas-data",
            "steps": [
                "No credentials are required - MITRE publishes the ATLAS knowledge base openly.",
                "Leave 'source_url' empty to use the official release from github.com/mitre-atlas/atlas-data (recommended).",
                "If your environment cannot reach raw.githubusercontent.com, mirror the data format v6 ATLAS.yaml file internally and set 'source_url' to the mirror URL.",
                "Click 'Test Connection' to fetch and validate the dataset (release version and object counts are reported).",
                "Bind the integration to an agent (agent > Integrations) to give it the ATLAS search, lookup, and pivot tools - this integration provides live agent tools, it is not a knowledge base import source.",
            ],
            "permissions": [
                {
                    "scope": "Public ATLAS dataset (read-only)",
                    "description": (
                        "Fetch the openly published ATLAS dataset over HTTPS - "
                        "tactics, techniques, mitigations, case studies, and "
                        "relationships. No platform knowledge base is created; "
                        "agents query the data live"
                    ),
                },
            ],
            "notes": (
                "MITRE ATLAS is the AI/ML security counterpart to ATT&CK: it "
                "catalogs how adversaries attack AI-enabled systems "
                "(Predictive AI, Generative AI, Agentic AI) with real-world "
                "incident case studies. The dataset (~620 KB YAML) is "
                "downloaded on first use and cached in-process for 6 hours - "
                "content releases ship roughly monthly. All deep links point "
                "to the public https://atlas.mitre.org website."
            ),
        },
    },
    "mitre_attack": {
        "name": "MITRE ATT&CK",
        "description": (
            "MITRE ATT&CK knowledge base (free, no API key) - search and look "
            "up adversary techniques, tactics, groups, software, mitigations, "
            "campaigns, data sources/components, ICS assets, and detection "
            "strategies across the Enterprise, Mobile, and ICS domains, with "
            "full relationship traversal (techniques used by a group, groups "
            "using a technique, mitigations and detections for a technique, "
            "campaign attribution, procedure examples)"
        ),
        "logo": "/logos/mitre_attack.svg",
        "category": "integration",
        # "threat_intelligence" matches the marketplace taxonomy bucket used
        # by the other threat-intel providers (MISP, GreyNoise, Filigran AI).
        "integration_category": "threat_intelligence",
        "integration_types": ["threat_intel"],
        # ATT&CK is a free public dataset - there are no credentials at all.
        # The optional extra fields only tune which domains are enabled, pin a
        # specific release, or point at a self-hosted mirror.
        "auth_type": "none",
        "supported_auth_types": ["none"],
        # Every extra field is a tuning knob with a working default — the
        # integration is fully functional with all of them blank, so it is
        # marked "connected" at creation time.
        "extra_fields_optional": True,
        "fields": [],
        "extra_fields": {
            "domains": (
                "ATT&CK domains to enable, comma-separated: enterprise-attack, "
                "mobile-attack, ics-attack (default: all three)"
            ),
            "version": ("Pin a specific ATT&CK release, e.g. 19.1 (default: latest - refreshed every 24h)"),
            "source_url": (
                "Custom STIX data mirror base URL for air-gapped deployments "
                "(default: the official mitre-attack/attack-stix-data GitHub "
                "repository)"
            ),
        },
        "setup_guide": {
            "title": "MITRE ATT&CK Setup",
            "url": "https://attack.mitre.org/resources/attack-data-and-tools/",
            "steps": [
                "No API key or account is needed - the ATT&CK knowledge base is free and public.",
                "Optionally restrict the enabled domains (default: enterprise-attack, mobile-attack, ics-attack).",
                "Optionally pin a specific ATT&CK release (e.g. 19.1) for reproducible results; the default follows the latest release.",
                "Optionally point 'source_url' at a mirror of the mitre-attack/attack-stix-data repository for air-gapped deployments (it must serve the same {domain}/{domain}.json layout plus index.json).",
                "Click 'Test Connection' to verify the data source is reachable.",
                "Bind the integration to an agent (agent > Integrations) to give it the ATT&CK search, lookup, and relationship tools - this integration provides live agent tools, it is not a knowledge base import source.",
            ],
            "permissions": [
                {
                    "scope": "Public ATT&CK dataset (read-only)",
                    "description": (
                        "Download and query the official ATT&CK STIX 2.1 "
                        "bundles over HTTPS - the dataset is read-only, so "
                        "every tool is a read. No platform knowledge base is "
                        "created; agents query the data live"
                    ),
                },
            ],
            "notes": (
                "Data comes from MITRE's official attack-stix-data GitHub "
                "repository (the same dataset served by the official TAXII 2.1 "
                "server at attack-taxii.mitre.org). The first query per domain "
                "downloads and indexes the STIX bundle (the Enterprise bundle "
                "is ~50 MB, so expect a one-off delay of up to ~30s), then "
                "results are served from an in-memory cache refreshed every "
                "24 hours. MITRE ATT&CK and ATT&CK are registered trademarks "
                "of The MITRE Corporation."
            ),
        },
    },
    "recorded_future": {
        "name": "Recorded Future",
        "description": (
            "Recorded Future Intelligence Cloud - enrich IPs, domains, URLs, "
            "hashes, CVEs, malware and companies (risk scores, evidence, and "
            "Intelligence Cards); batch SOAR enrichment and verdict triage; "
            "classic and Playbook Alerts (with category evidence panels) and "
            "case management; analyst notes (read + publish) and YARA/Sigma/"
            "Snort detection rules with deployable source; the Links "
            "relationship graph; threat actors, actor and malware threat "
            "maps, and risk history; Malware Intelligence sandbox queries; "
            "IOC list management; Collective Insights (submit + search); and "
            "Identity Intelligence (domain sweeps, identity lookups, password "
            "exposure checks)"
        ),
        "logo": "/logos/recorded_future_mark.png",
        "category": "integration",
        "integration_category": "threat_intelligence",
        "integration_types": ["threat_intel"],
        # Recorded Future authenticates with a single API token sent in the
        # X-RFToken header - there is no OAuth, so the only supported auth mode
        # is "api_key".
        "auth_type": "api_key",
        "supported_auth_types": ["api_key"],
        "fields": ["api_key"],
        "extra_fields": {
            "base_url": ("Recorded Future API base URL (default https://api.recordedfuture.com)"),
        },
        "setup_guide": {
            "title": "Recorded Future API Token Setup",
            "url": "https://docs.recordedfuture.com/reference/get-started",
            "steps": [
                "Sign in to the Recorded Future portal at app.recordedfuture.com as a user with API access.",
                "Open User Settings > API Access (or ask your Recorded Future administrator) and provision an API token for the Connect API. The token's capabilities follow your subscription (enrichment, alerts, Playbook Alerts, cases, analyst notes, detection rules, threat maps, risk history, Malware Intelligence, lists, Collective Insights, Identity Intelligence).",
                "Copy the API token (shown only once) and paste it into the 'API Key' field below.",
                "Leave 'base_url' as https://api.recordedfuture.com unless you use a dedicated or sovereign Recorded Future tenant.",
                "Click 'Test Connection' to verify the token (calls GET {base_url}/v2/ip/riskrules).",
            ],
            "permissions": [
                {
                    "scope": "Enrichment & Search (Connect + SOAR)",
                    "description": "Look up and search IPs, domains, URLs, hashes, CVEs, malware, and companies; batch-enrich and triage indicators",
                },
                {
                    "scope": "Alerts, Playbook Alerts & Cases",
                    "description": "List, read, and update classic alerts (with case management) and Playbook Alerts including category evidence panels",
                },
                {
                    "scope": "Intelligence",
                    "description": "Read and publish analyst notes; read YARA/Sigma/Snort detection rules (with source), the Links graph, threat actors, actor/malware threat maps, and risk history",
                },
                {
                    "scope": "Lists & Collective Insights",
                    "description": "Manage IOC lists/watchlists, submit detections to Collective Insights, and search the detection history",
                },
                {
                    "scope": "Identity Intelligence (optional)",
                    "description": "Search compromised credentials for your domains, look up specific identities, and check password-hash exposure (requires the Identity Intelligence module)",
                },
                {
                    "scope": "Malware Intelligence (optional)",
                    "description": "Natural-language queries over Recorded Future's sandbox corpus (requires the Malware Intelligence module)",
                },
            ],
            "notes": (
                "Recorded Future authenticates with a single API token sent in the "
                "X-RFToken header (no OAuth). All tools share one host "
                "(https://api.recordedfuture.com); set 'base_url' only for a "
                "dedicated or sovereign tenant. The token's capabilities are bound "
                "by your Recorded Future subscription and the modules it was "
                "provisioned for - tools for modules you are not licensed for "
                "return a clear permission error. The token is stored encrypted here."
            ),
        },
    },
    "ovhcloud": {
        "name": "OVHcloud",
        "description": (
            "OVHcloud infrastructure inventory and exposure: bare metal servers, Public Cloud "
            "instances, volumes, networks, Managed Kubernetes and Databases, VPS, Hosted Private "
            "Cloud VMs, IP blocks with firewall and DDoS mitigation state, IP Load Balancing, "
            "vRack and DNS zones, a normalized compute inventory, and reboot / start / stop actions"
        ),
        "logo": "/logos/ovhcloud.svg",
        "category": "integration",
        "integration_category": "exposure_management",
        "integration_types": ["cloud_security", "cmdb"],
        # Two OVHcloud credentials: an application key signed per request
        # (application key + secret + consumer key) or an OAuth2 service account
        # (client id + secret). Either secret is stored as the API key; the
        # rest lives in extra config.
        "auth_type": "api_key",
        "supported_auth_types": ["api_key"],
        "fields": ["api_key"],
        "extra_fields": {
            "endpoint": "API endpoint",
            "auth_mode": "Authentication mode",
            "application_key": "Application key (application key mode)",
            "consumer_key": "Consumer key (application key mode)",
            "client_id": "Service account client ID (OAuth2 mode)",
        },
        "extra_field_options": {
            "endpoint": [
                {"value": "ovh-eu", "label": "OVHcloud Europe - eu.api.ovh.com"},
                {"value": "ovh-ca", "label": "OVHcloud Canada - ca.api.ovh.com"},
                {"value": "ovh-us", "label": "OVHcloud US - api.us.ovhcloud.com"},
            ],
            "auth_mode": [
                {"value": "application_key", "label": "Application key + consumer key (signed requests)"},
                {"value": "oauth2", "label": "OAuth2 service account (client credentials)"},
            ],
        },
        "extra_field_required_when": {
            "application_key": {"auth_mode": ["", "application_key"]},
            "consumer_key": {"auth_mode": ["", "application_key"]},
            "client_id": {"auth_mode": ["oauth2"]},
        },
        "setup_guide": {
            "title": "OVHcloud API credentials",
            "url": "https://help.ovhcloud.com/csm/en-gb-api-getting-started-ovhcloud-api",
            "steps": [
                "Pick the API endpoint of the account: Europe (eu.api.ovh.com), Canada (ca.api.ovh.com) or US (api.us.ovhcloud.com).",
                "Application key mode: create an application and a consumer key at the endpoint's /createToken page with GET rights on /* for reads (add POST on the reboot, start and stop paths only if those actions are wanted). Paste the application secret into the 'API Key' field, the application key and the consumer key into their fields.",
                "OAuth2 mode: create a service account (POST /me/api/oauth2/client, flow CLIENT_CREDENTIALS) and give it an IAM policy with read actions on the resources. Paste the client secret into the 'API Key' field and the client ID into its field.",
                "Click 'Test Connection': it reads the account and, in application key mode, the access rules the consumer key grants.",
            ],
            "permissions": [
                {"scope": "GET /*", "description": "Read every resource the tools list (servers, projects, VPS, Hosted Private Cloud, IPs, DNS)"},
                {"scope": "POST /dedicated/server/*/reboot, /vps/*/reboot, /cloud/project/*/instance/*/reboot|start|stop", "description": "Optional: the lifecycle actions"},
            ],
        },
    },
    "threatconnect": {
        "name": "ThreatConnect",
        "description": (
            "Threat intelligence platform (TIP) — indicators (IOCs) with "
            "Threat/Confidence ratings, intel groups (incidents, adversaries, "
            "campaigns, threats, reports), Workflow cases with notes/artifacts/"
            "tasks, tags (incl. MITRE ATT&CK), security labels, victims, "
            "associations, TQL power-search, and playbook automation"
        ),
        "logo": "/logos/threatconnect.svg",
        "category": "integration",
        "integration_category": "threat_intelligence",
        "integration_types": ["threat_intel", "soar"],
        # ThreatConnect authenticates by signing every request with an Access
        # ID + Secret Key (HMAC-SHA256) — there is no OAuth, so the only
        # supported auth mode is "api_key".
        "auth_type": "api_key",
        "supported_auth_types": ["api_key"],
        "fields": ["api_key"],
        "extra_fields": {
            "base_url": (
                "ThreatConnect instance URL (Public Cloud "
                "https://app.threatconnect.com, or your Dedicated Cloud / "
                "On-Premises host, e.g. https://companyabc.threatconnect.com)"
            ),
            "access_id": ("API user Access ID (the identifier paired with the Secret Key)"),
        },
        "setup_guide": {
            "title": "ThreatConnect API User Setup",
            "url": "https://docs.threatconnect.com/en/latest/rest_api/quick_start.html",
            "steps": [
                "Log in to ThreatConnect with an Organization Administrator account.",
                "From the Settings menu on the top navigation bar, select Org Settings.",
                "On the Membership tab, click 'Create API User' and fill out the required fields.",
                "Copy the generated Access ID and paste it into the 'access_id' field below.",
                "Copy the Secret Key (shown only once) and paste it into the 'API Key' field below.",
                "Set 'base_url' to your ThreatConnect instance URL (https://app.threatconnect.com for Public Cloud, or your dedicated host).",
                "Click 'Test Connection' to verify the credentials (calls GET {base_url}/api/v3/security/owners).",
            ],
            "permissions": [
                {
                    "scope": "Organization role: Standard User (read/write)",
                    "description": (
                        "Read and write threat intelligence data — indicators, "
                        "groups, tags, attributes, security labels, victims, "
                        "and associations"
                    ),
                },
                {
                    "scope": "Organization role: Organization Administrator",
                    "description": (
                        "Required to add, edit, and delete Workflow Case "
                        "Management data (cases, notes, artifacts, tasks)"
                    ),
                },
                {
                    "scope": "Community/Source roles",
                    "description": (
                        "Per-owner roles control which Communities and Sources "
                        "the API user can read from or contribute to"
                    ),
                },
                {
                    "scope": "Playbooks (optional)",
                    "description": (
                        "Playbook inventory, activation, and execution history "
                        "require Playbook permissions in the Organization"
                    ),
                },
            ],
            "notes": (
                "ThreatConnect signs every API request with HMAC-SHA256 using the "
                "Access ID and Secret Key (no OAuth, no bearer token) — the "
                "request timestamp must be within 5 minutes of the server clock. "
                "The Secret Key is stored encrypted here. The API lives at "
                "{base_url}/api; both Public Cloud and Dedicated Cloud / "
                "On-Premises instances are supported. Case Management write "
                "operations require an Organization Administrator API user."
            ),
        },
    },
    "crowdsec": {
        "name": "CrowdSec",
        "description": (
            "Collaborative security / CTI platform (CrowdSec) - crowd-sourced "
            "IP reputation from 100k+ Security Engine deployments: single and "
            "batch IP lookups with behaviors, MITRE techniques, CVEs and "
            "threat scores, Lucene hunting over the smoke database, the "
            "community blocklist (fire), plus full console automation via the "
            "Service API: fleet-wide decisions (ban/captcha/throttle), "
            "private blocklist curation with auto-refreshing CTI queries, "
            "centralized allowlists, the CVE Live Exploit Tracker (exploited "
            "CVEs, exploiting IPs, phase-change feed, protect rules), "
            "firewall integration endpoints, and remediation metrics"
        ),
        "logo": "/logos/crowdsec.svg",
        "category": "integration",
        "integration_category": "threat_intelligence",
        "integration_types": ["threat_intel"],
        # CrowdSec authenticates with API keys sent in the x-api-key header
        # (one key per API) - there is no OAuth, so the only supported auth
        # mode is "api_key".
        "auth_type": "api_key",
        "supported_auth_types": ["api_key"],
        "fields": ["api_key"],
        "extra_fields": {
            "service_api_key": (
                "Service API key (optional, premium) - created in the CrowdSec "
                "Console under Settings > Service API Keys; unlocks console "
                "tools (decisions, blocklists, allowlists, exploit tracker)"
            ),
        },
        "setup_guide": {
            "title": "CrowdSec API Keys Setup",
            "url": "https://docs.crowdsec.net/u/cti_api/getting_started/",
            "steps": [
                "Sign in to the CrowdSec Console at app.crowdsec.net (create a free account if needed).",
                "Go to Settings > CTI API Keys and click 'Create API Key' (the free community tier works).",
                "Copy the CTI API key and paste it into the 'API Key' field below.",
                "Optional (premium): go to Settings > Service API Keys, create a key with the permissions you want to grant (blocklists, allowlists, decisions, integrations), and paste it into the 'service_api_key' field to unlock the console tools.",
                "Click 'Test Connection' to verify the key(s) (calls GET https://cti.api.crowdsec.net/v2/smoke/8.8.8.8 and, when a Service API key is set, GET https://admin.api.crowdsec.net/v1/info).",
            ],
            "permissions": [
                {
                    "scope": "CTI API (IP reputation)",
                    "description": "Single IP lookups and Lucene smoke-database search on every plan; batch lookups and the community blocklist (fire) require a premium CTI plan",
                },
                {
                    "scope": "Service API (console, optional)",
                    "description": "Decisions, blocklists, allowlists, integration endpoints, the CVE Live Exploit Tracker, and remediation metrics - per-key permissions are set when creating the key",
                },
            ],
            "notes": (
                "CrowdSec uses two independent API keys, both sent in the "
                "x-api-key header: a CTI API key for IP reputation "
                "(cti.api.crowdsec.net, free tier available with daily query "
                "quotas) and an optional Service API key for console "
                "management (admin.api.crowdsec.net, premium). Either key can "
                "be configured alone - tools needing the missing key return a "
                "clear configuration error. Community CTI keys are limited to "
                "10 results per search page and get 403 on the premium batch/"
                "fire endpoints. Both keys are stored encrypted here."
            ),
        },
    },
    "cyware": {
        "name": "Cyware",
        "description": (
            "Threat intelligence platform (Cyware Intel Exchange / CTIX) — "
            "CQL threat data search across every STIX object (the primary "
            "IOC lookup and hunting surface), object detail + relations, "
            "context reads (tags, feed sources, collections, subscribers, "
            "saved searches), intel creation (quick-add IOCs/SDOs, "
            "free-text IOC parsing, tags), IOC curation bulk actions "
            "(false-positive, deprecate, allowed-list, watchlist, analyst "
            "score/TLP, rules), knowledge reads (threat bulletins, notes, "
            "tasks, rules, enrichment verdicts, ATT&CK navigator), admin "
            "reads, and object deletion"
        ),
        "logo": "/logos/cyware.svg",
        "category": "integration",
        "integration_category": "threat_intelligence",
        "integration_types": ["threat_intel"],
        # CTIX Open API authenticates by signing every request with an
        # Access ID + Secret Key (HMAC-SHA1 query params) — there is no
        # OAuth, so the only supported auth mode is "api_key".
        "auth_type": "api_key",
        "supported_auth_types": ["api_key"],
        "fields": ["api_key"],
        "extra_fields": {
            "base_url": (
                "Open API endpoint URL from CTIX Integrators (usually ends "
                "with /ctixapi, e.g. https://yourtenant.cyware.com/ctixapi)"
            ),
            "access_id": ("Open API Access ID (the identifier paired with the Secret Key)"),
        },
        "setup_guide": {
            "title": "Cyware Intel Exchange Open API Setup",
            "url": "https://techdocs.cyware.com/ctix/en/configure-open-api.html",
            "steps": [
                "Sign in to Intel Exchange as an administrator.",
                "Go to Administration > Integration Management and select 'CTIX Integrators' under THIRD PARTY DEVELOPERS.",
                "Click 'Add New', enter a name and description, and pick an expiry (or 'Never Expire').",
                "Optionally assign an Open API role to scope what the credentials can access, and restrict allowed IPs.",
                "Click 'Download' or 'Copy' to capture the endpoint URL, Access ID, and Secret Key (they cannot be recovered later).",
                "Paste the endpoint URL into 'base_url' and the Access ID into 'access_id' below.",
                "Paste the Secret Key into the 'API Key' field below.",
                "Click 'Test Connection' to verify the credentials (calls GET {base_url}/ping/).",
            ],
            "permissions": [
                {
                    "scope": "Open API role: read components",
                    "description": (
                        "Read threat data, tags, feed sources, collections, "
                        "subscribers, saved searches, threat bulletins, "
                        "notes, tasks, rules, enrichment verdicts, ATT&CK "
                        "navigator, and admin context — required by every "
                        "search/get/list/manage tool"
                    ),
                },
                {
                    "scope": "Open API role: create/update components",
                    "description": (
                        "Create intel (quick-add IOCs/SDOs), parse free-text "
                        "IOCs, create tags, and run IOC curation bulk "
                        "actions (tagging, false-positive, deprecate, "
                        "allowed-list, watchlist, analyst score/TLP, rules)"
                    ),
                },
                {
                    "scope": "Open API role: delete components (optional)",
                    "description": (
                        "Delete notes, tasks, saved searches, allowed "
                        "indicators, watchlist keywords, reports, and "
                        "dashboards via delete_cyware_object — omit it to "
                        "keep the credentials non-destructive"
                    ),
                },
            ],
            "notes": (
                "CTIX Open API signs every request with HMAC-SHA1 query "
                "parameters (AccessID + Expires + Signature) derived from "
                "the Access ID and Secret Key — no OAuth, no bearer token; "
                "the signature is valid for at most 30 seconds, so the "
                "server clock matters. The Secret Key is stored encrypted "
                "here. Access is scoped by the Open API role attached to "
                "the credentials, and requests can be IP-restricted. Rate "
                "limits are tenant-configurable in Administration > Rate "
                "Limit. This integration targets Intel Exchange (CTIX); "
                "Cyware Collaborate (CSAP) uses separate credentials and is "
                "not covered."
            ),
        },
    },
    "greynoise": {
        "name": "GreyNoise",
        "description": (
            "Internet scanner + business service intelligence (GreyNoise) - "
            "separate background internet noise from targeted threats: combined "
            "IP context lookups (internet scanner intelligence + business "
            "service / RIOT trust levels), batch enrichment of up to 1000 IPs, "
            "GNQL search and aggregate stats, IP behavior timelines, CVE "
            "exploitation intelligence, the tag catalog, and enterprise "
            "GNQL-driven blocklists"
        ),
        "logo": "/logos/greynoise.svg",
        "category": "integration",
        "integration_category": "threat_intelligence",
        "integration_types": ["threat_intel"],
        # GreyNoise authenticates with a single API key sent in the "key"
        # header - there is no OAuth, so the only supported auth mode is
        # "api_key".
        "auth_type": "api_key",
        "supported_auth_types": ["api_key"],
        "fields": ["api_key"],
        "setup_guide": {
            "title": "GreyNoise API Key Setup",
            "url": "https://docs.greynoise.io/docs/getting-started",
            "steps": [
                "Sign in to the GreyNoise Visualizer at viz.greynoise.io (create an account at greynoise.io if needed).",
                "Open your Account page and locate the API Key section.",
                "Copy the API key and paste it into the 'API Key' field below.",
                "Click 'Test Connection' to verify the key (calls GET https://api.greynoise.io/ping).",
                "Free community keys work but are rate-limited to a small number of lookups per day and return reduced detail; enterprise plans unlock full IP context, GNQL, timelines, CVE intelligence, and blocklists.",
            ],
            "permissions": [
                {
                    "scope": "IP Intelligence",
                    "description": "Full and quick IP context lookups (internet scanner + business service intelligence) and batch multi-IP enrichment",
                },
                {
                    "scope": "GNQL Search",
                    "description": "Query the noise dataset with the GreyNoise Query Language and get aggregate campaign statistics",
                },
                {
                    "scope": "Timelines & Tags",
                    "description": "IP behavior timelines over up to 90 days and the tag metadata catalog",
                },
                {
                    "scope": "CVE Intelligence",
                    "description": "Per-CVE exploitation detail and the tracked-CVE catalog (bulk list requires an entitlement)",
                },
                {
                    "scope": "Blocklists (optional)",
                    "description": "Manage GNQL-driven auto-refreshing blocklists (requires the Blocklists entitlement and a workspace)",
                },
            ],
            "notes": (
                "GreyNoise authenticates with a single API key sent in the 'key' "
                "header (no OAuth). All tools target https://api.greynoise.io "
                "using the current v3 API. The key's capabilities follow your "
                "GreyNoise subscription - endpoints outside your plan return a "
                "clear permission error, and partial (HTTP 206) responses are "
                "flagged in tool output. The key is stored encrypted here."
            ),
        },
    },
    "anyrun": {
        "name": "ANY.RUN",
        "description": (
            "Interactive malware sandbox + threat intelligence (ANY.RUN) - "
            "detonate files and URLs in the interactive cloud sandbox, poll "
            "analysis status, read the verdict / IOCs / MITRE ATT&CK mapping, "
            "store an analysis artefact as a file (HTML / STIX / MISP / IOC "
            "reports, PCAP, SSL keys, video, screenshots, and the sample or "
            "process dumps only as ANY.RUN's password-protected ZIP), browse "
            "analysis history, look up indicators in Threat Intelligence "
            "Lookup, and run YARA searches over the public sample database"
        ),
        "logo": "/logos/anyrun.svg",
        "category": "integration",
        "integration_category": "threat_intelligence",
        "integration_types": ["threat_intel"],
        # ANY.RUN authenticates with a single API key sent in the
        # Authorization header (API-KEY prefix) - there is no OAuth, so the
        # only supported auth mode is "api_key".
        "auth_type": "api_key",
        "supported_auth_types": ["api_key"],
        "fields": ["api_key"],
        "setup_guide": {
            "title": "ANY.RUN API Key Setup",
            "url": "https://any.run/api-documentation/",
            "steps": [
                "Sign in to ANY.RUN at app.any.run (a paid Hunter/Enterprise plan is required for API access; TI Lookup is a separate subscription).",
                "Open your profile and go to the 'API and Limits' tab.",
                "Generate an API key and copy it.",
                "Paste the API key into the 'API Key' field below (paste the raw key - the 'API-KEY' prefix is added automatically).",
                "Click 'Test Connection' to verify the key (calls GET https://api.any.run/v1/user).",
            ],
            "permissions": [
                {
                    "scope": "Interactive Sandbox",
                    "description": "Submit files and URLs for detonation, poll status, read reports/IOCs, download analysis artefacts (reports, PCAP, video, screenshots, samples), and manage tasks (add time, stop, delete)",
                },
                {
                    "scope": "Threat Intelligence Lookup",
                    "description": "Search IOCs/IOAs/events across the public sample database (requires a TI Lookup subscription)",
                },
                {
                    "scope": "YARA Search",
                    "description": "Search the public sample database by YARA rule content (requires a TI subscription)",
                },
            ],
            "notes": (
                "ANY.RUN authenticates with a single API key sent in the "
                "Authorization header (no OAuth). All tools target "
                "https://api.any.run using the current v1 API. Sandbox tools "
                "need a paid plan with API access; Threat Intelligence Lookup "
                "and YARA search require the separate TI subscription and return "
                "a clear permission error otherwise. Submitting a detonation "
                "consumes an API quota unit and runs a live analysis. A "
                "downloaded artefact becomes a file of the conversation; the "
                "sample and process dumps are live malware, stored only as "
                "ANY.RUN's ZIP encrypted with the password 'infected' and never "
                "read by the platform. The key is stored encrypted here."
            ),
        },
    },
    "abuseipdb": {
        "name": "AbuseIPDB",
        "description": (
            "Crowdsourced IP threat intelligence - check IP and CIDR-block abuse "
            "reputation (confidence score, geo/ISP, Tor flag, report history), "
            "pull the blacklist of the most-reported IPs, report abusive IPs "
            "(single or CSV bulk), and clear your own submitted reports"
        ),
        "logo": "/logos/abuseipdb.svg",
        "category": "integration",
        "integration_category": "threat_intelligence",
        "integration_types": ["threat_intel"],
        # AbuseIPDB authenticates with a single API key sent in the `Key`
        # header - there is no OAuth, so the only supported auth mode is
        # "api_key".
        "auth_type": "api_key",
        "supported_auth_types": ["api_key"],
        "fields": ["api_key"],
        "setup_guide": {
            "title": "AbuseIPDB API Key Setup",
            "url": "https://www.abuseipdb.com/account/api",
            "steps": [
                "Create a free account at abuseipdb.com (or sign in).",
                "Open your account dashboard and go to the API tab (abuseipdb.com/account/api).",
                "Click 'Create Key' to generate an APIv2 key.",
                "Copy the key and paste it into the 'API Key' field below.",
                "Click 'Test Connection' to verify the key (calls GET /api/v2/check for 8.8.8.8).",
            ],
            "permissions": [
                {
                    "scope": "Check & Reports (read)",
                    "description": "Check IP/CIDR abuse reputation and page through individual abuse reports",
                },
                {
                    "scope": "Blacklist (read)",
                    "description": "Download the blacklist of the most-reported IP addresses",
                },
                {
                    "scope": "Report & Bulk Report (write)",
                    "description": "Report abusive IPs to the community (public reports)",
                },
                {
                    "scope": "Clear Address (destructive)",
                    "description": "Permanently delete reports previously submitted by this account for an IP",
                },
            ],
            "notes": (
                "AbuseIPDB authenticates with a single API key sent in the Key "
                "header (no OAuth). Rate limits are per-endpoint DAILY quotas "
                "that follow your plan: on the free tier check = 1,000/day, "
                "reports = 100/day, report = 1,000/day, check-block = 100/day, "
                "and blacklist / bulk-report / clear-address = 5/day. Webmaster "
                "verification and paid plans raise the limits (up to 50,000 "
                "checks/day on Premium). Blacklist confidence filtering below "
                "100 and country filters are subscriber features. The key is "
                "stored encrypted here."
            ),
        },
    },
    "urlscan": {
        "name": "urlscan.io",
        "description": (
            "URL and website scanning sandbox (urlscan.io) - search the "
            "historical scan corpus with Elasticsearch syntax (by domain, IP, "
            "hash, ASN, tags), submit URLs for sandboxed scanning with "
            "public/unlisted/private visibility, retrieve verdicts, page "
            "details, detected technologies, screenshots and DOM snapshots, "
            "find structurally similar scans (urlscan Pro), and check "
            "per-action quotas"
        ),
        "logo": "/logos/urlscan.svg",
        "category": "integration",
        "integration_category": "threat_intelligence",
        "integration_types": ["threat_intel"],
        # urlscan.io authenticates with a single API key sent in the
        # "API-Key" header - there is no OAuth, so the only supported auth
        # mode is "api_key".
        "auth_type": "api_key",
        "supported_auth_types": ["api_key"],
        "fields": ["api_key"],
        "setup_guide": {
            "title": "urlscan.io API Key Setup",
            "url": "https://urlscan.io/docs/api/",
            "steps": [
                "Create a free account at urlscan.io (or sign in).",
                "Open your user profile and go to Settings & API > API Keys.",
                "Create a new API key and copy it.",
                "Paste the key into the 'API Key' field below.",
                "Click 'Test Connection' to verify the key (calls GET https://urlscan.io/user/quotas/).",
                "Free accounts get per-minute/hour/day quotas for searches and public/unlisted/private scans; commercial subscriptions and urlscan Pro raise the quotas and unlock structure search.",
            ],
            "permissions": [
                {
                    "scope": "Search (read)",
                    "description": "Query historical scans by domain, IP, hash, ASN, tags, and dates (public scans by anyone + your unlisted/private scans)",
                },
                {
                    "scope": "Results & DOM (read)",
                    "description": "Retrieve scan verdicts, page details, technologies, screenshot URLs, and DOM snapshots",
                },
                {
                    "scope": "Scan submission (write)",
                    "description": "Submit URLs for sandboxed scanning (consumes scan quota; visibility defaults to unlisted)",
                },
                {
                    "scope": "Structure search (read, urlscan Pro)",
                    "description": "Find structurally similar scans to expand from one phishing page to the whole campaign",
                },
            ],
            "notes": (
                "urlscan.io authenticates with a single API key sent in the "
                "'API-Key' header (no OAuth). Rate limits are per-action "
                "fixed windows (minute/hour/day, reset at the full "
                "minute/hour/midnight UTC) that follow your subscription; "
                "429 responses are retried automatically. Scans take about "
                "10-30 seconds to finish. Visibility matters: public scans "
                "appear on the urlscan.io front page - the integration "
                "defaults to unlisted so URLs that may contain PII or "
                "non-public information are never exposed publicly. The key "
                "is stored encrypted here."
            ),
        },
    },
    "abusech": {
        "name": "abuse.ch",
        "description": (
            "Free community threat intelligence (abuse.ch) - instant IOC "
            "triage across the three flagship platforms: MalwareBazaar "
            "malware sample lookups and family/tag/certificate hunting, "
            "URLhaus malicious URL and host intelligence with payload "
            "history, and ThreatFox IOC search (C2 IPs, domains, URLs, "
            "hashes), recent IOC feeds, and community IOC sharing"
        ),
        "logo": "/logos/abusech.svg",
        "category": "integration",
        "integration_category": "threat_intelligence",
        "integration_types": ["threat_intel"],
        # abuse.ch authenticates with a single Auth-Key sent in the
        # `Auth-Key` header (shared across all abuse.ch platforms) - there
        # is no OAuth, so the only supported auth mode is "api_key".
        "auth_type": "api_key",
        "supported_auth_types": ["api_key"],
        "fields": ["api_key"],
        "setup_guide": {
            "title": "abuse.ch Auth-Key Setup",
            "url": "https://auth.abuse.ch/",
            "steps": [
                "Go to auth.abuse.ch and sign in (free - use Google, GitHub, X, or a Spamhaus account).",
                "Complete your profile if prompted (abuse.ch asks for basic profile info before issuing keys).",
                "On the auth.abuse.ch dashboard, locate the 'Auth-Key' section and copy your personal Auth-Key.",
                "Paste the key into the 'API Key' field below - the same key works for MalwareBazaar, URLhaus, and ThreatFox.",
                "Click 'Test Connection' to verify the key (calls GET urlhaus-api.abuse.ch/v1/urls/recent/limit/1/).",
            ],
            "permissions": [
                {
                    "scope": "MalwareBazaar (read + comment)",
                    "description": "Hash lookups, recent samples, tag/signature/filetype/certificate/YARA searches, and public sample comments",
                },
                {
                    "scope": "URLhaus (read)",
                    "description": "URL, host, and payload lookups plus recent malicious URL and payload feeds",
                },
                {
                    "scope": "ThreatFox (read + submit)",
                    "description": "IOC search by value/hash/tag/malware family, recent IOC feeds, reference data, and community IOC submission",
                },
            ],
            "notes": (
                "abuse.ch requires an Auth-Key on all its APIs (introduced "
                "2024-2025; anonymous access is no longer supported). One "
                "free key from auth.abuse.ch covers every abuse.ch platform "
                "and is sent in the Auth-Key HTTP header. The APIs are free "
                "under the abuse.ch Fair Use Policy - heavy commercial usage "
                "may require the paid enhanced API. ThreatFox expires IOCs "
                "older than 6 months. The key is stored encrypted here."
            ),
        },
    },
    "ransomware_live": {
        "name": "Ransomware.live",
        "description": (
            "Ransomware victim and group tracking (Ransomware.live) - recent "
            "claimed victims, victim search by group/sector/country/keyword/"
            "date, enriched per-victim detail (PRO), ransomware group profiles "
            "(leak sites, MITRE TTPs, exploited CVEs, tools), cyberattack "
            "press coverage, global victimology stats, and PRO group "
            "artifacts (IOCs, YARA rules, leaked negotiation chats, ransom "
            "notes, CSIRT contacts, SEC 8-K filings). Supports the "
            "free-forever PRO API key (full surface) and the unauthenticated "
            "v2 tier"
        ),
        "logo": "/logos/ransomware_live.svg",
        "category": "integration",
        "integration_category": "threat_intelligence",
        "integration_types": ["threat_intel"],
        # Ransomware.live has two public tiers: the authenticated PRO API
        # (X-API-KEY header) and the unauthenticated free v2 API. "api_key"
        # drives PRO (recommended - full surface); "none" drives the free v2
        # tier (victims/groups/press, ~1 req/min per endpoint). The API is
        # read-only either way.
        "auth_type": "api_key",
        "supported_auth_types": ["api_key", "none"],
        "fields": ["api_key"],
        "setup_guide": {
            "title": "Ransomware.live API Setup",
            "url": "https://www.ransomware.live/api",
            "steps": [
                "Recommended (PRO API - full surface): sign in at https://www.ransomware.live/my and copy your free-forever API key (500,000 requests/month).",
                "Keep the 'API Key' authentication mode selected and paste the key into the 'API Key' field below.",
                "Click 'Test Connection' to verify the key (calls GET /validate).",
                "Alternative (free v2 API - no key): switch the authentication mode to 'None'. This uses the unauthenticated API (https://api.ransomware.live/v2), rate limited to about 1 request per minute per endpoint, and covers recent victims, victim search, group profiles, and press coverage.",
                "PRO-only surfaces (enriched per-victim detail, server-side combined search, whole-year date filters, sector reference data, group IOCs, YARA rules, negotiation chats, ransom notes, CSIRT contacts, SEC 8-K filings) return actionable guidance on the free tier.",
            ],
            "permissions": [
                {
                    "scope": "Victims & Groups (read)",
                    "description": "List/search claimed victims and browse ransomware group profiles (both tiers)",
                },
                {
                    "scope": "Press & Stats (read)",
                    "description": "Cyberattack press coverage and global dataset statistics (both tiers)",
                },
                {
                    "scope": "Victim detail (read, PRO)",
                    "description": "Enriched per-victim detail by id, server-side combined search, and whole-year date filters",
                },
                {
                    "scope": "Group artifacts (read, PRO)",
                    "description": "Group IOCs, YARA rules, leaked negotiation chats, and ransom notes",
                },
                {
                    "scope": "Reference data (read, PRO)",
                    "description": "Victim sector list, national CSIRT/CERT contacts, and SEC Form 8-K cybersecurity filings",
                },
            ],
            "notes": (
                "Ransomware.live (by Julien Mousqueton) monitors ransomware "
                "groups' leak/extortion sites. The PRO API authenticates with a "
                "free-forever key sent in the X-API-KEY header (500,000 "
                "requests/month; HTTP 429 beyond quota) and exposes the full "
                "surface. The free v2 API needs no key but is rate limited to "
                "about 1 request/minute per endpoint and is intended for "
                "personal use. This integration is read-only - Ransomware.live "
                "has no mutation API. The key is stored encrypted here."
            ),
        },
    },
    "sekoia": {
        "name": "Sekoia.io",
        "description": (
            "Sekoia.io SOC platform - CTI Intelligence Center (indicator and "
            "observable lookup, IOC threat context, STIX object search, "
            "intelligence feeds, IOC collections) and XDR Operations Center "
            "(alert triage with workflow transitions and comments, cases, "
            "asynchronous event search with Dork queries, detection rules "
            "catalog, and asset inventory)"
        ),
        "logo": "/logos/sekoia.svg",
        "category": "integration",
        "integration_category": "siem_analytics",
        "integration_types": ["xdr", "siem", "threat_intel", "soar"],
        # Sekoia.io authenticates with a single API key sent as a Bearer token
        # - there is no OAuth, so the only supported auth mode is "api_key".
        "auth_type": "api_key",
        "supported_auth_types": ["api_key"],
        "fields": ["api_key"],
        "extra_fields": {
            "base_url": (
                "Sekoia.io API base URL (default https://api.sekoia.io; "
                "region-specific tenants use e.g. https://app.mco1.sekoia.io/api)"
            ),
        },
        "setup_guide": {
            "title": "Sekoia.io API Key Setup",
            "url": "https://docs.sekoia.io/getting_started/manage_api_keys/",
            "steps": [
                "Log in to Sekoia.io (app.sekoia.io or your regional host) as a user with admin rights.",
                "Go to Settings > Workspace > API Keys and click '+ API key'.",
                "Name the key, set an expiration, and select permissions: read permissions (alerts, cases, events/intakes, assets, rules, Intelligence Center) for visibility tools; add the matching write permissions (alert workflow/comments, IOC collections) only if the agent should act.",
                "Copy the generated key (it is shown only once) and paste it into the 'API Key' field above.",
                "Leave 'base_url' as https://api.sekoia.io unless your tenant is hosted in a specific region (e.g. https://app.mco1.sekoia.io/api - the app host followed by /api).",
                "Click 'Test Connection' to verify the key (calls GET {base_url}/v1/auth/validate).",
            ],
            "permissions": [
                {
                    "scope": "Operations Center - Alerts & Cases",
                    "description": "List/get alerts, change workflow status, read/post comments, and list cases",
                },
                {
                    "scope": "Operations Center - Events (SIC_READ_INTAKES)",
                    "description": "Create event search jobs and read their results for threat hunting",
                },
                {
                    "scope": "Operations Center - Rules & Assets",
                    "description": "Browse the detection rules catalog and the asset inventory",
                },
                {
                    "scope": "Intelligence Center (inthreat)",
                    "description": "Look up indicators/observables and their context, search STIX objects, consume feeds",
                },
                {
                    "scope": "Intelligence Center - Manage IOC (optional)",
                    "description": "Create IOC collections and add/delete indicators (write)",
                },
            ],
            "notes": (
                "Sekoia.io authenticates every request with a single API key sent "
                "as 'Authorization: Bearer <key>' (no OAuth). Keys are "
                "permission-based, not role-based - grant only what the agent "
                "needs; tools without the matching permission return a clear 403 "
                "error. Sekoia operates regional tenants: if you access the app "
                "via a regional host (e.g. https://app.mco1.sekoia.io), set "
                "'base_url' to that host followed by /api. Keys can expire "
                "(30/180/365 days or never) and are shown only once - they are "
                "stored encrypted here."
            ),
        },
    },
    "sublime": {
        "name": "Sublime Security",
        "description": (
            "Sublime Security email detection and response - triage the "
            "message-group queue (list/search/detail with flagged rules, "
            "attack score verdicts, user reports), review and remediate "
            "malicious mail (classify, trash, quarantine, restore, warning "
            "banners), hunt with MQL over any time range, manage detection "
            "rules (validate/create/update/activate) and threat-intel lists, "
            "and enrich alerts with raw EML, attack scores, and link analysis"
        ),
        "logo": "/logos/sublime.svg",
        "category": "integration",
        "integration_category": "detection_response",
        "integration_types": ["email_security", "email", "threat_intel"],
        # Sublime authenticates with a single API key sent as a Bearer token
        # - there is no OAuth, so the only supported auth mode is "api_key".
        "auth_type": "api_key",
        "supported_auth_types": ["api_key"],
        "fields": ["api_key"],
        "extra_fields": {
            "base_url": (
                "Sublime Platform base URL (default "
                "https://platform.sublime.security; regional tenants use e.g. "
                "https://eu.platform.sublime.security - see Automate > API on "
                "your dashboard)"
            ),
        },
        "setup_guide": {
            "title": "Sublime Security API Key Setup",
            "url": "https://docs.sublime.security/reference/authentication",
            "steps": [
                "Log in to your Sublime dashboard (e.g. platform.sublime.security or your regional/self-hosted host).",
                "Open Automate > API in the left-hand navigation.",
                "Note the Base URL shown on that page - paste it into 'base_url' below if it differs from https://platform.sublime.security.",
                "Click 'New Key', name the key (e.g. 'XTM One agent'), and click Save.",
                "Copy the generated key and paste it into the 'API Key' field above.",
                "Click 'Test Connection' to verify the key (calls GET {base_url}/v0/rules?limit=1).",
            ],
            "permissions": [
                {
                    "scope": "Message groups and messages (read)",
                    "description": "List/search/detail the triage queue, raw EML, attack scores, ASA verdicts",
                },
                {
                    "scope": "Hunt jobs (read/write)",
                    "description": "Start MQL hunt jobs and read their results",
                },
                {
                    "scope": "Rules and lists (read/write)",
                    "description": "Browse/validate/create/update detection rules and manage threat-intel lists",
                },
                {
                    "scope": "Review and actions (write)",
                    "description": "Classify message groups and apply trash/quarantine/restore/warning-banner actions",
                },
            ],
            "notes": (
                "Sublime authenticates every request with a single API key "
                "sent as 'Authorization: Bearer <key>' (no OAuth). The API "
                "key inherits the permissions of the user who created it - "
                "use a least-privilege account if the agent should only "
                "read. Base URLs are region-specific (NA-East "
                "platform.sublime.security, na-west./ca./uk./eu./au. "
                "prefixes, or your self-hosted host) - check Automate > API "
                "on your dashboard. Every API response carries an "
                "X-Request-ID header useful for Sublime support requests. "
                "The key is stored encrypted here."
            ),
        },
    },
    "whatsapp": {
        "name": "WhatsApp",
        "description": (
            "WhatsApp Business Platform (Cloud API) as one business phone number: "
            "send text, approved templates, media and interactive messages to a "
            "number or a group the business created, react, mark read, download "
            "received media, read the number's profile and its groups"
        ),
        "logo": "/logos/whatsapp.svg",
        "category": "integration",
        "integration_category": "communication",
        "integration_types": ["messaging"],
        #: The channel its messaging tools reach (a WhatsApp channel bot is the same platform).
        "messaging_channel": "whatsapp",
        #: The callback URL the Meta app posts its notifications to (shared with the
        #: whatsapp_bot channel bot; routed by phone_number_id). The
        #: integration page shows it with a copy button.
        "webhook_path": "/api/channels/whatsapp/webhook",
        # The Cloud API takes a system user access token; there is no OAuth.
        "auth_type": "api_key",
        "supported_auth_types": ["api_key"],
        "fields": ["api_key"],
        "extra_fields": {
            # Two backends: the official Cloud API (API Key = system user
            # token, phone_number_id) or a linked-device gateway (API Key = the
            # gateway's channel token, gateway_vendor, base_url).
            "backend": "Backend: Meta Cloud API (official, business number) or linked-device gateway (paired number: polls, existing groups)",
            "phone_number_id": "Meta Cloud API: phone number ID of the business number (WhatsApp Manager > API Setup), required on this backend",
            "waba_id": "Meta Cloud API: WhatsApp Business Account ID (optional, for template and number management)",
            "app_secret": "Meta Cloud API: Meta app secret, used to verify the signature of inbound webhook notifications (optional until inbound messages are enabled)",
            "verify_token": "Meta Cloud API: webhook verify token you choose and enter in the Meta app's WhatsApp > Configuration (optional until inbound messages are enabled)",
            "gateway_vendor": "Linked-device gateway: vendor (default whapi)",
            "base_url": "Linked-device gateway: API base URL (default https://gate.whapi.cloud)",
            "webhook_secret": "Linked-device gateway: a long random secret you choose; the gateway's webhook URL is {base_url}/api/channels/whatsapp/gateway/<integration id>?token=<this secret> (inbound messages, poll votes, group events)",
            "inbound_retention_days": "Days received messages are kept for list_whatsapp_conversations / read_whatsapp_messages (optional, 30 by default, 1 to 365)",
            "self_chat_agent_id": "Linked-device gateway: id of the agent the paired number's own 'message yourself' chat is routed to, as instructions of the integration's owner (needs an enabled WhatsApp agent account of that agent on this integration; empty: off, the default)",
        },
        "extra_field_options": {
            "backend": [
                {"value": "meta_cloud_api", "label": "Meta Cloud API (official)"},
                {"value": "linked_device_gateway", "label": "Linked-device gateway (polls, existing groups)"},
            ],
            "gateway_vendor": [
                {"value": "whapi", "label": "Whapi.Cloud"},
            ],
        },
        # The Cloud API (the default backend, also when ``backend`` is absent)
        # cannot build a credential without the phone number id (#9898); the
        # gateway's vendor and base URL have defaults.
        "extra_field_required_when": {
            "phone_number_id": {"backend": ["", "meta_cloud_api"]},
        },
        "setup_guide": {
            "title": "WhatsApp Cloud API Setup",
            "url": "https://developers.facebook.com/documentation/business-messaging/whatsapp/get-started",
            "steps": [
                "In Meta for Developers, create a Business app and add the WhatsApp product.",
                "Register a business phone number dedicated to the agent (a number already used in the consumer WhatsApp app cannot be operated by the API).",
                "In Meta Business Suite > Settings > System users, create a system user, assign it the app and the WhatsApp Business Account, and generate a permanent token with whatsapp_business_messaging and whatsapp_business_management.",
                "Paste the token into the 'API Key' field and the Phone number ID (WhatsApp Manager > API Setup) into 'phone_number_id'.",
                "Click 'Test Connection' (reads GET /<phone_number_id>).",
                "Inbound messages: copy the callback URL from the Webhooks card of this integration into the Meta app's WhatsApp > Configuration (Callback URL), enter the same value as this integration's 'verify_token' in Verify token, and set 'app_secret' so the notifications' signature is checked.",
                "Linked-device gateway instead (polls, existing groups): choose backend 'linked_device_gateway', create a Whapi.Cloud channel, paste its channel token into 'API Key', then pair a dedicated number by scanning the QR code (tool get_whatsapp_pairing_qr, or the Whapi.Cloud dashboard) from WhatsApp > Linked devices.",
                "Linked-device gateway inbound: set 'webhook_secret', then in the Whapi.Cloud channel settings add the webhook {base_url}/api/channels/whatsapp/gateway/<integration id>?token=<webhook_secret> with the events messages (POST, PATCH) and groups.",
            ],
            "permissions": [
                {
                    "scope": "whatsapp_business_messaging",
                    "description": "Send messages, reactions and read receipts; download media",
                },
                {
                    "scope": "whatsapp_business_management",
                    "description": "Read the phone number and its groups",
                },
            ],
            "notes": (
                "The API operates a business phone number, never a consumer "
                "WhatsApp account. Groups need an Official Business Account: the "
                "business creates the group and members join by an invite link "
                "(a business number cannot be added to an existing group), at "
                "most 8 participants. Interactive messages (buttons, lists) work "
                "1:1 only. WhatsApp polls can be neither created nor read through "
                "the API. Free-form messages need an open 24-hour window; outside "
                "it only approved templates go out. The token is stored encrypted."
            ),
        },
    },
    "proofpoint": {
        "name": "Proofpoint TAP",
        "description": (
            "Proofpoint Targeted Attack Protection (TAP) email security - "
            "triage SIEM threat events (blocked/permitted clicks, "
            "blocked/delivered messages with full threat details), pivot to "
            "campaigns, threat summaries, and sandbox forensic evidence, "
            "rank Very Attacked People and top clickers, and decode URL "
            "Defense rewritten links back to their original targets"
        ),
        "logo": "/logos/proofpoint.svg",
        "category": "integration",
        "integration_category": "detection_response",
        "integration_types": ["email_security", "email", "threat_intel"],
        # Proofpoint TAP authenticates every request with HTTP Basic auth
        # using a service principal (username) + secret (password) generated
        # in the TAP Dashboard - there is no OAuth, so the only supported
        # auth mode is "api_key" (the secret goes in the API Key field).
        "auth_type": "api_key",
        "supported_auth_types": ["api_key"],
        "fields": ["api_key"],
        "extra_fields": {
            "principal": (
                "TAP service principal (the UUID-style username generated "
                "with the secret in the TAP Dashboard - NOT your account "
                "email)"
            ),
        },
        "setup_guide": {
            "title": "Proofpoint TAP Service Credential Setup",
            "url": "https://help.proofpoint.com/Threat_Insight_Dashboard/API_Documentation",
            "steps": [
                "Sign in to the TAP Dashboard at threatinsight.proofpoint.com with an admin account.",
                "Go to Settings > Connected Applications.",
                "Click 'Create New Credential', name it (e.g. 'XTM One agent'), and click Generate.",
                "Copy the Service Principal and paste it into the 'principal' field below.",
                "Copy the Secret (shown only once) and paste it into the 'API Key' field below.",
                "Click 'Test Connection' to verify the credentials (runs a minimal SIEM query).",
            ],
            "permissions": [
                {
                    "scope": "SIEM events (read)",
                    "description": "Blocked/permitted clicks and blocked/delivered messages with threat details",
                },
                {
                    "scope": "Campaigns, threats & forensics (read)",
                    "description": "Campaign details, threat summaries, and sandbox forensic evidence (IOCs)",
                },
                {
                    "scope": "People (read)",
                    "description": "Very Attacked People rankings and top clickers",
                },
                {
                    "scope": "URL decoder (read)",
                    "description": "Decode URL Defense rewritten links to their original target URLs",
                },
            ],
            "notes": (
                "Proofpoint TAP authenticates with HTTP Basic auth: the "
                "service principal is the username and the secret is the "
                "password - both are generated together in the TAP Dashboard "
                "(Settings > Connected Applications) and grant read access "
                "to the whole TAP API surface (there are no per-scope "
                "permissions). Mind the API's rolling 24-hour quotas: SIEM "
                "and URL-decode calls allow 1800/24h, but campaign-ID "
                "listing, Very Attacked People, and top-clicker queries "
                "allow only 50/24h each. SIEM queries cover at most 1 hour "
                "per request and reach at most 7 days into the past. The "
                "secret is stored encrypted here."
            ),
        },
    },
    "threatq": {
        "name": "ThreatQuotient",
        "description": (
            "ThreatQuotient ThreatQ threat intelligence platform (self-hosted) - "
            "search and curate the Threat Library (indicators with status/score/"
            "expiration triage, events, adversaries, files, signatures, and "
            "STIX-style objects such as TTPs, malware, campaigns), enrich objects "
            "with attributes/comments/sources/tags, build relationships, "
            "download files and add any file of the conversation as one, and "
            "inspect data collections, scoring configuration, and export feeds "
            "(generated and stored as files)"
        ),
        "logo": "/logos/threatq.svg",
        "category": "integration",
        "integration_category": "threat_intelligence",
        "integration_types": ["threat_intel"],
        # ThreatQ authenticates with an OAuth2 password grant (client_id +
        # email + password exchanged for a short-lived bearer token) - the
        # password is the stored secret, so the supported auth mode is
        # "api_key" (the password goes in the API Key field).
        "auth_type": "api_key",
        "supported_auth_types": ["api_key"],
        "fields": ["api_key"],
        "extra_fields": {
            "base_url": ("ThreatQ appliance URL (e.g. https://threatq.example.com - no trailing slash, no /api)"),
            "client_id": ("OAuth client ID of the appliance (find it at https://<appliance>/assets/js/config.js)"),
            "email": "API user email address",
        },
        "setup_guide": {
            "title": "ThreatQ API Access Setup",
            "url": "https://helpcenter.threatq.com/Developer_Resources/ThreatQ_REST_API.htm",
            "steps": [
                "We recommend a dedicated API user. In the ThreatQ UI go to User Management, add a user, and assign a role bounding what the agent can do (a read-only role is enough for search/enrichment; contribution roles are needed to create and curate objects).",
                "Retrieve your appliance's OAuth client ID: open https://<your-appliance>/assets/js/config.js in a browser and copy the client_id value.",
                "Enter the appliance URL in 'base_url' (e.g. https://threatq.example.com - no trailing slash), the client ID in 'client_id', and the API user's email in 'email'.",
                "Enter the API user's password in the 'API Key' field below (it is stored encrypted and exchanged for short-lived bearer tokens).",
                "Click 'Test Connection' to verify the credentials (performs the OAuth2 password grant and a whoami call).",
            ],
            "permissions": [
                {
                    "scope": "Read (Threat Library access)",
                    "description": "Search and read indicators, events, adversaries, files (and download them), signatures, STIX objects, data collections, scoring configuration, and exports (and generate their output)",
                },
                {
                    "scope": "Contribute (object context & actions)",
                    "description": "Create/update objects, upload files, set indicator status/score/expiration, add attributes, comments, sources, tags, and relationships",
                },
                {
                    "scope": "Delete (maintenance)",
                    "description": "Delete objects, remove context, and manage watchlists via manage_threatq_object",
                },
            ],
            "notes": (
                "ThreatQ uses an OAuth2 password grant: the client ID, email, and "
                "password are exchanged at POST {base_url}/api/token for a "
                "short-lived (~1 h) bearer token, which this integration mints and "
                "caches automatically. There are no OAuth scopes - the API user's "
                "ThreatQ role bounds what the agent can do. The appliance must be "
                "reachable from this platform over HTTPS with a valid certificate. "
                "A downloaded file becomes a file of the conversation; a "
                "malware-locked file stays locked (its link only), and a file of "
                "a malware type is stored only when it is a password-protected "
                "ZIP, never read by the platform."
            ),
        },
    },
    "virustotal": {
        "name": "VirusTotal",
        "description": (
            "VirusTotal threat intelligence - enrich files, URLs, domains and "
            "IPs with multi-engine verdicts, relationships, and sandbox "
            "behaviour; universal and Intelligence corpus search; community "
            "comments and votes (read + write); URL and file submission with "
            "analysis polling; Livehunt YARA rulesets and notifications; "
            "Retrohunt jobs; IOC collections; and popular threat categories"
        ),
        "logo": "/logos/virustotal.svg",
        "category": "integration",
        "integration_category": "threat_intelligence",
        "integration_types": ["threat_intel"],
        # VirusTotal authenticates with a single API key sent in the x-apikey
        # header - there is no OAuth, so the only supported auth mode is
        # "api_key". The API host is fixed (https://www.virustotal.com/api/v3).
        "auth_type": "api_key",
        "supported_auth_types": ["api_key"],
        "fields": ["api_key"],
        "setup_guide": {
            "title": "VirusTotal API Key Setup",
            "url": "https://docs.virustotal.com/reference/overview",
            "steps": [
                "Sign in to VirusTotal at www.virustotal.com (create a free account if needed).",
                "Open your profile menu (top-right avatar) and select 'API key' to view your personal API key.",
                "Copy the API key and paste it into the 'API Key' field above.",
                "Click 'Test Connection' to verify the key (calls GET /popular_threat_categories).",
                "Optional: for Intelligence search, Livehunt, Retrohunt, and collection listing you need a premium (VT Enterprise) key - contact VirusTotal sales or your group admin.",
            ],
            "permissions": [
                {
                    "scope": "Reports & Relationships (all keys)",
                    "description": "File, URL, domain, and IP reports, relationships, sandbox behaviour, comments, and votes",
                },
                {
                    "scope": "Submissions (all keys)",
                    "description": "Submit URLs and upload files for analysis, request rescans, poll analyses",
                },
                {
                    "scope": "VT Intelligence (premium)",
                    "description": "Advanced corpus search with modifiers and collection listing",
                },
                {
                    "scope": "VT Hunting (premium)",
                    "description": "Livehunt YARA rulesets and notifications, Retrohunt jobs",
                },
            ],
            "notes": (
                "VirusTotal authenticates with a single API key sent in the "
                "x-apikey header (no OAuth). Public (free) API keys are rate "
                "limited to 4 requests/minute and 500 requests/day and must "
                "not be used for commercial products or services - premium "
                "(VT Enterprise) keys lift the limits and unlock Intelligence "
                "search, Livehunt, Retrohunt, and collection listing; tools "
                "for unlicensed features return a clear permission error. "
                "Files uploaded for scanning are shared with the VirusTotal "
                "community - never upload confidential documents. The key is "
                "stored encrypted here."
            ),
        },
    },
    "gti": {
        "name": "Google Threat Intelligence",
        "description": (
            "Google Threat Intelligence (Google / Mandiant / VirusTotal) - "
            "curated GTI assessments (verdict, threat score, severity) for "
            "files, URLs, domains and IPs plus batch IoC summaries; the "
            "Threat Landscape (threat actors, malware families, campaigns, "
            "toolkits, reports, vulnerabilities, country and industry "
            "profiles) with relationships, per-threat IoC search, MITRE "
            "ATT&CK trees and campaign timelines; hourly categorised threat "
            "lists; the unified IoC Stream; threat profiles with ML "
            "recommendations; Digital Threat Monitoring (deep/dark-web "
            "alerts with their attached files, monitors, document search); "
            "Attack Surface Management (entities, issues, technologies); "
            "Code Insight code analysis; and files stored in the "
            "conversation - report PDFs, IoC exports (JSON / CSV / STIX) and "
            "samples, only as Google TI's password-protected ZIP"
        ),
        "logo": "/logos/gti.svg",
        "category": "integration",
        "integration_category": "threat_intelligence",
        "integration_types": ["threat_intel"],
        # Google TI authenticates with a single API key sent in the x-apikey
        # header - there is no OAuth, so the only supported auth mode is
        # "api_key". The API host is fixed (https://www.virustotal.com/api/v3);
        # DTM and ASM live under /dtm and /asm on the same host with the same
        # key. The x-tool header is sent automatically so responses include
        # the gti_assessment attribute.
        "auth_type": "api_key",
        "supported_auth_types": ["api_key"],
        "fields": ["api_key"],
        "setup_guide": {
            "title": "Google Threat Intelligence API Key Setup",
            "url": "https://gtidocs.virustotal.com/reference/api-overview",
            "steps": [
                "Sign in to Google Threat Intelligence at www.virustotal.com with your Google TI licensed account.",
                "Open your profile menu (top-right avatar) and select 'API key' to view your personal API key.",
                "Copy the API key and paste it into the 'API Key' field above.",
                "Click 'Test Connection' to verify the key (calls GET /popular_threat_categories).",
                "Note: feature availability follows your Google TI license - Standard covers assessments and curated reports; Enterprise adds threat actors, campaigns and most threat lists; Enterprise Plus unlocks every threat list. DTM and ASM require the corresponding modules.",
            ],
            "permissions": [
                {
                    "scope": "IoC enrichment (all Google TI licenses)",
                    "description": "GTI assessments (verdict, threat score, severity) for files, URLs, domains, IPs, batch IoC summaries, and Code Insight (50 requests/day)",
                },
                {
                    "scope": "Threat Landscape (license-tiered)",
                    "description": "Curated threat actors, malware families, campaigns, toolkits, reports, vulnerabilities - threat actors and campaigns need Enterprise or Enterprise Plus",
                },
                {
                    "scope": "Threat Lists (license-tiered)",
                    "description": "Hourly categorised IoC packages - ransomware and malicious network infrastructure on all tiers; the rest on Enterprise / Enterprise Plus",
                },
                {
                    "scope": "IoC Stream & Threat Profiles",
                    "description": "Unified notification feed and personalised threat tracking with ML recommendations",
                },
                {
                    "scope": "DTM & ASM (module add-ons)",
                    "description": "Digital Threat Monitoring alerts (and the files attached to their analysis - list, download, upload)/monitors/document search and Attack Surface Management entities/issues/technologies",
                },
                {
                    "scope": "Files (license-tiered)",
                    "description": "Report PDFs and IoC exports (Enterprise / Enterprise Plus), and sample downloads as password-protected ZIPs (counted as downloads in the key's quota)",
                },
            ],
            "notes": (
                "Google Threat Intelligence authenticates with a single API "
                "key sent in the x-apikey header (no OAuth). Consumption "
                "quotas are license-based; requests beyond quota return 429 "
                "and are retried automatically. Tools for features outside "
                "the key's license tier return a clear permission error. "
                "A sample is live malware: it is fetched only as Google TI's "
                "ZIP encrypted with the password 'infected', stored as it came "
                "and never read by the platform. "
                "This integration covers the curated Google TI surface - for "
                "the community VirusTotal surface (scanning, comments, "
                "votes, Livehunt/Retrohunt) use the separate VirusTotal "
                "integration. The key is stored encrypted here."
            ),
        },
    },
    "anomali": {
        "name": "Anomali ThreatStream",
        "description": (
            "Anomali ThreatStream threat intelligence platform - search and "
            "enrich intelligence (observables) with structured filters or the "
            "advanced query language, passive DNS pivoting, import observables "
            "with or without approval and manage the import review queue, "
            "manage threat model entities (actors, campaigns, incidents, "
            "signatures, TTPs, vulnerabilities, threat bulletins, attack "
            "patterns) with tags/associations/comments, drive investigations, "
            "keyword rules and the org whitelist, and detonate URLs in the "
            "ThreatStream sandbox"
        ),
        "logo": "/logos/anomali.svg",
        "category": "integration",
        "integration_category": "threat_intelligence",
        "integration_types": ["threat_intel"],
        # ThreatStream authenticates with the ``Authorization: apikey
        # <username>:<api_key>`` header - there is no OAuth, so the only
        # supported auth mode is "api_key".
        "auth_type": "api_key",
        "supported_auth_types": ["api_key"],
        "fields": ["api_key"],
        "extra_fields": {
            "base_url": (
                "ThreatStream API base URL (default "
                "https://api.threatstream.com; set your own host for an "
                "on-prem / AirGap instance)"
            ),
            "username": "ThreatStream username (the account email paired with the API key)",
        },
        "setup_guide": {
            "title": "Anomali ThreatStream API Key Setup",
            "url": "https://ui.threatstream.com/static/apidoc",
            "steps": [
                "Sign in to ThreatStream (https://ui.threatstream.com or your own instance) as the user the agent should act as - a dedicated API service account is recommended.",
                "Open Settings > My Profile and locate the API key section; generate or copy the account's API key.",
                "Enter the account email in 'username', the API key in the 'API Key' field, and leave 'base_url' as https://api.threatstream.com unless you run an on-prem / AirGap instance.",
                "To let the agent import observables WITHOUT approval or approve import sessions, ensure the account has the 'Approve Intel' permission in ThreatStream.",
                "Click 'Test Connection' to verify the credentials.",
            ],
            "permissions": [
                {
                    "scope": "Standard user",
                    "description": "Search/enrich intelligence, browse threat model entities, investigations, rules, whitelist, import into the review queue, sandbox detonation",
                },
                {
                    "scope": "Approve Intel",
                    "description": "Import observables without approval and approve import sessions",
                },
                {
                    "scope": "Org admin (optional)",
                    "description": "List organization users to resolve investigation assignees",
                },
            ],
            "notes": (
                "ThreatStream authenticates every request with the "
                "'Authorization: apikey <username>:<api_key>' header (no "
                "OAuth). The API user's ThreatStream role bounds what the "
                "agent can do. The base URL is the API host "
                "(https://api.threatstream.com for SaaS), not the web UI host "
                "(https://ui.threatstream.com). The API key is stored "
                "encrypted here."
            ),
        },
    },
    "vulncheck": {
        "name": "VulnCheck",
        "description": (
            "Vulnerability and exploit intelligence (VulnCheck) - VulnCheck KEV "
            "(known exploited vulnerabilities), enriched NVD-2 CVE data, exploit "
            "intelligence (PoC/weaponized maturity, threat actor / ransomware / "
            "botnet exploitation), EPSS, the v4 advisory index, threat actor / "
            "ransomware / botnet / initial-access intelligence, IP and "
            "infrastructure intelligence (C2 / honeypot / attacker IP tags and "
            "Protective DNS lists), CPE and PURL product-to-CVE mapping, and "
            "Initial Access Intelligence Suricata/Snort detection rules"
        ),
        "logo": "/logos/vulncheck.svg",
        "category": "integration",
        "integration_category": "exposure_management",
        "integration_types": ["threat_intel"],
        # VulnCheck authenticates with a single API key sent as
        # "Authorization: Bearer <token>" - there is no OAuth, so the only
        # supported auth mode is "api_key".
        "auth_type": "api_key",
        "supported_auth_types": ["api_key"],
        "fields": ["api_key"],
        "setup_guide": {
            "title": "VulnCheck API Token Setup",
            "url": "https://docs.vulncheck.com/getting-started/api-tokens",
            "steps": [
                "Sign up for a VulnCheck account at vulncheck.com (community access is available for free).",
                "Log in to the VulnCheck Dashboard and open the API Sandbox to see the indexes your account is licensed for.",
                "Generate an API token for programmatic access and copy it.",
                "Paste the token into the 'API Key' field below.",
                "Click 'Test Connection' to verify the token (calls GET https://api.vulncheck.com/v3/index).",
                "Community tokens work but are rate-limited to 1,000 requests/minute and only expose community indexes (e.g. nist-nvd2); commercial plans unlock VulnCheck KEV, the enriched vulncheck-nvd2 index, exploit intelligence, IP intelligence, and Initial Access Intelligence.",
            ],
            "permissions": [
                {
                    "scope": "Vulnerability & exploit intelligence",
                    "description": "Enriched CVE lookup, VulnCheck KEV, exploit intelligence, EPSS, and the v4 advisory index",
                },
                {
                    "scope": "Threat intelligence",
                    "description": "Threat actors, ransomware groups, botnets, and initial-access intelligence (commercial indexes)",
                },
                {
                    "scope": "IP & infrastructure intelligence",
                    "description": "C2 / honeypot / initial-access IP intelligence, attacker IP tag denylists, and Protective DNS hostname lists",
                },
                {
                    "scope": "Product mapping & detection",
                    "description": "CPE and PURL product-to-CVE mapping, product identification, and Initial Access Intelligence Suricata/Snort rules",
                },
            ],
            "notes": (
                "VulnCheck authenticates with a single API token sent as "
                "'Authorization: Bearer <token>' (no OAuth). All tools target "
                "https://api.vulncheck.com (v3 index/search + v4 advisory). The "
                "token's capabilities follow your VulnCheck subscription - "
                "indexes outside your plan return a clear 402/403 permission "
                "error. The token is stored encrypted here."
            ),
        },
    },
    "vuln_intel": {
        "name": "Vulnerability Intelligence",
        "description": (
            "Aggregated vulnerability intelligence from the three "
            "authoritative free sources - NVD CVE 2.0 (CVE lookup, keyword/"
            "CPE/severity/CWE search, recent CVEs, CVSS v2/v3/v4, change "
            "history), the CISA Known Exploited Vulnerabilities catalog "
            "(exploited-in-the-wild verdicts, remediation due dates, "
            "ransomware campaign usage), and FIRST.org EPSS (exploitation "
            "probability scores, percentiles, batch scoring, top-N ranking). "
            "Includes a one-call CVE urgency assessment merging all three "
            "sources"
        ),
        "logo": "/logos/vuln_intel.svg",
        "category": "integration",
        "integration_category": "exposure_management",
        "integration_types": ["threat_intel"],
        # All three sources are free. The CISA KEV catalog and the FIRST EPSS
        # API are always unauthenticated; the NVD CVE API works without a key
        # but an optional free NVD API key raises its rate limit from 5 to 50
        # requests per rolling 30 seconds. "api_key" stores the NVD key;
        # "none" runs fully keyless. The tool set is identical either way and
        # everything is read-only.
        "auth_type": "none",
        "supported_auth_types": ["none", "api_key"],
        "fields": ["api_key"],
        "setup_guide": {
            "title": "Vulnerability Intelligence Setup",
            "url": "https://nvd.nist.gov/developers/request-an-api-key",
            "steps": [
                "No credentials are required - the CISA KEV catalog and the FIRST EPSS API are fully public, and the NVD CVE API works without a key at 5 requests per rolling 30 seconds. Keep the authentication mode on 'None' to start immediately.",
                "Recommended for heavier use: request a free NVD API key at https://nvd.nist.gov/developers/request-an-api-key (activated via a single-use email link).",
                "Switch the authentication mode to 'API Key' and paste the NVD key into the 'API Key' field - it raises the NVD rate limit to 50 requests per rolling 30 seconds. The key only affects the NVD; KEV and EPSS never need one.",
                "Click 'Test Connection' to verify (with a key: a 1-result NVD CVE page validating the apiKey header; keyless: a probe of the public EPSS API).",
            ],
            "permissions": [
                {
                    "scope": "NVD CVE API 2.0 (read)",
                    "description": "CVE lookup and search, CVSS v2/v3/v4 metrics, weaknesses, affected products, references, and change history",
                },
                {
                    "scope": "CISA KEV catalog (read)",
                    "description": "Known-exploited verdicts, recent additions, federal remediation due dates, and ransomware campaign usage",
                },
                {
                    "scope": "FIRST EPSS API (read)",
                    "description": "Exploitation probability scores and percentiles, batch scoring, historical scores, and top-N rankings",
                },
            ],
            "notes": (
                "The NVD rate limit (5 requests per rolling 30 seconds "
                "without a key, 50 with one) is enforced client-side with a "
                "shared token bucket so parallel agent tool calls can never "
                "breach it. The CISA KEV catalog is downloaded once and "
                "cached in-process for an hour. All three sources are "
                "read-only - no mutation endpoints exist. The optional NVD "
                "key is stored encrypted here. This product uses data from "
                "the NVD API but is not endorsed or certified by the NVD."
            ),
        },
    },
    "cybersixgill": {
        "name": "Cybersixgill",
        "description": (
            "Cybersixgill (a Bitsight company) deep and dark web threat "
            "intelligence - prioritise vulnerabilities with the Dynamic "
            "Vulnerability Exploit (DVE) score and remediation guidance, enrich "
            "IP/domain/URL/hash IOCs with underground context and pivot on the "
            "surfaced actors and source posts, search collected deep/dark web "
            "intel items (forum, marketplace, paste, and Telegram posts), "
            "triage per-organization actionable alerts (list, read, update, "
            "delete) with their matched underground content, read "
            "finished-intelligence analyst reports (the rendered report "
            "stored as an HTML file of the conversation), and manage the "
            "monitored organization assets that drive alert matching"
        ),
        "logo": "/logos/cybersixgill.svg",
        "category": "integration",
        "integration_category": "threat_intelligence",
        "integration_types": ["threat_intel"],
        # Cybersixgill authenticates with an OAuth2 client-credentials grant
        # (client ID + client secret exchanged for a short-lived bearer token)
        # - the client secret is the stored secret, so the supported auth mode
        # is "api_key" (the client secret goes in the API Key field).
        "auth_type": "api_key",
        "supported_auth_types": ["api_key"],
        "fields": ["api_key"],
        "extra_fields": {
            "base_url": (
                "Cybersixgill API base URL (default "
                "https://api.cybersixgill.com; leave blank unless directed to "
                "a regional/dedicated host)"
            ),
            "client_id": "Cybersixgill API client ID",
            "organization_id": (
                "MSSP organization ID (optional; scopes the actionable-alert, "
                "report, and organization-asset tools to that managed "
                "organization - not used by the enrichment and search tools)"
            ),
        },
        "setup_guide": {
            "title": "Cybersixgill API Client Setup",
            "url": "https://developer.cybersixgill.com",
            "steps": [
                "Sign in to the Cybersixgill / Bitsight developer portal at https://developer.cybersixgill.com with your Cybersixgill account.",
                "Click 'GENERATE API CLIENT AND SECRET' (or 'GENERATE NEW SECRET' to rotate). Copy the client ID and client secret immediately - the secret is only shown once.",
                "Enter the client ID in 'client_id' and the client secret in the 'API Key' field below (it is stored encrypted and exchanged for short-lived bearer tokens).",
                "Leave 'base_url' as https://api.cybersixgill.com unless directed to a regional/dedicated host.",
                "MSSP only: enter the managed organization ID in 'organization_id' - the actionable-alert, report, and organization-asset tools scope their requests to that organization.",
                "Click 'Test Connection' to verify the credentials (performs the OAuth2 client-credentials token exchange).",
            ],
            "permissions": [
                {
                    "scope": "Darkfeed Enrichment",
                    "description": "Enrich CVEs (DVE score + remediation), enrich IP/domain/URL/hash IOCs, pivot on actors and posts, and search dark web intel items",
                },
                {
                    "scope": "Actionable Alerts",
                    "description": "List, read, update (triage), and permanently delete per-organization actionable alerts and their matched content, read finished-intelligence reports, and read/manage the monitored organization assets",
                },
                {
                    "scope": "Multi-Tenancy API (MSSP)",
                    "description": "MSSP accounts only - scope the actionable-alert, report, and organization-asset tools to a managed organization via organization_id (not needed for single-tenant accounts)",
                },
            ],
            "notes": (
                "Cybersixgill authenticates with an OAuth2 client-credentials "
                "grant: the client ID and client secret are exchanged at "
                "POST {base_url}/auth/token for a short-lived bearer token, "
                "which this integration mints and caches automatically. There "
                "are no OAuth scopes to configure - the API client's permission "
                "set bounds what the agent can do (Darkfeed Enrichment for the "
                "enrichment and search tools; Actionable Alerts for the alert, "
                "report, and organization-asset tools; Multi-Tenancy only for "
                "MSSP accounts scoping requests via organization_id). Note the "
                "alert tools include write and destructive operations (triage "
                "updates, permanent alert deletion, asset replacement). The base "
                "URL is the API host "
                "(https://api.cybersixgill.com), not the investigative portal "
                "UI host (https://portal.cybersixgill.com). The client secret "
                "is stored encrypted here."
            ),
        },
    },
    "socradar": {
        "name": "SOCRadar",
        "description": (
            "SOCRadar Extended Threat Intelligence platform - triage and "
            "manage incidents/alarms (status, comments, assignees, tags), "
            "enrich IOCs with ThreatFusion analysis (risk score, whois, DNS, "
            "geo), consume threat intelligence feed collections, hunt dark "
            "web findings (botnet data, black market, PII exposure), review "
            "brand protection and fraud protection findings, manage the "
            "attack surface (digital asset inventory, registration, "
            "monitoring toggles), file and track takedown requests, and "
            "review platform audit logs"
        ),
        "logo": "/logos/socradar.svg",
        "category": "integration",
        "integration_category": "threat_intelligence",
        "integration_types": ["threat_intel"],
        # SOCRadar authenticates with the company API key in the ``API-Key``
        # header - there is no OAuth, so the only supported auth mode is
        # "api_key".
        "auth_type": "api_key",
        "supported_auth_types": ["api_key"],
        "fields": ["api_key"],
        "extra_fields": {
            "company_id": (
                "SOCRadar Company ID (the numeric ID shown next to the API "
                "key under Settings > API & Integrations > API Options)"
            ),
            "base_url": (
                "SOCRadar API base URL (default "
                "https://platform.socradar.com/api; only change it for "
                "testing/proxy setups)"
            ),
        },
        "setup_guide": {
            "title": "SOCRadar API Key Setup",
            "url": "https://platform.socradar.com/docs/api/",
            "steps": [
                "Sign in to the SOCRadar platform (https://platform.socradar.com) as a company administrator.",
                "Open Settings > API & Integrations and go to the API Options page.",
                "Copy the company API key (regenerate it via the Actions button if needed) and note the numeric Company ID displayed next to it.",
                "Enter the Company ID in 'company_id' and the API key in the 'API Key' field; leave 'base_url' as https://platform.socradar.com/api.",
                "Click 'Test Connection' to verify the credentials.",
            ],
            "permissions": [
                {
                    "scope": "Incident API",
                    "description": "List/triage incidents (alarms), change status, comment, assign, tag",
                },
                {
                    "scope": "ThreatFusion / IoC enrichment",
                    "description": "Indicator threat analysis (risk score, whois, DNS, geo, findings)",
                },
                {
                    "scope": "Threat intelligence feeds",
                    "description": "Pull threat feed collections (the OpenCTI connector's feed API)",
                },
                {
                    "scope": "Digital Risk Protection",
                    "description": "Dark web monitoring, brand protection and fraud protection findings",
                },
                {
                    "scope": "Attack Surface Management",
                    "description": "Digital asset inventory, asset registration and monitoring toggles",
                },
                {
                    "scope": "Takedowns",
                    "description": "File and track domain / social media / source code takedown requests",
                },
                {
                    "scope": "Audit logs",
                    "description": "Read the SOCRadar platform user activity trail",
                },
            ],
            "notes": (
                "SOCRadar authenticates every request with the company API "
                "key in the 'API-Key' header (no OAuth). Some modules "
                "(e.g. ThreatFusion threat analysis, takedowns) require "
                "the matching subscription and are rate limited per plan "
                "- the ThreatFusion analysis API allows roughly 6 "
                "requests per minute. The API key is stored encrypted "
                "here."
            ),
        },
    },
    "flashpoint": {
        "name": "Flashpoint",
        "description": (
            "Flashpoint Ignite threat intelligence - search analyst-written "
            "intelligence reports, hunt and enrich IOCs (technical "
            "intelligence with actor/malware/MITRE context), search illicit "
            "communities and forums and store the media they carry as files, "
            "monitor compromised credentials (CCM), review alerting-inbox "
            "notifications, and query vulnerability intelligence (VulnDB: "
            "severity, EPSS, ransomware scores, affected products and packages)"
        ),
        "logo": "/logos/flashpoint.svg",
        "category": "integration",
        "integration_category": "threat_intelligence",
        "integration_types": ["threat_intel"],
        # Flashpoint authenticates with a single API token sent as a Bearer
        # token - there is no OAuth, so the only supported auth mode is
        # "api_key".
        "auth_type": "api_key",
        "supported_auth_types": ["api_key"],
        "fields": ["api_key"],
        "extra_fields": {
            "base_url": (
                "Flashpoint Ignite API base URL (default https://api.flashpoint.io; only override for an API proxy)"
            ),
        },
        "setup_guide": {
            "title": "Flashpoint API Token Setup",
            "url": "https://app.flashpoint.io/tokens",
            "steps": [
                "Log in to Flashpoint Ignite at app.flashpoint.io with an account that has API access.",
                "Open the settings menu (upper-right) and go to 'API Tokens' (app.flashpoint.io/tokens).",
                "Click 'Generate token', name it, and confirm with your credentials.",
                "Copy the token and paste it into the 'API Key' field above (it is stored encrypted).",
                "Leave 'base_url' as https://api.flashpoint.io unless your requests must go through an API proxy.",
                "Click 'Test Connection' to verify the token (runs a minimal indicators query).",
                "Note: data surfaces follow your Flashpoint subscriptions - e.g. CCM credential search requires the Compromised Credentials Monitoring module, and the token must have been created after the subscription was enabled.",
            ],
            "permissions": [
                {
                    "scope": "Finished Intelligence",
                    "description": "Search and read analyst intelligence reports and related reporting",
                },
                {
                    "scope": "Technical Intelligence",
                    "description": "Hunt IOCs, single-IOC reputation lookups, and event observations",
                },
                {
                    "scope": "Communities Intelligence",
                    "description": "Full-text search over illicit forums, chats, marketplaces, and paste sites, and download of their media assets (images, videos, posted files)",
                },
                {
                    "scope": "Compromised Credentials Monitoring (CCM)",
                    "description": "Search credential sightings from stealer logs and breach dumps (subscription module)",
                },
                {
                    "scope": "Alerting",
                    "description": "Read the Ignite alerting inbox (saved-search and asset alerts)",
                },
                {
                    "scope": "Vulnerability Intelligence (VulnDB)",
                    "description": "Search vulnerabilities, CVE lookups, affected packages/libraries, vendors/products",
                },
            ],
            "notes": (
                "Flashpoint authenticates every request with a single API "
                "token sent as 'Authorization: Bearer <token>' (no OAuth). "
                "Tokens are tied to your user and your organization's "
                "subscriptions: endpoints outside your entitlement return "
                "403. The Ignite API is read-only - the integration exposes "
                "no write or destructive tools. A media asset becomes a file "
                "of the conversation; a posted file other than an image or a "
                "video can be live malware and is stored only as a "
                "password-protected ZIP. Rate limiting is enforced "
                "server-side with 429 responses, which the platform retries "
                "automatically."
            ),
        },
    },
    "doppel": {
        "name": "Doppel",
        "description": (
            "Doppel digital risk protection / social engineering defense - "
            "triage brand-impersonation alerts (phishing domains, fake "
            "social accounts, rogue mobile apps, scam ads, telco smishing, "
            "dark-web mentions), report suspicious URLs and phone numbers, "
            "drive takedowns via the alert queue workflow, submit referrer "
            "logs for threat discovery, scan URLs for phishing "
            "suspiciousness, manage brands and protected assets, and report "
            "on Human Risk Management phishing-simulation campaigns and "
            "per-user engagement events"
        ),
        "logo": "/logos/doppel.svg",
        "category": "integration",
        "integration_category": "exposure_management",
        "integration_types": ["brand_protection"],
        # Doppel supports two API generations behind the "api_key" auth mode:
        # V2 (preferred, GA 2026-07) authenticates with OAuth 2.0 client
        # credentials (client_id + client_secret in extra config, exchanged
        # for short-lived bearer tokens); V1 uses static header keys
        # (x-api-key + optional x-user-api-key). When both a client_id and
        # client_secret are configured, V2 wins.
        "auth_type": "api_key",
        "supported_auth_types": ["api_key"],
        "fields": ["api_key"],
        "extra_fields": {
            "base_url": (
                "Doppel API base URL (default https://api.doppel.com; only "
                "change it if Doppel gives you a different host)"
            ),
            "client_id": (
                "V2 OAuth Client ID from the Version 2 tab of Doppel Vision "
                "API Settings (preferred; set together with client_secret)"
            ),
            "client_secret": (
                "V2 OAuth Client Secret (shown once at creation; stored "
                "encrypted and exchanged for short-lived bearer tokens)"
            ),
            "user_api_key": (
                "V1 user API key from Doppel Vision API settings (V1 only: "
                "requests then run with that user's role and permissions)"
            ),
            "organization_code": (
                "V1 organization code (3 letters) - only required when the "
                "user belongs to multiple Doppel organizations (V2 derives "
                "the organization from the token)"
            ),
        },
        "setup_guide": {
            "title": "Doppel API Setup (V2 OAuth or V1 API key)",
            "url": "https://doppel.readme.io/docs/authentication",
            "steps": [
                "Preferred (API V2): as an organization admin, open Doppel Vision (https://app.doppel.com) > API Settings > Version 2 tab and create a Client ID + Client Secret (the secret is shown only once). Paste them into the client_id and client_secret fields below - the API Key field can stay empty.",
                "Alternative (API V1): ask your Doppel representative for your organization API key (x-api-key) and paste it into the API Key field. Optionally generate your personal User API key in Doppel Vision API settings and set user_api_key so requests run with your own role (self-service and self-rotatable).",
                "V1 only: if your user belongs to multiple Doppel organizations, set the 3-letter organization code in the organization_code field (V2 derives the organization from the token).",
                "Leave the base URL as https://api.doppel.com unless Doppel gave you a different API host.",
                "Click 'Test Connection' to verify the credentials (V2 performs the OAuth token exchange plus a 1-record alerts probe).",
            ],
            "permissions": [
                {
                    "scope": "Alerts (View)",
                    "description": "List and read alerts across all Doppel products, and download an alert's screenshot",
                },
                {
                    "scope": "Alerts (Edit)",
                    "description": (
                        "Create alerts, update queue/entity state (request "
                        "takedowns, resolve, archive), comment, and tag"
                    ),
                },
                {
                    "scope": "Referrer logs (Submit)",
                    "description": "Submit referrer logs for threat discovery",
                },
                {
                    "scope": "URL scanning (Submit)",
                    "description": ("Submit URLs for phishing-suspiciousness analysis and poll scan verdicts"),
                },
                {
                    "scope": "Brands (View / Delete)",
                    "description": (
                        "List active brands and archive a brand (disables its sourcing queries and assets)"
                    ),
                },
                {
                    "scope": "Protected assets (View / Edit)",
                    "description": ("List, create, and delete the owned URLs and handles that suppress alerting"),
                },
                {
                    "scope": "HRM (View)",
                    "description": ("Report on phishing-simulation campaigns and per-user engagement events"),
                },
            ],
            "notes": (
                "API V2 (OAuth client credentials) is Doppel's current "
                "generation and is preferred: tokens are short-lived and "
                "the organization is derived from the token. V1 static "
                "keys keep working: the org-wide 'x-api-key' header plus "
                "an optional personal 'x-user-api-key' so actions run "
                "under your own RBAC role. Alert responses include a "
                "doppel_link deep link into Doppel Vision "
                "(https://app.doppel.com) and a signed screenshot URL (1 hour), "
                "which the screenshot download stores as a file of the "
                "conversation. API rate limits are per-endpoint "
                "(roughly 100 requests/min for alert operations). All "
                "credentials are stored encrypted here."
            ),
        },
    },
    "aikido": {
        "name": "Aikido Security",
        "description": (
            "Aikido Security application security, the whole public API: issues and issue groups "
            "(export, detail, counts, reachability, exploitability, severity, snooze, ignore, solve, "
            "notes), code repositories, pull request checks, containers and registries, clouds, "
            "domains and DAST, virtual machines, SBOM and license exports, compliance reports "
            "(SOC 2, ISO 27001, NIS2, DORA, PCI, CIS, FedRAMP), Autofix, Zen runtime protection, "
            "pentests, custom SAST rules, endpoint protection, code quality and coverage, teams, "
            "users, task tracking and webhooks, plus webhook triggers"
        ),
        "logo": "/logos/aikido.svg",
        "category": "integration",
        "integration_category": "exposure_management",
        # application_security puts it in the Application security capability of a
        # fleet and keeps it out of the Vulnerability scanner one (#1434).
        "integration_types": ["vulnerability_management", "application_security"],
        "supports_assignment_webhooks": True,
        "available_events": [
            {
                "event": AIKIDO_TRIGGER_EVENTS[event],
                "label": label,
                # One description for every event (the label names it), so the
                # trigger picker reads it in the viewer's language.
                "description": (
                    "Triggers on this Aikido webhook event. Add a webhook for it in Aikido "
                    "(Settings > Integrations > Webhooks, or the add_aikido_webhook tool) pointing to the "
                    "webhook URL shown below."
                ),
                "mechanism": "webhook",
                "filter_keys": _AIKIDO_FILTER_KEYS,
            }
            for event, label in AIKIDO_EVENT_TYPES.items()
        ],
        # OAuth2 client credentials: the client secret is the stored secret.
        "auth_type": "api_key",
        "supported_auth_types": ["api_key"],
        "fields": ["api_key"],
        "extra_fields": {
            "region": "Data region of the Aikido workspace",
            "client_id": "API client ID",
            "webhook_secret": "Webhook HMAC signing secret (X-Aikido-Webhook-Signature)",
        },
        "extra_field_options": {
            "region": [
                {"value": "eu", "label": "Europe - app.aikido.dev"},
                {"value": "us", "label": "United States - app.us.aikido.dev"},
                {"value": "au", "label": "Australia - app.au.aikido.dev"},
                {"value": "me", "label": "Middle East - app.me.aikido.dev"},
            ],
        },
        "setup_guide": {
            "title": "Aikido API client",
            "url": "https://apidocs.aikido.dev/reference/authorization",
            "steps": [
                "In Aikido, as a workspace admin, open Settings > Integrations > API and create a REST API client.",
                "Grant the scopes the agents need: the read scopes for a read-only binding, the write scopes for triage and configuration tools.",
                "Pick the region in 'Data region of the Aikido workspace', paste the client ID in the 'API client ID' field and the client secret in the 'API key' field.",
                "Optional, for webhook triggers: in Settings > Integrations > Webhooks, click 'Add secret', paste the HMAC signing secret in the 'Webhook HMAC signing secret (X-Aikido-Webhook-Signature)' field, then add a webhook per event pointing to the webhook URL shown below.",
                "Click 'Test Connection': it mints a token and reads the workspace.",
            ],
            "permissions": [
                {
                    "scope": "issues:read, repositories:read",
                    "description": "Read issues and code repositories",
                },
                {
                    "scope": "containers:read, clouds:read",
                    "description": "Read containers and clouds",
                },
                {
                    "scope": "domains:read, basics:read",
                    "description": "Read domains and the workspace",
                },
                {
                    "scope": "teams:read, users:read",
                    "description": "Read teams and users",
                },
                {
                    "scope": "reports:read, research:read",
                    "description": "Read reports and research data",
                },
                {
                    "scope": "webhooks:read",
                    "description": "Read webhooks",
                },
                {
                    "scope": "issues:write and the other :write scopes",
                    "description": "Optional: triage, configuration and the destructive tools (each asks a person)",
                },
            ],
        },
    },
    "airwallex": {
        "name": "Airwallex",
        "description": (
            "Airwallex global business accounts, one integration per account or entity: balances and "
            "balance history, financial transactions, global accounts, deposits, transfers, beneficiaries, "
            "FX rates and conversions, cards and card transactions, invoices, payment intents, "
            "settlements, payment links and financial reports, with webhook triggers. Write tools "
            "(beneficiaries, transfers, FX quotes and conversions, cards and cardholders, payment links, "
            "billing invoices) ask a person on every call. Bank and card numbers are masked."
        ),
        "logo": "/logos/airwallex.svg",
        "category": "integration",
        "integration_category": "business_operations",
        "integration_types": ["finance"],
        "supports_assignment_webhooks": True,
        "available_events": [
            {
                "event": AIRWALLEX_TRIGGER_EVENTS[event],
                "label": label,
                # One description for every event (the label names it), so the
                # trigger picker reads it in the viewer's language.
                "description": (
                    "Triggers on this Airwallex webhook event. Add a webhook for it in "
                    "Airwallex > Settings > Developer > Webhooks, pointing to the webhook URL "
                    "shown below."
                ),
                "mechanism": "webhook",
                "filter_keys": _AIRWALLEX_FILTER_KEYS,
            }
            for event, label in AIRWALLEX_EVENT_TYPES.items()
        ],
        "auth_type": "api_key",
        "supported_auth_types": ["api_key"],
        "fields": ["api_key"],
        "extra_fields": {
            "environment": "Airwallex environment",
            "client_id": "Client ID",
            "login_as": "Account ID to sign in as (organisation keys only)",
            "webhook_secret": "Webhook secret (x-signature)",
        },
        "extra_field_options": {
            "environment": [
                {"value": "production", "label": "Production - api.airwallex.com"},
                {"value": "demo", "label": "Sandbox - api-demo.airwallex.com"},
            ],
        },
        "setup_guide": {
            "title": "Airwallex API key",
            "url": "https://www.airwallex.com/docs/developer-tools/api/manage-api-keys",
            "steps": [
                "In Airwallex, as an account admin, open Settings > Developer > API keys and create an API key (or a restricted key with the permissions below).",
                "Paste the Client ID in the 'Client ID' field and the API key in the 'API key' field, and pick the environment.",
                "One integration per Airwallex account (legal entity). An organisation key reaching several accounts: set 'Account ID to sign in as', or let the tools pass on_behalf_of per call.",
                "Every write tool (transfer, beneficiary, conversion, card, payment link, invoice) asks a person before it runs; bind the integration read-only to withhold them from an agent.",
                "Optional, for webhook triggers: in Settings > Developer > Webhooks, add a webhook pointing to the webhook URL shown below, subscribe to the events and paste its secret in 'Webhook secret (x-signature)'.",
                "Click 'Test Connection': it signs in and reads the account.",
            ],
            "permissions": [
                {
                    "scope": "Balances, Wallet, Global accounts: view",
                    "description": "Read balances, balance history, financial transactions and global accounts",
                },
                {
                    "scope": "Payouts, Transfers, Beneficiaries: view",
                    "description": "Read transfers and beneficiaries",
                },
                {
                    "scope": "FX: view",
                    "description": "Read rates, quotes and conversions",
                },
                {
                    "scope": "Issuing: view",
                    "description": "Read cards, cardholders and card transactions",
                },
                {
                    "scope": "Billing, Payments, Reports: view",
                    "description": "Read invoices, payment intents, settlements and generate financial reports",
                },
                {
                    "scope": "Payouts, FX, Issuing: edit",
                    "description": "The write tools: each asks a person before it runs",
                },
            ],
        },
    },
    "rydoo": {
        "name": "Rydoo",
        "description": (
            "Rydoo expense management, the whole public API: expenses ready for accounting by branch "
            "(legal entity), user, group or trip, expense detail with VAT lines, receipts (download and "
            "upload), reject, report and reimburse, reimbursements and jobs, card and bank transactions, "
            "users, groups, roles and treasurer permissions, approval and control flows, projects, "
            "categories, custom fields (cost centres, GL codes), trips, guests, linked accounts, branch "
            "exchange rates, e-invoices and payment methods (bank details masked), plus expense webhook "
            "triggers"
        ),
        "logo": "/logos/rydoo.svg",
        "category": "integration",
        "integration_category": "business_operations",
        "integration_types": ["finance", "expense_management"],
        # Rydoo activates one webhook URL per company (through its CSM): the
        # integration's URL, not one per assignment.
        "available_events": [
            {
                "event": RYDOO_TRIGGER_EVENTS[event],
                "label": label,
                "description": (
                    "Triggers on this Rydoo expense webhook event. Rydoo activates the webhook on request "
                    "(your CSM or connect@rydoo.com) for the webhook URL shown below. Without it, schedule "
                    "the list_rydoo_exported_expenses tool instead."
                ),
                "mechanism": "webhook",
                "filter_keys": _RYDOO_FILTER_KEYS,
            }
            for event, label in RYDOO_EVENT_TYPES.items()
        ],
        # OAuth2 client credentials: the client secret is the stored secret.
        "auth_type": "api_key",
        "supported_auth_types": ["api_key"],
        "fields": ["api_key"],
        "extra_fields": {
            "client_id": "API client ID",
            "webhook_token": "Webhook token (a long random secret you choose; needed for webhook triggers, see the setup guide)",
        },
        "setup_guide": {
            "title": "Rydoo API client",
            "url": "https://developers.rydoo.com/reference/authentication",
            "steps": [
                "Ask Rydoo (your CSM or connect@rydoo.com) for API access: Rydoo issues a client ID and a client secret for the company.",
                "Paste the client ID in the 'API client ID' field and the client secret in the 'API key' field.",
                "Click 'Test Connection': it mints a token and lists the company's branches (legal entities).",
                "Optional, for webhook triggers: set 'Webhook token' to a long random secret, then give Rydoo the webhook URL shown below followed by ?token=<webhook token> and ask them to activate the expense webhook. A delivery without the token, or with no token configured, is refused.",
                "Every write tool (reject, mark reported or reimbursed, create or update a user, group, project, list item or payment method) asks a person on each call; bind read-only to withhold them.",
            ],
            "permissions": [
                {
                    "scope": "api_xpd2",
                    "description": "The Rydoo public API scope (company-wide: every branch)",
                },
            ],
        },
    },
    "spendesk": {
        "name": "Spendesk",
        "description": (
            "Spendesk spend management, the whole Public API, multi-entity: wallet summary, wallet "
            "loads and settlements, card payments and transactions, requests, payables (expenses, "
            "invoices, card payments) with receipt downloads and missing receipts, cards, members, "
            "cost centres, analytical fields, expense categories, suppliers (bank details masked), "
            "chart of accounts, accounting journals and webhooks; every write asks a person, plus "
            "webhook triggers"
        ),
        "logo": "/logos/spendesk.svg",
        "category": "integration",
        "integration_category": "business_operations",
        "integration_types": ["spend_management"],
        "supports_assignment_webhooks": True,
        "available_events": [
            {
                "event": SPENDESK_TRIGGER_EVENTS[event],
                "label": label,
                "description": (
                    "Triggers on this Spendesk webhook event. Create a webhook for it in Spendesk "
                    "(the create_spendesk_webhook tool) pointing to the webhook URL shown below."
                ),
                "mechanism": "webhook",
                "filter_keys": _SPENDESK_FILTER_KEYS,
            }
            for event, label in SPENDESK_EVENT_TYPES.items()
        ],
        # OAuth2 client credentials: the client secret is the stored secret.
        "auth_type": "api_key",
        "supported_auth_types": ["api_key"],
        "fields": ["api_key"],
        "extra_fields": {
            "environment": "Spendesk environment",
            "client_id": "Public API client ID",
            "webhook_secret": "Webhook signing secret (x-spendesk-webhook-signature)",
        },
        "extra_field_options": {
            "environment": [
                {"value": "production", "label": "Production - public-api.spendesk.com"},
                {"value": "sandbox", "label": "Sandbox - public-api.demo.spendesk.com"},
            ],
        },
        "setup_guide": {
            "title": "Spendesk Public API credential",
            "url": "https://developer.spendesk.com/reference/general",
            "steps": [
                "In Spendesk, as an account owner, open Settings > Integrations > API access and create a Public API credential, at organisation level to reach every entity.",
                "Grant the scopes the agents need: the :read scopes for a read-only binding, the :write scopes for approvals, cards, suppliers, members and bookkeeping.",
                "Pick the environment, paste the client ID in the 'Public API client ID' field and the client secret in the 'API key' field.",
                "Optional, for webhook triggers: create a webhook with the create_spendesk_webhook tool pointing to the webhook URL shown below, and paste the secret it answers in the 'Webhook signing secret' field.",
                "Click 'Test Connection': it mints a token and lists the organisation's entities.",
            ],
            "permissions": [
                {
                    "scope": "payable:read, transaction:read, request:read",
                    "description": "Read payables, receipts, transactions and requests",
                },
                {
                    "scope": "settlement:read, wallet-summary:read, wallet-load:read",
                    "description": "Read the wallet, its loads and settlements",
                },
                {
                    "scope": "card:read, user:read, supplier:read, cost-center:read",
                    "description": "Read cards, members, suppliers and cost centres",
                },
                {
                    "scope": "the :write scopes",
                    "description": "Optional: every write tool, each asks a person before it runs",
                },
            ],
        },
    },
    "wiz": {
        "name": "Wiz",
        "description": (
            "Wiz cloud security (CNAPP) - triage and remediate prioritised "
            "cloud security Issues (misconfigurations, toxic combinations, "
            "threat detections) with status updates, due dates, resolutions, "
            "and investigation notes, query vulnerability findings with "
            "CVE/severity/exploitability/CISA-KEV filters, search the Wiz "
            "Security Graph for cloud resources by type, cloud platform, and "
            "subscription, list connected cloud accounts, and review projects "
            "and security/compliance frameworks - plus a one-shot posture "
            "summary for status reporting"
        ),
        "logo": "/logos/wiz.svg",
        "category": "integration",
        "integration_category": "exposure_management",
        "integration_types": ["cloud_security", "vulnerability_management"],
        # Wiz authenticates with an OAuth2 client-credentials grant (service
        # account client ID + client secret exchanged for a short-lived bearer
        # token) - the client secret is the stored secret, so the supported
        # auth mode is "api_key" (the client secret goes in the API Key field).
        "auth_type": "api_key",
        "supported_auth_types": ["api_key"],
        "fields": ["api_key"],
        "extra_fields": {
            "api_endpoint_url": (
                "Tenant-specific Wiz API endpoint URL (e.g. "
                "https://api.us1.app.wiz.io - shown in the Wiz portal under "
                "Profile > User Settings > API Endpoint URL; do NOT append "
                "/graphql)"
            ),
            "client_id": "Wiz service account client ID",
        },
        "setup_guide": {
            "title": "Wiz Service Account Setup",
            "url": "https://docs.wiz.io",
            "steps": [
                "Log in to the Wiz portal as an admin and go to Settings > Access Management > Service Accounts.",
                "Click 'Add Service Account', give it a descriptive name (e.g. 'XTM One agent'), and select the type 'Custom Integration (GraphQL API)'.",
                "Grant the API scopes the agent needs: read:issues, read:vulnerabilities, read:resources, read:cloud_accounts, read:projects, read:security_frameworks, and update:issues for issue triage (status updates, resolutions, and notes). Prefer least privilege - read-only scopes still enable every read tool.",
                "Copy the client ID and client secret immediately - the secret is only shown once at creation and cannot be recovered.",
                "Find your tenant's API endpoint URL in the Wiz portal under Profile > User Settings > API Endpoint URL (format https://api.<region>.app.wiz.io, e.g. https://api.us1.app.wiz.io).",
                "Enter the API endpoint URL in 'api_endpoint_url' and the client ID in 'client_id', then paste the client secret in the 'API Key' field below (it is stored encrypted and exchanged for short-lived bearer tokens).",
                "Click 'Test Connection' to verify (performs the OAuth2 client-credentials token exchange plus a no-scope GraphQL probe against your tenant endpoint).",
            ],
            "permissions": [
                {
                    "scope": "read:issues",
                    "description": ("List and read cloud security Issues and the posture summary counts"),
                },
                {
                    "scope": "read:vulnerabilities",
                    "description": "Query vulnerability findings (CVE detections)",
                },
                {
                    "scope": "read:resources",
                    "description": ("Search the Security Graph for cloud resources"),
                },
                {
                    "scope": "read:cloud_accounts",
                    "description": ("List the cloud accounts/subscriptions connected to Wiz"),
                },
                {
                    "scope": "read:projects / read:security_frameworks",
                    "description": ("List projects and security/compliance frameworks"),
                },
                {
                    "scope": "update:issues",
                    "description": (
                        "Update issue status, due dates, and resolutions, and "
                        "add investigation notes (only needed for the two "
                        "write tools)"
                    ),
                },
            ],
            "notes": (
                "Wiz exposes a GraphQL-only API: every call is a POST to your "
                "tenant-specific endpoint https://api.<region>.app.wiz.io"
                "/graphql. Authentication is an OAuth2 client-credentials "
                "grant against the Wiz identity provider (Amazon Cognito "
                "tenants: https://auth.app.wiz.io/oauth/token with audience "
                "wiz-api; legacy Auth0 tenants: https://auth.wiz.io"
                "/oauth/token with audience beyond-api) - this integration "
                "tries both automatically and caches the short-lived bearer "
                "token. The API endpoint URL is the API host, not the portal "
                "UI host (https://app.wiz.io). Wiz enforces per-service-"
                "account rate limits (guideline: under ~3 requests/second) "
                "with HTTP 429 responses, which the platform retries "
                "automatically. The client secret is stored encrypted here."
            ),
        },
    },
    "credly": {
        "name": "Credly",
        "description": "Digital Credentials — Badge Templates, Issued Badges, Organizations",
        "logo": "/logos/credly.svg",
        "category": "integration",
        "integration_category": "business_operations",
        "integration_types": ["credentials"],
        "auth_type": "api_key",
        "fields": ["api_key"],
        "extra_fields": {"organization_id": "Credly Organization ID"},
        "setup_guide": {
            "title": "Credly API Token Setup",
            "url": "https://docs.credly.com/browse/reference/get_v1-organizations-organization-id-badge-templates",
            "steps": [
                "Log in to Credly as an organization admin.",
                "Go to your organization settings or use the Authorization Tokens API.",
                "Generate an authorization token with the required permissions.",
                "Copy the authorization token and paste it below as the API Key.",
                "Enter your Credly Organization ID (found in your organization URL or API).",
            ],
            "permissions": [
                {
                    "scope": "read",
                    "description": "Read badge templates, issued badges, and organization info",
                },
                {
                    "scope": "badge_templates",
                    "description": "Manage badge templates (create, update, archive)",
                },
                {
                    "scope": "issued_badges",
                    "description": "Issue, revoke, and manage badges",
                },
            ],
            "notes": "Credly uses Basic Auth with your authorization token. The token is base64-encoded automatically. You need your Organization ID, which can be found in your Credly organization URL or via the API.",
        },
    },
    "wordsmith": {
        "name": "Wordsmith",
        "description": "Legal AI for in-house legal teams — Agents (ask and follow up), Sessions, Tasks, Reviews and Playbooks, Files and document actions, Reports, Repositories, Skills, Events (whole public API)",
        "logo": "/logos/wordsmith.svg",
        "category": "integration",
        "integration_category": "business_operations",
        "integration_types": ["legal"],
        # Wordsmith webhook endpoints (services/wordsmith_webhooks.py): one endpoint
        # per enabled trigger, signed deliveries, caught up from GET /events.
        "available_events": [
            {
                "event": event,
                "label": _WORDSMITH_EVENT_LABELS[event],
                "description": _WORDSMITH_EVENT_DESCRIPTIONS[event],
                "mechanism": "webhook",
                "filter_keys": _WORDSMITH_FILTER_KEYS,
            }
            for event in _WORDSMITH_EVENTS
        ],
        # API key by default; OAuth 2.1 registers this platform as a client
        # of Wordsmith by itself (no client id to paste: oauth_registration,
        # services/integration_oauth_registration.py), PKCE S256 and the REST
        # API as RFC 8707 resource. Wordsmith registers only the redirect URIs
        # on its allowlist (support@wordsmith.ai).
        "auth_type": "api_key",
        "supported_auth_types": ["api_key", "oauth"],
        "fields": ["api_key"],
        "authorize_url": "https://api.wordsmith.ai/oauth/authorize",
        "token_url": "https://api.wordsmith.ai/oauth/token",
        "oauth_registration": {
            "registration_url": "https://api.wordsmith.ai/oauth/register",
            "resource": "https://api.wordsmith.ai/api/v1",
        },
        # Wordsmith's defaults for the REST API plus webhooks:write (the
        # integration_event trigger); a :write scope includes its :read one
        # except files. directory:admin, audit:read and webhooks:admin are
        # never granted unless asked: an API key covers them.
        "scopes": (
            "me:read agents:read sessions:write files:read files:write reviews:write "
            "playbooks:write blueprints:write skills:write repositories:write reports:write "
            "tasks:write research:read directory:read usage:read webhooks:write"
        ),
        "extra_fields": {"region": "API Region"},
        "extra_field_options": {
            "region": [
                {"value": "default", "label": "Default — api.wordsmith.ai"},
                {"value": "us", "label": "US — api.us.wordsmith.ai"},
            ],
        },
        "setup_guide": {
            "title": "Wordsmith API Key Setup",
            "url": "https://app.wordsmith.ai/settings/api-keys",
            "steps": [
                "In Wordsmith, open Settings > API keys (your role needs the 'Use the Wordsmith API' permission).",
                "Create a key named like '{APP_NAME}'. Full access reaches every tool; Selected permissions limits the tools to its scopes.",
                "Optionally set an expiry; the key starts with sk-ws-api1-.",
                "Copy the key (shown once) and paste it below. Pick the US region only if your organisation is hosted there.",
                "OAuth instead of a key: choose the OAuth mode, leave the API key empty, save, then Authorize and approve the scopes on Wordsmith's consent screen. The platform registers itself with Wordsmith; your role needs 'Use the Wordsmith API'. Artifacts accept API keys only.",
            ],
            "permissions": [
                {
                    "scope": "Full access",
                    "description": "Every tool, including artifacts and the routes that need a full-access key",
                },
                {
                    "scope": "Selected permissions",
                    "description": "Only the routes the key's scopes cover; a call outside them answers insufficient_scope with the scope it needs",
                },
                {
                    "scope": "directory:admin / webhooks:write / audit:read",
                    "description": "Organisation management, webhook endpoints and the audit log",
                },
            ],
            "notes": "Each person can connect their own key or OAuth grant, so their XTM One agents reach their own Wordsmith agents, sessions and tasks. OAuth needs this platform's redirect URI on Wordsmith's allowlist (support@wordsmith.ai); until then, use an API key. Rate limit: 400 requests a minute per organisation.",
        },
    },
    "concord": {
        "name": "Concord",
        "description": "Contract Lifecycle Management — Agreements, Documents (PDF / Word, versions, attachments), Signatures, Templates, Negotiation",
        "logo": "/logos/concord.svg",
        "category": "integration",
        "integration_category": "business_operations",
        "integration_types": ["clm"],
        "auth_type": "api_key",
        "fields": ["api_key"],
        "extra_fields": {"organization_id": "Concord Organisation ID"},
        "setup_guide": {
            "title": "Concord API Key Setup",
            "url": "https://api.doc.concordnow.com/",
            "steps": [
                "Log in to your Concord account.",
                "Navigate to your account settings and generate an API key.",
                "Copy the API key and paste it in the field below.",
                "Enter your Organisation ID (found in your Concord account or via the API).",
                "The API key is passed as the X-API-KEY header on every request.",
            ],
            "permissions": [
                {
                    "scope": "Agreements",
                    "description": "List, view, create, update, and delete agreements",
                },
                {
                    "scope": "Documents",
                    "description": "Download the agreement document (PDF / Word) and its attachments, upload a new version and attachments",
                },
                {
                    "scope": "Signatures",
                    "description": "Configure signers, send for signature, check signature status",
                },
                {
                    "scope": "Templates",
                    "description": "List automated templates and create agreements from them",
                },
                {
                    "scope": "Collaboration",
                    "description": "Invite members, manage permissions, view comments",
                },
                {
                    "scope": "Organisation",
                    "description": "List folders, tags, and organisation members",
                },
            ],
            "notes": "Authentication uses the X-API-KEY HTTP header. If the key is missing or invalid, the API returns 401 Unauthorized. You can generate multiple API keys from your Concord account.",
        },
    },
    "dealhub": {
        "name": "DealHub",
        "description": (
            "DealHub CPQ, every documented API: quotes with line items, totals, discounts, playbook "
            "answers and approval steps, quote documents, DealRoom signers, opportunities; simulate, "
            "generate, submit for approval, publish and sign quotes; users; price book; versions, "
            "playbooks, product catalog and hierarchy. Every write asks a person before it runs"
        ),
        "logo": "/logos/dealhub.svg",
        "category": "integration",
        "integration_category": "business_operations",
        "integration_types": ["cpq"],
        # A secret bearer token a CPQ administrator generates; the instance
        # host (api.dealhub.io unless the account is on another instance)
        # lives in extra config.
        "auth_type": "api_key",
        "supported_auth_types": ["api_key"],
        "fields": ["api_key"],
        "extra_fields": {
            "instance_host": "DealHub API host (api.dealhub.io when empty)",
            "webhook_token": "Webhook token (real-time quote events; several tokens separated by commas while rotating)",
        },
        # Quote events: DealHub webhooks (Token authentication) in real time,
        # polling of the quote statuses every 15 minutes as the fallback
        # (services/dealhub_events.py).
        "available_events": _dealhub_catalog_events(),
        "setup_guide": {
            "title": "DealHub API token",
            "url": "https://developers.dealhub.io/docs/authentication-overview",
            "steps": [
                "In DealHub, as a CPQ administrator, generate an API token (Settings > Integrations > API).",
                "Paste the token in the 'API key' field.",
                "Leave 'DealHub API host' empty for api.dealhub.io, or set your instance's host (a name under dealhub.io).",
                "Click 'Test Connection': it reads the account's active users.",
                "Optional, real-time quote events: generate a long random token and paste it in 'Webhook token'; in DealHub, System Settings > API Settings > Webhooks, add a webhook whose URL is this integration's webhook URL ({base_url}/api/hooks/integrations/<integration id>), select the quote events and the Token authentication type with the same token. Without it, the events are polled every 15 minutes.",
            ],
            "permissions": [
                {
                    "scope": "Quote, Headless and CRM APIs",
                    "description": "Read quotes, approvals, documents and DealRoom signers; generate, submit, publish and sign quotes (each write asks a person)",
                },
                {
                    "scope": "User, Pricing and Version APIs",
                    "description": "Read users, prices, versions, playbooks and the product catalog; change them (each write asks a person)",
                },
            ],
            "notes": "DealHub has no API to approve or reject an approval step: approvals are read on the quote and decided in DealHub. The Subskribe billing API has its own credential and is not part of this integration.",
        },
    },
    "docusign": {
        "name": "DocuSign",
        "description": "e-Signature — send PDF and Word files of a conversation for signature as envelopes, follow signers, download the signed document, templates (eSignature REST API v2.1)",
        "logo": "/logos/docusign.svg",
        "category": "integration",
        "integration_category": "business_operations",
        "integration_types": ["clm"],
        # OAuth (Authorization Code Grant) is the default; the JWT Grant (an
        # integration key impersonating a user with an RSA key) is the "API Key"
        # mode, recommended for unattended assignments: the RSA private key goes
        # in the API Key field.
        "auth_type": "oauth",
        "supported_auth_types": ["oauth", "api_key"],
        "fields": ["client_id", "client_secret", "api_key"],
        "scopes": "signature",
        "available_events": [
            {
                "event": event,
                "label": label,
                "description": (
                    f"Triggers when {when}. Carries envelope_id, status, subject, sender_email, "
                    "signer_email, signer_name, signers and web_link. Real time when a DocuSign "
                    "Connect configuration posts to this integration's webhook URL with its HMAC "
                    "key stored here, else polled every 15 min (the interval DocuSign asks "
                    "integrations to keep); the first poll starts from now. Each envelope change "
                    "runs once, whichever path reads it first."
                ),
                "mechanism": "hybrid",
                "filter_keys": {
                    "envelope_ids": "Only these envelopes (ids as create_docusign_envelope returns them)",
                    "signer_emails": signer_key,
                    "sender_emails": "Only envelopes sent by these addresses or domains",
                    "subject_contains": "Only envelopes whose subject contains one of these strings",
                },
            }
            for event, label, when, signer_key in (
                (
                    "envelope_completed",
                    "Envelope completed",
                    "every signer of an envelope has signed and the envelope is completed",
                    "Only envelopes with a signer among these addresses or domains",
                ),
                (
                    "envelope_declined",
                    "Envelope declined",
                    "a signer declines an envelope (signer_email names who declined)",
                    "Only envelopes with a signer among these addresses or domains",
                ),
                (
                    "envelope_voided",
                    "Envelope voided",
                    "the sender voids an envelope (voided_reason says why)",
                    "Only envelopes with a signer among these addresses or domains",
                ),
                (
                    "recipient_completed",
                    "Signer signed",
                    "one signer of an envelope signs, once per signer (signer_email names who)",
                    "Only signatures by these addresses or domains",
                ),
            )
        ],
        "authorize_url": "https://account.docusign.com/oauth/auth",
        "token_url": "https://account.docusign.com/oauth/token",
        "extra_fields": {
            "environment": "Environment",
            "account_id": "DocuSign account ID (API Account ID; blank: the user's default account)",
            "integration_key": "Integration key (client ID) - JWT Grant mode",
            "user_id": "User ID to impersonate (API Username, a GUID) - JWT Grant mode",
            "connect_hmac_key": "DocuSign Connect HMAC key (real-time envelope events; several keys separated by commas while rotating)",
        },
        "extra_field_options": {
            "environment": [
                {"value": "production", "label": "Production - account.docusign.com"},
                {"value": "demo", "label": "Developer (demo) - account-d.docusign.com"},
            ],
        },
        "setup_guide": {
            "title": "DocuSign eSignature Setup",
            "url": "https://developers.docusign.com/platform/auth/",
            "steps": [
                "In DocuSign Admin (or the developer account at admindemo.docusign.com), open Apps and Keys and add an app: its Integration Key is the client ID.",
                "OAuth mode: add the redirect URI {base_url}/api/oauth/callback to the app and generate a Secret Key; paste the Integration Key as Client ID and the Secret Key as Client Secret.",
                "JWT Grant (API Key) mode: generate an RSA keypair on the app, paste the private key in the API Key field, the Integration Key and the User ID (your API Username, under Apps and Keys) in the fields below, then grant consent once by opening https://account.docusign.com/oauth/auth?response_type=code&scope=signature%20impersonation&client_id=<integration key>&redirect_uri=<a redirect URI of the app> signed in as that user.",
                "Choose the environment (production, or demo for a developer account) and optionally the API Account ID (Apps and Keys) when the user belongs to several accounts.",
                "Save, then Authorize (OAuth) or Test Connection (JWT): the platform reads /oauth/userinfo for the account's base URL.",
                "Optional, real-time envelope events: in DocuSign Admin > Connect, add a custom configuration whose URL to publish is this integration's webhook URL ({base_url}/api/hooks/integrations/<integration id>), data format REST v2.1, events Envelope Sent/Delivered/Completed/Declined/Voided and Recipient Signed/Completed, Include Data: Recipients; enable Include HMAC Signature, add a Connect HMAC key and paste it in the Connect HMAC key field. Without it the events are polled every 15 minutes.",
            ],
            "permissions": [
                {
                    "scope": "signature",
                    "description": "Create, send, list, void and download envelopes; list templates",
                },
                {
                    "scope": "impersonation (JWT Grant)",
                    "description": "Let the integration key act as the configured user without a browser sign-in",
                },
            ],
            "notes": (
                "DocuSign's OAuth hosts are account.docusign.com (production) and "
                "account-d.docusign.com (demo): the authorize and token URLs follow the "
                "environment chosen. The REST API host is the account's own base URI, "
                "read from /oauth/userinfo after sign-in. A demo (developer) account only "
                "produces test signatures; a production integration key must be promoted "
                "(Go Live) by DocuSign first."
            ),
        },
    },
    "netsuite": {
        "name": "NetSuite",
        "description": "Oracle NetSuite ERP, OneWorld-ready: SuiteQL, any record, subsidiaries, periods, chart of accounts, transactions and journal lines, trial balance, income statement and balance sheet per subsidiary or consolidated, AR/AP aging, intercompany balances; master data, bills, invoices, payments, journals, bank details and period close, the sensitive writes asked first",
        "logo": "/logos/netsuite.svg",
        "category": "integration",
        "integration_category": "business_operations",
        "integration_types": ["erp", "finance"],
        # Both modes sign their own requests (a client credentials grant with a
        # certificate, or OAuth 1.0a TBA): the secret goes in the API Key field,
        # the account and identifiers in extra_config.
        "auth_type": "api_key",
        "supported_auth_types": ["api_key"],
        "fields": ["api_key"],
        "extra_fields": {
            "account_id": "Account ID (Setup > Company > Company Information, e.g. 1234567 or 1234567_SB1 for a sandbox)",
            "auth_mode": "Authentication",
            "oauth_client_id": "Client ID of the integration record - OAuth 2.0 machine-to-machine",
            "certificate_id": "Certificate ID (OAuth 2.0 Client Credentials Setup) - OAuth 2.0 machine-to-machine",
            "oauth_scopes": "OAuth 2.0 scopes (default rest_webservices; add restlets to call RESTlets)",
            "consumer_key": "Consumer key of the integration record - Token-Based Authentication",
            "consumer_secret": "Consumer secret of the integration record - Token-Based Authentication",
            "token_id": "Token ID of the access token - Token-Based Authentication",
            "companion_script_id": "Companion RESTlet script ID (saved searches, file cabinet, attachments; optional)",
            "companion_deploy_id": "Companion RESTlet deployment ID",
        },
        "extra_field_options": {
            "auth_mode": [
                {
                    "value": "m2m",
                    "label": "OAuth 2.0 machine-to-machine (client credentials with a certificate), recommended",
                },
                {"value": "tba", "label": "Token-Based Authentication (OAuth 1.0a, HMAC-SHA256)"},
            ],
        },
        # Polled with SuiteQL (services/netsuite_events.py, every 5 min): NetSuite
        # posts no change notification without a script of the customer's.
        "available_events": [
            {
                "event": event,
                "label": label,
                "description": description,
                "mechanism": "polling",
                "filter_keys": _NETSUITE_TRANSACTION_FILTER_KEYS,
            }
            for event, label, description in _NETSUITE_TRANSACTION_EVENTS
        ]
        + [
            {
                "event": "accounting_period_changed",
                "label": "Accounting period changed",
                "description": (
                    "Triggers when an accounting period is closed, reopened, locked or unlocked. "
                    "Uses polling (every 5 minutes); the first poll starts from now."
                ),
                "mechanism": "polling",
                "filter_keys": {
                    "states": "Only periods now in one of these states: open, closed, ap_locked, ar_locked, all_locked",
                },
            }
        ],
        "setup_guide": {
            "title": "NetSuite Setup",
            "url": "https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_162686838198.html",
            "steps": [
                "Enable the features (Setup > Company > Enable Features > SuiteCloud): REST Web Services, OAuth 2.0 (machine-to-machine) or Token-Based Authentication, and SuiteAnalytics Workbook for SuiteQL.",
                "Create a dedicated role with the permissions listed below and assign it to an integration user (a person or a dedicated employee record). Grant only the write permissions the agents need: every tool family is its own tool, and the sensitive ones ask a person on every call.",
                "Create an integration record (Setup > Integration > Manage Integrations > New): for OAuth 2.0 machine-to-machine check Client Credentials (Machine to Machine) Grant and the scope REST Web Services (and RESTlets to call RESTlets); for TBA check Token-Based Authentication. Copy the client ID, or the consumer key and secret, shown once.",
                "OAuth 2.0 machine-to-machine (recommended): generate a key pair and a certificate (openssl req -new -x509 -newkey rsa:3072 -nodes -keyout private.pem -out public.pem -days 730 -sha256), then Setup > Integration > OAuth 2.0 Client Credentials (M2M) Setup > Create New: the entity (integration user), the role, the application and public.pem. Paste the Certificate ID and the Client ID below and private.pem in the API Key field.",
                "Token-Based Authentication: create an access token (Setup > Users/Roles > Access Tokens > New) for the application, user and role; paste the consumer key and secret and the token ID below and the token secret in the API Key field.",
                "Optional, saved searches, file cabinet downloads, transaction PDFs and attachments: deploy the companion RESTlet documented in the integrations usage guide (SuiteScript 2.1, the actions search, file, pdf and attach), add the RESTlets scope to the integration record and the OAuth scopes field, and paste its script and deployment IDs below.",
                "Set the account ID (sandbox: 1234567_SB1), choose the authentication mode, save, then Test Connection: it reads the subsidiaries the role reaches.",
            ],
            "permissions": [
                {
                    "scope": "Setup > REST Web Services (Full)",
                    "description": "Every REST call: records, metadata catalog, SuiteQL",
                },
                {
                    "scope": "Setup > Log in using OAuth 2.0 Access Tokens (Full), or Log in using Access Tokens (Full) for TBA",
                    "description": "Authenticate the integration",
                },
                {
                    "scope": "Reports > SuiteAnalytics Workbook (Edit)",
                    "description": "SuiteQL queries (every read tool)",
                },
                {
                    "scope": "Lists > Subsidiaries, Accounts, Accounting Periods, Departments, Classes, Locations, Currency, Customers, Vendors, Items (View)",
                    "description": "Reference data and the finance statements",
                },
                {
                    "scope": "Transactions > Find Transaction, Invoice, Credit Memo, Customer Payment, Sales Order, Bills, Vendor Payment, Purchase Order, Make Journal Entry, Intercompany Journal (View)",
                    "description": "Transactions, journal lines, aging, intercompany balances",
                },
                {
                    "scope": "Lists > Customers, Vendors, Items, Departments, Classes, Locations (Create or Edit)",
                    "description": "create_netsuite_record and update_netsuite_record (master data)",
                },
                {
                    "scope": "Transactions > Bills, Pay Bills, Invoice, Credit Memo, Customer Payment, Make Journal Entry, Intercompany Journal (Create or Edit)",
                    "description": "create_netsuite_transaction and update_netsuite_transaction (ask a person on every call)",
                },
                {
                    "scope": "Setup > Manage Accounting Periods (Full)",
                    "description": "set_netsuite_period_status: close, reopen, lock (asks a person on every call)",
                },
                {
                    "scope": "Custom record Entity Bank Details (View; Edit to change them)",
                    "description": "get_netsuite_entity_bank_details, set_netsuite_entity_bank_details (the write asks a person)",
                },
                {
                    "scope": "Lists > Documents and Files (View; Create to attach)",
                    "description": "download_netsuite_file and attach_netsuite_file through the companion RESTlet",
                },
            ],
            "notes": (
                "Reads are free; master-data writes are writes; posting transactions (bills, invoices, payments, "
                "journals), period close and lock, entity bank detail changes and RESTlet calls other than GET ask a "
                "person on every call. Each family is its own tool, so a binding narrows it with the integration's "
                "allowed tools. Concurrency and governance limits (HTTP 429) are retried with backoff on reads, never "
                "on writes. Consolidated statements use BUILTIN.CONSOLIDATE at the period's "
                "consolidation rates. A role missing a permission or a locked period is reported as such."
            ),
        },
    },
    "hibob": {
        "name": "HiBob",
        "description": "HR — Employees, Time Off, Goals (OKR), Job Catalog, Tasks, Reports, Documents, Payroll, Training, Attendance, Projects, Workforce Planning, Custom Tables, Metadata",
        "logo": "/logos/hibob.svg",
        "category": "integration",
        "integration_category": "hr",
        "integration_types": ["hr"],
        "auth_type": "api_key",
        "fields": ["api_key"],
        "extra_fields": {"service_user_id": "HiBob Service User ID"},
        "setup_guide": {
            "title": "HiBob API Setup",
            "url": "https://app.hibob.com/settings/api-access",
            "steps": [
                "Go to HiBob > Settings > Integrations > API Access.",
                "Create a new Service User or use an existing one.",
                "Set all required permissions on the service user (People, Time Off, Goals, Payroll, Reports, Docs, Tasks, etc.).",
                "Generate an API token for the service user.",
                "Copy the Service User ID and paste it in the 'Service User ID' field below.",
                "Copy the API Token and paste it in the 'API Key' field below.",
            ],
            "permissions": [
                {
                    "scope": "People",
                    "description": "Read, create, update, invite, terminate, and revoke employee profiles",
                },
                {
                    "scope": "Work",
                    "description": "Read and manage work history, departments, titles, and org structure",
                },
                {
                    "scope": "Employment",
                    "description": "Read and manage employment contracts, types, and FTE data",
                },
                {
                    "scope": "Payroll",
                    "description": "Read salary history, variable payments, and payroll data",
                },
                {
                    "scope": "Equity",
                    "description": "Read equity grants, vesting schedules, and exercise prices",
                },
                {
                    "scope": "Training",
                    "description": "Read employee training records and certifications",
                },
                {
                    "scope": "Time Off",
                    "description": "Full access — balances, requests, adjustments, policies, reason codes, who's out",
                },
                {
                    "scope": "Tasks",
                    "description": "Read and complete onboarding and HR tasks",
                },
                {
                    "scope": "Reports",
                    "description": "List and download company reports",
                },
                {
                    "scope": "Documents",
                    "description": "Read, download, upload (a file or a URL), and delete employee documents across all folders",
                },
                {
                    "scope": "Attendance",
                    "description": "Read summaries, daily breakdowns, import entries, manage projects and tasks",
                },
                {
                    "scope": "Onboarding",
                    "description": "Read onboarding wizard summaries and invite employees",
                },
                {
                    "scope": "Workforce Planning",
                    "description": "Search and manage positions and openings",
                },
                {
                    "scope": "Custom Tables",
                    "description": "Full CRUD on custom table definitions and entries",
                },
                {
                    "scope": "Metadata",
                    "description": "Read and manage employee field definitions and company named lists",
                },
                {
                    "scope": "Goals",
                    "description": "Full access — search, create, update, delete goals, key results, and progress check-ins",
                },
                {
                    "scope": "Job Catalog",
                    "description": "Read job profiles, roles, families, family groups, and metadata",
                },
                {
                    "scope": "Lists",
                    "description": "Edit all lists (sites, job titles, departments, etc.)",
                },
            ],
            "notes": "HiBob uses Basic Auth with a Service User ID and API Token. The service user's permissions determine what the API can access. Admin users create service users in Settings > Integrations > API Access. Set all the permissions listed above on the service user for full tool functionality (71 tools across 12 modules).",
        },
    },
    "carta": {
        "name": "Carta",
        "description": "Equity, Cap Table, Valuations",
        "logo": "/logos/carta.svg",
        "category": "integration",
        "integration_category": "business_operations",
        "integration_types": [],
        "auth_type": "api_key",
        "fields": ["api_key"],
        "setup_guide": {
            "title": "Carta API Setup",
            "url": "https://carta.com/developers/",
            "steps": [
                "Contact your Carta account representative to enable API access.",
                "Once approved, go to Carta > Settings > API.",
                "Generate a new API key.",
                "Copy the API key and paste it below.",
            ],
            "permissions": [
                {
                    "scope": "Cap Table",
                    "description": "Read stakeholders, securities, and ownership",
                },
                {
                    "scope": "Valuations",
                    "description": "Read 409A valuations and fair market value",
                },
                {
                    "scope": "Equity Plans",
                    "description": "Read stock option plans and grants",
                },
                {
                    "scope": "Transactions",
                    "description": "Read exercise and transfer history",
                },
            ],
            "notes": "Carta API access may require an enterprise plan. Contact Carta support if the API section is not visible in your settings.",
        },
    },
    "figma": {
        "name": "Figma",
        "description": "Files, Projects, Comments, Components, Styles, Variables, Version History, Dev Resources, Library Analytics",
        "logo": "/logos/figma.svg",
        "category": "integration",
        "integration_category": "development",
        "integration_types": ["design"],
        "auth_type": "oauth",
        "fields": ["client_id", "client_secret"],
        "scopes": "current_user:read file_content:read file_metadata:read file_comments:read file_comments:write file_dev_resources:read file_dev_resources:write file_variables:read file_variables:write file_versions:read projects:read library_assets:read library_content:read team_library_content:read library_analytics:read",
        "authorize_url": "https://www.figma.com/oauth",
        "token_url": "https://api.figma.com/v1/oauth/token",
        "setup_guide": {
            "title": "Figma OAuth App Setup",
            "url": "https://www.figma.com/developers/apps",
            "steps": [
                "Go to figma.com/developers/apps > Create a new app.",
                "App name: '{APP_NAME}'. Associate it with your team or organization.",
                "Under 'OAuth credentials', add the redirect URL: {base_url}/api/oauth/callback",
                "Under 'OAuth scopes', select ALL the scopes listed below.",
                "Copy the Client ID and Client Secret (shown only once at app creation).",
                "Publish the app as Private (for your team/org) or Public (for all users).",
            ],
            "permissions": [
                {
                    "scope": "current_user:read",
                    "description": "Read your name, email, and profile image",
                },
                {
                    "scope": "file_content:read",
                    "description": "Read file contents, nodes, and editor type",
                },
                {
                    "scope": "file_metadata:read",
                    "description": "Read file metadata (name, thumbnail, last modified)",
                },
                {
                    "scope": "file_comments:read",
                    "description": "Read comments and comment reactions on files",
                },
                {
                    "scope": "file_comments:write",
                    "description": "Post, delete comments, and manage reactions on files",
                },
                {
                    "scope": "file_dev_resources:read",
                    "description": "Read dev resources attached to files",
                },
                {
                    "scope": "file_dev_resources:write",
                    "description": "Create, update, and delete dev resources on files",
                },
                {
                    "scope": "file_variables:read",
                    "description": "Read variables and variable collections in files (Enterprise)",
                },
                {
                    "scope": "file_variables:write",
                    "description": "Create, update, and delete variables and collections in files (Enterprise)",
                },
                {
                    "scope": "file_versions:read",
                    "description": "Read version history for accessible files",
                },
                {
                    "scope": "projects:read",
                    "description": "List team projects and project files",
                },
                {
                    "scope": "selections:read",
                    "description": "Read most recent selection in accessible files",
                },
                {
                    "scope": "library_assets:read",
                    "description": "Read individual published components, component sets, and styles",
                },
                {
                    "scope": "library_content:read",
                    "description": "Read published components, component sets, and styles of files",
                },
                {
                    "scope": "team_library_content:read",
                    "description": "Read published components, component sets, and styles of teams",
                },
                {
                    "scope": "library_analytics:read",
                    "description": "Read design system analytics — component, style, and variable usage data (Enterprise)",
                },
                {
                    "scope": "webhooks:read",
                    "description": "Read webhook metadata, details, and delivery request logs",
                },
                {
                    "scope": "webhooks:write",
                    "description": "Create, update, and delete webhooks",
                },
            ],
            "notes": "Users will be prompted to grant access to their Figma account. The integration can only access files the authorizing user has access to. Some scopes (file_variables, library_analytics) require a Figma Enterprise plan. Org-level scopes (org:activity_log_read, org:discovery_read) are excluded because Figma does not allow mixing org and user scopes in the same authorization request. Figma access tokens expire after 90 days; the refresh token flow renews them automatically.",
        },
    },
    "planhat": {
        "name": "Planhat",
        "description": "Customer Success: Companies, Endusers, Conversations, Tasks, Opportunities, Licenses, Assets, Tickets",
        "logo": "/logos/planhat.svg",
        "category": "integration",
        "integration_category": "business_operations",
        "integration_types": ["crm"],
        "auth_type": "api_key",
        "fields": ["api_key"],
        "setup_guide": {
            "title": "Planhat API Setup",
            "url": "https://www.planhat.com/developers/api/introduction",
            "steps": [
                "Log in to Planhat and go to Settings > Private Apps.",
                "Create a new Private App and name it '{APP_NAME}'.",
                "Copy the API Access Token.",
                "Paste the token as the API Key when creating the integration.",
            ],
            "permissions": [
                {
                    "scope": "Companies",
                    "description": "Create, read, update companies with health scores, phases, and custom fields",
                },
                {
                    "scope": "Endusers",
                    "description": "Create, read, update contacts/end-user profiles",
                },
                {
                    "scope": "Conversations",
                    "description": "Read and create notes, email logs, call logs",
                },
                {
                    "scope": "Tasks",
                    "description": "Create, read, update tasks with statuses and due dates",
                },
                {
                    "scope": "Opportunities",
                    "description": "Create, read, update sales opportunities with stages and revenue",
                },
                {
                    "scope": "Licenses",
                    "description": "Read subscription/license data with MRR and renewal info",
                },
                {"scope": "Assets", "description": "Read product assets/instances"},
                {
                    "scope": "Tickets",
                    "description": "Read support tickets with status and severity",
                },
            ],
            "notes": "Planhat uses API Access Tokens for authentication. Tokens are generated from the Private Apps section in Settings. The token provides access based on the app's configured permissions.",
        },
    },
    "metabase": {
        "name": "Metabase",
        "description": "Business Intelligence: Dashboards, Questions, Exports (CSV / XLSX / JSON), SQL Queries, Schema Introspection, Collections, Alerts, Analytics",
        "logo": "/logos/metabase.svg",
        "category": "integration",
        "integration_category": "business_operations",
        "integration_types": [],
        "auth_type": "api_key",
        "fields": ["api_key"],
        "extra_fields": {"base_url": "Metabase instance URL (e.g. https://metabase.company.com)"},
        "setup_guide": {
            "title": "Metabase API Setup",
            "url": "https://www.metabase.com/docs/latest/api",
            "steps": [
                "Log in to Metabase as an admin.",
                "Go to Settings > Authentication > API Keys.",
                "Create a new API key and name it '{APP_NAME}'.",
                "Assign appropriate permissions (read + write access to collections, dashboards, and questions).",
                "Copy the API key.",
                "Enter your Metabase instance URL in the 'Base URL' field.",
                "Paste the API key in the API Key field.",
            ],
            "permissions": [
                {
                    "scope": "Dashboards",
                    "description": "Read, create, update, and delete dashboards",
                },
                {
                    "scope": "Questions/Cards",
                    "description": "Read, create, update, delete, run, and export (download results) saved questions",
                },
                {
                    "scope": "Databases",
                    "description": "List databases, browse schemas and tables, run native queries",
                },
                {
                    "scope": "Collections",
                    "description": "Browse, create, update, and organize content",
                },
                {
                    "scope": "Alerts",
                    "description": "List and create alerts on questions",
                },
                {
                    "scope": "Search",
                    "description": "Search across all Metabase content",
                },
            ],
            "notes": "For Metabase, the API key format is: enter your Metabase base URL in the extra config field, and the API key in the API Key field. The tools will use both to connect to your instance. For full functionality, ensure the API key has write permissions.",
        },
    },
    "odoo": {
        "name": "Odoo",
        "description": "ERP & Accounting (multi-company) - Chart of accounts, Journals, Journal entries and items, Trial balance, General ledger, Aged receivable and payable, FEC-style exports (CSV / XLSX), Invoices, Bills and credit notes (with attachments), Payments and reconciliation, Bank statements, Taxes, Fiscal positions, Analytic accounts, Fixed assets, Currencies, Lock dates, Products, and any model through generic search_read (JSON-2 API, JSON-RPC fallback)",
        "logo": "/logos/odoo.svg",
        "category": "integration",
        "integration_category": "business_operations",
        "integration_types": ["accounting", "finance"],
        "auth_type": "api_key",
        "fields": ["api_key"],
        "extra_fields": {
            "base_url": "Odoo URL (e.g. https://example.odoo.com)",
            "database": "Database name",
            "login": "Login (the user's email)",
            "company_ids": "Default company ids, comma-separated (optional)",
        },
        # Polled from the records' create / write dates (providers/odoo/events.py,
        # every 60 s): the external API has no outbound webhook.
        "available_events": [
            {
                "event": event,
                "label": label,
                "description": description,
                "mechanism": "polling",
                "filter_keys": {
                    "company": "Only records of these companies (ids or names)",
                    "journal": "Only records of these journals (ids, codes or names)",
                    "state": "Only records in these Odoo states (draft, posted, cancel; payments: in_process, paid...)",
                },
            }
            for event, label, description in (
                (
                    "journal_entry_posted",
                    "Journal entry posted",
                    "Triggers when a journal entry is posted in Odoo or a posted entry changes (one run per change). Uses polling (every minute); the first poll starts from now.",
                ),
                (
                    "vendor_bill_created",
                    "Vendor bill created",
                    "Triggers when a vendor bill or refund is created in Odoo. Uses polling (every minute); the first poll starts from now.",
                ),
                (
                    "payment_registered",
                    "Payment registered",
                    "Triggers when a payment is registered in Odoo. Uses polling (every minute); the first poll starts from now.",
                ),
            )
        ],
        "setup_guide": {
            "title": "Odoo API Key Setup",
            "url": "https://www.odoo.com/documentation/latest/developer/reference/external_api.html",
            "steps": [
                "Sign in to Odoo as the user the agents act as (a dedicated user is best: its access rights and companies bound what the tools reach).",
                "Open the user menu > My Profile (Preferences) > Account Security and create a New API Key named '{APP_NAME}'.",
                "Paste the key in the API Key field below.",
                "Fill in the Odoo URL, the database name (Settings > Activate developer mode shows it; on odoo.com it is usually the subdomain) and the login.",
                "Optionally list default company ids; every tool also takes company_ids, and list_odoo_companies names them.",
            ],
            "permissions": [
                {
                    "scope": "Accounting / Invoicing: Billing or Accountant",
                    "description": "Chart of accounts, journals, entries and items, reports, invoices, bills, payments, bank statements, taxes, analytics, assets, lock dates",
                },
                {
                    "scope": "Allowed companies",
                    "description": "The companies the user may switch to are the companies the tools reach (allowed_company_ids)",
                },
                {
                    "scope": "Contacts",
                    "description": "Partners and partner bank accounts",
                },
            ],
            "notes": "Odoo 19+ answers the JSON-2 API (bearer API key); older versions are reached through JSON-RPC with the same key. Read or write follows the Odoo operation: every write tool (create, update, post, cancel, reset, register a payment, reconcile, lock dates, generic create / write / unlink / method) asks a person on every call. Bank account numbers answer masked to their last 4 characters. Bind read-only to withhold every write tool. See https://www.odoo.com/documentation/latest/developer/reference/external_api.html.",
        },
    },
    "databricks": {
        "name": "Databricks",
        "description": "Lakehouse: SQL Warehouses, Unity Catalog, Jobs & Workflows, Model Serving, Vector Search, and files - volume, DBFS and workspace files (notebooks exported) read, stored as files of the conversation, and written from any file of the conversation",
        "logo": "/logos/databricks.png",
        "category": "integration",
        "integration_category": "business_operations",
        "integration_types": ["data_platform", "automation"],
        "auth_type": "api_key",
        "fields": ["api_key"],
        "extra_fields": {
            "base_url": "Databricks workspace URL (e.g. https://dbc-1234abcd-5e6f.cloud.databricks.com)",
            "client_id": "Optional — service principal Application ID, for OAuth machine-to-machine auth instead of a personal access token",
            "warehouse_id": "Optional — default SQL warehouse ID used when a query does not specify one",
        },
        "setup_guide": {
            "title": "Databricks Workspace Setup",
            "url": "https://docs.databricks.com/aws/en/dev-tools/auth/",
            "steps": [
                "Open your Databricks workspace and copy its URL from the browser address bar (e.g. https://dbc-1234abcd-5e6f.cloud.databricks.com) into the 'Base URL' field.",
                "Choose an authentication method. For a personal access token (simplest): go to your avatar > Settings > Developer > Access tokens > Generate new token, name it '{APP_NAME}', and paste it in the API Key field. Leave 'Client ID' empty.",
                "For OAuth machine-to-machine (recommended for production): go to Settings > Identity and access > Service principals, create or pick a service principal, then under its Secrets tab generate an OAuth secret.",
                "With OAuth, paste the service principal's Application ID in the 'Client ID' field and its OAuth secret in the API Key field.",
                "Grant the identity access to what it should reach: 'Can use' on the SQL warehouse, USE CATALOG / USE SCHEMA / SELECT on the Unity Catalog objects it should read, and 'Can view' or 'Can manage run' on the jobs it should monitor or trigger.",
                "Optional: paste a SQL warehouse ID (SQL > SQL Warehouses > the warehouse > Connection details) in the 'Warehouse ID' field so queries do not have to specify one.",
            ],
            "permissions": [
                {
                    "scope": "SQL Warehouses",
                    "description": "List warehouses and execute SQL statements ('Can use' on the warehouse)",
                },
                {
                    "scope": "Unity Catalog",
                    "description": "Browse catalogs, schemas, tables and volumes, and read table schemas (USE CATALOG, USE SCHEMA, SELECT, READ VOLUME)",
                },
                {
                    "scope": "Jobs & Workflows",
                    "description": "List and inspect jobs and runs; trigger and cancel runs when granted 'Can manage run'",
                },
                {
                    "scope": "Compute",
                    "description": "List all-purpose clusters and read their configuration and state",
                },
                {
                    "scope": "Model Serving",
                    "description": "List serving endpoints and invoke them ('Can query' on the endpoint)",
                },
                {
                    "scope": "Vector Search",
                    "description": "List indexes on a Vector Search endpoint and run similarity queries",
                },
                {
                    "scope": "Files",
                    "description": "Read and download files from Unity Catalog volumes (READ VOLUME), DBFS and the workspace ('Can read' on notebooks and folders); write them with WRITE VOLUME / 'Can edit'",
                },
            ],
            "notes": "The API Key field holds the personal access token when 'Client ID' is empty, and the service principal's OAuth secret when it is set — the integration infers the auth mode from the presence of a client ID. Grant read-only privileges unless agents genuinely need to write: query_databricks_sql refuses anything but SELECT/WITH/SHOW/DESCRIBE/EXPLAIN, but execute_databricks_sql can run DML and DDL and upload_databricks_file writes files, so withhold them via the agent's tool scoping if the lakehouse must stay read-only. Workspace exports and imports are limited to 10 MB by the Workspace API.",
        },
    },
    "supabase": {
        "name": "Supabase",
        "description": "Postgres development platform: projects and service health, tables with their RLS policies, read-only and full SQL or function calls as a database role under its RLS, migrations, security and performance advisors, logs, Edge Functions, Storage buckets and branches",
        "logo": "/logos/supabase.svg",
        "category": "integration",
        "integration_category": "development",
        "integration_types": ["data_platform", "observability"],
        # The Management API takes a personal access token as a bearer token;
        # there is no OAuth flow here (a Supabase OAuth app is a separate,
        # organization-level registration).
        "auth_type": "api_key",
        "supported_auth_types": ["api_key"],
        "fields": ["api_key"],
        "extra_fields": {
            "project_ref": "Optional — restrict the integration to one project: its reference (the 20 letters of PROJECT_REF in https://PROJECT_REF.supabase.co) or its URL",
            "db_role": "Optional — a Postgres login role of the project the data is reached as (e.g. agent_reader): its grants and row level security apply, and free SQL is replaced by calls to the allowed functions. The Database access tab lists the roles to choose from",
            "db_password": "The database role's password",
            "db_host": "The database host: the project's pooler (*.pooler.supabase.com) or db.PROJECT_REF.supabase.co. The Database access tab offers the project's hosts",
            "db_port": "5432 (session pooler or direct, default) or 6543 (transaction pooler)",
            "db_name": "The database (default postgres)",
            "db_functions": "The functions agents may call as the role, schema.name separated by commas (e.g. public.get_document). The Database access tab lists those the role may execute",
            "db_ca_cert": "Optional — the project's CA certificate (PEM, Project Settings > Database > SSL Configuration) when the host's certificate is not signed by a public authority",
        },
        "setup_guide": {
            "title": "Supabase Access Token Setup",
            "url": "https://supabase.com/docs/reference/api/introduction",
            "steps": [
                "Sign in to the Supabase dashboard with the account whose projects agents should reach.",
                "Open Account > Access Tokens (https://supabase.com/dashboard/account/tokens) and generate a new token named '{APP_NAME}'.",
                "Paste the token (it starts with 'sbp_') in the 'API Key' field.",
                "Optional: to give agents a single project, paste its reference (Project Settings > General > Project ID, or PROJECT_REF in https://PROJECT_REF.supabase.co) in the 'Project ref' field.",
                "Click 'Test Connection': it lists the projects the token reaches, or checks that it reaches the configured project.",
                "Optional, to reach the data under row level security: create a Postgres login role for the agents (e.g. agent_reader) with EXECUTE on the functions they may call, then open the integration's Database access tab: pick the project, the connection host and the role, type its password, probe, and allow the functions. Test Connection then also logs in as the role.",
            ],
            "permissions": [
                {
                    "scope": "Projects",
                    "description": "List organizations and projects, read a project's details and the health of its services",
                },
                {
                    "scope": "Database",
                    "description": "List and describe tables (columns, constraints, indexes, RLS policies), run read-only SQL; execute SQL and apply migrations with full privileges",
                },
                {
                    "scope": "Advisors & logs",
                    "description": "Read the security and performance advisors and query the project logs (24 hours at a time)",
                },
                {
                    "scope": "Edge Functions, Storage & branches",
                    "description": "List Edge Functions and Storage buckets, read function metadata, list preview branches, generate TypeScript types",
                },
                {
                    "scope": "Database role (optional)",
                    "description": "Log in as a Postgres role of the project to list and call the functions the integration allows, under the role's grants and row level security",
                },
            ],
            "notes": "A personal access token acts as its account on every organization and project that account belongs to; use a dedicated account that is a member of the needed organizations only, or set 'Project ref' to restrict every tool to one project. query_supabase_sql runs as the database's read-only role (Supabase refuses writes), but it reads every schema, auth included. execute_supabase_sql and apply_supabase_migration run with full privileges: withhold them through the agent's tool scoping when the database must stay read-only. Both Management API roles bypass row level security: set a database role to reach the data as that role instead - the three free SQL tools are then withheld and agents call only the allowed functions (list_supabase_functions, call_supabase_function), each result up to 40,000 characters. The database role connects to Postgres directly (TCP 5432 or 6543, TLS verified), so the platform needs that outbound port, and it cannot go through a relay group. Self-hosted Supabase has no Management API and is not supported.",
        },
    },
    "openaev": {
        "name": "OpenAEV",
        "description": "Adversarial Exposure Validation — scenarios, atomic testing, payloads, assets",
        "logo": "/logos/openaev.svg",
        "category": "integration",
        "integration_category": "adversarial_validation",
        "integration_types": ["adversarial_validation"],
        "supports_assignment_webhooks": True,
        # Delivered by a WEBHOOK notifier and LIVE trigger XTM One provisions in
        # OpenAEV (services/openaev_trigger_provisioning.py, trigger_config.provision).
        "available_events": [
            {
                "event": "simulation_finished",
                "label": "Simulation finished",
                "description": "Triggers when a simulation reaches the FINISHED status in OpenAEV.",
                "mechanism": "webhook",
            },
            {
                "event": "atomic_test_finished",
                "label": "Inject or atomic test finished",
                "description": "Triggers when an inject or atomic test ends executed, partial or in error in OpenAEV.",
                "mechanism": "webhook",
            },
            {
                "event": "finding_created",
                "label": "Finding created",
                "description": "Triggers when a new finding is created in OpenAEV.",
                "mechanism": "webhook",
            },
            {
                "event": "finding_updated",
                "label": "Finding updated",
                "description": "Triggers when a finding changes in OpenAEV (assets, occurrences, status).",
                "mechanism": "webhook",
            },
            {
                "event": "finding_deleted",
                "label": "Finding deleted",
                "description": "Triggers when a finding is deleted in OpenAEV.",
                "mechanism": "webhook",
            },
            {
                "event": "asset_created",
                "label": "Asset created",
                "description": "Triggers when an asset (endpoint) is created in OpenAEV.",
                "mechanism": "webhook",
            },
            {
                "event": "asset_updated",
                "label": "Asset updated",
                "description": "Triggers when an asset (endpoint) changes in OpenAEV.",
                "mechanism": "webhook",
            },
            {
                "event": "asset_stale",
                "label": "Asset turned stale",
                "description": (
                    "Triggers when an asset has not been seen in OpenAEV for longer than the tenant's "
                    "stale threshold (off until the threshold is set)."
                ),
                "mechanism": "webhook",
            },
            {
                "event": "vulnerability_created",
                "label": "Vulnerability created",
                "description": "Triggers when a vulnerability is created in OpenAEV.",
                "mechanism": "webhook",
            },
            {
                "event": "scenario_score_degraded",
                "label": "Scenario score degraded",
                "description": "Triggers when the score of a scenario drops in OpenAEV: a control that held no longer does.",
                "mechanism": "webhook",
            },
            {
                "event": "critical_asset_reached",
                "label": "Critical asset reached",
                "description": "Triggers when a finished simulation's attack path reached an asset of high or very high criticality in OpenAEV.",
                "mechanism": "webhook",
            },
        ],
        "auth_type": "api_key",
        "fields": ["api_key"],
        "extra_fields": {
            "base_url": "OpenAEV instance URL (e.g. https://openaev.mycompany.com)",
            "tenant_id": "Tenant ID (optional, multi-tenant OpenAEV only)",
            "webhook_secret": "Webhook secret (needed for OpenAEV events; OpenAEV sends it as X-OpenAEV-Token)",
        },
        # The tenant and the webhook secret are the deployment's choice, not the
        # registration's: an admin sets them on the registered integration too
        # (XTM One mode), or no OpenAEV event could ever be provisioned there.
        "registration_editable_extra_fields": ["tenant_id", "webhook_secret"],
        "setup_guide": {
            "title": "OpenAEV Integration Setup",
            "url": "https://docs.filigran.io/openaev",
            "steps": [
                "Log in to your OpenAEV instance.",
                "Go to your user profile or Settings > Security to find your API token.",
                "Copy the API token and paste it in the 'API Key' field below.",
                "Enter your OpenAEV instance base URL (e.g. https://openaev.mycompany.com) in the 'Base URL' field.",
                "On a multi-tenant OpenAEV, enter the ID of the tenant the agents work in. Leave it empty to use the account's only tenant, or the default tenant when the account belongs to several.",
            ],
            "permissions": [
                {
                    "scope": "Scenarios",
                    "description": "Create, read, update, and delete adversary simulation scenarios",
                },
                {
                    "scope": "Assets/Endpoints",
                    "description": "Read and manage target assets and asset groups",
                },
                {
                    "scope": "Teams & Players",
                    "description": "Read teams and players for scenario targeting",
                },
                {
                    "scope": "Attack Patterns",
                    "description": "Read MITRE ATT&CK techniques and kill chain phases",
                },
                {
                    "scope": "Payloads",
                    "description": "Create and manage command payloads for injects",
                },
                {
                    "scope": "Injector Contracts",
                    "description": "Read available inject types for scenario building",
                },
                {
                    "scope": "Atomic Testing",
                    "description": "Create and execute atomic tests against assets",
                },
                {
                    "scope": "Findings",
                    "description": "Read security findings detected on assets",
                },
                {
                    "scope": "Vulnerabilities",
                    "description": "Look up CVE data in the vulnerability database",
                },
                {
                    "scope": "Exercises",
                    "description": "Read and manage simulation exercises",
                },
            ],
            "notes": "OpenAEV uses a bearer token for API authentication. The token is scoped to the user's roles and permissions. Ensure the user has sufficient privileges to manage scenarios, payloads, and injects.",
        },
    },
    "opencrq": {
        "name": "OpenCRQ",
        "description": "Governance, Risk & Compliance — frameworks, control capabilities, compliance assessments, risk scenarios, CRQ",
        "logo": "/logos/opencrq.svg",
        "category": "integration",
        "integration_category": "grc",
        "integration_types": ["governance_risk_compliance"],
        "auth_type": "api_key",
        "supported_auth_types": ["api_key", "platform_managed"],
        "fields": ["api_key"],
        "extra_fields": {"base_url": "OpenCRQ instance URL (e.g. https://opencrq.mycompany.com)"},
        # Polled from OpenCRQ's job runs (services/opencrq_job_events.py, every 60 s):
        # OpenCRQ has no outbound webhook.
        "available_events": [
            {
                "event": "risk_scenario_recomputed",
                "label": "Risk scenarios recomputed",
                "description": "Triggers when a CRQ simulation or the re-pricing of risk scenarios finishes in OpenCRQ. Uses polling (every minute).",
                "mechanism": "polling",
                "filter_keys": _OPENCRQ_FILTER_KEYS,
            },
            {
                "event": "finding_score_changed",
                "label": "Finding scores recomputed",
                "description": "Triggers when OpenCRQ finishes recomputing finding scores. Uses polling (every minute).",
                "mechanism": "polling",
                "filter_keys": _OPENCRQ_FILTER_KEYS,
            },
            {
                "event": "control_validation_ingested",
                "label": "Control validation ingested",
                "description": "Triggers when OpenCRQ finishes ingesting OpenAEV results or recording the proven posture. Uses polling (every minute).",
                "mechanism": "polling",
                "filter_keys": _OPENCRQ_FILTER_KEYS,
            },
        ],
        "setup_guide": {
            "title": "OpenCRQ Integration Setup",
            "url": "https://docs.filigran.io/opencrq",
            "steps": [
                "The recommended setup is inbound registration: add the XTM One URL and registration token to your OpenCRQ configuration (xtm_one.url / xtm_one.token) - the integration then appears here automatically with per-user authentication.",
                "For a manual (service-account) setup instead: log in to your OpenCRQ instance.",
                "Go to Settings > Security > Service Accounts and create a service account with the capabilities the agent needs.",
                "Generate a token (ogrc_...) for the service account and paste it in the 'API Key' field below.",
                "Enter your OpenCRQ instance base URL (e.g. https://opencrq.mycompany.com) in the 'Base URL' field.",
            ],
            "permissions": [
                {
                    "scope": "Frameworks & Controls",
                    "description": "Read compliance frameworks (assessment catalog and Drata mirror) with their requirements and Drata controls, and control capabilities; declare a control capability's assessment (strength and coverage) and, per organization scope, its posture; add, remove, restore or reset the ATT&CK techniques it covers; read its assessment history",
                },
                {
                    "scope": "Assessments",
                    "description": "Create and manage compliance assessments (rename, publish, reopen) and answer their requirements on a level from 0 to 5 or not applicable",
                },
                {
                    "scope": "Risk & Attack Outcomes",
                    "description": "Full risk scenario lifecycle (create/update/delete), risk tolerance for a scenario or an organization, exposure analysis, attack outcomes with MITRE techniques, threat relevance settings",
                },
                {
                    "scope": "Cyber Risk Quantification",
                    "description": "Run Monte Carlo simulations, read runs and what-if results, manage TEF inputs and loss estimates, read loss drivers",
                },
                {
                    "scope": "Assets",
                    "description": "Full primary/supporting asset lifecycle (every field, including subcategory, tags and properties; reset to connector values), business value categories, dependency links with their roles, and vulnerability evidence (add, change state, delete, reset to the connector)",
                },
                {
                    "scope": "Vulnerabilities & MITRE",
                    "description": "Browse and search the CVE knowledge base (NIST NVD, FIRST EPSS, CISA KEV), list the tenant's vulnerability findings, and read MITRE ATT&CK reference data",
                },
                {
                    "scope": "Findings & Threats",
                    "description": "Read vulnerability findings, top vulnerabilities, asset posture, attack simulation results and proven control posture; browse the threat catalog, threats relevant to a risk scenario and threats exploiting a CVE",
                },
                {
                    "scope": "Risk Treatment",
                    "description": "Read risk treatment plans and budget reviews with their simulated loss reduction and return, and the risk dashboard",
                },
                {
                    "scope": "Connectors",
                    "description": "Read connector health, schedules and recent syncs",
                },
                {
                    "scope": "Administration",
                    "description": "Manage users, tenants, organizations (including containment time, estate mode and asset mapping), groups, roles, capabilities, and invitations (service-account tokens are never exposed)",
                },
                {
                    "scope": "Background Jobs",
                    "description": "Track, cancel, and retry background jobs (CRQ simulations, CVE syncs)",
                },
            ],
            "notes": "OpenCRQ is multi-tenant: every tool call is scoped by a tenant. The tools resolve the tenant automatically when the account has a single tenant; otherwise pass tenant_id explicitly (list_opencrq_tenants discovers them). Administrative writes additionally require the matching OpenCRQ capability (e.g. Manage Security, Create Tenants) on the calling account.",
        },
    },
    "xtm_hub": {
        "name": "XTM Hub",
        "description": (
            "XTM Hub, the Filigran ecosystem hub - search and read the "
            "ready-to-use resources it publishes for the XTM Platform: OpenCTI "
            "custom dashboards, custom views, playbooks and integrations "
            "(connectors, CSV / TAXII / RSS feeds, live streams, third-party "
            "integrations) and OpenAEV scenarios, filtered by use case and "
            "compatible version, with the Hub page that deploys each one"
        ),
        "logo": "/logos/xtm_hub.svg",
        "category": "integration",
        "integration_category": "threat_intelligence",
        "integration_types": ["marketplace", "threat_intel", "adversarial_validation"],
        "available_events": [],
        # The public catalog needs no credential ("none": the only field is
        # the Hub's address, optional - empty means XTM_HUB_DEFAULT_URL -
        # so the integration is connected at creation time). A personal XTM
        # Hub API token ("api_key", xhp_..., made on the Hub under Profile >
        # API tokens) adds what its user reaches in ONE organization: private
        # libraries, registered platforms, downloads. The tool set is the same
        # in both modes; a tool that needs the token says so.
        "auth_type": "none",
        "supported_auth_types": ["none", "api_key"],
        # The token acts only on the Hub it was made on: a new base_url (or a
        # switch to "none") without a new key forgets it
        # (integration_state.forget_key_bound_elsewhere). Empty is the default Hub.
        "api_key_bound_to": {"field": "base_url", "default": settings.xtm_hub_default_url},
        "extra_fields_optional": True,
        "fields": ["api_key"],
        "extra_fields": {
            "base_url": (f"XTM Hub URL (default: {settings.xtm_hub_default_url} - set it only for another XTM Hub)"),
        },
        "setup_guide": {
            "title": "XTM Hub Setup",
            "url": "https://docs.hub.filigran.io",
            "steps": [
                f"Leave the XTM Hub URL empty to use the default XTM Hub ({settings.xtm_hub_default_url}), or set the address of another XTM Hub.",
                "Public catalog only: keep the authentication mode 'None'. No account is needed.",
                "Private libraries, registered platforms and downloads: on the XTM Hub, open Profile > API tokens, create a token for the organization agents should act in (read only is enough for these tools), switch the authentication mode to 'API Key' and paste the token (xhp_...). It is shown once on the Hub.",
                "Click 'Test Connection': it checks that the XTM Hub answers and, with a token, names the user and organization the token acts as.",
                "Bind the integration to an agent (agent > Integrations) to give it the XTM Hub tools.",
            ],
            "permissions": [
                {
                    "scope": "Public resource catalog (read-only, no token)",
                    "description": (
                        "Search and read the resources published on the XTM "
                        "Hub - OpenCTI dashboards, views, playbooks and "
                        "integrations, OpenAEV scenarios - and the use cases "
                        "they are tagged with"
                    ),
                },
                {
                    "scope": "Organization libraries and platforms (API token)",
                    "description": (
                        "As the token's user, in the token's organization: "
                        "who the token acts as, the libraries the user reaches "
                        "(private ones included) and their resources, the "
                        "registered platforms, and downloading a resource file "
                        "(asks for approval once tool approvals are on)"
                    ),
                },
            ],
            "notes": (
                "Resources are published by the Filigran community, so the "
                "agent treats their descriptions as data, never as "
                "instructions. An API token acts as its user in one "
                "organization, never with platform administration rights, and "
                "never signs in, manages tokens or changes who can access what. "
                "It is sent over HTTPS only (or to an XTM Hub on this machine) "
                "and belongs to the XTM Hub it was made on: changing the XTM "
                "Hub URL forgets it. A token shared with a group acts as its "
                "owner for every member: prefer a read only token there."
            ),
        },
    },
    "picus": {
        "name": "Picus Security",
        "description": "Breach and Attack Simulation / Security Validation — run and read simulations (prevention + detection analytics), threat library, templates, agents, and vendor prevention/detection mitigation recommendations",
        "logo": "/logos/picus.svg",
        "category": "integration",
        "integration_category": "adversarial_validation",
        "integration_types": ["adversarial_validation", "threat_intel"],
        # Picus authenticates with a refresh token exchanged for a short-lived
        # access token — there is no OAuth app, so the only supported auth mode
        # is "api_key" (the refresh token).
        "auth_type": "api_key",
        "supported_auth_types": ["api_key"],
        "fields": ["api_key"],
        "extra_fields": {
            "base_url": "Picus manager base URL (SaaS default https://api.picussecurity.com; on-prem/regional managers differ)",
        },
        "setup_guide": {
            "title": "Picus Security REST API Token Setup",
            "url": "https://apidocs.picussecurity.com/docs/authentication-method",
            "steps": [
                "Log in to your Picus platform (e.g. app.picussecurity.com).",
                "Go to Settings > Rest API Token.",
                "Click Generate Token, give it a name/description and expiration (refresh tokens are valid up to ~6 months), and select the scope of endpoints the token can access.",
                "Copy the generated refresh token — it is shown only once. Paste it into the API Token field below.",
                "Set the Base URL to your Picus manager API host (SaaS: https://api.picussecurity.com; on-prem/regional managers use your own host).",
                "Click Test Connection to verify — XTM One exchanges the refresh token for a short-lived access token (valid ~2 hours) on every call.",
            ],
            "permissions": [
                {
                    "scope": "Simulations (read)",
                    "description": "List simulations, read details and run history, prevention/detection results, and threat/objective/action results",
                },
                {
                    "scope": "Simulations (write)",
                    "description": "Create and schedule simulations, re-run (simulate now), update, cancel, and delete",
                },
                {
                    "scope": "Threat Library (read)",
                    "description": "Browse threats, attack actions, threat actors, templates, and tags",
                },
                {
                    "scope": "Agents & Integrations (read)",
                    "description": "List simulation agents and detection integrations with health/status",
                },
                {
                    "scope": "Mitigations (read)",
                    "description": "Prevention devices + block stats, vendor signatures, not-blocked-action prevention recommendations, and detection-content rules",
                },
                {
                    "scope": "Summary & Activity (read)",
                    "description": "Account-wide prevention/detection posture summary and activity logs",
                },
            ],
            "notes": (
                "Picus uses an OAuth2 refresh/access token model: the refresh "
                "token (generated in the Picus app under Settings > Rest API "
                "Token, valid ~6 months) is exchanged for a short-lived access "
                "token (~2 hours) via POST /v1/auth/token, which XTM One mints "
                "and caches automatically. The refresh token's scope is chosen "
                "when it is generated, so scope it to only the endpoints the "
                "agent needs. The base URL is the Picus manager API host "
                "(SaaS default https://api.picussecurity.com). Available "
                "endpoints and rate are bounded by your Picus license."
            ),
        },
    },
    "opencti": {
        "name": "OpenCTI",
        "description": "Threat intelligence platform — indicators, reports, observables, incidents",
        "logo": "/logos/opencti.svg",
        "category": "integration",
        "integration_category": "threat_intelligence",
        "integration_types": ["threat_intel"],
        #: The route that opens any entity by its id, appended to the integration URL.
        "entity_link_route": "/dashboard/id/{id}",
        "supports_assignment_webhooks": True,
        "supported_auth_types": ["api_key", "platform_managed"],
        "available_events": [
            {
                "event": "create",
                "label": "Entity / relationship created",
                "description": "Triggers when a new entity (indicator, report, incident, observable, etc.) or relationship is created in OpenCTI.",
                "mechanism": "webhook",
            },
            {
                "event": "update",
                "label": "Entity / relationship updated",
                "description": "Triggers when an entity or relationship is updated in OpenCTI.",
                "mechanism": "webhook",
            },
            {
                "event": "delete",
                "label": "Entity / relationship deleted",
                "description": "Triggers when an entity or relationship is deleted in OpenCTI.",
                "mechanism": "webhook",
            },
        ],
        "auth_type": "api_key",
        "fields": ["api_key"],
        "extra_fields": {
            "base_url": "OpenCTI instance URL (e.g. https://opencti.mycompany.com)",
            "webhook_secret": "Webhook secret (needed for OpenCTI events; OpenCTI sends it as X-OpenCTI-Token)",
            "live_writes": "Live writes (knowledge changes skip the draft and its human review)",
        },
        # Off unless an admin picks "on": agents then write knowledge to the live
        # graph instead of a draft, and may validate drafts.
        "extra_field_options": {
            "live_writes": [
                {"value": "off", "label": "Off: changes are staged in a draft for review (default)"},
                {"value": "on", "label": "On: changes go to the live graph, no review"},
            ],
        },
        # The webhook secret and Live writes are the deployment's choice: an admin
        # sets them on the registered integration (XTM One mode), so OpenCTI
        # triggers can be provisioned and Live writes turned on there too.
        "registration_editable_extra_fields": ["webhook_secret", "live_writes"],
        "setup_guide": {
            "title": "OpenCTI Integration Setup",
            "url": "https://docs.opencti.io/latest/deployment/integrations/#api-authentication",
            "steps": [
                "Log in to your OpenCTI instance as an administrator.",
                "Go to Settings > Security > Users & Groups, and select your user (or create a dedicated API user).",
                "Copy the API Token from the user profile. This token authenticates all API calls.",
                "Paste the API Token in the 'API Key' field below.",
                "Enter your OpenCTI instance base URL (e.g. https://opencti.mycompany.com) in the 'Base URL' field.",
                "To receive real-time notifications, create a Webhook notifier in OpenCTI:",
                "  - Go to Settings > Customization > Notifiers and create a new Custom Webhook notifier.",
                "  - Each assignment displays a dedicated webhook URL — use it to target a single assignment.",
                "  - Alternatively, use the integration-level webhook URL to fan out events to all matching assignments.",
                "  - Configure triggers and filters in OpenCTI to control which entities and relationships trigger notifications.",
            ],
            "permissions": [
                {
                    "scope": "Read Knowledge",
                    "description": "Read STIX objects (indicators, reports, malware, etc.)",
                },
                {"scope": "Read Labels", "description": "Read labels and markings"},
                {
                    "scope": "Read Connectors",
                    "description": "Read connector and stream status",
                },
            ],
            "notes": "OpenCTI uses a bearer token for API authentication. The token is scoped to the user's roles and permissions. For webhook triggers, OpenCTI sends JSON payloads with STIX 2.1 bundles — the webhook receiver parses them automatically. Use trigger_config.events to filter by event type (create, update, delete). OpenCTI supports per-assignment webhook URLs: you can create as many Webhook notifiers in OpenCTI as needed, each pointing to a different assignment's dedicated URL.",
        },
    },
    "intune": {
        "name": "Microsoft Intune",
        "description": "Endpoint management — devices, compliance, configuration profiles, security posture",
        "logo": "/logos/intune.svg",
        "category": "integration",
        "integration_category": "identity_access",
        "integration_types": ["endpoint_management", "compliance"],
        "auth_type": "oauth",
        "fields": ["client_id", "client_secret"],
        "scopes": (
            "openid profile email offline_access "
            "DeviceManagementManagedDevices.Read.All "
            "DeviceManagementManagedDevices.ReadWrite.All "
            "DeviceManagementManagedDevices.PrivilegedOperations.All "
            "DeviceManagementConfiguration.Read.All "
            "DeviceManagementConfiguration.ReadWrite.All "
            "DeviceManagementApps.Read.All "
            "DeviceManagementApps.ReadWrite.All "
            "DeviceManagementScripts.ReadWrite.All "
            "DeviceManagementServiceConfig.Read.All "
            "DeviceManagementRBAC.Read.All"
        ),
        "authorize_url": "https://login.microsoftonline.com/common/oauth2/v2.0/authorize",
        "token_url": "https://login.microsoftonline.com/common/oauth2/v2.0/token",
        "extra_fields": {"tenant_id": "Azure AD tenant ID (optional, for single-tenant apps)"},
        "setup_guide": {
            "title": "Microsoft Intune Integration Setup",
            "url": "https://entra.microsoft.com/#view/Microsoft_AAD_RegisteredApps/ApplicationsListBlade",
            "steps": [
                "Go to Microsoft Entra admin center > App registrations > New registration.",
                "Name: '{APP_NAME} - Intune' (or any name you prefer).",
                "Supported account types: choose 'Single tenant' for your organization.",
                "Redirect URI: select 'Web' and enter {base_url}/api/oauth/callback",
                "After creating, copy the Application (client) ID.",
                "Go to 'Certificates & secrets' > 'New client secret'. Copy the secret value immediately.",
                "Go to 'API permissions' > 'Add a permission' > 'Microsoft Graph' > 'Delegated permissions'.",
                "Add each Intune permission listed below.",
                "Click 'Grant admin consent' — Intune permissions require tenant admin consent.",
                "Ensure the tenant has an active Microsoft Intune license.",
            ],
            "permissions": [
                {
                    "scope": "DeviceManagementManagedDevices.Read.All",
                    "description": "Read managed device properties (name, OS, compliance state)",
                },
                {
                    "scope": "DeviceManagementManagedDevices.ReadWrite.All",
                    "description": "Read and write managed device properties",
                },
                {
                    "scope": "DeviceManagementManagedDevices.PrivilegedOperations.All",
                    "description": "Remote actions: lock, wipe, reboot, retire, sync",
                },
                {
                    "scope": "DeviceManagementConfiguration.Read.All",
                    "description": "Read compliance policies and configuration profiles",
                },
                {
                    "scope": "DeviceManagementConfiguration.ReadWrite.All",
                    "description": "Read and write compliance policies and configuration profiles",
                },
                {
                    "scope": "DeviceManagementApps.Read.All",
                    "description": "Read detected applications and app configurations",
                },
                {
                    "scope": "DeviceManagementApps.ReadWrite.All",
                    "description": "Assign apps to groups and read their install status",
                },
                {
                    "scope": "DeviceManagementScripts.ReadWrite.All",
                    "description": "Create, assign and run device health scripts (remediations) and macOS shell scripts",
                },
                {
                    "scope": "DeviceManagementServiceConfig.Read.All",
                    "description": "Read Intune service configuration (device categories, RBAC)",
                },
                {
                    "scope": "DeviceManagementRBAC.Read.All",
                    "description": "Read Intune role-based access control assignments",
                },
            ],
            "notes": "Microsoft Intune requires an active Intune license on the tenant. All DeviceManagement* permissions require admin consent. The integration uses delegated OAuth — the signed-in user must have appropriate Intune roles (e.g. Intune Administrator or Endpoint Security Manager) for write/action operations.",
        },
    },
    "sentinel": {
        "name": "Microsoft Sentinel",
        "description": "Cloud-native SIEM — incidents, alerts, threat intelligence, KQL hunting, watchlists, analytics rules",
        "logo": "/logos/sentinel.svg",
        "category": "integration",
        "integration_category": "siem_analytics",
        "integration_types": ["siem", "threat_intel"],
        # Delegated OAuth (sign-in) is the default; app-only client credentials
        # (service principal) remains available via the "API Key" auth mode and
        # is recommended for fully-unattended assignments.
        "auth_type": "oauth",
        "supported_auth_types": ["oauth", "api_key"],
        "fields": ["client_id", "client_secret"],
        # Single-resource (Azure Resource Manager) scope + OIDC scopes. The
        # Log Analytics audience (api.loganalytics.io) is acquired at runtime by
        # redeeming the refresh token, so it must NOT be mixed in here (Azure AD
        # v2 rejects multi-resource token requests).
        "scopes": ("openid profile email offline_access https://management.azure.com/user_impersonation"),
        "authorize_url": "https://login.microsoftonline.com/common/oauth2/v2.0/authorize",
        "token_url": "https://login.microsoftonline.com/common/oauth2/v2.0/token",
        "extra_fields": {
            "tenant_id": "Azure AD Tenant ID (required)",
            "subscription_id": "Azure Subscription ID",
            "resource_group": "Resource Group name containing the Sentinel workspace",
            "workspace_name": "Log Analytics Workspace name",
        },
        "setup_guide": {
            "title": "Microsoft Sentinel Integration Setup",
            "url": "https://learn.microsoft.com/en-us/azure/sentinel/",
            "steps": [
                "In Microsoft Entra admin center, go to 'App registrations' and create a new registration (or reuse your existing Microsoft app).",
                "Redirect URI: select 'Web' and enter {base_url}/api/oauth/callback (required for the OAuth sign-in mode).",
                "Copy the Application (client) ID and Directory (tenant) ID from the Overview page; paste the Tenant ID below.",
                "Under 'Certificates & secrets', create a new client secret and copy its value into the Client Secret field below.",
                "OAuth (sign-in) mode: under 'API permissions' > 'Add a permission' > 'Azure Service Management' > 'Delegated permissions', add 'user_impersonation', then add 'Log Analytics API' > 'Delegated permissions' > 'Data.Read'. Click 'Grant admin consent'.",
                "App-only (API Key) mode: skip the delegated permissions — instead assign IAM roles to the app registration (next steps).",
                "Go to the Resource Group containing your Sentinel workspace > 'Access control (IAM)' and assign 'Microsoft Sentinel Responder' (or Contributor) and 'Log Analytics Reader' — to the signed-in user (OAuth mode) and/or the app registration (app-only mode).",
                "Fill in Tenant ID, Subscription ID, Resource Group, and Workspace Name below.",
                "Choose the authentication mode: 'OAuth' to sign in as a user (recommended for consistency with Intune/Microsoft), or 'API Key' for app-only client credentials (recommended for fully-unattended assignments).",
            ],
            "permissions": [
                {
                    "scope": "Azure Service Management — user_impersonation (Delegated, OAuth mode)",
                    "description": "Access the Azure Resource Manager API as the signed-in user to read and manage incidents, indicators, watchlists, and automation rules",
                },
                {
                    "scope": "Log Analytics API — Data.Read (Delegated, OAuth mode)",
                    "description": "Run KQL hunting queries against the Log Analytics workspace as the signed-in user",
                },
                {
                    "scope": "Microsoft Sentinel Responder / Contributor (IAM role)",
                    "description": "Read incidents and manage incident lifecycle (Responder) or full read/write (Contributor) — assigned to the user (OAuth) or app (app-only)",
                },
                {
                    "scope": "Log Analytics Reader (IAM role)",
                    "description": "Run KQL queries against the Log Analytics workspace for threat hunting",
                },
            ],
            "notes": (
                "Two authentication modes are supported. OAuth (sign-in) acts as the "
                "signed-in user and requires the 'user_impersonation' (Azure Service "
                "Management) and 'Data.Read' (Log Analytics) delegated permissions plus "
                "the relevant Azure RBAC roles on the workspace for that user. App-only "
                "(API Key) uses client credentials (service principal) and only needs the "
                "IAM role assignments on the Resource Group — it is the most robust choice "
                "for autonomous assignments because no user session can expire. Sentinel "
                "uses the Azure Resource Manager + Log Analytics APIs (not Microsoft Graph)."
            ),
        },
    },
    "defender": {
        "name": "Microsoft Defender XDR",
        "description": "Defender XDR — incidents, alerts, advanced hunting (KQL), custom detection rules, threat intelligence, IOCs, device response actions, vulnerability management, secure score",
        "logo": "/logos/defender.svg",
        "category": "integration",
        "integration_category": "detection_response",
        # Also a SIEM (#1373): advanced hunting over the unified Defender and
        # Sentinel tables, so it fills a fleet's SIEM capability beside Sentinel.
        "integration_types": ["xdr", "edr", "siem", "threat_intel", "soar"],
        # Delegated OAuth (sign-in) is the default; app-only application
        # permissions remain available via the "API Key" auth mode and are
        # recommended for fully-unattended assignments.
        "auth_type": "oauth",
        "supported_auth_types": ["oauth", "api_key"],
        "fields": ["client_id", "client_secret"],
        # Single-resource (Microsoft Graph) scopes + OIDC scopes. The Defender
        # for Endpoint audience (api.securitycenter.microsoft.com) is acquired at
        # runtime by redeeming the refresh token, so its scopes must NOT be mixed
        # in here (Azure AD v2 rejects multi-resource token requests).
        "scopes": (
            "openid profile email offline_access "
            "SecurityIncident.ReadWrite.All SecurityAlert.ReadWrite.All "
            "ThreatHunting.Read.All ThreatIntelligence.Read.All "
            "SecurityEvents.Read.All"
        ),
        "authorize_url": "https://login.microsoftonline.com/common/oauth2/v2.0/authorize",
        "token_url": "https://login.microsoftonline.com/common/oauth2/v2.0/token",
        "extra_fields": {
            "tenant_id": "Azure AD Tenant ID (required)",
        },
        "setup_guide": {
            "title": "Microsoft Defender XDR Integration Setup",
            "url": "https://learn.microsoft.com/en-us/graph/api/resources/security-api-overview",
            "steps": [
                "In Microsoft Entra admin center, go to 'App registrations' and create a new registration (or reuse your existing Microsoft app).",
                "Redirect URI: select 'Web' and enter {base_url}/api/oauth/callback (required for the OAuth sign-in mode).",
                "Copy the Application (client) ID and Directory (tenant) ID from the Overview page; paste the Tenant ID below.",
                "Under 'Certificates & secrets', create a new client secret and copy its value into the Client Secret field below.",
                "Go to 'API permissions' > 'Add a permission' > 'Microsoft Graph' and add the Graph permissions listed below — choose 'Delegated permissions' for OAuth (sign-in) mode, or 'Application permissions' for app-only (API Key) mode.",
                "Add a permission > 'APIs my organization uses' > 'WindowsDefenderATP' and add the Defender for Endpoint permissions below — again 'Delegated' for OAuth or 'Application' for app-only.",
                "Click 'Grant admin consent' — both delegated and application permissions for these security APIs require tenant admin consent.",
                "Choose the authentication mode: 'OAuth' to sign in as a user (recommended for consistency with Intune/Microsoft), or 'API Key' for app-only client credentials (recommended for fully-unattended assignments).",
                "OAuth (sign-in) mode: the signed-in user must hold a Microsoft Entra security role (e.g. Security Reader / Security Operator / Security Administrator) and a Defender for Endpoint role with device-group access for response actions.",
                "Threat Intelligence (articles, intel profiles, host enrichment, vulnerability intel) requires an active Microsoft Defender Threat Intelligence license.",
            ],
            "permissions": [
                {
                    "scope": "SecurityIncident.ReadWrite.All (Graph)",
                    "description": "Read and manage Defender XDR incidents and comments",
                },
                {
                    "scope": "SecurityAlert.ReadWrite.All (Graph)",
                    "description": "Read and manage Defender XDR alerts (alerts_v2) and comments",
                },
                {
                    "scope": "ThreatHunting.Read.All (Graph)",
                    "description": "Run advanced hunting (KQL) queries across Defender XDR tables",
                },
                {
                    "scope": "ThreatIntelligence.Read.All (Graph)",
                    "description": "Read Defender TI articles, intel profiles, host enrichment, and vulnerability intel (requires Defender TI license)",
                },
                {
                    "scope": "SecurityEvents.Read.All (Graph)",
                    "description": "Read Microsoft Secure Score and control profiles",
                },
                {
                    "scope": "CustomDetection.ReadWrite.All (Graph)",
                    "description": "List, create, update and delete custom detection rules (CustomDetection.Read.All is enough to list and get them); grant admin consent after adding it",
                },
                {
                    "scope": "Machine.Read.All (WindowsDefenderATP)",
                    "description": "Read device inventory, vulnerabilities, and logged-on users",
                },
                {
                    "scope": "Machine.Isolate (WindowsDefenderATP)",
                    "description": "Isolate / release devices from the network",
                },
                {
                    "scope": "Machine.Scan (WindowsDefenderATP)",
                    "description": "Run Microsoft Defender Antivirus scans on devices",
                },
                {
                    "scope": "Machine.CollectForensics (WindowsDefenderATP)",
                    "description": "Collect forensic investigation packages from devices, and in OAuth mode download them",
                },
                {
                    "scope": "Machine.ReadWrite.All (WindowsDefenderATP, Application - API Key mode)",
                    "description": "Download a collected investigation package in app-only mode (Get package SAS URI); grant admin consent after adding it",
                },
                {
                    "scope": "Machine.RestrictExecution (WindowsDefenderATP)",
                    "description": "Restrict / unrestrict application execution on devices",
                },
                {
                    "scope": "Machine.StopAndQuarantine (WindowsDefenderATP)",
                    "description": "Stop and quarantine malicious files on devices",
                },
                {
                    "scope": "Ti.ReadWrite.All (WindowsDefenderATP)",
                    "description": "List, create, and delete custom threat indicators (IOCs)",
                },
                {
                    "scope": "Vulnerability.Read.All (WindowsDefenderATP)",
                    "description": "Read vulnerabilities, software inventory, recommendations, and exposure/secure scores",
                },
            ],
            "notes": (
                "Microsoft Defender XDR calls two APIs with one Entra app: Microsoft "
                "Graph (incidents, alerts, advanced hunting, threat intelligence, secure "
                "score) and the Defender for Endpoint API (device response actions, IOCs, "
                "vulnerability management, entity stats). Both delegated and application "
                "permission types are supported for every tool. OAuth (sign-in) acts as the "
                "signed-in user and is bounded by that user's Defender RBAC role and device "
                "groups; app-only (API Key) uses client credentials for tenant-wide, "
                "fully-unattended access and never depends on a user session. Tokens for each "
                "audience are acquired automatically — for OAuth by redeeming the refresh "
                "token per audience, for app-only by client credentials. Grant admin consent "
                "for the chosen permission set."
            ),
        },
    },
    "entra": {
        "name": "Microsoft Entra ID",
        "description": "Identity & access management (Azure AD) — users, groups, RBAC roles, app & credential hygiene, sign-in/audit logs, and Identity Protection (risky users)",
        "logo": "/logos/entra.svg",
        "category": "integration",
        "integration_category": "identity_access",
        "integration_types": ["identity", "iam", "directory"],
        # Delegated OAuth (sign-in) is the default; app-only application
        # permissions remain available via the "API Key" auth mode and are
        # recommended for fully-unattended IAM automation (offboarding,
        # access reviews, scheduled audits).
        "auth_type": "oauth",
        "supported_auth_types": ["oauth", "api_key"],
        "fields": ["client_id", "client_secret"],
        # Single-resource (Microsoft Graph) delegated scopes + OIDC scopes.
        "scopes": (
            "openid profile email offline_access "
            "User.Read.All User.ReadWrite.All User.EnableDisableAccount.All "
            "User.RevokeSessions.All UserAuthenticationMethod.ReadWrite.All "
            "Group.ReadWrite.All GroupMember.ReadWrite.All Directory.Read.All "
            "RoleManagement.ReadWrite.Directory Application.Read.All "
            "Device.Read.All AuditLog.Read.All IdentityRiskyUser.ReadWrite.All "
            "IdentityRiskEvent.Read.All Policy.Read.All Organization.Read.All"
        ),
        "authorize_url": "https://login.microsoftonline.com/common/oauth2/v2.0/authorize",
        "token_url": "https://login.microsoftonline.com/common/oauth2/v2.0/token",
        "extra_fields": {
            "tenant_id": "Azure AD Tenant ID (required for app-only / API Key mode; recommended for single-tenant OAuth apps)",
        },
        "setup_guide": {
            "title": "Microsoft Entra ID Integration Setup",
            "url": "https://learn.microsoft.com/en-us/graph/api/resources/azure-ad-overview",
            "steps": [
                "In the Microsoft Entra admin center, go to 'App registrations' and create a new registration (or reuse your existing Microsoft app).",
                "Redirect URI: select 'Web' and enter {base_url}/api/oauth/callback (required for the OAuth sign-in mode).",
                "Copy the Application (client) ID and Directory (tenant) ID from the Overview page; paste the Tenant ID below.",
                "Under 'Certificates & secrets', create a new client secret and copy its value into the Client Secret field below.",
                "Go to 'API permissions' > 'Add a permission' > 'Microsoft Graph' and add the permissions below — choose 'Delegated permissions' for OAuth (sign-in) mode, or 'Application permissions' for app-only (API Key) mode.",
                "Click 'Grant admin consent' — the directory read/write, role management, and Identity Protection permissions all require tenant admin consent.",
                "Choose the authentication mode: 'OAuth' to act as the signed-in administrator, or 'API Key' for app-only client credentials (recommended for unattended assignments such as scheduled access reviews or offboarding).",
                "OAuth (sign-in) mode: the signed-in user must hold the Entra roles needed for write actions (e.g. User Administrator, Groups Administrator, Privileged Role Administrator, Security Administrator).",
                "Identity Protection (risky users / risk detections) and user sign-in activity require a Microsoft Entra ID P1/P2 license.",
            ],
            "permissions": [
                {
                    "scope": "User.ReadWrite.All",
                    "description": "Read and manage all users (lifecycle, profile updates, create/delete)",
                },
                {
                    "scope": "User.EnableDisableAccount.All",
                    "description": "Enable or disable user accounts (offboarding)",
                },
                {
                    "scope": "User.RevokeSessions.All",
                    "description": "Revoke a user's refresh tokens and sessions",
                },
                {
                    "scope": "UserAuthenticationMethod.ReadWrite.All",
                    "description": "Reset user passwords (administrative reset)",
                },
                {
                    "scope": "Group.ReadWrite.All",
                    "description": "Read and manage groups (create/update/delete, dynamic rules)",
                },
                {
                    "scope": "GroupMember.ReadWrite.All",
                    "description": "Manage group membership (add/remove members)",
                },
                {
                    "scope": "RoleManagement.ReadWrite.Directory",
                    "description": "Read and manage directory (RBAC) role assignments — privileged access governance",
                },
                {
                    "scope": "Application.Read.All",
                    "description": "Read app registrations, service principals, credentials, and OAuth2 consent grants",
                },
                {
                    "scope": "Directory.Read.All",
                    "description": "Read directory objects (devices, organization, domains, license SKUs)",
                },
                {
                    "scope": "AuditLog.Read.All",
                    "description": "Read sign-in logs and directory audit logs (requires Entra ID P1/P2)",
                },
                {
                    "scope": "IdentityRiskyUser.ReadWrite.All",
                    "description": "Read risky users and confirm-compromised / dismiss them (requires Entra ID P2)",
                },
                {
                    "scope": "IdentityRiskEvent.Read.All",
                    "description": "Read Identity Protection risk detections (requires Entra ID P2)",
                },
                {
                    "scope": "Policy.Read.All",
                    "description": "Read Conditional Access policies and named locations",
                },
            ],
            "notes": (
                "Microsoft Entra ID is Microsoft's identity & access management "
                "platform, driven through the Microsoft Graph API. Both delegated "
                "and application permission types are supported for every tool. "
                "OAuth (sign-in) acts as the signed-in administrator and is bounded "
                "by that user's Entra roles; app-only (API Key) uses client "
                "credentials for tenant-wide, fully-unattended access (the best "
                "choice for scheduled access reviews, offboarding, and audits) and "
                "never depends on a user session. Grant admin consent for the chosen "
                "permission set. Identity Protection and sign-in activity require an "
                "Entra ID P1/P2 license."
            ),
        },
    },
    # Inbound trust configuration, not an outbound tool integration: it holds
    # the three facts (see ``app.a2a.federation``) that let a partner agent's
    # own Entra tokens authenticate on this platform's A2A task surface. It
    # contributes NO tools — binding it to an agent (the ordinary Tools tab,
    # like any other integration) means exactly "this agent's A2A exposure
    # admits this partner's Entra tokens". Lives in the catalog beside
    # Microsoft Entra ID so one partner relationship is configured once and
    # bound to any number of agents. Listed/creatable on Enterprise Edition
    # — the A2A licence gate, enforced in ``api.v1.integrations``.
    # The key mirrors ``app.a2a.providers.ENTRA_A2A_PROVIDER``.
    # --- direction-bearing A2A entries ------------------------------------
    # ``a2a_directions`` lists which directions the mechanism supports; the
    # ``outbound`` variant says what that direction asks for and how it is set
    # up, the base entry being inbound.
    # The directions genuinely differ — inbound OAuth asks for nothing
    # because the platform MINTS the secret, while outbound needs a card
    # location and the partner's own credentials — so a flat field list would
    # show an operator fields that do nothing and make a required-field check
    # meaningless.
    "google_a2a": {
        "name": "Google Agent Platform A2A",
        "description": (
            "A partner relationship with Google Agent Platform (Vertex AI "
            "Agent Runtime), in either direction. Outbound: supply the "
            "partner's card location and a service account key, and this "
            "platform exchanges it for a cloud-platform-scoped token on every "
            "call. Inbound: pin the audience and the one service account "
            "allowed to call, and a Google-side agent tasks your agents with "
            "its own Google-issued token — no secret stored on either side."
        ),
        "logo": "/logos/google.svg",
        "category": "integration",
        "integration_category": "identity_access",
        "integration_types": ["identity", "a2a"],
        "auth_type": "none",
        "fields": [],
        # BOTH directions. The key exists at all because neither sibling's
        # credential flow reaches Vertex — outbound OAuth presents a
        # client-credentials bearer, and Vertex requires a signed
        # service-account assertion. On why inbound is not redundant with
        # ``oauth_a2a`` inbound, see ``integrations/providers/google_a2a.py``.
        "a2a_directions": ["inbound", "outbound"],
        "variant_selector": "a2a_direction",
        "variants": {
            "outbound": {
                "extra_fields": {
                    "card_urls": (
                        "Partner agent card URLs — the exact location each agent "
                        "publishes its card at, separated by commas or new lines. "
                        "One partner may expose several agents; each becomes its "
                        "own delegation tool (required)"
                    ),
                    # A CHOICE, not two fields one of which must be left blank.
                    # Both filled in is refused rather than ranked, so an operator
                    # who did not know that met a refusal instead of a form.
                    "credential_mode": "How this platform authenticates to the partner",
                    "impersonate_service_account": (
                        "The partner's service account to impersonate, e.g. "
                        "agent-caller@their-project.iam.gserviceaccount.com. "
                        "Nothing is stored: this platform presents its OWN Google "
                        "identity to borrow that account, and the partner revokes "
                        "by removing roles/iam.serviceAccountTokenCreator. Needs "
                        "this deployment to run with Google Application Default "
                        "Credentials (required)"
                    ),
                    "service_account_json": (
                        "The partner's service account key JSON, used to mint a "
                        "cloud-platform-scoped access token. The only mode that "
                        "needs no Google identity on this deployment — and "
                        "unavailable where the partner's organisation enforces "
                        "constraints/iam.disableServiceAccountKeyCreation, which "
                        "is Google's own recommended baseline (required)"
                    ),
                    "send_instance_identity": _A2A_SEND_INSTANCE_IDENTITY_LABEL,
                },
                "setup_guide": {
                    "title": "Google Agent Platform A2A Setup",
                    "url": "https://a2a-protocol.org/",
                    "steps": [
                        "Ask the partner for the card URL of each Agent Engine agent you may task, and choose how this platform authenticates to it: impersonating a service account they name (keyless — this deployment must run with Google Application Default Credentials) or a service account key JSON they issue.",
                        "Paste the card URLs — separated by commas or new lines — and the credential for the mode you chose, then save. The platform fetches and verifies each card and shows what the partner declares.",
                        "Bind it to each agent that should be able to delegate: agent configuration > Tools tab > Integrations. Every verified partner agent becomes one delegation tool; binding exposes nothing.",
                    ],
                    "permissions": [],
                    "notes": (
                        "Impersonation stores nothing: the partner grants "
                        "roles/iam.serviceAccountTokenCreator on their account to "
                        "this deployment's Google identity and revokes it by "
                        "removing the role. A key JSON is stored encrypted, and "
                        "is unavailable where the partner's organisation "
                        "disables service account key creation."
                    ),
                },
            },
        },
        "extra_field_options": {
            "credential_mode": [
                {
                    "value": "impersonation",
                    "label": "Impersonate a service account — keyless (recommended)",
                },
                {"value": "key", "label": "Service account key JSON"},
            ],
            "send_instance_identity": _A2A_SEND_INSTANCE_IDENTITY_OPTIONS,
        },
        # Which fields belong to which choice. Read by the creation form and
        # the configuration editor, which hide a field whose condition is not
        # met AND omit it when saving — so switching mode CLEARS the credential
        # that no longer applies rather than leaving it stored and unused.
        "extra_field_visible_when": {
            "impersonate_service_account": {"credential_mode": ["impersonation"]},
            "service_account_json": {"credential_mode": ["key"]},
        },
        "available_tools": _A2A_OPERATIONS,
        "a2a_inbound_agent_tools": _A2A_INBOUND_AGENT_TOOLS,
        "a2a_outbound_exposed": _A2A_OUTBOUND_EXPOSED,
        # INBOUND pins who may call us. Neither value is a secret: they name
        # the caller, they do not authenticate by being known. Google signs
        # the partner's tokens; this platform only verifies them against
        # these pins (see ``app.a2a.google_federation``).
        "extra_fields": {
            "audience": (
                "Audience — the exact 'aud' claim the partner's tokens "
                "carry, which is whatever the partner's code requests its "
                "identity token for (commonly this deployment's URL). It is "
                "defence in depth rather than the trust anchor: a Google "
                "token's audience is chosen by whoever mints it (required)"
            ),
            "service_account": (
                "Allowed caller — the service account address "
                "(name@project.iam.gserviceaccount.com), or its numeric "
                "subject id, of the ONE Google identity allowed to call. "
                "Google signs every identity's tokens with one keyset, so "
                "this pin is what separates the partner from any other "
                "Google account (required)"
            ),
        },
        "setup_guide": {
            "title": "Google Agent Platform A2A Setup",
            "url": "https://a2a-protocol.org/",
            "steps": [
                "In the partner's Google project, decide which service account its agent runs as and have it mint an identity token for this deployment. Copy that token's audience into 'Audience' and the service account's address into 'Allowed caller'.",
                "Bind it to each agent the partner should reach: agent configuration > Tools tab > Integrations. Binding is what exposes the agent (it adds no tools), and the card URL then appears in the Integrations list on the agent's Tools tab.",
                "Send the partner that card URL. Nothing else is exchanged: no secret is stored on either side, and Google refreshes the caller's credential forever.",
            ],
            "permissions": [],
            "notes": (
                "Inbound pins are not secrets — they pin who may call, and "
                "every request is verified against Google's published signing "
                "keys (RS256, exact audience, pinned caller). The allowed-"
                "caller pin carries the whole trust decision: Google signs "
                "tokens for every identity in the world with ONE keyset, so a "
                "valid signature proves only that some Google account minted "
                "the token. A configuration without that pin is refused. "
                "Editing this integration re-pins every agent it is bound to "
                "on the partner's next request."
            ),
        },
    },
    "entra_a2a": {
        "name": "Foundry A2A",
        "description": (
            "Inbound partner authentication for exposed A2A agents — accept "
            "a partner agent's own Microsoft Entra tokens (e.g. an Azure "
            "Foundry project's managed identity) instead of a client "
            "secret. Pin the partner tenant, audience, and calling identity "
            "once, then bind this configuration to any exposed agent."
        ),
        "logo": "/logos/entra.svg",
        "category": "integration",
        "integration_category": "identity_access",
        "integration_types": ["identity", "a2a"],
        # No credential at all: the three fields PIN who may call, they do
        # not authenticate by being known. Azure signs the partner's tokens;
        # this platform only verifies them against these pins.
        "auth_type": "none",
        "fields": [],
        "a2a_directions": ["inbound", "outbound"],
        # Inbound PINS who may call us; outbound presents OUR OWN Entra
        # credentials to Azure Foundry. Same credential family, opposite
        # direction, and not one field in common.
        "variant_selector": "a2a_direction",
        "variants": {
            "outbound": {
                "extra_fields": {
                    "card_urls": (
                        "Partner agent card URLs — the exact location each agent "
                        "publishes its card at, separated by commas or new lines "
                        "(required). Not derived from a "
                        "base URL: neither vendor serves it at the conventional "
                        "well-known path."
                    ),
                    "tenant_id": "Your Entra tenant (directory) ID (required)",
                    "client_id": "Your Entra application (client) ID (required)",
                    "client_secret": "Your Entra client secret (required)",
                    "send_instance_identity": _A2A_SEND_INSTANCE_IDENTITY_LABEL,
                },
                "setup_guide": {
                    "title": "Foundry A2A Setup",
                    "url": "https://entra.microsoft.com/#view/Microsoft_AAD_RegisteredApps/ApplicationsListBlade",
                    "steps": [
                        "In YOUR Entra tenant, create an app registration for {APP_NAME} (App registrations > New registration) and add a client secret. This is the identity the partner's Foundry agent will see calling it.",
                        "Have the partner grant that application access to their agent, and ask them for the agent card URL of each agent you may task.",
                        "Paste those card URLs — separated by commas or new lines — with your tenant ID, the application (client) ID and the secret, then save. The platform fetches and verifies each card and shows what the partner declares.",
                        "Bind it to each agent that should be able to delegate: agent configuration > Tools tab > Integrations. Every verified partner agent becomes one delegation tool; binding exposes nothing.",
                    ],
                    "permissions": [],
                    "notes": (
                        "The secret is yours, stored once for the relationship: "
                        "when your tenant rotates it, paste the new one here and "
                        "every bound agent keeps working. Card URLs are not "
                        "derived from a base URL — Foundry does not serve them at "
                        "the conventional well-known path."
                    ),
                },
            },
        },
        "extra_field_options": {"send_instance_identity": _A2A_SEND_INSTANCE_IDENTITY_OPTIONS},
        "available_tools": _A2A_OPERATIONS,
        "a2a_inbound_agent_tools": _A2A_INBOUND_AGENT_TOOLS,
        "a2a_outbound_exposed": _A2A_OUTBOUND_EXPOSED,
        "extra_fields": {
            "tenant_id": (
                "Partner tenant (directory) ID — the GUID of the Entra "
                "directory that signs the partner's tokens (required)"
            ),
            "audience": (
                "Audience — the exact 'aud' claim the partner's tokens "
                "carry: the Application ID URI (api://…) or client-id GUID "
                "of a DEDICATED app registration representing this "
                "platform. Never a shared audience like Microsoft Graph "
                "(required)"
            ),
            "client_id": (
                "Allowed caller — application (client) ID of the one "
                "partner identity allowed to call, e.g. the Foundry "
                "project's managed identity (required)"
            ),
        },
        "setup_guide": {
            "title": "Foundry A2A Setup",
            "url": "https://entra.microsoft.com/#view/Microsoft_AAD_RegisteredApps/ApplicationsListBlade",
            "steps": [
                "In the PARTNER's Entra tenant, create a dedicated app registration representing {APP_NAME} (App registrations > New registration). Do not reuse an existing app.",
                "On the new registration, expose an Application ID URI (Expose an API > Add) — this exact value is the 'Audience' below. The app's client-id GUID also works if the partner platform requests tokens by GUID.",
                "Grant the calling identity (e.g. the Azure Foundry project's managed identity) permission to request tokens for that audience.",
                "Copy the partner tenant's Directory (tenant) ID into 'Partner tenant ID' below.",
                "Copy the calling identity's Application (client) ID into 'Allowed caller' below. Pin what the partner's tokens actually carry (azp/appid/oid claims) — the GUID a portal blade shows for an identity is not always the one its tokens present.",
                "Create this integration, then bind it to the agent like any other integration: agent configuration > Tools tab > Integrations. Binding it is the federation opt-in for that agent's A2A exposure (it adds no tools). The same integration can be bound to any number of agents, and one agent may bind several partner configurations.",
                "Once bound, the agent card URL — the URL the partner platform registers — appears in the Integrations list on the agent's Tools tab. Binding is what exposes the agent, so there is no separate switch to turn on.",
                "In the partner platform (e.g. Azure Foundry), pick 'Microsoft Entra' in the A2A connection dialog and register the agent card URL. No secret is stored on either side, and nothing ever needs re-pasting.",
            ],
            "permissions": [],
            "notes": (
                "None of the three values is a secret — they pin who may "
                "call, and every request is verified against the partner "
                "tenant's published signing keys (RS256, exact audience, "
                "pinned caller). Tenant-wide audiences such as Microsoft "
                "Graph or Azure Resource Manager are refused: any app in a "
                "tenant can mint tokens for those. Editing this integration "
                "re-pins every agent it is bound to on the partner's next "
                "request — when the partner's caller identity churns (e.g. "
                "a rebuilt Foundry agent), fix it here once."
            ),
        },
    },
    # The OTHER inbound A2A partner mechanism (see ``app.a2a.providers``):
    # this platform mints the credential and the partner drives the OAuth
    # client-credentials exchange itself. Contributes no agent tools —
    # binding it to an agent is the exposure opt-in, exactly like its Entra
    # sibling. The key mirrors ``app.a2a.providers.OAUTH_A2A_PROVIDER``.
    "oauth_a2a": {
        "name": "OAuth A2A",
        "description": (
            "Inbound partner authentication for exposed A2A agents — the "
            "platform mints a client ID and secret that the partner "
            "exchanges for a short-lived token at the card's token URL. For "
            "partners that can re-run an OAuth exchange themselves (e.g. "
            "Google ADK / Vertex). Bind it to any agent to expose that agent."
        ),
        "logo": "/logos/oauth_a2a.svg",
        "category": "integration",
        "integration_category": "identity_access",
        "integration_types": ["identity", "a2a"],
        # INBOUND: no operator-supplied credential — the client id / secret
        # are minted by this platform on request from the Config tab, and the
        # secret is shown exactly once, at that moment (stored only as a
        # bcrypt hash). Deliberately NOT on create: the secret would exist in
        # clear only for the length of the create call, which returns the
        # integration and not the credential, so nobody could ever see it.
        "auth_type": "none",
        "fields": [],
        "a2a_directions": ["inbound", "outbound"],
        # OUTBOUND: the mirror image. The partner mints, we present. Its
        # target is anything accepting OAuth client-credentials — including
        # ANOTHER XTM ONE DEPLOYMENT, whose inbound surface issues exactly
        # this credential shape, which is why this is the one target testable
        # end to end without a vendor account.
        "variant_selector": "a2a_direction",
        "variants": {
            "outbound": {
                "extra_fields": {
                    "card_urls": (
                        "Partner agent card URLs — the exact location each agent "
                        "publishes its card at, separated by commas or new lines. "
                        "One partner may expose several agents; each becomes its "
                        "own delegation tool (required)"
                    ),
                    "token_url": (
                        "Partner token endpoint, where the client id and secret are exchanged for a bearer (required)"
                    ),
                    "client_id": "Client ID the partner issued you (required)",
                    "client_secret": "Client secret the partner issued you (required)",
                    "send_instance_identity": _A2A_SEND_INSTANCE_IDENTITY_LABEL,
                },
                "setup_guide": {
                    "title": "OAuth A2A Setup",
                    "url": "https://a2a-protocol.org/",
                    "steps": [
                        "Ask the partner for a client ID, a client secret and its token endpoint. From another XTM One deployment these come from its OAuth A2A inbound integration: its Config tab generates the credential, and the token endpoint is its base URL followed by /mcp/oauth/token.",
                        "Paste the partner's agent card URLs — one per agent it exposes, separated by commas or new lines — with the token endpoint, client ID and secret, then save. The platform fetches and verifies each card and shows what the partner declares.",
                        "Bind it to each agent that should be able to delegate: agent configuration > Tools tab > Integrations. Every verified partner agent becomes one delegation tool; binding exposes nothing.",
                        "Check the Config tab: each partner agent carries its own verdict, and 'Verify' re-checks it now. A partner running XTM One also lists the other agents your credential reaches, which appear here without retyping a URL.",
                    ],
                    "permissions": [],
                    "notes": (
                        "The credential is the partner's, stored once for the "
                        "relationship and presented on every delegation. Another "
                        "XTM One deployment serves its cards anonymously, so "
                        "registration needs the card URL alone; the credential is "
                        "first used when this platform asks the partner for its "
                        "agents or delegates work."
                    ),
                },
            },
        },
        "extra_field_options": {"send_instance_identity": _A2A_SEND_INSTANCE_IDENTITY_OPTIONS},
        "available_tools": _A2A_OPERATIONS,
        "a2a_inbound_agent_tools": _A2A_INBOUND_AGENT_TOOLS,
        "a2a_outbound_exposed": _A2A_OUTBOUND_EXPOSED,
        "setup_guide": {
            "title": "OAuth A2A Setup",
            "url": "https://a2a-protocol.org/",
            "steps": [
                "Create this integration, then generate its credential from the Config tab. The secret is shown ONCE, at that moment — copy it before leaving the page.",
                "Bind it to each agent the partner should reach: agent configuration > Tools tab > Integrations. Binding is what exposes the agent (it adds no tools).",
                "Send the partner the agent card URL (shown in the Integrations list on the agent's Tools tab), the client ID, and the secret.",
                "The partner exchanges the client ID and secret for a bearer token at the token URL advertised on the agent card, refreshing it on its own schedule.",
                "If the secret is ever lost or leaked, regenerate it here — the old one stops working immediately for every bound agent.",
            ],
            "permissions": [],
            "notes": (
                "One credential per partner relationship, valid on every "
                "agent this integration is bound to — so a leaked secret "
                "reaches all of them, and regenerating it re-keys all of "
                "them. Partners that cannot re-run a token exchange (Azure "
                "Foundry) should use the Foundry A2A integration instead, "
                "where Azure mints and refreshes the token."
            ),
        },
    },
    "onepassword": {
        "name": "1Password",
        "description": (
            "Secrets & credential management (1Password) - vaults and items "
            "via a Connect server: search/read items, reveal passwords and "
            "one-time passwords (TOTP, with local RFC 6238 fallback), read "
            "attached documents and store any item file as a file of the "
            "conversation, create/update/delete items (incl. adding "
            "TOTP secrets), audit the Connect access log; plus 1Password "
            "Business security telemetry via the Events Reporting API "
            "(sign-in attempts, item usage, audit events) and user/group "
            "provisioning via the SCIM bridge (invite, suspend, reactivate, "
            "group membership)"
        ),
        "logo": "/logos/onepassword.svg",
        "category": "integration",
        "integration_category": "identity_access",
        "integration_types": ["secrets_management", "identity", "audit"],
        "available_events": [],
        # All three 1Password surfaces authenticate with bearer tokens
        # (Connect access token, Events Reporting token, SCIM bridge token) -
        # there is no OAuth flow, so the only supported auth mode is
        # "api_key".
        "auth_type": "api_key",
        "supported_auth_types": ["api_key"],
        "fields": ["api_key"],
        "extra_fields": {
            "base_url": (
                "1Password Connect server URL (self-hosted, e.g. "
                "https://connect.company.com:8080) - required for vault and "
                "item tools"
            ),
            "events_api_key": (
                "Events Reporting bearer token (optional, 1Password "
                "Business) - issued at 1Password.com > Integrations > "
                "Events Reporting; unlocks sign-in attempts, item usage, "
                "and audit event tools"
            ),
            "events_base_url": (
                "Events API base URL (optional; default "
                "https://events.1password.com - use events.1password.ca, "
                "events.1password.eu, or events.ent.1password.com to match "
                "your account region)"
            ),
            "scim_base_url": (
                "SCIM bridge URL (optional, 1Password Business, e.g. "
                "https://scim.company.com) - unlocks user/group "
                "provisioning tools"
            ),
            "scim_api_key": ("SCIM bridge bearer token (optional) - generated when deploying the SCIM bridge"),
        },
        "setup_guide": {
            "title": "1Password Connect + Events + SCIM Setup",
            "url": "https://developer.1password.com/docs/connect/",
            "steps": [
                "Sign in to your 1Password Business account and go to Integrations (1Password.com > Integrations).",
                "Create a Secrets Automation workflow: choose the vaults the automation may access, then deploy the 1Password Connect server (Docker/Kubernetes) with the generated 1password-credentials.json.",
                "Copy the Connect access token issued for the workflow and paste it into the 'API Key' field; set 'base_url' to your Connect server URL (e.g. https://connect.company.com:8080).",
                "Optional (security telemetry): under Integrations, set up an Events Reporting integration, issue a bearer token with the event types you want (sign-in attempts, item usages, audit events), and paste it into 'events_api_key'. Set 'events_base_url' if your account is hosted in .ca/.eu/enterprise.",
                "Optional (provisioning): deploy the 1Password SCIM bridge, then put its URL in 'scim_base_url' and its bearer token in 'scim_api_key'.",
                "Click 'Test Connection' to verify every configured surface (Connect vault list, Events token introspection, SCIM bridge user probe).",
                "To pick 1Password items in secret fields and browser profiles (a password, an API token, a TOTP seed for an unattended sign-in), an administrator also connects the same Connect server as a secret vault in Settings > Access and security > Secret vaults.",
            ],
            "permissions": [
                {
                    "scope": "Connect: vaults & items (read + write)",
                    "description": "The Connect token only sees the vaults granted to its Secrets Automation workflow - grant read access for lookups/TOTP, write access to let the agent create, update, or delete items",
                },
                {
                    "scope": "Events Reporting (read, optional)",
                    "description": "Sign-in attempts, item usage events, and audit events - the token's feature scopes are chosen when it is issued",
                },
                {
                    "scope": "SCIM bridge (read + write, optional)",
                    "description": "List users/groups and manage provisioning: invite users, update names, suspend/reactivate accounts, and manage group membership",
                },
            ],
            "notes": (
                "1Password has no monolithic REST API: secrets access goes "
                "through a self-hosted Connect server scoped to specific "
                "vaults, security telemetry through the SaaS Events "
                "Reporting API, and user provisioning through the "
                "self-hosted SCIM bridge. Each surface has its own bearer "
                "token; configure only the ones you need - tools for an "
                "unconfigured surface explain what is missing instead of "
                "failing silently. One-time passwords are read from the "
                "server-computed TOTP when available and computed locally "
                "(RFC 6238) from the otpauth:// seed otherwise. A downloaded "
                "item file becomes a file of the conversation and can hold "
                "secrets; the Connect API offers no file upload (Document "
                "items cannot be created through it). All tokens "
                "are stored encrypted. This integration gives agents tools; "
                "the 1Password secret vault (Settings > Access and security > Secret "
                "vaults) lets any secret field and browser profile read an "
                "item's field instead of holding a copy. Both read through "
                "the same Connect client."
            ),
        },
    },
    "purview": {
        "name": "Microsoft Purview",
        "description": "Data governance, risk & compliance — eDiscovery, unified audit log search, sensitivity & retention labels, subject rights requests, and the data catalog (search, assets, lineage, glossary)",
        "logo": "/logos/purview.svg",
        "category": "integration",
        "integration_category": "grc",
        "integration_types": ["compliance", "data_governance", "ediscovery"],
        # Delegated OAuth (sign-in) is the default; app-only application
        # permissions remain available via the "API Key" auth mode and are
        # recommended for unattended governance / compliance automation.
        "auth_type": "oauth",
        "supported_auth_types": ["oauth", "api_key"],
        "fields": ["client_id", "client_secret"],
        # Single-resource (Microsoft Graph) delegated scopes + OIDC scopes. The
        # Purview Data Map audience (https://purview.azure.net) is acquired at
        # runtime by redeeming the refresh token, so its scope must NOT be mixed
        # in here (Azure AD v2 rejects multi-resource token requests) — the app
        # registration carries that delegated permission + admin consent instead.
        "scopes": (
            "openid profile email offline_access User.Read "
            "eDiscovery.Read.All eDiscovery.ReadWrite.All AuditLogsQuery.Read.All "
            "SensitivityLabels.Read.All RecordsManagement.Read.All "
            "RecordsManagement.ReadWrite.All SubjectRightsRequest.Read.All "
            "SubjectRightsRequest.ReadWrite.All"
        ),
        "authorize_url": "https://login.microsoftonline.com/common/oauth2/v2.0/authorize",
        "token_url": "https://login.microsoftonline.com/common/oauth2/v2.0/token",
        "extra_fields": {
            "tenant_id": "Azure AD Tenant ID (required for app-only / API Key mode; recommended for single-tenant OAuth apps)",
            "account_name": "Microsoft Purview governance account name (optional — required only for the Data Map / data catalog tools; e.g. 'mycompany' for https://mycompany.purview.azure.com)",
        },
        "setup_guide": {
            "title": "Microsoft Purview Integration Setup",
            "url": "https://learn.microsoft.com/en-us/graph/api/resources/security-ediscovery-apioverview",
            "steps": [
                "In the Microsoft Entra admin center, go to 'App registrations' and create a new registration (or reuse your existing Microsoft app).",
                "Redirect URI: select 'Web' and enter {base_url}/api/oauth/callback (required for the OAuth sign-in mode).",
                "Copy the Application (client) ID and Directory (tenant) ID from the Overview page; paste the Tenant ID below.",
                "Under 'Certificates & secrets', create a new client secret and copy its value into the Client Secret field below.",
                "Go to 'API permissions' > 'Add a permission' > 'Microsoft Graph' and add the compliance permissions below — choose 'Delegated permissions' for OAuth (sign-in) mode, or 'Application permissions' for app-only (API Key) mode. Note: Records Management and Subject Rights Requests are delegated-only (no app-only support).",
                "For the data catalog (Data Map) tools, also Add a permission > 'APIs my organization uses' > search 'Microsoft Purview' (resource https://purview.azure.net) and add the delegated 'user_impersonation' permission.",
                "Click 'Grant admin consent' — the eDiscovery, audit, and information-protection permissions require tenant admin consent.",
                "In the Microsoft Purview compliance portal, assign the principal the relevant roles (eDiscovery Manager/Administrator, Compliance Administrator, etc.).",
                "For the data catalog (Data Map) tools: set the Purview account name below, and in the Microsoft Purview governance portal assign the principal a data-plane role on the root collection — 'Data Curator' for catalog search/assets/lineage/glossary, 'Collection Admin' for collections.",
                "Choose the authentication mode: 'OAuth' to act as the signed-in user (covers all compliance + catalog tools), or 'API Key' for app-only client credentials (best for unattended automation; Records Management and Subject Rights Requests are unavailable in app-only mode).",
            ],
            "permissions": [
                {
                    "scope": "eDiscovery.ReadWrite.All",
                    "description": "Read and manage eDiscovery (Premium) cases, custodians, searches, and holds",
                },
                {
                    "scope": "AuditLogsQuery.Read.All",
                    "description": "Search the Microsoft Purview unified audit log",
                },
                {
                    "scope": "SensitivityLabels.Read.All",
                    "description": "Read Information Protection sensitivity labels",
                },
                {
                    "scope": "RecordsManagement.ReadWrite.All",
                    "description": "Read and manage retention labels and event-based retention (delegated only)",
                },
                {
                    "scope": "SubjectRightsRequest.Read.All",
                    "description": "Read Subject Rights Requests / DSRs (delegated only)",
                },
                {
                    "scope": "https://purview.azure.net/user_impersonation",
                    "description": "Access the Purview Data Map (data catalog, lineage, glossary) — delegated; the principal also needs a Purview data-plane role",
                },
            ],
            "notes": (
                "Microsoft Purview is Microsoft's unified data governance, risk & "
                "compliance platform. This integration spans both pillars: the "
                "Risk & Compliance APIs on Microsoft Graph (eDiscovery, audit, "
                "sensitivity & retention labels, subject rights requests) and the "
                "Unified Data Governance Data Map (catalog search, assets, lineage, "
                "glossary) on https://{account}.purview.azure.com. OAuth (sign-in) "
                "covers every tool; app-only (API Key) is best for unattended "
                "automation but cannot use the delegated-only Records Management "
                "and Subject Rights Request APIs. The data catalog tools require "
                "the Purview account name and a data-plane role (Data Curator / "
                "Collection Admin) on the principal."
            ),
        },
    },
    "tanium": {
        "name": "Tanium",
        "description": "Converged endpoint management & security — Threat Response alerts & intel, Interact questions (threat hunting), endpoint/asset inventory, and package/action deployment (remediation)",
        "logo": "/logos/tanium.svg",
        "category": "integration",
        "integration_category": "detection_response",
        "integration_types": ["edr", "xdr", "threat_intel", "soar"],
        "available_events": [],
        "auth_type": "api_key",
        "fields": ["api_key"],
        "extra_fields": {
            "base_url": (
                "Tanium base URL — Cloud: https://<customer>-api.cloud.tanium.com, On-Prem: https://<tanium-server>"
            ),
        },
        "setup_guide": {
            "title": "Tanium API Token Setup",
            "url": "https://developer.tanium.com/apis/api_intro",
            "steps": [
                "Sign in to the Tanium Console as an administrator.",
                "Go to Administration > Permissions > API Tokens and click 'New API Token'.",
                "Set a long expiration (e.g. 365 days — tokens default to 7 days and must be rotated before expiry).",
                "Under 'Trusted IP Addresses', add the public IP address(es) this platform connects from (use 0.0.0.0/0 only if your security policy allows — it disables IP allow-listing).",
                "Optionally scope the token to a Persona/roles that grant the capabilities you need (see permissions below).",
                "Create the token and copy its value immediately — it is shown only once.",
                "Paste the token value in the 'API Key' field below.",
                "Enter your Tanium base URL in the 'base_url' field (Cloud: https://<customer>-api.cloud.tanium.com, On-Prem: https://<tanium-server> — no trailing slash).",
                "Click 'Test Connection' to verify the token and URL.",
            ],
            "permissions": [
                {
                    "scope": "Threat Response (Show / Use)",
                    "description": "Read and manage Threat Response alerts, intel documents (created from text or a file of the conversation), live-response actions, connections, and file evidence (downloaded as password-protected ZIPs)",
                },
                {
                    "scope": "Interact — Ask Dynamic Question + Read Sensor",
                    "description": "Ask Tanium questions and read sensor/saved-question results for live threat hunting",
                },
                {
                    "scope": "Read Sensor / Read Saved Question",
                    "description": "Discover sensors and run saved questions",
                },
                {
                    "scope": "Read Action / Write Action / Read Package",
                    "description": "List packages and action groups, and deploy actions (remediation/response) to computer groups",
                },
                {
                    "scope": "Computer Group (read) + Gateway / Sensor Read",
                    "description": "Read computer groups and query endpoint/asset inventory via the API Gateway (GraphQL)",
                },
            ],
            "notes": (
                "Tanium authenticates with an API token sent in the 'session' HTTP header "
                "(not Authorization/Bearer). This integration spans the Core Platform REST "
                "API (/api/v2 — Interact questions, sensors, computer groups, packages, "
                "actions), the Threat Response REST API "
                "(/plugin/products/threat-response/api/v1 — alerts, intel, live response), "
                "and the API Gateway GraphQL endpoint "
                "(/plugin/products/gateway/graphql — endpoint/asset inventory). The token "
                "inherits the permissions of its creator/persona — use a dedicated service "
                "account scoped to the computer groups and capabilities the agent needs. "
                "File evidence taken from an endpoint can be live malware: it is stored "
                "only as Tanium's ZIP encrypted with the password 'infected' and never "
                "read by the platform. "
                "Remember to rotate the token before it expires or the integration will stop "
                "working."
            ),
        },
    },
    "thehive": {
        "name": "TheHive",
        "description": (
            "TheHive (StrangeBee security incident response platform) - triage "
            "alerts, promote them to cases, manage observables (IOCs) and "
            "investigation tasks, document findings in task logs and comments, "
            "and enrich observables with Cortex analyzers"
        ),
        "logo": "/logos/thehive.svg",
        "category": "integration",
        "integration_category": "detection_response",
        "integration_types": ["soar", "case_management"],
        "available_events": [],
        # TheHive authenticates with a per-user API key sent as
        # ``Authorization: Bearer <key>`` - there is no OAuth flow, so the
        # only supported auth mode is "api_key".
        "auth_type": "api_key",
        "supported_auth_types": ["api_key"],
        "fields": ["api_key"],
        "extra_fields": {
            "base_url": "TheHive instance URL (e.g. https://thehive.company.com)",
        },
        "setup_guide": {
            "title": "TheHive API Key Setup",
            "url": "https://docs.strangebee.com/thehive/api-docs/",
            "steps": [
                "Log in to your TheHive instance as an org-admin.",
                "We recommend creating a dedicated service user (Organisation > Users > Add user, type 'service') with a profile granting only the access the agent needs - 'read-only' for pure lookups, 'analyst' for full case/alert/task/observable management.",
                "On the user's row, click 'API Key' > 'Create' (or 'Reveal' if one exists) and copy the key.",
                "Paste the key into the 'API Key' field above.",
                "Set 'base_url' to your TheHive instance URL (e.g. https://thehive.company.com).",
                "Click 'Test Connection' to verify the key (calls GET /api/v1/user/current).",
            ],
            "permissions": [
                {
                    "scope": "Cases & Alerts (read + write)",
                    "description": "Search, read, create, update, close, merge cases; triage alerts and promote them to cases",
                },
                {
                    "scope": "Observables & TTPs (read + write)",
                    "description": "List, add, and update observables (IOCs); flag IOCs and record sightings",
                },
                {
                    "scope": "Tasks & Logs (read + write)",
                    "description": "List, create, and complete investigation tasks; add task logs documenting findings",
                },
                {
                    "scope": "Cortex (run analyzers, optional)",
                    "description": "List analyzers, run enrichment jobs on observables, and read job reports - requires a Cortex connector in TheHive",
                },
            ],
            "notes": (
                "TheHive authenticates with an API key sent as 'Authorization: "
                "Bearer <key>'. The key inherits the profile and organisation of "
                "the user that owns it, so use a dedicated service user with a "
                "scoped profile. The API lives under /api/v1 on the same host "
                "as the web UI. Primary target is TheHive 5 (StrangeBee); "
                "most endpoints also work on TheHive 4. The key is stored "
                "encrypted here."
            ),
        },
    },
    "xsoar": {
        "name": "Cortex XSOAR",
        "description": "Palo Alto Cortex XSOAR (SOAR) — incidents (search/create/update/close), War Room orchestration + run any command/automation, playbooks, threat-intel indicators (create/search/enrich/exclude), lists, and users",
        "logo": "/logos/xsoar.svg",
        "category": "integration",
        "integration_category": "detection_response",
        "integration_types": ["soar", "threat_intel"],
        "available_events": [],
        "auth_type": "api_key",
        "fields": ["api_key"],
        "extra_fields": {
            "base_url": (
                "Cortex XSOAR base URL — XSOAR 8/Cloud: https://api-<fqdn> "
                "(tenant API host), XSOAR 6/on-prem: https://<server>"
            ),
            "api_key_id": (
                "API Key ID (x-xdr-auth-id) — REQUIRED for Cortex XSOAR 8/Cloud; leave empty for XSOAR 6 on-prem"
            ),
            "auth_mode": (
                "Cortex XSOAR 8 key type: 'standard' or 'advanced' (default standard). "
                "Advanced keys are hashed per request with a nonce + timestamp"
            ),
        },
        "setup_guide": {
            "title": "Cortex XSOAR API Key Setup",
            "url": "https://docs-cortex.paloaltonetworks.com/r/Cortex-XSOAR-8-API/Get-started-with-Cortex-XSOAR-8-APIs",
            "steps": [
                "Cortex XSOAR 8 / Cloud: in the Cortex XSOAR console go to Settings & Info > Settings > Integrations > API Keys and click 'New Key'. Choose Standard (simplest) or Advanced, assign roles, and copy the generated key.",
                "Get the matching API Key ID from the API Keys table (the 'ID' column) and put it in the 'api_key_id' field below. Set 'auth_mode' to 'standard' or 'advanced' to match the key you created.",
                "Find your tenant API FQDN (the API host, e.g. api-<your-tenant>.xsoar.<region>.paloaltonetworks.com) and enter it as the base URL with the https:// scheme (no trailing slash, no /xsoar path — it is added automatically).",
                "Cortex XSOAR 6 / on-prem: go to Settings > Integrations > API Keys, create a key, copy it, and leave 'api_key_id' empty. Use your server URL (https://<server>) as the base URL.",
                "Paste the API key into the 'API Key' field below.",
                "Click 'Test Connection' to verify the key, key ID, and base URL.",
            ],
            "permissions": [
                {
                    "scope": "Incidents (read/write)",
                    "description": "Search, read, create, update, and close incidents",
                },
                {
                    "scope": "War Room / Automation execution",
                    "description": "Add War Room entries and run commands/automations via the synchronous execute endpoint",
                },
                {
                    "scope": "Playbooks (read)",
                    "description": "List playbooks (auto-run on incident creation)",
                },
                {
                    "scope": "Threat Intel / Indicators (read/write)",
                    "description": "Search, create, enrich, whitelist, and delete indicators",
                },
                {
                    "scope": "Lists & Users (read)",
                    "description": "Read reusable lists and user directory for assignment",
                },
            ],
            "notes": (
                "Cortex XSOAR 8 (Cortex platform) requires BOTH an API key "
                "(Authorization header) and an API Key ID (x-xdr-auth-id header); set "
                "api_key_id and choose auth_mode = standard or advanced. Advanced keys "
                "are hashed per request with a nonce + timestamp to prevent replay — "
                "this integration handles that automatically. The v8 public API lives "
                "under /xsoar/public/v1 (added automatically). Cortex XSOAR 6 (on-prem) "
                "uses a single API key in the Authorization header against the server "
                "root — leave api_key_id empty. The run-command tool exposes the entire "
                "installed integration/automation command surface, so the API key's "
                "assigned roles bound what the agent can do."
            ),
        },
    },
    "xdr": {
        "name": "Cortex XDR",
        "description": "Palo Alto Cortex XDR / XSIAM (XDR/EDR) — incidents (list/get/update), alerts, endpoints, response actions (isolate, scan, quarantine, retrieve, run script) with the download links of retrieved files, XQL threat hunting, file hash block/allow lists, endpoint tags, and audit logs",
        "logo": "/logos/xdr.svg",
        "category": "integration",
        "integration_category": "detection_response",
        "integration_types": ["xdr", "edr", "threat_intel", "soar"],
        "available_events": [],
        "auth_type": "api_key",
        "fields": ["api_key"],
        "extra_fields": {
            "base_url": (
                "Cortex XDR tenant API host — https://api-<fqdn> "
                "(from your API key's CURL example URL, without the /public_api path)"
            ),
            "api_key_id": "API Key ID (x-xdr-auth-id) — REQUIRED",
            "auth_mode": (
                "API key type: 'standard' or 'advanced' (default standard). "
                "Advanced keys are hashed per request with a nonce + timestamp"
            ),
        },
        "setup_guide": {
            "title": "Cortex XDR API Key Setup",
            "url": "https://docs-cortex.paloaltonetworks.com/r/Cortex-XDR-REST-API/Get-Started-with-Cortex-XDR-APIs",
            "steps": [
                "In the Cortex XDR / XSIAM console, go to Settings > Configurations > Integrations > API Keys and click 'New Key'.",
                "Choose the security level — Standard (simplest, key sent as-is) or Advanced (hashed per request) — assign a role with the permissions you need (Instance Administrator is required for XQL), and create the key. Copy the key value (shown once).",
                "Note the matching API Key ID from the 'ID' column and put it in the 'api_key_id' field below. Set 'auth_mode' to 'standard' or 'advanced' to match the key.",
                "Right-click the key > 'View Examples' and copy the FQDN from the CURL URL (https://api-<fqdn>/public_api/v1/...). Enter https://api-<fqdn> as the base URL below (no trailing slash, no /public_api — it is added automatically).",
                "Paste the API key into the 'API Key' field below.",
                "Click 'Test Connection' to verify the key, key ID, and base URL.",
            ],
            "permissions": [
                {
                    "scope": "Incidents & Alerts (view/edit)",
                    "description": "List, get, and update incidents; list alerts with MITRE mapping",
                },
                {
                    "scope": "Endpoint Administrations / Action Center",
                    "description": "List endpoints and run response actions (isolate, scan, quarantine, file retrieval, run scripts)",
                },
                {
                    "scope": "Instance Administrator (for XQL)",
                    "description": "Run XQL queries for threat hunting (XQL APIs require this role and available query quota)",
                },
                {
                    "scope": "IOCs / Allow & Block lists",
                    "description": "Add file hashes to the block or allow lists",
                },
            ],
            "notes": (
                "Cortex XDR (and Cortex XSIAM, which shares the same API) authenticates "
                "with an API key (Authorization header) plus an API Key ID "
                "(x-xdr-auth-id header). Advanced keys are hashed per request with a "
                "nonce + timestamp to prevent replay — this integration handles that "
                "automatically when auth_mode = advanced. All calls target "
                "/public_api/v1 on the tenant API host (added automatically). XQL "
                "queries require the Instance Administrator role and consume your daily "
                "query quota. The API key's assigned role bounds what the agent can do. "
                "Files retrieved from an endpoint (by a file retrieval or a script) can "
                "be live malware and Cortex serves them in an unencrypted ZIP, so the "
                "agent gets their download links and never stores or reads them. A "
                "script's package (its YAML in a ZIP) is stored as a file of the "
                "conversation and can be inserted from one (script management RBAC)."
            ),
        },
    },
    "qradar": {
        "name": "IBM QRadar",
        "description": "IBM QRadar SIEM — offenses (list/get/update/close/notes), AQL threat hunting (Ariel searches), reference data sets (IOC management), assets, rules, and log sources",
        "logo": "/logos/qradar.svg",
        "category": "integration",
        "integration_category": "siem_analytics",
        "integration_types": ["siem", "threat_intel"],
        "available_events": [],
        "auth_type": "api_key",
        "fields": ["api_key"],
        "extra_fields": {
            "base_url": "QRadar console URL (e.g. https://qradar.example.com)",
            "api_version": (
                "QRadar API version to pin (e.g. 20.0). Optional — defaults to a "
                "recent version, which safely falls back to the console's latest"
            ),
        },
        "setup_guide": {
            "title": "IBM QRadar Authorized Service Token Setup",
            "url": "https://www.ibm.com/docs/en/qradar-common?topic=api-endpoint-documentation-supported-versions",
            "steps": [
                "In the QRadar console, go to Admin > User Management > Authorized Services and click 'Add Authorized Service'.",
                "Give it a name, select a user role and security profile that grant the access the agent needs (offenses, Ariel/AQL, reference data, assets), and set an expiry (or no expiry).",
                "Create the service and copy the generated token — this is the value for the 'API Key' field below.",
                "Enter your QRadar console URL in the 'base_url' field (e.g. https://qradar.example.com — no trailing slash, no /api).",
                "Optionally set 'api_version' to pin the API version (e.g. 20.0). Leave blank to use a recent default.",
                "Click 'Test Connection' to verify the token and URL.",
            ],
            "permissions": [
                {
                    "scope": "Offenses (view/manage)",
                    "description": "List, read, update, close, and annotate offenses",
                },
                {
                    "scope": "Ariel / AQL (search)",
                    "description": "Run AQL searches against events/flows for threat hunting",
                },
                {
                    "scope": "Reference Data (view/manage)",
                    "description": "Read and update reference sets for IOC management",
                },
                {
                    "scope": "Assets, Offenses, Log Activity (view)",
                    "description": "Read assets, rules, and log sources for context",
                },
            ],
            "notes": (
                "QRadar authenticates with an Authorized Service token sent in the SEC "
                "HTTP header; this integration also pins the API Version header (IBM "
                "recommends always sending one so console upgrades don't break clients — "
                "requesting a version newer than the console supports safely falls back "
                "to the latest available). All calls target {base_url}/api. The token "
                "inherits the user role and security profile selected when the authorized "
                "service was created, which bound what the agent can do. The QRadar "
                "console must present a TLS certificate trusted by this platform."
            ),
        },
    },
    "fortisiem": {
        "name": "Fortinet FortiSIEM",
        "description": "Fortinet FortiSIEM — incidents (list/get/update/clear with triggering events), event queries (threat hunting), watch lists (IOC management), lookup tables, and CMDB device context",
        "logo": "/logos/fortisiem.svg",
        "category": "integration",
        "integration_category": "siem_analytics",
        "integration_types": ["siem", "threat_intel"],
        "available_events": [],
        "auth_type": "api_key",
        "fields": ["api_key"],
        "extra_fields": {
            "base_url": "FortiSIEM Supervisor URL (e.g. https://fortisiem.example.com)",
            "organization": (
                "Organization for the Basic auth login (sent as 'org/username'). "
                "Use 'super' for the Super/Global account or Enterprise "
                "deployments; leave blank to send the bare username"
            ),
            "username": "FortiSIEM account username",
        },
        # No credential without the Supervisor URL (#9898).
        "extra_field_required_when": {"base_url": {}},
        "setup_guide": {
            "title": "Fortinet FortiSIEM API Account Setup",
            "url": "https://docs.fortinet.com/document/fortisiem/7.3.0/integration-api-guide/991238",
            "steps": [
                "In the FortiSIEM GUI, go to CMDB > Users and create (or pick) an account for API access with a role that grants access to the features the agent will use (incidents, analytics/event queries, watch lists, CMDB).",
                "Enter your FortiSIEM Supervisor URL in the 'base_url' field (e.g. https://fortisiem.example.com — no trailing slash, no /phoenix).",
                "Set 'organization' to the account's organization ('super' for the Super/Global account, which Enterprise deployments also use; a Service Provider org name otherwise). FortiSIEM sends the Basic auth login as 'org/username'.",
                "Set 'username' to the account username, and paste the account password into the 'API Key' field below.",
                "Click 'Test Connection' to verify the credentials and URL.",
            ],
            "permissions": [
                {
                    "scope": "Incidents (view/manage)",
                    "description": "List and read incidents with triggering events; add comments, clear, and sync external tickets",
                },
                {
                    "scope": "Analytics / event queries (run)",
                    "description": "Run event queries against the event database for threat hunting",
                },
                {
                    "scope": "Resources > Watch Lists (view/manage)",
                    "description": "Read and update watch lists and lookup tables for IOC management",
                },
                {
                    "scope": "CMDB (view)",
                    "description": "Read device inventory and device details for context",
                },
            ],
            "notes": (
                "FortiSIEM authenticates every REST call with HTTP Basic "
                "credentials where the login is 'organization/username' (e.g. "
                "super/apiuser). All calls target {base_url}/phoenix/rest on the "
                "Supervisor. The incident, watch list, and lookup table APIs "
                "return JSON; the event query and CMDB APIs use XML — both are "
                "handled automatically. The account's FortiSIEM role bounds what "
                "the agent can do. The Supervisor must present a TLS certificate "
                "trusted by this platform."
            ),
        },
    },
    "splunk": {
        "name": "Splunk",
        "description": "Splunk Enterprise & Enterprise Security SIEM - SPL threat hunting (async search jobs), saved/correlation searches, indexes and fired alerts, KV Store lookups (push enrichment/IOCs), and - with Enterprise Security - notable events (gather/triage/respond) and threat-intel collections (IOC management). Targets the Splunk management REST API; works with Splunk Enterprise, Splunk Cloud, and Splunk Enterprise Security",
        "logo": "/logos/splunk.svg",
        "category": "integration",
        "integration_category": "siem_analytics",
        "integration_types": ["siem", "threat_intel"],
        "available_events": [],
        "auth_type": "api_key",
        "supported_auth_types": ["api_key"],
        "fields": ["api_key"],
        "extra_fields": {
            "base_url": "Splunk management REST endpoint (e.g. https://splunk.example.com:8089). For Splunk Cloud use your stack's API host on port 8089 - this is the REST/management port, not the web UI on :8000.",
            "username": "Optional. Leave blank to authenticate with a Splunk authentication token (recommended; Splunk Cloud + Enterprise Security). Set a username to use HTTP Basic auth (on-prem Splunk Enterprise) with the password in the API Key field.",
        },
        "setup_guide": {
            "title": "Splunk Authentication Token Setup",
            "url": "https://docs.splunk.com/Documentation/Splunk/latest/Security/UseAuthTokens",
            "steps": [
                "In Splunk Web, go to Settings > Tokens and ensure token authentication is enabled (Splunk Cloud has it on by default; on Splunk Enterprise an admin may need to enable it).",
                "Create a token for a user/role that grants the access the agent needs: search and saved searches for hunting, and for Enterprise Security the edit_notable_events capability plus threat-intel access.",
                "Copy the generated token and paste it into the 'API Key' field below (it is shown only once).",
                "Enter your Splunk management endpoint in the 'base_url' field (e.g. https://splunk.example.com:8089 - the REST/management port, not the web UI on :8000).",
                "Leave 'username' blank to use the token (recommended). To use HTTP Basic auth instead (on-prem Splunk Enterprise), set 'username' and put the account password in the 'API Key' field.",
                "Click 'Test Connection' to verify the credentials and URL.",
            ],
            "permissions": [
                {
                    "scope": "Search (run searches)",
                    "description": "Run SPL searches and dispatch saved/correlation searches for threat hunting",
                },
                {
                    "scope": "Saved searches (list/edit)",
                    "description": "List and manage saved searches and Enterprise Security correlation searches",
                },
                {
                    "scope": "KV Store (read/write)",
                    "description": "Query and push records into KV Store collections for lookups and enrichment",
                },
                {
                    "scope": "Enterprise Security - Notable events",
                    "description": "List, inspect, and update/triage notable events (edit_notable_events capability)",
                },
                {
                    "scope": "Enterprise Security - Threat intelligence",
                    "description": "List, add, update, and delete threat-intel indicators (IOCs)",
                },
            ],
            "notes": (
                "Splunk Enterprise, Splunk Cloud, and Splunk Enterprise Security all share one "
                "management REST API on port 8089. A Bearer authentication token (Settings > Tokens) "
                "is recommended and works for Splunk Cloud and Enterprise Security; HTTP Basic "
                "(username + password) is supported for on-prem Splunk Enterprise. The token or "
                "account role bounds what the agent can do. The Enterprise Security tools (notable "
                "events, threat intelligence) require the ES app to be installed. The Splunk "
                "management endpoint must present a TLS certificate trusted by this platform."
            ),
        },
    },
    "logrhythm": {
        "name": "LogRhythm",
        "description": "LogRhythm SIEM - cases (list/get/create/update/change-status/add-note), alarms (list/get/update status + Risk Based Priority/comment/events), indexed log and event search (async hunt), and admin context (hosts, entities). Calls the LogRhythm REST APIs on the Platform Manager gateway",
        "category": "integration",
        "integration_category": "siem_analytics",
        "integration_types": ["siem", "soar"],
        # LogRhythm authenticates with a third-party API token (a JWT) sent as a
        # Bearer token - there is no OAuth, so the only auth mode is "api_key".
        "auth_type": "api_key",
        "supported_auth_types": ["api_key"],
        "fields": ["api_key"],
        "extra_fields": {
            "base_url": "LogRhythm API gateway URL - the Platform Manager host on port 8501 (e.g. https://logrhythm-pm.example.com:8501)",
        },
        "setup_guide": {
            "title": "LogRhythm API Token Setup",
            "url": "https://developers.exabeam.com/logrhythm-siem/docs/logrhythm-rest-api",
            "steps": [
                "In the LogRhythm Client Console, open the Third Party Applications manager (used to register applications that call the REST API).",
                "Register a new third-party application and generate its API token (a JSON Web Token). Grant it access to the REST APIs the agent needs (Case, Alarm, Search, Administration).",
                "Copy the generated API token and paste it into the 'API Key' field below (it is shown only once).",
                "Enter your LogRhythm API gateway URL in the 'base_url' field - the Platform Manager host on port 8501 (e.g. https://logrhythm-pm.example.com:8501, no trailing slash).",
                "Ensure this platform can reach the gateway over HTTPS and trusts its TLS certificate.",
                "Click 'Test Connection' to verify the token and URL.",
            ],
            "permissions": [
                {
                    "scope": "Case API",
                    "description": "List, read, create, update, change status, and annotate cases",
                },
                {
                    "scope": "Alarm API",
                    "description": "List and read alarms, update status / Risk Based Priority, comment, and read alarm events",
                },
                {
                    "scope": "Search API",
                    "description": "Submit and read indexed log/event searches for threat hunting",
                },
                {
                    "scope": "Administration API",
                    "description": "Read hosts and entities for asset and segmentation context",
                },
            ],
            "notes": (
                "LogRhythm authenticates with a third-party API token (a JWT generated "
                "via the Client Console's Third Party Applications registration) sent in "
                "the Authorization: Bearer header. The REST APIs are served by the "
                "Platform Manager API gateway over HTTPS on port 8501, so base_url is the "
                "gateway host (not the Web Console UI). All calls target {base_url} with "
                "the lr-case-api, lr-alarm-api, lr-search-api, and lr-admin-api paths. The "
                "token inherits the access granted to the registered application, which "
                "bounds what the agent can do. The gateway must present a TLS certificate "
                "trusted by this platform."
            ),
        },
    },
    "crowdstrike": {
        "name": "CrowdStrike Falcon",
        "description": "Endpoint security (EDR/XDR) — detections & alerts, CrowdScore incidents, host containment, Spotlight vulnerabilities, custom IOCs, threat intelligence (report PDFs stored as files), sandbox (detonate a file of the conversation, store IOC packs, PCAPs, memory strings, screenshots, and samples as password-protected ZIPs), and Real Time Response",
        "logo": "/logos/crowdstrike.svg",
        "category": "integration",
        "integration_category": "detection_response",
        "integration_types": ["edr", "xdr", "threat_intel", "soar"],
        # CrowdStrike authenticates with an OAuth2 client-credentials API key
        # (client ID + secret) — there is no interactive user sign-in, so the
        # only auth mode is "api_key".
        "auth_type": "api_key",
        "supported_auth_types": ["api_key"],
        "fields": ["client_id", "client_secret"],
        "extra_fields": {
            "cloud": "Falcon cloud region: us-1, us-2, eu-1, us-gov-1, or us-gov-2 (default us-1)",
        },
        "setup_guide": {
            "title": "CrowdStrike Falcon API Setup",
            "url": "https://falcon.crowdstrike.com/api-clients-and-keys/clients",
            "steps": [
                "In the Falcon console, go to Support and resources > API clients and keys (or Support > API Clients and Keys).",
                "Click 'Add new API client', give it a descriptive name (e.g. 'XTM One SOC'), and select the API scopes listed below.",
                "Click 'Create' and copy the generated Client ID and Client Secret — the secret is shown only once.",
                "Paste the Client ID and Client Secret in the fields below.",
                "Set the Cloud region to match your tenant: us-1 (api.crowdstrike.com), us-2 (api.us-2.crowdstrike.com), eu-1 (api.eu-1.crowdstrike.com), us-gov-1, or us-gov-2.",
                "Grant each scope at the Read level for visibility, and Write where you want the agent to take action (alert triage, host containment, IOC management, Real Time Response).",
            ],
            "permissions": [
                {
                    "scope": "Alerts (Read & Write)",
                    "description": "List, read, and triage detections/alerts (status, assignment, comments, tags)",
                },
                {
                    "scope": "Incidents (Read & Write)",
                    "description": "Read CrowdScore incidents and behaviors, and update incident status/tags/name",
                },
                {
                    "scope": "Hosts (Read)",
                    "description": "List and read managed host (device) inventory and details",
                },
                {
                    "scope": "Host Group (Read)",
                    "description": "List host groups",
                },
                {
                    "scope": "Real Time Response (Read & Write)",
                    "description": "Network-contain / lift containment on hosts and run read-only Real Time Response commands",
                },
                {
                    "scope": "Spotlight Vulnerabilities (Read)",
                    "description": "List Spotlight vulnerabilities with CVE and host context",
                },
                {
                    "scope": "IOC Management / IOCs (Read & Write)",
                    "description": "List, create, update, and delete custom indicators of compromise",
                },
                {
                    "scope": "Threat Intelligence — Actors, Indicators, Reports (Read)",
                    "description": "Read Falcon Intelligence adversaries, indicators, and finished intelligence reports, and download report PDFs (requires a Falcon Intelligence subscription)",
                },
                {
                    "scope": "Sandbox (Falcon Intelligence) (Read & Write)",
                    "description": "Submit URLs/samples for detonation, read sandbox reports and download their artifacts (requires Falcon Intelligence Sandbox)",
                },
                {
                    "scope": "Sample uploads (Read & Write)",
                    "description": "Upload a file of the conversation for detonation and download samples as password-protected ZIPs",
                },
            ],
            "notes": (
                "CrowdStrike Falcon uses an OAuth2 client-credentials API key (no user "
                "sign-in). Create the API client in the Falcon console and grant only the "
                "scopes you need — Read scopes for visibility and Write scopes for response "
                "actions (alert triage, host network containment, IOC management, Real Time "
                "Response). Real Time Response is restricted to read-only commands in this "
                "integration. The cloud region must match your tenant; if you authenticate "
                "against the wrong cloud the API returns an authentication error. Threat "
                "Intelligence and Sandbox tools require the corresponding Falcon "
                "Intelligence subscription. A sample is live malware: it is fetched only "
                "as CrowdStrike's ZIP encrypted with the password 'infected', stored as it "
                "came and never read by the platform."
            ),
        },
    },
    "crowdstrike_ngsiem": {
        "name": "CrowdStrike Next-Gen SIEM",
        "description": "Falcon Next-Gen SIEM (LogScale) - CQL threat hunting across endpoint telemetry and third-party logs, correlation rules (list/read, activate/deactivate), NGSIEM detections, saved queries, and lookup files (download as files, upload a file of the conversation such as an IOC list)",
        "logo": "/logos/crowdstrike.svg",
        "category": "integration",
        "integration_category": "siem_analytics",
        "integration_types": ["siem"],
        # Same Falcon API client as the endpoint provider: an OAuth2
        # client-credentials API key (client ID + secret) and a cloud region,
        # no interactive sign-in.
        "auth_type": "api_key",
        "supported_auth_types": ["api_key"],
        "fields": ["client_id", "client_secret"],
        "extra_fields": {
            "cloud": "Falcon cloud region: us-1, us-2, eu-1, us-gov-1, or us-gov-2 (default us-1)",
        },
        "setup_guide": {
            "title": "CrowdStrike Next-Gen SIEM API Setup",
            "url": "https://falcon.crowdstrike.com/api-clients-and-keys/clients",
            "steps": [
                "In the Falcon console, go to Support and resources > API clients and keys.",
                "Click 'Add new API client', give it a descriptive name (e.g. 'XTM One SIEM'), and select the API scopes listed below. An existing CrowdStrike Falcon API client works too once it has these scopes.",
                "Click 'Create' and copy the generated Client ID and Client Secret - the secret is shown only once.",
                "Paste the Client ID and Client Secret in the fields below.",
                "Set the Cloud region to match your tenant: us-1 (api.crowdstrike.com), us-2 (api.us-2.crowdstrike.com), eu-1 (api.eu-1.crowdstrike.com), us-gov-1, or us-gov-2.",
                "Click 'Test Connection': it checks the credentials and that the API client can read Next-Gen SIEM.",
            ],
            "permissions": [
                {
                    "scope": "NGSIEM (Read)",
                    "description": "Run CQL searches, list and read saved queries and lookup files, download lookup files",
                },
                {
                    "scope": "NGSIEM (Write)",
                    "description": "Upload lookup files (only needed for upload_crowdstrike_ngsiem_lookup_file)",
                },
                {
                    "scope": "Correlation Rules (Read & Write)",
                    "description": "List and read correlation rules (Read); activate or deactivate a rule (Write)",
                },
                {
                    "scope": "Alerts (Read)",
                    "description": "List and read the detections NGSIEM correlation rules raise",
                },
            ],
            "notes": (
                "CrowdStrike Next-Gen SIEM uses the same OAuth2 client-credentials API "
                "key as CrowdStrike Falcon (no user sign-in); grant only the scopes you "
                "need. Searches run as LogScale query jobs: a search that does not "
                "finish within its timeout (at most 4 minutes) is stopped and its "
                "partial results are returned, marked as partial. Uploading a lookup "
                "file under the name of an existing one replaces it. Lookup files move "
                "through the platform up to 50 MB."
            ),
        },
    },
    "hackerone": {
        "name": "HackerOne",
        "description": (
            "Vulnerability disclosure & bug bounty platform — query and triage "
            "reports, generate & download report PDFs, manage programs, "
            "organizations, assets (ASM), bounties, automations, and analytics"
        ),
        "logo": "/logos/hackerone.svg",
        "category": "integration",
        "integration_category": "exposure_management",
        "integration_types": ["vulnerability_disclosure", "bug_bounty", "asm"],
        "available_events": [],
        # HackerOne authenticates with an API token using HTTP Basic auth (the
        # token identifier as the username and the token value as the password),
        # so the only auth mode is "api_key".
        "auth_type": "api_key",
        "supported_auth_types": ["api_key"],
        "fields": ["api_key"],
        "extra_fields": {
            "api_username": (
                "API token identifier (the token's username — NOT your account "
                "email). Shown next to the token on the API settings page."
            ),
        },
        "setup_guide": {
            "title": "HackerOne API Token Setup",
            "url": "https://docs.hackerone.com/en/articles/8521756-api-tokens",
            "steps": [
                "Sign in to HackerOne as an Admin and open your organization's settings.",
                "Go to the API Tokens page (Settings > API Tokens) and click 'Create API Token'.",
                "Give the token a name and assign it a group whose role grants the access the agent needs (e.g. a read-only group for triage/reporting, or a group with report management to comment, change state, assign, and award bounties).",
                "Copy the token IDENTIFIER and paste it into the 'api_username' field below.",
                "Copy the token VALUE (shown only once) and paste it into the 'API Key' field below.",
                "Click 'Test Connection' to verify the credentials.",
            ],
            "permissions": [
                {
                    "scope": "Reports (read)",
                    "description": "List and read vulnerability reports and generate report PDFs",
                },
                {
                    "scope": "Report management (write)",
                    "description": "Comment, change state, assign, set severity, award bounties, and manage report metadata",
                },
                {
                    "scope": "Programs & organizations (read)",
                    "description": "Read program/organization details, members, balances, and analytics",
                },
                {
                    "scope": "Assets / ASM (read & write)",
                    "description": "Manage the attack-surface inventory, scopes, tags, and scanner configuration",
                },
            ],
            "notes": (
                "HackerOne uses HTTP Basic authentication with an API token: the "
                "token IDENTIFIER is the username and the token VALUE is the "
                "password. Both come from the API Tokens page — the username is "
                "NOT your account email. The token inherits the role/permissions "
                "of the group it is assigned to, which bounds what the agent can "
                "do (grant a read-only group for visibility, or a management "
                "group for triage actions). API tokens require a HackerOne "
                "Professional, Community, or Enterprise edition."
            ),
        },
    },
    "domaintools": {
        "name": "DomainTools",
        "description": (
            "Domain and DNS intelligence (DomainTools Iris + Lookups & "
            "Monitors + Iris Detect) - enrich and pivot on domains with risk "
            "scores and guided pivots (Iris Investigate/Enrich), WHOIS + "
            "parsed WHOIS + WHOIS history, hosting and domain change history, "
            "reverse lookups (IP, IP WHOIS, name server, WHOIS), brand/"
            "registrant/infrastructure monitors, and Iris Detect "
            "lookalike-domain discovery, triage, and escalation"
        ),
        "logo": "/logos/domaintools.svg",
        "category": "integration",
        "integration_category": "threat_intelligence",
        "integration_types": ["threat_intel", "asm"],
        # DomainTools authenticates with an API username + API key pair
        # (HMAC-signed requests) - there is no OAuth, so the only supported
        # auth mode is "api_key".
        "auth_type": "api_key",
        "supported_auth_types": ["api_key"],
        "fields": ["api_key"],
        "extra_fields": {
            "api_username": ("DomainTools API username (from the API dashboard - NOT your account email)"),
        },
        "setup_guide": {
            "title": "DomainTools API Credentials Setup",
            "url": "https://docs.domaintools.com/api/",
            "steps": [
                "Sign in to DomainTools at research.domaintools.com with the API owner account.",
                "Open the account menu and select 'API Admin' (or My Account > View API Dashboard) to open the API dashboard.",
                "In the API Product Information tab, copy the API Username and paste it into the 'api_username' field below.",
                "Copy the API Key from the same tab and paste it into the 'API Key' field below (use 'Reset API Key' if you need a fresh one - resetting immediately invalidates the old key).",
                "Click 'Test Connection' to verify the credentials (calls GET /v1/account/ and lists your licensed products).",
            ],
            "permissions": [
                {
                    "scope": "Iris Investigate / Iris Enrich",
                    "description": "Domain enrichment with risk scores and pivot searches (per-product monthly quotas)",
                },
                {
                    "scope": "Lookups (WHOIS, history, reverse)",
                    "description": "WHOIS + parsed WHOIS + WHOIS history, hosting/domain history, reverse IP/NS/WHOIS lookups, risk and reputation scores",
                },
                {
                    "scope": "Monitors",
                    "description": "Brand, registrant, IP, name server, and IP registrant daily alert feeds",
                },
                {
                    "scope": "Iris Detect (read + write)",
                    "description": "Lookalike-domain monitors and discovered domains; watch/ignore/escalate actions require the corresponding Detect permissions on the account",
                },
            ],
            "notes": (
                "DomainTools authenticates every request with an HMAC-SHA256 "
                "signature built from the API username, a timestamp, and the "
                "request path, keyed by the API key - the key itself is never "
                "sent on the wire. Each API product (Iris Investigate, Iris "
                "Enrich, Iris Detect, WHOIS History, monitors, ...) is licensed "
                "and metered separately: tools for unlicensed products return a "
                "clear permission error, and get_domaintools_account shows the "
                "products, quotas, and usage on the account. The test domains "
                "domaintools.com, dnsdb.info, and example.com do not consume "
                "quota on most lookup endpoints."
            ),
        },
    },
    "intel471": {
        "name": "Intel 471",
        "description": (
            "Cybercrime threat intelligence (Intel 471 Titan API) - threat "
            "actors and finished-intelligence reports (information reports, "
            "breach alerts, spot/situation reports, news), malware intelligence "
            "(indicators, YARA, events, malware reports/families), IOCs, "
            "vulnerability (CVE) intelligence, compromised credentials, "
            "underground forum/marketplace/data-leak monitoring, global search, "
            "and alert/watcher management"
        ),
        "logo": "/logos/intel471.svg",
        "category": "integration",
        "integration_category": "threat_intelligence",
        "integration_types": ["threat_intel"],
        "available_events": [],
        # Intel 471 (Titan) authenticates with HTTP Basic auth: the account
        # email as the username and a Titan API key as the password. There is
        # no OAuth, so the only supported auth mode is "api_key".
        "auth_type": "api_key",
        "supported_auth_types": ["api_key"],
        "fields": ["api_key"],
        "extra_fields": {
            "api_username": ("Titan account email address (the HTTP Basic username paired with your Titan API key)."),
        },
        "setup_guide": {
            "title": "Intel 471 Titan API Key Setup",
            "url": "https://titan.intel471.com/",
            "steps": [
                "Sign in to the Intel 471 Titan portal at titan.intel471.com.",
                "Open your profile (top-right profile icon) and go to the API section.",
                "Generate an API key (Titan API key) and copy it.",
                "Paste your Titan account EMAIL into the 'api_username' field below.",
                "Paste the Titan API KEY into the 'API Key' field below.",
                "Click 'Test Connection' to verify the credentials (reads one GIR from the catalogue).",
            ],
            "permissions": [
                {
                    "scope": "Adversary intelligence",
                    "description": "Actors, forum posts, private/instant messages, marketplaces, data leaks, reports, entities",
                },
                {
                    "scope": "Malware intelligence",
                    "description": "Indicators, YARA, events, malware reports and families",
                },
                {
                    "scope": "Credential & vulnerability intelligence",
                    "description": "Compromised credentials and credential sets, CVE (vulnerability) reports",
                },
                {
                    "scope": "Watchers & alerts (read + write)",
                    "description": "Create and manage watcher groups/watchers and consume the resulting alert feed",
                },
            ],
            "notes": (
                "Intel 471 authenticates every request with HTTP Basic auth: "
                "the account EMAIL is the username and a Titan API KEY is the "
                "password. Generate the key in the Titan portal under your "
                "profile's API section. Access to each collection is bounded by "
                "the Titan subscriptions on your account (adversary intelligence, "
                "malware intelligence, credentials, ...): tools for an "
                "unsubscribed collection return a clear permission error. The "
                "account has a daily request quota that resets at midnight GMT; "
                "near the cap the API throttles and then returns HTTP 429."
            ),
        },
    },
    "mattermost": {
        "name": "Mattermost",
        "description": (
            "Mattermost, the self-hosted team chat - post alerts in channels and "
            "threads, read a channel's messages, react, manage channels and their "
            "members, look up and invite users"
        ),
        "logo": "/logos/mattermost.svg",
        "category": "integration",
        "integration_category": "communication",
        "integration_types": ["messaging"],
        "available_events": [],
        # A bot account or personal access token, sent as a Bearer token.
        "auth_type": "api_key",
        "supported_auth_types": ["api_key"],
        "fields": ["api_key"],
        "extra_fields": {
            "base_url": "Mattermost URL (e.g. https://chat.company.com)",
        },
        "setup_guide": {
            "title": "Mattermost Bot Token Setup",
            "url": "https://developers.mattermost.com/integrate/reference/bot-accounts/",
            "steps": [
                "As a system admin, enable bot accounts (System Console > Integrations > Bot Accounts).",
                "Create a bot (Product menu > Integrations > Bot Accounts > Add Bot Account) with the 'post:all' or 'post:channels' permission it needs, and copy its access token. A personal access token of a dedicated user works too.",
                "Add the bot to the teams and channels it posts in.",
                "Paste the token in the 'API Key' field and set 'base_url' to your Mattermost URL.",
                "Click 'Test Connection' (calls GET /api/v4/users/me).",
            ],
            "permissions": [
                {
                    "scope": "Messages and reactions",
                    "description": "Post in channels and threads, post ephemeral messages, read channel messages, react, delete posts",
                },
                {
                    "scope": "Channels and users (team or system admin for some)",
                    "description": "Search, create, archive and restore channels, add members, list and read users, deactivate users, invite by email",
                },
            ],
            "notes": (
                "The token acts as the bot or user it belongs to: it reads only the channels "
                "that account is a member of. The token is stored encrypted here and only "
                "sent to the configured Mattermost URL."
            ),
        },
    },
    "kubernetes": {
        "name": "Kubernetes",
        "description": (
            "Kubernetes, read-only - a cluster's services with their type, cluster IP, "
            "ports, node ports and external IPs, its namespaces and its nodes' addresses"
        ),
        "logo": "/logos/kubernetes.svg",
        "category": "integration",
        "integration_category": "exposure_management",
        "integration_types": ["cloud_security"],
        "available_events": [],
        # A service account token, sent as a Bearer token.
        "auth_type": "api_key",
        "supported_auth_types": ["api_key"],
        "fields": ["api_key"],
        "extra_fields": {
            "base_url": "API server URL (e.g. https://k8s.company.com:6443, the 'server' of your kubeconfig)",
            "ca_cert": "Optional - the cluster CA certificate (PEM, the base64-decoded 'certificate-authority-data' of your kubeconfig) when the API server's certificate is not publicly signed",
        },
        "extra_field_required_when": {"base_url": {}},
        "setup_guide": {
            "title": "Kubernetes Read-only Service Account Setup",
            "url": "https://kubernetes.io/docs/reference/access-authn-authz/service-accounts-admin/",
            "steps": [
                "Create a service account, e.g. 'kubectl create serviceaccount copilot-reader -n kube-system'.",
                "Create a ClusterRole with get and list on services, namespaces and nodes only, and bind it to the service account with a ClusterRoleBinding.",
                "Create a token for it, e.g. 'kubectl create token copilot-reader -n kube-system --duration=8760h' (or a long-lived Secret of type kubernetes.io/service-account-token).",
                "Paste the token in the 'API Key' field, the API server URL in 'base_url' and, for a private CA, the cluster CA certificate (PEM) in 'ca_cert'.",
                "Click 'Test Connection' (lists one namespace).",
            ],
            "permissions": [
                {
                    "scope": "get, list on services, namespaces, nodes (ClusterRole)",
                    "description": "List services with their ports and node ports, namespaces, and the nodes' addresses. Nothing is written.",
                },
            ],
            "notes": (
                "Every tool is a read. The token is stored encrypted here and only sent to the "
                "configured API server, whose certificate is verified against the pasted CA or "
                "the public roots, never skipped."
            ),
        },
    },
    "zendesk": {
        "name": "Zendesk",
        "description": (
            "Zendesk Support - remediation and incident tickets: search, read with "
            "comments, open, update (status, priority, assignee, tags, reply or "
            "internal note), delete and restore; ticket fields, users and organizations"
        ),
        "logo": "/logos/zendesk.svg",
        "category": "integration",
        "integration_category": "business_operations",
        "integration_types": ["ticketing"],
        "available_events": [],
        # An API token for an agent: HTTP Basic "<email>/token:<api_token>".
        "auth_type": "api_key",
        "supported_auth_types": ["api_key"],
        "fields": ["api_key"],
        "extra_fields": {
            "subdomain": "Zendesk subdomain (acme for https://acme.zendesk.com)",
            "email": "Email of the Zendesk agent the API token is used as",
        },
        "setup_guide": {
            "title": "Zendesk API Token Setup",
            "url": "https://support.zendesk.com/hc/en-us/articles/4408889192858",
            "steps": [
                "In Zendesk Admin Center, open Apps and integrations > APIs > Zendesk API and enable Token access.",
                "Click 'Add API token', name it and copy it (it is shown once).",
                "Paste the token in the 'API Key' field.",
                "Set 'subdomain' to your account's subdomain and 'email' to the Zendesk agent the changes are recorded as (a dedicated agent seat is recommended).",
                "Click 'Test Connection' (calls GET /api/v2/tickets/count.json).",
            ],
            "permissions": [
                {
                    "scope": "Tickets (read + write)",
                    "description": "Search, read, create, update, delete and restore tickets; read ticket fields",
                },
                {
                    "scope": "Users and organizations (read + write, admin for some changes)",
                    "description": "Search, read, create, update and delete users and organizations",
                },
            ],
            "notes": (
                "An API token acts with the role of the agent whose email goes with it. "
                "Comments added by update_zendesk_ticket are internal notes unless the "
                "agent asks for a public reply. The token is stored encrypted here."
            ),
        },
    },
    "gitlab": {
        "name": "GitLab",
        "description": (
            "GitLab (GitLab.com or self-managed) - remediation issues (list, read, open, "
            "edit, close, comment), merge requests, repository files (read, commit, "
            "delete) and releases"
        ),
        "logo": "/logos/gitlab.svg",
        "category": "integration",
        "integration_category": "development",
        "integration_types": ["repository", "ticketing"],
        "available_events": [],
        # A personal, project or group access token in the PRIVATE-TOKEN header.
        "auth_type": "api_key",
        "supported_auth_types": ["api_key"],
        "fields": ["api_key"],
        "extra_fields": {
            "base_url": ("Self-managed GitLab URL (e.g. https://gitlab.company.com); leave empty for GitLab.com"),
        },
        "setup_guide": {
            "title": "GitLab Access Token Setup",
            "url": "https://docs.gitlab.com/user/profile/personal_access_tokens/",
            "steps": [
                "In GitLab, create an access token: a project or group access token (Settings > Access tokens) for a service account, or a personal access token (Preferences > Access tokens).",
                "Give it the 'api' scope (or 'read_api' for read-only use) and a role able to edit issues (Reporter or above) or push files (Developer or above).",
                "Paste the token in the 'API Key' field.",
                "For a self-managed instance, set 'base_url' to its URL (without /api/v4).",
                "Click 'Test Connection' (calls GET /api/v4/user).",
            ],
            "permissions": [
                {
                    "scope": "api",
                    "description": "Read and write issues, notes, merge request comments, repository files and releases",
                },
                {
                    "scope": "read_api",
                    "description": "Read-only use: projects, issues, merge requests, files, releases",
                },
            ],
            "notes": (
                "The token acts with its owner's role on each project. File tools commit "
                "directly to the branch given. The token is stored encrypted here and only "
                "sent to the configured GitLab instance."
            ),
        },
    },
    "pagerduty": {
        "name": "PagerDuty",
        "description": (
            "PagerDuty incident response - page on-call responders for critical "
            "exposures: open, acknowledge, escalate, reassign and resolve incidents, "
            "add notes, read log entries, services, escalation policies and who is on call"
        ),
        "logo": "/logos/pagerduty.svg",
        "category": "integration",
        "integration_category": "observability",
        "integration_types": ["incident_management"],
        "available_events": [],
        # A REST API key sent as ``Authorization: Token token=<key>``; no OAuth.
        "auth_type": "api_key",
        "supported_auth_types": ["api_key"],
        "fields": ["api_key"],
        "extra_fields": {
            "region": "Service region: 'us' (default, api.pagerduty.com) or 'eu' (api.eu.pagerduty.com)",
            "from_email": (
                "Email of a PagerDuty user of the account - required by PagerDuty on every "
                "incident change (create, update, note)"
            ),
        },
        "setup_guide": {
            "title": "PagerDuty API Key Setup",
            "url": "https://support.pagerduty.com/main/docs/api-access-keys",
            "steps": [
                "In PagerDuty, open Integrations > API Access Keys (account admin), or My Profile > User Settings > Create API User Token for a key limited to one user's permissions.",
                "Create a key; leave 'Read-only API Key' unticked if agents should open, acknowledge or resolve incidents.",
                "Paste the key in the 'API Key' field.",
                "Set 'region' to 'eu' if your account is hosted in the EU service region (its URL ends in .eu.pagerduty.com).",
                "Set 'from_email' to the email of the PagerDuty user the changes are recorded as.",
                "Click 'Test Connection' (calls GET /users?limit=1).",
            ],
            "permissions": [
                {
                    "scope": "Incidents (read + write)",
                    "description": "List and read incidents, their notes and log entries; open, acknowledge, escalate, reassign and resolve incidents; add notes",
                },
                {
                    "scope": "Services, escalation policies, on-calls, users (read)",
                    "description": "Find the service to page and who is on call",
                },
            ],
            "notes": (
                "A read-only API key keeps every read tool; the incident changes then fail "
                "with PagerDuty's 403. The key is stored encrypted here."
            ),
        },
    },
    "securityscorecard": {
        "name": "SecurityScorecard",
        "description": (
            "SecurityScorecard security ratings - a company's score and grade, the "
            "ten risk factors behind it and the findings of each issue type (exposed "
            "hosts, ports, products, CVEs), score history, industry benchmarks, "
            "vendor portfolios and reports"
        ),
        "logo": "/logos/securityscorecard.svg",
        "category": "integration",
        "integration_category": "exposure_management",
        "integration_types": ["asm", "third_party_risk"],
        "available_events": [],
        # An API token sent as ``Authorization: Token <token>``; no OAuth.
        "auth_type": "api_key",
        "supported_auth_types": ["api_key"],
        "fields": ["api_key"],
        "setup_guide": {
            "title": "SecurityScorecard API Token Setup",
            "url": "https://securityscorecard.readme.io/reference/authentication",
            "steps": [
                "Sign in to the SecurityScorecard platform (platform.securityscorecard.io).",
                "Open My Settings > API, or create a bot user under Settings > Users for an unattended agent.",
                "Generate an API token and copy it (it is shown once).",
                "Paste the token in the 'API Key' field.",
                "Click 'Test Connection' (calls GET https://api.securityscorecard.io/portfolios).",
            ],
            "permissions": [
                {
                    "scope": "Scorecards (read)",
                    "description": "Read scores, factors, issue findings, score and factor history, industry scores",
                },
                {
                    "scope": "Portfolios (read + write)",
                    "description": "List portfolios and their companies, create and delete portfolios, add and remove companies",
                },
                {
                    "scope": "Reports (read + write)",
                    "description": "List recent reports and generate new ones",
                },
            ],
            "notes": (
                "The token acts as the user who generated it: a bot user with a "
                "scoped role is recommended for agents. All calls go to "
                "https://api.securityscorecard.io. The token is stored encrypted here."
            ),
        },
    },
    "tenable": {
        "name": "Tenable",
        "description": "Vulnerability management (Tenable Vulnerability Management) — aggregated vulnerabilities (workbenches), plugin/CVE detail, assets and their exposure, bulk vulnerability export for environment-wide hunts (the chunk stored as a JSON file), scans (list/get/launch/pause/resume/stop, scan reports exported as PDF / HTML / CSV / .nessus files, .nessus files of the conversation imported as scans), scanners, and tags",
        "logo": "/logos/tenable.svg",
        "category": "integration",
        "integration_category": "exposure_management",
        "integration_types": ["vulnerability_management", "asm"],
        # Tenable authenticates with an API key pair (access + secret) — there is
        # no OAuth, so the only supported auth mode is "api_key".
        "auth_type": "api_key",
        "supported_auth_types": ["api_key"],
        "fields": ["api_key"],
        "extra_fields": {
            "base_url": "Tenable cloud URL (default https://cloud.tenable.com; FedRAMP: https://fedcloud.tenable.com)",
            "access_key": "Tenable API Access Key (the paired identifier for the Secret Key)",
        },
        "setup_guide": {
            "title": "Tenable API Keys Setup",
            "url": "https://developer.tenable.com/docs/api-access",
            "steps": [
                "Log in to Tenable Vulnerability Management at cloud.tenable.com.",
                "We recommend creating a dedicated service user (Settings > Access Control > Users) with a role granting only the access the agent needs (e.g. a 'Basic'/'Can View' role for read-only visibility, or 'Scan Operator'/'Scan Manager' for launching scans).",
                "Sign in as that user, click the user profile icon (top-right) > My Account > API Keys, then click 'Generate'.",
                "Copy the Access Key and Secret Key (shown only once). Paste the Secret Key into the 'API Key' field below and the Access Key into the 'access_key' field.",
                "Leave 'base_url' as https://cloud.tenable.com unless you are on FedRAMP (https://fedcloud.tenable.com).",
                "Click 'Test Connection' to verify the keys.",
            ],
            "permissions": [
                {
                    "scope": "Can View (Vulnerabilities & Assets)",
                    "description": "List vulnerabilities (workbenches) and assets, get plugin/CVE detail, and run bulk vulnerability exports",
                },
                {
                    "scope": "Can View (Scans)",
                    "description": "List and read scans, scanners, and tags, and export scan reports",
                },
                {
                    "scope": "Can Use / Scan Operator (Scans)",
                    "description": "Launch scans, pause/resume/stop running scans, and import scan files",
                },
            ],
            "notes": (
                "Tenable authenticates with an API key pair sent in the X-ApiKeys header "
                "(accessKey + secretKey) — there is no OAuth. Each user account has a single "
                "key pair, and generating a new pair invalidates the previous one, so use a "
                "dedicated service user. The keys inherit that user's role and permissions, "
                "which bound what the agent can do (a read-only role is enough for visibility "
                "and hunting; launching scans requires a scan-capable role)."
            ),
        },
    },
    "qualys": {
        "name": "Qualys",
        "description": "Vulnerability management (Qualys VMDR) - host vulnerability detections with severity/QID/IP filters, KnowledgeBase QID detail (CVSS v2/v3, CVEs, diagnosis, solution), hosts/assets, scans (list, fetch results, launch, pause/resume/cancel/delete), asset groups, scanner appliances, and reports",
        "logo": "/logos/qualys.svg",
        "category": "integration",
        "integration_category": "exposure_management",
        "integration_types": ["vulnerability_management"],
        # Qualys' classic VM API authenticates with HTTP Basic (username +
        # password) - there is no OAuth, so the only supported auth mode is
        # "api_key" (the account password goes in the API Key field).
        "auth_type": "api_key",
        "supported_auth_types": ["api_key"],
        "fields": ["api_key"],
        "extra_fields": {
            "base_url": (
                "Qualys API server URL for your platform/POD (NOT the web UI host) - "
                "e.g. US1 https://qualysapi.qualys.com, US2 "
                "https://qualysapi.qg2.apps.qualys.com, EU1 "
                "https://qualysapi.qg1.apps.qualys.eu"
            ),
            "username": "Qualys API username",
        },
        "setup_guide": {
            "title": "Qualys VMDR API Setup",
            "url": "https://docs.qualys.com/en/vm/api/",
            "steps": [
                "Sign in to the Qualys UI and identify your platform/POD - the API server URL differs per platform (the Qualys 'Platform Identification' / API server list maps each platform to its API server, e.g. US Platform 1 = https://qualysapi.qualys.com, US Platform 2 = https://qualysapi.qg2.apps.qualys.com, EU Platform 1 = https://qualysapi.qg1.apps.qualys.eu).",
                "We recommend a dedicated API service account. In the Qualys UI go to Users, add a user, and assign a role with API access (a Reader or Scanner role with 'API Access' enabled is enough for visibility; a Scanner or Manager role is required to launch scans).",
                "Important: the API server host (qualysapi.*) is NOT the web UI host (qualysguard.*) - use the API server URL for base_url.",
                "Enter the API server URL in 'base_url', the API username in 'username', and the account password in the 'API Key' field below.",
                "Click 'Test Connection' to verify the credentials.",
            ],
            "permissions": [
                {
                    "scope": "API Access (Reader / Scanner role)",
                    "description": "Read host detections, the KnowledgeBase, hosts, scans, asset groups, scanner appliances, and reports",
                },
                {
                    "scope": "Scanner / Manager role",
                    "description": "Launch VM scans and pause/resume/cancel/delete them",
                },
            ],
            "notes": (
                "Qualys' classic VM API (/api/2.0/fo/) uses HTTP Basic authentication "
                "(username + password) and REQUIRES the X-Requested-With header on "
                "every request - this integration sends it automatically. Responses "
                "are XML. There is no OAuth; the API user's role bounds what the agent "
                "can do (a read-only role is enough for visibility and hunting, a "
                "scan-capable role is needed to launch scans). The API server URL is "
                "platform/POD-specific and differs from the web UI host, so set "
                "base_url to the API server for your subscription."
            ),
        },
    },
    "sumologic": {
        "name": "Sumo Logic",
        "description": "Log analytics + Cloud SIEM - asynchronous log search (Search Job API) for threat hunting, Cloud SIEM (CSE) insights (list/get/set status/comment), signals and entities (list/get), threat-intel sources and indicators (list/create IOCs), monitors, and collectors",
        "logo": "/logos/sumologic.svg",
        "category": "integration",
        "integration_category": "siem_analytics",
        "integration_types": ["siem", "threat_intel"],
        # Sumo Logic authenticates with an Access ID + Access Key pair sent as
        # HTTP Basic auth - there is no OAuth, so the only supported mode is
        # "api_key" (the Access Key is the secret; the Access ID is paired).
        "auth_type": "api_key",
        "supported_auth_types": ["api_key"],
        "fields": ["api_key"],
        "extra_fields": {
            "base_url": "Sumo Logic API endpoint for your deployment, ending in /api (default https://api.sumologic.com/api for US1; e.g. https://api.eu.sumologic.com/api for EU)",
            "access_id": "Sumo Logic Access ID (the paired identifier for the Access Key)",
        },
        "setup_guide": {
            "title": "Sumo Logic Access Key Setup",
            "url": "https://help.sumologic.com/docs/manage/security/access-keys/",
            "steps": [
                "Log in to Sumo Logic. We recommend creating a dedicated service account and a role granting only the access the agent needs (Search Job API needs 'Search'/'Download Search Results' and 'View Collectors'; Cloud SIEM tools need a role with Cloud SIEM capabilities).",
                "Signed in as that user, open the user menu (top-right) > Preferences > My Access Keys, then click '+ Add Access Key'.",
                "Name the key and click 'Create'. Copy the Access ID and Access Key (the Access Key is shown only once). Paste the Access Key into the 'API Key' field below and the Access ID into the 'access_id' field.",
                "Set 'base_url' to your deployment's API endpoint (it must end in /api). Find your deployment with the 'Which endpoint should I use?' page. Common endpoints: US1 https://api.sumologic.com/api, US2 https://api.us2.sumologic.com/api, EU https://api.eu.sumologic.com/api, AU https://api.au.sumologic.com/api, CA https://api.ca.sumologic.com/api, DE https://api.de.sumologic.com/api, JP https://api.jp.sumologic.com/api, IN https://api.in.sumologic.com/api, FED https://api.fed.sumologic.com/api.",
                "Click 'Test Connection' to verify the credentials.",
            ],
            "permissions": [
                {
                    "scope": "Search (Search Job API)",
                    "description": "Run log searches (run_sumologic_search) - requires an Enterprise account",
                },
                {
                    "scope": "Cloud SIEM (CSE capabilities)",
                    "description": "List/get insights, signals, entities; set insight status; comment; manage threat-intel indicators - requires Cloud SIEM Enterprise",
                },
                {
                    "scope": "View Collectors / View Monitors",
                    "description": "List collectors and monitors",
                },
            ],
            "notes": (
                "Sumo Logic authenticates with an Access ID + Access Key pair sent "
                "as HTTP Basic auth (Authorization: Basic base64(accessId:accessKey)) "
                "- there is no OAuth. The keys inherit the creating user's role, "
                "which bounds what the agent can do, so use a dedicated service "
                "account. The base URL is region-specific and must end in /api. The "
                "Cloud SIEM (CSE) tools require a Cloud SIEM Enterprise subscription "
                "and a role with Cloud SIEM capabilities."
            ),
        },
    },
    "servicenow": {
        "name": "ServiceNow",
        "description": (
            "ITSM & operations - triage and update incidents, changes, and "
            "problems; manage the unified task queue; search the CMDB (CIs + "
            "relationships) and knowledge base; resolve users/groups; order "
            "Service Catalog items; and reach any table via the Table API "
            "(query/get/count/create/update/delete) plus attachments"
        ),
        "logo": "/logos/servicenow.svg",
        "category": "integration",
        "integration_category": "business_operations",
        "integration_types": ["itsm", "ticketing", "cmdb"],
        # ServiceNow's REST API authenticates with HTTP Basic (username +
        # password, ideally a dedicated integration user). OAuth 2.0 also exists
        # but Basic with a service account is the common, simplest setup, so the
        # only supported mode is "api_key" (the account password goes in the API
        # Key field).
        "auth_type": "api_key",
        "supported_auth_types": ["api_key"],
        "fields": ["api_key"],
        "extra_fields": {
            "base_url": ("ServiceNow instance URL, e.g. https://acme.service-now.com (this is also the web UI host)"),
            "username": "ServiceNow username (a dedicated integration user)",
        },
        "setup_guide": {
            "title": "ServiceNow REST API Setup",
            "url": "https://www.servicenow.com/docs/bundle/xanadu-api-reference/page/integrate/inbound-rest/concept/c_TableAPI.html",
            "steps": [
                "Identify your instance URL - https://<instance>.service-now.com (the host you sign in to).",
                "We strongly recommend a dedicated integration user (User Administration > Users > New) rather than a personal account.",
                "Assign that user the roles the agent needs: 'itil' for incident/change/problem/task read+write; 'sn_request_read'/'catalog' for Service Catalog; 'knowledge' for KB read; CMDB roles (e.g. 'cmdb_read'/'asset') for configuration items. Grant only what is required.",
                "Set 'Web service access only' on the integration user so it cannot log into the UI, and use a strong password.",
                "Enter the instance URL in 'base_url', the integration username in 'username', and that user's password in the 'API Key' field below.",
                "Click 'Test Connection' to verify the credentials.",
            ],
            "permissions": [
                {
                    "scope": "itil",
                    "description": "Read/write incidents, changes, problems, and tasks (the ITSM core)",
                },
                {
                    "scope": "catalog / sn_request_read",
                    "description": "Browse and order Service Catalog items and read the resulting requests",
                },
                {
                    "scope": "knowledge",
                    "description": "Search and read knowledge base articles",
                },
                {
                    "scope": "cmdb_read / asset",
                    "description": "Read configuration items and their relationships",
                },
            ],
            "notes": (
                "ServiceNow authenticates with HTTP Basic (username + password). "
                "There is no default instance URL - it is specific to your "
                "subscription (https://<instance>.service-now.com) and doubles as "
                "the web UI host, so the agent can build record deep links. The "
                "integration user's roles bound exactly what the agent can read "
                "and write, so scope a dedicated service account to least "
                "privilege. The generic table tools (query/manage_servicenow_record) "
                "can reach any table the user has access to, including custom "
                "(x_*) applications."
            ),
        },
    },
    "sentinelone": {
        "name": "SentinelOne",
        "description": "Endpoint security (EDR/XDR) — threats (mitigate, verdict, notes), endpoints (isolate, scan), Deep Visibility hunting, threat intelligence IOCs, blocklist, exclusions, activities, and application vulnerabilities",
        "logo": "/logos/sentinelone.svg",
        "category": "integration",
        "integration_category": "detection_response",
        "integration_types": ["edr", "xdr", "threat_intel", "soar"],
        # SentinelOne authenticates with a management API token (no OAuth) — the
        # only supported auth mode is "api_key".
        "auth_type": "api_key",
        "supported_auth_types": ["api_key"],
        "fields": ["api_key"],
        "extra_fields": {
            "base_url": "Management console URL, e.g. https://yourcompany.sentinelone.net",
        },
        "setup_guide": {
            "title": "SentinelOne API Token Setup",
            "url": "https://www.sentinelone.com/",
            "steps": [
                "Log in to your SentinelOne management console.",
                "We recommend creating a dedicated service user (Settings > Users) so the token is not tied to a personal account, then assign it a role with the scopes you need (e.g. Admin / IR Team / SOC for response actions, or a read-only role for visibility only).",
                "Open the user's profile (top-right user menu > My User, or Settings > Users > the service user) and, under 'API Token', click 'Generate API Token'.",
                "Copy the token — it is shown only once. Paste it into the API Token field below.",
                "Set the Base URL to your management console URL (e.g. https://yourcompany.sentinelone.net). The API is served from the same host under /web/api/v2.1.",
            ],
            "permissions": [
                {
                    "scope": "Threats (View)",
                    "description": "List and read threats, timelines, and notes",
                },
                {
                    "scope": "Threats (Mitigate / Edit)",
                    "description": "Mitigate threats (kill/quarantine/remediate), set analyst verdict and incident status, add notes",
                },
                {
                    "scope": "Endpoints / Agents (View)",
                    "description": "List and read agents and their installed applications",
                },
                {
                    "scope": "Endpoint Actions (Disconnect/Connect/Scan)",
                    "description": "Network-isolate / reconnect agents and initiate full disk scans",
                },
                {
                    "scope": "Deep Visibility (SDL Search / View)",
                    "description": "Run Deep Visibility threat-hunting queries (requires Complete SKU)",
                },
                {
                    "scope": "Threat Intelligence / IOCs (View & Edit)",
                    "description": "List, create, and delete Threat Intelligence database IOCs",
                },
                {
                    "scope": "Blocklist & Exclusions (View & Edit)",
                    "description": "List exclusions and manage black-hash blocklist items",
                },
                {
                    "scope": "Activity (View)",
                    "description": "Read the console activity / audit log",
                },
            ],
            "notes": (
                "SentinelOne uses a management API token (Authorization: ApiToken <token>) "
                "against your tenant-specific console URL — there is no OAuth for the "
                "management API. Use a service-user token scoped to a role with only the "
                "permissions you need; response actions (mitigate, isolate, scan, blocklist) "
                "require a role such as Admin, IR Team, or SOC, while a read-only role is "
                "enough for visibility and hunting. Deep Visibility hunting requires the "
                "Complete SKU. The token is shown only once when generated."
            ),
        },
    },
    "harfanglab": {
        "name": "HarfangLab",
        "description": "Endpoint security (EDR) — security events (triage, status), threats (details, notes, status), endpoints (search, isolate/deisolate), telemetry hunting (processes, network, DNS, authentications), IOC management, and investigation jobs",
        "logo": "/logos/harfanglab.svg",
        "category": "integration",
        "integration_category": "detection_response",
        "integration_types": ["edr", "threat_intel", "soar"],
        # HarfangLab authenticates with an API token (no OAuth) — the only
        # supported auth mode is "api_key".
        "auth_type": "api_key",
        "supported_auth_types": ["api_key"],
        "fields": ["api_key"],
        "extra_fields": {
            "base_url": "Manager URL, e.g. https://edr.yourcompany.com",
        },
        "setup_guide": {
            "title": "HarfangLab API Token Setup",
            "url": "https://harfanglab.io/",
            "steps": [
                "Log in to your HarfangLab manager console (self-hosted or cloud).",
                "We recommend creating a dedicated service user so the token is not tied to a personal account, then assign it a role with the permissions you need (response actions require isolation and job rights; a read-only role is enough for visibility).",
                "Open your user menu and generate an API token (non-expirable for unattended use), or POST to /api/user/api_token/ with your credentials.",
                "Copy the token and paste it into the API Token field below.",
                "Set the Base URL to your manager URL (e.g. https://edr.yourcompany.com). The API is served from the same host under /api.",
            ],
            "permissions": [
                {
                    "scope": "Security events (View & Edit)",
                    "description": "List and read security events (alerts) and update their status",
                },
                {
                    "scope": "Threats (View & Edit)",
                    "description": "List and read threats, update status, and manage analyst notes",
                },
                {
                    "scope": "Endpoints / Agents (View)",
                    "description": "List and read agents, policies, and isolation state",
                },
                {
                    "scope": "Endpoint response (Isolate/Deisolate)",
                    "description": "Network-isolate and deisolate endpoints (the agent policy must allow isolation)",
                },
                {
                    "scope": "Telemetry & Hunting (View)",
                    "description": "Search telemetry (processes, network, DNS, event logs, authentications) and hunting data",
                },
                {
                    "scope": "Threat Intelligence / IOCs (View & Edit)",
                    "description": "List IOC sources, search, add, and delete IOC rules",
                },
                {
                    "scope": "Investigation jobs (Create & View)",
                    "description": "Run artifact-collection / hunt jobs on endpoints and read their results",
                },
            ],
            "notes": (
                "HarfangLab (Hurukai) uses an API token (Authorization: Token <token>) "
                "against your manager URL — there is no OAuth for the manager API. Use "
                "a service-user token scoped to a role with only the permissions you "
                "need; response actions (isolate, IOC management, jobs) require write "
                "roles, while a read-only role is enough for triage and hunting. "
                "Endpoint isolation additionally requires the agent's policy to allow "
                "it. HarfangLab EDR is ANSSI-certified and can be deployed self-hosted "
                "or in the cloud; both expose the same API."
            ),
        },
    },
    "attackiq": {
        "name": "AttackIQ",
        "description": "Breach & Attack Simulation / CTEM — assessments (run, status, runs), tests (master jobs), scenarios, validation results and phase results, on-prem assets/test points, tags, detection rules, and platform connectors",
        "logo": "/logos/attackiq.svg",
        "category": "integration",
        "integration_category": "adversarial_validation",
        "integration_types": ["adversarial_validation", "ctem"],
        # AttackIQ authenticates with an account API token (no OAuth) — the
        # only supported auth mode is "api_key".
        "auth_type": "api_key",
        "supported_auth_types": ["api_key"],
        "fields": ["api_key"],
        "extra_fields": {
            "base_url": "Tenant instance URL, e.g. https://yourcompany.attackiq.com",
        },
        "setup_guide": {
            "title": "AttackIQ API Token Setup",
            "url": "https://www.attackiq.com/",
            "steps": [
                "Log in to your AttackIQ platform tenant (https://<yourcompany>.attackiq.com).",
                "Click your profile in the top-right corner and open 'Account Token' (or Settings > Users > your user > API Token).",
                "Click 'Generate' / 'Copy Token to Clipboard' and save the token — it is shown only once. Paste it into the API Token field below.",
                "Set the Base URL to your tenant instance URL (e.g. https://yourcompany.attackiq.com). The REST API is served from the same host under /v1 and /v2.",
            ],
            "permissions": [
                {
                    "scope": "Assessments (View)",
                    "description": "List, search, and read assessments, tests, scenarios, results, and assets",
                },
                {
                    "scope": "Assessments (Run / Edit)",
                    "description": "Run assessments, activate, create from template, assign assets/tags, and delete",
                },
                {
                    "scope": "Tags (View & Edit)",
                    "description": "List tags and create/delete/apply/remove tag assignments",
                },
                {
                    "scope": "Detection Rules & Connectors (View)",
                    "description": "Read unified mitigation detection rules and platform connectors",
                },
            ],
            "notes": (
                "AttackIQ authenticates with an account API token sent as "
                "'Authorization: Token <token>' (not Bearer) against your "
                "tenant instance URL — there is no OAuth for the REST API. The "
                "token inherits the generating user's role, so use a "
                "least-privilege service user: a read-only role is enough for "
                "visibility, while running assessments and editing assets/tags "
                "requires a role with the corresponding write permissions. The "
                "tenant base URL doubles as the web UI host, so the agent can "
                "build assessment deep links."
            ),
        },
    },
    "elastic_security": {
        "name": "Elastic Security",
        "description": "SIEM + endpoint security (Elastic Stack) — detection alerts, rules, cases, exception/value lists, Elastic Defend endpoint response, and ES|QL / EQL / Query DSL threat hunting",
        "logo": "/logos/elastic_security.svg",
        "category": "integration",
        "integration_category": "siem_analytics",
        "integration_types": ["siem", "edr", "xdr", "threat_intel", "soar"],
        # Elastic authenticates with an Elasticsearch API key — there is no
        # OAuth, so the only supported auth mode is "api_key".
        "auth_type": "api_key",
        "supported_auth_types": ["api_key"],
        "fields": ["api_key"],
        "extra_fields": {
            "base_url": "Kibana URL, e.g. https://my-deployment.kb.eu-west-1.aws.cloud.es.io",
            "es_url": "Elasticsearch URL for hunting (ES|QL/EQL/_search), e.g. https://my-deployment.es.eu-west-1.aws.cloud.es.io (optional)",
            "space_id": "Kibana space ID (optional, defaults to 'default')",
        },
        "setup_guide": {
            "title": "Elastic Security API Key Setup",
            "url": "https://www.elastic.co/docs/api/doc/kibana/group/endpoint-security-detections-api",
            "steps": [
                "In Kibana, go to Stack Management > Security > API keys (or Management > API keys) and click 'Create API key'.",
                "Give the key a name (e.g. 'XTM One SOC') and assign role descriptors / privileges for the Security features you need — Detections, Cases, and (for response actions) Elastic Defend / endpoint privileges. A read-only key is enough for visibility and hunting.",
                "Create the key and copy its 'Encoded' value (the base64 string). Paste it into the API Key field below.",
                "Set the Base URL to your Kibana URL (e.g. https://<deployment>.kb.<region>.cloud.es.io).",
                "Optionally set the Elasticsearch URL (es_url) to enable ES|QL / EQL / Query DSL threat hunting (e.g. https://<deployment>.es.<region>.cloud.es.io).",
                "If your Security data lives in a non-default Kibana space, set the Space ID; otherwise leave it as 'default'.",
            ],
            "permissions": [
                {
                    "scope": "Security: Detections (Read/All)",
                    "description": "List and read detection alerts and rules; set alert status, tags, assignees; enable/disable rules",
                },
                {
                    "scope": "Security: Cases (Read/All)",
                    "description": "List, read, create, comment, and update cases",
                },
                {
                    "scope": "Security: Endpoint / Elastic Defend (Read + Response actions)",
                    "description": "List endpoints and run response actions (isolate, release, running processes)",
                },
                {
                    "scope": "Lists & Exceptions (Read/All)",
                    "description": "List exception lists/items and value lists, and add value-list items for IOC enforcement",
                },
                {
                    "scope": "Elasticsearch: read indices (for hunting)",
                    "description": "Run ES|QL, EQL, and Query DSL (_search) queries against your data for threat hunting (requires the Elasticsearch URL)",
                },
            ],
            "notes": (
                "Elastic Security uses an Elasticsearch API key (Authorization: ApiKey "
                "<encoded>) — there is no OAuth. Create the key in Kibana and grant only "
                "the Security privileges you need; response actions (isolate/release) "
                "require Elastic Defend endpoint privileges, while a read-only key is "
                "enough for visibility and hunting. Security operations use the Kibana "
                "APIs (space-aware via the Space ID); ES|QL / EQL / Query DSL hunting "
                "uses the Elasticsearch APIs and needs the optional Elasticsearch URL. "
                "The encoded API key is shown only once when generated."
            ),
        },
    },
    "google_secops": {
        "name": "Google SecOps",
        "description": "Google Security Operations (formerly Chronicle) SIEM + SOAR — UDM threat hunting, entity lookup, IoC matches, case management (list/update/assign/close/reopen), alerts with their UDM events and triage feedback, YARA-L detection rules, retrohunts, and reference lists",
        "logo": "/logos/google_secops.svg",
        "category": "integration",
        "integration_category": "siem_analytics",
        "integration_types": ["siem", "threat_intel", "soar"],
        # Google SecOps authenticates with a service-account JSON key (OAuth2
        # JWT-bearer flow) — paste the JSON into the API Key field.
        "auth_type": "api_key",
        "supported_auth_types": ["api_key"],
        "fields": ["api_key"],
        "extra_fields": {
            "region": "SecOps region / instance location, e.g. us, europe, asia-southeast1",
            "project_id": "Google Cloud project ID linked to your SecOps instance",
            "customer_id": "SecOps instance Customer ID (Settings > SIEM Settings > Profile)",
        },
        "setup_guide": {
            "title": "Google SecOps (Chronicle) API Setup",
            "url": "https://cloud.google.com/chronicle/docs/reference/rest",
            "steps": [
                "In the Google Cloud project linked to your SecOps instance, enable the Chronicle API (chronicle.googleapis.com).",
                "Create a service account (IAM & Admin > Service Accounts) and grant it the 'Chronicle API Editor' role (or 'Chronicle API Viewer' for read-only).",
                "Create a JSON key for the service account and download it.",
                "Paste the entire service account JSON (single line is fine) into the API Key field below.",
                "Set Region to your SecOps instance location (e.g. 'us' or 'europe'), Project ID to the linked Google Cloud project, and Customer ID to your SecOps instance ID (found under Settings > SIEM Settings > Profile in the SecOps UI).",
            ],
            "permissions": [
                {
                    "scope": "Chronicle API Editor (or Viewer)",
                    "description": "Read alerts/cases/detections, run UDM searches and entity lookups, and (Editor) update alert triage feedback, manage cases, create/update rules, run retrohunts, and manage reference lists",
                },
                {
                    "scope": "OAuth scope: https://www.googleapis.com/auth/cloud-platform",
                    "description": "Granted automatically via the service-account JWT-bearer flow",
                },
            ],
            "notes": (
                "Google SecOps uses a service-account JSON key — there is no static API "
                "token. XTM One signs a short-lived JWT with the key and redeems it for an "
                "OAuth2 access token (scope cloud-platform). Grant the service account the "
                "Chronicle API Editor role for write actions (alert triage feedback, case "
                "priority/stage/assignee/close, rule create/update/enable, retrohunts, "
                "reference list edits) or Viewer for read-only visibility and hunting. The "
                "region, project ID, and customer (instance) ID identify your Chronicle "
                "instance. API calls target https://{region}-chronicle.googleapis.com, with "
                "the version chosen per endpoint: hunting, alerts and detection rules on "
                "/v1alpha, the case collection on /v1beta. Case tools additionally need the "
                "SOAR-side case permissions (chronicle.cases.*) — the SIEM-only roles let "
                "hunting and rules work while cases come back empty."
            ),
        },
    },
    "newsapi": {
        "name": "NewsAPI",
        "description": "Search 100 000+ news sources worldwide — headlines, articles, and source discovery",
        "logo": "/logos/newsapi.svg",
        "category": "integration",
        "integration_category": "search_news",
        "integration_types": ["news"],
        "auth_type": "api_key",
        "fields": ["api_key"],
        "setup_guide": {
            "title": "NewsAPI Setup",
            "url": "https://newsapi.org/register",
            "steps": [
                "Go to newsapi.org and create a free account.",
                "After signing in, your API key is displayed on the dashboard.",
                "Copy the API key and paste it in the field below.",
                "Free plan: 100 requests/day, articles up to 1 month old. Paid plans remove limits.",
            ],
            "permissions": [
                {
                    "scope": "Everything",
                    "description": "Search all articles from 100 000+ sources by keyword, date, language",
                },
                {
                    "scope": "Top Headlines",
                    "description": "Get current headlines by category (business, technology, science, health, etc.)",
                },
                {
                    "scope": "Sources",
                    "description": "List and filter available news sources",
                },
            ],
            "notes": "The free tier is limited to 100 requests/day and articles from the last month. For production use, consider the Business plan for higher limits and real-time articles.",
        },
    },
    "feedly": {
        "name": "Feedly",
        "description": (
            "Open-source intelligence feeds + Feedly for Threat Intelligence - "
            "read team folders, boards, and AI Feeds, search AI-enriched "
            "articles, enrich CVEs / threat actors / malware / IoCs from the "
            "Insight Cards, export STIX IoCs from any stream, query the threat "
            "landscape dashboards, and curate boards"
        ),
        "logo": "/logos/feedly.svg",
        "category": "integration",
        "integration_category": "threat_intelligence",
        "integration_types": ["news", "threat_intel"],
        # Feedly authenticates with a single Enterprise API access token sent
        # as a Bearer token - there is no OAuth flow, so the only supported
        # auth mode is "api_key".
        "auth_type": "api_key",
        "supported_auth_types": ["api_key"],
        "fields": ["api_key"],
        "setup_guide": {
            "title": "Feedly API Token Setup",
            "url": "https://feedly.com/i/team/api",
            "steps": [
                "Sign in to Feedly with an Enterprise account that has admin rights (the API is an Enterprise add-on - contact sales@feedly.com if the page below is unavailable).",
                "Open the API self-service page at feedly.com/i/team/api.",
                "Click 'New API Token' and copy the token immediately - it is only shown once.",
                "Paste the token into the 'API Key' field below.",
                "Click 'Test Connection' to verify the token (calls GET https://api.feedly.com/v3/profile).",
            ],
            "permissions": [
                {
                    "scope": "Streams & Search",
                    "description": "Read team folders, boards, and AI Feeds, collect articles, and run AI-layer power searches",
                },
                {
                    "scope": "Threat Intelligence",
                    "description": "CVE / threat actor / malware / IoC insight cards, relationship graphs, STIX 2.1 IoC exports, and the Threat Landscape / Vulnerability / Cyberattacks dashboards (Feedly for Threat Intelligence package)",
                },
                {
                    "scope": "Ask AI (optional)",
                    "description": "RAG answers over Feedly content - requires the separate 'Ask AI API' add-on",
                },
                {
                    "scope": "Board Curation",
                    "description": "Save articles to team boards, remove them, and add annotations",
                },
            ],
            "notes": (
                "Feedly authenticates with a single Enterprise API access token "
                "sent as 'Authorization: Bearer' (no OAuth). All tools target "
                "https://api.feedly.com (v3). Plans include up to 100,000 "
                "requests per month; HTTP 429 responses carry X-RateLimit-* "
                "headers and are retried automatically. Threat intelligence "
                "tools require the Feedly for Threat Intelligence package. The "
                "token is stored encrypted here."
            ),
        },
    },
    "shodan": {
        "name": "Shodan",
        "description": "Search engine for Internet-connected devices - host/domain intelligence, banner search with filters, on-demand scanning, and network monitoring alerts",
        "logo": "/logos/shodan.png",
        "category": "integration",
        "integration_category": "threat_intelligence",
        "integration_types": ["threat_intel", "asm"],
        # Shodan authenticates with a single API key sent as the "key" query
        # parameter on every request - there is no OAuth.
        "auth_type": "api_key",
        "supported_auth_types": ["api_key"],
        "fields": ["api_key"],
        "setup_guide": {
            "title": "Shodan Setup",
            "url": "https://account.shodan.io/",
            "steps": [
                "Sign in at account.shodan.io (or create a Shodan account).",
                "Open the 'Account' page - your API key is shown under 'API Key'.",
                "Copy the API key and paste it in the field below.",
                "Search, host lookups, and DNS use query credits; on-demand scanning and alerts require a paid membership.",
            ],
            "permissions": [
                {
                    "scope": "Search & Intelligence",
                    "description": "Look up host/domain intelligence and search the Internet-wide banner database",
                },
                {
                    "scope": "On-Demand Scanning",
                    "description": "Request Shodan to crawl IPs/netblocks (consumes scan credits, paid plans)",
                },
                {
                    "scope": "Network Alerts & Notifiers",
                    "description": "Create and manage alerts that monitor IP ranges plus their notification services",
                },
                {
                    "scope": "Organization",
                    "description": "View organization details and manage members (Enterprise)",
                },
            ],
            "notes": "The API key inherits your membership's query/scan credit limits. A free key can search and look up hosts; scanning and monitoring alerts require a paid membership.",
        },
    },
    "nova": {
        "name": "Nova Prompt Safety",
        "description": (
            "Active prompt-safety guardrails: scan user prompts before the LLM "
            "call and block or log attacks (jailbreaks, injections). Rules use "
            "the Nova .nov format; connect to sync the community rule set and "
            "manage rules from this integration."
        ),
        "logo": "/logos/nova.svg",
        "category": "integration",
        "integration_category": "detection_response",
        "integration_types": ["guardrails"],
        # No credentials: activation is a platform opt-in. Connecting runs the
        # provider's on_connect hook (creates the rule-sync source); the guard
        # then applies to every agent until disconnected.
        "auth_type": "none",
        "supported_auth_types": ["none"],
        "fields": [],
        "setup_guide": {
            "title": "Nova Prompt Safety Setup",
            # The upstream project the rule format and community rules come
            # from; author and licence as its repository's LICENCE file states.
            "open_source": {
                "name": "NOVA",
                "url": "https://github.com/Nova-Hunting/nova-framework",
                "author": "Thomas Roccia",
                "license": "MIT",
            },
            "steps": [
                "Connect the integration — no credentials are required.",
                "The community rule set from github.com/Nova-Hunting/nova-rules "
                "is pulled automatically and refreshed on a schedule.",
                "Manage rules and the sync source from the Rules tab.",
            ],
            "permissions": [
                {
                    "scope": "Guardrail",
                    "description": "Scan and block/log user prompts before the agent's LLM call, per your rules.",
                },
            ],
            "notes": (
                "The advanced semantic/LLM rule tiers require the optional "
                "nova-hunting package in the backend image; they then run on "
                "the platform's own embedding and LLM providers. Without it "
                "the built-in keyword/regex tier still runs. Test Connection "
                "reports which tiers are live, and how many rules are loaded."
            ),
        },
    },
    "platform_email": {
        "name": "Platform email",
        "description": (
            "Let agents send email from the platform's own address through the SMTP "
            "server this deployment configures, to the recipient domains you allow"
        ),
        "logo": "/logos/platform_email.svg",
        "category": "integration",
        "integration_category": "communication",
        "integration_types": ["email"],
        # No credential: it sends through the deployment's SMTP settings. Offered
        # only while SMTP is configured, created by an administrator only, as a
        # company-managed integration (integrations.py reads both flags).
        "auth_type": "none",
        "supported_auth_types": ["none"],
        "requires_platform_smtp": True,
        "company_managed_only": True,
        "fields": [],
        "extra_fields": {
            "allowed_domains": "Allowed recipient domains",
            "daily_limit": "Emails per agent per day",
        },
        # The control and the rule of each field: the form draws a domain list as
        # tags and the limit as a bounded number, and the create and update
        # routes refuse a value outside the rule (invalid_extra_fields).
        "extra_field_inputs": {
            "allowed_domains": {
                "kind": "domains",
                "help": "Type a domain such as example.com, then press Enter. Agents can only write to these domains; with none, every send is refused.",
            },
            "daily_limit": {
                "kind": "integer",
                "minimum": 0,
                "maximum": 1000,
                "default": "50",
                "help": "Emails one agent may send per UTC day, from 0 to 1000. 0 stops every send.",
            },
        },
        "setup_guide": {
            "title": "Platform email setup",
            "steps": [
                "The deployment's SMTP settings (SMTP_HOSTNAME and the other SMTP_* variables) are the server and the sender: nothing to enter here.",
                "Add the recipient domains agents may write to, one at a time (type a domain, then press Enter). An address outside them is refused.",
                "Set the daily limit per agent (default 50), then bind the integration to the agents that may send.",
            ],
            "permissions": [
                {
                    "scope": "Send",
                    "description": "Send email from the platform sender to the allowed domains; every send asks for approval and is written to the audit log.",
                },
            ],
            "notes": (
                "Test Connection says whether SMTP is configured and which domains are allowed. "
                "Bcc recipients each receive their own copy, never named in a header."
            ),
        },
    },
    "rss": {
        "name": "RSS / Atom Feeds",
        "description": "Read news from any RSS or Atom feed — curated sources for any topic",
        "logo": "/logos/rss.svg",
        "category": "integration",
        "integration_category": "search_news",
        "integration_types": ["news"],
        "auth_type": "none",
        "fields": [],
        "extra_fields": {"feed_urls": "Feed URLs (one URL per line)"},
        "setup_guide": {
            "title": "RSS / Atom Feeds Setup",
            "steps": [
                "Collect the RSS or Atom feed URLs for the news sources you want to follow.",
                "Paste all feed URLs in the field below — one URL per line.",
                "The feeds will be fetched and parsed automatically by the agent's tools.",
            ],
            "permissions": [
                {
                    "scope": "Read",
                    "description": "Fetch and read articles from all configured feeds",
                },
            ],
            "notes": (
                "No API key needed — just paste feed URLs (one per line). Examples:\n\n"
                "**Cybersecurity:**\n"
                "  https://feeds.feedburner.com/TheHackersNews\n"
                "  https://krebsonsecurity.com/feed/\n"
                "  https://www.bleepingcomputer.com/feed/\n"
                "  https://www.darkreading.com/rss.xml\n"
                "  https://www.cisa.gov/news.xml\n\n"
                "**Economy & Business:**\n"
                "  https://feeds.reuters.com/reuters/businessNews\n"
                "  https://feeds.bbci.co.uk/news/business/rss.xml\n"
                "  https://www.economist.com/finance-and-economics/rss.xml\n\n"
                "**Technology:**\n"
                "  https://feeds.arstechnica.com/arstechnica/index\n"
                "  https://techcrunch.com/feed/\n"
                "  https://www.theverge.com/rss/index.xml\n\n"
                "**General:**\n"
                "  https://feeds.bbci.co.uk/news/rss.xml\n"
                "  https://rss.nytimes.com/services/xml/rss/nyt/HomePage.xml"
            ),
        },
    },
    "filigran_ai": {
        "name": "Filigran AI",
        "description": "Fine-tuned AI models for cyber threat intelligence — STIX extraction, entity/relationship extraction, and TTP extraction",
        "logo": "/logos/filigran.svg",
        "category": "integration",
        "integration_category": "threat_intelligence",
        "integration_types": ["threat_intel"],
        # An extraction model, not a data source: never offered in a fleet's
        # capability slots (services.fleet_capability_slots.providers_for).
        "capability_slots": False,
        "auth_type": "none",
        "fields": [],
        "extra_fields": {
            "base_url_stix": "STIX extraction API base URL (e.g. https://importdoc.ariane.filigran.io)",
            "base_url_ttp": "TTP extraction API base URL (e.g. https://ttpextraction.ariane.filigran.io)",
        },
        "setup_guide": {
            "title": "Filigran AI Setup",
            "steps": [
                "Enter the STIX extraction API base URL for STIX and entity/relationship extraction.",
                "Enter the TTP extraction API base URL for TTP extraction.",
                "Make sure a valid enterprise licence PEM is configured in the platform settings.",
                "Attach files to a conversation and use the extraction tools.",
            ],
            "permissions": [
                {
                    "scope": "STIX Extraction",
                    "description": "Send documents for STIX entity extraction",
                },
                {
                    "scope": "Entity & Relationship Extraction",
                    "description": "Send documents for entity and relationship extraction",
                },
                {
                    "scope": "TTP Extraction",
                    "description": "Send documents for TTP extraction",
                },
            ],
            "notes": "Requires a valid Filigran enterprise licence. Supports PDF, DOCX, and other document formats.",
        },
    },
    "google_search": {
        "name": "Google Search",
        "description": "Search the web using Google Custom Search — find articles, documentation, news, images, and any public information",
        "logo": "/logos/google_search.svg",
        "category": "integration",
        "integration_category": "search_news",
        "integration_types": ["search", "images"],
        "auth_type": "api_key",
        "fields": ["api_key"],
        "extra_fields": {"search_engine_id": "Programmable Search Engine ID (cx)"},
        "setup_guide": {
            "title": "Google Custom Search API Setup",
            "url": "https://programmablesearchengine.google.com/",
            "steps": [
                "Go to the Google Cloud Console: console.cloud.google.com/apis/credentials.",
                "Create a new project (or select an existing one).",
                "Enable the 'Custom Search API' in APIs & Services > Library.",
                "Go to APIs & Services > Credentials > Create Credentials > API Key.",
                "Copy the API key and paste it in the 'API Key' field below.",
                "Go to Programmable Search Engine: programmablesearchengine.google.com.",
                "Click 'Add' to create a new search engine.",
                "In 'What to search', select 'Search the entire web'.",
                "Enable 'Image search' in the search engine settings (required for image search to work).",
                "Copy the Search Engine ID (cx) and paste it in the 'Search Engine ID' field below.",
            ],
            "permissions": [
                {
                    "scope": "Custom Search API",
                    "description": "Perform web searches via Google's Custom Search JSON API",
                },
            ],
            "notes": "The free tier allows 100 search queries per day. For higher limits, enable billing on the Custom Search API in Google Cloud Console ($5 per 1,000 queries, up to 10,000 queries/day). The Programmable Search Engine can be configured to search the entire web or specific sites. Image search is enabled by default — make sure 'Image search' is turned ON in your Programmable Search Engine settings for the image search tool to work.",
        },
    },
    "brave_search": {
        "name": "Brave Search",
        "description": "Search the web with Brave Search's own independent index: web, news, images, videos, suggestions, spelling, LLM context for grounding, cited answers and places",
        "logo": "/logos/brave_search.svg",
        "category": "integration",
        "integration_category": "search_news",
        "integration_types": ["search", "news", "images", "videos"],
        "auth_type": "api_key",
        "fields": ["api_key"],
        "setup_guide": {
            "title": "Brave Search API key",
            "url": "https://api-dashboard.search.brave.com/",
            "steps": [
                "Sign in to the Brave Search API dashboard (api-dashboard.search.brave.com).",
                "Subscribe to a plan: Search covers web, news, images, videos and places; Answers and LLM Context come with the AI plans; Suggest and Spellcheck are their own plans.",
                "Open API Keys, create a key and copy it.",
                "Paste it in the 'API Key' field below, then run Test connection (one web query).",
            ],
            "permissions": [
                {"scope": "Search", "description": "Web, news, image, video search and local places"},
                {"scope": "AI", "description": "LLM Context and Answers (optional)"},
                {"scope": "Suggest / Spellcheck", "description": "Autocomplete and spelling correction (optional)"},
            ],
            "notes": "The key is sent as the X-Subscription-Token header. A tool whose endpoint the plan does not include answers a one-line error naming it; a rate limit answers the wait Brave asks for. Setting BUILTIN_BRAVE_SEARCH_API_KEY on the deployment gives every agent Brave web search without adding this integration.",
        },
    },
    "giphy": {
        "name": "GIPHY",
        "description": "GIFs and stickers for conversational agents: search, the best GIF for a phrase, random and trending GIFs, shown in the web chat and as images in Teams, Slack and the other channels",
        "logo": "/logos/giphy.svg",
        "category": "integration",
        "integration_category": "social_media",
        "integration_types": ["social", "images"],
        "auth_type": "api_key",
        "fields": ["api_key"],
        "setup_guide": {
            "title": "GIPHY API key",
            "url": "https://developers.giphy.com/dashboard/",
            "steps": [
                "Sign in to the GIPHY Developers dashboard (developers.giphy.com/dashboard).",
                "Click 'Create an API Key', choose API (not SDK), name the app and accept GIPHY's API terms.",
                "Copy the API key and paste it in the 'API Key' field below, then run Test connection (one search).",
                "Bind the integration to the agent, and say in its instructions when a GIF fits (for example: 'celebrate a resolved incident with a GIF').",
            ],
            "permissions": [
                {"scope": "Search", "description": "Search GIFs and stickers, translate a phrase into a GIF"},
                {"scope": "Random / Trending", "description": "A random GIF for a tag, the trending GIFs"},
            ],
            "notes": "A new key is a rate-limited beta key (GIPHY caps its calls per hour); apply for a production key in the dashboard once the agent is in use. Every call sends a content rating, G by default and never above PG-13. The key travels as the api_key query parameter, the only form GIPHY accepts. GIPHY asks apps to credit it ('Powered by GIPHY'): the tool results say so.",
        },
    },
    "ashby": {
        "name": "Ashby",
        "description": "Applicant Tracking & Recruiting - Candidates (with resumes and files), Applications and stage history, Interview schedules and scorecards, Jobs, Postings, Openings, Offers, Referrals, Reports",
        "logo": "/logos/ashby.svg",
        "category": "integration",
        "integration_category": "hr",
        "integration_types": ["ats", "hr"],
        "auth_type": "api_key",
        "fields": ["api_key"],
        "setup_guide": {
            "title": "Ashby API Key Setup",
            "url": "https://app.ashbyhq.com/admin/api/keys",
            "steps": [
                "Log in to Ashby as an Admin.",
                "Navigate to Admin > Integrations > API Keys.",
                "Click 'Create API Key' and give it a descriptive name.",
                "Grant the module permissions your agents need (read and/or write per module, listed below). Add 'API Keys Read' so the connection test can show what the key holds.",
                "Turn on 'Allow access to confidential jobs and projects' if agents must see candidates of confidential jobs, and 'Allow access to non-offer private fields' for private fields - without them Ashby answers as if those records did not exist.",
                "Copy the API key and paste it in the field below.",
            ],
            "permissions": [
                {
                    "scope": "Candidates Read",
                    "description": "Search and read candidates, applications, stage history, scorecards, notes, AI criteria evaluations, referrals and survey answers; download resumes and candidate files",
                },
                {
                    "scope": "Candidates Write",
                    "description": "Create and update candidates and applications, change stages, transfer, add notes, tags and projects, submit feedback, upload files, set custom field values, submit referrals",
                },
                {"scope": "Candidates Delete", "description": "Delete applications"},
                {
                    "scope": "Jobs Read",
                    "description": "Jobs, job postings, openings, job interview plans, job boards and templates",
                },
                {
                    "scope": "Jobs Write",
                    "description": "Create and update jobs, postings and openings; open, close, publish",
                },
                {
                    "scope": "Interviews Read",
                    "description": "Interview schedules and events, interview definitions, plans, stages, take-home assignments",
                },
                {"scope": "Interviews Write", "description": "Schedule, update and cancel interviews"},
                {
                    "scope": "Hiring Process Read",
                    "description": "Feedback forms, archive and close reasons, sources, candidate tags, custom fields, interviewer pools, survey forms",
                },
                {"scope": "Hiring Process Write", "description": "Create candidate tags"},
                {
                    "scope": "Organization Read",
                    "description": "Users, departments, locations, brands, hiring team roles",
                },
                {"scope": "Organization Write", "description": "Hiring team members of jobs and openings"},
                {"scope": "Offers Read", "description": "List and read offers"},
                {
                    "scope": "Offers Write",
                    "description": "Start and create offers, set their status and decision date, send them for approval",
                },
                {"scope": "Projects Read", "description": "List and search projects (talent pools)"},
                {"scope": "Projects Write", "description": "Create, update, archive and restore projects"},
                {"scope": "Approvals Read", "description": "Approval status of offers, jobs and openings"},
                {"scope": "Reports Read", "description": "Run saved reports"},
                {
                    "scope": "API Keys Read",
                    "description": "Lets the connection test and get_ashby_api_key_info list the key's permissions",
                },
                {"scope": "Notetaker Read", "description": "AI Notetaker interview transcripts (Ashby add-on)"},
            ],
            "notes": "Ashby uses HTTP Basic Auth: the API key is the username with an empty password, and every endpoint is a POST. A tool that lacks a permission says which one to grant. Ashby allows 1,000 requests per minute per key and 15 report runs per minute per organization. See https://developers.ashbyhq.com/reference/authentication for the permission table.",
        },
    },
    "quotapath": {
        "name": "QuotaPath",
        "description": (
            "Sales commission management - users, teams and managers, compensation plans and paths, "
            "quotas, deals and their crediting, payouts (earned, paid, still owed), commission totals "
            "by rep, team, plan and period, CSV payout and deal reports. Commission amounts are people "
            "data; every write asks a person"
        ),
        "logo": "/logos/quotapath.svg",
        "category": "integration",
        "integration_category": "business_operations",
        "integration_types": ["sales_compensation"],
        "auth_type": "api_key",
        "fields": ["api_key"],
        "available_events": [
            {
                "event": "payout_updated",
                "label": "Payout created or updated",
                "description": (
                    "Triggers when a QuotaPath payout is created or changes (a deal credited, a rate or "
                    "quota changed). Uses polling (every 15 min); the first poll starts from now."
                ),
                "mechanism": "polling",
            },
            {
                "event": "payout_resolved",
                "label": "Payout resolved",
                "description": (
                    "Triggers when a QuotaPath payout is resolved (the period paid out). Uses polling "
                    "(every 15 min); the first poll starts from now."
                ),
                "mechanism": "polling",
            },
            {
                "event": "payout_unresolved",
                "label": "Payout newly owed",
                "description": (
                    "Triggers when a payout appears among the unresolved ones (commission owed, the "
                    "accrual). Uses polling (every 15 min); what is owed when the trigger is enabled "
                    "does not run."
                ),
                "mechanism": "polling",
            },
        ],
        "setup_guide": {
            "title": "QuotaPath API Key Setup",
            "url": "https://api.quotapath.com/docs/",
            "steps": [
                "Sign in to QuotaPath as a workspace admin.",
                "Open Settings > Integrations and generate an API key.",
                "Paste the key in the field below. Test Connection reads the workspace users.",
            ],
            "permissions": [
                {
                    "scope": "Workspace API key",
                    "description": "Reads users, teams, plans, quotas, deals and payouts; writes deals, quotas and imports (each write asks a person)",
                },
            ],
            "notes": "QuotaPath sends the key as 'Authorization: Token <key>' to https://api.quotapath.com/v1. Its public API has no disputes, adjustments, draws, payout-period approval or lock, CRM sync status, statement PDFs or webhooks: those stay in the QuotaPath application, and events are polled.",
        },
    },
    "pennylane": {
        "name": "Pennylane",
        "description": "Accounting, Invoicing & Banking - Ledger entries and lines, Trial balance, FEC and general ledger exports, Customer and supplier invoices (with PDFs, payments, matching), Customers, Suppliers, Products, Quotes, Bank transactions, Analytical categories (Company API v2)",
        "logo": "/logos/pennylane.svg",
        "category": "integration",
        "integration_category": "business_operations",
        "integration_types": ["accounting", "finance"],
        "auth_type": "api_key",
        "fields": ["api_key"],
        "extra_fields": {"company_id": "Company ID (firm token only)"},
        # Polled from the Company API v2 changelogs (services/pennylane_events.py,
        # every 5 minutes), each change read with its record.
        "available_events": [
            {
                "event": event,
                "label": label,
                "description": description,
                "mechanism": "polling",
                "filter_keys": _PENNYLANE_FILTER_KEYS,
            }
            for event, label, description in _PENNYLANE_EVENTS
        ],
        "setup_guide": {
            "title": "Pennylane API Token Setup",
            "url": "https://app.pennylane.com",
            "steps": [
                "Log in to Pennylane as an administrator of the company.",
                "Open Settings > Connectivity > Developers and generate an API token (Company API v2).",
                "Give it the read-only scopes your agents need, and the write scopes only where they may write (listed below).",
                "Paste the token in the API Key field below. One integration per Pennylane company.",
                "With a firm (accounting practice) token, also fill in the Company ID of the company it should act on.",
            ],
            "permissions": [
                {
                    "scope": "ledger_entries:readonly",
                    "description": "Ledger entries and lines, lettering, their analytics and changelogs",
                },
                {
                    "scope": "ledger_accounts:readonly / journals:readonly",
                    "description": "Chart of accounts and journals",
                },
                {
                    "scope": "trial_balance:readonly / fiscal_years:readonly",
                    "description": "Trial balance and fiscal years",
                },
                {
                    "scope": "exports:fec / exports:gl / exports:agl",
                    "description": "FEC, general ledger and analytical general ledger exports",
                },
                {
                    "scope": "customer_invoices:readonly / supplier_invoices:readonly",
                    "description": "Invoices, lines, payments, matched transactions, PDFs, changelogs",
                },
                {
                    "scope": "customers:readonly / suppliers:readonly / products:readonly / quotes:readonly",
                    "description": "Customers, suppliers (IBAN masked), products, quotes",
                },
                {
                    "scope": "bank_accounts:readonly / transactions:readonly",
                    "description": "Bank accounts and transactions with their matching",
                },
                {"scope": "categories:readonly", "description": "Analytical category groups and categories"},
                {
                    "scope": "*:all (write)",
                    "description": "Draft customer invoices, supplier invoice updates, customers, suppliers, ledger entries, matching, analytical categorisation, paid status (each on its own resource scope); file_attachments:all for uploads",
                },
            ],
            "notes": "Pennylane allows 25 requests per 5 seconds per token; a 429 is retried after Retry-After. A tool that lacks a scope names it. No tool initiates a payment, changes bank details or touches mandates. Bind read-only to withhold every write tool; list_pennylane_bank_accounts is the only tool showing bank account data. See https://pennylane.readme.io/reference.",
        },
    },
    "linkedin": {
        "name": "LinkedIn",
        "description": "Posts (text, images, video, documents), Pages (Organizations), Analytics, Comments, Reactions",
        "logo": "/logos/linkedin.svg",
        "category": "integration",
        "integration_category": "social_media",
        "integration_types": ["social"],
        "available_events": [
            {
                "event": "linkedin_social_action",
                "label": "Organization social action",
                "description": "Triggers when someone interacts with your LinkedIn Page (likes, comments, shares, or mentions on organization posts). Requires webhooks configured in the LinkedIn Developer Portal and Community Management API access.",
                "mechanism": "webhook",
            },
            {
                "event": "linkedin_comment",
                "label": "New comment on page post",
                "description": "Triggers when someone comments on a post from your LinkedIn Page.",
                "mechanism": "webhook",
            },
            {
                "event": "linkedin_like",
                "label": "New like on page post",
                "description": "Triggers when someone likes a post from your LinkedIn Page.",
                "mechanism": "webhook",
            },
            {
                "event": "linkedin_share",
                "label": "Post shared",
                "description": "Triggers when someone shares a post from your LinkedIn Page.",
                "mechanism": "webhook",
            },
            {
                "event": "linkedin_mention",
                "label": "Page mentioned in a share",
                "description": "Triggers when your LinkedIn Page is mentioned in a share.",
                "mechanism": "webhook",
            },
        ],
        "needs_shared_webhook": True,
        "auth_type": "oauth",
        # What its sign-in learns about the account: a person's own credential
        # never reads the integration's.
        "account_config_keys": ["organizations", "organization_id"],
        "fields": ["client_id", "client_secret"],
        "extra_fields": {"account_type": "Account type"},
        "extra_field_options": {
            "account_type": [
                {"value": "personal", "label": "Personal Account"},
                {"value": "page", "label": "LinkedIn Page (Organization)"},
            ],
        },
        # Personal accounts: Sign In + Share on LinkedIn products.
        # Page (organization) accounts: Community Management API + full org scopes.
        "scopes": "r_verify openid profile w_member_social email r_profile_basicinfo",
        "scopes_personal": "r_verify openid profile w_member_social email r_profile_basicinfo",
        "scopes_page": "r_member_postAnalytics r_organization_followers r_organization_social rw_organization_admin r_organization_social_feed w_member_social r_member_profileAnalytics w_organization_social r_basicprofile w_organization_social_feed w_member_social_feed r_1st_connections_size",
        "authorize_url": "https://www.linkedin.com/oauth/v2/authorization",
        "token_url": "https://www.linkedin.com/oauth/v2/accessToken",
        "setup_guide": {
            "title": "LinkedIn Developer App Setup",
            "url": "https://www.linkedin.com/developers/apps",
            "steps": [
                "Go to linkedin.com/developers/apps and click 'Create app'.",
                "App name: '{APP_NAME}'. Associate it with your LinkedIn Page (required by LinkedIn).",
                "Under 'Auth' tab, add the redirect URL: {base_url}/api/oauth/callback",
                "Under 'Products', request access to 'Share on LinkedIn' and 'Sign In with LinkedIn using OpenID Connect'.",
                "For page management, also request 'Community Management API' (requires LinkedIn approval).",
                "Copy the Client ID and Client Secret from the 'Auth' tab.",
                "Select 'Personal Account' or 'LinkedIn Page (Organization)' as the account type — scopes are adjusted automatically.",
                "For page management, the authenticating user must be an admin of the LinkedIn Page.",
                "For real-time webhook events, go to the 'Webhooks' tab in the Developer Portal:",
                "  - Click 'Create webhook' and enter the shared webhook URL shown in the Webhooks section below.",
                "  - LinkedIn will validate the URL with a challenge-response handshake (handled automatically).",
                "  - The webhook is re-validated every 2 hours — keep the endpoint reachable.",
                "  - Subscribe to Organization Social Action Notifications via the Event Subscription API.",
            ],
            "permissions_personal": [
                {"scope": "r_verify", "description": "Get your profile verification"},
                {"scope": "openid", "description": "Use your name and photo"},
                {"scope": "profile", "description": "Use your name and photo"},
                {
                    "scope": "w_member_social",
                    "description": "Create, modify, and delete posts, comments, and reactions on your behalf",
                },
                {
                    "scope": "email",
                    "description": "Use the primary email address associated with your LinkedIn account",
                },
                {
                    "scope": "r_profile_basicinfo",
                    "description": "Access your basic profile information, such as your name, headline, profile photo, and profile URL",
                },
            ],
            "permissions_page": [
                {
                    "scope": "r_member_postAnalytics",
                    "description": "Retrieve your posts and their reporting data",
                },
                {
                    "scope": "r_organization_followers",
                    "description": "Use your followers' data so your organization can mention them in posts",
                },
                {
                    "scope": "r_organization_social",
                    "description": "Retrieve your organization's posts, comments, reactions, and other engagement data",
                },
                {
                    "scope": "rw_organization_admin",
                    "description": "Manage your organization's pages and retrieve reporting data",
                },
                {
                    "scope": "r_organization_social_feed",
                    "description": "Retrieve comments, reactions, and other engagement data on your organization's posts",
                },
                {
                    "scope": "w_member_social",
                    "description": "Create, modify, and delete posts, comments, and reactions on your behalf",
                },
                {
                    "scope": "r_member_profileAnalytics",
                    "description": "Retrieve your profile analytics, including number of profile viewers, followers, and search appearances",
                },
                {
                    "scope": "w_organization_social",
                    "description": "Create, modify, and delete posts, comments, and reactions on your organization's behalf",
                },
                {
                    "scope": "r_basicprofile",
                    "description": "Use your basic profile including your name, photo, headline, and public profile URL",
                },
                {
                    "scope": "w_organization_social_feed",
                    "description": "Create, modify, and delete comments and reactions on your organization's posts",
                },
                {
                    "scope": "w_member_social_feed",
                    "description": "Create, modify, and delete comments and reactions on posts on your behalf",
                },
                {
                    "scope": "r_1st_connections_size",
                    "description": "Retrieve the number of 1st-degree connections within your network",
                },
            ],
            "permissions": [],
            "notes": "Personal mode requires 'Share on LinkedIn' + 'Sign In with LinkedIn using OpenID Connect' products. Page mode additionally requires 'Community Management API' (needs LinkedIn approval). The OAuth scopes requested depend on the selected account type — personal accounts request basic profile and posting scopes, page accounts include full organization-level scopes for managing pages, analytics, and social engagement. For webhooks, the admin must register the shared webhook URL in the LinkedIn Developer Portal Webhooks tab. Webhook events are only available for LinkedIn Page (Organization) mode with rw_organization_admin permission.",
        },
    },
    "x": {
        "name": "X (Twitter)",
        "description": "Tweets (with images, GIFs and videos), Search, Timelines, Engagement, DMs, Bookmarks, Social Graph",
        "logo": "/logos/x.svg",
        "category": "integration",
        "integration_category": "social_media",
        "integration_types": ["social"],
        "auth_type": "oauth",
        "fields": ["client_id", "client_secret"],
        "scopes": "tweet.read tweet.write users.read follows.read follows.write like.read like.write bookmark.read bookmark.write list.read list.write dm.read dm.write media.write offline.access",
        "authorize_url": "https://twitter.com/i/oauth2/authorize",
        "token_url": "https://api.x.com/2/oauth2/token",
        "setup_guide": {
            "title": "X (Twitter) Developer App Setup",
            "url": "https://developer.x.com/en/portal/projects-and-apps",
            "steps": [
                "Go to the X Developer Portal and sign up for a developer account (or use an existing one).",
                "Create a new Project and App in the Developer Portal.",
                "Under 'User authentication settings', click 'Set up'.",
                "Select 'OAuth 2.0' as the authentication method.",
                "App permissions: select 'Read and write and Direct message'.",
                "Type of App: select 'Web App, Automated App or Bot'.",
                "Callback URL: {base_url}/api/oauth/callback",
                "Website URL: your {APP_NAME} instance URL.",
                "Copy the Client ID and Client Secret (shown only once).",
                "Ensure your app has access to the X API v2 endpoints.",
            ],
            "permissions": [
                {
                    "scope": "tweet.read",
                    "description": "View tweets, timelines, and search results",
                },
                {
                    "scope": "tweet.write",
                    "description": "Create, delete, and manage tweets",
                },
                {
                    "scope": "users.read",
                    "description": "View user profiles and account information",
                },
                {
                    "scope": "follows.read",
                    "description": "View followers and following lists",
                },
                {"scope": "follows.write", "description": "Follow and unfollow users"},
                {"scope": "like.read", "description": "View liked tweets"},
                {"scope": "like.write", "description": "Like and unlike tweets"},
                {"scope": "bookmark.read", "description": "View bookmarked tweets"},
                {"scope": "bookmark.write", "description": "Add and remove bookmarks"},
                {"scope": "list.read", "description": "View lists and list members"},
                {"scope": "list.write", "description": "Create and manage lists"},
                {
                    "scope": "dm.read",
                    "description": "View direct message conversations",
                },
                {"scope": "dm.write", "description": "Send direct messages"},
                {
                    "scope": "media.write",
                    "description": "Upload images, GIFs and videos to attach to tweets and direct messages",
                },
                {
                    "scope": "offline.access",
                    "description": "Maintain access with refresh tokens",
                },
            ],
            "notes": "X uses OAuth 2.0 with PKCE (Proof Key for Code Exchange). The platform handles PKCE automatically. A developer account with at least Basic access level is required for most API v2 endpoints. Free tier has limited tweet creation (1,500/month). Rate limits vary by endpoint and access level. Attaching images, GIFs or videos to tweets and DMs needs the media.write scope: an integration authorized before it was requested must be authorized again.",
        },
    },
    "facebook": {
        "name": "Facebook",
        "description": "Posts (with photos and videos), Pages, Comments, Insights, Engagement",
        "logo": "/logos/facebook.svg",
        "category": "integration",
        "integration_category": "social_media",
        "integration_types": ["social"],
        "auth_type": "oauth",
        # What its sign-in learns about the account: a person's own credential
        # never reads the integration's.
        "account_config_keys": ["pages", "page_id", "page_name", "page_access_token"],
        "fields": ["client_id", "client_secret"],
        "extra_fields": {"account_type": "Account type"},
        "extra_field_options": {
            "account_type": [
                {"value": "personal", "label": "Personal Account"},
                {"value": "page", "label": "Facebook Page"},
            ],
        },
        "scopes": "public_profile email pages_show_list pages_manage_posts pages_read_engagement pages_read_user_content pages_manage_metadata pages_manage_engagement read_insights",
        "authorize_url": "https://www.facebook.com/v21.0/dialog/oauth",
        "token_url": "https://graph.facebook.com/v21.0/oauth/access_token",
        "setup_guide": {
            "title": "Facebook (Meta) Developer App Setup",
            "url": "https://developers.facebook.com/apps/",
            "steps": [
                "Go to developers.facebook.com > My Apps > Create App.",
                "Select 'Business' as the app type.",
                "App name: '{APP_NAME}'. Select your business portfolio.",
                "Under 'App settings' > 'Basic', copy the App ID (Client ID) and App Secret (Client Secret).",
                "Under 'Products', add 'Facebook Login for Business'.",
                "In Facebook Login settings, add the redirect URI: {base_url}/api/oauth/callback",
                "Under 'Permissions and Features', request the permissions listed below.",
                "If managing a Facebook Page, select 'Facebook Page' as the account type.",
                "For page mode, the authenticating user must be an admin of the Facebook Page.",
                "Submit your app for App Review to access all permissions in production.",
            ],
            "permissions": [
                {
                    "scope": "public_profile",
                    "description": "Access basic profile (name, picture)",
                },
                {"scope": "email", "description": "Access email address"},
                {
                    "scope": "pages_show_list",
                    "description": "List pages the user manages",
                },
                {
                    "scope": "pages_manage_posts",
                    "description": "Create, edit, and delete page posts",
                },
                {
                    "scope": "pages_read_engagement",
                    "description": "Read reactions, shares, and comments on page posts",
                },
                {
                    "scope": "pages_read_user_content",
                    "description": "Read user-generated content on pages",
                },
                {
                    "scope": "pages_manage_metadata",
                    "description": "Manage page settings and metadata",
                },
                {
                    "scope": "pages_manage_engagement",
                    "description": "Manage comments and likes on page posts",
                },
                {
                    "scope": "read_insights",
                    "description": "Read page and post analytics/insights",
                },
            ],
            "notes": "Facebook requires App Review for most permissions beyond basic profile access. Personal posting is limited; page management provides the richest feature set. Page mode fetches administered pages after OAuth and stores the selected page ID and page access token. Page access tokens derived from long-lived user tokens do not expire.",
        },
    },
    "bluesky": {
        "name": "Bluesky",
        "description": "Posts (with images and video), Timelines, Search, Social Graph, Notifications (AT Protocol)",
        "logo": "/logos/bluesky.svg",
        "category": "integration",
        "integration_category": "social_media",
        "integration_types": ["social"],
        "auth_type": "api_key",
        "fields": ["api_key"],
        "extra_fields": {
            "handle": "Bluesky handle (e.g. user.bsky.social)",
            "pds_url": "PDS URL (optional, defaults to https://bsky.social)",
        },
        "setup_guide": {
            "title": "Bluesky App Password Setup",
            "url": "https://bsky.app/settings/app-passwords",
            "steps": [
                "Log in to Bluesky at bsky.app.",
                "Go to Settings > App Passwords.",
                "Click 'Add App Password' and give it a name like '{APP_NAME}'.",
                "Copy the generated app password and paste it in the 'API Key' field below.",
                "Enter your Bluesky handle (e.g. yourname.bsky.social) in the 'Handle' field.",
                "Leave the PDS URL empty unless you use a custom PDS (defaults to bsky.social).",
            ],
            "permissions": [
                {
                    "scope": "Posts",
                    "description": "Create, delete, and view posts (skeets)",
                },
                {
                    "scope": "Timeline",
                    "description": "Read home timeline and custom feeds",
                },
                {"scope": "Search", "description": "Search posts and users"},
                {
                    "scope": "Social Graph",
                    "description": "Follow/unfollow users, view followers/following",
                },
                {
                    "scope": "Likes & Reposts",
                    "description": "Like, unlike, repost, and unrepost",
                },
                {
                    "scope": "Notifications",
                    "description": "View notifications (likes, reposts, follows, replies, mentions)",
                },
            ],
            "notes": "Bluesky uses the AT Protocol. App passwords provide full account access — treat them like your main password. They can be revoked anytime from Settings > App Passwords. The integration authenticates via the AT Protocol session API and caches JWT tokens for the session duration. Custom PDS servers are supported for self-hosted instances.",
        },
    },
    "youtrack": {
        "name": "YouTrack",
        "description": "Issue Tracking & Project Management — Issues, Attachments, Work Items, Time Tracking, Custom Fields",
        "logo": "/logos/youtrack.svg",
        "category": "integration",
        "integration_category": "development",
        "integration_types": ["project_management"],
        "available_events": [],
        "auth_type": "api_key",
        "fields": ["api_key"],
        "extra_fields": {"base_url": "YouTrack Cloud URL (e.g. https://myorg.youtrack.cloud)"},
        "setup_guide": {
            "title": "YouTrack Permanent Token Setup",
            "url": "https://www.jetbrains.com/help/youtrack/cloud/Manage-Permanent-Token.html",
            "steps": [
                "Log in to your YouTrack Cloud instance.",
                "Click your avatar in the top-right corner and select 'Profile'.",
                "Go to the 'Account Security' tab.",
                "Under 'Tokens', click 'New token'.",
                "Enter a name (e.g. '{APP_NAME}') and select the scopes: "
                + "'YouTrack' (full access) or restrict to 'Read Issue', 'Create Issue', 'Update Issue' as needed.",
                "Click 'Create' and copy the generated token immediately — it will not be shown again.",
                "Enter your YouTrack Cloud URL in the 'Base URL' field below "
                + "(e.g. https://myorg.youtrack.cloud — no trailing slash).",
                "Paste the permanent token in the 'API Key' field.",
            ],
            "permissions": [
                {
                    "scope": "YouTrack",
                    "description": "Full access to read and write issues, comments, attachments, work items, and custom fields.",
                },
                {
                    "scope": "YouTrack (Read only)",
                    "description": "Read-only access to issues (and their attachments), projects, and custom fields.",
                },
            ],
            "notes": (
                "Only YouTrack Cloud (youtrack.cloud) is supported — not YouTrack Server or Hub. "
                "Permanent tokens are the recommended auth method for YouTrack Cloud integrations. "
                "The token inherits the permissions of the user who created it — use a dedicated "
                "service account for team-wide integrations. "
                "Tokens do not expire by default unless you set an expiration date during creation."
            ),
        },
    },
    # Any REST API: the tools are not in this catalog but in each
    # integration's own imported Swagger 2.0 / OpenAPI 3.x document
    # (``spec_import``), so ``available_tools`` stays empty here and the UI
    # lists the integration's tools instead. See app/services/openapi_import.
    "openapi": {
        "name": "OpenAPI / Swagger",
        "description": (
            "Connect any REST API described by a Swagger 2.0 or OpenAPI 3.x "
            "document — JSON or YAML, from a URL, a file or a ZIP of its files. "
            "Pick the operations to expose: each one becomes an agent tool that "
            "calls the API with the right path, query, header and body parameters."
        ),
        "logo": "/logos/openapi.svg",
        "category": "integration",
        "integration_category": "development",
        "integration_types": ["automation"],
        "available_events": [],
        "auth_type": "api_key",
        "supported_auth_types": ["api_key", "oauth", "client_credentials", "oauth_password", "none"],
        "fields": ["api_key"],
        "extra_fields": {"base_url": "API base URL (e.g. https://api.example.com/v1)"},
        # The creation form imports the spec, and the detail page shows its
        # tools; both read this marker rather than the provider key.
        "spec_import": "openapi",
        # OAuth2 scopes come from the imported spec's flow (edited per
        # integration), not from a catalog-wide list — see
        # ``integration_owns_oauth_scopes``.
        "oauth_scopes_from_integration": True,
        "setup_guide": {
            "title": "OpenAPI / Swagger integration",
            "steps": [
                "Paste the URL of the API's swagger.json / openapi.json / openapi.yaml, or upload the file. Swagger UI pages usually load the document from a URL such as …/swagger.json or …/v3/api-docs. A spec split across files is imported from its URL, or uploaded as a ZIP of all its files.",
                "Review the operations found in the document and switch off the ones agents should not call. Operations that cannot become tools (TRACE, file content in a parameter) are listed with the reason.",
                "Check the API base URL (pre-filled from the spec's servers) and pick how the API authenticates: API key, bearer token, HTTP Basic, OAuth2 (authorization code, implicit, device code, client credentials, password), OpenID Connect, a client certificate, or none.",
                "Enter the credential. For a sign-in flow (authorization code or implicit), register {base_url}/api/oauth/callback as the redirect URI in the API's OAuth app, create the integration, then click Authorize. With a device code, Authorize shows the code to enter on the API's sign-in page.",
                "Click 'Test Connection': it calls one enabled GET operation that needs no argument.",
            ],
            "permissions": [],
            "notes": (
                "Tools are generated from the document, never as code: each call "
                "goes through the platform's declarative HTTP client. Requests may "
                "reach public and private-network hosts; loopback, link-local and "
                "cloud-metadata addresses are refused. Refresh a URL-sourced spec "
                "from the API Spec tab when the API changes."
            ),
        },
    },
    "n8n": {
        "name": "n8n",
        "description": "Assignment Automation — Create, manage, execute, and monitor n8n assignments programmatically",
        "logo": "/logos/n8n.svg",
        "category": "integration",
        "integration_category": "automation",
        "integration_types": ["automation"],
        "available_events": [],
        "auth_type": "api_key",
        "fields": ["api_key"],
        "extra_fields": {"base_url": "n8n instance URL (e.g. https://your-n8n.example.com)"},
        "setup_guide": {
            "title": "n8n API Setup",
            "url": "https://docs.n8n.io/api/authentication/",
            "steps": [
                "Open your n8n instance in the browser.",
                "Go to Settings > API (or navigate to /settings/api).",
                "Click 'Create an API key' and copy the generated key.",
                "Paste the API key in the 'API Key' field below.",
                "Enter your n8n instance URL in the 'n8n instance URL' field (e.g. https://your-n8n.example.com — no trailing slash).",
                "Click 'Test Connection' to verify the integration is working.",
            ],
            "permissions": [
                {
                    "scope": "Assignments",
                    "description": "List, create, update, delete, activate, and deactivate assignments",
                },
                {
                    "scope": "Executions",
                    "description": "List, get, delete, and retry assignment executions",
                },
                {
                    "scope": "Tags",
                    "description": "Create, update, and delete tags for assignment organization",
                },
                {
                    "scope": "Variables",
                    "description": "Manage environment variables (Pro/Enterprise)",
                },
                {
                    "scope": "Credentials",
                    "description": "List and create credentials for assignment nodes",
                },
                {
                    "scope": "Projects",
                    "description": "Manage projects for grouping assignments and credentials",
                },
            ],
            "notes": "The n8n API requires an API key with appropriate permissions. API access may not be available on all n8n plans (not available during free trial). The API key provides full access to your n8n instance — treat it securely. Self-hosted instances have access to all API features.",
        },
    },
    # Channel bots (slack_bot, teams_bot, whatsapp_bot, telegram_bot) have been
    # moved to the dedicated ChannelBot entity — see api/v1/channel_bots.py.
}


def integration_owns_oauth_scopes(provider: str) -> bool:
    """Whether an OAuth integration's scopes are its own, not the catalog's.

    Native OAuth providers pin their scopes in this catalog, and the authorize
    and callback steps re-sync them from here (the single source of truth,
    which also cures scope drift). Imported providers and OpenAPI integrations
    have no catalog-wide list: their scopes come from the imported spec or
    bundle and are stored on the integration, so a sync would wipe them.
    """
    if provider.startswith("custom:"):
        return True
    return bool(PROVIDER_CATALOG.get(provider, {}).get("oauth_scopes_from_integration"))


# Providers whose build_tools() validates the token format before
# producing definitions need a compound dummy token so they don't
# early-return with an empty list (some of them also log a warning on
# every introspection call).  The ``https://…||__dummy__``
# format satisfies compound-token providers (split on ``||``) and
# RSS (needs a valid URL).
_URL_DUMMY_PROVIDERS = {
    "opencti",
    "openaev",
    "opencrq",
    "onepassword",
    "metabase",
    "drata",
    "credly",
    "concord",
    "google_search",
    "rss",
    "jira",
    "hibob",
    "bluesky",
    "n8n",
    # Grafana's compound token is base_url||api_key; without the URL dummy
    # its builder warns "token missing base URL or API key" on every
    # catalog fetch and returns zero tools.
    "grafana",
    "youtrack",
    "tanium",
    "xsoar",
    "qradar",
    # FortiSIEM's compound token is base_url||organization||username||password.
    "fortisiem",
    # Qualys' compound token is base_url||username||password.
    "qualys",
    # CrowdStrike's compound token is base_url||client_id||client_secret;
    # a URL-shaped dummy keeps the resolved base URL valid for introspection.
    "crowdstrike",
    "crowdstrike_ngsiem",
    # SentinelOne's compound token is base_url||api_token.
    "sentinelone",
    # HarfangLab's compound token is base_url||api_token.
    "harfanglab",
    # AttackIQ's compound token is base_url||api_token.
    "attackiq",
    # Elastic Security's compound token is kibana_url||es_url||api_key||space.
    "elastic_security",
    # Google SecOps's compound token is region||project||customer||sa_json.
    "google_secops",
    # Amazon SES's compound token is region||access_key_id||secret||config_set;
    # the URL-shaped dummy keeps introspection from tripping the compound parse.
    "ses",
    # AWS Security's compound token is region||access_key_id||secret.
    "aws_security",
    # ThreatConnect's compound token is base_url||access_id||secret_key.
    "threatconnect",
    # Cyware's compound token is base_url||access_id||secret_key.
    "cyware",
    # Infoblox's compound token is base_url||api_key.
    "infoblox",
    # Databricks' compound token is workspace_url||client_id||warehouse_id||secret;
    # the URL-shaped dummy keeps the resolved workspace host valid so
    # introspection sees the full tool surface.
    "databricks",
    # Rapid7's compound token is base_url||api_key.
    "rapid7",
    # Sumo Logic's compound token is base_url||access_id||access_key.
    "sumologic",
    # ServiceNow's compound token is base_url||username||password.
    "servicenow",
    # LogRhythm's compound token is base_url||api_token.
    "logrhythm",
    # Splunk's compound token is base_url||username||token.
    "splunk",
    # MISP's compound token is base_url||api_key.
    "misp",
    # Sekoia's compound token is base_url||api_key; the plain dummy parses to
    # (base_url="__dummy__", api_key="") and build_tools() early-returns with
    # zero tools, hiding the provider's tools from the catalog.
    "sekoia",
    # Sublime Security's compound token is base_url||api_key.
    "sublime",
    # Picus's compound token is base_url||refresh_token.
    "picus",
    # Anomali ThreatStream's compound token is base_url||username||api_key.
    "anomali",
    # SOCRadar's compound token is base_url||company_id||api_key.
    "socradar",
    # Flashpoint's compound token is base_url||api_token; the plain dummy
    # parses to (base_url="__dummy__", api_token="") and build_tools()
    # early-returns with zero tools, hiding the provider from the catalog.
    "flashpoint",
    # Doppel's compound token is base_url||api_key||user_api_key||org_code.
    "doppel",
    # Wiz's compound token is api_endpoint_url||client_id||client_secret; a
    # URL-shaped dummy keeps the resolved API endpoint valid for introspection.
    "wiz",
    # Recorded Future's compound token is base_url||api_token, TheHive's
    # base_url||api_key: the plain dummy leaves the secret empty and both
    # builders return no tools.
    "recorded_future",
    "thehive",
    # Channel bots: Teams' token is tenant_id||client_id||client_secret (the
    # legacy two-part form is accepted), WhatsApp's phone_number_id||token.
    "teams_bot",
    "whatsapp_bot",
    # WhatsApp's integration token is phone_number_id||access_token, like its bot.
    "whatsapp",
    # GitLab's token is base_url||token (base_url optional, gitlab.com).
    "gitlab",
    # Mattermost's token is base_url||token (base_url required).
    "mattermost",
}

# Providers whose builder validates more than the URL dummy's two parts, by the
# exact shape it parses. Tenable needs base_url||access_key||secret_key (both
# keys non-empty); Intel 471 api_username||api_key, with no URL.
_SHAPED_DUMMIES: dict[str, str] = {
    # NetSuite's token is account||mode||...||secret.
    "netsuite": "1234567||tba||__dummy__||__dummy__||__dummy__||__dummy__",
    # PagerDuty's token is region||from_email||api_key.
    "pagerduty": "us||agent@example.com||__dummy__",
    # Zendesk's token is subdomain||email||api_token.
    "zendesk": "acme||agent@example.com||__dummy__",
    "tenable": "https://dummy.example.com||__dummy__||__dummy__",
    "intel471": "__dummy__||__dummy__",
    # Aikido's token is region||client_id||client_secret.
    "aikido": "eu||__dummy__||__dummy__",
    # Airwallex's token is environment||client_id||login_as||api_key.
    "airwallex": "production||__dummy__||||__dummy__",
    # Rydoo's token is client_id||client_secret.
    "rydoo": "__dummy__||__dummy__",
    # Spendesk's token is environment||client_id||client_secret.
    "spendesk": "production||__dummy__||__dummy__",
    # Kubernetes' token is base_url||ca_segment||token (no CA: an empty segment).
    "kubernetes": "https://dummy.example.com||||__dummy__",
}

# Sentinel needs a 6-part compound token (tenant||client_id||secret||sub||rg||ws)
_SENTINEL_DUMMY = "||".join(["__dummy__"] * 6)
# Cortex XDR requires base_url||api_key||api_key_id||auth_mode (the api_key_id
# is mandatory, so the 2-part url dummy would early-return [] tools).
_XDR_DUMMY = "https://dummy.example.com||__dummy__||__dummy__||standard"


def build_dummy_token(provider: str) -> str:
    """Return the dummy credential used to introspect a provider's tools.

    Single source of truth for every code path that calls a provider's
    ``build_tools()`` purely to extract ``ToolDefinition`` metadata (the
    integration catalog and the per-integration tools endpoint, the derived
    HITL whitelist, the channel-bot catalog, tool approvals).  Each provider -
    integration or channel bot - gets the token shape its builder validates, so
    introspection never trips token-format guards (early-return with zero tools
    and/or a warning logged on every call).
    ``tests/test_dummy_token_shapes.py`` pins that every builder answers it.
    """
    if provider == "sentinel":
        return _SENTINEL_DUMMY
    if provider == "xdr":
        return _XDR_DUMMY
    if provider in _SHAPED_DUMMIES:
        return _SHAPED_DUMMIES[provider]
    if provider in _URL_DUMMY_PROVIDERS:
        return "https://dummy.example.com||__dummy__"
    return "__dummy__"


def build_provider_tools() -> dict[str, list[ProviderTool]]:
    """Build tool definitions for every provider with a tool builder.

    Tool builders only need a dummy token to produce their
    ``ToolDefinition`` metadata.
    """
    from app.services.integration_tools import PROVIDER_TOOLS as _BUILDERS

    return {provider: build_tools_for_provider(provider) or [] for provider in _BUILDERS}


def build_tools_for_provider(provider: str) -> list[ProviderTool] | None:
    """The tools of one provider, each with its read/write type; ``None`` when
    the provider has no tool builder.

    ``tool_type`` is the shared classifier's (``definition_tool_type``: the
    definition's ``read_only`` declaration, else its name), the one a read-only
    binding enforces.
    """
    from app.services.integration_tools import PROVIDER_TOOLS as _BUILDERS
    from app.tools.base import CallableToolExecutor

    builder = _BUILDERS.get(provider)
    if builder is None:
        return None
    try:
        defs = builder(
            build_dummy_token(provider), CallableToolExecutor(), refresh_fn=lambda: None
        )
    except Exception:
        logger.debug("Failed to build tools for provider %s", provider, exc_info=True)
        return []
    return [
        ProviderTool(
            name=d.name,
            description=d.description,
            tool_type=definition_tool_type(d),
        )
        for d in defs
    ]
