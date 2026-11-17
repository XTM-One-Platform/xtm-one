# SPDX-License-Identifier: Apache-2.0

"""Google Gemini direct-query integration.

Exposes Google's Gemini models to agents *as tools* so an agent can prompt a
Gemini model directly, list available models, compare model outputs,
evaluate/judge a piece of text (LLM-as-a-judge), and run web-grounded queries
using Google Search grounding.

This is intentionally distinct from the platform LLM provider system (which
powers an agent's *own* reasoning). Here Gemini is a *target* the agent queries
and evaluates - e.g. for brand monitoring ("what does Gemini say about
<brand>?") and for grading or fact-checking another model's response.

API docs: https://ai.google.dev/gemini-api/docs
Auth: API key sent in the ``x-goog-api-key`` header.
Base URL: https://generativelanguage.googleapis.com/v1beta
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

import httpx

from app.core.egress import egress_client
from app.integrations.http import format_entity_list, safe_request
from app.tools.base import CallableToolExecutor, ToolDefinition

_PROVIDER = "Google Gemini"
_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
# Default model. The agent can override per call and discover the live
# catalogue via ``list_gemini_models``.
_DEFAULT_MODEL = "gemini-2.5-flash"
_TIMEOUT = 120

# ``gemini-2.5-*`` (the default model) are "thinking" models: their internal
# reasoning tokens are counted against ``maxOutputTokens``. If the budget is too
# small the reasoning can consume all of it and the API returns an empty
# candidate with ``finishReason=MAX_TOKENS`` (handled in ``_no_text_reason``).
# Keep the cap at the model maximum and the defaults generous enough to leave
# room for both the reasoning and a useful answer on the default thinking model.
_MAX_OUTPUT_TOKENS_CAP = 65536
_DEFAULT_MAX_TOKENS = 2048
_COMPARE_MODELS_CAP = 5
_COMPARE_DEFAULT_MAX_TOKENS = 1024
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
    return {"x-goog-api-key": api_key.strip(), "Content-Type": "application/json"}


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


# A Gemini model id is a single path segment of ``[A-Za-z0-9._-]`` (e.g.
# ``gemini-2.5-flash``, ``gemini-1.5-pro-002``). The id is interpolated into the
# request path (``/models/{id}:generateContent``), so anything beyond this set -
# ``/``, ``:``, ``?``, ``#`` or whitespace - could redirect the call to a
# different Gemini endpoint and must be stripped before use.
_MODEL_ID_RE = re.compile(r"[A-Za-z0-9._-]+")


def _normalize_model(model: str) -> str:
    """Accept ids with or without the ``models/`` prefix; return a single,
    URL-safe bare id, falling back to the default for empty / malformed input.

    The id ends up as a path segment in ``/models/{id}:generateContent``. A
    crafted ``model`` such as ``gemini-x:batchGenerateContent`` or ``../foo``
    could otherwise alter the effective path, so only the leading
    ``[A-Za-z0-9._-]`` run is kept and all-dot ids (``""``, ``..``) collapse to
    the default.
    """
    model = (model or "").strip()
    if model.startswith("models/"):
        model = model[len("models/") :].strip()
    match = _MODEL_ID_RE.match(model)
    candidate = match.group(0) if match else ""
    if not candidate or set(candidate) <= {"."}:
        return _DEFAULT_MODEL
    return candidate


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
    """Call the Gemini API and return ``(data, None)`` or ``(None, error)``.

    When *client* is supplied it is reused (and left open for the caller to
    close) so a handler making several concurrent calls - e.g.
    ``compare_gemini_models`` - shares one connection pool instead of
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


def _build_payload(
    prompt: str,
    *,
    system: str = "",
    temperature: float | None = None,
    max_tokens: int | None = None,
    tools: list[dict] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}]
    }
    if (system or "").strip():
        payload["systemInstruction"] = {"parts": [{"text": system}]}
    gen: dict[str, Any] = {}
    if temperature is not None:
        gen["temperature"] = temperature
    if max_tokens:
        gen["maxOutputTokens"] = max_tokens
    if gen:
        payload["generationConfig"] = gen
    if tools:
        payload["tools"] = tools
    return payload


def _extract_text(data: dict) -> str:
    candidates = data.get("candidates") or []
    if not candidates:
        return ""
    parts = ((candidates[0] or {}).get("content") or {}).get("parts") or []
    return "".join(
        p.get("text", "") for p in parts if isinstance(p, dict)
    ).strip()


def _no_text_reason(data: dict) -> str:
    candidates = data.get("candidates") or []
    finish = (candidates[0] or {}).get("finishReason") if candidates else None
    block = (data.get("promptFeedback") or {}).get("blockReason")
    if block:
        return f"(no answer - the prompt was blocked: {block})"
    if finish == "MAX_TOKENS":
        # Empty text + MAX_TOKENS means the whole budget was spent before any
        # visible output. On Gemini 2.5 thinking models the internal reasoning
        # counts against max_tokens, so this is almost always a budget problem
        # rather than the model having nothing to say - give the agent an
        # actionable, self-correcting signal instead of a silent empty result.
        return (
            "(no answer - the max_tokens budget was exhausted before any "
            "visible text was produced. Gemini 2.5 'thinking' models count "
            "internal reasoning toward max_tokens; retry with a larger "
            "max_tokens.)"
        )
    if finish and finish != "STOP":
        return f"(no answer - generation stopped: {finish})"
    return "(no text content returned)"


def _footer(data: dict, model: str) -> str:
    usage = data.get("usageMetadata") or {}
    bits = [f"model={model}"]
    pt, ct = usage.get("promptTokenCount"), usage.get("candidatesTokenCount")
    if pt or ct:
        bits.append(f"tokens in/out={pt or 0}/{ct or 0}")
    candidates = data.get("candidates") or []
    if candidates and (candidates[0] or {}).get("finishReason") == "MAX_TOKENS":
        bits.append("note: output truncated at maxOutputTokens")
    return f"\n\n[{_PROVIDER}: {', '.join(bits)}]"


def _format_response(data: dict, model: str) -> str:
    return (_extract_text(data) or _no_text_reason(data)) + _footer(data, model)


def _format_grounded_response(data: dict, model: str) -> str:
    answer = _extract_text(data) or _no_text_reason(data)
    candidates = data.get("candidates") or []
    meta = (candidates[0] or {}).get("groundingMetadata") or {} if candidates else {}
    sources: dict[str, str] = {}
    for chunk in meta.get("groundingChunks") or []:
        web = chunk.get("web") if isinstance(chunk, dict) else None
        if isinstance(web, dict) and web.get("uri"):
            sources.setdefault(web["uri"], web.get("title") or web["uri"])
    queries = [q for q in (meta.get("webSearchQueries") or []) if isinstance(q, str)]
    if queries:
        answer += "\n\nSearches run: " + "; ".join(queries[:_MAX_SOURCES])
    if sources:
        listed = list(sources.items())[:_MAX_SOURCES]
        answer += "\n\nSources:\n" + "\n".join(f"- {t} ({u})" for u, t in listed)
    return answer + _footer(data, model)


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


def build_tools(
    token: str, executor: CallableToolExecutor, **_kwargs: Any
) -> list[ToolDefinition]:
    """Build Google Gemini direct-query tools. ``token`` is the Gemini API key."""
    api_key = (token or "").strip()

    # Schema-required args (prompt/content/criteria/models/query) still carry
    # Python defaults so that a tool call omitting them runs the in-handler
    # validation and returns a friendly "Error: '<field>' is required." instead
    # of raising a TypeError on the missing positional argument (the executor
    # forwards only the keys the model supplied).

    async def query_gemini(
        prompt: str = "",
        model: str = "",
        system_prompt: str = "",
        temperature: float | None = None,
        max_tokens: int = _DEFAULT_MAX_TOKENS,
    ) -> str:
        if not (prompt or "").strip():
            return "Error: 'prompt' is required."
        model_id = _normalize_model(model)
        payload = _build_payload(
            prompt,
            system=system_prompt,
            temperature=_clamp_temperature(temperature),
            max_tokens=_clamp_int(max_tokens, _DEFAULT_MAX_TOKENS, 1, _MAX_OUTPUT_TOKENS_CAP),
        )
        data, err = await _request(
            api_key,
            "post",
            f"/models/{model_id}:generateContent",
            "query Gemini",
            json=payload,
        )
        if err:
            return err
        return _format_response(data, model_id)

    async def list_gemini_models() -> str:
        data, err = await _request(
            api_key, "get", "/models", "list models", params={"pageSize": 1000}
        )
        if err:
            return err
        models = [
            m
            for m in (data.get("models") or [])
            if "generateContent" in (m.get("supportedGenerationMethods") or [])
        ]

        def _fmt(m: dict) -> str:
            name = (m.get("name") or "").removeprefix("models/") or "?"
            label = m.get("displayName")
            return f"- {name}" + (f" ({label})" if label and label != name else "")

        return format_entity_list(
            models, _fmt, noun="Gemini model",
            empty_msg="No Gemini models available for this API key.",
        )

    async def evaluate_with_gemini(
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
        model_id = _normalize_model(model)
        user = f"CRITERIA:\n{criteria}\n\n"
        if (reference or "").strip():
            user += f"REFERENCE / GROUND TRUTH:\n{reference}\n\n"
        user += f"CONTENT TO EVALUATE:\n{content}"
        payload = _build_payload(
            user,
            system=_JUDGE_SYSTEM.format(scale=scale),
            temperature=0.0,
            max_tokens=_DEFAULT_MAX_TOKENS,
        )
        data, err = await _request(
            api_key,
            "post",
            f"/models/{model_id}:generateContent",
            "evaluate content with Gemini",
            json=payload,
        )
        if err:
            return err
        return _format_response(data, model_id)

    async def compare_gemini_models(
        prompt: str = "",
        models: list[str] | None = None,
        system_prompt: str = "",
        max_tokens: int = _COMPARE_DEFAULT_MAX_TOKENS,
    ) -> str:
        if not (prompt or "").strip():
            return "Error: 'prompt' is required."
        model_list = _coerce_str_list(models)[:_COMPARE_MODELS_CAP]
        if not model_list:
            return "Error: provide at least one model id in 'models'."
        capped = _clamp_int(
            max_tokens, _COMPARE_DEFAULT_MAX_TOKENS, 1, _MAX_OUTPUT_TOKENS_CAP
        )

        async def _one(client: httpx.AsyncClient, model_id: str) -> tuple[str, str]:
            bare = _normalize_model(model_id)
            payload = _build_payload(
                prompt, system=system_prompt, max_tokens=capped
            )
            # Constant action: the model id is user-supplied, so keep it out of
            # the request log safe_request emits; it is surfaced per-model below.
            data, err = await _request(
                api_key,
                "post",
                f"/models/{bare}:generateContent",
                "query the model",
                json=payload,
                client=client,
            )
            return bare, (err or (_extract_text(data) or _no_text_reason(data)))

        # Reuse one AsyncClient across the concurrent per-model calls so the
        # comparison shares a single connection pool instead of opening a fresh
        # client/socket per model.
        async with egress_client(timeout=_TIMEOUT) as client:
            results = await asyncio.gather(*[_one(client, m) for m in model_list])
        blocks = [f"### {model_id}\n{answer}" for model_id, answer in results]
        return f"Compared {len(results)} Gemini models on the same prompt:\n\n" + "\n\n".join(
            blocks
        )

    async def query_gemini_grounded(
        query: str = "",
        model: str = "",
        max_tokens: int = _DEFAULT_MAX_TOKENS,
    ) -> str:
        if not (query or "").strip():
            return "Error: 'query' is required."
        model_id = _normalize_model(model)
        payload = _build_payload(
            query,
            max_tokens=_clamp_int(max_tokens, _DEFAULT_MAX_TOKENS, 1, _MAX_OUTPUT_TOKENS_CAP),
            tools=[{"google_search": {}}],
        )
        data, err = await _request(
            api_key,
            "post",
            f"/models/{model_id}:generateContent",
            "run a Google-Search-grounded Gemini query",
            json=payload,
        )
        if err:
            return err
        return _format_grounded_response(data, model_id)

    executor.register("query_gemini", query_gemini)
    executor.register("list_gemini_models", list_gemini_models)
    executor.register("evaluate_with_gemini", evaluate_with_gemini)
    executor.register("compare_gemini_models", compare_gemini_models)
    executor.register("query_gemini_grounded", query_gemini_grounded)

    return [
        ToolDefinition(
            name="query_gemini",
            description=(
                "Send a prompt directly to a Google Gemini model and return its raw "
                "answer. Use this to ask Gemini what it knows or thinks about a topic "
                "(e.g. brand monitoring: 'What do you know about COMPANY?', 'Which "
                "CATEGORY vendors would you recommend?'), or to generate or transform "
                "text with Gemini. This queries Gemini as an external model - it is NOT "
                "this assistant's own model. Returns the model's text plus a token-usage "
                "footer."
            ),
            priority=0,
            input_schema={
                "type": "object",
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": "The user prompt / question to send to Gemini.",
                    },
                    "model": {
                        "type": "string",
                        "description": (
                            "Gemini model id (e.g. 'gemini-2.5-flash'). Defaults to a "
                            "current Gemini Flash. Use list_gemini_models to discover ids."
                        ),
                    },
                    "system_prompt": {
                        "type": "string",
                        "description": "Optional system instructions that steer Gemini's behaviour.",
                    },
                    "temperature": {
                        "type": "number",
                        "description": "Optional sampling temperature 0.0-2.0. Omit to use the model default.",
                    },
                    "max_tokens": {
                        "type": "integer",
                        "description": (
                            "Maximum output tokens (1-65536, default 2048). On "
                            "Gemini 2.5 'thinking' models this budget also covers "
                            "the model's internal reasoning, so keep it generous."
                        ),
                        "default": _DEFAULT_MAX_TOKENS,
                    },
                },
                "required": ["prompt"],
            },
            source="integration:gemini",
        ),
        ToolDefinition(
            name="list_gemini_models",
            description=(
                "List the Gemini models available to this API key that support content "
                "generation. Call this before query_gemini / compare_gemini_models when "
                "unsure which model id to target."
            ),
            priority=1,
            input_schema={"type": "object", "properties": {}, "required": []},
            source="integration:gemini",
        ),
        ToolDefinition(
            name="evaluate_with_gemini",
            description=(
                "Use Gemini as an impartial judge (LLM-as-a-judge) to evaluate a piece of "
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
                        "description": "Gemini model id to judge with (optional).",
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
            source="integration:gemini",
        ),
        ToolDefinition(
            name="compare_gemini_models",
            description=(
                "Send the same prompt to several Gemini models at once and return their "
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
                        "description": "List of Gemini model ids to compare (max 5).",
                    },
                    "system_prompt": {
                        "type": "string",
                        "description": "Optional system instructions applied to every model.",
                    },
                    "max_tokens": {
                        "type": "integer",
                        "description": "Max output tokens per model (default 1024).",
                        "default": _COMPARE_DEFAULT_MAX_TOKENS,
                    },
                },
                "required": ["prompt", "models"],
            },
            source="integration:gemini",
        ),
        ToolDefinition(
            name="query_gemini_grounded",
            description=(
                "Ask Gemini a question with Google Search grounding enabled, so the answer "
                "is grounded in current web results and returns the search queries Gemini "
                "ran plus cited sources (title + URL). Best for up-to-date brand "
                "monitoring (recent news, reputation, what the live web says about a "
                "company/product) that the model's training data alone cannot answer."
            ),
            priority=0,
            input_schema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The question to answer using Google Search grounding.",
                    },
                    "model": {
                        "type": "string",
                        "description": "Gemini model id (optional; must support grounding).",
                    },
                    "max_tokens": {
                        "type": "integer",
                        "description": "Maximum output tokens (default 2048).",
                        "default": _DEFAULT_MAX_TOKENS,
                    },
                },
                "required": ["query"],
            },
            source="integration:gemini",
        ),
    ]
