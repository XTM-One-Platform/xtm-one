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

"""How each metric reads on a single entry, as a platform-neutral lookup.

Some metrics only mean something across entries. The classification F1s are three ways of
combining entries: on one entry there is nothing to combine, so two of them repeat or
rename the entry's own label F1. The package declares this on the metric's metadata, so the
API reads it from there and the run page follows it, never a key or a family it knows of.

An unknown key, or one the package declares nothing for, is shown under its own name.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.services.evaluation.metric_package import registry as _registry_module


@dataclass(frozen=True)
class EntryView:
    """Whether a metric is shown on one entry, and the name it takes there."""

    shown: bool = True
    #: ``None`` keeps the metric's own name.
    label: str | None = None


def metric_entry_view(key: str) -> EntryView:
    """How the metric ``key`` reads on one entry; shown under its own name by default."""
    declared = _registry_module.registry().entry_views.get(key)
    return EntryView() if declared is None else EntryView(shown=declared[0], label=declared[1])


__all__ = ["EntryView", "metric_entry_view"]
