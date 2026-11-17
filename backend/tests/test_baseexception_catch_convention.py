"""Pin the CodeQL ``py/catch-base-exception`` convention across every
tracing close-path the PR introduced.

Earlier rounds established the convention at the agent loop's
tool-execution helpers (``_exec_with_span`` single and parallel branches —
already pinned by ``test_parallel_tool_path_has_no_baseexception_catch``
in ``test_tool_span_error_handling.py``), ``_continue_truncated_response``,
and the empty-response-retry rescue block: wherever the previous shape
was ``except BaseException as exc: ... raise``, split into an explicit
``except asyncio.CancelledError`` handler (close + re-raise) followed by
``except Exception`` (close + re-raise).  ``SystemExit`` /
``KeyboardInterrupt`` propagate uninstrumented at every site — they must
never be intercepted by tracing instrumentation even when the catcher
re-raises, both for static-analysis cleanliness (CodeQL
``py/catch-base-exception``) and so an operator-initiated SIGINT cannot
be masked by a tracing close-path.

This file pins the SAME convention at the four remaining tracing
close-paths that were not previously covered by a static guard:

* ``backend/app/services/assignment_engine.py::_run_activation_gate``
  (the Copilot-flagged site).
* ``backend/app/tools/agent_loop.py`` — streaming-path LLM-call
  ``try/except`` (the ``_gen_ctx`` close + re-raise around the
  streaming ``llm.chat_stream()`` call).
* ``backend/app/tools/agent_loop.py`` — non-streaming-path LLM-call
  ``try/except`` (the same shape around ``llm.chat()``).
* ``backend/app/tools/agent_loop.py`` — iteration-span ``finally``
  block (the ``_iter_error`` capture wrapping the per-iteration body).
* ``backend/app/services/langfuse_tracing.py::traced_generation`` —
  the shared async context manager used by every direct-generation
  call (conversation memory compaction, persistent memory curator,
  agent memory consolidation, user memory consolidation, routing
  evaluator, the three chat-file direct generations).

The tests below are textual guards that read the source of each helper
and assert (1) the forbidden ``except BaseException`` pattern is gone
AND (2) the explicit ``except asyncio.CancelledError`` + ``except
Exception`` handlers ARE present, so a regression that strips both at
once still trips loudly.  Runtime semantics (close-with-exception-object,
re-raise, contextvar reset where applicable) are already covered by the
per-site behavioural tests (``test_activation_gate_nesting.py``,
``test_continuation_subspans.py``, ``test_empty_response_retry_subspan.py``,
``test_traced_generation_helper.py``, ``test_tool_span_error_handling.py``)
— this file pins the structural shape so the static-analysis contract
cannot silently drift back.
"""

from __future__ import annotations

import inspect
import re

from app.services import assignment_engine as ae
from app.services import langfuse_tracing as lf
from app.tools import agent_loop as al

# ``_HAS_BASEEXCEPTION_CATCH`` is the textual marker for a forbidden
# clause.  Anchored on whitespace + ``except BaseException`` (no leading
# backticks, no Markdown) so it only matches actual Python ``except``
# statements — not the new explanatory comment blocks that mention
# ``except BaseException`` in prose to document what the fix replaced.
_HAS_BASEEXCEPTION_CATCH = re.compile(
    r"(?m)^[ \t]+except[ \t]+BaseException(?:[ \t]+as[ \t]+\w+)?[ \t]*:"
)


def _assert_split_convention(body: str, *, where: str) -> None:
    """Assert ``body`` matches the explicit ``CancelledError`` + ``Exception``
    split convention and contains no ``except BaseException`` catcher.

    A single helper because the regression message has to point the
    contributor at the same fix at every site — keeping the assertions
    consistent here also helps a future engineer who only reads one
    failure and copies the explanation across to the others.
    """
    assert _HAS_BASEEXCEPTION_CATCH.search(body) is None, (
        f"{where} must NOT catch BaseException — it trips CodeQL "
        f"py/catch-base-exception and silently re-intercepts SystemExit "
        f"/ KeyboardInterrupt even though it re-raises. Split into "
        f"`except asyncio.CancelledError` (close + re-raise) and "
        f"`except Exception` (close + re-raise), matching the existing "
        f"convention adopted by the agent loop's tool-execution / "
        f"continuation / empty-response-retry sites."
    )
    assert "except asyncio.CancelledError" in body, (
        f"{where} must include an explicit `except asyncio.CancelledError` "
        f"handler that records the cancellation on the trace observation "
        f"before re-raising — without it, an asyncio cancellation "
        f"mid-call would let the `finally` close the observation as a "
        f"clean success (`error=None`)."
    )
    assert "except Exception" in body, (
        f"{where} must include an explicit `except Exception` handler "
        f"as the catch-all for ordinary failures. The CancelledError "
        f"handler alone is not enough — a generic exception inside the "
        f"observed call would otherwise propagate uninstrumented."
    )


def test_activation_gate_uses_split_handlers():
    """``_run_activation_gate`` (the Copilot-flagged site) must use the
    explicit ``CancelledError`` + ``Exception`` split.

    The activation gate guards expensive assignment runs by asking an
    LLM whether the trigger payload is worth processing; recording a
    mid-gate cancellation on the gate's generation tells operators
    exactly why a run never reached the agent loop — a regression to
    ``except BaseException`` (or to ``except Exception`` only) would
    either trip CodeQL or silently drop the cancellation case from the
    Langfuse trace.
    """
    body = inspect.getsource(ae._run_activation_gate)
    _assert_split_convention(body, where="_run_activation_gate")


def test_traced_generation_uses_split_handlers():
    """``langfuse_tracing.traced_generation`` is the shared async context
    manager used by every direct-generation call site — conversation
    memory compaction, persistent memory curator/reconciliation, agent
    memory consolidation, user memory consolidation, routing evaluator
    and the chat-file direct generations.  A single regression here
    flips the convention back across all of those at once, so the
    static guard is especially valuable for this site.
    """
    body = inspect.getsource(lf.traced_generation)
    _assert_split_convention(body, where="langfuse_tracing.traced_generation")


def test_streaming_path_llm_call_uses_split_handlers():
    """The streaming-path LLM call in ``run_agent_loop`` (around
    ``llm.chat_stream()``) closes the ``_gen_ctx`` observation in its
    error handler and re-raises.  Same shape as the existing fix on
    ``_exec_with_span``: explicit split is required for CodeQL
    compliance even though the catcher re-raises.
    """
    source = inspect.getsource(al.run_agent_loop)
    # Anchor on the generation close of an aborted call at this site
    # (``_end_aborted_generation`` closes the span and records what the
    # stream reported) because the variable name is unique to this site
    # (the non-streaming site uses ``_exc``, the iteration boundary uses
    # ``_iter_error``).
    anchor = "_end_aborted_generation(_gen_ctx, _gen_exc, _usage_sink, _merge_usage)"
    assert anchor in source, (
        "expected to find the streaming-path generation close call "
        f"({anchor!r}) inside run_agent_loop"
    )
    # Carve a sliding window of source around the anchor — 1500 chars
    # either side covers the full ``except asyncio.CancelledError`` +
    # ``except Exception`` pair plus the surrounding ``try`` body and
    # the ``raise`` statements.
    idx = source.find(anchor)
    window = source[max(0, idx - 1500) : idx + 1500]
    _assert_split_convention(
        window, where="run_agent_loop streaming-path LLM-call error handler"
    )


def test_non_streaming_path_llm_call_uses_split_handlers():
    """The non-streaming-path LLM call closes ``_gen_ctx`` and re-raises
    on the same convention.  Distinct from the streaming-path test
    above because the two sites bind the caught exception to different
    names and a regression could revert only one of them.
    """
    source = inspect.getsource(al.run_agent_loop)
    # Anchor on the non-streaming close call. The non-streaming site
    # binds the exception to ``_exc`` (the streaming site uses
    # ``_gen_exc``).
    anchor = "_end_aborted_generation(_gen_ctx, _exc, _usage_sink, _merge_usage)"
    assert anchor in source, (
        "expected to find the non-streaming-path generation close call "
        "(``_end_aborted_generation`` with the ``_exc`` exception name) "
        "inside run_agent_loop"
    )
    idx = source.find(anchor)
    window = source[max(0, idx - 1500) : idx + 1500]
    _assert_split_convention(
        window, where="run_agent_loop non-streaming-path LLM-call error handler"
    )


def test_iteration_span_finally_uses_split_handlers():
    """The per-iteration outer ``try/finally`` records ``_iter_error``
    and re-raises so the iteration span closes with the right error.
    Same convention as the LLM-call sites — splitting separates a real
    iteration failure from a process-terminating ``SystemExit`` /
    ``KeyboardInterrupt`` (which must not be intercepted by the
    iteration-span instrumentation).
    """
    source = inspect.getsource(al.run_agent_loop)
    # Anchor on the iteration-span error capture (the variable name is
    # unique to this site).
    anchor = "_iter_error = _exc"
    assert anchor in source, (
        "expected to find the iteration-span error capture "
        f"({anchor!r}) inside run_agent_loop"
    )
    idx = source.find(anchor)
    window = source[max(0, idx - 1500) : idx + 1500]
    _assert_split_convention(
        window, where="run_agent_loop iteration-span error handler"
    )
