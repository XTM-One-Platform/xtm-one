import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { test, expect } from '@playwright/test';
import {
  EMPTY_EXTRACTOR,
  EXTRACTOR_OPERATIONS,
  EXTRACTOR_OUTPUT_KINDS,
  EXTRACTOR_SOURCE_KINDS,
  operationMeta,
} from '../src/components/evaluation/extractor-catalog';

// ---------------------------------------------------------------------------
// Per-agent output extraction — the TypeScript half of the contract.
//
// `extractor-catalog.ts` mirrors `backend/app/services/evaluation/extraction/spec.py`.
// Both halves are checked against ONE table, `fixtures/extractor-catalog.json`,
// which `backend/tests/test_evaluation_extraction.py` writes from the
// Python source, so a UI/server mismatch fails here, not on an author
// mid-configuration.
//
// No live stack: pure contract parity, like `chrome-pattern.spec.ts`.
// ---------------------------------------------------------------------------

interface CatalogFixture {
  operations: { op: string; accepts: string; produces: string | null }[];
  source_kinds: string[];
  output_kinds: string[];
  path_operations: string[];
}

const fixture: CatalogFixture = JSON.parse(
  readFileSync(
    fileURLToPath(new URL('./fixtures/extractor-catalog.json', import.meta.url)),
    'utf8',
  ),
);

test.describe('extractor catalog parity', () => {
  test('the UI offers exactly the operations the server accepts, in order', () => {
    expect(EXTRACTOR_OPERATIONS.map((entry) => entry.op)).toEqual(
      fixture.operations.map((entry) => entry.op),
    );
  });

  test('every operation declares the same kinds on both sides', () => {
    for (const declared of fixture.operations) {
      const local = operationMeta(declared.op as never);
      expect(local, `missing operation ${declared.op}`).toBeTruthy();
      expect(local!.accepts).toBe(declared.accepts);
      // `parse_json` and `select_path` produce "whichever kind the value is";
      // the UI shows that as prose rather than a single kind.
      if (declared.produces === null) {
        expect(['object or array', 'any']).toContain(local!.produces);
      } else {
        expect(local!.produces.replace(' ', '_')).toBe(declared.produces);
      }
    }
  });

  test('only the path operations take a path', () => {
    const withPath = EXTRACTOR_OPERATIONS.filter((entry) => entry.needsPath).map(
      (entry) => entry.op,
    );
    expect(withPath.sort()).toEqual([...fixture.path_operations].sort());
  });

  test('source kinds match', () => {
    expect([...EXTRACTOR_SOURCE_KINDS].sort()).toEqual([...fixture.source_kinds].sort());
  });

  test('output kinds match', () => {
    expect([...EXTRACTOR_OUTPUT_KINDS].sort()).toEqual([...fixture.output_kinds].sort());
  });

  test('the default extractor a new row starts from is itself valid', () => {
    expect(EXTRACTOR_OPERATIONS.map((e) => e.op)).toContain(
      EMPTY_EXTRACTOR.operations[0].op,
    );
    expect(EXTRACTOR_OUTPUT_KINDS).toContain(EMPTY_EXTRACTOR.output_kind);
    expect(EXTRACTOR_SOURCE_KINDS).toContain(EMPTY_EXTRACTOR.source.kind);
  });

  test('no operation is higher-order', () => {
    // `map_values` would consume the rest of the chain, so the declared output
    // kind would describe an element rather than the result.
    expect(EXTRACTOR_OPERATIONS.map((entry) => entry.op)).not.toContain('map_values');
    expect(fixture.operations.map((entry) => entry.op)).not.toContain('map_values');
  });
});
