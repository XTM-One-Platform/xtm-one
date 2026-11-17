# SPDX-License-Identifier: Apache-2.0

"""Vault references: a secret field that points at an enterprise vault instead of holding the value.

A secret field (an integration's API key or client secret, an MCP server
header or environment variable, an LLM provider API key, a secret variable,
a channel bot token) holds either the secret itself or a reference::

    vault://<provider>/<path>#<key>

* ``provider`` - the name of a vault provider the operator declared
  (``SECRET_VAULT_<NAME>_TYPE``, ``app/services/vault/config.py``): lowercase
  letters, digits and ``_``, starting with a letter.
* ``path`` - where the secret lives in that vault, as the vault names it
  (``secret/github`` in HashiCorp Vault, ``prod/github-token`` or an ARN in
  AWS Secrets Manager).
* ``#key`` - optional: the entry to read when the secret holds several
  (a Vault KV secret, a JSON secret string). Without it, a secret stored as
  one value is read whole, and a Vault KV secret holding one entry is read
  as that entry.

The reference is stored encrypted like any secret and resolved where the
secret is used (``app.services.vault.reveal_field``); ``decrypt_value``
refuses one. A compound credential the platform builds itself
(``<api key>||<base url>``) may hold a reference in any ``||`` segment.

Pure standard library: the settings, the security helpers and the request
schemas all import it.
"""

from __future__ import annotations

import re
from collections.abc import Collection
from dataclasses import dataclass

VAULT_REFERENCE_PREFIX = "vault://"

#: A provider name: also the ``<NAME>`` of its ``SECRET_VAULT_<NAME>_*`` variables.
PROVIDER_NAME_PATTERN = r"[a-z][a-z0-9_]{0,31}"
# Matched with fullmatch(): a ``$`` anchor would still admit a trailing newline.
_PROVIDER_NAME_RE = re.compile(PROVIDER_NAME_PATTERN)
#: One path segment: what HashiCorp, AWS, GCP and Azure accept in a secret's name, no space.
_SEGMENT = r"[A-Za-z0-9._~@:+=,\-]+"
_PATH_RE = re.compile(rf"{_SEGMENT}(?:/{_SEGMENT})*")
_KEY_RE = re.compile(r"[A-Za-z0-9._~@:+=,\-/]{1,256}")
_MAX_REFERENCE_CHARS = 1024
#: The separator of the compound credentials the platform stores (``<key>||<base url>``).
COMPOUND_SEPARATOR = "||"


class SecretReferenceSyntaxError(ValueError):
    """A value that starts like a vault reference but is not one; the message says what is wrong."""


@dataclass(frozen=True)
class SecretReference:
    provider: str
    path: str
    key: str | None = None

    def __str__(self) -> str:
        suffix = f"#{self.key}" if self.key else ""
        return f"{VAULT_REFERENCE_PREFIX}{self.provider}/{self.path}{suffix}"


def is_secret_reference(value: object) -> bool:
    """Whether *value* is meant as a vault reference: it starts with ``vault://``, case and outer spaces aside."""
    return isinstance(value, str) and value.strip()[: len(VAULT_REFERENCE_PREFIX)].lower() == VAULT_REFERENCE_PREFIX


def holds_secret_reference(text: object) -> bool:
    """Whether stored secret text is, or holds in a ``||`` segment, a vault reference."""
    if not isinstance(text, str):
        return False
    if is_secret_reference(text):
        return True
    return COMPOUND_SEPARATOR in text and any(is_secret_reference(part) for part in text.split(COMPOUND_SEPARATOR))


def parse_secret_reference(value: str) -> SecretReference:
    """The reference *value* spells; :class:`SecretReferenceSyntaxError` naming the problem otherwise."""
    text = value.strip()
    if not is_secret_reference(text):
        raise SecretReferenceSyntaxError("A vault reference starts with vault://")
    if len(text) > _MAX_REFERENCE_CHARS:
        raise SecretReferenceSyntaxError(f"A vault reference is at most {_MAX_REFERENCE_CHARS} characters long")
    body = text[len(VAULT_REFERENCE_PREFIX) :]
    location, has_key, key = body.partition("#")
    provider, _, path = location.partition("/")
    if not _PROVIDER_NAME_RE.fullmatch(provider):
        raise SecretReferenceSyntaxError(
            "A vault reference names its provider first, in lowercase letters, digits and _ "
            "(vault://<provider>/<path>#<key>)"
        )
    if not path:
        raise SecretReferenceSyntaxError("A vault reference names the secret's path after the provider")
    if not _PATH_RE.fullmatch(path) or any(segment in (".", "..") for segment in path.split("/")):
        raise SecretReferenceSyntaxError(
            "A vault reference path is made of letters, digits and . _ ~ @ : + = , - separated by /"
        )
    if has_key and not _KEY_RE.fullmatch(key):
        raise SecretReferenceSyntaxError(
            "A vault reference key after # is made of letters, digits and . _ ~ @ : + = , - /"
        )
    return SecretReference(provider=provider, path=path, key=key or None)


def secret_references_in(text: str) -> list[SecretReference]:
    """Every reference stored secret text holds, whole or in its ``||`` segments; one that does not parse raises.

    No reference contains ``||`` (neither a path nor a key may), so a value
    holding it is always a compound.
    """
    if COMPOUND_SEPARATOR in text:
        return [parse_secret_reference(part) for part in text.split(COMPOUND_SEPARATOR) if is_secret_reference(part)]
    return [parse_secret_reference(text)] if is_secret_reference(text) else []


def secret_reference_problem(value: object, providers: Collection[str]) -> str | None:
    """Why *value*, written to a secret field, cannot be stored: ``None`` for a plain value or a usable reference.

    *providers* are the names of the vault providers this deployment declares;
    a reference to any other is refused when it is written rather than failing
    every time it is used. A compound value (``<key>||<base url>``) is checked
    segment by segment.
    """
    if not holds_secret_reference(value):
        return None
    try:
        references = secret_references_in(str(value))
    except SecretReferenceSyntaxError as exc:
        return str(exc)
    for reference in references:
        if reference.provider not in providers:
            if not providers:
                return "Vault references are not available: no vault provider is configured on this platform"
            available = ", ".join(sorted(providers))
            return f"No vault provider named '{reference.provider}' is configured (available: {available})"
    return None


def describe_reference(reference: SecretReference) -> str:
    """How a reference is shown where a secret would be masked: its provider only."""
    return f"{VAULT_REFERENCE_PREFIX}{reference.provider}/..."
