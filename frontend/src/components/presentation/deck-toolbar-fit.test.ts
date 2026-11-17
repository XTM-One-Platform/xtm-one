// Gate: the deck and page bars never squeeze their actions over the slide
// counter (UX round 8 of #10302, A1: at 1024 px with the studio rail out the
// right-hand icon buttons shrank to 16 px and painted over Previous slide, the
// counter and Next slide). The right region keeps its content width, its
// children never shrink, and the first / last slide buttons leave a narrow bar.

import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, it } from "vitest";

const read = (name: string) => readFileSync(resolve(__dirname, name), "utf8");
const DECK = read("DeckToolbar.tsx");
const PAGE = read("PageToolbar.tsx");
const CSS = read("presentation.css");
const VIEWER = read("../../pages/PresentationViewer.tsx");
const CHROME = read("../studio/StudioChrome.tsx");

describe("deck toolbar fit", () => {
  it("keeps the actions region at its content width, children unshrunk", () => {
    const match = DECK.match(/export const DECK_TOOLBAR_ACTIONS = "([^"]+)";/);
    expect(match).not.toBeNull();
    const classes = (match?.[1] ?? "").split(/\s+/);
    expect(classes).toContain("min-w-fit");
    expect(classes).toContain("[&>*]:shrink-0");
    expect(classes).not.toContain("min-w-0");
  });

  it("uses that region in both bars", () => {
    for (const source of [DECK, PAGE]) {
      expect(source).toContain("className={DECK_TOOLBAR_ACTIONS}");
      expect(source).not.toContain('"flex min-w-0 flex-1 items-center justify-end gap-0.5"');
    }
  });

  it("drops the first and last slide buttons on a narrow bar", () => {
    expect(DECK.match(/className="deck-toolbar-edge"/g) ?? []).toHaveLength(2);
    expect(CSS).toMatch(/@container deck-toolbar \(max-width: \d+px\) \{\s*\.deck-toolbar-edge \{\s*display: none;/);
  });

  // UX round 1 of #10528, B1: the page bar's leading region (back, icon,
  // title, format chip) overlapped Previous page at a 644 px bar. Its zoom
  // in / out buttons and the format chip leave a narrow bar.
  it("frees the page bar's leading region on a narrow bar", () => {
    expect(PAGE.match(/className="deck-toolbar-edge"/g) ?? []).toHaveLength(2);
    expect(VIEWER).toMatch(/className="deck-toolbar-chip /);
    expect(CSS).toMatch(/@container deck-toolbar \(max-width: \d+px\) \{[^}]*\}\s*\.deck-toolbar-chip \{\s*display: none;/);
  });

  // B2: the back button of every studio bar keeps its 32 px square.
  it("never shrinks the studio bar back button", () => {
    expect(CHROME).toContain("className={`${STUDIO_BAR_BUTTON} shrink-0`}");
  });
});
