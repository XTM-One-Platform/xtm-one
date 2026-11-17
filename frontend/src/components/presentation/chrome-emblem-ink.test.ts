// The template's corner emblem (`chrome.emblem`) draws the palette mark, a
// near-white symbol. On a light slide it must be inked like the watermark and
// the cover mark, through `--deck-hero-mark-filter` (left unfiltered,
// the Filigran Brand Deck's emblem measured 1.02:1 on its #E6F7FF light page).
// The rules are read from the real stylesheet.

import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

const stylesheet = readFileSync(`${__dirname}/presentation.css`, "utf8");

/** The declarations of the first rule whose selector is exactly `selector`. */
function ruleBody(selector: string): string {
  const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const match = new RegExp(`(?:^|\\n)${escaped}\\s*\\{([^}]*)\\}`).exec(stylesheet);
  expect(match, `no rule for ${selector}`).not.toBeNull();
  return match![1];
}

describe("corner emblem ink", () => {
  it("inks the emblem mark through the scheme's mark filter", () => {
    expect(ruleBody(".deck-root .deck-chrome-emblem .deck-chrome-emblem-mark")).toMatch(
      /filter:\s*var\(--deck-hero-mark-filter\)/,
    );
  });

  it("inverts the near-white mark on a light slide and leaves it alone on a dark one", () => {
    expect(ruleBody('.deck-root [data-scheme="light"]')).toMatch(/--deck-hero-mark-filter:\s*invert\(1\)/);
    expect(ruleBody('.deck-root [data-scheme="dark"]')).toMatch(/--deck-hero-mark-filter:\s*none/);
  });

  // `emblem_ink: "accent"` draws the one-colour mark as a
  // mask over the scheme's ink - the accent (Royal Blue on the Filigran light
  // slide), never the inverted near-black asset.
  it("inks an accent emblem with the accent on a light slide and the foreground on a dark one", () => {
    expect(ruleBody('.deck-root [data-scheme="light"]')).toMatch(/--deck-emblem-ink:\s*var\(--deck-accent\)/);
    expect(ruleBody('.deck-root [data-scheme="dark"]')).toMatch(/--deck-emblem-ink:\s*var\(--deck-fg\)/);
    const ink = ruleBody(".deck-root .deck-chrome-emblem .deck-chrome-emblem-ink");
    expect(ink).toMatch(/background-color:\s*var\(--deck-emblem-ink\)/);
    expect(ink).toMatch(/(?:^|\s)mask:\s*var\(--deck-emblem-mask\)/);
    expect(ink).not.toMatch(/filter:/);
  });
});
