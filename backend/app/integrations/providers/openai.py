# SPDX-License-Identifier: Apache-2.0

"""OpenAI direct-query integration.

Exposes OpenAI models to agents *as tools* so an agent can prompt a GPT model
directly, list available models, compare model outputs, evaluate/judge a piece
of text (LLM-as-a-judge), and run web-grounded queries via the Responses API
web search tool.

This is intentionally distinct from the platform LLM provider system (which
powers an agent's *own* reasoning). Here OpenAI is a *target* the agent queries
and evaluates - e.g. for brand monitoring ("what does GPT say about <brand>?")
and for grading or fact-checking another model's response.

API docs: https://platform.openai.com/docs/api-reference
Auth: API key sent as ``Authorization: Bearer <key>``.
Base URL: https://api.openai.com/v1
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from app.core.egress import egress_client
from app.integrations.http import format_entity_list, safe_request
from app.llm.model_capabilities import build_openai_generation_params
from app.tools.base import CallableToolExecutor, ToolDefinition

_PROVIDER = "OpenAI"
_BASE_URL = "https://api.openai.com/v1"
# Default model. The agent can override per call and discover the live
# catalogue via ``list_openai_models``.
_DEFAULT_MODEL = "gpt-4o"
_TIMEOUT = 120

_MAX_OUTPUT_TOKENS_CAP = 16384
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
        "Authorization": f"Bearer {api_key.strip()}",
        "Content-Type": "application/json",
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
    return max(0.0, min(2.0, t))


def _coerce_str_list(value: Any) -> list[str]:
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
    """Call the OpenAI API and return ``(data, None)`` or ``(None, error)``.

    When *client* is supplied it is reused (and left open for the caller to
    close) so a handler making several concurrent calls - e.g.
    ``compare_openai_models`` - shares one connection pool instead of
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


def _build_chat_payload(
    model: str,
    messages: list[dict],
    *,
    temperature: float | None,
    max_tokens: int,
) -> dict[str, Any]:
    payload: dict[str, Any] = {"model": model, "messages": messages}
    payload.update(
        build_openai_generation_params(
            model,
            temperature=temperature,
            max_tokens=max_tokens,
        )
    )
    return payload


def _chat_text(data: dict) -> str:
    choices = data.get("choices") or []
    if not choices:
        return ""
    message = (choices[0] or {}).get("message") or {}
    content = message.get("content")
    return content.strip() if isinstance(content, str) else ""


def _chat_footer(data: dict) -> str:
    usage = data.get("usage") or {}
    bits: list[str] = []
    if data.get("model"):
        bits.append(f"model={data['model']}")
    pt, ct = usage.get("prompt_tokens"), usage.get("completion_tokens")
    if pt or ct:
        bits.append(f"tokens in/out={pt or 0}/{ct or 0}")
    finish = ((data.get("choices") or [{}])[0] or {}).get("finish_reason")
    if finish == "length":
        bits.append("note: output truncated at the token limit")
    return f"\n\n[{_PROVIDER}: {', '.join(bits)}]" if bits else ""


def _format_chat_response(data: dict) -> str:
    return (_chat_text(data) or "(no text content returned)") + _chat_footer(data)


def _format_responses_web_search(data: dict) -> str:
    text_parts: list[str] = []
    sources: dict[str, str] = {}
    for item in data.get("output") or []:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        for block in item.get("content") or []:
            if not isinstance(block, dict) or block.get("type") != "output_text":
                continue
            if block.get("text"):
                text_parts.append(block["text"])
            for ann in block.get("annotations") or []:
                if isinstance(ann, dict) and ann.get("type") == "url_citation" and ann.get("url"):
                    sources.setdefault(ann["url"], ann.get("title") or ann["url"])
    answer = "".join(text_parts).strip() or "(no answer returned)"
    if sources:
        listed = list(sources.items())[:_MAX_SOURCES]
        answer += "\n\nSources:\n" + "\n".join(f"- {t} ({u})" for u, t in listed)
    usage = data.get("usage") or {}
    bits: list[str] = []
    if data.get("model"):
        bits.append(f"model={data['model']}")
    it, ot = usage.get("input_tokens"), usage.get("output_tokens")
    if it or ot:
        bits.append(f"tokens in/out={it or 0}/{ot or 0}")
    return answer + (f"\n\n[{_PROVIDER}: {', '.join(bits)}]" if bits else "")


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


def build_tools(
    token: str, executor: CallableToolExecutor, **_kwargs: Any
) -> list[ToolDefinition]:
    """Build OpenAI direct-query tools. ``token`` is the OpenAI API key."""
    api_key = (token or "").strip()

    # Schema-required args (prompt/content/criteria/models/query) still carry
    # Python defaults so that a tool call omitting them runs the in-handler
    # validation and returns a friendly "Error: '<field>' is required." instead
    # of raising a TypeError on the missing positional argument (the executor
    # forwards only the keys the model supplied).

    async def query_openai(
        prompt: str = "",
        model: str = "",
        system_prompt: str = "",
        temperature: float | None = None,
        max_tokens: int = _DEFAULT_MAX_TOKENS,
    ) -> str:
        if not (prompt or "").strip():
            return "Error: 'prompt' is required."
        model = (model or "").strip() or _DEFAULT_MODEL
        messages: list[dict] = []
        if (system_prompt or "").strip():
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})
        payload = _build_chat_payload(
            model,
            messages,
            temperature=_clamp_temperature(temperature),
            max_tokens=_clamp_int(max_tokens, _DEFAULT_MAX_TOKENS, 1, _MAX_OUTPUT_TOKENS_CAP),
        )
        data, err = await _request(
            api_key, "post", "/chat/completions", "query the model", json=payload
        )
        if err:
            return err
        return _format_chat_response(data)

    async def list_openai_models() -> str:
        data, err = await _request(api_key, "get", "/models", "list models")
        if err:
            return err
        models = sorted(
            data.get("data") or [], key=lambda m: m.get("id", ""), reverse=True
        )
        return format_entity_list(
            models,
            lambda m: f"- {m.get('id', '?')}",
            noun="OpenAI model",
            empty_msg="No OpenAI models available for this API key.",
        )

    async def evaluate_with_openai(
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
        model = (model or "").strip() or _DEFAULT_MODEL
        user = f"CRITERIA:\n{criteria}\n\n"
        if (reference or "").strip():
            user += f"REFERENCE / GROUND TRUTH:\n{reference}\n\n"
        user += f"CONTENT TO EVALUATE:\n{content}"
        messages = [
            {"role": "system", "content": _JUDGE_SYSTEM.format(scale=scale)},
            {"role": "user", "content": user},
        ]
        payload = _build_chat_payload(
            model, messages, temperature=0.0, max_tokens=_DEFAULT_MAX_TOKENS
        )
        data, err = await _request(
            api_key, "post", "/chat/completions", "evaluate content", json=payload
        )
        if err:
            return err
        return _format_chat_response(data)

    async def compare_openai_models(
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
            messages: list[dict] = []
            if (system_prompt or "").strip():
                messages.append({"role": "system", "content": system_prompt})
            messages.append({"role": "user", "content": prompt})
            payload = _build_chat_payload(
                model_id, messages, temperature=None, max_tokens=capped
            )
            # Constant action: model_id is user-supplied, so keep it out of the
            # request log safe_request emits; it is surfaced per-model below.
            data, err = await _request(
                api_key,
                "post",
                "/chat/completions",
                "query the model",
                json=payload,
                client=client,
            )
            return model_id, (err or (_chat_text(data) or "(no text content returned)"))

        # Reuse one AsyncClient across the concurrent per-model calls so the
        # comparison shares a single connection pool instead of opening a fresh
        # client/socket per model.
        async with egress_client(timeout=_TIMEOUT) as client:
            results = await asyncio.gather(*[_one(client, m) for m in model_list])
        blocks = [f"### {model_id}\n{answer}" for model_id, answer in results]
        return f"Compared {len(results)} OpenAI models on the same prompt:\n\n" + "\n\n".join(
            blocks
        )

    async def query_openai_web_search(
        query: str = "",
        model: str = "",
        max_tokens: int = _DEFAULT_MAX_TOKENS,
    ) -> str:
        if not (query or "").strip():
            return "Error: 'query' is required."
        payload: dict[str, Any] = {
            "model": (model or "").strip() or _DEFAULT_MODEL,
            "input": query,
            "tools": [{"type": "web_search"}],
            "max_output_tokens": _clamp_int(
                max_tokens, _DEFAULT_MAX_TOKENS, 16, _MAX_OUTPUT_TOKENS_CAP
            ),
        }
        data, err = await _request(
            api_key, "post", "/responses", "run a web-grounded query", json=payload
        )
        if err:
            return err
        return _format_responses_web_search(data)

    executor.register("query_openai", query_openai)
    executor.register("list_openai_models", list_openai_models)
    executor.register("evaluate_with_openai", evaluate_with_openai)
    executor.register("compare_openai_models", compare_openai_models)
    executor.register("query_openai_web_search", query_openai_web_search)

    return [
        ToolDefinition(
            name="query_openai",
            description=(
                "Send a prompt directly to an OpenAI model (e.g. GPT-4o) and return its "
                "raw answer. Use this to ask GPT what it knows or thinks about a topic "
                "(e.g. brand monitoring: 'What do you know about COMPANY?', 'Which "
                "CATEGORY vendors would you recommend?'), or to generate or transform "
                "text with GPT. This queries OpenAI as an external model - it is NOT this "
                "assistant's own model. Returns the model's text plus a token-usage footer."
            ),
            priority=0,
            input_schema={
                "type": "object",
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": "The user prompt / question to send to the model.",
                    },
                    "model": {
                        "type": "string",
                        "description": (
                            "OpenAI model id (e.g. 'gpt-4o'). Defaults to gpt-4o. Use "
                            "list_openai_models to discover available ids."
                        ),
                    },
                    "system_prompt": {
                        "type": "string",
                        "description": "Optional system instructions that steer the model.",
                    },
                    "temperature": {
                        "type": "number",
                        "description": (
                            "Optional sampling temperature 0.0-2.0 (applied only to "
                            "standard chat models; ignored for reasoning/GPT-5 models)."
                        ),
                    },
                    "max_tokens": {
                        "type": "integer",
                        "description": "Maximum output tokens (default 1024).",
                        "default": _DEFAULT_MAX_TOKENS,
                    },
                },
                "required": ["prompt"],
            },
            source="integration:openai",
        ),
        ToolDefinition(
            name="list_openai_models",
            description=(
                "List the models available to this OpenAI API key. Call this before "
                "query_openai / compare_openai_models when unsure which model id to target."
            ),
            priority=1,
            input_schema={"type": "object", "properties": {}, "required": []},
            source="integration:openai",
        ),
        ToolDefinition(
            name="evaluate_with_openai",
            description=(
                "Use an OpenAI model as an impartial judge (LLM-as-a-judge) to evaluate a "
                "piece of CONTENT against explicit CRITERIA, returning a structured SCORE "
                "/ VERDICT / REASONING. Ideal for grading another model's answer, scoring "
                "brand sentiment or accuracy, fact-checking, or rating tone/quality. Runs "
                "at temperature 0 for consistency."
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
                        "description": "OpenAI model id to judge with (optional).",
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
            source="integration:openai",
        ),
        ToolDefinition(
            name="compare_openai_models",
            description=(
                "Send the same prompt to several OpenAI models at once and return their "
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
                        "description": "List of OpenAI model ids to compare (max 5).",
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
            source="integration:openai",
        ),
        ToolDefinition(
            name="query_openai_web_search",
            description=(
                "Ask an OpenAI model a question with the built-in web search tool enabled "
                "(via the Responses API), so the answer is grounded in current web results "
                "and returns cited sources (title + URL). Best for up-to-date brand "
                "monitoring (recent news, reputation, what the live web says about a "
                "company/product) that the model's training data alone cannot answer."
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
                        "description": "OpenAI model id (optional; must support the web_search tool).",
                    },
                    "max_tokens": {
                        "type": "integer",
                        "description": "Maximum output tokens (default 1024).",
                        "default": _DEFAULT_MAX_TOKENS,
                    },
                },
                "required": ["query"],
            },
            source="integration:openai",
        ),
    ]
