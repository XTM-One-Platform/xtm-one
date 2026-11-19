# SPDX-License-Identifier: Apache-2.0

"""What an MCP client is told about a tool of XTM One's MCP servers.

Every tool listed by ``/mcp/user``, ``/mcp/global``, ``/mcp/opencti``,
``/mcp/openaev``, ``/mcp/opencrq`` and ``/mcp/xtm`` is serialized here, with a
``title`` and the MCP tool ``annotations``. A client reads them to decide which
calls need the user's confirmation, and a catalog review checks them: the Claude
Connectors Directory refuses a tool that has no title or carries the wrong
read-only / destructive hint.

The annotations are READ from the tool name, deterministically, through the
platform's one read/write classifier (``services/tool_classification.py``,
which the integration catalog's "Read" badge and the agent loop's dedup cache
and loop breaker read too), plus two token sets that tell an additive write
(``create_*``, ``add_*``) from one that changes or removes what exists
(``update_*``, ``delete_*``, ``manage_*``). A read the classifier found on a
word of the name rather than its verb never outweighs a destructive token: a
wrong ``readOnlyHint: true`` is the unsafe direction, since clients skip the
confirmation of a read-only tool. They are hints, as the specification says:
the tool's own permission gates are what enforce anything. A name these rules cannot read keeps the specification's
defaults - not read-only, destructive - and ``tests/test_mcp_tool_metadata.py``
fails on such a name on any MCP surface until it is read here.

An agent exposed as a tool is a run of the agentic loop, which can do whatever
the agent's tools do: it is never read-only, may be destructive, and reaches an
open world.
"""

from __future__ import annotations

import hashlib
from typing import Any

from app.services.tool_classification import classify_tool_name
from app.tools.base import ToolDefinition, fit_descriptions_to_surface

#: The longest tool name the hosted clients accept: Claude's tool names match
#: ``^[a-zA-Z0-9_-]{1,64}$``, and the Claude Connectors Directory applies the
#: same bound. A longer name is a tool the model can never call.
MAX_TOOL_NAME_LENGTH = 64

#: Hex digits of the slug digest that keep two shortened agent names apart:
#: 64 bits, so no one can craft a second slug that shortens to the same name.
_SLUG_DIGEST_LENGTH = 16

#: Opens the digest of a shortened agent name. A slug is always lowercase
#: (``slugify`` and ``validate_slug`` both lower it), so no agent's slug can
#: ever BE another agent's shortened name.
_SHORTENED_MARKER = "X"

#: Names the shared classifier cannot read (no verb it knows) that are reads.
#: Kept here rather than in the classifier: adding ``inspect`` or ``load`` to
#: its read verbs would also put every such tool of every integration in the
#: agent loop's dedup cache.
_READ_ONLY_NAMES = frozenset(
    {
        # Compares two evaluation runs of the health suite, reading both.
        "compare_health_runs",
        # Its [DEPRECATED] name (dev-docs/platform-health.md section 2.3).
        "compare_concierge_runs",
        "inspect_agent_traces",
        "inspect_my_traces",
        "load_skill",
    }
)

#: Tools whose call starts an agent run, which can do whatever the agent's
#: tools do: annotated as an agent tool is (destructive, open world), whatever
#: their verb says.
_AGENT_RUN_NAMES = frozenset({"trigger_assignment", "run_agent_once"})

#: A write naming one of these anywhere changes or removes what already exists.
_DESTRUCTIVE_TOKENS = frozenset(
    {
        "activate",
        "archive",
        # ``bind_agent_to_intent`` upserts: it changes the priority or lock of a binding that exists.
        "bind",
        "cancel",
        "change",
        "clear",
        "close",
        "configure",
        "deactivate",
        "delete",
        "detach",
        "disable",
        "edit",
        "kill",
        "lock",
        "manage",
        "merge",
        "modify",
        "move",
        "overwrite",
        "patch",
        "purge",
        "remove",
        "rename",
        "replace",
        "reset",
        "restore",
        "retire",
        "revoke",
        "rollback",
        "set",
        "stop",
        "terminate",
        "toggle",
        "transfer",
        "trash",
        "unassign",
        "unbind",
        "unlink",
        "unlock",
        "unpublish",
        "update",
        "wipe",
    }
)

#: A write OPENING with one of these (and naming no destructive token) only
#: adds: a new object, a label, a run.
_ADDITIVE_VERBS = frozenset(
    {
        "add",
        "append",
        "attach",
        # Builds a new dataset (``build_dataset_from_traces``) from runs it reads.
        "build",
        "clone",
        "copy",
        "create",
        "duplicate",
        "generate",
        "import",
        "invite",
        "launch",
        "link",
        "post",
        "promote",
        # Files a new proposal (``propose_improvement``): changes nothing that exists.
        "propose",
        "publish",
        # Queues a new health cycle (``request_health_cycle``).
        "request",
        "run",
        "schedule",
        "send",
        "start",
        "store",
        "submit",
        "tag",
        "trigger",
        "upload",
        # Arms a new watch (``watch_platform_event``): adds one, changes nothing.
        "watch",
    }
)

#: A write opening with one of these leaves the same end state when repeated.
#: Deliberately short: ``update_*``, ``set_*`` or ``manage_*`` may well be
#: idempotent, but ``idempotentHint`` stays ``False`` for them - the
#: conservative answer, not a claim that repeating them differs.
_IDEMPOTENT_VERBS = frozenset({"delete", "detach", "remove", "revoke", "unassign", "unbind", "unlink"})

#: How a word of a tool name is written in its title.
_TITLE_WORDS = {
    "api": "API",
    "crq": "CRQ",
    "cve": "CVE",
    "cves": "CVEs",
    "id": "ID",
    "ids": "IDs",
    "json": "JSON",
    "kb": "KB",
    "llm": "LLM",
    "mcp": "MCP",
    "mitre": "MITRE",
    "nlq": "NLQ",
    "openaev": "OpenAEV",
    "opencrq": "OpenCRQ",
    "opencti": "OpenCTI",
    "stix": "STIX",
    "tef": "TEF",
    "url": "URL",
    "xtm": "XTM",
}


def tool_title(name: str) -> str:
    """The human title of a tool, read from its snake_case name."""
    words = [word for word in name.split("_") if word]
    if not words:
        return name
    titled = [_TITLE_WORDS.get(word.lower(), word) for word in words]
    first = titled[0]
    if first == words[0]:
        titled[0] = first[:1].upper() + first[1:]
    return " ".join(titled)


def _read_name(name: str) -> tuple[bool, bool, bool]:
    """``(read_only, destructive, by_name)`` for a tool *name*.

    ``by_name`` is ``False`` when the rules found nothing in the name and the
    answer is the specification's default (not read-only, destructive).
    """
    lowered = name.lower()
    if lowered in _READ_ONLY_NAMES:
        return True, False, True
    if lowered in _AGENT_RUN_NAMES:
        return False, True, True
    tool_type, classified_by = classify_tool_name(lowered)
    tokens = lowered.split("_")
    destructive = any(token in _DESTRUCTIVE_TOKENS for token in tokens)
    # A read found on a word of the name, not its verb (``intune_status``),
    # does not outweigh a destructive token: ``intune_status_purge`` is no read.
    if tool_type == "read" and not (classified_by == "read_token" and destructive):
        return True, False, True
    if destructive:
        return False, True, True
    if tokens[0] in _ADDITIVE_VERBS:
        return False, False, True
    return False, True, False


def annotations_read_from_name(name: str) -> bool:
    """Whether the annotations of *name* come from its name, not a default."""
    return _read_name(name)[2]


def tool_annotations(name: str, *, title: str | None = None) -> dict[str, Any]:
    """The MCP ``ToolAnnotations`` of the tool called *name*.

    ``openWorldHint`` is ``False``: the platform servers and XTM One's own act
    on one platform's data, a closed domain - except a tool that starts an
    agent run, which reaches whatever the agent's tools reach."""
    read_only, destructive, _by_name = _read_name(name)
    annotations: dict[str, Any] = {
        "title": title or tool_title(name),
        "readOnlyHint": read_only,
        "destructiveHint": destructive,
    }
    if not read_only:
        annotations["idempotentHint"] = name.lower().split("_")[0] in _IDEMPOTENT_VERBS
    annotations["openWorldHint"] = name.lower() in _AGENT_RUN_NAMES
    return annotations


def tool_to_mcp(defn: ToolDefinition) -> dict[str, Any]:
    """A tool definition in the MCP ``tools/list`` wire format."""
    title = tool_title(defn.name)
    return {
        "name": defn.name,
        "title": title,
        "description": defn.description,
        "inputSchema": defn.input_schema or {"type": "object", "properties": {}},
        "annotations": tool_annotations(defn.name, title=title),
    }


def tools_to_mcp(definitions: list[ToolDefinition]) -> list[dict[str, Any]]:
    """The ``tools/list`` result of a server that serves exactly *definitions*.

    Each description is resolved against that same list
    (``fit_descriptions_to_surface``), as the agent loop resolves it,
    so a tool names another tool only when this server serves it.
    """
    return [tool_to_mcp(d) for d in fit_descriptions_to_surface(definitions) or ()]


def agent_tool_name(slug: str) -> str:
    """The MCP tool name of the agent whose slug is *slug*.

    The slug itself when it fits :data:`MAX_TOOL_NAME_LENGTH`; otherwise its
    head, ``-X`` and a 64-bit digest of the whole slug: stable from one
    request to the next, two long slugs sharing a head stay apart, and the
    uppercase marker keeps any slug from naming a shortened agent.
    """
    if len(slug) <= MAX_TOOL_NAME_LENGTH:
        return slug
    digest = hashlib.sha256(slug.encode("utf-8")).hexdigest()[:_SLUG_DIGEST_LENGTH]
    suffix = f"-{_SHORTENED_MARKER}{digest}"
    head = slug[: MAX_TOOL_NAME_LENGTH - len(suffix)].rstrip("-")
    return f"{head}{suffix}"


def agent_tool_annotations(title: str) -> dict[str, Any]:
    """The annotations of an agent exposed as a tool: a run of the agentic loop."""
    return {
        "title": title,
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": False,
        "openWorldHint": True,
    }
