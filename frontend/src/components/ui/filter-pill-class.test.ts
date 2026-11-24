/**
 * One filter pill - `yarn test`.
 *
 * The filter pill's classes live in `components/ui/filter-pill.tsx` alone
 * (`filterPillClass`, `FilterPillTrigger`, `FilterPillPanel`). A copy of the
 * class string anywhere else fails here: draw the pill from the shared
 * component instead. The match is on the pill's shared
 * fragment, not its padding, so a copy with `px-2` and `gap-1` (the tag
 * overflow trigger caught at round 2, B2) fails as well.
 */
import { expect, test } from "vitest";
import { readdirSync, readFileSync } from "node:fs";
import { join, sep } from "node:path";
import { filterPanelAlign, filterPanelMaxHeight } from "./filter-pill";

// vitest runs from frontend/ (CI and `yarn test`), as shadow-overlay-floating.test.ts.
const SRC = join(process.cwd(), "src") + sep;
const OWNER = "components/ui/filter-pill.tsx";
const PILL = "h-8 content-compact font-medium transition-colors border";

function* sourceFiles(dir: string): Generator<string> {
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const full = join(dir, entry.name);
    if (entry.isDirectory()) {
      if (entry.name !== "node_modules") yield* sourceFiles(full);
    } else if (/\.tsx?$/.test(entry.name) && !/\.test\.tsx?$/.test(entry.name)) {
      yield full;
    }
  }
}

test("the filter pill class string is declared in filter-pill.tsx only", () => {
  const holders = [...sourceFiles(SRC)]
    .filter((full) => readFileSync(full, "utf8").includes(PILL))
    .map((full) => full.slice(SRC.length).split(sep).join("/"));
  expect(holders).toContain(OWNER);
  expect(
    holders.filter((path) => path !== OWNER),
    "a page re-declares the filter pill: use filterPillClass / FilterPillTrigger from components/ui/filter-pill",
  ).toEqual([]);
});

// The toolbar clear control is one 32 px row control, shared by every toolbar.
test("the filter clear control keeps the toolbar height", () => {
  const pill = readFileSync(join(SRC, OWNER), "utf8");
  const clearClass = pill.slice(pill.indexOf("const CLEAR_CLASS"), pill.indexOf("export function FilterClearButton"));
  expect(clearClass).toContain("h-8");
});

// UX r1 B1 on #11339: in the one-row toolbar (filtersInPill) the ja label of
// "Clear all filters" wrapped onto two lines inside the 32 px control.
test("the filter clear control stays on one line and never shrinks", () => {
  const pill = readFileSync(join(SRC, OWNER), "utf8");
  const clearClass = pill.slice(pill.indexOf("const CLEAR_CLASS"), pill.indexOf("export function FilterClearButton"));
  expect(clearClass).toContain("whitespace-nowrap");
  expect(clearClass).toContain("shrink-0");
});

// UX r2 B2 on #11339: under filtersInPill at 1080 (ja, fr) the fitted scope
// switcher shrank to 0 px and its segments overlapped the Filters pill and the
// More tabs menu. A fitted bar keeps its width, and the compact one-row toolbar
// draws Clear all filters as an icon named by aria-label and a tooltip.
test("a one-row toolbar never squeezes the fitted scope switcher", () => {
  const segmented = readFileSync(join(SRC, "components/ui/segmented-filter.tsx"), "utf8");
  expect(segmented).toContain('fitted.measuring ? "flex-auto overflow-hidden" : "shrink-0"');
  const table = readFileSync(join(SRC, "components/ui/data-table.tsx"), "utf8");
  const compactClear = table.slice(table.indexOf("filtersInPill && compactToolbar ? ("), table.indexOf("{selection && selected.length > 0"));
  expect(compactClear).toContain('aria-label={t_i18n("Clear all filters")}');
  expect(compactClear).toContain('<Tip content={t_i18n("Clear all filters")}>');
  // UX r3 A1: the clear icon is the design system's, never a direct lucide import.
  expect(compactClear).toContain('<Icon name="x"');
  expect(compactClear).not.toMatch(/<X\b/);
});

// UX r3 B3 on #11339: on the fleet route the 248 px list switcher (toolbarLead) pushed
// the nowrap one-row toolbar 137 px past its box at 1080. The lead is measured and,
// when the room it leaves is under the one-row minimum, the row wraps. A selection
// (its count and bulk actions, #11666) wraps the row too.
test("a host lead never pushes the one-row toolbar past its box", () => {
  const table = readFileSync(join(SRC, "components/ui/data-table.tsx"), "utf8");
  expect(table).toContain("const ONE_ROW_TOOLBAR_MIN_PX = 852;");
  expect(table).toContain("boxWidth - leadWidth < ONE_ROW_TOOLBAR_MIN_PX");
  expect(table).toContain("const toolbarWraps = !filtersInPill || leadCrowds || selectionShown;");
  expect(table).toContain('toolbarWraps ? "flex-wrap" : "min-w-0 flex-nowrap"');
  expect(table).toContain("<div ref={setLead}");
});

// A one-row toolbar whose full labels run past its box (the Tasks lead at 1440: Status and
// Quick filters, #1004) records the lead and box widths it overflowed at and draws the
// compact controls from then on, measured before paint.
test("full labels that overflow the one-row toolbar fall back to the compact controls", () => {
  const table = readFileSync(join(SRC, "components/ui/data-table.tsx"), "utf8");
  expect(table).toContain("ref={setToolbarRow}");
  expect(table).toContain("toolbarRow.scrollWidth > toolbarRow.clientWidth + 1");
  expect(table).toContain("leadWidth >= fullLabelsOverflow.lead");
  expect(table).toContain("boxWidth <= fullLabelsOverflow.box");
  expect(table).toContain("boxWidth < COMPACT_TOOLBAR_BELOW_PX || leadNeedsCompact");
});

// The selected Properties row keeps its label in the primary ink: accent text on
// bg-accent/15 read 4.08:1 in dark at 12 px (UX r1 A1 on #10372). The accent
// stays on the icon and the check (3:1 for a graphic).
test("the selected Properties row draws its label in the primary ink, the accent on the icon", () => {
  const panel = readFileSync(join(SRC, "components/ui/property-filter.tsx"), "utf8");
  const rowClass = panel.match(/isSelected \? "([^"]*)" : "hover:bg-elevation-hover"/);
  expect(rowClass, "the selected row class is a literal in property-filter.tsx").not.toBeNull();
  expect(rowClass?.[1]).toContain("text-default-primary");
  expect(rowClass?.[1]).not.toMatch(/\btext-accent\b/);
  expect(panel).toMatch(/<Check className="[^"]*\btext-accent\b/);
});

// Tab walks the Properties rows and the chip remove buttons: each draws the
// design-system focus ring (UX r1 A1 on #10527).
test("the Properties rows and chip remove buttons draw the focus ring", () => {
  const panel = readFileSync(join(SRC, "components/ui/property-filter.tsx"), "utf8");
  const buttons = [...panel.matchAll(/<button\b[\s\S]*?<\/button>/g)].map((m) => m[0]);
  expect(buttons.length, "property-filter.tsx draws the remove and row buttons").toBe(2);
  for (const button of buttons) {
    expect(button).toMatch(/focus-visible:ring-2/);
    expect(button).toMatch(/focus-visible:ring-focus/);
  }
});

// The "Showing X-Y of Z" count never wraps and never runs under the control
// beside it: at 1024 in fr ("Affichage de 1 à 18 sur 18") it first broke onto
// two lines beside the 12 px sort switch (UX r1 B1 on #10420), then, kept on
// one line, overflowed its 124 px slot by 7 px under the switch (UX r2 B1).
// It truncates inside its slot (truncate = nowrap + overflow hidden, min-w-0
// so it can shrink) and a design-system Tip carries the whole text. jsdom does
// not lay out, so the guard reads the count span and its Tip in the source.
test("the list bar count stays on one line and inside its slot in every locale", () => {
  const source = readFileSync(join(SRC, "components/ui/list-control-bar.tsx"), "utf8");
  const block = source.match(/<Tip content=\{(\w+)\}>\s*<span data-testid="list-control-bar-count" className="([^"]*)">\s*\{(\w+)\}/);
  expect(block, "count span wrapped in a Tip").not.toBeNull();
  const classes = block?.[2].split(/\s+/) ?? [];
  expect(classes).toContain("truncate");
  expect(classes).toContain("min-w-0");
  expect(classes).not.toContain("shrink-0");
  // The Tip shows the very text the span draws.
  expect(block?.[1]).toBe(block?.[3]);
});

// One clear control (#9378 slice 4g): the DataTable toolbar, the Properties pill
// and the tag filter (inline row and collapsed panel) draw FilterClearButton,
// none keeps a hand-made clear button.
const CLEAR_FILES = ["components/ui/data-table.tsx", "components/ui/property-filter.tsx", "components/ui/tag-filter.tsx"];

/** Every raw `<button ...>...</button>` block of a source file. */
function rawButtons(source: string): string[] {
  const blocks: string[] = [];
  let from = source.indexOf("<button");
  while (from !== -1) {
    const end = source.indexOf("</button>", from);
    blocks.push(source.slice(from, end === -1 ? undefined : end));
    from = source.indexOf("<button", from + 1);
  }
  return blocks;
}

// Round 1 A2 of #10024: the clear controls drawn outside the DataTable kit
// (Agentic Flow focus picker, AI Adoption group picker, Evaluations run search)
// use the shared control too.
const PAGE_CLEAR_FILES = ["components/ui/agent-focus-picker.tsx", "pages/AiAdoption.tsx", "pages/Evaluations.tsx"];

test("the toolbar clear controls are the shared FilterClearButton", () => {
  for (const file of [...CLEAR_FILES, ...PAGE_CLEAR_FILES]) {
    const source = readFileSync(join(SRC, file), "utf8");
    expect(source, file).toContain("<FilterClearButton");
  }
});

// Round 1 B1: the presence of one FilterClearButton hid a second, hand-made
// clear in the collapsed tag panel. Count the raw clear buttons instead.
test("no toolbar keeps a hand-made clear button beside FilterClearButton", () => {
  for (const file of CLEAR_FILES) {
    const handMade = rawButtons(readFileSync(join(SRC, file), "utf8")).filter((block) =>
      /onChange\(\[\]\)/.test(block) || /onClick=\{clearAll\}/.test(block),
    );
    // The DataTable no-results sentence keeps "Clear all filters" as an inline
    // text link (round 1 A3): one raw clearAll button there, none elsewhere.
    const allowed = file === "components/ui/data-table.tsx" ? 1 : 0;
    expect(handMade.length, file).toBe(allowed);
  }
});

// One control height in the DataTable toolbar (UX run 8d20d29c on #10066: pills
// 28 px beside a 32 px page size and Export CSV): every toolbar control, the
// pills included, takes TOOLBAR_CONTROL_HEIGHT and no literal height of its own.
test("the DataTable toolbar draws every control at TOOLBAR_CONTROL_HEIGHT", () => {
  const source = readFileSync(join(SRC, "components/ui/data-table.tsx"), "utf8");
  expect(source).toContain('const TOOLBAR_CONTROL_HEIGHT = "h-8";');
  const uses = source.split("TOOLBAR_CONTROL_HEIGHT").length - 2;
  // search, the page's own search kind, filter pill, Clear all filters, Columns,
  // page size, Export CSV (standard v2, #10916); the total is the shared TotalChip
  // at the same 32 px (TOTAL_CHIP_HEIGHT, UX r1 B3 on #11192); the sort picker and its
  // direction button of a card list (`GridSort`, slice 8 of #9378); the icon-only
  // Clear all filters of a compact one-row toolbar (UX r2 B2 on #11339); the page box
  // slot of a selectable card list, first in the toolbar (#9378, 14 Nov 2026).
  expect(uses).toBe(11);
  expect(readFileSync(join(SRC, "components/ui/total-chip.tsx"), "utf8")).toContain('export const TOTAL_CHIP_HEIGHT = "h-8";');
  expect(source.replace('const TOOLBAR_CONTROL_HEIGHT = "h-8";', "")).not.toMatch(/["\s]h-[78]["\s]/);
});

// One control height on every list toolbar (UX r1 A3 on #10109: /agents drew
// 28 px Properties and Tags pills beside a 36 px search): the pill, the clear
// control and the toolbar search field are 32 px, like the visibility segments
// and the grid/list toggle (28 px buttons in a 2 px padded group). Chips listed
// inside a pill's panel keep PANEL_CHIP_HEIGHT.
test("list toolbars draw the pill, the clear control and the search at one 32 px height", () => {
  const pill = readFileSync(join(SRC, OWNER), "utf8");
  const pillBase = pill.slice(pill.indexOf("const PILL_BASE"), pill.indexOf("const PILL_ON"));
  expect(pillBase).toContain(" h-8 ");
  expect(pill).toContain('export const PANEL_CHIP_HEIGHT = "h-7";');
  const search = readFileSync(join(SRC, "components/ui/search-input.tsx"), "utf8");
  expect(search).toContain('const TOOLBAR_SEARCH_HEIGHT = "h-8";');
  expect(search).toContain("toolbar && TOOLBAR_SEARCH_HEIGHT");
  for (const [file, group] of [
    ["components/ui/segmented-filter.tsx", "p-0.5"],
    ["components/ui/view-toggle.tsx", "p-0.5"],
  ] as const) {
    const source = readFileSync(join(SRC, file), "utf8");
    expect(source, file).toContain("h-7");
    expect(source, file).toContain(group);
  }
  const tags = readFileSync(join(SRC, "components/ui/tag-filter.tsx"), "utf8");
  expect(tags.split("PANEL_CHIP_HEIGHT)").length - 1, "tag-filter panel chips and panel Clear").toBe(3);
});

// UX r1 B1 on #10117: 7 list pages (Fleets, Work items, Shared state, Worksets,
// Background tasks, Task history, Dashboards) drew a 36 px search beside 32 px
// pills because they keep their own width and so skip `toolbar`. Any file that
// draws a filter control beside a SearchInput gives that search the 32 px
// height: through `toolbar`, or TOOLBAR_SEARCH_HEIGHT as its inputClassName.
const FILTER_CONTROL = /<(PropertyFilter|TagFilter|VisibilityFilter|FilterPillTrigger|SegmentedFilter)\b/;

test("a search drawn beside a filter control is 32 px", () => {
  const offenders: string[] = [];
  for (const full of sourceFiles(SRC)) {
    const source = readFileSync(full, "utf8");
    if (!FILTER_CONTROL.test(source)) continue;
    for (const match of source.matchAll(/<SearchInput\b([\s\S]*?)\/>/g)) {
      const attributes = match[1];
      const toolbar = /(^|\s)toolbar(\s|=\{true\}|$)/.test(attributes);
      if (!toolbar && !attributes.includes("inputClassName={TOOLBAR_SEARCH_HEIGHT}")) {
        offenders.push(full.slice(SRC.length).split(sep).join("/"));
      }
    }
  }
  expect(offenders, "pass `toolbar` or inputClassName={TOOLBAR_SEARCH_HEIGHT}").toEqual([]);
});

// UX r2 B1-B3 on #10117: a picker drawn in the same toolbar as a 32 px search
// (relay Activity "Every relay", API keys "Any owner", OpenRouter author and
// Sort) stayed at 36 px. The pickers that follow the search inside its parent
// element are 32 px too: `h-8` in a SearchableSelect or SelectTrigger class,
// `height="h-8"` on a MultiSelect (its root class does not size the field).
const PICKER = /<(SearchableSelect|MultiSelect|SelectTrigger)\b([\s\S]*?)\/?>/g;

function toolbarPickersOffHeight(source: string): string[] {
  const offenders: string[] = [];
  for (const match of source.matchAll(/<SearchInput\b([\s\S]*?)\/>/g)) {
    const attributes = match[1];
    const toolbar = /(^|\s)toolbar(\s|=\{true\}|$)/.test(attributes);
    if (!toolbar && !attributes.includes("inputClassName={TOOLBAR_SEARCH_HEIGHT}")) continue;
    const start = match.index ?? 0;
    const indent = start - (source.lastIndexOf("\n", start) + 1);
    // The search's parent closes on the first line indented less than the search.
    const close = new RegExp(`\\n {0,${Math.max(indent - 1, 0)}}</`).exec(source.slice(start));
    const toolbarText = source.slice(start, close ? start + close.index : source.length);
    for (const picker of toolbarText.matchAll(PICKER)) {
      const [, name, pickerAttributes] = picker;
      const sized =
        name === "MultiSelect"
          ? /height="h-8"/.test(pickerAttributes)
          : /className=("|\{[^}]*?")[^"]*\bh-8\b/.test(pickerAttributes) ||
            /className=\{cn\(TOOLBAR_CONTROL_HEIGHT\b/.test(pickerAttributes);
      if (!sized) offenders.push(`${name}: ${pickerAttributes.trim().split("\n")[0]}`);
    }
  }
  return offenders;
}

test("a picker in the toolbar of a 32 px search is 32 px", () => {
  const offenders: string[] = [];
  for (const full of sourceFiles(SRC)) {
    const source = readFileSync(full, "utf8");
    // Both forms of the 32 px search count: `toolbar` (the /templates "All
    // kinds" picker sat at 36 px beside one, UX r3 on #10152) and the constant.
    if (!source.includes("<SearchInput")) continue;
    for (const offender of toolbarPickersOffHeight(source)) {
      offenders.push(`${full.slice(SRC.length).split(sep).join("/")} ${offender}`);
    }
  }
  expect(offenders, "h-8 on the picker class, height=\"h-8\" on a MultiSelect").toEqual([]);
});

test("the toolbar picker check reads only the search's own toolbar", () => {
  const page = [
    "  <div>",
    "    <SearchInput inputClassName={TOOLBAR_SEARCH_HEIGHT} value={q} />",
    '    <SearchableSelect className="w-56" />',
    '    <MultiSelect className="w-60" />',
    '    <SelectTrigger className="h-8 w-48" />',
    "  </div>",
    '  <SearchableSelect className="w-60" />',
  ].join("\n");
  expect(toolbarPickersOffHeight(page)).toEqual([
    'SearchableSelect: className="w-56"',
    'MultiSelect: className="w-60"',
  ]);
  expect(toolbarPickersOffHeight(page.replace('"w-56"', '"h-8 w-56"').replace('className="w-60" />\n    <Select', 'className="w-60" height="h-8" />\n    <Select'))).toEqual([]);
});

// A caller's slot controls sit in the same toolbar row: the fleet "Review
// actions" bulk buttons stayed 24 px beside 32 px controls (UX round 5 B1 on
// #10034). Every Button such a slot renders takes TOOLBAR_CONTROL_HEIGHT.
const SLOT_CALLERS = ["components/fleets/FleetGateReview.tsx"];

test("a DataTable toolbar or bulk-action slot draws its buttons at TOOLBAR_CONTROL_HEIGHT", () => {
  for (const file of SLOT_CALLERS) {
    const source = readFileSync(join(SRC, file), "utf8");
    const slots = [...source.matchAll(/(?:toolbar=\{|actions: \()[\s\S]*?<\/Button>/g)].map((m) => m[0]);
    expect(slots.length, file).toBeGreaterThan(0);
    for (const slot of slots) {
      const button = slot.slice(slot.lastIndexOf("<Button"));
      expect(button, file).toContain("className={TOOLBAR_CONTROL_HEIGHT}");
    }
  }
});

// The Properties pill at x 840 opened a 288 px
// panel to x 1128 and scrolled a 1024 page 104 px sideways.
test("the filter panel opens from the pill's right edge when its left edge would pass the viewport", () => {
  expect(filterPanelAlign({ left: 840, right: 949 }, 288, 1024)).toBe("right");
  expect(filterPanelAlign({ left: 840, right: 949 }, 288, 1440)).toBe("left");
  expect(filterPanelAlign({ left: 212, right: 320 }, 288, 1024)).toBe("left");
});

// The stacked From / To row of a DateRangeFilter needs about 390 px; the panel's
// default w-72 (288 px) let "To" and its calendar button end 102 px past the
// Audit logs panel (UX round 2 B1 on #10382). A panel that holds a stacked range
// sizes to its content with w-auto, as the Videos Media panel does.
test("a filter panel holding a stacked date range sizes to its content", () => {
  const offenders: string[] = [];
  for (const full of sourceFiles(SRC)) {
    const source = readFileSync(full, "utf8");
    for (const match of source.matchAll(/<FilterPillPanel\b([^>]*)>([\s\S]*?)<\/FilterPillPanel>/g)) {
      if (!/<DateRangeFilter\b[^>]*\bstacked\b/.test(match[2])) continue;
      const classes = /className="([^"]*)"/.exec(match[1])?.[1] ?? "";
      if (!classes.split(/\s+/).includes("w-auto")) offenders.push(full.slice(SRC.length).split(sep).join("/"));
    }
  }
  expect(offenders, "add w-auto to the FilterPillPanel className").toEqual([]);
});

// Inside a Filters pill the pickers take the panel's full width: a fixed
// w-[240px] owner picker beside a 200 px fleet picker drew two widths in one
// panel (UX r1 A1 on #10478). Every FleetFilter, FleetAgentPickerGroup and
// SearchableSelect between <FiltersPill> and </FiltersPill> is w-full or flex-1.
test("the pickers of a Filters pill fill the panel width", () => {
  const offenders: string[] = [];
  for (const full of sourceFiles(SRC)) {
    const source = readFileSync(full, "utf8");
    for (const match of source.matchAll(/<FiltersPill\b[\s\S]*?<\/FiltersPill>/g)) {
      for (const picker of match[0].matchAll(/<(FleetFilter|FleetAgentPickerGroup|SearchableSelect)\b([\s\S]*?)\/?>/g)) {
        const attrs = picker[2];
        const path = full.slice(SRC.length).split(sep).join("/");
        if (/w-\[\d+px\]/.test(attrs)) offenders.push(`${path}: ${picker[1]} has a fixed width`);
        if (picker[1] !== "SearchableSelect" && !/\bw-full\b/.test(attrs)) {
          offenders.push(`${path}: ${picker[1]} without w-full`);
        }
      }
    }
  }
  expect(offenders, "give the picker w-full (or flex-1 beside a button) inside the Filters pill").toEqual([]);
});

test("the filter panel stays on the left edge when the right edge would not fit either", () => {
  expect(filterPanelAlign({ left: 100, right: 180 }, 288, 300)).toBe("left");
});

test("a fitted filter panel stops above the viewport's bottom edge and keeps a usable height", () => {
  // The Cases Filters panel opened at y 248 in a 1080 viewport ran to y 1602 (UX r1 B1 on #1822).
  expect(filterPanelMaxHeight(248, 1080)).toBe(824);
  expect(filterPanelMaxHeight(248.6, 1080)).toBe(823);
  // A pill near the bottom still opens a panel the user can scroll inside.
  expect(filterPanelMaxHeight(1000, 1080)).toBe(160);
});

test("the Filters pill panel fits the viewport with its own scroller", () => {
  const source = readFileSync(join(SRC, OWNER), "utf8");
  const pill = source.slice(source.indexOf("export function FiltersPill("));
  expect(pill).toMatch(/<FilterPillPanel[\s\S]*?\bfitViewport\b[\s\S]*?>/);
  expect(source).toContain('fitViewport && "overflow-y-auto overscroll-contain"');
});
