// Gate: the deck inspector's field controls (SchemaFieldControls, every block
// and slide inspector) stay on the Forms standard (Design standards s.2): every
// field label at body size, and every help,
// warning or problem line tied to its control by aria-describedby (UX notes of
// #9783, #9816, #10139 and #10241 r1).

import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, it } from "vitest";

const SOURCE = readFileSync(resolve(__dirname, "SchemaFieldControls.tsx"), "utf8");

describe("SchemaFieldControls type scale", () => {
  it("draws every field label at body size", () => {
    expect(SOURCE).toContain('const FIELD_LABEL = "content-base text-default-primary";');
  });

  it("ties every help, warning and problem line to its control", () => {
    const lines = SOURCE.match(/<p\b[^>]*className=\{(?:FORM_FIELD_HELP|FIELD_WARNING)\}[^>]*>/g) ?? [];
    expect(lines.length).toBeGreaterThanOrEqual(5);
    for (const line of lines) expect(line).toMatch(/\bid=\{/);
  });

  // UX round 5 of #10302, A2: the Selection tab Add buttons (lines, icons,
  // optional groups) are 36 px, not 24 or 28 px.
  it("draws every Add button at the 36 px default", () => {
    const adds: string[] = [];
    for (let at = SOURCE.indexOf("<Plus "); at !== -1; at = SOURCE.indexOf("<Plus ", at + 1)) {
      adds.push(SOURCE.slice(SOURCE.lastIndexOf("<Button", at), at));
    }
    expect(adds.length).toBeGreaterThanOrEqual(3);
    for (const tag of adds) {
      expect(tag).not.toMatch(/size="sm"/);
      expect(tag).not.toMatch(/\bh-[5-8]\b/);
    }
  });

  // UX round 1 of #10488, A1: one Add style across the inspector (lines,
  // icons, list, table Column/Row, optional group), the secondary button.
  it("draws every Add button in one style, secondary", () => {
    const adds: string[] = [];
    for (let at = SOURCE.indexOf("<Plus "); at !== -1; at = SOURCE.indexOf("<Plus ", at + 1)) {
      adds.push(SOURCE.slice(SOURCE.lastIndexOf("<Button", at), at));
    }
    expect(adds.length).toBeGreaterThanOrEqual(6);
    for (const tag of adds) expect(tag).toMatch(/variant="secondary"/);
  });

  it("draws the tri-state picker at the 36 px default", () => {
    expect(SOURCE).not.toMatch(/<SelectTrigger\b[^>]*className="[^"]*\bh-[5-8]\b/);
  });
});

// The Selection panel around those fields (SlideInspector: section labels,
// breadcrumb, Back to the slide, notes status, hints) is on the same scale
// (UX round 1 of #10269, B2).
const INSPECTOR = readFileSync(resolve(__dirname, "SlideInspector.tsx"), "utf8");

describe("SlideInspector type scale", () => {
  it("draws every section label at body size, sentence case", () => {
    expect(INSPECTOR).toContain('const SECTION_LABEL = "content-base text-default-primary";');
  });

  // UX round 2 of #10269, B1: in fr at 1024 (a 340 px panel) the block
  // inspector's "Slide N \u00b7 blocks.i" line was cut next to "Back to the slide".
  // The header row wraps and neither the title nor the position line truncates.
  it("wraps the block inspector header instead of cutting it", () => {
    const header = INSPECTOR.slice(INSPECTOR.indexOf('data-testid="block-inspector-header"'));
    const headerRow = header.slice(0, header.indexOf("</button>"));
    expect(headerRow).toContain("flex flex-wrap");
    expect(headerRow).not.toMatch(/\btruncate\b/);
    expect(headerRow).toMatch(/<p className="break-words content-compact/);
  });

  // UX rounds 2 and 3 of #10269, A3: the position line reads "Slide 1 · Column 1
  // · Block 1", never the internal path ("columns.0.0") in monospace.
  it("shows the block position in words, not the internal path", () => {
    const header = INSPECTOR.slice(INSPECTOR.indexOf('data-testid="block-inspector-header"'));
    const headerRow = header.slice(0, header.indexOf("</button>"));
    expect(headerRow).toContain("blockPlaces(blockPath)");
    expect(headerRow).not.toMatch(/&middot; \{blockPath\}/);
    expect(headerRow).not.toContain("font-mono-plex");
  });

  // UX rounds 2 and 3 of #10269, A1: the tri-state picker (Accent bar) was
  // w-fit (80 px) beside full-width neighbours.
  it("draws the tri-state picker at full width like its neighbours", () => {
    expect(SOURCE).toMatch(/<SelectTrigger\s+id=\{id\}\s+className="w-full"/);
  });
});

// The slide inspector's style pickers, the template picture field and the
// slide row layout picker (UX Director drift note on #10269 r2, measured at
// 1440 and 1024): field labels at body size, and the
// layout picker in sentence case at the 36 px control height.
const PICKERS = readFileSync(resolve(__dirname, "SlideStylePickers.tsx"), "utf8");
const ASSET_FIELD = readFileSync(resolve(__dirname, "TemplateAssetField.tsx"), "utf8");
const EDITOR_PANEL = readFileSync(resolve(__dirname, "PresentationEditorPanel.tsx"), "utf8");

describe("Slide inspector pickers type scale", () => {
  it("draws the template picture label at body size and its buttons at the 36 px default", () => {
    expect(ASSET_FIELD).toContain('<Label id={labelId} className="content-base text-default-primary">');
    expect(ASSET_FIELD).not.toMatch(/size="sm"/);
    expect(ASSET_FIELD).not.toMatch(/\bh-[5-8]\b/);
  });

  it("draws the slide row layout picker in sentence case at 36 px", () => {
    const start = EDITOR_PANEL.indexOf("aria-label={layoutTip}");
    const tag = EDITOR_PANEL.slice(EDITOR_PANEL.lastIndexOf("<SelectTrigger", start), start);
    expect(tag).toContain("h-9");
    expect(tag).not.toMatch(/\buppercase\b|\bcontent-caption\b/);
  });

  // UX round 1 of #10302, B1: the fr "Diapositive de titre" (about 122 px of
  // text) was cut in a 140 px trigger and the tooltip did not name the layout.
  it("leaves room for the longest translated layout name and names it in the tooltip", () => {
    const start = EDITOR_PANEL.indexOf("aria-label={layoutTip}");
    const tag = EDITOR_PANEL.slice(EDITOR_PANEL.lastIndexOf("<SelectTrigger", start), start);
    const cap = /max-w-\[(\d+)px\]/.exec(tag);
    expect(cap === null || Number(cap[1]) >= 180).toBe(true);
    expect(EDITOR_PANEL).toContain("<Tip content={layoutTipContent}>");
    expect(EDITOR_PANEL).toMatch(/layoutTipContent = \([\s\S]*?layoutLabel\(slide\.layout, t_i18n, unit\)/);
  });

  // UX round 2 of #10302, B1: the wider trigger crushed the row actions at
  // 1024 fr (Delete past the viewport). The trigger may shrink and cut its
  // value (the tooltip names the layout); the four actions never shrink.
  it("lets the layout trigger shrink and keeps the slide row actions whole", () => {
    const start = EDITOR_PANEL.indexOf("aria-label={layoutTip}");
    const tag = EDITOR_PANEL.slice(EDITOR_PANEL.lastIndexOf("<SelectTrigger", start), start);
    expect(tag).toMatch(/\bmin-w-0\b/);
    expect(tag).toMatch(/\bmax-w-full\b/);
    expect(tag).not.toMatch(/\bshrink-0 gap\b/);
    expect(EDITOR_PANEL).toMatch(/<SelectValue\s+className="min-w-0 truncate [^"]*"\s+data-part="layout-value"/);
    const row = EDITOR_PANEL.slice(EDITOR_PANEL.indexOf('data-part="slide-row-actions"'));
    expect(row).toMatch(/^[^\n]*\n\s*<span className="ml-auto flex shrink-0 /);
  });

  // UX round 5 of #10302, B1: beside the four row buttons the trigger was
  // 74 px at 1024 and "Contenu" / "Conclusion" both read "Co...". The picker
  // has its own full-width line under the title: nothing else shares it, the
  // trigger fills it (w-full, the select's own span stretched), and no width
  // cap is set, so its clip width is the column, not what the buttons leave.
  it("puts the layout picker alone on a full-width line under the title", () => {
    const start = EDITOR_PANEL.indexOf('data-part="slide-row-layout"');
    expect(start).toBeGreaterThan(-1);
    const open = EDITOR_PANEL.slice(EDITOR_PANEL.lastIndexOf("<div", start), start);
    expect(open).toMatch(/className="flex w-full min-w-0 \[&>span\]:min-w-0 \[&>span\]:flex-1"/);
    const body = EDITOR_PANEL.slice(start, EDITOR_PANEL.indexOf("</div>", start));
    expect(body).toMatch(/^[^\n]*\n\s*\{layoutPicker\(index\)\}\n\s*$/);
    // Title line, then the picker's line, then the buttons' line.
    const head = EDITOR_PANEL.indexOf('data-part="slide-row-head"');
    expect(head).toBeGreaterThan(-1);
    expect(head).toBeLessThan(start);
    expect(start).toBeLessThan(EDITOR_PANEL.indexOf('data-part="slide-row-actions"'));
    const trigger = EDITOR_PANEL.slice(
      EDITOR_PANEL.lastIndexOf("<SelectTrigger", EDITOR_PANEL.indexOf("aria-label={layoutTip}")),
      EDITOR_PANEL.indexOf("aria-label={layoutTip}"),
    );
    expect(trigger).toMatch(/\bw-full\b/);
    expect(trigger).not.toMatch(/max-w-\[/);
  });

  // UX round 6 of #10302, B1: beside the four row buttons the title had 44 px
  // at 1024 ("Key takeaways" read "Key..."). The title line holds the title
  // button alone, at the full width of the column (about 200 px at 1024, over
  // the 140 px floor); the buttons have their own line under the picker.
  it("gives the slide title a line of its own, without the row buttons", () => {
    const start = EDITOR_PANEL.indexOf('data-part="slide-row-head"');
    const open = EDITOR_PANEL.slice(EDITOR_PANEL.lastIndexOf("<div", start), start);
    expect(open).toMatch(/\bw-full\b/);
    const end = EDITOR_PANEL.indexOf('data-part="slide-row-layout"', start);
    const line = EDITOR_PANEL.slice(start, end);
    expect(line).not.toContain("<Button");
    expect(line).not.toContain('data-part="slide-row-actions"');
    expect(line.match(/<button\b/g) ?? []).toHaveLength(1);
  });

  // UX round 7 of #10302, B1: a title longer than the column was cut with an
  // ellipsis and could not be read anywhere. The row renders SlideRowTitle,
  // which wraps the cut title in the design-system Tip (never a native title).
  it("names a cut slide title whole in the design-system tooltip", () => {
    const start = EDITOR_PANEL.indexOf('data-part="slide-row-head"');
    const line = EDITOR_PANEL.slice(start, EDITOR_PANEL.indexOf('data-part="slide-row-layout"', start));
    expect(line).toContain("<SlideRowTitle");
    const fn = EDITOR_PANEL.slice(EDITOR_PANEL.indexOf("function SlideRowTitle"));
    const body = fn.slice(0, fn.indexOf("\n}\n"));
    expect(body).toMatch(/scrollWidth > node\.clientWidth/);
    expect(body).toMatch(/<Tip content=\{cut \? text : undefined\}/);
    expect(body).toMatch(/data-part="slide-row-title"[\s\S]*\btruncate\b/);
    expect(body).not.toMatch(/\btitle=/);
  });

  // The clip width of that line is the slide row's text column: at 1024 the
  // 340 px panel less the 88 px thumbnail, the gap and the row padding leaves
  // more than 200 px, and the trigger keeps its padding and chevron (about
  // 32 px). The longest built-in layout name in en, fr, es and ja, measured
  // at 12 px (about 7 px a Latin character, 12 px a CJK one), stays under
  // the 168 px that remain, so no built-in name is cut at 1024.
  it("fits the longest built-in layout name of every locale in the clip width", () => {
    const ids = ["Title slide", "Title page", "Section", "Agenda", "Content", "Roadmap", "Takeaways", "Dashboard", "Closing"];
    const width = (text: string) =>
      [...text].reduce((sum, ch) => sum + (/[\u3000-\u9fff\uff00-\uffef]/.test(ch) ? 12 : 7), 0);
    for (const locale of ["en", "fr", "es", "ja"]) {
      const catalog = JSON.parse(readFileSync(resolve(__dirname, `../../i18n/lang/${locale}.json`), "utf8")) as Record<string, string>;
      for (const id of ids) {
        const name = catalog[id] ?? id;
        expect(width(name), `${locale} "${name}"`).toBeLessThanOrEqual(168);
      }
    }
  });

  // UX round 3 of #10302, B1: a long layout name (fr "Diapositive de titre")
  // wrapped the actions of that row only, so rows had different heights. The
  // row never wraps: its height cannot depend on the name length.
  it("keeps every slide row on one line whatever the layout name", () => {
    const open = EDITOR_PANEL.slice(EDITOR_PANEL.lastIndexOf("<div", EDITOR_PANEL.indexOf('data-part="slide-row-actions"')));
    const tag = open.slice(0, open.indexOf(">"));
    expect(tag).toMatch(/\bflex-nowrap\b/);
    expect(tag).not.toMatch(/\bflex-wrap\b/);
  });

  // UX round 1 of #10302, B2: picker options under the 32 px floor, and hero
  // colour names (fr "Dégradé de la marque") wrapping out of a fixed 28 px box.
  it("draws the style picker options at 32 px at least and lets hero colour names wrap inside", () => {
    expect(PICKERS).not.toMatch(/optionClassName=\{\(\) => "[^"]*\bh-7\b/);
    const hero = PICKERS.slice(PICKERS.indexOf('label={t_i18n("Hero colour")}'));
    expect(hero).toMatch(/optionClassName=\{\(\) => "h-auto min-h-8 /);
  });
});
