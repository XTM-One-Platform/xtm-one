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

"""RFC 6901 JSON Pointer construction.

Correctly escaped pointers for field paths, comparison policy paths, and JSON Schema
error locations. Per RFC 6901, ``~`` is escaped as ``~0`` and ``/`` as ``~1`` within a
reference token.
"""

from collections.abc import Iterable


def escape_token(token: str) -> str:
    """Escape a single JSON Pointer reference token per RFC 6901.

    Args:
        token: The raw reference token.

    Returns:
        The token with ``~`` escaped as ``~0`` and ``/`` as ``~1``.
    """
    return token.replace("~", "~0").replace("/", "~1")


def child_pointer(parent: str, token: object) -> str:
    """Build the pointer for a token nested under a parent pointer.

    Args:
        parent: The parent JSON Pointer (``""`` for the document root).
        token: The child key or array index.

    Returns:
        The escaped child JSON Pointer.
    """
    return f"{parent}/{escape_token(str(token))}"


def pointer_from_tokens(tokens: Iterable[object]) -> str:
    """Build a JSON Pointer from an ordered sequence of reference tokens.

    Args:
        tokens: The ordered reference tokens.

    Returns:
        The escaped JSON Pointer; an empty sequence (the whole document) renders as ``/``
        for readability.
    """
    parts = [escape_token(str(token)) for token in tokens]
    return "/" + "/".join(parts) if parts else "/"


__all__ = ["child_pointer", "escape_token", "pointer_from_tokens"]
