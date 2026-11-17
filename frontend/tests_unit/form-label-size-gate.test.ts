// @vitest-environment node
import { readdir, readFile } from "node:fs/promises";
import path from "node:path";
import { describe, it, expect } from "vitest";

/**
 * Form labels at body size (dev-docs/reference/frontend-design-system.md,
 * "Forms"; design standards section 2; #9375, UX round 1 B1 of #11053).
 *
 * The design system Label is 14 px / 500. A caption or compact size class on
 * a `<Label>` (content-compact, content-caption, text-xs, text-[9-12px])
 * shrinks a form label to 12 px or less under a 36 px field, a repeat finding
 * of the form reviews. This gate fails on any new one.
 *
 * PENDING is the ratchet of the files the conversion slices of #9375 have not
 * reached yet, with their count: a file may only go down, and a file brought
 * below its count is lowered (or taken off at 0) in the same pull request.
 *
 * A field heading drawn without the Label (a div, p, span or raw label with a
 * caption size, font-medium and text-default-secondary, a form control on the
 * next lines) renders at 10 to 12 px too (UX round 1 B1 of #11235): HEADING_PENDING
 * is its own ratchet. A read-only section heading (no control after it) is not
 * counted. A heading followed by a button group (role="group" or
 * role="radiogroup", UX round 1 B1 of #11285) is a field heading too, and so is
 * a heading over a file picker (an `<input type="file">` within the next
 * FILE_WINDOW lines, its trigger button in between: UX round 1 B1 of #11315).
 * Convert it to the Label, bound to the control with htmlFor or aria-labelledby.
 */

const SRC = path.resolve(__dirname, "../src");

const SMALL_TEXT = /\b(?:content-compact|content-caption|text-xs|text-\[(?:9|10|11|12)px\])(?=[\s"'`}]|$)/;

/** Files not converted yet, with their count. Only shrinks; empty since every slice of #9375 landed. */
const PENDING: Record<string, number> = {};

/** Field headings not converted to the Label yet, with their count. Only shrinks. */
const HEADING_PENDING: Record<string, number> = {
  "pages/assignment-detail/ParametersCard.tsx": 1,
  "pages/profile/agent-accounts/AgentAccountRules.tsx": 2,
  "pages/public/components.tsx": 1,
};

const HEADING_TAG = /<(?:div|p|span|label)\s+className="([^"]*)"[^>]*>/g;
const FORM_CONTROL = /<(?:Input|SearchableSelect|Select|SelectTrigger|Textarea|textarea|input|select|Switch|Checkbox|TagInput|FilterOptionList|[A-Z]\w*Picker|[A-Z]\w*Select)\b|\srole="(?:radio)?group"/;

const FILE_INPUT = /\stype="file"/;
const FILE_WINDOW = 30;

/** Caption-size field headings drawn without the Label, followed by a control. */
function smallFieldHeadings(source: string): number {
  const lines = source.split("\n");
  let n = 0;
  lines.forEach((line, i) => {
    for (const match of line.matchAll(HEADING_TAG)) {
      const cls = match[1];
      if (!cls.includes("font-medium") || !cls.includes("text-default-secondary") || !SMALL_TEXT.test(cls)) continue;
      const after = line.slice((match.index ?? 0) + match[0].length) + "\n" + lines.slice(i + 1, i + 4).join("\n");
      const fileWindow = lines.slice(i + 1, i + 1 + FILE_WINDOW).join("\n");
      if (FORM_CONTROL.test(after) || FILE_INPUT.test(fileWindow)) n += 1;
    }
  });
  return n;
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

function smallLabels(source: string): number {
  let n = 0;
  for (const match of source.matchAll(/<Label[\s/>]/g)) {
    const end = openingTagEnd(source, match.index);
    const tag = source.slice(match.index, end === -1 ? undefined : end + 1);
    if (SMALL_TEXT.test(tag)) n += 1;
  }
  return n;
}

async function scan(count: (source: string) => number = smallLabels): Promise<Record<string, number>> {
  const found: Record<string, number> = {};
  for await (const file of walk(SRC)) {
    const n = count(await readFile(file, "utf8"));
    if (n > 0) found[path.relative(SRC, file).split(path.sep).join("/")] = n;
  }
  return found;
}

describe("Form label size gate (#9375)", () => {
  it("fails on a new caption-size Label, outside the pending ratchet", async () => {
    const over = Object.entries(await scan())
      .filter(([file, n]) => n > (PENDING[file] ?? 0))
      .map(([file, n]) => `"${file}": ${n}, (pending ${PENDING[file] ?? 0})`);
    expect(over, `drop the size class: the design system Label is body size:\n  ${over.join("\n  ")}`).toEqual([]);
  });

  it("keeps the pending list shrinking: a converted file is taken off", async () => {
    const found = await scan();
    const stale = Object.entries(PENDING)
      .filter(([file, n]) => (found[file] ?? 0) < n)
      .map(([file, n]) => `"${file}": now ${found[file] ?? 0}, listed ${n}`);
    expect(stale, `lower or remove these PENDING entries:\n  ${stale.join("\n  ")}`).toEqual([]);
  });

  it("flags a size class on a Label, not on the field beside it", () => {
    expect(smallLabels('<Label className="content-compact">Name</Label>')).toBe(1);
    expect(smallLabels('<Label htmlFor="x" className="flex gap-1 text-xs">Name</Label>')).toBe(1);
    expect(smallLabels('<Label htmlFor="x">Name</Label>\n<Input className="content-compact" />')).toBe(0);
    expect(smallLabels('<Label className="text-default-secondary">Name</Label>')).toBe(0);
  });

  it("fails on a new caption-size field heading drawn without the Label", async () => {
    const over = Object.entries(await scan(smallFieldHeadings))
      .filter(([file, n]) => n > (HEADING_PENDING[file] ?? 0))
      .map(([file, n]) => `"${file}": ${n}, (pending ${HEADING_PENDING[file] ?? 0})`);
    expect(over, `use the design system Label, bound to the field:\n  ${over.join("\n  ")}`).toEqual([]);
  });

  it("keeps the heading pending list shrinking", async () => {
    const found = await scan(smallFieldHeadings);
    const stale = Object.entries(HEADING_PENDING)
      .filter(([file, n]) => (found[file] ?? 0) < n)
      .map(([file, n]) => `"${file}": now ${found[file] ?? 0}, listed ${n}`);
    expect(stale, `lower or remove these HEADING_PENDING entries:\n  ${stale.join("\n  ")}`).toEqual([]);
  });

  it("flags a field heading, not a read-only section heading", () => {
    expect(smallFieldHeadings('<div className="content-caption font-medium text-default-secondary">Scope</div>\n<Select value={v}>')).toBe(1);
    expect(smallFieldHeadings('<label className="content-compact font-medium text-default-secondary">Name</label>\n<Input value={v} />')).toBe(1);
    expect(smallFieldHeadings('<p className="content-caption font-medium text-default-secondary">Runs</p>\n<ul>\n<li>a</li>\n</ul>\n<Input />')).toBe(0);
    expect(smallFieldHeadings('<Label htmlFor="x">Scope</Label>\n<Select value={v}>')).toBe(0);
    expect(smallFieldHeadings('<label className="content-compact font-medium text-default-secondary">Type</label>\n<div role="group" className="flex gap-2">')).toBe(1);
    expect(smallFieldHeadings('<Label id="t">Type</Label>\n<div role="group" aria-labelledby="t">')).toBe(0);
    const filePicker = '<label className="content-compact font-medium text-default-secondary">Files</label>\n<ul>\n<li>a</li>\n</ul>\n<div>\n<input type="file" className="hidden" />';
    expect(smallFieldHeadings(filePicker)).toBe(1);
    // The column headings of the DataTable Filters pill, over their toggles (UX round 1 B1 of #12136).
    expect(smallFieldHeadings('<p className="content-compact font-medium text-default-secondary">Status</p>\n<FilterOptionList options={o} />')).toBe(1);
    expect(smallFieldHeadings(filePicker.replace(/<label[^>]*>Files<\/label>/, '<Label id="f">Files</Label>'))).toBe(0);
  });
});
