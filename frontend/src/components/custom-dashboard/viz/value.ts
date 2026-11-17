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
 * A single value's styles (`valueStyle`), apart from the drawing:
 *
 * - `standard`: XTM One's single value;
 * - `difference`: a large figure (32 px, weight 600, SI form) under the change
 *   over its period, written whole with an arrow - up and green above zero,
 *   down and red below, a flat arrow in grey at zero - and the period in
 *   brackets (OpenCTI's number: `WidgetNumber`, `NumberDifference`);
 * - `tile`: an icon tile (42 px, radius 4, its colour at 10% behind it), the
 *   figure counting up from the value before (1200 ms, ease-out cubic,
 *   compact form) and a trend chip - `+N` / `-N` in compact form, no sign at
 *   zero, trending up, down or flat - telling the previous value on hover
 *   (OpenAEV's number: `NumberWidget`, `TrendChip`).
 *
 * Pure, relative imports only: runs on `node --test`.
 */
import { formatNamedNumber } from "../charts/formats.ts";

const VALUE_STYLES = ["standard", "difference", "tile"] as const;
export type ValueStyle = (typeof VALUE_STYLES)[number];

export const readValueStyle = (options: Record<string, unknown>): ValueStyle =>
  (VALUE_STYLES as readonly unknown[]).includes(options.valueStyle) ? (options.valueStyle as ValueStyle) : "standard";

export type Direction = "up" | "down" | "flat";

export const directionOf = (change: number | null | undefined): Direction =>
  typeof change !== "number" || !Number.isFinite(change) || change === 0 ? "flat" : change > 0 ? "up" : "down";

/** The tone of a direction: up is good news, down bad, flat neither. */
export const DIRECTION_TONES: Record<Direction, "success" | "danger" | "neutral"> = { up: "success", down: "danger", flat: "neutral" };

/** A difference written whole, its sign kept (`3`, `-2`, `0`): a difference style's figure. */
export const wholeDifference = (change: number): string => String(Math.round(change * 100) / 100);

/** A trend chip's label: `+N` / `-N` in compact form, `0` without a sign. */
export function trendLabel(change: number): string {
  if (change === 0) return "0";
  const written = formatNamedNumber(Math.abs(change), "compact");
  return `${change > 0 ? "+" : "-"}${written}`;
}

/** The tile's tint: the icon's colour at 10% behind it, its glow at 13% inside. */
export const tileStyle = (color: string): { backgroundColor: string; boxShadow: string } => ({
  backgroundColor: `color-mix(in srgb, ${color} 10%, transparent)`,
  boxShadow: `inset 0 0 14px color-mix(in srgb, ${color} 13%, transparent)`,
});

/** A trend chip's colours: its tone at 14% behind, 28% around, full on the text. */
export const chipStyle = (color: string): { backgroundColor: string; border: string; color: string } => ({
  backgroundColor: `color-mix(in srgb, ${color} 14%, transparent)`,
  border: `1px solid color-mix(in srgb, ${color} 28%, transparent)`,
  color,
});
