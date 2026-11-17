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

"""Caller-supplied configuration for the PROSE family.

``policy`` has no default, so the normalization applied is always the caller's choice.

``required_phrases`` and ``forbidden_phrases`` tell *absent* (``None``) from *explicitly
empty* (``()``). Both yield ``skipped``, not a vacuous 100, with distinct reason codes, so a
forgotten configuration differs from a deliberately empty constraint set.

The cross-list conflict check lives only here, where both lists coexist. A phrase metric
called directly gets one list and so skips conflict detection.
"""

from collections.abc import Sequence
from typing import Self

from pydantic import BaseModel, ConfigDict, model_validator

from app.evaluation_metrics.core.errors import ConfigurationError
from app.evaluation_metrics.quality.families.prose.phrases import prepare_phrases
from app.evaluation_metrics.quality.normalization.prose import ProseNormalizationPolicy


class ProseConfig(BaseModel):
    """Typed, frozen prose configuration carried on ``FamilyConfig.prose``."""

    model_config = ConfigDict(frozen=True)

    policy: ProseNormalizationPolicy
    required_phrases: tuple[str, ...] | None = None
    forbidden_phrases: tuple[str, ...] | None = None

    @model_validator(mode="before")
    @classmethod
    def _reject_non_text_elements(cls, data: object) -> object:
        """Raise ``ConfigurationError`` before pydantic reports its own type error.

        Pydantic would raise ``ValidationError`` for a non-``str`` element, which is not the
        contract's exception and does not name the list and index.
        """
        if not isinstance(data, dict):
            return data
        for list_name in ("required_phrases", "forbidden_phrases"):
            raw = data.get(list_name)
            if raw is None:
                continue
            if isinstance(raw, str):
                raise ConfigurationError(
                    f"{list_name} must be a sequence of phrases, not a single str; "
                    f"pass ({raw!r},) to supply one phrase"
                )
            if not isinstance(raw, Sequence):
                continue
            for index, element in enumerate(raw):
                if not isinstance(element, str):
                    raise ConfigurationError(
                        f"{list_name}[{index}] must be str, "
                        f"got {type(element).__name__}"
                    )
        return data

    @model_validator(mode="after")
    def _validate_phrases(self) -> Self:
        """Validate both lists and reject an unsatisfiable constraint set."""
        required = (
            prepare_phrases(self.required_phrases, self.policy, "required_phrases")
            if self.required_phrases is not None
            else None
        )
        forbidden = (
            prepare_phrases(self.forbidden_phrases, self.policy, "forbidden_phrases")
            if self.forbidden_phrases is not None
            else None
        )
        if required is None or forbidden is None:
            return self
        # Compared on normalized forms, so a conflict the policy itself creates is caught.
        conflicts = sorted(set(required.phrases) & set(forbidden.phrases))
        if conflicts:
            raise ConfigurationError(
                "phrase appears in both required_phrases and forbidden_phrases after "
                f"normalization: {conflicts}"
            )
        return self


__all__ = ["ProseConfig"]
