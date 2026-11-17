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
 * Which slide fields DO something on the slide as it stands.
 *
 * The inspector draws every field of a layout's model, but some only act on
 * one FORM of the slide: `ratio`, `equal_height` and `equal_rows` on a columns
 * slide, `distribute` when there are at least two blocks to spread and their
 * columns do not line them up as rows, `stretch` as a
 * vertical alignment on columns (a single flow reads it as `start`). A switch
 * that flips and changes nothing on the stage reads as a broken editor; the
 * reason is said under the control instead, in one sentence, while the value
 * stays editable - it is still saved, and takes effect the moment the slide
 * takes the form it applies to. The one exception is a `wide_*` ratio on
 * three columns, which the validator REFUSES rather than ignores: its reason
 * says so, and the stage draws three columns equal whatever the draft says.
 */
import type { DeckBlock, DeckLogoGridBlock, PresentationSlide } from "@/types";

// The explicit extensions let Node's runner load this module for
// `field-applicability.test.ts` (the bundler resolves them either way).
import {
  axisReading,
  hasSecondaryAxis,
  isMirroredKind,
  legendShown,
  mapLegendStyle,
  unitScaleApplies,
  VALUE_LABEL_FIT_KINDS,
  VALUE_LABEL_SIZE_KINDS,
} from "./blocks/chart-format.ts";
import { PLAIN_TILE_UNDRAWN } from "./blocks/metric-look.ts";
import { isLoneCardGrid, isLoneStatement } from "./blocks/quote-style.ts";
import { logoGridAlign, logoGridEqualCells, logoRows, shortRows } from "./logo-sizing.ts";

const undrawnOnPlain = (key: string) => (PLAIN_TILE_UNDRAWN as readonly string[]).includes(key);
/** The chart fields only a butterfly reads. */
const BUTTERFLY_FIELDS: readonly string[] = ["spine", "symmetric", "show_totals"];

/** The i18n key of the reason a field has no visible effect on *slide*, or `null` when it acts. */
export function fieldInactiveReason(slide: PresentationSlide, key: string): string | null {
  if (slide.layout !== "content") return null;
  const columns = slide.columns;
  const blocks = slide.blocks ?? [];
  switch (key) {
    case "ratio":
      if (!columns) return "No effect on a single flow of blocks: the ratio is for a slide with two columns.";
      // Three columns draw equal whatever the ratio says AND the validator
      // refuses a `wide_*` there - said as a refusal, not as a no-op.
      if (columns.length !== 2) {
        return "Not allowed with {count} columns: wide left / right needs exactly two, and the save refuses it.";
      }
      return null;
    case "equal_height":
      return columns ? null : "No effect on a single flow of blocks: equal height is for columns.";
    case "equal_rows":
      return columns ? null : "No effect on a single flow of blocks: equal row heights are for columns.";
    case "distribute": {
      // Columns whose blocks line up as rows are not spread.
      if (columns && slide.equal_rows) {
        return "Equal row heights line the blocks up across the columns, so they are not spread: turn it off to spread them.";
      }
      const spreadable = columns ? columns.some((column) => column.length >= 2) : blocks.length >= 2;
      return spreadable ? null : "No effect with a single block: spreading needs at least two blocks to put space between.";
    }
    case "vertical_align":
      // A body that is one statement quote is centred (`bodyVerticalAlign`).
      if (!columns && isLoneStatement(blocks) && slide.vertical_align !== "end") {
        return "A slide whose only block is a statement quote centres it; only End moves it, to the bottom.";
      }
      // A lone card grid is centred by default (`bodyVerticalAlign`).
      if (!columns && isLoneCardGrid(blocks) && (slide.vertical_align ?? "start") === "start") {
        return "A slide whose only block is a card grid centres it; choose End to move it to the bottom.";
      }
      if (!columns && slide.vertical_align === "stretch") {
        return "Stretch is for columns; on a single flow of blocks it places them at the top, like Start.";
      }
      return null;
    default:
      return null;
  }
}

/**
 * The same question for a BLOCK's field: the i18n key of why it cannot act
 * on the block as it stands, or `null`. A quote is credited EITHER by its
 * legacy one-line `attribution` or by `author` / `role` / `organization` -
 * the save refuses both - so each side says so while the other holds.
 * A `plain` metric draws its value and label only (`tileAdornments`): its
 * icon, sublabel, delta, trend and delta tone stay saved and show on the card. A logo
 * grid's gap is only a minimum where its free space is shared out
 * (`logoGridGapInactiveReason`). A chart in a dashboard panel (`inPanel`)
 * stands on the panel, whatever its `framed` says, and is titled by
 * the panel: a `title` of its own is REFUSED by the validator there,
 * said as a refusal like a `wide_*` ratio on three columns. A chart's
 * `legend_position` places a legend it may not draw (`legendShown`),
 * on a slide and in a panel alike. Its `unit_scale` / `secondary_unit_scale`
 * pin the currency amounts of an axis chart's left-hand / right-hand
 * axis (`unitScaleApplies`), and its `value_label_fit` fits the
 * values of the bars, upright or horizontal, which a line, a pie or a
 * waterfall does not draw. Its `value_label_size` sizes the values of
 * an axis chart or a butterfly, which a pie, a donut or a gauge is not. A
 * butterfly's own fields - `spine`, `symmetric`, `show_totals` - act
 * on a butterfly only, its totals under a legend it draws; and the save
 * REFUSES a target or a pinned axis range on one.
 * Its `region` zooms a map, and only a map; so do its legend style and
 * markers, and its class count acts once the legend is classes.
 */
export function blockFieldInactiveReason(
  block: DeckBlock,
  key: string,
  { inPanel = false }: { inPanel?: boolean } = {},
): string | null {
  if (block.type === "chart") {
    if (key === "legend_position" && !legendShown(block)) {
      return "This chart draws no legend: the position takes effect once it does (Show legend on, or more than one series).";
    }
    if (key === "unit_scale" && !unitScaleApplies({ ...axisReading(block, "primary"), kind: block.kind })) {
      return "The unit scale writes currency amounts against a value axis: it takes effect once the number format is Currency on a bar, line, area, combo or waterfall chart.";
    }
    if (
      key === "secondary_unit_scale" &&
      !(hasSecondaryAxis(block) && unitScaleApplies({ ...axisReading(block, "secondary"), kind: block.kind }))
    ) {
      return "The right axis unit scale writes its currency amounts: it takes effect once a series reads against the right axis and its number format is Currency.";
    }
    if (key === "value_label_fit" && !VALUE_LABEL_FIT_KINDS.has(block.kind)) {
      return "No effect here: this fits the values of the bars, on a bar, stacked bar or combo chart, upright or horizontal.";
    }
    if (key === "value_label_size" && !VALUE_LABEL_SIZE_KINDS.has(block.kind)) {
      return "No effect here: this sizes the values of a bar, stacked bar, line, area, combo, waterfall or butterfly chart.";
    }
    if (BUTTERFLY_FIELDS.includes(key) && !isMirroredKind(block.kind)) {
      return "No effect here: this is a butterfly's, the chart that mirrors two series about a central spine.";
    }
    if (key === "show_totals" && !legendShown(block)) {
      return "This chart draws no legend: the totals are written under it once it does (Show legend on).";
    }
    if (isMirroredKind(block.kind) && key === "target") {
      return "Not allowed on a butterfly: it has no reference line, each side reading from the spine, and the save refuses one.";
    }
    if (isMirroredKind(block.kind) && (key === "y_min" || key === "y_max")) {
      return "Not allowed on a butterfly: its two sides read from the spine, and the save refuses a pinned axis range.";
    }
    if (key === "region" && block.kind !== "map") return "No effect here: the region zooms a map chart.";
    if ((key === "legend_style" || key === "markers") && block.kind !== "map") {
      return "No effect here: this is a map chart's, the chart that colours countries.";
    }
    if (key === "classes" && mapLegendStyle(block) !== "classes") {
      return "No effect here: this counts the classes of a map whose legend is Classes.";
    }
    if (!inPanel) return null;
    if (key === "framed") {
      return "A dashboard panel is this chart's card: Framed takes effect once the chart stands on a slide of its own.";
    }
    if (key === "title") {
      return "Not allowed in a dashboard panel: the panel's own Title names its chart, and the save refuses a second one.";
    }
    return null;
  }
  if (block.type === "metric") {
    return block.variant === "plain" && undrawnOnPlain(key)
      ? "A plain tile draws its value and label only: this shows again once the tile is a card."
      : null;
  }
  if (block.type === "logo_grid") return key === "gap" ? logoGridGapInactiveReason(block) : null;
  if (block.type !== "quote") return null;
  const split = !!(block.author || block.role || block.organization);
  if (block.attribution && (key === "author" || key === "role" || key === "organization")) {
    return "The one-line attribution credits this quote: clear it to credit an author, a role and an organization.";
  }
  if (key === "attribution" && split) {
    return "An attribution cannot sit next to an author, a role or an organization - the save refuses both: clear it.";
  }
  return null;
}

/**
 * A logo grid's `gap` is the space between packed logos, between
 * rows, and between the framed tiles of equal cells. Where the free space is
 * shared out it is only a minimum: along a row whose logos are spread edge to
 * edge (`stretch`, every row full) or centred in equal unframed cells
 * (`even`, and `start` under `columns`, where it moves each logo a fraction
 * of itself), and down the grid when there is one row or `fill` spreads them.
 */
function logoGridGapInactiveReason(block: DeckLogoGridBlock): string | null {
  if (block.items.length < 2) return "No effect with a single logo: the gap is the space between two.";
  const rows = logoRows(block.items.length, { perRow: block.columns, rows: block.rows });
  if (rows.length > 1 && !block.fill) return null;
  const align = logoGridAlign(block);
  if (align === "stretch" && !shortRows(rows, block.columns).some(Boolean)) {
    return "Stretch spreads each row edge to edge, so the gap is only a minimum here: pick Even, Start or Center to set the spacing.";
  }
  if (logoGridEqualCells(block) && block.framed === false) {
    return "Equal cells centre each logo in its own, so the gap barely moves them: turn Framed on to see it as the space between tiles.";
  }
  return null;
}

/**
 * The same for a field of one ITEM of a block's list: every tile of a `plain`
 * `metrics_grid` draws its value and label only, so its icon,
 * sublabel, delta, trend and delta tone stay saved and show once the grid is cards again.
 */
export function blockItemFieldInactiveReason(block: DeckBlock, key: string): string | null {
  if (block.type === "metrics_grid" && block.variant === "plain" && undrawnOnPlain(key)) {
    return "A plain grid draws each tile's value and label only: this shows again once the grid is back to cards.";
  }
  return null;
}

/** The values `fieldInactiveReason` interpolates, per key. */
export function fieldInactiveValues(slide: PresentationSlide, key: string): Record<string, number> | undefined {
  if (slide.layout === "content" && key === "ratio" && slide.columns) return { count: slide.columns.length };
  return undefined;
}
