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
 * The `dashboard` layout: a row of KPI tiles over a three-column grid
 * of panels - two equal columns with `panel_columns: 2` - each panel
 * holding one chart, table or KPI block.
 *
 * Fits by construction rather than by shrinking: the tile row takes its own
 * height and the panel grid divides what is left into equal rows
 * (`minmax(0, 1fr)`), so a panel never grows with its content - a chart is
 * drawn at the room it gets, a table taller than its panel is clipped and
 * reported by the layout audit as `clipped`, never painted under the footer.
 */
import type { DashboardSlide, DeckDashboardPanel } from "@/types";

import { Block } from "../blocks";
import { MetricTile } from "../blocks/metrics";
import { InlineMarkup } from "../inline-markup";
import { FooterSlot, SlideBodyArea, SlideHeader } from "./frame";

/** Columns of the panel grid when the slide does not say (`DASHBOARD_GRID_COLUMNS`). */
const DEFAULT_GRID_COLUMNS = 3;

const panelSpan = (panel: DeckDashboardPanel, columns: number): number =>
  Math.min(Math.max(panel.span ?? 1, 1), columns);

/**
 * Rows the panels occupy under CSS grid auto-placement (sparse: a panel that
 * does not fit the rest of its row starts the next one). Mirrors the backend's
 * `_grid_rows`, which caps a deck at two rows, so the validator's fit budget
 * and the drawing agree; the grid divides the room by this count.
 */
function dashboardRows(panels: readonly DeckDashboardPanel[], columns: number): number {
  let rows = 0;
  let used = columns;
  for (const panel of panels) {
    const span = panelSpan(panel, columns);
    if (used + span > columns) {
      rows += 1;
      used = 0;
    }
    used += span;
  }
  return Math.max(rows, 1);
}

/**
 * An `<article>`, never a `<section>`: reveal reads a `<section>` nested in a
 * slide as a VERTICAL slide, so section panels turned a dashboard into a
 * stack of three slides shown one at a time (and the toolbar counted them).
 */
function Panel({ panel, index, columns }: { panel: DeckDashboardPanel; index: number; columns: number }) {
  return (
    <article className="deck-panel" data-span={panelSpan(panel, columns)}>
      {panel.title ? (
        <InlineMarkup as="h3" className="deck-panel-title" value={panel.title} field={`panels.${index}.title`} />
      ) : null}
      <div className="deck-panel-body">
        <Block block={panel.block} path={`panels.${index}.block`} />
      </div>
    </article>
  );
}

export function DashboardLayout({ slide }: { slide: DashboardSlide }) {
  const columns = slide.panel_columns ?? DEFAULT_GRID_COLUMNS;
  return (
    <>
      <SlideHeader title={slide.title} subtitle={slide.subtitle} />
      <SlideBodyArea>
        <div className="deck-dashboard" data-rows={dashboardRows(slide.panels, columns)}>
          <div className="deck-dashboard-tiles" data-count={slide.tiles.length} data-size={slide.tile_size ?? "md"}>
            {slide.tiles.map((tile, index) => (
              <MetricTile key={index} metric={tile} field={`tiles.${index}`} />
            ))}
          </div>
          <div className="deck-dashboard-panels" data-columns={columns}>
            {slide.panels.map((panel, index) => (
              <Panel key={index} panel={panel} index={index} columns={columns} />
            ))}
          </div>
        </div>
      </SlideBodyArea>
      <FooterSlot block={slide.footer_block} />
    </>
  );
}
