# SPDX-License-Identifier: Apache-2.0

"""Anthropic (Claude) direct-query integration.

Exposes Anthropic's Claude models to agents *as tools* so an agent can prompt
Claude directly, list available models, compare model outputs, evaluate/judge a
piece of text with Claude (LLM-as-a-judge), and run web-grounded queries.

This is intentionally distinct from the platform LLM provider system (which
powers an agent's *own* reasoning). Here Claude is a *target* the agent queries
and evaluates - e.g. for brand monitoring ("what does Claude say about
<brand>?", "does Claude recommend us for <category>?") and for grading or
fact-checking another model's response.

API docs: https://docs.anthropic.com/en/api/messages
Auth: API key sent in the ``x-api-key`` header (plus ``anthropic-version``).
Base URL: https://api.anthropic.com/v1
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from app.core.egress import egress_client
from app.integrations.http import format_entity_list, safe_request
from app.tools.base import CallableToolExecutor, ToolDefinition

_PROVIDER = "Anthropic"
_BASE_URL = "https://api.anthropic.com/v1"
_API_VERSION = "2023-06-01"
# Default Claude model. The agent can override per call and discover the live
# catalogue via ``list_anthropic_models``.
_DEFAULT_MODEL = "claude-sonnet-5"
# Stable server-side web search tool version, supported across current Claude
# models. (Newer dynamic-filtering versions require the code-execution tool.)
_WEB_SEARCH_TOOL = "web_search_20250305"
_TIMEOUT = 120

_MAX_OUTPUT_TOKENS_CAP = 8192
_DEFAULT_MAX_TOKENS = 1024
_COMPARE_MODELS_CAP = 5
_MAX_SOURCES = 15

_JUDGE_SYSTEM = (
    "You are a meticulous, impartial evaluation judge. Assess the CONTENT "
    "strictly against the CRITERIA and output a concise structured verdict. "
    "Do not be swayed by length, fluency, or confident phrasing - judge only "
    "against the criteria. Respond in exactly this format:\n"
    "SCORE: a score on the {scale} scale\n"
    "VERDICT: a one-line pass/fail or summary judgement\n"
    "REASONING: 2-5 sentences citing specific evidence from the content"
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _headers(api_key: str) -> dict[str, str]:
    return {
        "x-api-key": api_key.strip(),
        "anthropic-version": _API_VERSION,
        "content-type": "application/json",
    }


def _clamp_int(value: Any, default: int, lo: int, hi: int) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


def _clamp_temperature(value: Any) -> float | None:
    if value is None:
        return None
    try:
        t = float(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, min(1.0, t))


def _coerce_str_list(value: Any) -> list[str]:
    """Coerce an LLM-supplied list/CSV string into a clean list of strings."""
    if isinstance(value, str):
        items = value.split(",")
    elif isinstance(value, (list, tuple)):
        items = value
    else:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        text = str(item).strip()
        if text and text not in seen:
            seen.add(text)
            out.append(text)
    return out


async def _request(
    api_key: str,
    method: str,
    path: str,
    action: str,
    *,
    json: dict | None = None,
    params: dict | None = None,
    client: httpx.AsyncClient | None = None,
) -> tuple[dict | None, str | None]:
    """Call the Anthropic API and return ``(data, None)`` or ``(None, error)``.

    When *client* is supplied it is reused (and left open for the caller to
    close) so a handler making several concurrent calls - e.g.
    ``compare_anthropic_models`` - shares one connection pool instead of
    spinning up a fresh ``AsyncClient`` per request. Without it a short-lived
    client is created for the single call.
    """
    if not (api_key or "").strip():
        return None, f"Error: {_PROVIDER} API key is missing. Configure the integration."

    async def _send(c: httpx.AsyncClient) -> tuple[dict | None, str | None]:
        resp, err = await safe_request(
            c,
            method,
            f"{_BASE_URL}{path}",
            _PROVIDER,
            action,
            headers=_headers(api_key),
            json=json,
            params=params,
        )
        if err:
            return None, err
        try:
            return resp.json(), None
        except ValueError:
            return None, f"Error: {_PROVIDER} returned a non-JSON response while trying to {action}."

    if client is not None:
        return await _send(client)
    async with egress_client(timeout=_TIMEOUT) as owned:
        return await _send(owned)


def _extract_text(data: dict) -> str:
    parts = [
        b.get("text", "")
        for b in (data.get("content") or [])
        if isinstance(b, dict) and b.get("type") == "text"
    ]
    return "".join(parts).strip()


def _usage_footer(data: dict) -> str:
    usage = data.get("usage") or {}
    bits: list[str] = []
    if data.get("model"):
        bits.append(f"model={data['model']}")
    in_tok, out_tok = usage.get("input_tokens"), usage.get("output_tokens")
    if in_tok or out_tok:
        bits.append(f"tokens in/out={in_tok or 0}/{out_tok or 0}")
    if data.get("stop_reason") == "max_tokens":
        bits.append("note: output truncated at max_tokens")
    return f"\n\n[{_PROVIDER}: {', '.join(bits)}]" if bits else ""


def _format_query_response(data: dict) -> str:
    text = _extract_text(data) or "(no text content returned)"
    return text + _usage_footer(data)


def _format_web_search_response(data: dict) -> str:
    text_parts: list[str] = []
    sources: dict[str, str] = {}
    for block in data.get("content") or []:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            if block.get("text"):
                text_parts.append(block["text"])
            for cite in block.get("citations") or []:
                if isinstance(cite, dict) and cite.get("url"):
                    sources.setdefault(cite["url"], cite.get("title") or cite["url"])
        elif btype == "web_search_tool_result":
            for result in block.get("content") or []:
                if isinstance(result, dict) and result.get("url"):
                    sources.setdefault(
                        result["url"], result.get("title") or result["url"]
                    )
    answer = "".join(text_parts).strip() or "(no answer returned)"
    if sources:
        listed = list(sources.items())[:_MAX_SOURCES]
        answer += "\n\nSources:\n" + "\n".join(f"- {t} ({u})" for u, t in listed)
    return answer + _usage_footer(data)


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


def build_tools(
    token: str, executor: CallableToolExecutor, **_kwargs: Any
) -> list[ToolDefinition]:
    """Build Anthropic direct-query tools. ``token`` is the Anthropic API key."""
    api_key = (token or "").strip()

    # Handlers default every schema-"required" argument so a malformed tool
    # call that omits one returns the friendly in-handler validation message
    # instead of a TypeError that the executor would surface as a raw
    # exception string (mirrors the defensive contract in CallableToolExecutor).

    async def query_anthropic(
        prompt: str = "",
        model: str = "",
        system_prompt: str = "",
        temperature: float | None = None,
        max_tokens: int = _DEFAULT_MAX_TOKENS,
    ) -> str:
        if not (prompt or "").strip():
            return "Error: 'prompt' is required."
        payload: dict[str, Any] = {
            "model": (model or "").strip() or _DEFAULT_MODEL,
            "max_tokens": _clamp_int(max_tokens, _DEFAULT_MAX_TOKENS, 1, _MAX_OUTPUT_TOKENS_CAP),
            "messages": [{"role": "user", "content": prompt}],
        }
        if (system_prompt or "").strip():
            payload["system"] = system_prompt
        temp = _clamp_temperature(temperature)
        if temp is not None:
            payload["temperature"] = temp
        data, err = await _request(api_key, "post", "/messages", "query Claude", json=payload)
        if err:
            return err
        return _format_query_response(data)

    async def list_anthropic_models() -> str:
        data, err = await _request(
            api_key, "get", "/models", "list models", params={"limit": 100}
        )
        if err:
            return err

        def _fmt(m: dict) -> str:
            label = m.get("display_name")
            return f"- {m.get('id', '?')}" + (f" ({label})" if label else "")

        return format_entity_list(
            data.get("data") or [], _fmt, noun="Claude model",
            empty_msg="No Claude models available for this API key.",
        )

    async def evaluate_with_anthropic(
        content: str = "",
        criteria: str = "",
        model: str = "",
        scale: str = "1-10",
        reference: str = "",
    ) -> str:
        if not (content or "").strip():
            return "Error: 'content' (the text to evaluate) is required."
        if not (criteria or "").strip():
            return "Error: 'criteria' (what to assess) is required."
        scale = (scale or "1-10").strip()
        user = f"CRITERIA:\n{criteria}\n\n"
        if (reference or "").strip():
            user += f"REFERENCE / GROUND TRUTH:\n{reference}\n\n"
        user += f"CONTENT TO EVALUATE:\n{content}"
        payload: dict[str, Any] = {
            "model": (model or "").strip() or _DEFAULT_MODEL,
            "max_tokens": _DEFAULT_MAX_TOKENS,
            "temperature": 0,
            "system": _JUDGE_SYSTEM.format(scale=scale),
            "messages": [{"role": "user", "content": user}],
        }
        data, err = await _request(
            api_key, "post", "/messages", "evaluate content with Claude", json=payload
        )
        if err:
            return err
        return _format_query_response(data)

    async def compare_anthropic_models(
        prompt: str = "",
        models: list[str] | None = None,
        system_prompt: str = "",
        max_tokens: int = 512,
    ) -> str:
        if not (prompt or "").strip():
            return "Error: 'prompt' is required."
        model_list = _coerce_str_list(models)[:_COMPARE_MODELS_CAP]
        if not model_list:
            return "Error: provide at least one model id in 'models'."
        capped = _clamp_int(max_tokens, 512, 1, _MAX_OUTPUT_TOKENS_CAP)

        async def _one(client: httpx.AsyncClient, model_id: str) -> tuple[str, str]:
            payload: dict[str, Any] = {
                "model": model_id,
                "max_tokens": capped,
                "messages": [{"role": "user", "content": prompt}],
            }
            if (system_prompt or "").strip():
                payload["system"] = system_prompt
            data, err = await _request(
                api_key,
                "post",
                "/messages",
                f"query {model_id}",
                json=payload,
                client=client,
            )
            return model_id, (err or (_extract_text(data) or "(no text content returned)"))

        # Reuse one AsyncClient across the concurrent per-model calls so the
        # comparison shares a single connection pool instead of opening a fresh
        # client/socket per model.
        async with egress_client(timeout=_TIMEOUT) as client:
            results = await asyncio.gather(*[_one(client, m) for m in model_list])
        blocks = [f"### {model_id}\n{answer}" for model_id, answer in results]
        return f"Compared {len(results)} Claude models on the same prompt:\n\n" + "\n\n".join(
            blocks
        )

    async def query_anthropic_web_search(
        query: str = "",
        model: str = "",
        max_searches: int = 5,
        max_tokens: int = _DEFAULT_MAX_TOKENS,
    ) -> str:
        if not (query or "").strip():
            return "Error: 'query' is required."
        payload: dict[str, Any] = {
            "model": (model or "").strip() or _DEFAULT_MODEL,
            "max_tokens": _clamp_int(max_tokens, _DEFAULT_MAX_TOKENS, 1, _MAX_OUTPUT_TOKENS_CAP),
            "messages": [{"role": "user", "content": query}],
            "tools": [
                {
                    "type": _WEB_SEARCH_TOOL,
                    "name": "web_search",
                    "max_uses": _clamp_int(max_searches, 5, 1, 10),
                }
            ],
        }
        data, err = await _request(
            api_key, "post", "/messages", "run a web-grounded Claude query", json=payload
        )
        if err:
            return err
        return _format_web_search_response(data)

    executor.register("query_anthropic", query_anthropic)
    executor.register("list_anthropic_models", list_anthropic_models)
    executor.register("evaluate_with_anthropic", evaluate_with_anthropic)
    executor.register("compare_anthropic_models", compare_anthropic_models)
    executor.register("query_anthropic_web_search", query_anthropic_web_search)

    return [
        ToolDefinition(
            name="query_anthropic",
            description=(
                "Send a prompt directly to an Anthropic Claude model and return its raw "
                "answer. Use this to ask Claude what it knows or thinks about a topic "
                "(e.g. brand monitoring: 'What do you know about COMPANY?', 'Which "
                "CATEGORY vendors would you recommend?'), or to generate or transform "
                "text with Claude. This queries Claude as an external model - it is NOT "
                "this assistant's own model. Returns the model's text plus a token-usage "
                "footer."
            ),
            priority=0,
            input_schema={
                "type": "object",
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": "The user prompt / question to send to Claude.",
                    },
                    "model": {
                        "type": "string",
                        "description": (
                            "Claude model id (e.g. 'claude-sonnet-5'). "
                            "Defaults to a current Claude Sonnet. Use list_anthropic_models "
                            "to discover available ids."
                        ),
                    },
                    "system_prompt": {
                        "type": "string",
                        "description": "Optional system instructions that steer Claude's behaviour.",
                    },
                    "temperature": {
                        "type": "number",
                        "description": "Optional sampling temperature 0.0-1.0. Omit to use the model default.",
                    },
                    "max_tokens": {
                        "type": "integer",
                        "description": "Maximum output tokens (1-8192, default 1024).",
                        "default": _DEFAULT_MAX_TOKENS,
                    },
                },
                "required": ["prompt"],
            },
            source="integration:anthropic",
        ),
        ToolDefinition(
            name="list_anthropic_models",
            description=(
                "List the Claude models available to this Anthropic API key, with their "
                "ids and display names. Call this before query_anthropic / "
                "compare_anthropic_models when you are unsure which model id to target."
            ),
            priority=1,
            input_schema={"type": "object", "properties": {}, "required": []},
            source="integration:anthropic",
        ),
        ToolDefinition(
            name="evaluate_with_anthropic",
            description=(
                "Use Claude as an impartial judge (LLM-as-a-judge) to evaluate a piece of "
                "CONTENT against explicit CRITERIA, returning a structured SCORE / VERDICT "
                "/ REASONING. Ideal for grading another model's answer, scoring brand "
                "sentiment or accuracy, fact-checking, or rating tone/quality. Runs at "
                "temperature 0 for consistency."
            ),
            priority=0,
            input_schema={
                "type": "object",
                "properties": {
                    "content": {
                        "type": "string",
                        "description": "The text/answer to evaluate.",
                    },
                    "criteria": {
                        "type": "string",
                        "description": (
                            "What to assess, e.g. 'accuracy and brand sentiment toward "
                            "Acme Corp' or 'does it follow the style guide?'."
                        ),
                    },
                    "model": {
                        "type": "string",
                        "description": "Claude model id to judge with (optional).",
                    },
                    "scale": {
                        "type": "string",
                        "description": "Scoring scale to apply (default '1-10').",
                        "default": "1-10",
                    },
                    "reference": {
                        "type": "string",
                        "description": "Optional ground-truth / reference answer to judge against.",
                    },
                },
                "required": ["content", "criteria"],
            },
            source="integration:anthropic",
        ),
        ToolDefinition(
            name="compare_anthropic_models",
            description=(
                "Send the same prompt to several Claude models at once and return their "
                "answers side by side. Useful for spotting differences between model "
                "versions (e.g. whether each model mentions a brand, or how answers drift "
                "across versions). Up to 5 models per call."
            ),
            priority=1,
            input_schema={
                "type": "object",
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": "The prompt to send to every model.",
                    },
                    "models": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of Claude model ids to compare (max 5).",
                    },
                    "system_prompt": {
                        "type": "string",
                        "description": "Optional system instructions applied to every model.",
                    },
                    "max_tokens": {
                        "type": "integer",
                        "description": "Max output tokens per model (default 512).",
                        "default": 512,
                    },
                },
                "required": ["prompt", "models"],
            },
            source="integration:anthropic",
        ),
        ToolDefinition(
            name="query_anthropic_web_search",
            description=(
                "Ask Claude a question with Anthropic's server-side web search enabled, so "
                "the answer is grounded in current web results and returns cited sources "
                "(title + URL). Best for up-to-date brand monitoring (recent news, "
                "reputation, what the live web says about a company/product) that the "
                "model's training data alone cannot answer."
            ),
            priority=0,
            input_schema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The question to answer using live web search.",
                    },
                    "model": {
                        "type": "string",
                        "description": "Claude model id (optional; must support web search).",
                    },
                    "max_searches": {
                        "type": "integer",
                        "description": "Maximum number of web searches Claude may run (1-10, default 5).",
                        "default": 5,
                    },
                    "max_tokens": {
                        "type": "integer",
                        "description": "Maximum output tokens (default 1024).",
                        "default": _DEFAULT_MAX_TOKENS,
                    },
                },
                "required": ["query"],
            },
            source="integration:anthropic",
        ),
    ]
