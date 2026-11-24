// @vitest-environment node
import { describe, expect, it as test } from 'vitest';
import {
  AGENDA_GRID_COLUMNS,
  AGENDA_GRID_MAX_ITEMS,
  AGENDA_GRID_MIN_ITEMS,
  agendaColumns,
  agendaRowGroups,
  agendaRows,
  agendaStyle,
  autoAgendaColumns,
  resolveAgendaLayout,
  type AgendaColumns,
} from '../src/components/presentation/agenda-layout';
import type { AgendaSlide, DeckAgendaFlow, DeckAgendaItem } from '../src/types';

// ---------------------------------------------------------------------------
// Agenda layout resolver - columns, look, rows, table-driven.
//
// `agenda-layout.ts` is pure TypeScript (no DOM), so it is exercised here
// directly, without a page, like `logo-position.test.ts`. The backend owns the
// rule (`auto_agenda_columns` / `auto_agenda_style` in `presentation_spec.py`)
// and `tests/test_presentation_spec_v2.py` pins the two SOURCES together; this
// spec runs the frontend half, so a threshold moved on one side only fails a
// test instead of drawing the grid where the tool description said it would
// not. The rendered geometry (subgrid rows, distribution) is CSS and is
// checked on a live deck, not here.
// ---------------------------------------------------------------------------

/** The documented rule: item count -> columns when the spec leaves it out.
 *  The SAME table as `test_auto_agenda_columns_follows_the_documented_rule`. */
const AUTO_COLUMNS: readonly (readonly [count: number, columns: AgendaColumns])[] = [
  [2, 1], [3, 1], [4, 1], [5, 2], [6, 2], [7, 3], [8, 3], [9, 3], [10, 2],
];

const COUNTS = AUTO_COLUMNS.map(([count]) => count);
const COLUMN_COUNTS: readonly AgendaColumns[] = [1, 2, 3];
const FLOWS: readonly DeckAgendaFlow[] = ['row', 'column'];

const items = (count: number, withDescriptions = false): DeckAgendaItem[] =>
  Array.from({ length: count }, (_, index) => ({
    title: `Section ${index + 1}`,
    ...(withDescriptions ? { description: `What section ${index + 1} covers` } : {}),
  }));

const agenda = (count: number, extra: Partial<AgendaSlide> = {}): AgendaSlide => ({
  layout: 'agenda',
  items: items(count),
  ...extra,
});

/** `groups` as the eye reads them: 1-based numbers, one line per row. */
const picture = (groups: number[][]): string => groups.map((row) => row.map((index) => index + 1).join(' ')).join(' / ');

describe('Presentations - agenda layout resolver', () => {
  test('the grid constants are the backend rule', () => {
    expect([AGENDA_GRID_COLUMNS, AGENDA_GRID_MIN_ITEMS, AGENDA_GRID_MAX_ITEMS]).toEqual([3, 6, 9]);
  });

  test('an omitted columns follows the documented thresholds', () => {
    for (const [count, columns] of AUTO_COLUMNS) {
      expect(autoAgendaColumns(count), `${count} items`).toBe(columns);
      expect(agendaColumns(agenda(count)), `${count} items`).toBe(columns);
    }
  });

  test('an explicit columns wins over the auto rule', () => {
    for (const count of COUNTS) {
      for (const columns of COLUMN_COUNTS) {
        expect(agendaColumns(agenda(count, { columns })), `${count} items, columns=${columns}`).toBe(columns);
      }
    }
  });

  test('the grid look comes with three columns and nowhere else', () => {
    for (const [count, columns] of AUTO_COLUMNS) {
      expect(agendaStyle(agenda(count)), `${count} items auto`).toBe(columns === 3 ? 'grid' : 'list');
    }
    for (const columns of COLUMN_COUNTS) {
      expect(agendaStyle(agenda(6, { columns })), `columns=${columns}`).toBe(columns === 3 ? 'grid' : 'list');
    }
    // An explicit style overrides the columns in both directions.
    expect(agendaStyle(agenda(9, { columns: 3, style: 'list' }))).toBe('list');
    expect(agendaStyle(agenda(4, { columns: 2, style: 'grid' }))).toBe('grid');
    expect(agendaStyle(agenda(2, { style: 'grid' }))).toBe('grid');
  });

  test('rows are the ceiling of items over columns, one at the least', () => {
    expect(agendaRows(9, 3)).toBe(3);
    expect(agendaRows(7, 3)).toBe(3);
    expect(agendaRows(6, 3)).toBe(2);
    expect(agendaRows(5, 2)).toBe(3);
    expect(agendaRows(10, 2)).toBe(5);
    expect(agendaRows(4, 1)).toBe(4);
    expect(agendaRows(0, 3)).toBe(1);
  });

  test('row flow reads left to right, then down - the PPTX order', () => {
    expect(picture(agendaRowGroups(9, 3, 'row'))).toBe('1 2 3 / 4 5 6 / 7 8 9');
    expect(picture(agendaRowGroups(6, 3, 'row'))).toBe('1 2 3 / 4 5 6');
    // Seven and eight leave the LAST row short, never a row in the middle.
    expect(picture(agendaRowGroups(7, 3, 'row'))).toBe('1 2 3 / 4 5 6 / 7');
    expect(picture(agendaRowGroups(8, 3, 'row'))).toBe('1 2 3 / 4 5 6 / 7 8');
    expect(picture(agendaRowGroups(5, 2, 'row'))).toBe('1 2 / 3 4 / 5');
    expect(picture(agendaRowGroups(10, 2, 'row'))).toBe('1 2 / 3 4 / 5 6 / 7 8 / 9 10');
    expect(picture(agendaRowGroups(3, 1, 'row'))).toBe('1 / 2 / 3');
  });

  test('column flow runs down the first column first', () => {
    expect(picture(agendaRowGroups(9, 3, 'column'))).toBe('1 4 7 / 2 5 8 / 3 6 9');
    expect(picture(agendaRowGroups(6, 3, 'column'))).toBe('1 3 5 / 2 4 6');
    // A column short of items leaves its last rows short on the RIGHT.
    expect(picture(agendaRowGroups(7, 3, 'column'))).toBe('1 4 7 / 2 5 / 3 6');
    expect(picture(agendaRowGroups(8, 3, 'column'))).toBe('1 4 7 / 2 5 8 / 3 6');
    expect(picture(agendaRowGroups(5, 2, 'column'))).toBe('1 4 / 2 5 / 3');
    // One column has one order: both flows read the same.
    expect(agendaRowGroups(4, 1, 'column')).toEqual(agendaRowGroups(4, 1, 'row'));
  });

  test('every item lands in exactly one cell, whatever the count, columns and flow', () => {
    for (const count of COUNTS) {
      for (const columns of COLUMN_COUNTS) {
        // What the validator lets a deck store: three columns hold 6-9 items.
        const storable = columns !== AGENDA_GRID_COLUMNS || (count >= AGENDA_GRID_MIN_ITEMS && count <= AGENDA_GRID_MAX_ITEMS);
        for (const flow of FLOWS) {
          const label = `${count} items, columns=${columns}, flow=${flow}`;
          const groups = agendaRowGroups(count, columns, flow);
          expect(groups.length, label).toBe(agendaRows(count, columns));
          const seen = groups.flat();
          expect([...seen].sort((a, b) => a - b), label).toEqual(Array.from({ length: count }, (_, i) => i));
          for (const group of groups) {
            expect(group.length, label).toBeGreaterThan(0);
            expect(group.length, label).toBeLessThanOrEqual(columns);
          }
          // The rows never get LONGER down the grid: a short row is a last one.
          for (let row = 1; row < groups.length; row++) {
            expect(groups[row].length, label).toBeLessThanOrEqual(groups[row - 1].length);
          }
          // Row flow fills every row but the last; column flow does too for
          // every count a deck can store (4 items down 3 columns would leave
          // the third column empty - a combination the validator refuses).
          if (count >= columns && (flow === 'row' || storable)) expect(groups[0].length, label).toBe(columns);
          if (flow === 'row') {
            for (let row = 0; row < groups.length - 1; row++) expect(groups[row].length, label).toBe(columns);
            expect(seen, label).toEqual(Array.from({ length: count }, (_, i) => i));
          }
        }
      }
    }
  });

  test('a nine-item agenda left on auto resolves to the 3 x 3 grid', () => {
    const layout = resolveAgendaLayout(agenda(9));
    expect(layout).toMatchObject({ columns: 3, style: 'grid', flow: 'row', distribute: true, hasDescriptions: false });
    expect(picture(layout.rows)).toBe('1 2 3 / 4 5 6 / 7 8 9');
  });

  test('a deck saved before the grid keeps its chips', () => {
    // Two columns saved explicitly, or an auto count under seven: the list
    // look, rows grouped the same way the CSS used to wrap the chips.
    expect(resolveAgendaLayout(agenda(6, { columns: 2 }))).toMatchObject({ columns: 2, style: 'list', flow: 'row' });
    expect(resolveAgendaLayout(agenda(4))).toMatchObject({ columns: 1, style: 'list', flow: 'row' });
    expect(picture(resolveAgendaLayout(agenda(4)).rows)).toBe('1 / 2 / 3 / 4');
  });

  test('the switches are honoured', () => {
    expect(resolveAgendaLayout(agenda(9, { style: 'list' })).style).toBe('list');
    expect(resolveAgendaLayout(agenda(6, { columns: 2, style: 'grid' })).style).toBe('grid');
    expect(resolveAgendaLayout(agenda(9, { distribute: false })).distribute).toBe(false);
    expect(resolveAgendaLayout(agenda(9, { distribute: true })).distribute).toBe(true);
    // The brand template's plain agenda reads the flag too.
    expect(resolveAgendaLayout(agenda(6, { distribute: true }), 'plain').distribute).toBe(true);
    expect(resolveAgendaLayout(agenda(6), 'plain').distribute).toBe(true);
    expect(resolveAgendaLayout(agenda(6, { distribute: false }), 'plain').distribute).toBe(false);
    const down = resolveAgendaLayout(agenda(9, { flow: 'column' }));
    expect(down.flow).toBe('column');
    expect(picture(down.rows)).toBe('1 4 7 / 2 5 8 / 3 6 9');
  });

  test('a single column has one fill order: column flow collapses to row', () => {
    for (const count of [2, 3, 4]) {
      const layout = resolveAgendaLayout(agenda(count, { flow: 'column' }));
      expect(layout.columns, `${count} items`).toBe(1);
      expect(layout.flow, `${count} items`).toBe('row');
    }
    const forced = resolveAgendaLayout(agenda(9, { columns: 1, flow: 'column' }));
    expect(forced.flow).toBe('row');
    expect(forced.rows.length).toBe(9);
  });

  test('one described item reserves the description line for every cell', () => {
    expect(resolveAgendaLayout(agenda(9)).hasDescriptions).toBe(false);
    expect(resolveAgendaLayout({ layout: 'agenda', items: items(9, true) }).hasDescriptions).toBe(true);
    const one = items(6);
    one[3] = { ...one[3], description: 'The only one that says more' };
    expect(resolveAgendaLayout({ layout: 'agenda', items: one }).hasDescriptions).toBe(true);
    // An empty description is nothing to reserve for.
    expect(resolveAgendaLayout({ layout: 'agenda', items: [{ title: 'A', description: '' }, { title: 'B' }] }).hasDescriptions).toBe(false);
  });
});
