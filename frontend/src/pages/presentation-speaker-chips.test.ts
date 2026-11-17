/**
 * Speaker view status chips - `yarn test`.
 *
 * The speaker header shows two status tokens (the link to the presentation
 * window and the audience window). Both are the design system's Chip, never a
 * hand-drawn pill, so the label and its wash keep the contrast the Chip pairs
 * in both themes (UX round 1 of xtm-one #10645, B1 and B2).
 */
import { expect, test } from "vitest";
import { readFileSync } from "node:fs";
import { join } from "node:path";

// vitest runs from frontend/ (CI and `yarn test`), as presentations-copy.test.ts.
const source = readFileSync(join(process.cwd(), "src", "pages", "PresentationSpeaker.tsx"), "utf8");

test("the speaker view draws no hand pill", () => {
  expect(source).not.toMatch(/rounded-full[^"]*\bpx-2\.5\b/);
  expect(source).not.toMatch(/bg-feedback-success-primary\/10/);
});

test("the status tokens are design-system chips", () => {
  expect(source).toMatch(/import \{[^}]*\bChip\b[^}]*\} from "@filigran\/design-system";/);
  for (const label of [
    "Following the presentation",
    "Rehearsal - no presentation window connected",
    "Audience window live",
    "No audience window",
  ]) {
    expect(source, label).toMatch(new RegExp(`<Chip\\s+label=\\{t_i18n\\("${label}"\\)\\}`));
  }
});
