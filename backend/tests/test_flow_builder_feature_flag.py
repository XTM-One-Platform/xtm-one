"""The visual flow builder's surface stays behind ``FLOW_BUILDER``.

The flag exists because the builder is not announced externally yet, so what
matters is that nothing *offers* it. Most of that lives in the frontend, which
has no unit-test harness — so it is pinned here, next to the registry the flag
is declared in.

Two boundaries are easy to get wrong, and both are asserted below.

**Not everything under ``/flows`` is the builder.** That page's
Agent-to-agent map (with its Schedule view) and Fleets tabs read agents,
fleets, assignments and delegations, no flow endpoint between them, so the
page, its nav entry, those two tabs and their documentation stay live. Only
the ``Flows`` library tab and the ``/flows/library/<id>`` canvas are hidden.

**The flag is UI-only.** The ``/api/v1/flows`` router stays *mounted* because
the agent-creation wizard reads the node catalog, and the engine keeps
executing existing flows. What it drops is everything that announces the
feature: the library tab, the builder route, the ⌘K commands, the published
OpenAPI operations and the two builder documentation pages. Asserting both
halves is what tells the next reader that a still-reachable endpoint is a
decision, not an oversight.

**Known coverage gap.** Only the *backend* flag-off behaviour is executable
here — ``test_the_routes_stay_mounted_but_leave_the_published_schema`` imports
the real app and reads its real schema. Everything about the frontend is a
source assertion, because the frontend has no unit-test harness and the
Playwright suite runs with the flag ON (it drives the builder end to end; see
``.github/workflows/e2e.yml``). So no executable test exercises the flag-off
UI. Closing that means a second compose service and a Playwright project with
its own ``baseURL`` — worth it if this flag outlives the feature, and worth
knowing about rather than mistaking these assertions for behavioural coverage.

The source assertions at least try not to be brittle: ``_has`` compares with
whitespace collapsed and ``_ts_function`` delimits by dedent, so a prettier
reflow does not fail a test about behaviour. Both are documented below.

See ``dev-docs/flow-builder-feature-flag.md``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core.feature_flags import KNOWN_FEATURE_FLAGS, is_feature_enabled

BACKEND = Path(__file__).resolve().parent.parent
FRONTEND = BACKEND.parent / "frontend"
DOCS = BACKEND.parent / "docs"

FLAG = "FLOW_BUILDER"

#: Documentation for the builder itself — unpublished with the feature.
UNPUBLISHED_PAGES = ("usage/flows.md", "usage/flows-api.md")

#: The agent-to-agent map and the fleets. Not the builder's, stays published.
PUBLISHED_FLOW_PAGE = "usage/agentic-flow.md"

#: Prefixes the builder's operations live under, matched exactly rather than by
#: searching paths for "flow": an unrelated ``/api/v1/workflows`` would make a
#: substring test fail for no reason. The cost is that a *new* flow router under a
#: fourth prefix would slip past — add it here when one appears.
FLOW_PATH_PREFIXES = (
    "/api/v1/flows",
    "/api/v1/flow-event-types",
    "/api/hooks/flows",
)


def _read(*parts: str) -> str:
    return FRONTEND.joinpath(*parts).read_text(encoding="utf-8")


def _squash(text: str) -> str:
    """Collapse all whitespace, so a pin describes code rather than formatting.

    Every assertion below that quotes source does it through this: a prettier
    reflow that only moves a line break must not fail a test about behaviour.
    """
    return " ".join(text.split())


def _has(source: str, snippet: str) -> bool:
    return _squash(snippet) in _squash(source)


def _ts_function(source: str, name: str) -> str:
    """A top-level ``function <name>``, up to its closing brace at column 0.

    Not a fixed character window: slicing ``[:1400]`` and then calling
    ``str.index`` inside it dies with a bare ``ValueError: substring not
    found`` the moment a comment pushes the interesting line past the end — a
    formatting change reported as a mystery instead of an assertion failure.

    Brace *matching* was the first attempt and was worse: the first ``{`` after
    the name belongs to a destructured parameter (``({ children })``) or a TS
    return-type annotation (``: { enabled: boolean }``), both of which close
    immediately and yield a one-line "body". Column-0 dedent is what actually
    delimits these — every one of them is a top-level, prettier-formatted
    function, so nothing nested can close at column 0 before it does.
    """
    start = source.index(f"function {name}")
    end = source.find("\n}", start)
    assert end != -1, f"`function {name}` has no closing brace at column 0"
    return source[start : end + 2]


def test_the_flag_is_in_both_registries():
    """The backend ``Literal``/frozenset and the frontend list are hand-synced;
    an id missing from either side is silently fail-closed, which looks exactly
    like the feature being broken."""
    assert FLAG in KNOWN_FEATURE_FLAGS
    assert FLAG in _read("src", "lib", "utils.ts")


def test_the_nav_entry_is_not_gated():
    """``/flows`` still leads to the map and the fleets, so hiding the entry
    would take two unrelated, already-announced surfaces with it."""
    nav = _read("src", "components", "layout", "nav-model.ts")
    entry = next(line for line in nav.splitlines() if 'leaf("/flows"' in line)
    assert "flowBuilder" not in entry
    assert "flowBuilder" not in nav, (
        "the nav no longer needs the flag — a leftover NavFlags field is dead code"
    )


def test_the_page_route_is_open_and_the_builder_route_is_gated():
    app_tsx = _read("src", "App.tsx")
    page = next(
        line for line in app_tsx.splitlines() if 'path="/flows/:tab?"' in line
    )
    builder = next(
        line
        for line in app_tsx.splitlines()
        if 'path="/flows/library/:id"' in line
    )
    assert "FeatureFlagGate" not in page, (
        "gating the page hides the map and the fleets too"
    )
    assert '<FeatureFlagGate flag="FLOW_BUILDER">' in builder, "the canvas is reachable with the flag off"


def test_the_gate_waits_for_the_config_before_redirecting():
    """A guard that redirects while ``/auth/config`` is still in flight bounces
    every deep link and hard refresh to the dashboard on an instance where the
    flag IS on — the failure mode is a page that "randomly" won't open."""
    gate = _ts_function(_read("src", "App.tsx"), "FeatureFlagGate")
    assert "isResolved" in gate
    assert gate.index("isResolved") < gate.index("Navigate")


def test_only_the_library_tab_is_gated_and_the_map_is_the_default():
    """The tabs share one page, so the gate is a tab filter. The map is the
    landing tab whatever the flag says, so a hidden ``library`` can
    never be the landing tab and render the page empty."""
    flows = _read("src", "pages", "Flows.tsx")
    assert _has(flows, 'key !== "library" || flowBuilderEnabled')
    assert _has(flows, 'const DEFAULT_TAB: FlowTab = "map";')
    assert "flowBuilderEnabled ?" not in flows, (
        "the flag no longer changes the landing tab"
    )
    assert "tabs.some" in flows, (
        "unknown/gated tab segments must normalize to the default, so "
        "/flows/library lands on the Map instead of rendering blank"
    )


def test_only_the_library_command_is_gated():
    """``resolveCommands`` drops a parent whose children all fail their ``when``,
    so a key on the parent would take the Map and Schedule commands with it."""
    manifest = _read("src", "pages", "Flows.commands.ts")
    ungated = ('id: "flows.map"', 'id: "flows.schedule"', 'id: "flows.open"')
    for line in manifest.splitlines():
        if 'id: "flows.library"' in line:
            assert '"flowBuilder"' in line
        elif any(marker in line for marker in ungated):
            assert "flowBuilder" not in line


def test_every_builder_canvas_command_is_gated():
    """The palette lists commands from screens that are not mounted, so without
    the key these would be offered where the canvas cannot be opened."""
    manifest = _read("src", "pages", "flow-builder", "FlowBuilder.commands.ts")
    handlers = [line for line in manifest.splitlines() if "handler:" in line]
    assert len(handlers) == manifest.count('when: ["flowBuilder"]') == len(handlers)


def test_the_context_key_is_known_to_the_palette_and_its_validator():
    """``CONTEXT_KEYS`` is duplicated in the validation script by design (it
    parses manifests as text). A key in one and not the other either fails the
    build or, worse, resolves as ``undefined`` and hides nothing."""
    assert '"flowBuilder"' in _read("src", "lib", "commands", "types.ts")
    assert "flowBuilder" in _read("scripts", "validate-commands.mjs")
    assert "flowBuilder," in _read("src", "lib", "commands", "CommandsProvider.tsx")


requires_flag_off = pytest.mark.skipif(
    is_feature_enabled(FLAG),
    reason=f"ENABLED_FEATURES has {FLAG} on; these assert the flag-off shape",
)


@requires_flag_off
def test_the_routes_stay_mounted_but_leave_the_published_schema():
    """Mounted: the agent-creation wizard calls ``GET /flows/node-catalog``, so
    unmounting the router would break a shipped feature — the flag is UI-only,
    not an authorization boundary.

    Undocumented: ``/api/openapi.json`` is external communication like the docs
    site. ``include_in_schema=False`` is what does it — dropping the "flows"
    entry from ``OPENAPI_TAGS`` only removes the group's description, leaving
    every path and schema in the document.
    """
    from app.main import application

    mounted = {getattr(route, "path", "") for route in application.routes}
    assert "/api/v1/flows" in mounted, "the flow routes must stay reachable"
    assert "/api/v1/flows/node-catalog" in mounted, (
        "the wizard's node catalog must stay reachable"
    )

    schema = application.openapi()
    leaked = sorted(
        path
        for path in schema["paths"]
        if path.startswith(FLOW_PATH_PREFIXES)
    )
    assert not leaked, f"flow endpoints still in the published schema: {leaked}"
    assert "flows" not in {tag["name"] for tag in schema.get("tags", [])}


def test_the_wizard_still_justifies_leaving_the_routes_mounted():
    """The reason the REST surface is ungated is one call site. If it goes, the
    trade-off above should be revisited rather than inherited."""
    assert "useFlowNodeCatalog" in _read("src", "pages", "NewAgentWizard.tsx")


def test_the_node_catalog_drops_the_flow_builder_tool_option():
    """The catalog that keeps the router mounted was also advertising the
    feature. ``GET /flows/node-catalog`` feeds the flow canvas *and* the
    agent-creation wizard's "Tools the agent can call" step, which renders
    ``sources.tools`` verbatim — so with the flag off the wizard offered
    "Flow builder (built-in)" as a selectable card. Filtered server-side so
    every consumer of the catalog agrees."""
    source = (BACKEND / "app" / "services" / "flow_access.py").read_text(
        encoding="utf-8"
    )
    assert _has(source, '("flow_builder", "Flow builder"'), (
        "the catalog tuple moved — check the filter below still matches it"
    )
    assert _has(
        source,
        'tool_id == "flow_builder" and not is_feature_enabled("FLOW_BUILDER")',
    )


def test_the_example_flow_is_not_seeded_while_the_builder_is_hidden():
    """An ACTIVE example flow on a hidden-builder instance manufactures runs
    nobody can answer: an ``Ask user`` node parks the run in
    ``awaiting_human``, and the canvas, the Executions feed and the HITL bell
    are all gated. Both seeding paths — boot and the runtime AI toggle — are
    behind the flag. Parsed rather than grepped: the comments legitimately name
    the function while explaining the gate."""
    import ast

    for module, symbol in (
        ("app/main.py", "seed_example_flow"),
        ("app/api/v1/settings.py", "seed_example_flow"),
    ):
        tree = ast.parse((BACKEND / module).read_text(encoding="utf-8"))
        gates = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.If) and FLAG in ast.unparse(node.test)
        ]
        assert gates, f"{module} has no FLOW_BUILDER gate"
        called = {
            node.func.attr if isinstance(node.func, ast.Attribute) else node.func.id
            for gate in gates
            for node in ast.walk(gate)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name | ast.Attribute)
        }
        assert symbol in called, f"{module} seeds the example flow outside the gate"


def test_the_builtin_tools_catalog_drops_the_flow_builder_key():
    """``GET /settings/builtin-tools/catalog`` published the key unconditionally
    — thirteen flow-authoring sub-tools by name and description. No UI renders
    it, so nothing on screen changed, but it is the endpoint an integrator
    reads, which is exactly what this flag is about."""
    import inspect

    from app.api.v1.settings import _offered_builtin_tool_catalog, get_builtin_tool_catalog
    from app.services.builtin_tool_catalog import build_builtin_tool_catalog

    # The endpoint serves the offered catalog, which the groups endpoint shares:
    # the gate lives in that one helper.
    assert "_offered_builtin_tool_catalog()" in inspect.getsource(get_builtin_tool_catalog)
    endpoint = inspect.getsource(_offered_builtin_tool_catalog)
    # The flow builder is a sub-area of the platform group: the
    # helper drops its functions from the group (and the former key).
    from app.services.builtin_tool_catalog import withhold_flow_builder_tools

    assert "withhold_flow_builder_tools(" in endpoint
    helper = inspect.getsource(withhold_flow_builder_tools)
    assert 'is_feature_enabled("FLOW_BUILDER")' in helper
    assert 'catalog.pop("flow_builder"' in helper

    # And NOT in the shared builder: ``agent_skills._build_builtin_id_set()``
    # reads its keys as "which built-ins exist on this platform", so filtering
    # there made importing a skill package that references the tool fail with
    # 400 on a flag-off instance — the flag is UI-only and must never break an
    # agent that already carries the id.
    # The registry is the member catalog: the group catalog
    # merges its former ids and passes every other key through.
    from app.services.builtin_tool_catalog import build_builtin_tool_member_catalog

    registry = inspect.getsource(build_builtin_tool_member_catalog)
    assert "build_builtin_tool_member_catalog(" in inspect.getsource(build_builtin_tool_catalog)
    assert "is_feature_enabled(" not in inspect.getsource(build_builtin_tool_catalog)
    # The call, not the word: the comment in there legitimately names the flag
    # to explain why the gate is *not* applied, and a substring test finds that
    # mention first (the same trap `test_ai_disabled_boot` documents).
    assert "is_feature_enabled(" not in registry, (
        "the built-in id registry must stay complete; filter at the endpoint"
    )
    assert 'catalog["flow_builder"]' in registry


def test_an_unfetchable_config_is_not_the_same_answer_as_flag_off():
    """``useAuthConfig`` no longer throws, so a failed fetch has to be reported
    rather than silently read as "off". Folding ``isError`` into ``isResolved``
    made the gate (``FeatureFlagGate``) redirect on an outage — a real failure rendered as
    a plausible bounce to the dashboard, indistinguishable from the feature not
    existing."""
    hook = _ts_function(
        _read("src", "hooks", "useFeatureFlag.ts"), "useFeatureFlagResolved"
    )
    # Both derived from having a config, never from the query's status: v5's
    # `isError` covers refetch failures, so reading it raw tore down whatever
    # the flag gated whenever a cached-and-fine config failed to revalidate.
    assert _has(hook, "const hasConfig = data !== undefined;")
    assert _has(hook, "isResolved: hasConfig"), (
        "an errored config must not count as a resolved flag, and a cached one "
        "must not stop counting as resolved when a refetch fails"
    )
    assert _has(hook, "isError: isError && !hasConfig"), (
        "a background refetch failure must not read as an unfetchable config"
    )
    assert "isPending" not in hook, (
        "`isPending` is redundant once both flags derive from `hasConfig` — "
        "and it reintroduces the refetch-blip bug if it comes back"
    )
    gate = _ts_function(_read("src", "App.tsx"), "FeatureFlagGate")
    assert gate.index("isError") < gate.index("Navigate"), (
        "the gate must surface an unfetchable config before it redirects"
    )


def test_login_reports_a_config_it_could_not_load():
    """Silencing the query without this is worse than throwing: on an SSO-only
    deployment an undefined config falls back to "local auth, no SSO", i.e. an
    email/password form that can never succeed and says nothing."""
    login = _read("src", "pages", "Login.tsx")
    assert _has(login, "isError: authConfigFetchError")
    assert _has(
        login, "const authConfigError = authConfigFetchError && !authConfig;"
    ), (
        "the banner must not post over a working SSO list when a cached config "
        "merely failed to revalidate"
    )
    assert _has(login, "authConfigError &&")


def test_the_executions_page_does_not_fetch_on_a_guess():
    """Same first-render trap as `Flows.tsx`: the flag decides which feed to
    request, so reading it fail-closed fired and discarded a request, then
    flipped the whole view when the config landed. Gating only the skeleton
    would not help — hooks run before the early return."""
    page = _read("src", "pages", "TaskHistory.tsx")
    assert "useFeatureFlagResolved" in page
    # The feeds also wait while a `?fleet=` waits on the reader's fleet access.
    assert _has(page, "const flagKnown = (flagResolved || flagError) && !fleetPending;")
    assert _has(page, "const assignmentsEnabled = flagKnown &&")
    assert _has(page, "const flowsEnabled = flagKnown")


@pytest.mark.parametrize("flows_enabled", [True, False])
async def test_the_bell_is_left_exactly_as_it_was(monkeypatch, flows_enabled):
    """The flag drops flow rows from the bell and changes nothing else.

    Two compensations for the resulting under-report were tried and both cost
    more than the symptom: a conditional limit (a flag-ON instance fetched, then
    re-fetched and discarded, on every cold load), and capping the merged list
    to pay for an unconditional over-fetch, which shortened the bell on every
    instance — assignment runs and activity rows merge here, so that list was
    never capped at 20 to begin with. The bell reads the server's feed
    (``services/notification_feed.py``): pinned there, so a third attempt has to
    argue with this comment first.
    """
    from types import SimpleNamespace

    from app.api.v1 import activity, assignments
    from app.core import feature_flags
    from app.services import notification_feed as feed

    asked: dict[str, int] = {}

    async def _runs(**kwargs):
        asked["runs"] = kwargs["limit"]
        return [SimpleNamespace(n=n) for n in range(20)]

    async def _activity(**kwargs):
        asked["activity"] = kwargs["limit"]
        return [SimpleNamespace(n=n) for n in range(20)]

    def _item(prefix):
        return lambda row, **_: feed.NotificationItem(
            id=f"{prefix}{row.n}", kind="run", created_at=f"2026-10-20T00:00:{row.n:02d}", route="/", title="t"
        )

    async def _read_state(_db, _user_id):
        return None, []

    monkeypatch.setattr(assignments, "list_all_runs", _runs)
    monkeypatch.setattr(activity, "list_activity", _activity)
    monkeypatch.setattr(feature_flags, "is_feature_enabled", lambda _flag: flows_enabled)
    monkeypatch.setattr(feed, "present_run", _item("run-"))
    monkeypatch.setattr(feed, "present_activity", _item("activity-"))
    monkeypatch.setattr(feed._ActivityView, "of", staticmethod(lambda row: row))
    monkeypatch.setattr(feed, "load_read_state", _read_state)

    built = await feed.build_feed(None, SimpleNamespace(id="u"))

    assert asked == {"runs": 20, "activity": 20}, "the window must stay what it was before the flag, and unconditional"
    assert len(built["items"]) == 40, "the merged notification list is not capped"


def test_every_new_user_facing_string_is_translated():
    """A flag-off instance showed the `/flows` description in English between
    translated neighbours. Locale files are hand-maintained, so the strings
    this PR introduces are asserted present in all four."""
    import json

    # The `/flows` description that motivated this test is gone: the page
    # carries no subtitle any more (amendment 3), so its key left the
    # catalogs with it.
    added = (
        "The available sign-in options could not be loaded, so what is shown "
        "below may be incomplete.",
    )
    for lang in ("en", "fr", "es", "ja"):
        catalog = json.loads(
            (FRONTEND / "src" / "i18n" / "lang" / f"{lang}.json").read_text(
                encoding="utf-8"
            )
        )
        for string in added:
            assert string in catalog, f"{lang}.json is missing {string[:40]!r}"
        if lang != "en":
            for string in added:
                assert catalog[string] != string, (
                    f"{lang}.json carries {string[:40]!r} untranslated"
                )


def test_the_analytics_source_picker_drops_the_flow_option():
    """A user-visible control naming the gated feature, missed by the first
    sweep. `ALLOWED_SOURCES` deliberately keeps `flow` — it validates the
    `?source=` URL param, and a stale link to flow traces should keep working;
    what the flag removes is the offer, not access to data that exists."""
    page = _read("src", "pages", "Analytics.tsx")
    assert _has(page, 'source !== "flow" || flowBuilderEnabled || selected.sources.includes(source)')
    assert _has(
        page, "const ALLOWED_SOURCES = new Set<string>(Object.keys(SOURCE_LABELS));"
    ), "URL validation must still accept every source"
    assert _has(_read("src", "pages", "analytics", "constants.ts"), 'flow: "Flows",')


def test_reading_the_flag_cannot_take_the_whole_app_down():
    """``useFeatureFlag`` reads ``useAuthConfig``, and `Header` /
    `CommandsProvider` call it — so that query now runs inside `AppLayout`,
    under the *fullPage* ErrorBoundary. With the global ``throwOnError: true``
    it inherits from `main.tsx`, one transient 5xx on ``/auth/config`` would
    replace the entire app with the error screen. It also keeps the ``isError``
    branch of ``useFeatureFlagResolved`` reachable, so a route guard cannot
    hang on a skeleton when the config never arrives."""
    config = _ts_function(
        _read("src", "hooks", "api", "useAuthApi.ts"), "useAuthConfig"
    )
    assert "throwOnError: false" in config
    # No explicit `retry`: the app default (main.tsx) is deliberately kept, so
    # two render-blocking gates do not sit through a 1s+2s+4s backoff.
    assert "retry:" not in config


def test_the_flows_page_waits_for_the_config_before_choosing_a_tab():
    """The landing tab is derived from a fail-closed flag. Without the wait, a
    flag-ON instance mounts `MapTab` on every cold load, fires its live-map
    queries, then swaps in `LibraryTab` once ``/auth/config`` lands — wasted
    fetches, a remount and a tab bar that grows a tab under the cursor."""
    flows = _read("src", "pages", "Flows.tsx")
    assert "useFeatureFlagResolved" in flows
    # `&& !isError`: this page's other two tabs work without the flag, so a
    # failed config carries on with the library hidden rather than blocking.
    assert _has(flows, "if (!isResolved && !isError) return <PageSkeleton />;")


def test_the_builder_docs_are_unpublished_and_the_map_docs_are_not():
    """Dropping a page from ``nav`` is not enough — mkdocs still builds and
    indexes a file that is merely absent from it, so it stays reachable by URL
    and findable in search. ``exclude_docs`` is what unpublishes them."""
    for config in ("mkdocs.yml", "mkdocs_serve.yml"):
        text = (DOCS / config).read_text(encoding="utf-8")
        nav = text[text.index("\nnav:") :]
        for page in UNPUBLISHED_PAGES:
            assert page not in nav, f"{page} is back in {config}'s nav"
            assert page in text, f"{page} is not excluded in {config}"
        assert PUBLISHED_FLOW_PAGE in nav, (
            f"{PUBLISHED_FLOW_PAGE} documents the map and the fleets, which stay live"
        )


def test_the_flag_doc_describes_the_three_tab_page():
    """The page has three tabs (map, fleets, flows) and the
    schedule is a view of the map. The dev-doc that says what the flag hides
    must not describe the page it replaced: a Live Map tab and a Schedule tab
    no longer exist, and the flag no longer picks the landing tab."""
    doc = (BACKEND.parent / "dev-docs" / "flow-builder-feature-flag.md").read_text(
        encoding="utf-8"
    )
    assert "Live Map" not in doc
    assert "`Schedule` tabs" not in doc
    assert "landing tab |" not in doc, "the flag no longer changes the landing tab"
    for tab in ("`Agent-to-agent map`", "`Fleets`", "`Flows`"):
        assert tab in doc, f"{tab} tab not described"
    assert "/flows/map/schedule" in doc, "the Schedule view is a path route (#10203)"


#: Vocabulary that only the visual builder has. Deliberately narrow — the
#: published ``agentic-flow.md`` legitimately says "flow" and "Agentic Flow"
#: all over, so a bare "flow" would match it. These four are the builder's own
#: nouns: its name, its node palette, one of its nodes, and its drawing
#: surface.
BUILDER_VOCABULARY = ("flow builder", "node library", "budget node", "canvas")


def test_no_published_page_describes_the_builder():
    """Links are not the only way a page can carry the feature.

    ``agent-budgets.md`` stayed in ``nav`` with a whole section on the ``Budget``
    node — how to drag it out of the node library, which socket to wire it to —
    while the pages it linked to were unpublished. The link test below passed
    the entire time, because prose is not a link. The flow-specific sections
    were removed and the general rules they carried (precedence, and the
    typed-versus-copied authority rule) re-homed on the same page.
    """
    docs = (DOCS / "docs").resolve()
    unpublished = {Path(page).name for page in UNPUBLISHED_PAGES}
    offenders = [
        f"{page.relative_to(docs)}: {term!r}"
        for page in docs.rglob("*.md")
        if page.name not in unpublished
        for term in BUILDER_VOCABULARY
        if term in page.read_text(encoding="utf-8").lower()
    ]
    assert not offenders, f"published pages describing the builder: {offenders}"


def test_no_published_page_links_to_an_unpublished_one():
    """A link to an excluded page is a 404 for the reader and a warning that
    fails the strict build."""
    docs = (DOCS / "docs").resolve()
    unpublished = {Path(page).name for page in UNPUBLISHED_PAGES}
    offenders = [
        f"{page.relative_to(docs)} -> {target}"
        for page in docs.rglob("*.md")
        if page.name not in unpublished
        for target in unpublished
        if f"]({target}" in (body := page.read_text(encoding="utf-8"))
        or f"](../{target}" in body
    ]
    assert not offenders, f"links into unpublished pages: {offenders}"
