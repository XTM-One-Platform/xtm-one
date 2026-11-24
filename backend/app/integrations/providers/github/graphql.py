"""GitHub GraphQL tools: the raw escape hatch and its query-only sibling.

``github_graphql_query`` runs any document, mutations included, so it is a
write tool and a read-only binding withholds it. ``query_github_graphql`` runs
query documents only: :func:`read_only_refusal` reads the document's top-level
definitions before anything is sent and refuses a mutation, a subscription or
anything it cannot read, so the tool is a read and a read-only binding keeps it.

``query_github_graphql`` is a ``workspace_ingest`` tool: with
``to_workspace_path`` the whole JSON answer, uncut, is written to the sandbox
workspace instead of returned. A mutation that fails (a gateway 5xx, a 200
with ``errors``) may have been applied in part: GitHub runs each top-level
field on its own and rolls nothing back, so the error names the response keys
the document sent, those the answer's partial ``data`` shows applied and those
not confirmed (:func:`partial_mutation_report`).
"""

from __future__ import annotations

import json
import re
from typing import Any

from app.core.egress import egress_client
from app.tools.base import CallableToolExecutor, ToolDefinition, workspace_ingest_requested

from . import _common
from ._common import _MAX_GRAPHQL_CHARS

_SOURCE = "integration:github"

#: Top-level definitions a query-only document may hold.
_READ_DEFINITIONS = frozenset({"query", "fragment"})

#: One lexical token of a GraphQL document, or a span the guard skips. Block
#: strings come before plain strings so ``"""`` is never read as ``""``.
_TOKEN = re.compile(
    r'(?P<skip>[\s,\ufeff]+|#[^\n\r]*|"""(?:\\"""|[^"]|"(?!""))*"""|"(?:\\.|[^"\\\n\r])*")'
    r"|(?P<name>[_A-Za-z][_0-9A-Za-z]*)"
    r"|(?P<punct>[{}()])"
    r"|(?P<other>.)",
    re.DOTALL,
)


def read_only_refusal(document: str) -> str | None:
    """Why *document* is not a query-only GraphQL document, or ``None`` when it is.

    Each top-level definition must be a ``query`` operation, the ``{ ... }``
    query shorthand or a ``fragment``. Names inside strings, block strings and
    comments are skipped, so a mutation is found wherever it sits and a field
    or argument named ``mutation`` inside a selection is not mistaken for one.
    A document the guard cannot read (unbalanced braces, no operation) is
    refused too: the doubt is never sent.
    """
    braces = parens = 0
    expect_definition = True
    operations = 0
    for match in _TOKEN.finditer(document or ""):
        kind = match.lastgroup
        if kind == "skip":
            continue
        text = match.group()
        if expect_definition:
            if kind == "name":
                if text not in _READ_DEFINITIONS:
                    return f"a top-level '{text}' definition"
                operations += text == "query"
                expect_definition = False
                continue
            if text == "{":
                operations += 1
                expect_definition = False
            else:
                return f"an unexpected '{text}' at the top level"
        if text == "{":
            braces += 1
        elif text == "}":
            braces -= 1
            if braces < 0:
                return "unbalanced braces"
            if braces == 0 and parens == 0:
                expect_definition = True
        elif text == "(":
            parens += 1
        elif text == ")":
            parens -= 1
            if parens < 0:
                return "unbalanced parentheses"
    if braces or parens:
        return "an unfinished document"
    if not operations:
        return "no query operation"
    return None


#: Aliases a mutation batch should stay under: one 5xx then costs a re-read of
#: ten objects, not of thirty.
_ALIAS_BATCH_HINT = 10

#: Response keys named in a partial-mutation report before the list is cut.
_MAX_KEYS_NAMED = 60


def mutation_response_keys(document: str) -> list[str]:
    """The response keys (alias, else field name) of the top-level fields of the mutations of *document*.

    Empty when *document* holds no ``mutation`` operation. Fields nested in a
    selection, arguments, directives and fragment spreads are not keys.
    """
    tokens = [(m.lastgroup, m.group()) for m in _TOKEN.finditer(document or "") if m.lastgroup != "skip"]
    keys: list[str] = []
    braces = parens = 0
    expect_definition = True
    in_mutation = False
    index = 0
    while index < len(tokens):
        kind, text = tokens[index]
        previous = tokens[index - 1][1] if index else ""
        following = tokens[index + 1][1] if index + 1 < len(tokens) else ""
        if braces == 0 and parens == 0 and expect_definition and kind == "name":
            in_mutation = text == "mutation"
            expect_definition = False
        elif text == "{":
            if braces == 0 and parens == 0 and expect_definition:
                in_mutation = False  # the query shorthand
            expect_definition = False
            braces += 1
        elif text == "}":
            braces -= 1
            if braces == 0 and parens == 0:
                expect_definition = True
        elif text == "(":
            parens += 1
        elif text == ")":
            parens -= 1
        elif in_mutation and braces == 1 and parens == 0 and kind == "name" and previous not in {"@", "."}:
            keys.append(text)
            if following == ":":
                index += 2  # the aliased field's own name is not a response key
        index += 1
    return keys


#: Merge mutations the raw tool never sends: each has a dedicated tool that reads the pull request's
#: review state first (manage_github_pr enable_automerge, merge_github_pr), and sent raw they let
#: GitHub land a pull request that still waits for its review.
_GUARDED_MERGE_MUTATIONS = frozenset({"enablePullRequestAutoMerge", "enqueuePullRequest", "mergePullRequest"})


def guarded_merge_refusal(document: str) -> str | None:
    """The refusal when a mutation of *document* calls a guarded merge field, else ``None``.

    Names inside strings, block strings and comments are skipped, as in :func:`read_only_refusal`.
    """
    if not mutation_response_keys(document):
        return None
    names = {m.group() for m in _TOKEN.finditer(document or "") if m.lastgroup == "name"}
    found = sorted(names & _GUARDED_MERGE_MUTATIONS)
    if not found:
        return None
    return (
        f"Error: mutation not sent, nothing was written: {', '.join(found)} goes through "
        "manage_github_pr (action=enable_automerge) or merge_github_pr, which read the pull request's "
        "review decision and review-loop verdict first."
    )


def _named(keys: list[str]) -> str:
    shown = ", ".join(keys[:_MAX_KEYS_NAMED])
    return shown + (f" (+{len(keys) - _MAX_KEYS_NAMED} more)" if len(keys) > _MAX_KEYS_NAMED else "")


def partial_mutation_report(keys: list[str], partial: dict[str, Any] | None) -> str:
    """What a failed mutation of *keys* applied, read from the answer's partial ``data``."""
    lines = [
        "",
        f"This mutation may have been applied in part: GitHub runs each of its {len(keys)} top-level field(s) "
        f"on its own and rolls nothing back, and this tool never resends a mutation. Sent: {_named(keys)}.",
    ]
    if partial is None:
        lines.append(
            "The answer carries no data, so no field is confirmed either way: re-read the objects these "
            "fields change before resending any of them."
        )
    else:
        applied = [key for key in keys if partial.get(key) is not None]
        unconfirmed = [key for key in keys if partial.get(key) is None]
        lines.append(f"Applied per the answer's partial data: {_named(applied) if applied else 'none'}.")
        if unconfirmed:
            lines.append(
                f"Not confirmed: {_named(unconfirmed)}. Re-read these objects before resending: a field "
                "GitHub dropped from the answer may still have been applied."
            )
    if len(keys) > _ALIAS_BATCH_HINT:
        lines.append(
            f"Send at most {_ALIAS_BATCH_HINT} aliases per mutation: a gateway error then costs a re-read of "
            "that batch only."
        )
    return "\n".join(lines)


def register(
    headers: dict[str, str],
    executor: CallableToolExecutor,
    mark_self_action: Any,
) -> list[ToolDefinition]:
    """Build the GitHub GraphQL tools and register their callables."""

    async def _run(query: str, variables: str, *, ingest_hint: bool = False) -> str:
        parsed_variables: dict[str, Any] | None = None
        if variables:
            try:
                parsed_variables = json.loads(variables)
            except json.JSONDecodeError:
                return (
                    "Error: 'variables' must be a valid JSON string "
                    '(e.g. \'{"owner": "octocat", "number": 1}\').'
                )
        mutation_keys = mutation_response_keys(query)
        guarded = guarded_merge_refusal(query) if mutation_keys else None
        if guarded:
            return guarded
        async with egress_client(timeout=60) as client:
            data, err = await _common.gh_graphql(
                client,
                headers,
                query,
                parsed_variables,
                action="execute GraphQL query",
                keep_partial=bool(mutation_keys),
            )
            if err:
                return err + partial_mutation_report(mutation_keys, data) if mutation_keys else err
            text = json.dumps(data, indent=2, default=str)
            if workspace_ingest_requested():
                # Bound for the sandbox workspace: the whole answer, uncut.
                return text
            if len(text) > _MAX_GRAPHQL_CHARS:
                hint = "; pass to_workspace_path for the whole answer in the sandbox workspace" if ingest_hint else ""
                text = text[:_MAX_GRAPHQL_CHARS] + f"\n… (truncated at {_MAX_GRAPHQL_CHARS:,} of {len(text):,} chars{hint})"
            return text

    async def github_graphql_query(query: str, variables: str = "") -> str:
        """Execute a GraphQL query or mutation against the GitHub GraphQL API.

        This unlocks data not exposed by the REST API or the first-class
        project tools. Prefer the dedicated list_github_projects /
        list_github_project_items / set_github_project_field tools for Projects
        v2 work; use this only for queries those tools do not cover.
        """
        return await _run(query, variables)

    async def query_github_graphql(query: str, variables: str = "") -> str:
        """Execute a query-only GraphQL document; anything else is refused unsent."""
        reason = read_only_refusal(query)
        if reason is not None:
            return (
                f"Error: query_github_graphql runs query documents only and found {reason}: "
                "nothing was sent to GitHub. A mutation or subscription is refused here, so this tool "
                "stays available on a read-only GitHub binding; use github_graphql_query for a write "
                "where the binding allows it."
            )
        return await _run(query, variables, ingest_hint=True)

    executor.register("github_graphql_query", github_graphql_query)
    executor.register("query_github_graphql", query_github_graphql)

    variables_schema = {
        "type": "string",
        "description": 'JSON-encoded variables for the query (optional). Example: \'{"owner": "octocat", "number": 1}\'',
        "default": "",
    }
    return [
        ToolDefinition(
            name="github_graphql_query",
            description=(
                "Execute an arbitrary GraphQL query or mutation against the GitHub GraphQL API "
                "(https://api.github.com/graphql). Use this only for data the REST tools and the first-class Projects "
                "v2 tools (list_github_projects, get_github_project, list_github_project_items, "
                "set_github_project_field, ...) do not cover. Pass parameters via the 'variables' JSON string. "
                "Never move a branch with a mutation (updateRef, updateRefs): moving a pull request's branch onto "
                "its base makes GitHub close the pull request; use manage_github_ref action=update_branch, which "
                "refuses that move. A failed mutation may have been applied in part (GitHub rolls nothing "
                "back): the error names the aliases the answer shows applied and those to re-read before "
                "resending; keep about 10 aliases per mutation."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "A GitHub GraphQL query or mutation string."},
                    "variables": variables_schema,
                },
                "required": ["query"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="query_github_graphql",
            description=(
                "Run a read-only GraphQL query against the GitHub GraphQL API: query operations and fragments "
                "only. A mutation or subscription is refused before anything is sent, so the tool stays available "
                "on a read-only GitHub binding. Use it for reads the REST tools need several calls for (a pull "
                "request's closing issues, many objects in one request); variables as a JSON string. "
                "resource(url:) is a root field, not a field of Repository. For a large answer (search pages) "
                "pass to_workspace_path: the whole JSON lands in the sandbox workspace, uncut."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "A GitHub GraphQL query document (no mutation)."},
                    "variables": variables_schema,
                },
                "required": ["query"],
            },
            source=_SOURCE,
            priority=0,
            workspace_ingest=True,
        ),
    ]
