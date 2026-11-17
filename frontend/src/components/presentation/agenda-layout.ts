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
 * How an agenda slide is laid out.
 *
 * The spec may leave `columns` and `style` out; the renderer then resolves the
 * SAME rule the backend documents on `AgendaSlide` (`auto_agenda_columns` /
 * `auto_agenda_style` in `presentation_spec.py`, pinned together by
 * `tests/test_presentation_spec_v2.py`): one column up to four items, two for
 * five or six, the 3-column grid - `N. | TITLE` cells with the description
 * under - for seven to nine, and two columns of five for the ten-item maximum,
 * which no 3-column grid holds in full rows. The grid look comes with the
 * three columns; one or two columns keep the numbered chips unless the spec
 * asks for `grid`.
 *
 * The rows are EXPLICIT: the renderer groups the items into rows here (row by
 * row, or down the columns for `flow: "column"`) and draws one grid per row,
 * whose two tracks - head and description - every cell shares through a CSS
 * subgrid. That is what puts the titles of a row on one baseline AND the
 * descriptions under them on another, whatever a neighbour wraps to, while the
 * rows themselves are spread evenly over the body without the free height ever
 * landing between a title and its own description.
 *
 * Kept out of `layouts/body.tsx` so the component file exports components only
 * and the rule can be tested without a page.
 */
import type { AgendaSlide, DeckAgendaFlow, DeckAgendaStyle } from "@/types";

/** The column count that is the grid; the items it needs - two rows filled at
 *  the least (six), three rows at the most (nine). */
export const AGENDA_GRID_COLUMNS = 3;
export const AGENDA_GRID_MIN_ITEMS = 6;
export const AGENDA_GRID_MAX_ITEMS = 9;

export type AgendaColumns = 1 | 2 | 3;

/** Columns an agenda of `count` items takes when the spec leaves it out. */
export function autoAgendaColumns(count: number): AgendaColumns {
  if (count <= 4) return 1;
  if (count <= 6) return 2;
  if (count <= AGENDA_GRID_MAX_ITEMS) return AGENDA_GRID_COLUMNS;
  return 2;
}

/** The slide's columns - its own when set, the auto rule otherwise. */
export function agendaColumns(slide: Pick<AgendaSlide, "items" | "columns">): AgendaColumns {
  return slide.columns ?? autoAgendaColumns(slide.items.length);
}

/** The slide's look - its own when set, the grid at three columns otherwise. */
export function agendaStyle(
  slide: Pick<AgendaSlide, "items" | "columns" | "style">,
): DeckAgendaStyle {
  return slide.style ?? (agendaColumns(slide) === AGENDA_GRID_COLUMNS ? "grid" : "list");
}

/** Rows the grid needs for `count` items over `columns`. */
export function agendaRows(count: number, columns: AgendaColumns): number {
  return Math.max(1, Math.ceil(count / columns));
}

/**
 * The 0-based item indices of each row. `row` fills left to right then down
 * (1 2 3 / 4 5 6 / 7 8 9); `column` runs down the first column, then the next
 * (1 4 7 / 2 5 8 / 3 6 9), a column short of items leaving its last rows
 * short on the right - never a row missing in the middle.
 */
export function agendaRowGroups(count: number, columns: AgendaColumns, flow: DeckAgendaFlow): number[][] {
  const rows = agendaRows(count, columns);
  const groups: number[][] = [];
  for (let row = 0; row < rows; row++) {
    const group: number[] = [];
    for (let column = 0; column < columns; column++) {
      const index = flow === "column" ? column * rows + row : row * columns + column;
      if (index < count) group.push(index);
    }
    if (group.length) groups.push(group);
  }
  return groups;
}

interface ResolvedAgendaLayout {
  columns: AgendaColumns;
  /** The item indices per row, in the fill order the flow asks for. */
  rows: number[][];
  style: DeckAgendaStyle;
  flow: DeckAgendaFlow;
  distribute: boolean;
  /** Any item carries a description: every cell reserves the line so the rows
   *  keep one height and the descriptions of a row start on one line. */
  hasDescriptions: boolean;
}

/**
 * The `plain` agenda of a template that opts into it (`design.agenda`,
 * spec 3.11): a numbered grid without cards, two columns, three from seven
 * items, the two-digit number in front of each title. `distribute` is read as
 * on every other agenda: the rows spread over the body height unless the
 * slide sets `distribute: false` (the brand
 * template had packed them on top whatever the slide asked).
 */
export function plainAgendaColumns(count: number): AgendaColumns {
  return count >= 7 ? AGENDA_GRID_COLUMNS : 2;
}

export function resolveAgendaLayout(slide: AgendaSlide, composition?: "plain"): ResolvedAgendaLayout {
  if (composition === "plain") {
    const columns = slide.columns ?? plainAgendaColumns(slide.items.length);
    const flow: DeckAgendaFlow = columns > 1 && slide.flow === "column" ? "column" : "row";
    return {
      columns,
      rows: agendaRowGroups(slide.items.length, columns, flow),
      style: "list",
      flow,
      distribute: slide.distribute !== false,
      hasDescriptions: slide.items.some((item) => Boolean(item.description)),
    };
  }
  const columns = agendaColumns(slide);
  // A single column has one fill order; `column` only means something once
  // there is a second column to run down into.
  const flow: DeckAgendaFlow = columns > 1 && slide.flow === "column" ? "column" : "row";
  return {
    columns,
    rows: agendaRowGroups(slide.items.length, columns, flow),
    style: agendaStyle(slide),
    flow,
    distribute: slide.distribute !== false,
    hasDescriptions: slide.items.some((item) => Boolean(item.description)),
  };
}
