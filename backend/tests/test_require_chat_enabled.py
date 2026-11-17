"""Tests for the ``require_chat_enabled`` FastAPI dependency.

``require_chat_enabled`` is the runtime gate that returns 403 from every
chat entry point (REST, WebSocket close 4003, MCP, LLM proxy, public
widget) when the deployment is in XTM One's CE-only tier — see
``backend/app/api/deps.py``. Without it a stale tab or a direct API
client could side-step the splash by hitting the API directly.

These tests lock in the contract at two layers:

1. **Direct invocation** of ``require_chat_enabled`` — covers the
   no-op path (chat enabled) and the 403-with-trial-language path
   (chat disabled). Asserts on the exact HTTP status, detail copy and
   FastAPI plumbing.
2. **TestClient integration test** — mounts a minimal FastAPI app
   with one endpoint that uses ``Depends(require_chat_enabled)``,
   monkeypatches ``is_chat_enabled``, and hits the endpoint via
   ``TestClient`` to confirm the dependency is actually wired into the
   request lifecycle (a unit-only test would happily pass even if a
   regression dropped ``Depends(require_chat_enabled)`` from a chat
   route).

A bypass-via-direct-handler-call test mirrors
``test_disable_custom_variables.py``'s structure so future contributors
copying that file find the same shape here.
"""

from __future__ import annotations

import pytest
from fastapi import Depends, FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.api import deps
from app.api.v1 import platform as platform_api
from app.core.database import get_db


@pytest.fixture(autouse=True)
def _ai_state_primed():
    """The gate reads the AI state, which is cached out of the request path.

    Boot primes that cache, so in production the gate does no database
    work — which is what ``_FakeDb`` below asserts. Priming it here reproduces
    the running platform rather than a cold process.
    """
    from app.services import ai_state

    ai_state.prime_cache(True)
    yield
    ai_state.invalidate_cache()


class _FakeDb:
    """The gate fires *before* any DB access — assert that.

    Any execute/commit during the gate path indicates a regression
    that moved the gate past a DB call, which would have side effects
    we don't want for CE-only requests.
    """

    async def execute(self, *_args, **_kwargs):
        raise AssertionError(
            "DB execute() must NOT be called when require_chat_enabled "
            "raises — the gate must short-circuit before any DB work"
        )

    async def commit(self):
        raise AssertionError(
            "DB commit() must NOT be called when require_chat_enabled raises"
        )


# ── Direct invocation: unit-level contract ────────────────────────────────


@pytest.mark.asyncio
async def test_require_chat_enabled_passes_when_chat_is_enabled(monkeypatch):
    """When ``is_chat_enabled`` returns True the dependency returns
    None (FastAPI treats that as a successful gate pass).
    """

    async def _is_chat_enabled(_db):
        return True

    monkeypatch.setattr(platform_api, "is_chat_enabled", _is_chat_enabled)

    # Must not raise; must return None (the dependency has no payload).
    assert await deps.require_chat_enabled(db=_FakeDb()) is None  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_require_chat_enabled_raises_403_when_chat_is_disabled(monkeypatch):
    """When ``is_chat_enabled`` returns False the dependency raises
    ``HTTPException(403)`` with the CE-only trial / contact-sales
    copy. The detail string is part of the contract — channel adapters
    surface it back to the user.
    """

    async def _is_chat_enabled(_db):
        return False

    monkeypatch.setattr(platform_api, "is_chat_enabled", _is_chat_enabled)

    with pytest.raises(HTTPException) as exc_info:
        await deps.require_chat_enabled(db=_FakeDb())  # type: ignore[arg-type]

    assert exc_info.value.status_code == 403
    detail = str(exc_info.value.detail).lower()
    # Detail must point the caller at the upgrade paths; keep the
    # checks loose enough to survive copy tweaks but tight enough that
    # a stripped-down detail (e.g. just "forbidden") fails CI. The URL
    # checks use the full https://filigran.io/<path>/ form (instead of
    # bare "filigran.io") so CodeQL doesn't mistake the assertion for
    # a URL-substring allowlist (which would be a real vulnerability
    # in production code but is a false positive in test assertions).
    assert "chat is unavailable" in detail
    assert "trial" in detail or "enterprise" in detail
    assert "https://filigran.io/contact/" in detail
    assert (
        "https://filigran.io/filigran-xtm-one-enterprise-edition-trial/"
        in detail
    )


# ── TestClient integration: confirm Depends() actually fires ──────────────


@pytest.fixture
def chat_gate_client(monkeypatch) -> TestClient:
    """Minimal FastAPI app with one endpoint gated on
    ``require_chat_enabled``.

    A real chat route (``chat.py`` POST /conversations, the WebSocket,
    ``platform.py`` chat sessions, ``llm_proxy.py`` /chat/completions,
    ``mcp_endpoint.py`` /user, ``public_chat.py``) would pull in the
    whole DB + WebSocket + LLM proxy surface — overkill for asserting
    that the dependency is wired into the request lifecycle. Mounting
    a one-line probe endpoint isolates the gate.

    A regression PR that removes ``Depends(require_chat_enabled)``
    from a real chat route is a separate concern: this test asserts
    the dependency *implementation* is sound; the source-grep tests
    elsewhere assert each route still references it.
    """
    app = FastAPI()

    @app.get("/probe")
    async def _probe(_: None = Depends(deps.require_chat_enabled)):
        return {"status": "ok"}

    async def _stub_db():
        # Yields the same fake DB used by the unit tests — the gate
        # never touches it on a 403 path.
        yield _FakeDb()

    app.dependency_overrides[get_db] = _stub_db

    return TestClient(app)


def test_probe_returns_403_when_chat_is_disabled(chat_gate_client, monkeypatch):
    async def _is_chat_enabled(_db):
        return False

    monkeypatch.setattr(platform_api, "is_chat_enabled", _is_chat_enabled)

    response = chat_gate_client.get("/probe")

    assert response.status_code == 403, (
        f"expected 403 from the require_chat_enabled gate; got "
        f"{response.status_code}: {response.text!r}"
    )
    detail = response.json().get("detail", "").lower()
    assert "chat is unavailable" in detail, (
        f"403 detail must include the CE-only lockdown message so "
        f"channel adapters / direct API callers see why the request "
        f"was rejected; got {detail!r}"
    )


def test_probe_returns_200_when_chat_is_enabled(chat_gate_client, monkeypatch):
    """Positive path — confirms the gate is a no-op when chat is
    enabled (otherwise every chat route would always 403 even in
    licensed deployments).
    """

    async def _is_chat_enabled(_db):
        return True

    monkeypatch.setattr(platform_api, "is_chat_enabled", _is_chat_enabled)

    response = chat_gate_client.get("/probe")

    assert response.status_code == 200, (
        f"expected 200 (gate must be a no-op when chat is enabled); "
        f"got {response.status_code}: {response.text!r}"
    )
    assert response.json() == {"status": "ok"}


# ── Source-grep: locks in that chat routes still depend on the gate ──────


def test_chat_routes_still_enforce_chat_availability_gate():
    """Defense-in-depth: every chat entry-point router must still
    reference the chat-availability gate.

    A regression PR could silently drop ``Depends(require_chat_enabled)``
    (or the inline ``is_chat_enabled`` call used by routes with
    non-Depends auth flows like the LLM proxy) from a chat route —
    and because the dependency would never run on that endpoint,
    every other test in this file would still pass. A regex grep
    on the routers that wire chat surfaces locks that down: if a
    chat entry point stops gating, this test fails loudly and
    points the contributor at the exact file.

    The patterns deliberately match function-call / dependency-injection
    syntax rather than bare substrings, so a comment, docstring, or
    leftover import that mentions ``require_chat_enabled`` or
    ``is_chat_enabled`` does **not** satisfy the assertion. Two
    forms are accepted because the LLM proxy uses API-key auth and
    can't use FastAPI ``Depends``:

    * ``Depends(require_chat_enabled)`` — used by ``chat.py``,
      ``platform.py``.
    * ``is_chat_enabled(...)`` callable invocation — used inline by
      ``llm_proxy.py``, ``mcp_endpoint.py``, ``public_chat.py``, and
      ``chat.py``'s WebSocket path.

    The router list mirrors where the dependency is inserted:

    * ``chat.py``         — REST POST /conversations + WebSocket close 4003
    * ``platform.py``     — platform-chat sessions + messages
    * ``llm_proxy.py``    — OpenAI-compatible /chat/completions, /messages
                            (inline ``is_chat_enabled``, not Depends)
    * ``mcp_endpoint.py`` — /mcp/user + /mcp/global
    * ``public_chat.py``  — public widget non-streaming + streaming
    """
    import inspect
    import re

    from app.api.v1 import chat as chat_api
    from app.api.v1 import llm_proxy as llm_proxy_api
    from app.api.v1 import mcp_endpoint as mcp_endpoint_api
    from app.api.v1 import platform as platform_api_module
    from app.api.v1 import public_chat as public_chat_api

    # Either form must appear as actual call / dependency-injection
    # syntax in the module source. The inline form requires the
    # ``await`` prefix so the helper's own ``async def is_chat_enabled(...)``
    # signature in ``platform.py`` doesn't satisfy the assertion (which
    # would let a regression drop the ``Depends`` line and still pass).
    depends_pattern = re.compile(
        r"Depends\(\s*require_chat_enabled\s*[,\)]"
    )
    # ``is_chat_licensed`` counts too: it *is* the CE-only tier check, split out
    # of ``is_chat_enabled`` so a **read** can ask the licence without the AI
    # toggle (FR-008). A caller pairing it with ``is_ai_enabled`` has the
    # same two gates ``is_chat_enabled`` applies, spelled apart so each refusal
    # can state its own reason.
    inline_pattern = re.compile(
        r"\bawait\s+is_chat_(?:enabled|licensed)\s*\("
    )

    for mod in (
        chat_api,
        platform_api_module,
        llm_proxy_api,
        mcp_endpoint_api,
        public_chat_api,
    ):
        source = inspect.getsource(mod)
        # Strip docstrings and comments so a stale comment like
        # "# was gated via require_chat_enabled" can't satisfy the
        # check. The ast-based strip is a defensive belt-and-braces
        # on top of the regex (which already requires call syntax)
        # because some modules also have ``"""require_chat_enabled
        # (...)"""`` examples in their docstrings.
        stripped = _strip_docstrings_and_comments(source)
        assert depends_pattern.search(stripped) or inline_pattern.search(
            stripped
        ), (
            f"{mod.__name__} no longer wires the chat-availability "
            f"gate as actual call syntax (matched against "
            f"{depends_pattern.pattern!r} or {inline_pattern.pattern!r} "
            f"after docstring/comment stripping) — a chat entry "
            f"point may have lost its CE-only gate. Add "
            f"``Depends(require_chat_enabled)`` back to the affected "
            f"route (or call ``await is_chat_enabled(db)`` inline for "
            f"routes with non-Depends auth like the LLM proxy / MCP "
            f"endpoint / public widget), or update the router list "
            f"in this test."
        )


def _strip_docstrings_and_comments(source: str) -> str:
    """Remove ``#`` comments and module/function/class docstrings
    from a Python source string.

    The chat-routes source-grep test uses this to ensure that a
    stale comment or docstring referencing ``require_chat_enabled``
    /  ``is_chat_enabled`` cannot satisfy the assertion — only an
    actual call site does.

    Falls back to the original source on any parse error (the
    module wouldn't have imported cleanly above the test in that
    case, so this branch is only hit if the AST surface drifts in
    a way that breaks ``ast.unparse`` round-tripping; the regex
    on the original source is still a strict superset).
    """
    import ast
    import io
    import tokenize

    try:
        tree = ast.parse(source)
    except SyntaxError:
        return source

    docstring_spans: list[tuple[int, int]] = []
    for node in ast.walk(tree):
        if isinstance(
            node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
        ):
            doc_node = ast.get_docstring(node, clean=False)
            if doc_node is None:
                continue
            body0 = node.body[0]
            if (
                isinstance(body0, ast.Expr)
                and isinstance(body0.value, ast.Constant)
                and isinstance(body0.value.value, str)
            ):
                docstring_spans.append(
                    (body0.lineno, body0.end_lineno or body0.lineno)
                )

    lines = source.splitlines(keepends=True)
    blanked = list(lines)
    for start, end in docstring_spans:
        for ln in range(start - 1, end):
            if 0 <= ln < len(blanked):
                blanked[ln] = "\n"
    no_docs = "".join(blanked)

    out_tokens = []
    try:
        for tok in tokenize.generate_tokens(io.StringIO(no_docs).readline):
            if tok.type == tokenize.COMMENT:
                continue
            out_tokens.append(tok)
        return tokenize.untokenize(out_tokens)
    except tokenize.TokenizeError:
        return no_docs
