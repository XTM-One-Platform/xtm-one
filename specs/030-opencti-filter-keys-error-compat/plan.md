# Implementation Plan: Keep the OpenCTI filter-key hint working across OpenCTI versions

**Branch**: `030-opencti-filter-keys-error-compat` | **Date**: 2026-10-19 | **Spec**: [spec.md](spec.md)

**Input**: Feature specification from `/specs/030-opencti-filter-keys-error-compat/spec.md`

## Summary

`_filter_key_hint` adds close schema keys to a rejected-filter-key error, but only recognises `UNSUPPORTED_ERROR`. It will recognise the old and the new code through one module-level tuple, so older OpenCTI keeps working ([research.md](research.md) R1). The status in the error details is never read ([research.md](research.md) R2).

## Technical Context

**Language/Version**: Python 3.14 (backend)

**Primary Dependencies**: none new

**Storage**: N/A

**Testing**: pytest, `backend/tests/test_opencti_list_entities_filter_keys.py`

**Target Platform**: Linux server

**Project Type**: web-service (backend only)

**Performance Goals**: N/A; one extra substring test, only after a rejection.

**Constraints**: OpenCTI stays the judge of valid keys; no pre-check against `filterKeysSchema`.

**Scale/Scope**: one function, one constant, one test module.

## Constitution Check

| Principle | Verdict |
|-----------|---------|
| VI. CI gates | Pass: `ruff check` and the parametrized tests cover both codes. |
| VII. Rationale | Pass: one short comment on why two codes are kept. |
| XI. Minimal writing | Pass: one fact per artifact; `data-model.md` and `contracts/` omitted, nothing to put in them. |
| XII. Generic mechanisms | Pass: the change is in the OpenCTI provider adapter, not a generic layer. |
| Others (I–V, VIII–X) | Not touched: no data, migration, UI or agent-loop change. |

No violations; Complexity Tracking omitted. Re-checked after design: unchanged.

## Project Structure

### Documentation (this feature)

```text
specs/030-opencti-filter-keys-error-compat/
├── plan.md
├── research.md
├── quickstart.md
└── tasks.md             # /speckit-tasks
```

### Source Code (repository root)

```text
backend/
├── app/integrations/providers/opencti/knowledge.py      # _filter_key_hint
└── tests/test_opencti_list_entities_filter_keys.py      # parametrize over both codes
```

**Structure Decision**: edit the two existing files; no new module.
