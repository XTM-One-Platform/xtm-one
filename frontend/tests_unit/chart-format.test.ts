// @vitest-environment node
import { describe, expect, it as test } from 'vitest';
import {
  AXIS_COMPACT_FROM,
  MAX_LABELLED_VALUES,
  axisCeiling,
  axisInk,
  axisReading,
  axisSurface,
  axisView,
  compactNumber,
  dualAxisScales,
  formatChartValue,
  gaugeReading,
  hasSecondaryAxis,
  hasTargetLine,
  isStacked,
  labelSurface,
  legendIdentities,
  legendPosition,
  legendShown,
  paletteColor,
  resolveHidden,
  secondaryTargetLegendName,
  toggleHidden,
  seriesColors,
  targetLegendName,
  valuesShown,
} from '../src/components/presentation/blocks/chart-format';
import type { DeckChartBlock, DeckChartSeries } from '../src/types';

// ---------------------------------------------------------------------------
// Chart value formatting and reading rules - pure, table-driven.
//
// `chart-format.ts` is the ONE rule for how a chart writes a number (axis
// ticks, value labels, tooltips, the gauge dial), which colour a series takes
// and what the legend names; the PPTX writer mirrors these rules when it
// builds the editable chart. It is plain TypeScript with no DOM, so it is
// exercised here directly, like `logo-position.test.ts`. The rendered figure
// (the same strings in SVG glyphs, the palette as computed colours) is
// asserted on a live deck in `presentation-charts.spec.ts`.
// ---------------------------------------------------------------------------

/** Every string here is written in one locale so the table reads the same everywhere. */
const LOCALE = 'en-US';

/** A chart block with the fields the helpers read; the rest is filled in. */
const block = (extra: Partial<DeckChartBlock>): DeckChartBlock => ({
  type: 'chart',
  kind: 'bar',
  series: [{ name: 'Series', values: [1, 2, 3] }],
  categories: ['A', 'B', 'C'],
  ...extra,
});

const series = (name: string, tone?: DeckChartSeries['tone']): DeckChartSeries => ({
  name,
  values: [1],
  ...(tone ? { tone } : {}),
});

describe('Chart value formatting', () => {
  test('should write a currency compact with the sign of its unit, glued for a symbol and spaced for a code', () => {
    const cases: ReadonlyArray<[number, string | undefined, string]> = [
      // The BOD deck's own numbers: the two the issue names.
      [16_500_000, 'EUR', '€16.5M'],
      [527_100, 'EUR', '€527.1k'],
      // EUR is the default currency: an unset unit reads the same.
      [16_500_000, undefined, '€16.5M'],
      // Known ISO codes become their sign; a symbol typed as is stays as is.
      [8_300_000, 'USD', '$8.3M'],
      [1_250, 'GBP', '£1.3k'],
      [4_000_000_000, 'JPY', '¥4B'],
      [900, '$', '$900'],
      // An unknown code is a word: it keeps a space before the amount.
      [16_500_000, 'CHF', 'CHF 16.5M'],
      // A negative amount keeps its sign in front of the currency sign.
      [-1_200_000, 'EUR', '-€1.2M'],
    ];
    for (const [value, unit, expected] of cases) {
      expect(formatChartValue(value, { format: 'currency', unit }, LOCALE), `${value} ${unit ?? '(default)'}`).toBe(
        expected,
      );
    }
  });

  test('should write every amount of a pinned unit scale in its one letter, with the decimals', () => {
    const pinned = { format: 'currency', unit: 'EUR', decimals: 2, unit_scale: 'M' } as const;
    const cases: ReadonlyArray<[number, string]> = [
      [2_500_000, '€2.50M'],
      [1_250_000, '€1.25M'],
      [-250_000, '-€0.25M'],
      [3_500_000, '€3.50M'],
      // Too small for the letter: written with the decimals, never dropped.
      [1_000, '€0.00M'],
    ];
    for (const [value, expected] of cases) {
      expect(formatChartValue(value, pinned, LOCALE), String(value)).toBe(expected);
    }
    // `auto` is today's rule; a pie and a percent keep their own.
    expect(formatChartValue(-250_000, { ...pinned, unit_scale: 'auto' }, LOCALE)).toBe('-€250.00k');
    expect(formatChartValue(480_000, { ...pinned, decimals: 1, kind: 'pie' }, LOCALE)).toBe('€480.0k');
    expect(formatChartValue(86, { format: 'percent', unit_scale: 'M' }, LOCALE)).toBe('86%');
  });

  test('should write a percent as the value itself with a percent sign, whatever the unit says', () => {
    expect(formatChartValue(86, { format: 'percent' }, LOCALE)).toBe('86%');
    expect(formatChartValue(86, { format: 'percent', unit: 'EUR' }, LOCALE)).toBe('86%');
    expect(formatChartValue(12.345, { format: 'percent' }, LOCALE)).toBe('12.3%');
    expect(formatChartValue(0, { format: 'percent' }, LOCALE)).toBe('0%');
  });

  test('should write a number with the locale thousands separator and the unit on labels only', () => {
    // A one-character unit is glued (`3.4h`); a word keeps a space (`1,204 users`).
    expect(formatChartValue(1204, { unit: 'users' }, LOCALE)).toBe('1,204 users');
    expect(formatChartValue(3.4, { unit: 'h' }, LOCALE)).toBe('3.4h');
    expect(formatChartValue(12_480, {}, LOCALE)).toBe('12,480');
    // The axis drops the unit: the label carries it once, the ticks would repeat it five times.
    expect(formatChartValue(1204, { unit: 'users' }, LOCALE, 'axis')).toBe('1,204');
    expect(formatChartValue(12_480, { unit: 'users' }, LOCALE, 'axis_compact')).toBe('12.5k');
    // Another locale, another separator: the same rule, the person's own digits.
    expect(formatChartValue(1204.5, {}, 'de-DE')).toBe('1.204,5');
    // The app's own locale ids (`fr-fr`, the i18n context's) read the same way.
    expect(formatChartValue(1204.5, {}, 'fr-fr')).toBe(formatChartValue(1204.5, {}, 'fr-FR'));
    expect(formatChartValue(1204.5, {}, 'fr-fr')).not.toBe(formatChartValue(1204.5, {}, 'en-us'));
    // Nothing is written for a value that is not a number.
    expect(formatChartValue(Number.NaN, {}, LOCALE)).toBe('');
  });

  test('should compact a number at one decimal at most, trailing zero dropped', () => {
    const cases: ReadonlyArray<[number, string]> = [
      [850, '850'],
      [1_000, '1k'],
      [527_100, '527.1k'],
      [1_000_000, '1M'],
      [16_500_000, '16.5M'],
      [4_000_000_000, '4B'],
      [-2_500, '-2.5k'],
    ];
    for (const [value, expected] of cases) expect(compactNumber(value, LOCALE), `${value}`).toBe(expected);
  });

  test('should decide once, from its ceiling, whether an axis writes compact ticks', () => {
    expect(axisSurface(AXIS_COMPACT_FROM - 1)).toBe('axis');
    expect(axisSurface(AXIS_COMPACT_FROM)).toBe('axis_compact');
    expect(axisSurface(-AXIS_COMPACT_FROM)).toBe('axis_compact');
    // The ceiling is the tallest point of a grouped chart, the tallest STACK of
    // a stacked one, and the target when it stands above both.
    const grouped = block({ series: [series('A'), series('B')] });
    grouped.series[0].values = [3, 9, 4];
    grouped.series[1].values = [8, 2, 1];
    expect(axisCeiling(grouped)).toBe(9);
    expect(axisCeiling({ ...grouped, kind: 'stacked_bar' })).toBe(11);
    expect(axisCeiling({ ...grouped, target: 20 })).toBe(20);
    // A negative value never pulls a stack down.
    grouped.series[1].values = [-8, 2, 1];
    expect(axisCeiling({ ...grouped, kind: 'stacked_bar' })).toBe(11);
  });
});

describe('Chart reading rules', () => {
  test('should give the template palette to the untoned series in order and a semantic colour to a toned one', () => {
    const colors = seriesColors([series('Actual'), series('Plan', 'muted'), series('Forecast')]);
    expect(colors).toEqual(['var(--deck-chart-1)', 'var(--deck-muted)', 'var(--deck-chart-2)']);
    // The palette has six colours and cycles past them.
    expect(paletteColor(0)).toBe('var(--deck-chart-1)');
    expect(paletteColor(5)).toBe('var(--deck-chart-6)');
    expect(paletteColor(6)).toBe('var(--deck-chart-1)');
  });

  test('should draw a target line on an axis chart only, named in the legend by its label or its value', () => {
    expect(hasTargetLine({ kind: 'bar', target: 30 })).toBe(true);
    expect(hasTargetLine({ kind: 'line', target: 0 })).toBe(true);
    expect(hasTargetLine({ kind: 'bar' })).toBe(false);
    // A gauge reads its target as its maximum, a pie has no axis to draw on.
    expect(hasTargetLine({ kind: 'gauge', target: 100 })).toBe(false);
    expect(hasTargetLine({ kind: 'pie', target: 100 })).toBe(false);

    expect(targetLegendName(block({ target: 30_000_000, goal_label: 'Plan', format: 'currency' }), LOCALE)).toBe('Plan');
    expect(targetLegendName(block({ target: 30_000_000, format: 'currency', unit: 'EUR' }), LOCALE)).toBe('€30M');
    expect(targetLegendName(block({ kind: 'donut', target: 5, format: 'number' }), LOCALE)).toBeNull();
    // As precise as the values it is read against: 1.25 among 0.96 and 1.57, never 1.3.
    const ratio = block({ kind: 'line', series: [{ name: 'Burn multiple', values: [0.96, 1.57, 1.27] }], target: 1.25 });
    expect(targetLegendName(ratio, LOCALE)).toBe('1.25');
  });

  test('should show the legend whenever there is more than one thing to tell apart', () => {
    const one = block({});
    expect(legendShown(one)).toBe(false);
    expect(legendShown(block({ series: [series('A'), series('B')] }))).toBe(true);
    expect(legendShown(block({ target: 10 }))).toBe(true);
    // A pie names its slices in the legend; a gauge has one value and no legend.
    expect(legendShown(block({ kind: 'pie' }))).toBe(true);
    expect(legendShown(block({ kind: 'gauge' }))).toBe(false);
    // The block's own word wins over every rule.
    expect(legendShown(block({ kind: 'gauge', show_legend: true }))).toBe(true);
    expect(legendShown(block({ series: [series('A'), series('B')], show_legend: false }))).toBe(false);
  });

  test('should sit the legend at the top unless the block says otherwise, whatever is drawn', () => {
    expect(legendPosition(block({}))).toBe('top');
    expect(legendPosition(block({ legend_position: 'bottom' }))).toBe('bottom');
    expect(legendPosition(block({ legend_position: 'right' }))).toBe('right');
    // A placement, not a switch: it says nothing about whether a legend is drawn.
    expect(legendShown(block({ kind: 'gauge', legend_position: 'bottom' }))).toBe(false);
    expect(legendShown(block({ series: [series('A'), series('B')], show_legend: false, legend_position: 'right' }))).toBe(
      false,
    );
  });

  test('should stack a stacked_bar always and a bar or area only on request', () => {
    expect(isStacked({ kind: 'stacked_bar' })).toBe(true);
    expect(isStacked({ kind: 'bar' })).toBe(false);
    expect(isStacked({ kind: 'bar', stacked: true })).toBe(true);
    expect(isStacked({ kind: 'area', stacked: true })).toBe(true);
    expect(isStacked({ kind: 'line', stacked: true })).toBe(false);
  });

  test('should write values on the figure when asked for, a pie or a donut only up to its slice limit', () => {
    const categories = (count: number) => Array.from({ length: count }, (_, index) => `C${index}`);
    const values = (count: number) => Array.from({ length: count }, (_, index) => index);
    // An axis chart is never capped by a count: three series over
    // twelve categories are 36 values, and the drawing decides from its room.
    const grouped = block({
      series: ['A', 'B', 'C'].map((name) => ({ name, values: values(12) })),
      categories: categories(12),
    });
    expect(valuesShown(grouped)).toBe(true);
    expect(valuesShown({ ...grouped, kind: 'stacked_bar' })).toBe(true);
    // A pie plots one series: its slices count, up to the limit.
    expect(valuesShown(block({ kind: 'pie', categories: categories(MAX_LABELLED_VALUES) }))).toBe(true);
    expect(valuesShown(block({ kind: 'donut', categories: categories(MAX_LABELLED_VALUES + 1) }))).toBe(false);
    // The gauge always writes its reading; `show_values: false` silences any chart.
    expect(valuesShown(block({ kind: 'gauge' }))).toBe(true);
    expect(valuesShown(block({ kind: 'gauge', show_values: false }))).toBe(false);
    expect(valuesShown({ ...grouped, show_values: false })).toBe(false);
  });

  test('should write the values on an axis chart compact once its axis is', () => {
    // `2.2M users` on the bar, never `2,200,000 users`; the tooltip keeps every digit.
    const users = block({ unit: 'users', series: [{ name: 'Users', values: [1_200_000, 2_200_000] }], categories: ['Q1', 'Q2'] });
    expect(labelSurface(users)).toBe('label_compact');
    expect(formatChartValue(2_200_000, users, LOCALE, labelSurface(users))).toBe('2.2M users');
    expect(formatChartValue(2_200_000, users, LOCALE)).toBe('2,200,000 users');
    // Below the axis threshold the labels keep their digits.
    const small = block({ unit: 'users', series: [{ name: 'Users', values: [120, 1204] }], categories: ['Q1', 'Q2'] });
    expect(labelSurface(small)).toBe('label');
    expect(formatChartValue(1204, small, LOCALE, labelSurface(small))).toBe('1,204 users');
    // A stack reaches its total: `12,480` stacked from three series is compact.
    const stack = block({
      kind: 'stacked_bar',
      series: [
        { name: 'A', values: [1900] },
        { name: 'B', values: [4600] },
        { name: 'C', values: [5980] },
      ],
      categories: ['Q3'],
    });
    expect(formatChartValue(12_480, stack, LOCALE, labelSurface(stack))).toBe('12.5k');
  });

  test('should read a gauge against its target, or 100 for a percent dial, clamped to the dial', () => {
    expect(gaugeReading({ series: [{ name: 'NRR', values: [86] }], format: 'percent' })).toEqual({
      value: 86,
      max: 100,
      ratio: 0.86,
    });
    expect(gaugeReading({ series: [{ name: 'Done', values: [30] }], target: 40 })).toEqual({
      value: 30,
      max: 40,
      ratio: 0.75,
    });
    // Past the dial the needle stops at the end; below zero it stays at the start.
    expect(gaugeReading({ series: [{ name: 'Over', values: [130] }], format: 'percent' }).ratio).toBe(1);
    expect(gaugeReading({ series: [{ name: 'Under', values: [-5] }], format: 'percent' }).ratio).toBe(0);
    // A plain number with no target is read against itself: a full dial.
    expect(gaugeReading({ series: [{ name: 'Count', values: [42] }] })).toEqual({ value: 42, max: 42, ratio: 1 });
    // An empty dial reads zero of one, never zero of zero.
    expect(gaugeReading({ series: [] })).toEqual({ value: 0, max: 1, ratio: 0 });
  });
});

describe('Two value axes', () => {
  /** New logos and ASP: a count on the left-hand axis, a price on the right-hand one. */
  const logos = block({
    kind: 'combo',
    series: [
      { name: 'New logos', kind: 'bar', values: [24, 34, 42, 56] },
      { name: 'ASP', kind: 'line', axis: 'secondary', values: [240_000, 330_000, 420_000, 540_000] },
    ],
    categories: ['Q4 25', 'Q1 26', 'Q2 26', 'Q3 26'],
    secondary_format: 'currency',
    secondary_unit: 'EUR',
  });
  /** Burn and net new ARR: millions on the left, the burn multiple on the right. */
  const burn = block({
    kind: 'combo',
    format: 'currency',
    unit: 'EUR',
    series: [
      { name: 'Burn', kind: 'bar', tone: 'error', values: [3_400_000, 3_900_000, 4_600_000, 3_100_000] },
      { name: 'Net new ARR', kind: 'bar', tone: 'success', values: [2_100_000, 3_400_000, 4_900_000, 6_200_000] },
      { name: 'Burn multiple', kind: 'line', axis: 'secondary', values: [1.62, 1.15, 0.94, 0.5] },
    ],
    categories: ['Q4 25', 'Q1 26', 'Q2 26', 'Q3 26'],
    secondary_format: 'number',
    secondary_unit: 'x',
    secondary_target: 1,
  });

  test('should read a series on the right-hand axis of a bar, line or combo chart only', () => {
    expect(hasSecondaryAxis(logos)).toBe(true);
    expect(hasSecondaryAxis({ ...logos, kind: 'line' })).toBe(true);
    expect(hasSecondaryAxis({ ...logos, kind: 'stacked_bar' })).toBe(false);
    expect(hasSecondaryAxis({ ...logos, horizontal: true })).toBe(false);
    expect(hasSecondaryAxis(block({}))).toBe(false);
  });

  test('should give each axis its own series, target and format', () => {
    expect(axisReading(logos, 'primary')).toEqual({ format: undefined, unit: undefined });
    expect(axisReading(logos, 'secondary')).toEqual({ format: 'currency', unit: 'EUR' });
    const right = axisView(burn, 'secondary');
    expect(right.series.map((entry) => entry.name)).toEqual(['Burn multiple']);
    expect([right.target, right.format, right.unit]).toEqual([1, 'number', 'x']);
    const left = axisView(burn, 'primary');
    expect(left.series.map((entry) => entry.name)).toEqual(['Burn', 'Net new ARR']);
    expect(left.target).toBeUndefined();
    // A chart on one axis is its own view.
    const single = block({});
    expect(axisView(single, 'primary')).toBe(single);
  });

  test('should cut both axes into the same round steps so they share the gridlines', () => {
    const tall = dualAxisScales(logos, 5);
    expect(tall.primary.ticks).toEqual([0, 20, 40, 60]);
    expect(tall.secondary.ticks).toEqual([0, 200_000, 400_000, 600_000]);
    const money = dualAxisScales(burn, 5);
    expect(money.primary.ticks).toEqual([0, 2_000_000, 4_000_000, 6_000_000, 8_000_000]);
    expect(money.secondary.ticks).toEqual([0, 0.5, 1, 1.5, 2]);
    // A short panel keeps three ticks on both sides.
    const dense = dualAxisScales(logos, 3);
    expect(dense.primary.ticks).toHaveLength(3);
    expect(dense.secondary.ticks).toHaveLength(3);
    // A percent axis under 100 still tops out at 100, a negative value is reached.
    const share = block({
      series: [
        { name: 'Net', values: [-1_200, 800, 2_400] },
        { name: 'Margin', axis: 'secondary', values: [12, 31, 64] },
      ],
      secondary_format: 'percent',
    });
    const scales = dualAxisScales(share, 5);
    expect(scales.secondary.max).toBe(100);
    expect(scales.primary.min).toBeLessThanOrEqual(-1_200);
    expect(scales.primary.ticks).toHaveLength(scales.secondary.ticks.length);
  });

  test('should write each axis in its own unit, its ticks included', () => {
    expect(formatChartValue(1.5, axisReading(burn, 'secondary'), LOCALE, 'axis', { axisUnit: true })).toBe('1.5x');
    expect(formatChartValue(1.5, axisReading(burn, 'secondary'), LOCALE, 'axis')).toBe('1.5');
    expect(formatChartValue(1.62, axisReading(burn, 'secondary'), LOCALE)).toBe('1.6x');
    expect(formatChartValue(540_000, axisReading(logos, 'secondary'), LOCALE, 'axis', { axisUnit: true })).toBe('€540k');
    expect(secondaryTargetLegendName(burn, LOCALE)).toBe('1x');
    expect(secondaryTargetLegendName(logos, LOCALE)).toBeNull();
  });

  test('should ink an axis like its series when they wear one colour, muted otherwise', () => {
    expect(axisInk(logos, 'primary')).toBe('var(--deck-chart-1)');
    expect(axisInk(logos, 'secondary')).toBe('var(--deck-chart-2)');
    // Burn and net new ARR wear two tones: the left-hand axis stays muted.
    expect(axisInk(burn, 'primary')).toBe('var(--deck-muted)');
    expect(axisInk(burn, 'secondary')).toBe('var(--deck-chart-1)');
  });
});

describe('Chart legend hidden series', () => {
  const ids = (...names: string[]) => legendIdentities(names);
  const hide = (...names: string[]) => new Set(ids(...names));
  /** The toggles a click leaves, read back as the positions they hide in `names`. */
  const click = (hidden: ReadonlySet<string>, index: number, ...names: string[]) =>
    [...resolveHidden(toggleHidden(hidden, index, ids(...names)), ids(...names))].sort();

  test('should tell a repeated name apart, and forget its toggle once the entries change', () => {
    const both = ids('Revenue', 'Revenue', 'Cost');
    expect(new Set(both).size).toBe(3);
    // The second Revenue hidden: it stays hidden while the entries are unchanged.
    const second = new Set([both[1]]);
    expect([...resolveHidden(second, ids('Revenue', 'Revenue', 'Cost'))]).toEqual([1]);
    // The first Revenue hidden, then removed: the survivor is not hidden in its place.
    expect([...resolveHidden(new Set([both[0]]), ids('Revenue', 'Cost'))]).toEqual([]);
    // The second one hidden, the first removed: which one survived is unknown, so nothing is hidden.
    expect([...resolveHidden(second, ids('Revenue', 'Cost'))]).toEqual([]);
    // A unique name keeps its toggle across the same edit.
    expect([...resolveHidden(new Set([both[2]]), ids('Revenue', 'Cost'))]).toEqual([1]);
  });

  test('should keep a hidden series hidden, and no other, when an edit moves it', () => {
    // B hidden in [A, B, C]; the edit removes A: B is still the hidden one.
    expect([...resolveHidden(hide('B'), ids('B', 'C'))]).toEqual([0]);
    // The series are reordered: B is hidden wherever it lands.
    expect([...resolveHidden(hide('B'), ids('C', 'A', 'B'))]).toEqual([2]);
    // B is removed: its toggle hides nothing.
    expect([...resolveHidden(hide('B'), ids('A', 'C'))]).toEqual([]);
  });

  test('should show every series again when an edit leaves none visible', () => {
    // A and B hidden, C visible; the edit removes C.
    expect([...resolveHidden(hide('A', 'B'), ids('A', 'B'))]).toEqual([]);
    // A hidden series that still exists stays hidden while another one is visible.
    expect([...resolveHidden(hide('A', 'C'), ids('A', 'B'))]).toEqual([0]);
  });

  test('should never let a legend click hide the last visible series', () => {
    expect(click(hide('A'), 1, 'A', 'B')).toEqual([0]);
    expect(click(new Set(), 0, 'A')).toEqual([]);
    expect(click(hide('A'), 1, 'A', 'B', 'C')).toEqual([0, 1]);
    // A click on a hidden series shows it again.
    expect(click(hide('A', 'B'), 0, 'A', 'B', 'C')).toEqual([1]);
    // A toggle of a series the block no longer has is dropped by the next click.
    expect([...toggleHidden(hide('Gone'), 0, ids('A', 'B'))]).toEqual(ids('A'));
  });
});
