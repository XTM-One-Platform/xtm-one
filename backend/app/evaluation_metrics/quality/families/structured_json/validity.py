# Copyright (c) 2025-2026 Filigran SAS
#
# This file is part of the XTM One Enterprise Edition ("EE") and is
# licensed under the XTM One Enterprise Edition License (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.

"""STRUCTURED_JSON — JSON Schema Draft 2020-12 validity.

``json_schema_validity`` validates an already-parsed value against a caller-supplied
Draft 2020-12 schema; an invalid schema is caller misconfiguration and raises
``ConfigurationError``, never a score.

A failure is the agent's own outcome, so it is ``scored``/0.0 and counts in run
aggregation, where ``invalid`` would be dropped. Text that is not valid JSON fails schema
conformance too: ``families/routing.py`` scores ``json.schema_validity`` 0.0 for it from
``json_parse_error``, whose reason tells "not JSON" from "schema violation". A separate
syntactic-validity metric said nothing more and was removed.
"""

import json
from collections.abc import Mapping

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from app.evaluation_metrics.core.errors import ConfigurationError
from app.evaluation_metrics.quality.families.structured_json.pointer import pointer_from_tokens
from app.evaluation_metrics.quality.primitives.pass_fail import pass_fail_counts
from app.evaluation_metrics.quality.results import (
    JSONValue,
    MetricDirection,
    MetricError,
    MetricMetadata,
    MetricRollup,
    MetricStatus,
    ScoreResult,
)

_SCHEMA_META = MetricMetadata(
    key="json.schema_validity",
    label="Schema Validity",
    kind="validity",
    primary_measure="valid",
    direction=MetricDirection.higher_is_better,
    rollup=MetricRollup.pooled,
)


def _reject_nonstandard_constant(token: str) -> float:
    """Reject the non-standard JSON constants ``NaN``/``Infinity``/``-Infinity``."""
    raise ValueError(f"non-standard JSON constant not permitted: {token}")


def json_parse_error(prediction: str | bytes) -> MetricError | None:
    """Return a structured parse error for ``prediction``, or ``None`` if it is valid JSON.

    Also used by ``families/routing.py`` to build the ``json.schema_validity`` result for
    text that never parsed.
    """
    try:
        json.loads(prediction, parse_constant=_reject_nonstandard_constant)
    except UnicodeDecodeError as exc:
        return MetricError(
            code="json_parse_error",
            message=f"invalid UTF-8 byte sequence: {exc.reason}",
            locations=[f"byte {exc.start}"],
            details={"start": exc.start, "end": exc.end},
        )
    except json.JSONDecodeError as exc:
        return MetricError(
            code="json_parse_error",
            message=exc.msg,
            locations=[f"line {exc.lineno} column {exc.colno}"],
            details={"pos": exc.pos},
        )
    except ValueError as exc:
        # Raised by ``parse_constant`` for NaN/Infinity/-Infinity.
        return MetricError(code="json_parse_error", message=str(exc), locations=[], details={})
    return None


def json_schema_validity(
    prediction: object, schema: Mapping[str, JSONValue]
) -> ScoreResult:
    """Validate a parsed value against a JSON Schema Draft 2020-12 ``schema``.

    Args:
        prediction: The already-parsed JSON value under evaluation.
        schema: The Draft 2020-12 schema to validate against.

    Returns:
        ``scored``/100 when the value conforms, otherwise ``scored``/0 with the violation
        locations (RFC 6901 pointers) and messages in ``detail``.

    Raises:
        ConfigurationError: If ``schema`` is itself malformed (caller misconfiguration).
    """
    try:
        Draft202012Validator.check_schema(dict(schema))
    except SchemaError as exc:
        raise ConfigurationError(f"invalid JSON Schema: {exc.message}") from exc

    validator = Draft202012Validator(dict(schema))
    errors = sorted(
        validator.iter_errors(prediction),
        key=lambda e: (pointer_from_tokens(e.absolute_path), e.message),
    )
    if not errors:
        return ScoreResult(
            status=MetricStatus.scored,
            value=100.0,
            detail=pass_fail_counts(True),
            metadata=_SCHEMA_META,
        )
    return ScoreResult(
        status=MetricStatus.scored,
        value=0.0,
        detail={
            "reason": "schema_violation",
            "error_code": "schema_invalid",
            "error_message": f"{len(errors)} schema violation(s)",
            "locations": [pointer_from_tokens(e.absolute_path) for e in errors],
            "messages": [e.message for e in errors],
            **pass_fail_counts(False),
        },
        metadata=_SCHEMA_META,
    )


__all__ = ["json_parse_error", "json_schema_validity"]
