# SPDX-License-Identifier: Apache-2.0

"""A utility answer cut at its output budget: the one check, and the one retry.

Every provider maps the truncation of its API onto the canonical
``ChatResponse.stop_reason`` value ``"max_tokens"`` (``chat_stream`` onto
``last_stop_reason``). A utility call - one outside the agentic loop, which
continues a cut answer itself - that parses or keeps a model answer never uses a
cut one as if it were complete. It checks with
:func:`answer_was_cut` (a stream with :class:`StreamStopReason`), asks once more
through :func:`chat_retrying_a_cut` with the budget
:func:`app.llm.model_limits.retry_output_budget` allows - twice its own, capped
at the model's output ceiling - and an answer still cut takes the path the
caller already takes when the call fails, or, where a person edits the text
before anything is kept, is handed back flagged as cut.

A decision read from the first token of an answer - a routing number, a yes or
a no - is complete once that token is there, whatever a cut takes off after it:
such a caller passes ``decided``, and only an answer cut before it carries a
decision is asked again.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from app.llm.base import (
    STREAM_STOP_REASON_UNRECORDED,
    forget_task_stream_stop_reason,
    task_stream_stop_reason,
)
from app.llm.model_limits import retry_output_budget

logger = logging.getLogger(__name__)

CUT_STOP_REASON = "max_tokens"


def answer_was_cut(response: object) -> bool:
    """True when *response* stopped at its output budget."""
    return getattr(response, "stop_reason", None) == CUT_STOP_REASON


class StreamStopReason:
    """How ONE ``chat_stream`` call ended, read by the task that iterates it.

    Built before the stream starts and read once it is exhausted. The router
    caches a provider and concurrent requests share it, so its
    ``last_stop_reason`` may already describe another request's stream; this
    reads what the provider recorded for THIS task
    (:func:`app.llm.base.task_stream_stop_reason`). An object that records
    nothing for the task (not a ``BaseLLMProvider``) is read through its
    ``last_stop_reason``.
    """

    __slots__ = ("_provider",)

    def __init__(self, provider: object) -> None:
        self._provider = provider
        forget_task_stream_stop_reason()

    def was_cut(self) -> bool:
        reason = task_stream_stop_reason()
        if reason is STREAM_STOP_REASON_UNRECORDED:
            reason = getattr(self._provider, "last_stop_reason", None)
        return reason == CUT_STOP_REASON


def still_cut(label: str) -> RuntimeError:
    """What a caller whose failed-call path is an exception raises for an
    answer still cut after the retry."""
    return RuntimeError(f"{label} answer still cut at max_tokens after a retry")


def cut_retry_budget(llm: object, model: str | None, max_tokens: int, *, label: str) -> int | None:
    """The budget to ask an answer cut at *max_tokens* again with, logged.

    ``None`` when the model's output ceiling leaves no more room than
    *max_tokens*: the retry would be the same request.
    """
    budget = retry_output_budget(llm, model, max_tokens)
    if budget <= max_tokens:
        logger.warning(
            "%s answer hit max_tokens=%d, the model's output ceiling - not retried",
            label,
            max_tokens,
        )
        return None
    logger.warning(
        "%s answer hit max_tokens=%d - retrying once with max_tokens=%d",
        label,
        max_tokens,
        budget,
    )
    return budget


def _cut_and_undecided[R](response: R, decided: Callable[[R], bool] | None) -> bool:
    return answer_was_cut(response) and not (decided is not None and decided(response))


async def chat_retrying_a_cut[R](
    llm: object,
    model: str | None,
    max_tokens: int,
    call: Callable[[int], Awaitable[R]],
    *,
    label: str,
    decided: Callable[[R], bool] | None = None,
) -> tuple[R, bool]:
    """Run ``call(max_tokens)``; a cut answer is asked once more.

    *call* performs the caller's own request with the budget it is given, so
    the caller keeps its messages, tracing and timeouts. Returns the last
    response and whether it is still cut. A cut answer *decided* accepts is
    used as it is. No retry is made when the model's output ceiling leaves no
    more room than *max_tokens*: it would be the same request. An exception
    from either call propagates: the caller's failed-call path handles it as it
    always has.
    """
    response = await call(max_tokens)
    if not _cut_and_undecided(response, decided):
        return response, False
    budget = cut_retry_budget(llm, model, max_tokens, label=label)
    if budget is None:
        return response, True
    response = await call(budget)
    return response, _cut_and_undecided(response, decided)
