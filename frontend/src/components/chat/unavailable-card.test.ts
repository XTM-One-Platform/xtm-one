/**
 * One "no longer available" card in the chat - `yarn test`.
 *
 * Every chat card renders the shared `UnavailableCard` when the object its
 * marker names cannot be read any more. This test fails when the
 * "is no longer available." copy is rendered under `components/` anywhere
 * but as the `message` of `UnavailableCard`.
 */
import { expect, test } from "vitest";
import { readdirSync, readFileSync } from "node:fs";
import { join, sep } from "node:path";

// vitest runs from frontend/ (CI and `yarn test`), as shadow-overlay-floating.test.ts.
const COMPONENTS = join(process.cwd(), "src", "components") + sep;

/**
 * Files that may say "is no longer available." without the card, and why.
 * Not a chat card: the work item dialog's empty state (`EmptyState`).
 */
const NOT_A_CHAT_CARD = new Set(["work-items/WorkItemDetailDialog.tsx"]);

const COPY = /is no longer available\.["']/;
const THROUGH_THE_CARD = /<UnavailableCard message=\{t_i18n\(["'][^"']*is no longer available\.["']\)\} \/>/;

function* sources(dir: string): Generator<string> {
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const full = join(dir, entry.name);
    if (entry.isDirectory()) {
      yield* sources(full);
    } else if (/\.tsx?$/.test(entry.name) && !/\.test\.tsx?$/.test(entry.name)) {
      yield full;
    }
  }
}

test('"is no longer available." is rendered through UnavailableCard only', () => {
  const offenders: string[] = [];
  let throughCard = 0;
  for (const full of sources(COMPONENTS)) {
    const path = full.slice(COMPONENTS.length).split(sep).join("/");
    if (NOT_A_CHAT_CARD.has(path)) continue;
    readFileSync(full, "utf8")
      .split("\n")
      .forEach((line, i) => {
        if (!COPY.test(line)) return;
        if (THROUGH_THE_CARD.test(line)) throughCard += 1;
        else offenders.push(`${path}:${i + 1}`);
      });
  }
  expect(offenders, "render the unavailable state with <UnavailableCard message={t_i18n(...)} />").toEqual([]);
  // Video, presentation, report, investigation and scheduled work item cards.
  expect(throughCard).toBeGreaterThanOrEqual(5);
});
