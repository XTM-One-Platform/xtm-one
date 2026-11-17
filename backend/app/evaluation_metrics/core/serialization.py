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

"""Immutable JSON containers shared by both suites' result types.

Internal to :mod:`evaluation_metrics.core`: these types encode *how* payloads are frozen and
serialized, a mechanism rather than a contract. Exporting them from ``core`` would make that
mechanism public API, so the suites import them from here and ``core.__all__`` lists only
contracts.
"""

from collections.abc import Mapping, Sequence
from types import MappingProxyType
from typing import Annotated, Any

from pydantic import PlainSerializer, PlainValidator


def _deep_freeze(value: Any) -> Any:
    """Recursively convert JSON containers to immutable equivalents.

    Mappings become read-only ``MappingProxyType`` views and lists/tuples become
    tuples, so a stored ``detail``/``details`` structure cannot be mutated in place.
    """
    if isinstance(value, Mapping):
        return MappingProxyType({k: _deep_freeze(v) for k, v in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_deep_freeze(v) for v in value)
    return value


def _deep_thaw(value: Any) -> Any:
    """Rebuild plain JSON containers from the frozen representation for serialization."""
    if isinstance(value, Mapping):
        return {k: _deep_thaw(v) for k, v in value.items()}
    if isinstance(value, tuple):
        return [_deep_thaw(v) for v in value]
    return value


def _to_str_tuple(value: Any) -> tuple[str, ...]:
    return tuple(str(item) for item in value)


def _to_str_list(value: Sequence[str]) -> list[str]:
    return list(value)


def _empty_mapping() -> Mapping[str, object]:
    return MappingProxyType({})


def _empty_locations() -> Sequence[str]:
    return ()


# Deeply immutable JSON object: nested arrays become tuples and nested objects read-only
# views; serializes back to a plain ``dict`` so the JSON shape is unchanged.
FrozenMapping = Annotated[
    Mapping[str, object],
    PlainValidator(_deep_freeze),
    PlainSerializer(_deep_thaw, return_type=dict, when_used="always"),
]
# Immutable string sequence exposed as a read-only ``Sequence``; serializes to a list.
FrozenStrList = Annotated[
    Sequence[str],
    PlainValidator(_to_str_tuple),
    PlainSerializer(_to_str_list, return_type=list, when_used="always"),
]
