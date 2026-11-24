import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { test, expect } from '@playwright/test';
import {
  BUILTIN_PLACEHOLDERS,
  MAX_CHROME_VARIABLES,
  MAX_FIELD_VALUE_CHARS,
  MAX_PATTERN_CHARS,
  MAX_VARIABLE_HINT_CHARS,
  MAX_VARIABLE_LABEL_CHARS,
  VARIABLE_KEY_RE,
  canonicalFieldKey,
  chromeText,
  chromeWatermark,
  declaresPattern,
  renderChromePattern,
  templateChromeOf,
  validateTemplateChrome,
  type ChromeProblem,
  type TemplateChromeDraft,
} from '../src/components/presentation/chrome-pattern';
import type { PresentationSpec, TemplateChrome } from '../src/types';

// ---------------------------------------------------------------------------
// Template header and footer - the TypeScript half of the rule.
//
// `chrome-pattern.ts` mirrors `backend/app/services/presentation_chrome.py`:
// the same placeholder syntax, the same collapse rule, the same validation.
// Both halves are checked against ONE table, `fixtures/chrome-pattern-cases.json`,
// which `backend/tests/test_presentation_chrome.py` feeds to the Python side.
// Pure TypeScript (no DOM), so it runs here without a page, like
// `tests_unit/architecture-layout.test.ts`. A case that renders differently on the two
// sides is a footer the tool result quotes one way and the screen draws another.
// ---------------------------------------------------------------------------

interface RenderCase {
  name: string;
  pattern: string | null;
  values: Record<string, string>;
  expected: string;
}

interface DeclarationCase {
  name: string;
  declaration: { footer: string; header: string; variables: TemplateChromeDraft['variables'] };
  canonical: TemplateChrome | null;
  problems: ChromeProblem['code'][];
}

interface DeckCase {
  name: string;
  spec: Pick<PresentationSpec, 'title' | 'slides' | 'chrome'>;
  declaration: Record<string, unknown>;
  footer?: string;
  header?: string;
  /** `false` when the reader must return `null` for this declaration. */
  reads?: boolean;
}

interface Cases {
  render: RenderCase[];
  declarations: DeclarationCase[];
  decks: DeckCase[];
}

const CASES: Cases = JSON.parse(
  readFileSync(fileURLToPath(new URL('./fixtures/chrome-pattern-cases.json', import.meta.url)), 'utf8'),
);

test.describe('Presentations - template chrome pattern (shared table)', () => {
  test('the table covers every kind of case', () => {
    expect(CASES.render.length).toBeGreaterThan(20);
    expect(CASES.declarations.length).toBeGreaterThan(15);
    expect(CASES.decks.length).toBeGreaterThan(4);
    // Every problem code the validator can report is exercised at least once.
    const reported = new Set(CASES.declarations.flatMap((entry) => entry.problems));
    const codes: ChromeProblem['code'][] = [
      'pattern_too_long',
      'pattern_unbalanced',
      'pattern_malformed_slot',
      'too_many_variables',
      'variable_key_invalid',
      'variable_key_builtin',
      'variable_key_duplicate',
      'variable_label_missing',
      'variable_label_too_long',
      'variable_hint_too_long',
      'variable_default_too_long',
      'variables_without_pattern',
      'placeholder_undeclared',
      'variable_unused',
    ];
    for (const code of codes) expect(reported.has(code), code).toBe(true);
  });

  for (const entry of CASES.render) {
    test(`renders: ${entry.name}`, () => {
      expect(renderChromePattern(entry.pattern, entry.values)).toBe(entry.expected);
    });
  }

  for (const entry of CASES.declarations) {
    test(`validates: ${entry.name}`, () => {
      const { canonical, problems } = validateTemplateChrome({
        footer: entry.declaration.footer,
        header: entry.declaration.header,
        variables: entry.declaration.variables,
      });
      expect(problems.map((problem) => problem.code).sort()).toEqual([...entry.problems].sort());
      expect(canonical).toEqual(entry.canonical);
    });
  }

  for (const entry of CASES.decks) {
    test(`fills a deck: ${entry.name}`, () => {
      const templateChrome = templateChromeOf({ extra_metadata: { chrome: entry.declaration } });
      if (entry.reads === false) {
        expect(templateChrome).toBeNull();
        // The v1 footer stays: event label and footer text as two segments.
        expect(chromeText(entry.spec, templateChrome).footer).toEqual([
          entry.spec.chrome?.event_label,
          entry.spec.chrome?.footer_text,
        ]);
        return;
      }
      expect(templateChrome).not.toBeNull();
      const text = chromeText(entry.spec, templateChrome);
      // A template pattern is ONE segment (it carries its own separators).
      expect(text.footer.length).toBeLessThanOrEqual(1);
      expect(text.footer.join('')).toBe(entry.footer);
      expect(text.header ?? '').toBe(entry.header);
    });
  }

  test('the body-slide watermark: a literal true, stored only when on, never a pattern', () => {
    const read = (chrome: Record<string, unknown>) => templateChromeOf({ extra_metadata: { chrome } });
    expect(read({ footer: '{{title}}', watermark: true })?.watermark).toBe(true);
    for (const value of ['true', 1, null, false]) {
      expect(read({ footer: '{{title}}', watermark: value })?.watermark, String(value)).toBeUndefined();
    }
    // Alone it is a declaration that draws no pattern: the deck keeps its v1 footer.
    const alone = read({ watermark: true });
    expect(alone).not.toBeNull();
    expect(declaresPattern(alone)).toBe(false);
    expect(read({ watermark: false })).toBeNull();
    const spec = { title: 'Deck', slides: [], chrome: { event_label: 'FIC 2026', footer_text: 'Internal' } };
    expect(chromeText(spec, alone).footer).toEqual(['FIC 2026', 'Internal']);
    // Drawn when the template declares it and the deck shows its logo.
    expect(chromeWatermark(spec, alone)).toBe(true);
    expect(chromeWatermark({ chrome: { ...spec.chrome, show_logo: false } }, alone)).toBe(false);
    expect(chromeWatermark(spec, read({ footer: '{{title}}' }))).toBe(false);
    expect(chromeWatermark(spec, null)).toBe(false);
    // The form's writer mirrors the backend's canonical shape.
    expect(validateTemplateChrome({ footer: '', header: '', variables: [], watermark: true })).toEqual({
      canonical: { footer: null, header: null, variables: [], watermark: true },
      problems: [],
    });
    expect(validateTemplateChrome({ footer: '{{title}}', header: '', variables: [], watermark: false }).canonical).toEqual({
      footer: '{{title}}',
      header: null,
      variables: [],
    });
  });

  test('the bounds and the grammar are the backend constants', () => {
    // Pinned on both sides: `test_the_frontend_mirror_renders_the_same_rule_from_the_same_pattern`
    // reads these values out of this module's source.
    expect(BUILTIN_PLACEHOLDERS).toEqual(['title', 'subtitle', 'event']);
    expect(VARIABLE_KEY_RE.source).toBe('^[a-z][a-z0-9_]{0,31}$');
    expect(MAX_CHROME_VARIABLES).toBe(8);
    expect(MAX_PATTERN_CHARS).toBe(200);
    expect(MAX_VARIABLE_LABEL_CHARS).toBe(60);
    expect(MAX_VARIABLE_HINT_CHARS).toBe(160);
    expect(MAX_FIELD_VALUE_CHARS).toBe(120);
    expect(canonicalFieldKey(' Classification ')).toBe('classification');
  });
});
