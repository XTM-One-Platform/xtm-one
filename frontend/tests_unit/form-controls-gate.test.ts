// @vitest-environment node
import { readdir, readFile } from "node:fs/promises";
import path from "node:path";
import { describe, it, expect } from "vitest";

/**
 * The form gates of the "Forms" standard (dev-docs/reference/
 * frontend-design-system.md, "Forms > Checks"; #9375 slice 2).
 *
 * A form control is the full-size design-system control: 36 px or more, its
 * text at body size. Four patterns break that rule and each one fails here:
 *
 *   compact     an `<Input compact>`: 24 px with centred caption text, made for
 *               a table cell or a list row, never a form field.
 *   trigger     a `<SelectTrigger>` with `size="sm"` or an `h-6` / `h-7` /
 *               `h-8` class: the same, for a select.
 *   raw         a raw `<select>` or `<input>`: no design-system name, size,
 *               focus ring or error state.
 *   comma       a "comma-separated" list typed as free text: a list is a
 *               `TagInput`.
 *   date        an `<Input type="date">`: the browser's own field, which
 *               ignores the app's locale ("mm/dd/yyyy" in French); a day is
 *               the design system's DatePicker (`DateFilterInput`).
 *   gridTrigger a `<SelectTrigger>` with no width class inside a 2-column
 *               form grid: the trigger is w-fit, so it stops short of its
 *               track and its edge misses the field beside it (#10891 UX B3).
 *   captionLabel a raw `<label>` at caption size right above a `<Select>`:
 *               a form label is the design system's SelectLabel or Label, at
 *               body size, and it names the trigger (#10891 UX B4).
 *   describedValue a bare `<SelectValue />` whose items print a label and a
 *               description: the closed trigger repeats the whole item and
 *               cuts it; it shows the label only (#10891 UX B2, as #10876).
 *   truncatedValue a `<TruncatedText>` inside a `<SelectValue>`: Radix sets
 *               pointer-events: none on the value, so its tooltip never opens
 *               on hover; the tooltip goes on the trigger (#10891 UX r2 B1).
 *   iconOverlay an `<Input>` whose className pads the inner input's left
 *               side (`[&_input]:pl-*`) to make room for an icon painted
 *               over it: the leading icon is the Input's own `startIcon`
 *               slot, the trailing one its `endIcon` (#11052 UX r1 B2).
 *
 * An ALLOWED reason never says "the library has no X" when the design
 * system exports X (#11052 UX r1 B1: "no slider" while it ships Slider).
 *
 * Each gate has two lists. ALLOWED names a genuine table-cell or list-row
 * editor (or a control the library has no component for), each with its
 * reason; it is reviewed like code. PENDING is the ratchet of the files the
 * conversion slices of #9375 have not reached yet, with their count of
 * occurrences: a file may only go down. A new file, or a count that grows,
 * fails; a file converted (or brought below its count) must be taken off (or
 * its count lowered) in the same pull request, so the list only shrinks.
 *
 * Static, like the input-name and switch guards: a form behind a permission
 * or a rare state never renders in a test, and source has no such blind spot.
 */

const SRC = path.resolve(__dirname, "../src");

type Gate =
  | "compact"
  | "trigger"
  | "raw"
  | "comma"
  | "date"
  | "gridTrigger"
  | "captionLabel"
  | "describedValue"
  | "truncatedValue"
  | "iconOverlay";

/** Genuine cell or row editors, and controls with no library component. Reason required. */
const ALLOWED: Record<Gate, Record<string, string>> = {
  compact: {},
  trigger: {
    "components/fleets/FleetMembersView.tsx":
      "the role and state pickers of the members list toolbar: 32 px beside the 32 px toolbar search (design standards s.3, #9378), not form fields",
    "components/fleets/FleetOperations.tsx":
      "the list picker that leads each Operations list toolbar: 32 px beside the 32 px toolbar search and segments (design standards s.3, #10260 UX B2), not a form field",
    "components/integrations/integration-catalog-browser.tsx":
      "the Sort picker of the integrations catalog toolbar: 32 px beside the 32 px toolbar search (design standards s.3 and s.9, #9378), not a form field",
    "components/custom-dashboard/DashboardDesignBar.tsx":
      "the Zoom picker of the dashboard canvas toolbar: 32 px beside the 32 px icon buttons of the bar, not a form field",
    "components/video-studio/editor/PreviewStage.tsx":
      "the playback speed picker of the video transport bar: 32 px beside the transport buttons and the scrubber, not a form field",
    "pages/settings/ApiKeysAdminSection.tsx":
      "the Key exposure picker of the API keys list toolbar: 32 px beside the 32 px toolbar search and status segments (design standards s.3), not a form field",
  },
  raw: {
    "components/ui/agent-focus-picker.tsx":
      "the graph toolbar's ARIA combobox (aria-activedescendant over its own listbox): the library has no combobox that takes free text and keeps the list open, not a form field",
    "components/ui/input.tsx": "the Input adapter itself: the one raw input every form field is drawn from",
    "pages/custom-tool-detail/ConfigTab.tsx":
      "the type picker of an input-parameter row: a 28 px cell of the five-column parameter list editor (name, type, description, required, remove), beside the row's compact name and description cells, not a form field",
    "pages/flow-builder/QuickAddMenu.tsx":
      "the quick-add node search of the canvas popover menu: a combobox filter whose arrow, Enter and Escape keys drive the result list and close the menu, not a form field",
    "components/ui/prompt-generator.tsx":
      "the borderless prompt line inside the generator's accent strip, beside its send and cancel buttons: the strip is the field's frame, not a form field",
    "components/ui/tag-filter.tsx":
      "the search boxes inside the tag filter pill's popover panels: a popover search line, not a form field",
    "components/ui/tag-input.tsx":
      "the TagInput itself: the bare inner input that sits among the chips of the one framed box every list field is drawn from",
    "components/ui/variable-insert-button.tsx":
      "the search line inside the variable insert popover: a popover search line, not a form field",
    "components/ui/vault-picker.tsx":
      "the search line inside the vault secret popover: a popover search line, not a form field",
    "components/presentation/SlideJumpMenu.tsx":
      "the slide number search at the top of the slide jump menu, a keyboard listbox filter inside a popover, not a form field",
    "components/studio/StudioChrome.tsx":
      "the inline title of a studio bar (video, dashboard), sized to its text like a heading, not a form field",
  },
  comma: {},
  date: {},
  gridTrigger: {},
  captionLabel: {},
  describedValue: {},
  truncatedValue: {},
  iconOverlay: {},
};

/** Files not converted yet, with their count. Only shrinks (#9375 slice 3 onward). */
const PENDING: Record<Gate, Record<string, number>> = {
  compact: {},
  trigger: {},
  raw: {
    "components/ui/agent-graph-toolbar.tsx": 1,
  },
  comma: {},
  date: {},
  gridTrigger: {},
  captionLabel: {},
  describedValue: {},
  truncatedValue: {},
  iconOverlay: {},
};

/** The block that opens on the line at `start` and closes at the same indent. */
function indentedBlock(source: string, start: number): string {
  const lineStart = source.lastIndexOf("\n", start) + 1;
  const indent = /^[ \t]*/.exec(source.slice(lineStart))?.[0] ?? "";
  const close = source.indexOf(`\n${indent}</`, start);
  return source.slice(start, close === -1 ? undefined : close);
}

/** SelectTriggers with no width class inside a 2-column grid of a form. */
function gridTriggers(source: string): number {
  let n = 0;
  for (const grid of source.matchAll(/<div className="[^"]*\bgrid-cols-2\b[^"]*"/g)) {
    const block = indentedBlock(source, grid.index);
    n += openingTags(block, /<SelectTrigger[\s/>]/g).filter((tag) => !/\bw-(?:full|fit|\d|\[|auto|min|max)/.test(tag)).length;
  }
  return n;
}

/** Raw caption-size labels directly above a Select. */
function captionLabels(source: string): number {
  return [...source.matchAll(/<label\b[^>]*\bcontent-caption\b[^>]*>(?:(?!<\/label>)[\s\S])*<\/label>\s*<Select[\s>]/g)].length;
}

/** Bare SelectValues in a Select whose items render a label and a description. */
function describedValues(source: string): number {
  let n = 0;
  for (const select of source.matchAll(/<Select[\s>]/g)) {
    const end = source.indexOf("</Select>", select.index);
    if (end === -1) continue;
    const block = source.slice(select.index, end);
    if (!/<SelectValue\s*\/>/.test(block)) continue;
    const items = block.split(/<SelectItem[\s>]/).slice(1).map((item) => item.split("</SelectItem>")[0]);
    if (items.some((item) => (item.match(/<span[\s>]/g) ?? []).length >= 2)) n += 1;
  }
  return n;
}

/** TruncatedTexts rendered inside a SelectValue, where no pointer event reaches them. */
function truncatedValues(source: string): number {
  return [...source.matchAll(/<SelectValue\b[^>]*(?<!\/)>(?:(?!<\/SelectValue>)[\s\S])*?<TruncatedText[\s/>]/g)].length;
}

async function* walk(dir: string): AsyncGenerator<string> {
  for (const entry of await readdir(dir, { withFileTypes: true })) {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) yield* walk(full);
    else if (full.endsWith(".tsx") && !full.includes(".test.")) yield full;
  }
}

/** End of the JSX opening tag at `start`, tracking braces and strings. */
function openingTagEnd(source: string, start: number): number {
  let depth = 0;
  let inString: string | null = null;
  for (let i = start; i < source.length; i++) {
    const c = source[i];
    if (inString) {
      if (c === inString) inString = null;
      continue;
    }
    if (c === '"' || c === "'" || c === "`") {
      inString = c;
      continue;
    }
    if (c === "{") depth += 1;
    else if (c === "}") depth -= 1;
    else if (c === ">" && depth === 0) return i;
  }
  return -1;
}

function openingTags(source: string, pattern: RegExp): string[] {
  const tags: string[] = [];
  for (const match of source.matchAll(pattern)) {
    const end = openingTagEnd(source, match.index);
    tags.push(source.slice(match.index, end === -1 ? undefined : end + 1));
  }
  return tags;
}

async function scan(): Promise<Record<Gate, Record<string, number>>> {
  const found: Record<Gate, Record<string, number>> = {
    compact: {},
    trigger: {},
    raw: {},
    comma: {},
    date: {},
    gridTrigger: {},
    captionLabel: {},
    describedValue: {},
    truncatedValue: {},
    iconOverlay: {},
  };
  const add = (gate: Gate, file: string, n: number) => {
    if (n > 0) found[gate][file] = n;
  };
  for await (const file of walk(SRC)) {
    const source = await readFile(file, "utf8");
    const rel = path.relative(SRC, file).split(path.sep).join("/");
    add("compact", rel, openingTags(source, /<Input[\s/>]/g).filter((tag) => /\scompact(?=[\s/>=])/.test(tag)).length);
    add(
      "trigger",
      rel,
      openingTags(source, /<SelectTrigger[\s/>]/g).filter((tag) => /size=(?:"sm"|\{"sm"\})|\bh-[678]\b/.test(tag)).length,
    );
    // A file chooser or a hidden value has no design-system control: the
    // visible part is a Button or a drop zone, so it is not a form field.
    add(
      "raw",
      rel,
      openingTags(source, /<(?:select|input)[\s/>]/g).filter((tag) => !/\btype="(?:file|hidden)"/.test(tag)).length,
    );
    add("comma", rel, [...source.matchAll(/comma[- ]separated/gi)].length);
    add("date", rel, openingTags(source, /<Input[\s/>]/g).filter((tag) => /\stype=(?:"date"|\{"date"\})/.test(tag)).length);
    add("gridTrigger", rel, gridTriggers(source));
    add("captionLabel", rel, captionLabels(source));
    add("describedValue", rel, describedValues(source));
    add("truncatedValue", rel, truncatedValues(source));
    add("iconOverlay", rel, openingTags(source, /<Input[\s/>]/g).filter((tag) => /\[&_input\]:pl-/.test(tag)).length);
  }
  return found;
}

const GATES: Gate[] = [
  "compact",
  "trigger",
  "raw",
  "comma",
  "date",
  "gridTrigger",
  "captionLabel",
  "describedValue",
  "truncatedValue",
  "iconOverlay",
];

describe("Form controls gate (#9375)", () => {
  for (const gate of GATES) {
    it(`fails on a new ${gate} form control, outside the allow-list and the pending ratchet`, async () => {
      const found = (await scan())[gate];
      const over = Object.entries(found)
        .filter(([file, n]) => !(file in ALLOWED[gate]) && n > (PENDING[gate][file] ?? 0))
        .map(([file, n]) => `"${file}": ${n}, (pending ${PENDING[gate][file] ?? 0})`);
      expect(over, `${gate}: convert to the full-size control (frontend-design-system.md "Forms"):\n  ${over.join("\n  ")}`).toEqual([]);
    });

    it(`keeps the ${gate} pending list shrinking: a converted file is taken off`, async () => {
      const found = (await scan())[gate];
      const stale = Object.entries(PENDING[gate])
        .filter(([file, n]) => (found[file] ?? 0) < n)
        .map(([file, n]) => `"${file}": now ${found[file] ?? 0}, listed ${n}`);
      expect(stale, `${gate}: lower or remove these PENDING entries:\n  ${stale.join("\n  ")}`).toEqual([]);
    });

    it(`gives every allowed ${gate} exception a reason and a live occurrence`, async () => {
      const found = (await scan())[gate];
      const bad = Object.entries(ALLOWED[gate])
        .filter(([file, reason]) => reason.trim().length < 10 || !found[file])
        .map(([file]) => file);
      expect(bad).toEqual([]);
    });
  }

  it("flags a TruncatedText inside a SelectValue, not one beside it", () => {
    expect(truncatedValues("<SelectValue>\n  <TruncatedText text={label} />\n</SelectValue>")).toBe(1);
    expect(truncatedValues("<SelectValue>\n  <span>{label}</span>\n</SelectValue>\n<TruncatedText text={x} />")).toBe(0);
    expect(truncatedValues("<SelectValue />\n</Select>\n<TruncatedText text={x} />")).toBe(0);
  });

  it("never allows an exception because the library lacks a component it exports", async () => {
    const types = await readFile(path.resolve(__dirname, "../node_modules/@filigran/design-system/dist/index.d.ts"), "utf8");
    const exported = new Set(
      [...types.matchAll(/^export \{([^}]*)\}/gm)].flatMap((m) => m[1].split(",").map((name) => name.trim().split(/\s+/).pop() ?? "")),
    );
    expect(exported.has("Input") && exported.has("Slider")).toBe(true);
    const wrong = GATES.flatMap((gate) =>
      Object.entries(ALLOWED[gate]).flatMap(([file, reason]) =>
        [...reason.matchAll(/library has no ([a-z]+)(?=[,.;:]|$| and )/g)]
          .map((m) => m[1].charAt(0).toUpperCase() + m[1].slice(1))
          .filter((component) => exported.has(component))
          .map((component) => `${gate} ${file}: the library exports ${component}`),
      ),
    );
    expect(wrong).toEqual([]);
  });

  it("scans a meaningful number of files, so a broken walker cannot pass", async () => {
    let files = 0;
    for await (const _ of walk(SRC)) files += 1;
    expect(files).toBeGreaterThan(500);
  });
});
