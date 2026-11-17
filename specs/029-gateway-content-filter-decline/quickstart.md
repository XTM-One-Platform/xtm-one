# Quickstart: validate the decline

Prerequisite: the backend venv (`backend/.venv`). From `backend/`:

```bash
.venv/bin/python -m pytest tests/test_model_declined.py tests/test_gateway_provider.py -q
```

Expected: the new gateway-shaped cases pass for [the three paths](data-model.md), and the existing Anthropic decline cases still pass (the shared builder).

Lint (blocking gate):

```bash
.venv/bin/ruff check app tests
```

Live check, once the gateway's refusal signal is deployed: send a request the model refuses through a `FiligranGatewayProvider` model; expect a decline with its category, not an empty reply. The Bedrock category may be absent ([spec.md](spec.md) edge cases). Also check the gateway lists the decline fallback ids (`claude-sonnet-5`, `claude-opus-4-8`): if it does not, the chain ends on that error and the decline category is lost.
