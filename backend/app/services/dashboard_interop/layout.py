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

"""The platforms' grids and the dashboard's grid.

OpenCTI and OpenAEV place widgets on a 12-column grid (``x``, ``y``, ``w``,
``h``), and so does an XTM One dashboard, with shorter rows
(``dashboard_grid``). Reading a platform's grid keeps each widget's place and
size, its rows converted; writing one converts them back, so a dashboard comes
back from a round trip as it was. Widgets are also read in reading order (top
to bottom, left to right), the order a narrow screen stacks them in.
"""

from __future__ import annotations

import copy

from app.services.dashboard_grid import fill_layouts, layout_from_platform, layout_to_platform, width_of


def reading_order(positions: list[tuple[object, object]]) -> list[int]:
    """The indexes of *positions* (``(x, y)``) in reading order; unplaced ones last, as given."""

    def key(index: int) -> tuple[int, float, float, int]:
        x, y = positions[index]
        placed = isinstance(x, int | float) and isinstance(y, int | float) and not isinstance(x, bool)
        return (0, float(y), float(x), index) if placed else (1, 0.0, 0.0, index)  # type: ignore[arg-type]

    return sorted(range(len(positions)), key=key)


def placement(x: object, y: object, w: object, h: object, platform: str) -> dict:
    """The ``layout`` and ``width`` of a widget placed at ``x, y, w, h`` on *platform*'s grid (none when it does not say)."""
    layout = layout_from_platform(x, y, w, h, platform)
    if layout is None:
        return {}
    return {"layout": layout, "width": width_of(layout["w"])}


def platform_layouts(widgets: list[dict], platform: str) -> list[dict]:
    """``{x, y, w, h}`` on *platform*'s grid for each of *widgets* (placed first when one has no layout)."""
    placed = fill_layouts([copy.deepcopy(w) for w in widgets])
    return [layout_to_platform(w["layout"], platform) for w in placed]
