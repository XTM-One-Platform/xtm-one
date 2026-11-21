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

"""The structured ask of a work item waiting for a person (#9437).

The model and its readability guard are shared by every human gate and live
in :mod:`app.services.decision_ask` (#842); this module keeps the names the
work-item paths import.
"""

from __future__ import annotations

from app.services.decision_ask import (
    ASK_FIELDS_MESSAGE,
    MAX_CONTEXT_CHARS,
    MAX_EFFECT_CHARS,
    MAX_KEY_CHARS,
    MAX_LABEL_CHARS,
    MAX_LINKS,
    MAX_OPTIONS,
    MAX_QUESTION_CHARS,
    MAX_URL_CHARS,
    READABLE_ASK_EXAMPLE,
    RECOMMENDED_EFFECT_MESSAGE,
    ask_lines,
    normalize_ask,
    person_ask_missing,
    refuse_incomplete_person_ask,
    source_links,
    unreadable_reasons,
    with_source_links,
)

__all__ = [
    "ASK_FIELDS_MESSAGE",
    "MAX_CONTEXT_CHARS",
    "MAX_EFFECT_CHARS",
    "MAX_KEY_CHARS",
    "MAX_LABEL_CHARS",
    "MAX_LINKS",
    "MAX_OPTIONS",
    "MAX_QUESTION_CHARS",
    "MAX_URL_CHARS",
    "READABLE_ASK_EXAMPLE",
    "RECOMMENDED_EFFECT_MESSAGE",
    "ask_lines",
    "normalize_ask",
    "person_ask_missing",
    "refuse_incomplete_person_ask",
    "source_links",
    "unreadable_reasons",
    "with_source_links",
]
