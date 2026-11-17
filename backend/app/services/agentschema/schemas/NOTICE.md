# Vendored Microsoft AgentSchema v1.0 schemas

The `*.yaml` files in this directory are copied verbatim from the
[`microsoft/AgentSchema`](https://github.com/microsoft/AgentSchema) repository,
path `schemas/v1.0/`, and are used **only** to validate AgentSchema documents
that XTM One exports or imports (see
`backend/app/services/agentschema/spec.py`).

| | |
| --- | --- |
| Upstream repository | <https://github.com/microsoft/AgentSchema> |
| Specification site | <https://microsoft.github.io/AgentSchema> |
| Vendored path | `schemas/v1.0/*.yaml` |
| Pinned commit | `b7833d1ff6888d655643de2f11355e791133b939` (2026-06-24) |
| Upstream license | MIT (see below) |

## Why these files are vendored rather than fetched or taken from an SDK

- **Determinism.** Validation must not depend on network access at request
  time, and the spec is still published as `v1.0.0-beta.*` — pinning a commit
  means an upstream edit can never change how existing XTM One exports
  validate. Refreshing is a deliberate, reviewable commit.
- **No new runtime dependency.** `jsonschema` and `PyYAML` are already backend
  dependencies, so these files validate with what we ship. The official
  `agentschema` PyPI package is a beta that additionally declares `ruff` and
  `black` as *runtime* requirements.
- **Fidelity.** "The export validates against the official AgentSchema
  schema" is asserted against Microsoft's own files rather than against our
  re-interpretation of them.

## Refreshing the vendored copy

```bash
python backend/scripts/refresh_agentschema_vendor.py
```

Then update the pinned commit in the table above and in
`AGENTSCHEMA_UPSTREAM_COMMIT` (`spec.py`), and run
`pytest tests/test_agentschema_spec.py` — the vendor-integrity test fails if
the two get out of sync.

## MIT License

```
MIT License

Copyright (c) Microsoft Corporation.

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```
