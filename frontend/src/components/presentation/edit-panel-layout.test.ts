/**
 * The deck Edit panel (Slides tab, Selection tab, style pickers): the layout
 * picker shows the whole layout name (the design-system SelectValue carries
 * its own text-content-base, so the scale goes on the value) and no width cap
 * cuts "Diapositive de titre". Guards the finding B2 of UX round 1 on #10278
 * (issue #10276). Type sizes follow the design-system scale: no product floor.
 */
import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
const read = (name: string) => readFileSync(join(HERE, name), "utf8");

describe("deck Edit panel layout", () => {
  it("the layout picker value is 12 px and not width-capped", () => {
    const source = read("PresentationEditorPanel.tsx");
    expect(source).toMatch(/<SelectValue\s+className="[^"]*text-content-compact[^"]*"\s+data-part="layout-value"/);
    const trigger = source.slice(source.indexOf("<SelectTrigger", source.indexOf("const layoutTip")));
    expect(trigger.slice(0, trigger.indexOf(">"))).not.toMatch(/max-w-\[/);
  });

  // UX round 2 on #10278, B1: at 1024 in fr/es/ja the uncapped layout name
  // squeezed the row buttons out of the slide card. The buttons are one
  // shrink-0 group, so they keep their size and stay inside; the layout
  // trigger shrinks instead. UX round 3 on #10302, B1: the row never wraps,
  // so every slide row has one height whatever its layout name.
  it("the slide row stays on one line and its buttons never shrink", () => {
    const source = read("PresentationEditorPanel.tsx");
    const start = source.indexOf('data-part="slide-row-actions"');
    expect(start).toBeGreaterThan(-1);
    const rowOpen = source.slice(source.lastIndexOf("<div", start), start);
    expect(rowOpen).toMatch(/\bflex-nowrap\b/);
    expect(rowOpen).not.toMatch(/\bflex-wrap\b/);
    const row = source.slice(start, source.indexOf("</div>", start));
    expect(row).toMatch(/<span className="ml-auto flex shrink-0 /);
    const buttons = row.match(/className="h-8 w-8[^"]*"/g) ?? [];
    expect(buttons).toHaveLength(4);
    for (const cls of buttons) expect(cls).toMatch(/\bshrink-0\b/);
  });

  // UX round 5 on #10302, A1-A3: the Deck tab field labels are at body size
  // in sentence case (no uppercase caption labels), the panel close button
  // is the 36 px default and the slide row icon buttons are 32 px, the
  // toolbar floor, beside the 36 px layout picker.
  it("draws the Deck tab labels at body size, sentence case", () => {
    const source = read("PresentationEditorPanel.tsx");
    const deck = source.slice(source.indexOf('tab === "deck"'));
    expect(deck).not.toMatch(/\buppercase\b/);
    // UX round 1 on #10488, B2: every Label opening tag, wherever its
    // className sits (after htmlFor, on its own line), is at body size:
    // no content-compact and no text-[1Npx] caption scale.
    const labels = deck.match(/<Label\b[^>]*>/g) ?? [];
    expect(labels.length).toBeGreaterThan(0);
    for (const tag of labels) {
      expect(tag).not.toMatch(/\bcontent-compact\b/);
      expect(tag).not.toMatch(/text-\[1\dpx\]/);
    }
  });

  // UX round 1 on #10488, B1: the Header & footer labels (Show logo, Logo
  // position, Show footer, Show slide numbers, Event, template fields such
  // as Classification, Footer text) are at body size, primary colour.
  it("draws the Header & footer labels at body size", () => {
    const source = read("PresentationEditorPanel.tsx");
    for (const id of ["deck-chrome-logo", "deck-chrome-logo-position", "deck-chrome-footer", "deck-chrome-numbers", "deck-chrome-event", "deck-chrome-footer-text"]) {
      expect(source).toMatch(new RegExp(`<Label htmlFor="${id}" className="content-base text-default-primary">`));
    }
    expect(source).toMatch(/htmlFor=\{`deck-chrome-field-\$\{variable\.key\}`\}\s+className="content-base text-default-primary"/);
  });

  it("draws the panel close at 36 px and no slide row button under 32 px", () => {
    const source = read("PresentationEditorPanel.tsx");
    const close = source.slice(source.lastIndexOf("<Button", source.indexOf('onClick={onClose}')), source.indexOf('onClick={onClose}'));
    expect(close).not.toMatch(/size="sm"/);
    const start = source.indexOf('data-part="slide-row-actions"');
    const row = source.slice(start, source.indexOf("</div>", start));
    expect(row).not.toMatch(/\bh-[5-7]\b/);
  });

  // UX round 2 on #10278, B2: the fr save status was truncated and its
  // tooltip shows the save time, not the label: the label wraps instead.
  it("the save status label wraps instead of being cut", () => {
    const source = read("PresentationEditorPanel.tsx");
    const label = source.slice(source.indexOf('data-part="save-label"') - 60, source.indexOf('data-part="save-label"'));
    expect(label).not.toMatch(/\btruncate\b/);
    expect(source).not.toMatch(/<span className="truncate">\{label\}<\/span>/);
  });
});
