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

"""Tokens of a dashboard: the values its inputs set, put into its data sources.

A definition refers to a token as ``$name$``
(``$name|s$`` and the other filters are the page's: they say how a value is
written into text or a URL). The page holds the tokens - inputs, buttons,
defaults, computed expressions, the viewer's environment, a source's result -
and sends the ones a data source uses with each read (:class:`TokenValues`,
bounded). The server puts them into the source, never into query text:

- a filter's values (``{key, values}`` anywhere in a native widget, a
  ``derived`` step's ``value``): a value that is only ``$name$`` becomes the
  token's value, a list token several values; a value holding ``$name$`` among
  other characters gets the token's text in place. The result goes through
  the same checks as a stored widget (``check_native``), as GraphQL variables
  or JSON fields;
- a platform source's ``range``: a range key, or the token holding one.

A source that uses a token with no value waits (code ``waiting``) until
its input is set - but for an optional token (an entity
input that is not required): a filter whose values are only such tokens, none
set, is left out, as OpenAEV reads a dashboard parameter nobody picked.
"""

from __future__ import annotations

import re
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

#: ``$name$`` or ``$name|f$``: a token name (an input's, ``env:user``, ``ds:result.count``) and a filter.
PLACEHOLDER_RE = re.compile(r"\$([A-Za-z_][A-Za-z0-9_.:]{0,99})(?:\|([shun]))?\$")
NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.:]{0,99}$")
MAX_TOKENS = 50
MAX_VALUE_CHARS = 500
MAX_LIST = 50


class TokensWaitingError(Exception):
    """A source uses a token that has no value yet."""

    def __init__(self, name: str):
        super().__init__(name)
        self.name = name


TokenValue = str | list[Annotated[str, Field(max_length=MAX_VALUE_CHARS)]]


class TokenValues(BaseModel):
    """The tokens a read is made with: names checked, values bounded."""

    model_config = ConfigDict(extra="forbid")

    values: Annotated[dict[str, TokenValue], Field(default_factory=dict, max_length=MAX_TOKENS)]

    @field_validator("values")
    @classmethod
    def _bounded(cls, values: dict) -> dict:
        for name, value in values.items():
            if not NAME_RE.match(name):
                raise ValueError(f"'{name[:60]}' is not a token name")
            if isinstance(value, str) and len(value) > MAX_VALUE_CHARS:
                raise ValueError(f"token '{name}' holds more than {MAX_VALUE_CHARS} characters")
            if isinstance(value, list) and len(value) > MAX_LIST:
                raise ValueError(f"token '{name}' holds more than {MAX_LIST} values")
        return values


def names_in(value: object) -> set[str]:
    """Every token name *value* (any JSON) refers to."""
    found: set[str] = set()
    if isinstance(value, str):
        found.update(match.group(1) for match in PLACEHOLDER_RE.finditer(value))
    elif isinstance(value, dict):
        for item in value.values():
            found |= names_in(item)
    elif isinstance(value, list):
        for item in value:
            found |= names_in(item)
    return found


def _lookup(name: str, tokens: dict[str, Any]) -> str | list[str]:
    value = tokens.get(name)
    if value is None or value == [] or value == "":
        raise TokensWaitingError(name)
    return value


def substitute(value: str, tokens: dict[str, Any]) -> str | list[str]:
    """*value* with its tokens put in: the token itself when *value* is only ``$name$``, its text otherwise."""
    whole = PLACEHOLDER_RE.fullmatch(value)
    if whole:
        return _lookup(whole.group(1), tokens)

    def text(match: re.Match[str]) -> str:
        token = _lookup(match.group(1), tokens)
        return ",".join(token) if isinstance(token, list) else token

    return PLACEHOLDER_RE.sub(text, value)


def _values(values: list, tokens: dict[str, Any]) -> list:
    out: list = []
    for item in values:
        if isinstance(item, str) and "$" in item:
            replaced = substitute(item, tokens)
            out.extend(replaced if isinstance(replaced, list) else [replaced])
        else:
            out.append(put_in_filters(item, tokens))
    return out[: MAX_LIST * 2]


def _left_out(item: object, tokens: dict[str, Any], optional: frozenset[str]) -> bool:
    """Whether *item* is a filter whose values are only optional tokens with no value."""
    if not optional or not isinstance(item, dict) or "key" not in item or not isinstance(item.get("values"), list):
        return False
    whole = [PLACEHOLDER_RE.fullmatch(v) if isinstance(v, str) else None for v in item["values"]]
    return bool(whole) and all(
        match is not None and match.group(1) in optional and tokens.get(match.group(1)) in (None, "", []) for match in whole
    )


def put_in_filters(value: Any, tokens: dict[str, Any], optional: frozenset[str] = frozenset()) -> Any:
    """*value* (a native widget, a step list) with tokens put into its filters' ``values`` and steps' ``value``.

    Nothing else is touched: a token anywhere else stays as written and the
    platform's checks refuse it like any other unexpected text. A filter on
    *optional* tokens none of which is set is left out.
    """
    if isinstance(value, list):
        return [put_in_filters(item, tokens, optional) for item in value if not _left_out(item, tokens, optional)]
    if not isinstance(value, dict):
        return value
    out = {}
    for key, item in value.items():
        if key == "values" and isinstance(item, list) and "key" in value:
            out[key] = _values(item, tokens)
        elif key == "value" and value.get("op") == "where" and isinstance(item, str) and "$" in item:
            out[key] = substitute(item, tokens)
        else:
            out[key] = put_in_filters(item, tokens, optional)
    return out


def range_of(value: object, tokens: dict[str, Any], fallback: str | None) -> str | None:
    """A platform source's ``range``: its key, the token holding one, or the page's range."""
    if not isinstance(value, str) or not value:
        return fallback
    resolved = substitute(value, tokens)
    return resolved if isinstance(resolved, str) else fallback


__all__ = [
    "MAX_TOKENS",
    "NAME_RE",
    "PLACEHOLDER_RE",
    "TokenValues",
    "TokensWaitingError",
    "names_in",
    "put_in_filters",
    "range_of",
    "substitute",
]
