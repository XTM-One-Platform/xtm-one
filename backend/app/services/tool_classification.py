"""The one read/write classifier of tool names.

A tool is ``read`` or ``write``, read from its snake_case name. The integration
catalog's "Read" badge (``integration_catalog``) and the HITL whitelists
derived from it, the agent loop's dedup cache and loop breaker
(``tools/agent_loop.py``) and the annotations of XTM One's MCP servers
(``services/mcp_tool_metadata.py``) all read this module, so the
customer-facing guarantee and what the runtime does can never drift.
Standard library only: cheap to import from any layer.
"""

from __future__ import annotations

from typing import Literal

# The "Read" badge is a security guarantee surfaced to customers: a tool
# classified as read must be strictly non-destructive. Classification
# is token-based (names split on "_") so noun fragments can never leak a verb
# (e.g. "credit" does not match "edit", "flagged" does not match "flag"), and
# tools whose verb is unknown FAIL SAFE to "write" instead of "read".

# Leading verbs that guarantee a non-destructive operation. Only checked in
# first position: tool names follow the verb_object convention, so e.g.
# get_qradar_reference_set is a read even though "set" appears later.
#
# A verb that names a read for some services and a change for others stays
# out, whatever one integration means by it: this set also classifies the
# tools of company-managed MCP servers, whose reads skip human approval
# deployment-wide. ``reverse`` (a transaction), ``identify`` (an analytics
# user's traits), ``triage`` and ``resolve`` (an issue's state), ``enrich``
# (a CRM record), ``assess`` (an assessment record) and ``analyze`` (a link
# followed, a sample sent) all write somewhere, so tools named with them keep
# asking.
READ_VERBS = {
    "ask",
    "check",
    "count",
    "describe",
    "discover",
    "download",
    "export",
    "extract",
    "fetch",
    "find",
    "get",
    "list",
    "lookup",
    "parse",
    "preview",
    "query",
    "read",
    "retrieve",
    "search",
    "validate",
    "view",
}

# Mutating / privileged verbs, matched as whole tokens anywhere in the name.
# This is what catches consolidated tools (manage_*, edit_*) and verbs hidden
# behind a noun prefix (intune_lock_device, mark_microsoft_email_read).
WRITE_TOKENS = {
    "acknowledge",
    "activate",
    "add",
    "allowlist",
    "append",
    "apply",
    "archive",
    "assign",
    "ban",
    "blocklist",
    "cancel",
    "change",
    "clone",
    "close",
    "collect",
    "complete",
    "configure",
    "contain",
    "copy",
    "create",
    "deactivate",
    "delete",
    "deploy",
    "disable",
    "duplicate",
    "edit",
    "enable",
    "exclude",
    "execute",
    "flag",
    "follow",
    "forward",
    "import",
    "invite",
    "isolate",
    "kick",
    "kill",
    "launch",
    "lift",
    "like",
    "link",
    "lock",
    "manage",
    "map",
    "mark",
    "merge",
    "mitigate",
    "modify",
    "move",
    "mute",
    "open",
    "patch",
    "pause",
    "pin",
    "post",
    "promote",
    "publish",
    "quarantine",
    "react",
    "reboot",
    "reconnect",
    "release",
    "remove",
    "rename",
    "reopen",
    "replace",
    "reply",
    "repost",
    "request",
    "reschedule",
    "reset",
    "respond",
    "restore",
    "restrict",
    "resume",
    "retire",
    "retry",
    "retweet",
    "revert",
    "revoke",
    "rollback",
    "run",
    "scan",
    "schedule",
    "send",
    "set",
    "share",
    "start",
    "stop",
    "store",
    "submit",
    "sync",
    "tag",
    "terminate",
    "toggle",
    "transfer",
    "transition",
    "trash",
    "trigger",
    "unarchive",
    "unassign",
    "unflag",
    "unfollow",
    "unisolate",
    "unlike",
    "unlink",
    "unlock",
    "unmute",
    "unpin",
    "unpublish",
    "unrestrict",
    "unretweet",
    "update",
    "upload",
    "use",
    "wipe",
    "write",
}

# Read-safe tokens for noun-prefixed tools that do not start with a read verb
# (e.g. intune_get_device, intune_compliance_overview, full_text_search_*).
# Only consulted after the write-token check. ``lookup`` reads only as the
# leading verb: later in a name it is the object of an unknown verb
# (``purge_lookup_cache``, ``whois_credit_lookup``), which must keep failing
# safe to "write".
READ_TOKENS = (READ_VERBS - {"lookup"}) | {
    "overview",
    "stats",
    "status",
    "summary",
}

READ_OVERRIDES = {
    # "run" is a write token, but this executes a SAVED card by id.
    # ``run_metabase_query`` is NOT here: it takes a free-form statement.
    "run_metabase_question",
    # "map" is not a read verb, but this only resolves ATT&CK ids against the
    # local ATLAS crosswalk index - a pure read.
    "map_attack_to_atlas",
    # "render" is not a read verb (``render_video`` starts a render job), but
    # this only validates the rows it is handed into a chat chart block:
    # nothing is persisted and nothing leaves the platform.
    "render_chart",
}

WRITE_OVERRIDES = {
    # Arbitrary GraphQL can execute mutations, not just queries.
    "github_graphql_query",
    # Same argument: the ``query`` string is POSTed verbatim as a native
    # statement to /api/dataset with no restriction on what it may contain,
    # unlike ``query_databricks_sql``, which allow-lists SELECT/WITH/SHOW/...
    # Classified as a read, DELETE FROM against the warehouse never reached a
    # human.
    "run_metabase_query",
}


#: Which rule of :func:`classify_tool_name` decided a name.
ClassifiedBy = Literal["override", "read_verb", "write_token", "read_token", "default"]


def classify_tool_name(tool_name: str) -> tuple[str, ClassifiedBy]:
    """Classify a tool as ``read`` or ``write`` from its name, with the rule
    that decided it. Order matters:

    1. explicit overrides (``override``);
    2. a conjunction naming a write (``get_or_create_entity``,
       ``search_and_delete``) makes it a write, leading read verb or not
       (``write_token``);
    3. a leading read verb wins, verb_object convention (``read_verb``);
    4. any write token anywhere makes it a write (``write_token``);
    5. a read token anywhere - a noun-prefixed read tool - makes it a read
       (``read_token``): the weakest evidence, a word of the name rather than
       its verb;
    6. unknown names default to ``write`` (``default``) - never present an
       unclassified tool as a safe read. The answer is then a precaution, not
       a reading of the name.
    """
    name_lower = tool_name.lower()
    if name_lower in READ_OVERRIDES:
        return "read", "override"
    if name_lower in WRITE_OVERRIDES:
        return "write", "override"
    tokens = name_lower.split("_")
    # A conjunction that names a write beats the leading read verb:
    # ``get_or_create_entity`` creates and ``search_and_delete`` deletes, yet
    # both open with a read verb and were classified "read". The rule stays
    # narrow deliberately — "any write token anywhere" would re-gate genuine
    # reads where the token is a noun (``get_opengrc_crq_run``,
    # ``list_opengrc_crq_run_history``), which is why the leading verb wins in
    # the first place.
    if any(joiner in tokens for joiner in ("or", "and")) and any(
        token in WRITE_TOKENS for token in tokens
    ):
        return "write", "write_token"
    if tokens[0] in READ_VERBS:
        return "read", "read_verb"
    if any(token in WRITE_TOKENS for token in tokens):
        return "write", "write_token"
    if any(token in READ_TOKENS for token in tokens):
        return "read", "read_token"
    return "write", "default"


def infer_tool_type(tool_name: str) -> str:
    """Classify a tool as ``read`` or ``write`` from its name.

    The rules are :func:`classify_tool_name`'s; an unknown name is a ``write``.
    """
    return classify_tool_name(tool_name)[0]


def definition_tool_type(definition: object) -> str:
    """``read`` or ``write`` for a tool definition: its own declaration first.

    A definition declared ``read_only`` (``ToolDefinition.read_only``: no call
    of it changes anything, whatever its arguments) is a read whatever its
    verb, the statement ``withhold_write_tools`` and the repeat guard already
    trust; otherwise its name decides (:func:`infer_tool_type`). Duck-typed so
    this module stays importable from any layer.
    """
    if getattr(definition, "read_only", False) is True:
        return "read"
    return infer_tool_type(str(getattr(definition, "name", "") or ""))
