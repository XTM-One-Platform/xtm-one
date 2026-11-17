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

"""STIX_BUNDLE — STIX canonicalization protocol and entity-type selection policy.

:class:`Canonicalizer`, **separate** from :class:`StixValidator`, regenerates canonical
identifiers, normalizes observables and rewrites relationship references. The same
backend, version and policy MUST be applied to reference and prediction. The core never
reimplements platform STIX ID generation; ``backends.stix_opencti`` supplies an
OpenCTI/pycti-compatible backend.

:class:`StixObjectTypeSelection` defaults to scoring all STIX Domain Objects (SDOs) and
Cyber-observable Objects (SCOs), and excludes ``relationship`` (scored separately as
triples), ``sighting`` and the meta objects ``marking-definition``, ``language-content``
and ``extension-definition``. Callers may pass an allowlist, an exclusion list, or both:
the allowlist applies first, exclusions to what remains.
"""

from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

_DEFAULT_EXCLUDED = frozenset(
    {
        "relationship",
        "sighting",
        "marking-definition",
        "language-content",
        "extension-definition",
    }
)


def _default_excludelist() -> frozenset[str]:
    return _DEFAULT_EXCLUDED


@runtime_checkable
class Canonicalizer(Protocol):
    """A backend that canonicalizes a STIX bundle.

    Produces canonical identifiers, normalized observables and rewritten relationship
    references. The same backend, version and policy MUST be applied to both reference
    and prediction.
    """

    def canonicalize(self, bundle: object) -> object:
        """Return ``bundle`` with canonical identifiers and rewritten references.

        Args:
            bundle: The bundle to canonicalize.

        Returns:
            The canonicalized bundle.
        """
        ...


class StixObjectTypeSelection(BaseModel):
    """Entity-scoring object-type selection policy (strict, deterministic default).

    **Allowlist first, then exclusions**: with an ``allowlist`` a type must appear in it,
    then ``excludelist`` applies to what remains, so a type in both is excluded. With no
    ``allowlist``, every type is included except ``excludelist`` (SROs, ``sighting`` and
    meta objects).
    """

    model_config = ConfigDict(frozen=True)

    allowlist: frozenset[str] | None = None
    excludelist: frozenset[str] = Field(default_factory=_default_excludelist)

    def includes(self, object_type: str) -> bool:
        """Return whether ``object_type`` is scored as an entity under this policy.

        Args:
            object_type: The STIX object type, e.g. ``"indicator"``.

        Returns:
            ``True`` when the type survives the allowlist (if any) and is not excluded.
        """
        if self.allowlist is not None and object_type not in self.allowlist:
            return False
        return object_type not in self.excludelist


__all__ = ["Canonicalizer", "StixObjectTypeSelection"]
