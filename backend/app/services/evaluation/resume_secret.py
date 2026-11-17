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

"""The resume secret of a callback evaluation.

It signs the external callbacks (``POST /evaluations/resume``), so it is a
credential (invariant 2): stored Fernet-encrypted as
``trigger_config.resume_secret_enc``, served masked, never exported. A write
sends it in clear as ``trigger_config.resume_secret``.
"""

from cryptography.fernet import InvalidToken

from app.core.secret_refs import holds_secret_reference
from app.core.security import (
    SecretReferenceError,
    UnusableSecretError,
    configured_secret,
    decrypt_value,
    encrypt_value,
)

RESUME_SECRET_KEY = "resume_secret"  # noqa: S105 - config key name, not a secret
RESUME_SECRET_ENC_KEY = "resume_secret_enc"  # noqa: S105 - config key name, not a secret
MASKED_SECRET = "***"  # noqa: S105 - the mask served in its place


def without_resume_secret(trigger_config: dict | None) -> dict:
    """The trigger config with no secret in it, for an export or an import."""
    if not isinstance(trigger_config, dict):
        return {}
    return {k: v for k, v in trigger_config.items() if k not in (RESUME_SECRET_KEY, RESUME_SECRET_ENC_KEY)}


def stored_trigger_config(incoming: dict | None, stored: dict | None = None) -> dict:
    """The trigger config a write stores, the secret encrypted.

    A write that sends no ``resume_secret``, or the mask it was served, keeps
    the stored one; ``null`` or ``""`` removes it. A value no sender can sign
    with, or a vault reference, which the callback does not resolve, raises
    :class:`~app.core.security.UnusableSecretError`.
    """
    config = without_resume_secret(incoming)
    if incoming is None or incoming.get(RESUME_SECRET_KEY, MASKED_SECRET) == MASKED_SECRET:
        kept = (stored or {}).get(RESUME_SECRET_ENC_KEY)
        if kept:
            config[RESUME_SECRET_ENC_KEY] = kept
        return config
    secret = configured_secret(incoming[RESUME_SECRET_KEY])
    if holds_secret_reference(secret):
        raise UnusableSecretError("a vault reference is not supported for the resume secret")
    if secret is not None:
        config[RESUME_SECRET_ENC_KEY] = encrypt_value(secret)
    return config


def served_trigger_config(stored: dict | None) -> dict:
    """The trigger config a response serves: the secret masked."""
    config = without_resume_secret(stored)
    if (stored or {}).get(RESUME_SECRET_ENC_KEY):
        config[RESUME_SECRET_KEY] = MASKED_SECRET
    return config


def callback_unreachable(trigger_config: dict | None) -> bool:
    """Whether the evaluation waits for callbacks no sender can sign: no secret is set.

    Its entries would park until their callback timeout, after the agent's first
    turn ran (an imported evaluation keeps ``await_callback`` and loses its secret).
    """
    config = trigger_config if isinstance(trigger_config, dict) else {}
    return bool(config.get("await_callback")) and not config.get(RESUME_SECRET_ENC_KEY)


def resume_secret(trigger_config: dict | None) -> str | None:
    """The secret that signs the evaluation's callbacks, or ``None`` when none is set.

    A stored value no sender can prove - not a string, not decryptable with this
    key, or a vault reference - raises :class:`~cryptography.fernet.InvalidToken`,
    which the public callback answers as a wrong signature, never a 500.
    """
    stored = (trigger_config or {}).get(RESUME_SECRET_ENC_KEY)
    if not stored:
        return None
    if not isinstance(stored, str):
        raise InvalidToken
    try:
        return decrypt_value(stored)
    except SecretReferenceError as exc:
        raise InvalidToken from exc
