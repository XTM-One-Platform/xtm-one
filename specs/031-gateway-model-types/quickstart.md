# Quickstart: Gateway Model Types and Decision Models

All checks stub the gateway's `/v1/models` (an `httpx.MockTransport`, as `tests/test_gateway_provider.py` does). No gateway is needed.

## Prerequisites

A worktree needs the main checkout's `.env` and `.venv` linked in; `PLATFORM_MODE` in `.env` changes suite results, so pin it to compare with the baseline.

## Types (US1, US2)

```bash
cd backend && uv run pytest tests/test_gateway_provider.py tests/test_settings_llm_providers.py -q
```

Expected, per [the list contract](contracts/gateway-model-list.md):

- a model typed chat and embedding is listed by the chat and embedding pickers and by no other (US1, SC-001);
- a listing with `type` only, and one with neither field, list what they listed before (US2, SC-002);
- a listing mixing unknown and known types loads every picker (SC-003).

## Decision picker (US3)

```bash
cd backend && uv run pytest tests/test_gateway_decision_models.py tests/test_decision_model_settings.py -q
```

Expected: a gateway provider and a custom OpenAI-compatible provider each offer exactly the models whose types include decision, a model also typed chat stays in the chat pickers, and the listing sends the gateway one request, `GET /v1/models` (SC-005). A gateway typing none is offered for a typed model id.

## Gates

```bash
cd backend && uv run ruff check app/ && uv run pytest tests/ -q -n 8
```

Run the whole suite and compare the failing ids with `origin/main`: a `-k` subset misses guard tests elsewhere (`tests/test_no_workarounds.py`, `tests/test_llm_decisions.py`).

## Manual (needs a real gateway)

Add a Filigran AI Gateway provider and open Settings > AI models > Speech generation: a model typed speech still lists and reads aloud.
