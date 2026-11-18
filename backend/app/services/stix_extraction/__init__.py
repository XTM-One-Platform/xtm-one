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

"""Grounded STIX 2.1 extraction from a report's text (``dev-docs/stix-grounded-extraction.md``).

Entry point: :func:`extract_stix` with a :class:`StructuredCall` built on the run's provider
(:func:`provider_structured_call`). The built-in tool ``extract_stix_from_document``
(``builtin_tools.stix_extraction``) is how an agent runs it.
"""

from app.services.stix_extraction.llm import StructuredCall, Usage, provider_structured_call
from app.services.stix_extraction.pipeline import ExtractionOptions, ExtractionResult, extract_stix

__all__ = [
    "ExtractionOptions",
    "ExtractionResult",
    "StructuredCall",
    "Usage",
    "extract_stix",
    "provider_structured_call",
]
