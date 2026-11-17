# SPDX-License-Identifier: Apache-2.0

"""Which provider config served an LLM call, as recorded on traces.

``provider_name`` is the provider TYPE: two ``filigran_gateway`` rows produce
identical traces, so a trace could not say which credentials, key or budget
served a request. The config id is the only thing that identifies one
(``display_name`` is optional and mostly unset).

``llm_provider`` stays the type it always was (pricing resolves costs from it);
the keys below are added next to it.
"""

from __future__ import annotations

import logging
from typing import Any

from app.core.utils import format_exception_message

logger = logging.getLogger(__name__)

TRACE_CONFIG_ID_KEY = "llm_provider_config_id"
TRACE_CONFIG_LABEL_KEY = "llm_provider_config_label"
# Fallback configs that served at least one call of the trace, in order.
TRACE_FAILOVER_KEY = "llm_provider_failover"
# The budget-driven model downgrade of the run, when one happened.
TRACE_DOWNGRADE_KEY = "llm_model_downgrade"

# Kept in the trace context, never in its metadata: the failover wrapper whose
# fallback the trace names, and the config id it wrote there.
_FAILOVER_OWNER_KEY = "_llm_failover_owner"


def _clean(value: object) -> str | None:
    """*value* stripped, or ``None`` when it is not a string or is blank."""
    if not isinstance(value, str):
        return None
    return value.strip() or None


def config_identity(config: Any) -> tuple[str | None, str | None]:
    """``(id, label)`` naming an ``LLMProviderConfig`` row: the id as a string,
    the display name or ``None`` (never an empty or blank label).

    What ``build_provider()`` stamps on the instance it builds from the row.
    """
    config_id = getattr(config, "id", None)
    return (
        _clean(str(config_id)) if config_id else None,
        _clean(getattr(config, "display_name", None)),
    )


def _provider_identity(provider: Any) -> tuple[str | None, str | None]:
    return (
        _clean(getattr(provider, "provider_config_id", None)),
        _clean(getattr(provider, "provider_config_label", None)),
    )


def _identity_metadata(config_id: str | None, label: str | None) -> dict[str, str]:
    if config_id is None:
        return {}
    meta = {TRACE_CONFIG_ID_KEY: config_id}
    if label is not None:
        meta[TRACE_CONFIG_LABEL_KEY] = label
    return meta


def provider_config_metadata(provider: Any) -> dict[str, str]:
    """Trace metadata naming the config *provider* was built from.

    Empty when the provider carries no config (built directly, a test stub):
    an absent id is honest, a guessed one is not.
    """
    return _identity_metadata(*_provider_identity(provider))


def config_metadata(config: Any) -> dict[str, str]:
    """The same keys for a config row used without a provider instance (the
    Anthropic pass-through calls the API with the row's key directly)."""
    return _identity_metadata(*config_identity(config))


def _current_trace() -> dict | None:
    """The trace the running model call belongs to: its root, never the
    iteration or tool span the agent loop made current around the call."""
    from app.services.langfuse_tracing import get_current_trace_ctx, trace_root_ctx

    return trace_root_ctx(get_current_trace_ctx())


def record_failover_served(provider: Any, *, owner: object) -> None:
    """Point the current trace at the fallback config that just served a call.

    The trace was opened naming the primary; after a failover that name would
    be confidently wrong, so the config keys are rewritten to the config that
    actually answered (cleared when it carries none) and the fallback is
    appended to the trace's failover record. *owner* is the failover wrapper:
    it alone may point the trace back at its primary (``record_primary_served``).
    Idempotent: recording the fallback the trace already names changes
    nothing. Never raises into the call path.
    """
    try:
        from app.services.langfuse_tracing import enrich_trace_metadata

        ctx = _current_trace()
        if not ctx:
            return
        config_id, label = _provider_identity(provider)
        served = {
            "provider": getattr(provider, "provider_name", None),
            "config_id": config_id,
            "label": label,
        }
        history = list((ctx.get("metadata") or {}).get(TRACE_FAILOVER_KEY) or [])
        if served not in history:
            history.append(served)
        enrich_trace_metadata(
            ctx,
            {
                TRACE_CONFIG_ID_KEY: config_id,
                TRACE_CONFIG_LABEL_KEY: label,
                TRACE_FAILOVER_KEY: history,
            },
        )
        ctx[_FAILOVER_OWNER_KEY] = (owner, config_id)
    except Exception as exc:  # pragma: no cover - tracing must never break a call
        logger.debug(
            "Could not record the failover provider on the trace: %s",
            format_exception_message(exc),
        )


def record_primary_served(owner: object, primary: Any) -> None:
    """The primary of the failover wrapper *owner* served a call.

    A trace spans many model calls and the wrapper tries its primary first on
    every one, so after a transient failure the primary serves again: when the
    trace still names the fallback *owner* moved it to, it is pointed back at
    the primary (the fallback stays in ``TRACE_FAILOVER_KEY``). A trace another
    writer re-pointed since (a budget downgrade, another wrapper's failover) is
    left alone. Never raises into the call path.
    """
    try:
        from app.services.langfuse_tracing import enrich_trace_metadata

        ctx = _current_trace()
        if not ctx:
            return
        mark = ctx.get(_FAILOVER_OWNER_KEY)
        if not mark or mark[0] is not owner:
            return
        del ctx[_FAILOVER_OWNER_KEY]
        if (ctx.get("metadata") or {}).get(TRACE_CONFIG_ID_KEY) != mark[1]:
            return
        config_id, label = _provider_identity(primary)
        enrich_trace_metadata(
            ctx, {TRACE_CONFIG_ID_KEY: config_id, TRACE_CONFIG_LABEL_KEY: label}
        )
    except Exception as exc:  # pragma: no cover - tracing must never break a call
        logger.debug(
            "Could not record the recovered primary on the trace: %s",
            format_exception_message(exc),
        )


def record_model_downgrade(provider: Any, *, from_model: str | None, to_model: str) -> None:
    """Point the current trace at the model and config a budget downgrade
    moved the run to.

    The rest of the run is served there, so the config keys and the trace
    model name it from now on, and the switch is recorded under
    ``TRACE_DOWNGRADE_KEY``: without it a trace that ends on a cheaper model
    reads as if the model changed on its own. ``llm_provider`` stays the type
    the run started on, as after a failover. Never raises into the run.
    """
    try:
        from app.services.langfuse_tracing import enrich_trace_metadata, set_trace_model

        ctx = _current_trace()
        if not ctx:
            return
        config_id, label = _provider_identity(provider)
        # The downgrade names the config from now on: a recovered failover
        # primary must not point the trace back at itself.
        ctx.pop(_FAILOVER_OWNER_KEY, None)
        enrich_trace_metadata(
            ctx,
            {
                "model": to_model,
                TRACE_CONFIG_ID_KEY: config_id,
                TRACE_CONFIG_LABEL_KEY: label,
                TRACE_DOWNGRADE_KEY: {
                    "from_model": from_model,
                    "to_model": to_model,
                    "provider": getattr(provider, "provider_name", None),
                    "config_id": config_id,
                    "label": label,
                },
            },
        )
        set_trace_model(ctx, to_model)
    except Exception as exc:  # pragma: no cover - tracing must never break a run
        logger.debug(
            "Could not record the model downgrade on the trace: %s",
            format_exception_message(exc),
        )
