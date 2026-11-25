// @vitest-environment node
import { describe, expect, it as test } from 'vitest';
import { resolveLogoPosition, type ResolvedLogoPosition } from '../src/components/presentation/logo-position';
import { DECK_LOGO_POSITIONS } from '../src/components/presentation/previewSpec';
import type { DeckLogoPosition, PresentationSlide, SlideLayout } from '../src/types';

// ---------------------------------------------------------------------------
// Logo placement resolver - the per-slide answer, table-driven.
//
// `resolveLogoPosition` is pure TypeScript (no DOM), so it is exercised here
// directly, without a page, like `architecture-layout.test.ts`. The deck stores
// ONE value; what the chrome draws is decided per slide, and this table pins
// every layout x every stored value x every title alignment. The rendered
// geometry (where the bar actually puts the mark) is asserted on a live deck in
// `presentations.spec.ts` - "logo placement on the opening and closing slides".
// ---------------------------------------------------------------------------

/** Every stored value, `undefined` (a deck saved before the setting existed) included. */
const STORED: readonly (DeckLogoPosition | undefined)[] = [undefined, ...DECK_LOGO_POSITIONS];

/** A slide of the given layout - the resolver reads `layout` and, on a title
 *  slide, `align`; the rest of the slide is irrelevant to it. */
const slide = (layout: SlideLayout, extra: Record<string, unknown> = {}): PresentationSlide =>
  ({ layout, ...extra }) as PresentationSlide;

/** The title alignments a slide is tried with: none (a cover with no `align`
 *  is left-aligned) and the two the title slide declares. */
const ALIGNMENTS = [undefined, 'start', 'center'] as const;
type Alignment = (typeof ALIGNMENTS)[number];

/** Where `auto` puts the mark on a placeable slide, given its alignment. */
type AutoAnswer = (align: Alignment) => ResolvedLogoPosition;

/**
 * The resolver's contract per layout, typed as a Record so a layout added to
 * the union without a decision here fails to compile. `corner` is the layouts
 * whose bottom row is shared with the footer bar and the number: they answer
 * `end` whatever the deck stores. Every other entry is a layout the stored
 * value applies to, written as where `auto` lands on it - a layout cannot be
 * declared placeable without saying where its mark goes, and the generated
 * tests below check both halves for each one.
 */
const CONTRACT: Record<SlideLayout, 'corner' | AutoAnswer> = {
  // The mark starts on the edge the headline starts on.
  title: (align) => (align === 'center' ? 'center' : 'start'),
  // The closing slide is composed on the slide axis unless it asks for the
  // left layout: then the mark starts on the edge the headline starts on.
  closing: (align) => (align === 'start' ? 'start' : 'center'),
  section: 'corner',
  agenda: 'corner',
  content: 'corner',
  dashboard: 'corner',
  roadmap: 'corner',
  takeaways: 'corner',
};

const LAYOUTS = Object.keys(CONTRACT) as SlideLayout[];
const BODY_LAYOUTS = LAYOUTS.filter((layout) => CONTRACT[layout] === 'corner');
const PLACEABLE = LAYOUTS.flatMap((layout) => {
  const answer = CONTRACT[layout];
  return answer === 'corner' ? [] : [{ layout, auto: answer }];
});

const EXPLICIT: readonly ResolvedLogoPosition[] = ['start', 'center', 'end'];

describe('Presentations - logo placement resolver', () => {
  test('the picker order is the spec order, auto first', () => {
    expect([...DECK_LOGO_POSITIONS]).toEqual(['auto', 'start', 'center', 'end']);
  });

  test('the contract knows the two placeable layouts', () => {
    expect(PLACEABLE.map(({ layout }) => layout)).toEqual(['title', 'closing']);
  });

  for (const layout of BODY_LAYOUTS) {
    test(`${layout} slides keep the corner whatever the deck stores`, () => {
      for (const stored of STORED) {
        expect(resolveLogoPosition(stored, slide(layout)), `stored=${String(stored)}`).toBe('end');
      }
      // Not even a title alignment on a body slide changes that.
      expect(resolveLogoPosition('center', slide(layout, { align: 'center' }))).toBe('end');
    });
  }

  // One generated test per placeable layout, so a layout declared placeable
  // in the contract is checked here without a hand-written case: an explicit
  // value pins the mark, `auto` (and a deck saved before the setting existed) lands where
  // the contract says the layout composes it, whatever the alignment.
  for (const { layout, auto } of PLACEABLE) {
    test(`${layout} slides take the stored value, auto following their composition`, () => {
      for (const align of ALIGNMENTS) {
        const tried = slide(layout, align ? { align } : {});
        const label = `${layout} align=${String(align)}`;
        for (const stored of EXPLICIT) {
          expect(resolveLogoPosition(stored, tried), `${label} stored=${stored}`).toBe(stored);
        }
        for (const stored of [undefined, 'auto'] as const) {
          expect(resolveLogoPosition(stored, tried), `${label} stored=${String(stored)}`).toBe(auto(align));
        }
      }
    });
  }

  test('auto follows the title slide alignment', () => {
    for (const stored of [undefined, 'auto'] as const) {
      // No `align` is a left-aligned cover: the mark sits on the edge the headline starts on.
      expect(resolveLogoPosition(stored, slide('title')), `stored=${String(stored)}`).toBe('start');
      expect(resolveLogoPosition(stored, slide('title', { align: 'start' })), `stored=${String(stored)}`).toBe('start');
      expect(resolveLogoPosition(stored, slide('title', { align: 'center' })), `stored=${String(stored)}`).toBe('center');
    }
  });

  test('auto puts the closing slide logo on the slide axis unless it is left-aligned', () => {
    expect(resolveLogoPosition(undefined, slide('closing'))).toBe('center');
    expect(resolveLogoPosition('auto', slide('closing'))).toBe('center');
    expect(resolveLogoPosition('auto', slide('closing', { align: 'center' }))).toBe('center');
    expect(resolveLogoPosition('auto', slide('closing', { align: 'start' }))).toBe('start');
  });

  test('auto is always resolved away', () => {
    for (const layout of LAYOUTS) {
      for (const stored of STORED) {
        expect(['start', 'center', 'end']).toContain(resolveLogoPosition(stored, slide(layout)));
      }
    }
  });
});
