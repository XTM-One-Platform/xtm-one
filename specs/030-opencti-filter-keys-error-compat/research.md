# Research: filter-key rejection codes

## R1. Accept both codes

- **Decision**: test the error text against a tuple `("UNSUPPORTED_ERROR", "BAD_USER_INPUT_ERROR")`; keep the `filter keys` and `keys` checks.
- **Rationale**: deployments run old and new OpenCTI side by side; the code reaches the text through `_describe_graphql_failure` (`[CODE]`), so one substring test per code is enough.
- **Alternatives**: match the message and keys only, dropping the code (wider net, loses the guard against unrelated errors); replace the old code (breaks un-upgraded OpenCTI).

## R2. Ignore the status

- **Decision**: do not read `extensions.data.http_status`.
- **Rationale**: it is echoed into the text as data; the HTTP response stays 200 either way, and the 500-only context-failure path in `_common.py` (`_CONTEXT_FAILURE_STATUSES`) reads the real HTTP status, not this field.
- **Alternatives**: none worth a code path.

## R3. Upstream contract

- **Decision**: build on `BAD_USER_INPUT_ERROR` with status 400 (renamed from `FILTER_KEYS_ERROR` during upstream review; the generic code is why the `filter keys` wording guard stays).
- **Rationale**: confirmed in [OpenCTI PR #19096](https://github.com/OpenCTI-Platform/opencti/pull/19096): both `checkFilterKeys` throws switch to it, the GraphQL response stays HTTP 200, and the message and `keys` attribute are unchanged. The PR asks not to merge before XTM One handles it.
- **Note**: the second throw (`Incorrect filter keys containing invalid characters`) carries the same code and the `filter keys` wording, so it reaches the hint as it does today.
