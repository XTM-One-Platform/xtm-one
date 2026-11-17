"""The generic layers carry no workaround (architecture invariant 18).

XTM One is a generic platform: the agent loop, the tool base classes, the
memory, the chat and assignment orchestration and the prompt assembly work the
same for every integration, every language and every model. A fix shaped like
the incident that revealed it - a vendor's name, a tool's name, a list of the
words a model or a user happened to write - does not belong there; the owner
declares what it needs (the integration catalog, the provider adapter, the
``ToolDefinition``), the model answers in a structure, or the platform uses a
fact it holds.

The checks, on the parsed source of every module of :data:`GENERIC_LAYER_PATTERNS`:

* no import of an integration provider or of a vendor's channel adapter
  (relative imports resolved), and no string literal naming an integration
  provider (its catalog slug or display name, a name built on the slug such
  as ``download_notion_file``, or the name inside prose) or a vendor channel;
* no module-level collection or regular expression of natural-language
  PHRASES (two or more multi-word entries such as ``"please continue"`` or
  ``would you like``): a phrase list reading what a model or a person wrote is
  a prompt in another form, bound to one language and never complete;
* no WORD list: a module-level collection named for words (``..._WORDS``,
  ``..._VERBS``, ``..._TOKENS``, ...) or a case-insensitive regex of plain
  words, the same thing one word at a time.

What already breaks a rule is listed in :data:`KNOWN_VENDOR_IMPORTS`,
:data:`KNOWN_VENDOR_LITERALS`, :data:`KNOWN_PHRASE_LISTS` and
:data:`KNOWN_WORD_LISTS`: the open part. The lists only shrink - an
entry that no longer matches fails the build until it is removed - and a new
entry is never the fix.
"""

from __future__ import annotations

import ast
import re
from collections import Counter

import pytest

from tests import app_sources

APP = app_sources.APP_DIR

#: The generic layers, as patterns relative to ``app/``: a new module of a
#: layer is covered the day it is written.
GENERIC_LAYER_PATTERNS: tuple[str, ...] = (
    "llm/router.py",
    "services/url_guard.py",
    "tools/*.py",
    "services/builtin_tools/*.py",
    "services/agent_*.py",
    "services/assignment_*.py",
    "services/autonomous.py",
    "services/channel_*.py",
    "services/chat_*.py",
    "services/content_safety.py",
    "services/conversation_*.py",
    "services/delegation.py",
    "services/general_chat.py",
    "services/mcp_tool_*.py",
    "services/memory_*.py",
    "services/parent_wake.py",
    "services/persistent_memory.py",
    "services/presentations.py",
    "services/prompt_*.py",
    "services/sub_agent*.py",
    "services/tool_*.py",
    "services/user_memory*.py",
)

GENERIC_MODULES: tuple[str, ...] = tuple(
    sorted({app_sources.relative(path) for pattern in GENERIC_LAYER_PATTERNS for path in APP.glob(pattern)})
)

#: Literals naming a suite product that are not vendor logic, with the reason.
#: The suite's own products (OpenCTI, OpenAEV, OpenCRQ) are integrations too,
#: but the platform registration that speaks for them is not.
PLATFORM_IDENTITY_LITERALS: dict[str, frozenset[str]] = {
    # OpenAEV's legacy registration identifier ``openbas`` and its new one,
    # resolved as one platform identity.
    "tools/approval.py": frozenset({"openaev"}),
    # OpenCTI's own ``/dashboard/xtm-hub`` route section, in the table of
    # OpenCTI routes a link may point at: a path segment of OpenCTI, not the
    # XTM Hub integration.
    "services/url_guard.py": frozenset({"xtm-hub"}),
}

#: Vendor channel adapters (the generic ones, ``channels.base`` and
#: ``channels.router``, are the interface a generic layer goes through; the
#: web widget is the platform's own channel).
VENDOR_CHANNEL_MODULES = frozenset({"slack", "teams", "discord", "telegram", "whatsapp", "google_chat"})

#: The channel layer's generic interface; every other module of ``app/channels``
#: is a vendor's adapter (its submodules included, such as ``teams_graph_worker``).
GENERIC_CHANNEL_MODULES = frozenset({"__init__", "base", "router"})
VENDOR_CHANNEL_ADAPTERS = frozenset(path.stem for path in (APP / "channels").glob("*.py")) - GENERIC_CHANNEL_MODULES

#: Provider slugs too short to tell from an ordinary literal; their catalog
#: display names are still matched.
COMMON_WORD_PROVIDERS = frozenset({"x"})

#: Vendor imports of a generic module, still open.
KNOWN_VENDOR_IMPORTS: dict[str, frozenset[str]] = {
    # The scheduler renews Teams subscriptions by hand.
    "services/autonomous.py": frozenset(
        {
            "app.channels.teams",
        }
    ),
    # Cross-channel continuation branches on Slack / Teams.
    "services/channel_continuation.py": frozenset(
        {
            "app.channels.slack",
            "app.channels.teams",
        }
    ),
    # A background task's result is delivered to Slack / Teams by hand.
    "services/sub_agent.py": frozenset({"app.channels.slack", "app.channels.teams"}),
}

#: Per module, each literal naming a vendor (for prose, the vendor's name) and how many
#: times it occurs, still open: one more occurrence fails, one fewer must be counted down.
KNOWN_VENDOR_LITERALS: dict[str, dict[str, int]] = {
    # Providers recognised by model-name prefixes (capabilities from model metadata).
    "llm/router.py": {
        "anthropic": 2,
        "anthropic.": 1,
        "anthropic/": 1,
        "azure_openai": 4,
        "custom_openai": 1,
        "gemini": 7,
        "gemini-": 1,
        "google/": 1,
        "mistral": 5,
        "mistral-": 1,
        "mistral-embed": 1,
        "mistral.": 1,
        "open-mistral-": 1,
        "openai": 8,
        "openai/": 1,
        "openrouter": 2,
    },
    # The OpenCTI link route and the OpenAEV write restriction in the prompt, the messaging-provider set.
    "services/agent_chat.py": {
        "openaev": 1,
    },
    # Posts delivered per vendor (Microsoft mail, Slack, Teams).
    "services/agent_posts.py": {
        "microsoft_mail_draft": 2,
        "microsoft_mail_sent": 1,
        "slack": 4,
        "teams": 2,
    },
    # Default ordering keys per mail provider.
    "services/assignment_dispatch.py": {
        "google": 1,
        "microsoft": 1,
    },
    # Channel names in the run prompt.
    "services/assignment_helpers.py": {
        "slack": 1,
        "teams": 1,
    },
    # Vendor steps in the scheduler tick (Teams subscriptions, GitHub, Nova).
    "services/autonomous.py": {
        "_teams_subscription_tick_counter": 1,
        "github": 3,
        "nova": 2,
        "teams": 2,
    },
    # The OpenAEV attack-path orchestrator's tool restrictions.
    "services/builtin_tools/__init__.py": {
        "is_openaev_arsenal_advisory_agent": 1,
        "is_openaev_orchestrator_agent": 1,
        "is_openaev_write_tool": 1,
        "reset_advisory_openaev_writes": 1,
        "set_advisory_openaev_writes": 1,
        "strip_openaev_write_tools": 1,
    },
    # Vendors named in the platform builder's tool texts.
    "services/builtin_tools/agent_builder.py": {
        "anthropic": 1,
        "filigran ai": 1,
        "gemini": 1,
        "github": 9,
        "hubspot": 1,
        "microsoft": 4,
        "notion": 1,
        "openaev": 1,
        "openai": 1,
        "opencti": 1,
        "slack": 7,
    },
    # A vendor named in a built-in tool's text.
    "services/builtin_tools/computer_use.py": {
        "drata": 2,
    },
    # The OpenAEV orchestrator and advisory agents, by agent slug and tool name.
    "services/builtin_tools/filtering.py": {
        "advisory_openaev_writes": 1,
        "author_openaev_attack_path_step": 1,
        "consume_openaev_directives": 1,
        "delete_openaev_attack_path_step": 1,
        "ensure_openaev_target_team": 1,
        "evaluate_openaev_attack_path": 1,
        "manage_openaev_": 1,
        "manage_openaev_threat_arsenal": 2,
        "openaev": 3,
        "openaev-attack-path-orchestrator": 2,
        "openaev-payload-creator": 1,
        "promote_openaev_finding_to_asset": 1,
        "record_openaev_event": 1,
        "set_openaev_run_scope": 1,
        "update_openaev_attack_path_step": 1,
        "update_openaev_run_status": 1,
    },
    # The memory-write screen bound to the Nova guard by name.
    "services/builtin_tools/memory.py": {
        "nova": 3,
        "nova_guard": 2,
    },
    # A vendor named in a built-in tool's text.
    "services/builtin_tools/template_authoring.py": {
        "openaev": 1,
    },
    # A vendor named in a built-in tool's text.
    "services/builtin_tools/workspaces.py": {
        "opencti": 1,
    },
    # Cross-channel continuation branches per channel vendor.
    "services/channel_continuation.py": {
        "Discord": 1,
        "Slack": 1,
        "Telegram": 1,
        "WhatsApp": 1,
        "discord": 1,
        "google": 1,
        "google_chat": 1,
        "microsoft": 1,
        "slack": 5,
        "teams": 5,
        "teams_chat_unknown": 1,
        "telegram": 1,
        "whatsapp": 1,
    },
    # Channel notices keyed per vendor channel and suite product.
    "services/channel_notices.py": {
        "continuation_where_slack": 1,
        "continuation_where_teams": 1,
        "discord_processing": 1,
        "google_chat_added": 1,
        "google_chat_not_configured": 1,
        "openaev": 4,
        "opencrq": 4,
        "opencti": 4,
        "teams_monitoring_stopped": 1,
    },
    # Suite products special-cased in MCP tool metadata.
    "services/mcp_tool_metadata.py": {
        "OpenAEV": 1,
        "OpenCRQ": 1,
        "OpenCTI": 1,
        "openaev": 1,
        "opencrq": 1,
        "opencti": 1,
    },
    # The prompt guard bound to Nova by name.
    "services/prompt_guards.py": {
        "nova": 1,
    },
    # A background task's result delivered to Slack / Teams by hand.
    "services/sub_agent.py": {
        "slack": 4,
        "slack_bot": 1,
        "teams": 4,
        "teams:service_url:*": 1,
    },
    # Read / write decided for named vendor tools.
    "services/tool_classification.py": {
        "github_graphql_query": 1,
        "run_metabase_query": 1,
        "run_metabase_question": 1,
    },
    # Suite products' route manifests applied by the answer pipeline.
    "services/url_guard.py": {
        "openaev": 1,
        "opencrq": 1,
        "opencti": 1,
        "teams": 1,
    },
}

#: ``(module, module-level name)`` pairs holding a phrase list, still open.
KNOWN_PHRASE_LISTS: dict[str, frozenset[str]] = {
    # Provider errors classified by their text instead of typed errors.
    "llm/router.py": frozenset(
        {
            "REQUEST_TOO_LARGE_KEYWORDS",
            "_PROVIDER_REFUSED_KEYWORDS",
            "_PROVIDER_UNAVAILABLE_KEYWORDS",
        }
    ),
    # Run errors classified by their text instead of ``failure_cause``.
    "services/assignment_engine.py": frozenset({"_CREDENTIAL_FAILURE_RE", "_TRANSIENT_LLM_KEYWORDS"}),
    # The user's "continue" read from a phrase list.
    "services/chat_framing.py": frozenset({"_CONTINUE_PHRASES"}),
    # The compaction answer read back by its headings.
    "services/conversation_memory.py": frozenset({"_DIRECTIVES_HEADING_RE", "_SUMMARY_HEADING_RE"}),
    # "No template" read from the request.
    "services/presentations.py": frozenset({"_NO_TEMPLATE_PHRASES"}),
    # The stall and resume-promise guards; the attachment claims the
    # platform's own tools write.
    "tools/agent_loop.py": frozenset(
        {
            "_ACTION_INTENT_RE",
            "_ATTACHMENT_CLAIM_RE",
            "_COMPLETION_AFTER_PROMISE_RE",
            "_DEFERRED_PROMISE_RE",
            "_HANDBACK_RE",
            "_NEGATION_BEFORE_PROMISE_RE",
            "_RESUME_PROMISE_RE",
            "_WARNING_PREDICATE_RE",
        }
    ),
    # Tool failures classified by their text instead of a typed error kind.
    "tools/base.py": frozenset(
        {
            "_AUTH_FAILURE_KEYWORDS",
            "_ERROR_PREFIXES",
            "_PERMISSION_VALIDATION_KEYWORDS",
            "_RATE_LIMIT_ERROR_PHRASES",
            "_RATE_LIMIT_KEYWORDS",
            "_RATE_LIMIT_PREFIXES",
        }
    ),
}

#: ``(module, module-level name)`` pairs holding a word list, still open.
KNOWN_WORD_LISTS: dict[str, frozenset[str]] = {
    # Providers and chat models recognised by model-name fragments.
    "llm/router.py": frozenset(
        {
            "_MODEL_PROVIDER_PREFIXES",
            "_NON_CHAT_MODEL_MARKERS",
        }
    ),
    # MCP tools classified by the verbs of their names.
    "services/mcp_tool_metadata.py": frozenset(
        {
            "_ADDITIVE_VERBS",
            "_DESTRUCTIVE_TOKENS",
            "_IDEMPOTENT_VERBS",
            "_TITLE_WORDS",
        }
    ),
    # Tools classified read / write by the verbs of their names.
    "services/tool_classification.py": frozenset(
        {
            "READ_TOKENS",
            "READ_VERBS",
            "WRITE_TOKENS",
        }
    ),
    # The waiting narration.
    "tools/agent_loop.py": frozenset({"_WAITING_NARRATION_RE"}),
}

#: Phrase-shaped lists that are not workarounds, with the reason. An identifier
#: pattern used as a security boundary matches identifiers, not language, and
#: its labels are the refusal's wording.
NOT_WORKAROUNDS: dict[str, frozenset[str]] = {
    # The tradecraft target guard: IP / URL / email / UUID / hostname patterns
    # a lesson may never carry across engagements, each with its label.
    "services/persistent_memory.py": frozenset({"_TRADECRAFT_FORBIDDEN_PATTERNS"}),
    # SVG attribute names (width / height / viewBox): a format's syntax.
    "services/builtin_tools/image_assets.py": frozenset({"_SVG_ATTR_RE"}),
    # The prompt-injection screen of user input: a security control matching
    # known attack phrasings, defense in depth behind the Nova engine, never a
    # decision about what the request asks for.
    "services/prompt_guard.py": frozenset({"_PATTERNS"}),
}


def _catalog_names() -> tuple[dict[str, str], frozenset[str]]:
    from app.api.v1.integration_catalog import PROVIDER_CATALOG

    exact: dict[str, str] = {}
    slugs: set[str] = set()
    for slug, entry in PROVIDER_CATALOG.items():
        name = str(entry.get("name") or "").strip().lower()
        if name:
            exact[name] = slug
        if slug in COMMON_WORD_PROVIDERS:
            continue
        exact[slug.lower()] = slug
        if len(slug) >= 4:
            slugs.add(slug.lower())
    return exact, frozenset(slugs | VENDOR_CHANNEL_MODULES)


def _docstrings(module: ast.Module) -> set[int]:
    found: set[int] = set()
    for node in app_sources.walk(module):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                found.add(id(body[0].value))
    return found


_TOKEN_SPLIT = re.compile(r"[_\-.:/ ]+")


def _vendor_literals(relative: str) -> Counter[str]:
    found = _vendor_literals_in(app_sources.tree(APP / relative))
    for literal in PLATFORM_IDENTITY_LITERALS.get(relative, frozenset()):
        found.pop(literal, None)
    return found


def _vendor_imports(relative: str) -> set[str]:
    return _vendor_imports_in(app_sources.tree(APP / relative), package=_package_of(relative))


def _phrase_lists(relative: str) -> set[str]:
    return _phrase_lists_in(app_sources.tree(APP / relative)) - NOT_WORKAROUNDS.get(relative, frozenset())


def _word_lists(relative: str) -> set[str]:
    found = _word_lists_in(app_sources.tree(APP / relative)) - _phrase_lists(relative)
    return found - NOT_WORKAROUNDS.get(relative, frozenset())


def _package_of(relative: str) -> str:
    return ".".join(["app", *relative.split("/")[:-1]])


def _vendor_in_prose() -> re.Pattern[str]:
    exact, tokens = _catalog_names()
    names = sorted({name for name in exact if len(name) >= 4} | tokens, key=len, reverse=True)
    return re.compile(r"(?<![\w.-])(" + "|".join(map(re.escape, names)) + r")(?![\w-])", re.IGNORECASE)


def _vendor_literals_in(module: ast.Module) -> Counter[str]:
    """Literals naming a vendor (for prose, the vendor names it holds), counted by occurrence."""
    docstrings = _docstrings(module)
    exact, tokens = _catalog_names()
    prose = _vendor_in_prose()
    found: Counter[str] = Counter()
    for node in app_sources.walk(module):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str) or id(node) in docstrings:
            continue
        value = node.value.strip().lower()
        names_a_vendor = value in exact or value in VENDOR_CHANNEL_MODULES
        # Between separators, so a compound slug (``elastic_security``) is found
        # in ``download_elastic_security_report`` too.
        normalized = f"_{_TOKEN_SPLIT.sub('_', value)}_"
        built_on_a_vendor = (
            len(value) <= 80 and " " not in value and any(f"_{token}_" in normalized for token in tokens)
        )
        if names_a_vendor or built_on_a_vendor:
            found[node.value] += 1
        elif " " in value:
            found.update(match.group(1).lower() for match in prose.finditer(node.value))
    return found


def _vendor_imports_in(module: ast.Module, *, package: str = "app") -> set[str]:
    found: set[str] = set()
    for node in app_sources.walk(module):
        names: list[str] = []
        if isinstance(node, ast.ImportFrom):
            base = package.split(".")
            if node.level:
                base = base[: len(base) - (node.level - 1)]
            imported = ".".join([*base, node.module] if node.module else base) if node.level else node.module or ""
            names = [imported] + [f"{imported}.{alias.name}" for alias in node.names]
        elif isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        for name in names:
            parts = name.split(".")
            if parts[:3] == ["app", "integrations", "providers"]:
                found.add(".".join(parts[:4]))
            elif parts[:2] == ["app", "channels"] and len(parts) > 2 and parts[2] in VENDOR_CHANNEL_ADAPTERS:
                found.add(".".join(parts[:3]))
    return found


#: Letters of any script (accented Latin, CJK, Cyrillic, ...): the guard reads every language.
_LETTERS = r"[^\W\d_]+"
#: A phrase: two or more plain words (letters, apostrophes), as a person writes them.
_PHRASE = re.compile(rf"^{_LETTERS}(?:['\u2019]{_LETTERS})?(?: {_LETTERS}(?:['\u2019]{_LETTERS})?)+$")


def _is_phrase(text: str) -> bool:
    """Lower case (a proper name such as a font family is not a phrase), at most 40 characters."""
    text = text.strip()
    return text == text.lower() and len(text) <= 40 and bool(_PHRASE.match(text))


_REGEX_SYNTAX = re.compile(r"\\[bBsSwWdD]\+?\*?|\(\?[:!=<]*|[()\[\]^$?+*{}]|\\")


def _phrases_in_pattern(pattern: str) -> list[str]:
    phrases = []
    for alternative in pattern.lower().split("|"):
        cleaned = " ".join(_REGEX_SYNTAX.sub(" ", alternative).split())
        if _is_phrase(cleaned):
            phrases.append(cleaned)
    return phrases


def _phrase_lists_in(module: ast.Module) -> set[str]:
    docstrings = _docstrings(module)
    found: set[str] = set()
    for statement in module.body:
        if isinstance(statement, ast.Assign) and len(statement.targets) == 1:
            target, value = statement.targets[0], statement.value
        elif isinstance(statement, ast.AnnAssign) and statement.value is not None:
            target, value = statement.target, statement.value
        else:
            continue
        if not isinstance(target, ast.Name):
            continue
        is_collection = isinstance(value, (ast.Tuple, ast.List, ast.Set)) or (
            isinstance(value, ast.Call) and ast.unparse(value.func) in {"frozenset", "set", "tuple"}
        )
        compiled, plain = _compiled_patterns(value, docstrings)
        if not (compiled or is_collection):
            continue
        phrases = [phrase for pattern, _ in compiled for phrase in _phrases_in_pattern(pattern)]
        if is_collection:
            phrases += [s for s in plain if _is_phrase(s)]
        if len(phrases) >= 2:
            found.add(target.id)
    return found


def _compiled_patterns(value: ast.AST, docstrings: set[int]) -> tuple[list[tuple[str, bool]], list[str]]:
    """Every ``re.compile`` in *value*, nested ones included, as (lower-cased pattern, ignores case),
    and the strings outside them."""
    compiled: list[tuple[str, bool]] = []
    inside: set[int] = set()
    for node in ast.walk(value):
        if isinstance(node, ast.Call) and ast.unparse(node.func).endswith("compile"):
            strings = [
                sub
                for sub in ast.walk(node)
                if isinstance(sub, ast.Constant) and isinstance(sub.value, str) and id(sub) not in docstrings
            ]
            inside.update(id(sub) for sub in strings)
            pattern = "".join(sub.value for sub in strings).lower()
            flags = ast.unparse(node)
            ignores = "IGNORECASE" in flags or re.search(r"\bre\.I\b", flags) is not None or "(?i)" in pattern
            compiled.append((pattern, ignores))
    plain = [
        node.value
        for node in ast.walk(value)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
        and id(node) not in inside
    ]
    return compiled, plain


#: A module-level name that announces a list of words.
_WORD_LIST_NAME = re.compile(r"(WORDS|KEYWORDS|PHRASES|STEMS|HINTS|MARKERS|TOKENS|VERBS|TERMS|PREFIXES|SENTINELS)$")
_WORD = re.compile(rf"^{_LETTERS}(?:['\u2019 -]{_LETTERS})*$")
#: An alternation of three or more plain words: ``(?:resolved|closed|done)``.
_WORD_ALTERNATION = re.compile(r"\((?:\?:)?(?:[^\W\d_]{3,}\|){2,}[^\W\d_]{3,}\)")


def _word_lists_in(module: ast.Module) -> set[str]:
    """Module-level word lists (by name) and case-insensitive word or phrase regexes."""
    docstrings = _docstrings(module)
    found: set[str] = set()
    for statement in module.body:
        if isinstance(statement, ast.Assign) and len(statement.targets) == 1:
            target, value = statement.targets[0], statement.value
        elif isinstance(statement, ast.AnnAssign) and statement.value is not None:
            target, value = statement.target, statement.value
        else:
            continue
        if not isinstance(target, ast.Name):
            continue
        compiled, plain = _compiled_patterns(value, docstrings)
        if any(
            ignores and (_WORD_ALTERNATION.search(pattern) or _phrases_in_pattern(pattern))
            for pattern, ignores in compiled
        ) or (
            not compiled
            and _WORD_LIST_NAME.search(target.id)
            and sum(1 for s in plain if s == s.lower() and _WORD.match(s.strip())) >= 3
        ):
            found.add(target.id)
    return found


@pytest.mark.parametrize("relative", GENERIC_MODULES)
def test_a_generic_module_imports_no_integration_provider_or_vendor_channel(relative: str) -> None:
    new = _vendor_imports(relative) - KNOWN_VENDOR_IMPORTS.get(relative, frozenset())
    assert not new, (
        f"app/{relative} imports {sorted(new)}: a generic layer goes through the integration's or the "
        "channel's generic interface (the catalog, ``channels.router``), never through one vendor's "
        "module (invariant 18)."
    )


@pytest.mark.parametrize("relative", GENERIC_MODULES)
def test_a_generic_module_names_no_vendor(relative: str) -> None:
    new = sorted((_vendor_literals(relative) - Counter(KNOWN_VENDOR_LITERALS.get(relative, {}))).elements())
    assert not new, (
        f"app/{relative} names {sorted(new)}: vendor logic in a generic layer (invariant 18). Declare "
        "what the integration needs on the integration (catalog entry, provider adapter, ToolDefinition) "
        "and read it generically; never add the literal to KNOWN_VENDOR_LITERALS."
    )


@pytest.mark.parametrize("relative", GENERIC_MODULES)
def test_a_generic_module_holds_no_phrase_list(relative: str) -> None:
    new = _phrase_lists(relative) - KNOWN_PHRASE_LISTS.get(relative, frozenset())
    assert not new, (
        f"app/{relative} defines {sorted(new)}: a list of natural-language phrases deciding behaviour "
        "(invariant 18). Ask the model for a structured answer, use a fact the platform holds, or let "
        "the model's decision stand; never add the name to KNOWN_PHRASE_LISTS."
    )


@pytest.mark.parametrize("relative", GENERIC_MODULES)
def test_a_generic_module_holds_no_word_list(relative: str) -> None:
    new = _word_lists(relative) - KNOWN_WORD_LISTS.get(relative, frozenset())
    assert not new, (
        f"app/{relative} defines {sorted(new)}: a list of words (or a case-insensitive word regex) "
        "deciding behaviour from what a model, a person or a tool's name says (invariant 18). Read a "
        "declared property, a structured answer or a fact the platform holds; never add the name to "
        "KNOWN_WORD_LISTS."
    )


def test_the_checks_catch_what_they_describe() -> None:
    caught = ast.parse(
        "from app.integrations.providers.github import tools\n"
        "from app.channels.slack import SlackChannel\n"
        "from app.channels.router import handle_message\n"
        "COSMETIC = {'download_notion_file': 'filename'}\n"
        "LABEL = 'GitHub'\n"
        "HANDBACK = re.compile(r'(would you like|do you want|\\bshould i\\b)', re.IGNORECASE)\n"
        "CONTINUE = ('continue', 'please continue', 'go on')\n"
        "STATUSES = ('pending', 'running', 'completed')\n"
        "FONTS = ('IBM Plex Sans', 'Geologica Light')\n"
        "SUGGESTION = 'Review open PRs on GitHub'\n"
        "def f():\n"
        "    '''Talks about Notion and would you like, in prose.'''\n"
    )
    assert _vendor_imports_in(caught) == {"app.integrations.providers.github", "app.channels.slack"}
    assert _vendor_literals_in(caught) == Counter({"download_notion_file": 1, "GitHub": 1, "github": 1})
    assert _phrase_lists_in(caught) == {"HANDBACK", "CONTINUE"}

    relative = ast.parse("from ..integrations.providers.github import tools\nfrom . import helpers\n")
    assert _vendor_imports_in(relative, package="app.services") == {"app.integrations.providers.github"}

    words = ast.parse(
        "TITLE_STATUS_WORDS = ('done', 'closed', 'resolved')\n"
        "TERMINAL_RE = re.compile(r'\\b(?:resolved|closed|done)\\b', re.IGNORECASE)\n"
        "WAITING_RE = re.compile(r'\\bwaiting for\\b', re.IGNORECASE)\n"
        "STATUSES = ('pending', 'running', 'completed')\n"
        "UUID_RE = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}', re.IGNORECASE)\n"
        "SHORT_FLAG_RE = re.compile(r'\\b(?:resolved|closed|done)\\b', re.I)\n"
    )
    assert _word_lists_in(words) == {"TITLE_STATUS_WORDS", "TERMINAL_RE", "WAITING_RE", "SHORT_FLAG_RE"}
    compound = ast.parse("TOOL = 'download_elastic_security_report'\n")
    assert _vendor_literals_in(compound) == Counter({"download_elastic_security_report": 1})
    again = ast.parse("A = 'opencti'\nB = 'opencti'\n")
    assert _vendor_literals_in(again) == Counter({"opencti": 2})
    adapter = ast.parse("from app.channels.teams_graph_worker import run\n")
    assert _vendor_imports_in(adapter) == {"app.channels.teams_graph_worker"}

    every_language = ast.parse(
        "EXPORT_REQUESTS = ('t\u00e9l\u00e9charger maintenant', 'exporter le fichier')\n"
        "CONTINUE_PHRASES = ('\u7d9a\u3051\u3066', '\u7d9a\u884c', '\u9032\u3081\u3066')\n"
    )
    assert _phrase_lists_in(every_language) == {"EXPORT_REQUESTS"}
    assert _word_lists_in(every_language) == {"CONTINUE_PHRASES"}

    nested = ast.parse(
        "PATTERNS = (\n"
        "    re.compile(r'ignore (all )?previous instructions', re.IGNORECASE),\n"
        "    re.compile(r'you are now', re.IGNORECASE),\n"
        ")\n"
    )
    assert _phrase_lists_in(nested) == {"PATTERNS"}
    assert _word_lists_in(nested) == {"PATTERNS"}


def test_the_open_exceptions_still_exist() -> None:
    """The exception lists only shrink: an entry fixed is an entry removed."""
    stale: list[str] = []
    for relative, imports in KNOWN_VENDOR_IMPORTS.items():
        assert relative in GENERIC_MODULES, relative
        stale += [f"{relative}: import {name}" for name in sorted(imports - _vendor_imports(relative))]
    for relative, literals in KNOWN_VENDOR_LITERALS.items():
        assert relative in GENERIC_MODULES, relative
        found = _vendor_literals(relative)
        stale += [
            f"{relative}: {literal!r} x{count} (now {found[literal]})"
            for literal, count in sorted(literals.items())
            if found[literal] < count
        ]
    for relative, names in KNOWN_PHRASE_LISTS.items():
        assert relative in GENERIC_MODULES, relative
        stale += [f"{relative}: {name}" for name in sorted(names - _phrase_lists(relative))]
    for relative, names in KNOWN_WORD_LISTS.items():
        assert relative in GENERIC_MODULES, relative
        stale += [f"{relative}: {name}" for name in sorted(names - _word_lists(relative))]
    assert not stale, f"Fixed (remove from the exception lists): {stale}"


def test_every_scope_pattern_still_names_a_module() -> None:
    """A module renamed or moved out of a pattern would silently leave the guard."""
    empty = [pattern for pattern in GENERIC_LAYER_PATTERNS if not any(APP.glob(pattern))]
    assert not empty, empty
