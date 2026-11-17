"""Mid-loop / long-call code paths must not pin a pooled DB connection.

The pre-loop ``release_db_connection_before_loop()`` contract (see
``test_chat_connection_release.py`` / ``test_streaming_connection_release.py``)
only helps if nothing RE-ACQUIRES the released session mid agentic loop and
holds it until the post-loop commit. Production kept logging
"Pool near exhaustion: 40/40 connections checked out" because:

* file-persisting builtin tools (``generate_file``, ``code_interpreter``
  output files, browser screenshots, ``generate_image``, custom-tool
  ``$output_files``) flushed on the CALLER session mid-loop — pinning a
  connection across every remaining LLM call of the run;
* the LLM proxy's non-streaming branches held the request connection across
  the whole upstream completion (10-120s);
* the memory auto-flush wrapped multiple LLM + embedding round-trips in one
  long transaction;
* ``store_knowledge`` held its session across the per-chunk embedding loop.

These tests pin the fixes via static source inspection, mirroring the
existing connection-release test style.
"""

import inspect
import re


def _source(obj) -> str:
    return inspect.getsource(obj)


# ---------------------------------------------------------------------------
# F1 — artifact persistence must use its own short-lived session
# ---------------------------------------------------------------------------


def test_persist_generated_file_never_writes_on_caller_session():
    """``_persist_generated_file`` is the single persistence point for every
    agent-produced file and is invoked from tool closures MID agentic loop.
    It must write + commit on a dedicated session (``_own_session``), never
    on the caller's (already released) session."""
    from app.services.builtin_tools import _shared

    source = _source(_shared._persist_generated_file)
    assert "del db" in source, (
        "_persist_generated_file must not use the caller session — writes go "
        "through a dedicated short-lived session"
    )
    assert "_own_session()" in source, (
        "_persist_generated_file must open its own session via _own_session()"
    )
    assert "own_db.commit()" in source, (
        "_persist_generated_file must commit its own session so the pooled "
        "connection is returned immediately"
    )


def test_generate_image_closure_never_writes_on_caller_session():
    """The ``generate_image`` closure runs mid-loop; its FileAttachment +
    ChatImage rows must land on a dedicated session, not the captured
    request session (a pending write there pins a connection until the
    post-loop commit)."""
    from app.services.builtin_tools.media import build_image_generation_tool

    source = _source(build_image_generation_tool)
    # Bare ``db.add(`` = the captured caller session; ``img_db.add(`` (the
    # dedicated session) is fine.
    assert not re.search(r"(?<![A-Za-z_])db\.add\(", source), (
        "generate_image must not add rows to the caller session"
    )
    assert "_own_session()" in source, (
        "generate_image must persist via the dedicated short-lived session"
    )


# ---------------------------------------------------------------------------
# F2 — LLM proxy non-streaming branches release before the upstream call
# ---------------------------------------------------------------------------


def test_proxy_chat_completions_releases_before_nonstreaming_create():
    """The non-streaming /v1/chat/completions branch calls the upstream
    provider synchronously (10-120s). The pooled connection must be released
    first — everything after the call uses its own session."""
    from app.api.v1.llm_proxy import proxy_chat_completions

    source = _source(proxy_chat_completions)
    create_pos = source.find("provider.client.chat.completions.create(**params)")
    assert create_pos != -1, (
        "proxy_chat_completions must have a non-streaming create(**params) call"
    )
    release = "await release_db_connection_before_loop("
    assert source.rfind(release, 0, create_pos) != -1, (
        "proxy_chat_completions must release the pooled DB connection BEFORE "
        "the non-streaming upstream completion call"
    )


def test_proxy_anthropic_messages_releases_before_nonstreaming_post():
    """The non-streaming /v1/messages pass-through (Claude Code with
    stream=false) posts to Anthropic synchronously; the request connection
    must be released first."""
    from app.api.v1.llm_proxy import proxy_anthropic_messages

    source = _source(proxy_anthropic_messages)
    post_pos = source.find("client.post(url, headers=headers, content=body)")
    assert post_pos != -1, (
        "proxy_anthropic_messages must have a non-streaming client.post call"
    )
    release = "await release_db_connection_before_loop("
    assert source.rfind(release, 0, post_pos) != -1, (
        "proxy_anthropic_messages must release the pooled DB connection "
        "BEFORE the non-streaming upstream Anthropic call"
    )


# ---------------------------------------------------------------------------
# F3 — memory auto-flush must not hold one transaction across LLM/embeds
# ---------------------------------------------------------------------------


def test_flush_facts_has_no_outer_transaction():
    """``_flush_facts_to_memory`` used to wrap the whole reconciliation
    (extraction LLM + N embeds + curator LLM + apply) in ``db.begin()``,
    pinning a connection for 30-90s per conversation compaction. Atomicity
    now lives in the apply phase inside ``reconcile_and_persist_facts``."""
    from app.services.conversation_memory import _flush_facts_to_memory

    source = _source(_flush_facts_to_memory)
    assert "async with db.begin()" not in source, (
        "_flush_facts_to_memory must not open a whole-function transaction — "
        "the apply phase inside reconcile_and_persist_facts commits itself"
    )


def test_reconcile_commits_release_points():
    """``reconcile_and_persist_facts`` must hand the pooled connection back
    (commit) around its LLM / embedding round-trips and commit the apply
    phase itself."""
    from app.services.persistent_memory import reconcile_and_persist_facts

    source = _source(reconcile_and_persist_facts)
    assert source.count("await db.commit()") >= 3, (
        "reconcile_and_persist_facts must commit-release before the embedding "
        "gather, before the curator LLM call, and after the apply phase"
    )


# ---------------------------------------------------------------------------
# F4 — store_knowledge embeds chunks without an open session
# ---------------------------------------------------------------------------


def test_store_knowledge_embeds_between_sessions():
    """The chunk-embedding loop (N HTTP round-trips, 10-30s for large docs)
    must run BETWEEN the config-read session (Phase A) and the write session
    (Phase C), never inside one."""
    from app.services.builtin_tools.kb import build_kb_store_tool

    source = _source(build_kb_store_tool)
    cfg_pos = source.find("as cfg_db")
    embed_pos = source.find("embedding_provider.embed(")
    write_pos = source.find("as kb_db")
    assert cfg_pos != -1 and embed_pos != -1 and write_pos != -1, (
        "store_knowledge must keep the phase A (config) / phase B (embed) / "
        "phase C (write) structure"
    )
    assert cfg_pos < embed_pos < write_pos, (
        "store_knowledge must embed chunks AFTER the config session closes "
        "and BEFORE the write session opens"
    )


# ---------------------------------------------------------------------------
# F5 — hourly memory maintenance releases around LLM / embedding calls
# ---------------------------------------------------------------------------


def test_consolidation_releases_before_merge_llm_call():
    from app.services.persistent_memory import consolidate_agent_facts

    source = _source(consolidate_agent_facts)
    commit_pos = source.find("await db.commit()")
    llm_pos = source.find("await llm.chat(")
    assert commit_pos != -1 and llm_pos != -1
    assert commit_pos < llm_pos, (
        "consolidate_agent_facts must commit-release the session before the "
        "per-scope merge LLM call"
    )


def test_reembed_releases_before_page_embeds():
    from app.services.persistent_memory import reembed_stale_memories

    source = _source(reembed_stale_memories)
    commit_pos = source.find("await db.commit()")
    embed_pos = source.find("provider.embed(")
    assert commit_pos != -1 and embed_pos != -1
    assert commit_pos < embed_pos, (
        "reembed_stale_memories must commit-release the session before each "
        "page's embedding round-trips"
    )


# ═══════════════════════════════════════════════════════════════════════════
# Phase 2 — setup-phase and non-agentic LLM-call holds
# ═══════════════════════════════════════════════════════════════════════════

_RELEASE = "release_db_connection_before_loop"


def _release_call_positions(source: str) -> list[int]:
    """Start offsets of ACTUAL awaited release calls, direct or aliased.

    A bare substring match on the helper name would also hit import
    statements and comments, making the guards prone to false positives.
    Only ``await <name>(`` counts, where
    ``<name>`` is the canonical helper or an ``import ... as`` alias bound
    in the same source.
    """
    names = {_RELEASE}
    for m in re.finditer(rf"{_RELEASE}\s+as\s+(\w+)", source):
        names.add(m.group(1))
    pattern = re.compile(
        r"await\s+(?:" + "|".join(re.escape(n) for n in sorted(names)) + r")\("
    )
    return [m.start() for m in pattern.finditer(source)]


def _assert_release_before(source: str, marker: str, context: str) -> None:
    marker_pos = source.find(marker)
    assert marker_pos != -1, f"{context}: marker {marker!r} not found"
    assert any(pos < marker_pos for pos in _release_call_positions(source)), (
        f"{context}: must await a pooled-connection release before {marker!r}"
    )


def test_ws_chat_releases_before_compaction_llm_call():
    """On compaction-cycle turns, compaction awaits a full LLM
    summarization — the WS per-message session must not hold
    its transaction across it."""
    from app.api.v1.chat import run_chat_connection

    _assert_release_before(
        _source(run_chat_connection), "await _manage_ws(", "run_chat_connection"
    )


def test_rest_chat_releases_before_compaction_llm_call():
    from app.api.v1.chat import send_message

    _assert_release_before(
        _source(send_message),
        "await manage_conversation_memory(",
        "send_message",
    )


def test_chat_releases_before_title_llm_calls():
    from app.api.v1.chat import _generate_title_after_done, run_chat_connection, send_message

    _assert_release_before(
        _source(send_message),
        "await generate_conversation_title(",
        "send_message (title)",
    )
    # The WS turn no longer awaits the title before ``done``:
    # the helper it spawns holds no session across the LLM call - its
    # sessions are opened before and after the call, never around it.
    assert "generate_conversation_title" not in _source(run_chat_connection)
    helper = _source(_generate_title_after_done).splitlines()
    call = next(line for line in helper if "await generate_conversation_title(" in line)
    sessions = [line for line in helper if "async with async_session()" in line]
    assert sessions, "the title helper writes through a session of its own"
    indent = len(call) - len(call.lstrip())
    assert all(indent <= len(s) - len(s.lstrip()) for s in sessions), (
        "the title LLM call must not run inside a session block"
    )


def test_public_chat_releases_before_compaction():
    import app.api.v1.public_chat as public_chat_module

    source = inspect.getsource(public_chat_module)
    # Every compaction call site in the module must be preceded by a release
    # within its enclosing block — approximate with ordering: each call must
    # have an awaited release call after the previous compaction call and
    # before its own position.
    release_positions = _release_call_positions(source)
    prev_end = 0
    call = "await manage_conversation_memory("
    found = 0
    while True:
        call_pos = source.find(call, prev_end)
        if call_pos == -1:
            break
        found += 1
        assert any(prev_end <= pos < call_pos for pos in release_positions), (
            f"public_chat compaction call #{found} lacks a preceding "
            "connection release"
        )
        prev_end = call_pos + len(call)
    # Both widget endpoints compact through ``_compact_public_history``, which
    # the metering wrapper calls once its quota gate let the turn through.
    assert found == 1, "expected the one public-chat compaction call site"
    assert call in inspect.getsource(public_chat_module._compact_public_history)


def test_channel_router_releases_before_compaction():
    from app.channels.router import _compact_history

    _assert_release_before(
        _source(_compact_history),
        "await manage_conversation_memory(",
        "_compact_history",
    )


def test_activation_gate_releases_before_llm_call():
    from app.services.assignment_engine import _run_activation_gate

    _assert_release_before(
        _source(_run_activation_gate),
        "await activation_llm.chat(",
        "_run_activation_gate",
    )


def test_load_mcp_tools_releases_before_uncached_connect():
    from app.services.builtin_tools.loaders import load_mcp_tools

    source = _source(load_mcp_tools)
    connect_pos = source.find("mcp_manager.connect_server(rt)")
    assert connect_pos != -1
    assert any(pos < connect_pos for pos in _release_call_positions(source)), (
        "load_mcp_tools must release the caller's pooled connection before "
        "the uncached network connect gather"
    )


def test_inline_oauth_refresh_releases_before_network():
    # `_try_inline_refresh` resolves the integration's relay route and
    # runs `_refresh_inline` under it; the lock-wait and the HTTP flow are there.
    from app.services.integration_tools import _refresh_inline

    source = _source(_refresh_inline)
    release_positions = _release_call_positions(source)
    lock_pos = source.find("acquire_refresh_lock(")
    assert release_positions and lock_pos != -1
    assert release_positions[0] < lock_pos, (
        "_refresh_inline must release the caller's pooled connection "
        "before the lock-wait / token-endpoint HTTP flow"
    )


def test_inline_oauth_refresh_releases_its_own_session_before_the_token_request():
    """The chat-time refresh re-reads the stored row on a session of its own
    under the refresh lock; that read must not pin a pooled connection across
    the token endpoint call."""
    from app.services.integration_tools import _refresh_inline_locked

    _assert_release_before(
        _source(_refresh_inline_locked),
        "await _do_inline_refresh(",
        "_refresh_inline_locked",
    )


def test_token_refresh_releases_before_http_gather():
    from app.services.token_refresh import refresh_expiring_tokens

    _assert_release_before(
        _source(refresh_expiring_tokens),
        "async with egress_client() as http:",
        "refresh_expiring_tokens",
    )


def test_locked_integration_refresh_releases_before_the_token_request():
    """The sweep re-reads each integration under its refresh lock; that read
    must not pin a pooled connection across the token endpoint call."""
    from app.services.token_refresh import _refresh_stored_integration

    _assert_release_before(
        _source(_refresh_stored_integration),
        "await _do_refresh_integration(",
        "_refresh_stored_integration",
    )


def test_locked_mcp_refresh_releases_before_the_token_request():
    """The sweep re-reads each MCP server under its refresh lock; that read
    must not pin a pooled connection across the token endpoint call."""
    from app.services.token_refresh import _refresh_stored_mcp

    _assert_release_before(
        _source(_refresh_stored_mcp),
        "await _do_refresh_mcp(",
        "_refresh_stored_mcp",
    )


def test_event_poller_releases_before_provider_poll():
    from app.services.event_poller import _dispatch_poll_page, poll_integration_events

    # The provider is polled page after page by ``_dispatch_poll_page``;
    # every page after the first follows a dispatch, whose commit released the
    # connection again.
    assert "await _poll_provider(" in _source(_dispatch_poll_page)
    _assert_release_before(
        _source(poll_integration_events),
        "await _dispatch_poll_page(",
        "poll_integration_events",
    )


def test_skill_refresh_releases_before_http_loop():
    from app.services.autonomous import _refresh_stale_skills

    _assert_release_before(
        _source(_refresh_stale_skills),
        "async with egress_client(",
        "_refresh_stale_skills",
    )


def test_evaluation_entries_release_before_agent_and_judge():
    from app.services.evaluation_engine import execute_evaluation_run

    source = _source(execute_evaluation_run)
    release_positions = _release_call_positions(source)
    exec_pos = source.find("_execute_agent_full_loop(")
    judge_pos = source.find("await score_result(")
    assert exec_pos != -1 and judge_pos != -1
    assert any(pos < exec_pos for pos in release_positions), (
        "execute_evaluation_run must release before the per-entry agent "
        "execution"
    )
    assert any(exec_pos < pos < judge_pos for pos in release_positions), (
        "execute_evaluation_run must release again before the judge LLM "
        "call"
    )


def test_consult_agent_is_bounded_by_semaphore():
    """Parallel consults each open a session + run a nested agent setup —
    both the chat and assignment consult closures must gate on the per-run
    fan-out semaphore."""
    from app.services import assignment_engine as engine_module
    from app.services.agent_chat import run_agent_chat

    # The consult closure lives inside ``run_agent_chat`` — its source is
    # both narrower than the whole module and keeps a single import style
    # for ``app.services.agent_chat`` in this file (code-quality follow-up).
    chat_src = inspect.getsource(run_agent_chat)
    engine_src = inspect.getsource(engine_module)
    assert "async with _consult_sem, _session_factory() as consult_db:" in chat_src, (
        "agent_chat._consult_agent must acquire the per-run consult "
        "semaphore before opening its session"
    )
    assert "async with _consult_sem, async_session() as consult_db:" in engine_src, (
        "assignment_engine._consult_agent must acquire the per-run consult "
        "semaphore before opening its session"
    )


def test_persistent_context_load_supports_provider_preresolution():
    """The per-turn memory context load must accept a pre-resolved embedding
    provider and a release-before-embed flag so the setup-phase query
    embedding never runs on a pinned request connection."""
    import inspect as _inspect

    from app.services.persistent_memory import (
        load_persistent_context,
        load_persistent_entries,
    )

    for loader in (load_persistent_context, load_persistent_entries):
        sig = _inspect.signature(loader)
        assert "_provider_model" in sig.parameters
        assert "release_before_embed" in sig.parameters

    from app.services.agent_chat import run_agent_chat
    from app.services.assignment_helpers import _load_memory_context

    for fn, name, call in (
        (run_agent_chat, "agent_chat", "load_persistent_context("),
        (_load_memory_context, "assignment_helpers", "load_persistent_entries("),
    ):
        src = _source(fn)
        call_pos = src.find(call)
        assert call_pos != -1, f"{name} no longer calls {call[:-1]}"
        window = src[call_pos : call_pos + 600]
        assert "release_before_embed=True" in window, (
            f"{name} must pass release_before_embed=True to "
            f"{call[:-1]}"
        )


def test_pool_warning_is_debounced_and_names_holders():
    """The near-exhaustion warning must be rate-limited and include the
    oldest holders — a bare per-checkout count flood is useless for
    root-causing."""
    import app.core.database as db_module

    source = inspect.getsource(db_module)
    assert "_WARN_INTERVAL_S" in source
    assert "Oldest holders" in source
    assert '@event.listens_for(engine.sync_engine, "checkin")' in source, (
        "checkin listener required to stop holder entries leaking"
    )


def test_pool_holder_bookkeeping_add_and_remove():
    """Checkout records a holder entry; checkin removes it."""
    from types import SimpleNamespace

    import app.core.database as db_module

    record = object()
    proxy = SimpleNamespace(_pool=SimpleNamespace(checkedout=lambda: 1))
    db_module._on_checkout(None, record, proxy)
    assert id(record) in db_module._holders
    entry = db_module._holders[id(record)]
    assert set(entry) >= {"task", "where", "since"}
    db_module._on_checkin(None, record)
    assert id(record) not in db_module._holders
