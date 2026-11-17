/**
 * Presentations copy - `yarn test`.
 *
 * House rule: no em dash (U+2014) and no en dash (U+2013) in any UI string.
 * The Presentations page header, its empty state, the Work card and the
 * not-found copy of the viewer and the speaker view carry none, neither in
 * the English key nor in any translation.
 */
import { expect, test } from "vitest";
import { readFileSync } from "node:fs";
import { join } from "node:path";

// vitest runs from frontend/ (CI and `yarn test`), as chat-unavailable-copy.test.ts.
const SRC = join(process.cwd(), "src");
const read = (path: string) => readFileSync(join(SRC, path), "utf8");

const LOCALES = ["en", "fr", "es", "ja"];
const DASHES = /[\u2013\u2014]/;

const PRESENTATIONS_KEYS = [
  "Decks and pages your agents generated: yours, shared with you, and published to the company.",
  "Decks and pages your agents generated: yours, shared with you, and published to the company. Present, share, export.",
  "Ask your assistant for a deck. It lands here, ready to present, share with colleagues and export to PDF or PowerPoint.",
  "It may have been deleted, or it has not been shared with you. The link alone does not grant access. Ask whoever sent it to share the presentation with you.",
];

test("the Presentations copy carries no em or en dash, in any catalog", () => {
  for (const locale of LOCALES) {
    const catalog = JSON.parse(read(`i18n/lang/${locale}.json`)) as Record<string, string>;
    for (const key of PRESENTATIONS_KEYS) {
      expect(DASHES.test(key), key).toBe(false);
      expect(catalog[key], `${locale}.json: ${key}`).toBeTruthy();
      expect(DASHES.test(catalog[key]), `${locale}.json: ${key}`).toBe(false);
    }
  }
});

test("the Presentations pages render the dash-free keys", () => {
  const sources = ["pages/Presentations.tsx", "pages/Work.tsx", "pages/PresentationViewer.tsx", "pages/PresentationSpeaker.tsx"]
    .map(read)
    .join("\n");
  for (const key of PRESENTATIONS_KEYS) {
    expect(sources.includes(key), key).toBe(true);
  }
});
