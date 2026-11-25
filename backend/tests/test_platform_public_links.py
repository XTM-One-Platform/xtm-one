"""Links shown to people are on the platform's registered URL.

In the standard Docker deployments XTM One reaches OpenCTI on
``http://opencti:8080`` (``OPENCTI_API_URL``, recorded on the registration as
``api_url``) while its users browse ``http://localhost:8080``, the URL it
registered with. The internal URL is only ever called server to server: every
link a person is handed - the platform tools' "Open in" links, the URL the
agent is told to build links from, the answer's links, ``trigger.platform_url``
- is on the registered URL, and the scope matcher takes either URL as the same
platform. With no internal URL recorded nothing changes.
"""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

import pytest

from app.core.config import settings
from app.core.platform_identity import (
    RegisteredPlatform,
    platforms_reached_internally,
    show_on_public_urls,
)
from app.core.security import encrypt_value
from app.models.integration import Integration
from app.services.integration_tools import BindingBasis, ToolBindings
from app.tools.base import CallableToolExecutor, ToolCall, ToolDefinition, ToolResult

PUBLIC = "http://localhost:8080"
INTERNAL = "http://opencti:8080"
ENTITY = "3f1a2b4c-5d6e-4f70-8a9b-0c1d2e3f4a5b"

REACHED = RegisteredPlatform(platform_identifier="opencti", platform_url=PUBLIC, api_url=INTERNAL)
PUBLIC_ONLY = RegisteredPlatform(platform_identifier="opencti", platform_url=PUBLIC)


def _integration(base_url: str, *, auth_type: str = "platform_managed", provider: str = "opencti") -> Integration:
    return Integration(
        id=uuid.uuid4(),
        provider=provider,
        name="OpenCTI",
        auth_type=auth_type,
        extra_config_enc=encrypt_value(json.dumps({"base_url": base_url})),
        is_company_managed=True,
        status="connected",
        scope_config={"match_key": "base_url", "resolve_from_header": "X-Platform-URL"},
    )


def _registration(integration_id: uuid.UUID, api_url: str | None = INTERNAL, platform_url: str = PUBLIC):
    return SimpleNamespace(
        integration_id=integration_id,
        platform_identifier="opencti",
        platform_url=platform_url,
        api_url=api_url,
    )


class _Result:
    def __init__(self, rows: list) -> None:
        self._rows = rows

    def all(self) -> list:
        return list(self._rows)

    def scalars(self) -> _Result:
        return self

    def scalar_one_or_none(self) -> object:
        return self._rows[0] if self._rows else None


class _Db:
    """Answers ``platform_registrations`` queries with *registrations* and any
    other query with *integrations*; counts the registration reads."""

    def __init__(self, registrations: list | None = None, integrations: list | None = None) -> None:
        self.registrations = registrations or []
        self.integrations = integrations or []
        self.registration_reads = 0

    async def execute(self, statement):
        if "platform_registrations" in str(statement):
            self.registration_reads += 1
            return _Result(self.registrations)
        return _Result(self.integrations)

    def begin_nested(self):
        return _Savepoint(self)


class _Savepoint:
    def __init__(self, db: _Db) -> None:
        self.db = db

    async def __aenter__(self):
        self.db.savepoints = getattr(self.db, "savepoints", 0) + 1
        return self

    async def __aexit__(self, exc_type, *_exc):
        self.db.rolled_back = exc_type is not None
        return False


# ---------------------------------------------------------------------------
# The rule: a URL on the internal URL is shown on the registered URL
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "shown"),
    [
        (f"{INTERNAL}/dashboard/id/{ENTITY}", f"{PUBLIC}/dashboard/id/{ENTITY}"),
        (f"[Open in OpenCTI]({INTERNAL}/dashboard/id/{ENTITY})", f"[Open in OpenCTI]({PUBLIC}/dashboard/id/{ENTITY})"),
        ("HTTP://OpenCTI:8080/dashboard?q=1#top", f"{PUBLIC}/dashboard?q=1#top"),
        (f"{INTERNAL}/", f"{PUBLIC}/"),
        (INTERNAL, PUBLIC),
        (f"see <{INTERNAL}>.", f"see <{PUBLIC}>."),
        (f"{INTERNAL}/storage/get/abc and {INTERNAL}/admin", f"{PUBLIC}/storage/get/abc and {PUBLIC}/admin"),
        (f"It runs at {INTERNAL}.", f"It runs at {PUBLIC}."),
        (f"at {INTERNAL}: done", f"at {PUBLIC}: done"),
        (f"({INTERNAL}),", f"({PUBLIC}),"),
    ],
)
def test_a_url_on_the_internal_url_is_shown_on_the_registered_url(text, shown):
    assert REACHED.shown(text) == shown


@pytest.mark.parametrize(
    "text",
    [
        "http://opencti:80801/dashboard",
        "http://opencti:8080.example.com/dashboard",
        "http://opencti:8080:9090/dashboard",
        "http://opencti-old:8080/dashboard",
        "https://opencti:8080/dashboard",
        "http://user@opencti:8080/dashboard",
        "http://opencti/dashboard",
        "git+http://opencti:8080/repo",
        f"{PUBLIC}/dashboard/id/{ENTITY}",
    ],
)
def test_any_other_url_is_left_as_it_is(text):
    assert REACHED.shown(text) == text


def test_the_default_port_is_the_same_internal_url():
    platform = RegisteredPlatform("opencti", "https://cti.example.com", api_url="http://opencti")
    assert platform.shown("http://opencti:80/dashboard") == "https://cti.example.com/dashboard"
    assert platform.shown("http://opencti:8080/dashboard") == "http://opencti:8080/dashboard"


def test_an_internal_url_with_a_path_is_matched_on_its_whole_path():
    platform = RegisteredPlatform("opencti", "https://cti.example.com", api_url="http://gateway/opencti")
    assert platform.shown("http://gateway/opencti/dashboard") == "https://cti.example.com/dashboard"
    assert platform.shown("http://gateway/opencti-old/dashboard") == "http://gateway/opencti-old/dashboard"


def test_a_url_already_shown_is_never_moved_again():
    """One platform's registered URL may be another's internal URL: each link
    is moved once, onto its own platform, whatever the order."""
    first = RegisteredPlatform("opencti", "http://gateway:8080", api_url="http://opencti:8080")
    second = RegisteredPlatform("openaev", "https://aev.example.com", api_url="http://gateway:8080")
    text = "http://opencti:8080/dashboard and http://gateway:8080/admin"
    shown = "http://gateway:8080/dashboard and https://aev.example.com/admin"
    assert show_on_public_urls(text, [first, second]) == shown
    assert show_on_public_urls(text, [second, first]) == shown


def test_the_longest_internal_url_wins():
    gateway = RegisteredPlatform("openaev", "https://aev.example.com", api_url="http://gateway")
    under_it = RegisteredPlatform("opencti", "https://cti.example.com", api_url="http://gateway/opencti")
    text = "http://gateway/opencti/dashboard http://gateway/admin"
    shown = "https://cti.example.com/dashboard https://aev.example.com/admin"
    assert show_on_public_urls(text, [gateway, under_it]) == shown


@pytest.mark.parametrize(
    "registered",
    [
        "http://admin:s3cret@localhost:8080",
        "http://localhost:8080/?token=s3cret",
        "http://localhost:8080/#frag",
        "http://loc<al>host:8080",
        'http://local"host:8080',
        "http://local host:8080",
        "javascript:alert(1)",
        "not a url",
        "http://localhost:0",
    ],
)
def test_a_registered_url_that_names_no_identity_is_never_copied_into_a_link(registered):
    """The registered URL is shown in its canonical form, never as raw text: one
    carrying credentials, a query, a fragment or malformed bytes leaves the
    links as they are rather than putting it in front of a person."""
    platform = RegisteredPlatform("opencti", registered, api_url=INTERNAL)
    text = f"[Open in OpenCTI]({INTERNAL}/dashboard/id/{ENTITY})"
    assert platform.shown(text) == text
    assert show_on_public_urls(text, [platform, REACHED]) == f"[Open in OpenCTI]({PUBLIC}/dashboard/id/{ENTITY})"


@pytest.mark.parametrize(
    ("registered", "shown"),
    [
        (" HTTPS://CTI.Example.com:443/ ", "https://cti.example.com"),
        ("https://example.com/@tenant", "https://example.com/@tenant"),
        ("https://m\u00fcnchen.example/cti", "https://xn--mnchen-3ya.example/cti"),
        ("https://cti.example.com/caf\u00e9", "https://cti.example.com/caf%C3%A9"),
        ("http://localhost:8080/a b", "http://localhost:8080/a%20b"),
        ("http://localhost:8080/a\x00b", "http://localhost:8080/a%00b"),
        ("http://localhost:8080/x)y", "http://localhost:8080/x%29y"),
        ('http://localhost:8080/x"y', "http://localhost:8080/x%22y"),
        ("http://localhost:8080/x<y>", "http://localhost:8080/x%3Cy%3E"),
        ("http://[::1]:8080", "http://[::1]:8080"),
        ("https://cti.example.com/a%zzb", "https://cti.example.com/a%25zzb"),
        ("https://cti.example.com/a%2Fb", "https://cti.example.com/a%2Fb"),
        ("https://cti.example.com/a%2fb%", "https://cti.example.com/a%2fb%25"),
        ("https://cti.example.com/100%", "https://cti.example.com/100%25"),
        ("https://cti.example.com:8443/cti", "https://cti.example.com:8443/cti"),
    ],
)
def test_the_registered_url_is_shown_in_its_canonical_link_safe_form(registered, shown):
    """An internationalized host is written in its IDNA form and a path byte a
    link cannot carry is percent-encoded: the links are moved, and no raw byte
    of the registration reaches them."""
    platform = RegisteredPlatform("opencti", registered, api_url=INTERNAL)
    assert platform.shown(f"[x]({INTERNAL}/dashboard)") == f"[x]({shown}/dashboard)"


def test_a_credential_bearing_registration_never_reaches_a_tool_result():
    from app.services.integration_tools import _shown_tool_result

    platform = RegisteredPlatform("opencti", "http://admin:s3cret@localhost:8080", api_url=INTERNAL)
    result = ToolResult(tool_call_id="c", content=f"{INTERNAL}/dashboard/id/{ENTITY}")
    assert "s3cret" not in _shown_tool_result(result, platform).content
    assert "s3cret" not in _serialized_values(_shown_tool_result({"url": f"{INTERNAL}/x"}, platform))


def _serialized_values(result: object) -> str:
    """Every value a tool result carries, as the model reads it: a dict or list
    result is what the executor serializes, a string is itself."""
    return result if isinstance(result, str) else json.dumps(result, default=str)


def test_the_value_check_catches_a_secret_in_a_value():
    """``in`` on a dict only reads its keys: the check reads the values."""
    leaked = {"url": "http://admin:s3cret@localhost:8080/x"}
    assert "s3cret" not in leaked
    assert "s3cret" in _serialized_values(leaked)
    assert "s3cret" in _serialized_values(json.dumps(leaked))


async def test_a_credential_bearing_registration_never_reaches_an_answer():
    from app.services.url_guard import PlatformLinkBases, finalize_platform_links

    platform = RegisteredPlatform("opencti", "http://admin:s3cret@localhost:8080", api_url=INTERNAL)

    async def _bases():
        return PlatformLinkBases(urls=[], platforms=(platform,))

    out = await finalize_platform_links(f"See {INTERNAL}/dashboard/id/{ENTITY}.", _bases)
    assert "s3cret" not in out


def test_without_an_internal_url_nothing_is_rewritten():
    text = f"[Open in OpenCTI]({INTERNAL}/dashboard/id/{ENTITY})"
    assert PUBLIC_ONLY.shown(text) == text
    assert show_on_public_urls(text, []) == text


async def test_the_registration_is_the_one_the_integration_resolves_to():
    """The same registration ``platform_for_integration`` picks: the active,
    most recently seen one first - and only when it recorded an internal URL."""
    with_internal, without = uuid.uuid4(), uuid.uuid4()
    db = _Db(
        [
            _registration(with_internal),
            _registration(without, api_url=None),
            _registration(without, api_url="http://stale:8080"),
        ]
    )
    platforms = await platforms_reached_internally(db, [with_internal, without, "not-a-uuid"])
    assert set(platforms) == {str(with_internal)}
    assert platforms[str(with_internal)].identity == PUBLIC


async def test_a_failed_read_is_rolled_back_to_its_savepoint():
    """The read is best-effort on the caller's session: a failure is undone to
    its savepoint, so the caller's transaction stays usable."""

    class _FailingDb(_Db):
        async def execute(self, statement):
            raise RuntimeError("relation does not exist")

    db = _FailingDb()
    assert await platforms_reached_internally(db, [uuid.uuid4()]) == {}
    assert db.savepoints == 1
    assert db.rolled_back is True


async def test_no_integration_costs_no_query():
    db = _Db()
    assert await platforms_reached_internally(db, []) == {}
    assert db.registration_reads == 0


# ---------------------------------------------------------------------------
# The URL the agent builds links from
# ---------------------------------------------------------------------------


def test_the_listed_integration_url_is_the_registered_url():
    from app.services.integration_tools import get_integration_public_url

    integ = _integration(INTERNAL)
    assert get_integration_public_url(integ, REACHED) == PUBLIC
    assert get_integration_public_url(integ) == INTERNAL


def test_an_edited_base_url_is_listed_as_it_is():
    """A ``base_url`` edited away from the platform is not its internal URL."""
    from app.services.integration_tools import get_integration_public_url

    assert get_integration_public_url(_integration("http://elsewhere:8080"), REACHED) == "http://elsewhere:8080"


# ---------------------------------------------------------------------------
# The platform tools: called on the internal URL, links on the registered one
# ---------------------------------------------------------------------------


@pytest.fixture
def platform_tools(monkeypatch):
    """A fake OpenCTI builder registering one tool, and the base URL it called."""
    from app.services import integration_tools

    called: list[str] = []

    def _builder(token, executor, **_kwargs):
        base_url, _jwt = token.split("||", 1)

        async def lookup(entity_id: str) -> str:
            called.append(f"{base_url}/graphql")
            return f"Link: [Open in OpenCTI]({base_url}/dashboard/id/{entity_id})"

        async def lookup_result(entity_id: str) -> ToolResult:
            return ToolResult(tool_call_id="", content=f"{base_url}/dashboard/id/{entity_id}")

        executor.register("opencti_lookup", lookup)
        executor.register("opencti_lookup_result", lookup_result)
        return [
            ToolDefinition(name="opencti_lookup", description="", input_schema={}),
            ToolDefinition(name="opencti_lookup_result", description="", input_schema={}),
        ]

    async def _token(_db, _integration_id, provider, **_kwargs):
        return f"{INTERNAL}||signed"

    async def _no_refresh(_db, _rows):
        return {}, set()

    monkeypatch.setitem(integration_tools.PROVIDER_TOOLS, "opencti", _builder)
    monkeypatch.setattr(integration_tools, "_get_token", _token)
    monkeypatch.setattr(integration_tools, "_parallel_refresh_expiring_tokens", _no_refresh)
    return called


async def _run(executor, name: str) -> str:
    result = await executor.execute_tool(ToolCall(id="c1", name=name, arguments={"entity_id": ENTITY, "junk": 1}))
    return result.content


async def test_the_tools_call_the_internal_url_and_link_to_the_registered_url(platform_tools):
    from app.services.integration_tools import build_integration_tools

    integ = _integration(INTERNAL)
    db = _Db([_registration(integ.id)], [integ])
    _defs, executor = await build_integration_tools(db, ToolBindings({"opencti": [str(integ.id)]}, basis=BindingBasis.CALLER_CHECKED, reason="test"))

    assert await _run(executor, "opencti_lookup") == f"Link: [Open in OpenCTI]({PUBLIC}/dashboard/id/{ENTITY})"
    assert await _run(executor, "opencti_lookup_result") == f"{PUBLIC}/dashboard/id/{ENTITY}"
    assert platform_tools == [f"{INTERNAL}/graphql"]


async def test_without_an_internal_url_the_tools_are_left_as_built(platform_tools):
    from app.services.integration_tools import build_integration_tools

    integ = _integration(INTERNAL)
    db = _Db([_registration(integ.id, api_url=None)], [integ])
    _defs, executor = await build_integration_tools(db, ToolBindings({"opencti": [str(integ.id)]}, basis=BindingBasis.CALLER_CHECKED, reason="test"))

    assert await _run(executor, "opencti_lookup") == f"Link: [Open in OpenCTI]({INTERNAL}/dashboard/id/{ENTITY})"
    assert executor.handler("opencti_lookup").__name__ == "lookup"
    assert not hasattr(executor.handler("opencti_lookup"), "__wrapped__")


async def test_an_integration_no_registration_created_costs_no_query(platform_tools):
    from app.services.integration_tools import build_integration_tools

    integ = _integration(INTERNAL, auth_type="api_key")
    db = _Db([_registration(integ.id)], [integ])
    _defs, executor = await build_integration_tools(db, ToolBindings({"opencti": [str(integ.id)]}, basis=BindingBasis.CALLER_CHECKED, reason="test"))

    assert await _run(executor, "opencti_lookup") == f"Link: [Open in OpenCTI]({INTERNAL}/dashboard/id/{ENTITY})"
    assert db.registration_reads == 0


def test_a_dict_result_is_shown_as_the_executor_would_serialize_it():
    from app.services.integration_tools import _shown_tool_result

    assert _shown_tool_result({"url": f"{INTERNAL}/x"}, REACHED) == json.dumps({"url": f"{PUBLIC}/x"})
    unchanged = {"url": "https://elsewhere/x"}
    assert _shown_tool_result(unchanged, REACHED) is unchanged


def test_a_wrapped_handler_keeps_its_parameters_and_file_context():
    from app.services.integration_tools import _show_tool_results_on_public_url

    executor = CallableToolExecutor()

    async def read(entity_id: str, file_context=None) -> str:
        return f"{INTERNAL}/dashboard/id/{entity_id}"

    executor.register("read", read)
    _show_tool_results_on_public_url([ToolDefinition(name="read", description="", input_schema={})], executor, REACHED)
    assert executor._accepted_params["read"] == {"entity_id", "file_context"}
    assert executor._accepts_file_context["read"] is True


# ---------------------------------------------------------------------------
# Scope matching: either URL names the platform
# ---------------------------------------------------------------------------


async def test_the_registered_url_selects_the_integration_reached_internally():
    from app.services.integration_tools import resolve_integration_by_scope

    reached, other = _integration(INTERNAL), _integration("http://other-cti:8080")
    db = _Db([_registration(reached.id)])
    picked = await resolve_integration_by_scope(
        db, "opencti", [reached, other], {"x-platform-url": f"{PUBLIC}/"}, acting_user_id=None
    )
    assert picked is reached


async def test_the_internal_url_still_selects_it():
    from app.services.integration_tools import resolve_integration_by_scope

    reached, other = _integration(INTERNAL), _integration("http://other-cti:8080")
    db = _Db([_registration(reached.id)])
    picked = await resolve_integration_by_scope(
        db, "opencti", [reached, other], {"x-platform-url": INTERNAL}, acting_user_id=None
    )
    assert picked is reached


async def test_without_an_internal_url_the_scope_is_matched_as_before():
    from app.services.integration_tools import resolve_integration_by_scope

    reached, other = _integration(INTERNAL), _integration("http://other-cti:8080")
    db = _Db([_registration(reached.id, api_url=None)])
    picked = await resolve_integration_by_scope(
        db, "opencti", [reached, other], {"x-platform-url": PUBLIC}, acting_user_id=None
    )
    assert picked is None


# ---------------------------------------------------------------------------
# The answer: links on the internal URL shown on the registered URL
# ---------------------------------------------------------------------------


async def _bases(platforms=(REACHED,)):
    from app.services.url_guard import PlatformLinkBases

    return PlatformLinkBases(urls=[PUBLIC], platforms=tuple(platforms))


async def test_an_answer_link_on_the_internal_url_is_guarded_and_shown_on_the_registered_url():
    from app.services.url_guard import finalize_platform_links

    text = (
        f"See [the IOC]({INTERNAL}/dashboard/threats/indicators/{ENTITY}), "
        f"the file {INTERNAL}/storage/get/abc and `{INTERNAL}` in the compose file."
    )
    out = await finalize_platform_links(text, _bases)
    assert out == (
        f"See [the IOC]({PUBLIC}/dashboard/observations/indicators/{ENTITY}), "
        f"the file {PUBLIC}/storage/get/abc and `{INTERNAL}` in the compose file."
    )


async def test_an_answer_without_an_internal_url_is_guarded_as_before():
    from app.services.url_guard import finalize_platform_links, guard_urls

    text = f"See [the IOC]({PUBLIC}/dashboard/threats/indicators/{ENTITY}) and {INTERNAL}/storage/get/abc."
    out = await finalize_platform_links(text, lambda: _bases(()))
    assert out == guard_urls(text, [PUBLIC])


async def test_an_answer_without_a_link_resolves_nothing():
    from app.services.url_guard import finalize_platform_links

    async def _no_resolve():  # pragma: no cover - must not be called
        raise AssertionError("resolved the bases of an answer without a link")

    assert await finalize_platform_links("No link here.", _no_resolve) == "No link here."


async def test_the_url_guard_kill_switch_leaves_the_answer_as_written(monkeypatch):
    from app.services.url_guard import finalize_platform_links

    async def _no_resolve():  # pragma: no cover - must not be called
        raise AssertionError("resolved the bases with the URL guard off")

    monkeypatch.setattr(settings, "url_guard_enabled", False)
    text = f"{INTERNAL}/dashboard/id/{ENTITY}"
    assert await finalize_platform_links(text, _no_resolve) == text


async def test_the_bound_platforms_are_collected_on_their_registered_url():
    from app.services.url_guard import collect_platform_link_bases

    integ = _integration(INTERNAL)
    bases = await collect_platform_link_bases(_Db([_registration(integ.id)], [integ]), {"opencti": [str(integ.id)]})
    assert bases.urls == [PUBLIC]
    assert [p.identity for p in bases.platforms] == [PUBLIC]

    unchanged = _integration(INTERNAL)
    bases = await collect_platform_link_bases(
        _Db([_registration(unchanged.id, api_url=None)], [unchanged]), {"opencti": [str(unchanged.id)]}
    )
    assert bases.urls == [INTERNAL]
    assert bases.platforms == ()


async def test_an_opencrq_link_on_its_internal_url_is_shown_on_its_registered_url():
    """OpenCRQ has no route the guard validates, but its links are shown too."""
    from app.services.url_guard import collect_platform_link_bases, finalize_platform_links

    integ = _integration("http://opencrq:3000", provider="opencrq")
    registration = _registration(integ.id, api_url="http://opencrq:3000", platform_url="http://localhost:3000")
    db = _Db([registration], [integ])

    out = await finalize_platform_links(
        "[Open in OpenCRQ](http://opencrq:3000/tenant/risks/1)",
        lambda: collect_platform_link_bases(db, {"opencrq": [str(integ.id)]}),
    )
    assert out == "[Open in OpenCRQ](http://localhost:3000/tenant/risks/1)"


class _TransactionalDb(_Db):
    """A session whose transaction is aborted by a failed statement outside a
    savepoint, as PostgreSQL's is: every later statement then fails."""

    def __init__(self, *, fail_on: str, **kwargs) -> None:
        super().__init__(**kwargs)
        self.fail_on = fail_on
        self.aborted = False
        self.depth = 0

    async def execute(self, statement):
        if self.aborted:
            raise RuntimeError("current transaction is aborted")
        if self.fail_on in str(statement):
            # Inside a savepoint the failure is rolled back to it on exit.
            self.aborted = self.depth == 0
            raise RuntimeError("relation does not exist")
        return await super().execute(statement)

    def begin_nested(self):
        db = self

        class _Nested:
            async def __aenter__(self):
                db.depth += 1

            async def __aexit__(self, *_exc):
                db.depth -= 1
                return False

        return _Nested()


@pytest.mark.parametrize(
    ("fail_on", "urls"),
    [
        # Nothing could be read: no base URL at all, as before.
        ("FROM integrations", []),
        # The integrations were read: their links are guarded as stored.
        ("platform_registrations", [INTERNAL]),
    ],
)
async def test_a_failed_lookup_leaves_the_callers_session_usable(fail_on, urls):
    """Either query of the lookup may fail: each runs in a savepoint."""
    from app.services.url_guard import PlatformLinkBases, collect_platform_link_bases

    integ = _integration(INTERNAL)
    db = _TransactionalDb(fail_on=fail_on, registrations=[_registration(integ.id)], integrations=[integ])
    bases = await collect_platform_link_bases(db, {"opencti": [str(integ.id)]})

    assert bases == PlatformLinkBases(urls=urls)
    assert not db.aborted
    await db.execute("SELECT 1")


# ---------------------------------------------------------------------------
# Assignment results: the summary the run keeps, on the registered URL
# ---------------------------------------------------------------------------

_JSON_SCHEMA = {"type": "object", "properties": {"report": {"type": "string"}}, "required": ["report"]}


@pytest.fixture
def engine_run(monkeypatch):
    """``execute_assignment_run`` end to end (the stale-reference harness), with
    the run's bindings resolving to a platform reached on an internal URL."""
    import tests.test_assignment_stale_reference as harness
    from app.services import url_guard
    from tests.agentic_gate_stub import stub_agentic_gate

    stub_agentic_gate(monkeypatch)

    async def _bases(_db, _bindings):
        return url_guard.PlatformLinkBases(urls=[PUBLIC], platforms=(REACHED,))

    monkeypatch.setattr(url_guard, "collect_platform_link_bases", _bases)

    async def _run(answer: str, *, output_format: str = "markdown", report_integration_id: str | None = None):
        monkeypatch.setattr(harness, "SUMMARY", answer)
        run, assignment, agent = harness._engine_objects()
        agent.output_format = output_format
        agent.output_schema = _JSON_SCHEMA if output_format == "json" else None
        run, _session, _mocks = await harness._drive_run(
            monkeypatch,
            report_integration_id=report_integration_id,
            engine_objects=(run, assignment, agent),
        )
        return run

    return _run


async def test_a_structured_json_result_keeps_its_links_on_the_registered_url(engine_run):
    """The output-schema path re-serializes the repaired payload from the raw
    answer: the links are moved on the JSON the run keeps."""
    run = await engine_run(json.dumps({"report": f"See {INTERNAL}/dashboard/id/{ENTITY}"}), output_format="json")
    assert run.status == "completed", run.error
    assert json.loads(run.result_summary) == {"report": f"See {PUBLIC}/dashboard/id/{ENTITY}"}


async def test_a_markdown_result_is_guarded_on_the_registered_url(engine_run):
    run = await engine_run(f"[the IOC]({INTERNAL}/dashboard/threats/indicators/{ENTITY})")
    assert run.result_summary == f"[the IOC]({PUBLIC}/dashboard/observations/indicators/{ENTITY})"


async def test_a_failed_runs_summary_is_on_the_registered_url(engine_run):
    import tests.test_assignment_stale_reference as harness

    run = await engine_run(f"Synced to {INTERNAL}/dashboard/id/{ENTITY}", report_integration_id=harness.INTEGRATION_ID)
    assert run.status == "failed"
    assert run.result_summary == f"Synced to {PUBLIC}/dashboard/id/{ENTITY}"


# ---------------------------------------------------------------------------
# trigger.platform_url
# ---------------------------------------------------------------------------


async def test_the_trigger_platform_url_is_the_registered_url():
    from app.api.v1.webhooks import _shown_platform_url

    integ = _integration(INTERNAL)
    assert await _shown_platform_url(_Db([_registration(integ.id)]), integ) == PUBLIC

    no_internal = _integration(INTERNAL)
    assert await _shown_platform_url(_Db([_registration(no_internal.id, api_url=None)]), no_internal) == INTERNAL

    db = _Db([_registration(uuid.uuid4())])
    assert await _shown_platform_url(db, _integration(INTERNAL, auth_type="api_key")) == INTERNAL
    assert db.registration_reads == 0
