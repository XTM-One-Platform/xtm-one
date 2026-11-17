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

"""The dedup key of a proposal: a re-run of the same finding files no duplicate.

``sha256(type | target type | target id | evidence fingerprint)``. The
evidence fingerprint is the finding's own fingerprint when the author passes
one (a finding matched across cycles keeps it), else the sha256 of the sorted,
de-duplicated ``kind:id`` lines of the evidence references - so the order the
author listed them in, or a reference listed twice, makes no new key.

Migration ``c3cnc8254032`` computes the same key in SQL for the persona
rewrites it moves in: change both together.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Mapping, Sequence
from typing import Any


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def evidence_fingerprint(finding: Mapping[str, Any], evidence_refs: Sequence[Mapping[str, Any]]) -> str:
    fingerprint = finding.get("fingerprint")
    if isinstance(fingerprint, str) and fingerprint.strip():
        return fingerprint.strip()
    lines = sorted({f"{ref.get('kind', '')}:{ref.get('id', '')}" for ref in evidence_refs})
    return _sha256("\n".join(lines))


def dedup_key(proposal_type: str, target_type: str, target_id: uuid.UUID, fingerprint: str) -> str:
    return _sha256(f"{proposal_type}|{target_type}|{target_id}|{fingerprint}")
