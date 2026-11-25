// @vitest-environment node
import { describe, expect, it as test } from 'vitest';
import { regularUserEmail } from '../tests_e2e/constants';

// ---------------------------------------------------------------------------
// The per-spec regular user address - the fixture's contract,
// table-driven.
//
// `regularUserEmail` is pure TypeScript, so it is exercised here directly,
// without a page, like `logo-position.test.ts`. The specs that call it only
// ever pass valid labels, so this is where its refusals are held: a label or
// a slot it let through would surface as a 422 or a 409 when the user is
// created, or as two specs sharing one account.
//
// The length rule is the backend's: `create_user` validates the address with
// plain `EmailStr`, which runs email-validator without `strict`. That checks
// the 254-character total (`EMAIL_MAX_LENGTH`) and skips the 64-character
// local-part limit (`LOCAL_PART_MAX_LENGTH`, strict mode only).
// ---------------------------------------------------------------------------

/** The characters of an address that are not its label, at a one-digit slot. */
const FIXED = 'e2e-regular--w0@e2e-example.com'.length;

describe('E2E fixtures - regular user address', () => {
  test('the labels in use keep their addresses', () => {
    expect(regularUserEmail('company-managed', 0)).toBe('e2e-regular-company-managed-w0@e2e-example.com');
    expect(regularUserEmail('agents-observability', 3)).toBe('e2e-regular-agents-observability-w3@e2e-example.com');
  });

  test('a label that is not a lowercase slug is refused, never rewritten', () => {
    // The backend refuses some of these (a space) and lowercases others, which
    // the exact leftover lookup would then never match again.
    const labels: unknown[] = [
      '',
      'Company-Managed',
      'company managed',
      ' company-managed',
      'company-managed\n',
      'company_managed',
      'company.managed',
      '-company-managed',
      'company-managed-',
      'company--managed',
      'café',
      undefined,
      null,
      42,
    ];
    for (const label of labels) {
      expect(() => regularUserEmail(label as string, 0), `label ${JSON.stringify(label)}`).toThrow(
        /is not a lowercase slug/,
      );
    }
  });

  test('an index that is not a worker slot is refused', () => {
    const slots: unknown[] = [-1, 1.5, Number.NaN, Number.POSITIVE_INFINITY, Number.MAX_SAFE_INTEGER + 1, '0', undefined];
    for (const slot of slots) {
      expect(() => regularUserEmail('company-managed', slot as number), `slot ${String(slot)}`).toThrow(
        /is not a worker slot/,
      );
    }
    expect(regularUserEmail('company-managed', Number.MAX_SAFE_INTEGER)).toBe(
      `e2e-regular-company-managed-w${Number.MAX_SAFE_INTEGER}@e2e-example.com`,
    );
  });

  test('the address stays within the 254 characters EmailStr accepts', () => {
    const longest = 'a'.repeat(254 - FIXED);
    expect(regularUserEmail(longest, 0)).toHaveLength(254);
    expect(() => regularUserEmail(`${longest}a`, 0)).toThrow(/255-character address/);
    // A two-digit slot takes one character from the label.
    expect(() => regularUserEmail(longest, 10)).toThrow(/255-character address/);
    expect(regularUserEmail(longest.slice(1), 10)).toHaveLength(254);
  });

  test('a local part over 64 characters is accepted, as EmailStr accepts it', () => {
    expect(regularUserEmail('a'.repeat(50), 0).split('@')[0]).toHaveLength(65);
  });

  test('two labels or two slots never share an address', () => {
    // Labels that end like a slot suffix, and slots whose digits run together.
    const labels = ['a', 'a1', 'a-1', 'a-w', 'a-w1', 'w1'];
    const slots = [0, 1, 2, 10, 12];
    const addresses = labels.flatMap((label) => slots.map((slot) => regularUserEmail(label, slot)));
    expect(new Set(addresses).size).toBe(labels.length * slots.length);
  });
});
