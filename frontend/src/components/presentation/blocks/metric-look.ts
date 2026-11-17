/*
Copyright (c) 2025-2026 Filigran SAS

This file is part of the XTM One Enterprise Edition ("EE") and is
licensed under the XTM One Enterprise Edition License (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
*/

/**
 * What a KPI tile draws besides its value and label, apart from the
 * component so it can be unit-tested.
 *
 * A `plain` tile - the `plain` variant of a `metric` / `metrics_grid`, and
 * every figure under a quote - is the value, in its tone, and the label. The
 * icon, the trend line, the sublabel and the delta a tile may carry belong to
 * the card: they stay in the spec (the tile shows them again the moment it is
 * a card) and are never drawn on a plain tile, so the editable PPTX and the
 * layout check, which both read the rendered DOM, see the tile the screen shows.
 */
import type { DeckMetricItem, DeckMetricVariant } from "@/types";

/** The adornments a tile draws: what it carries, or none at all on a plain tile. */
type TileAdornments = Pick<DeckMetricItem, "icon" | "sparkline" | "sublabel" | "delta">;

/** The tile fields a `plain` tile keeps for the card and does not draw. */
export const PLAIN_TILE_UNDRAWN = ["sublabel", "icon", "delta", "trend", "delta_tone", "sparkline"] as const;

export function tileAdornments(metric: DeckMetricItem, variant: DeckMetricVariant | undefined): TileAdornments {
  if (variant === "plain") return {};
  return { icon: metric.icon, sparkline: metric.sparkline, sublabel: metric.sublabel, delta: metric.delta };
}
