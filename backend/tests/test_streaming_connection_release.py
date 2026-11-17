"""Long-lived streaming endpoints must release the pooled DB connection first.

FastAPI runs a ``Depends(get_db)`` teardown (commit + close) only AFTER the
response finishes. For an SSE / ``StreamingResponse`` "finishes" means when the
stream closes — which for the activity feed is when the browser tab closes and
for the LLM proxy is when the whole completion has streamed. Any endpoint that
opens a transaction on the request session (a ``SELECT`` autobegins one, which
checks out a pooled connection) and then returns a long-lived stream therefore
pins that connection (idle-in-transaction) for the entire stream. N open
dashboards / in-flight completions ⇒ N pinned connections ⇒ the main pool
exhausts ("Pool near exhaustion: 25/25 connections checked out").

The fix is to call :func:`app.core.database.release_db_connection_before_loop`
after the pre-stream DB work and BEFORE returning the ``StreamingResponse`` (the
generators use their own short-lived sessions or none). This test pins that
ordering via static source inspection, mirroring
``test_chat_connection_release.py``.
"""

import inspect


def _assert_release_before_stream(func, func_name: str) -> None:
    source = inspect.getsource(func)
    release_pos = source.find("await release_db_connection_before_loop(")
    assert release_pos != -1, (
        f"{func_name} must call release_db_connection_before_loop() so it does "
        "not pin a pooled DB connection for the whole stream"
    )
    stream_pos = source.find("StreamingResponse(")
    assert stream_pos != -1, f"{func_name} must return a StreamingResponse"
    assert release_pos < stream_pos, (
        f"{func_name}: release_db_connection_before_loop must be called BEFORE "
        "returning the StreamingResponse"
    )


def test_activity_stream_releases_connection_before_sse():
    """The SSE activity feed does all its work over a dedicated Redis pub/sub
    client and never touches ``db`` again, yet without the release it holds one
    main-pool connection per open dashboard tab for the tab's whole lifetime."""
    from app.api.v1.activity import activity_stream

    _assert_release_before_stream(activity_stream, "activity_stream")


def test_proxy_chat_completions_releases_connection_before_stream():
    """The OpenAI-compatible proxy streams for the whole (multi-minute)
    completion; the generator uses its own session for usage tracking."""
    from app.api.v1.llm_proxy import proxy_chat_completions

    _assert_release_before_stream(proxy_chat_completions, "proxy_chat_completions")


def test_proxy_anthropic_messages_releases_connection_before_stream():
    """The Anthropic pass-through (used by Claude Code) streams for the whole
    completion via its own httpx client."""
    from app.api.v1.llm_proxy import proxy_anthropic_messages

    _assert_release_before_stream(proxy_anthropic_messages, "proxy_anthropic_messages")


def test_proxy_chat_completions_releases_before_provider_bridges():
    """The /v1/chat/completions Anthropic and Gemini/Bedrock streaming bridges
    (``_proxy_via_anthropic`` / ``_proxy_via_provider_chat``) receive no ``db``
    and stream the whole completion from the provider. Since Claude is the
    platform default, the Anthropic bridge carries most proxy traffic, so the
    caller MUST release the pooled connection before dispatching to each bridge
    — otherwise one main-pool connection stays pinned (idle-in-transaction) for
    every in-flight completion, which was the dominant pool-exhaustion source.
    """
    from app.api.v1.llm_proxy import proxy_chat_completions

    source = inspect.getsource(proxy_chat_completions)
    release = "await release_db_connection_before_loop("
    for bridge in ("_proxy_via_anthropic(", "_proxy_via_provider_chat("):
        bridge_pos = source.find(bridge)
        assert bridge_pos != -1, f"proxy_chat_completions must dispatch to {bridge}"
        assert source.rfind(release, 0, bridge_pos) != -1, (
            "proxy_chat_completions must call release_db_connection_before_loop() "
            f"before dispatching to {bridge} (the bridge streams without the "
            "request db)"
        )
