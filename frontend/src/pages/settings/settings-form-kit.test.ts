/**
 * Settings forms are built from the settings form kit (#9173, S1c) - `yarn test`.
 *
 * Two source guards over frontend/src/pages/settings (test files excluded):
 *   - no `compact` Input: that size (24 px, centred caption text) fits a table
 *     cell, never a form field;
 *   - no `text-[10px]` / `text-[11px]`: labels and help text read at the
 *     content scale;
 *   - no `content-caption` (10 px) outside a badge or a table cell: a help,
 *     status or error line reads at `content-compact` (12 px) at least, so a
 *     swap from `text-[11px]` to `content-caption` cannot pass the guard above.
 * The sections not yet moved onto the kit are listed below with their count.
 * The lists only shrink: a section moved onto the kit is deleted from them in
 * the same PR, a count that drops must be lowered, and a file that is not
 * listed fails on its first occurrence.
 */
import { describe, expect, it } from "vitest";
import { readdirSync, readFileSync } from "node:fs";
import { join, relative, sep } from "node:path";

// vitest runs from frontend/ (CI and `yarn test`).
const SETTINGS = join(process.cwd(), "src", "pages", "settings");

/** Files still carrying a `compact` Input, with the count; only ever shrinks. */
const COMPACT_INPUT_PENDING: Record<string, number> = {};

/** Files still carrying `text-[10px]` / `text-[11px]`, with the count; only ever shrinks. */
const MICRO_TEXT_PENDING: Record<string, number> = {
  "DeleteProviderDialog.tsx": 1,
  "ModelAnnouncements.tsx": 9,
  "ModelDirectory.tsx": 6,
};

/** Files still carrying `content-caption` outside a badge or a table cell, with the count; only ever shrinks. */
const CAPTION_TEXT_PENDING: Record<string, number> = {
  "ChannelManagement.tsx": 18,
  "HealthPetLibrary.tsx": 1,
  "GAHandoverPicker.tsx": 7,
  "GeneralAssistantSection.tsx": 10,
  "MCPEndpointSection.tsx": 14,
  "ModelAnnouncements.tsx": 4,
  "ModelDirectory.tsx": 4,
  "ModelPricingSection.tsx": 8,
  "OAuthDefaultsSection.tsx": 5,
  "OrganizationAiDraftDialog.tsx": 1,
  "PlatformModelSetting.tsx": 1,
  "PronunciationLexiconSection.tsx": 3,
  "ProviderCard.tsx": 17,
  "QuotasSection.tsx": 37,
  "ResourceToggleCard.tsx": 1,
  "RoiUnitsSection.tsx": 1,
  "SecurityAuditSection.tsx": 3,
  "ToolApprovalsSection.tsx": 1,
  "WorkspaceKnowledgeSettingsCard.tsx": 2,
};

/** The elements where the 10 px caption scale stays allowed: a badge, a table cell. */
const CAPTION_ALLOWED_TAGS = new Set(["Badge", "EditionBadge", "TableCell", "TableHead", "td", "th"]);

function* sources(dir: string): Generator<string> {
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const full = join(dir, entry.name);
    if (entry.isDirectory()) yield* sources(full);
    else if (/\.tsx?$/.test(entry.name) && !/\.test\.tsx?$/.test(entry.name)) yield full;
  }
}

/** The `<Input ... />` tags of a source whose attributes include a bare `compact`. */
function countCompactInputs(source: string): number {
  let count = 0;
  for (const match of source.matchAll(/<Input\b/g)) {
    const start = (match.index ?? 0) + match[0].length;
    const end = source.indexOf("/>", start);
    const attributes = source.slice(start, end === -1 ? undefined : end);
    if (/(^|\s)compact(?=[\s/]|$)/.test(attributes)) count += 1;
  }
  return count;
}

function countMicroText(source: string): number {
  return source.match(/text-\[1[01]px\]/g)?.length ?? 0;
}

/** `content-caption` occurrences whose enclosing tag is not a badge or a table cell. */
function countCaptionText(source: string): number {
  let count = 0;
  for (const match of source.matchAll(/\bcontent-caption\b/g)) {
    const open = source.lastIndexOf("<", match.index ?? 0);
    const tag = open === -1 ? null : /^<([A-Za-z][\w.]*)/.exec(source.slice(open))?.[1];
    if (!tag || !CAPTION_ALLOWED_TAGS.has(tag)) count += 1;
  }
  return count;
}

/**
 * `<SettingsSaveBar>` tags opened right inside a `<div>`: a wrapper as tall as
 * the bar leaves `sticky bottom-0` no room, so Cancel and Save scroll away with
 * the form (UX r1 B1 on #9949). The bar is a child of the form content itself.
 */
function countWrappedSaveBars(source: string): number {
  let count = 0;
  for (const match of source.matchAll(/<SettingsSaveBar\b/g)) {
    const before = source.slice(Math.max(0, (match.index ?? 0) - 400), match.index);
    if (/<div\b[^<>]*>\s*$/.test(before)) count += 1;
  }
  return count;
}

/**
 * `<textarea>` tags with neither an `id` (named by its SettingsField label)
 * nor an `aria-label`: a placeholder is not a name (UX r2 B1 on #9976, the
 * licence PEM field).
 */
function countUnnamedTextareas(source: string): number {
  let count = 0;
  for (const match of source.matchAll(/<textarea\b/g)) {
    const start = (match.index ?? 0) + match[0].length;
    const end = source.indexOf("/>", start);
    const attributes = source.slice(start, end === -1 ? undefined : end);
    if (!/(^|\s)(id|aria-label|aria-labelledby)=/.test(attributes)) count += 1;
  }
  return count;
}

/**
 * `size="sm"` buttons inside a DataTable `rowActions={...}` prop: a row action of a
 * settings row editor is the shared `RowRemoveButton` (32 px, 16 px glyph) or a full-size
 * icon button, never a 28x24 px small button beside 32 px ones (UX r1 B1 on #10853).
 */
function countSmallRowActions(source: string): number {
  let count = 0;
  for (const match of source.matchAll(/\browActions=\{/g)) {
    const start = (match.index ?? 0) + match[0].length;
    let depth = 1;
    let end = start;
    for (; end < source.length && depth > 0; end++) {
      if (source[end] === "{") depth += 1;
      else if (source[end] === "}") depth -= 1;
    }
    const body = source.slice(start, end);
    for (const button of body.matchAll(/<(Button|IconButton)\b[^>]*>/g)) {
      if (/\bsize="sm"/.test(button[0])) count += 1;
    }
  }
  return count;
}

/** Settings textareas still without a name, shrink only. */
const UNNAMED_TEXTAREA_PENDING: Record<string, number> = {};

function measure(count: (source: string) => number): Record<string, number> {
  const found: Record<string, number> = {};
  for (const file of sources(SETTINGS)) {
    const n = count(readFileSync(file, "utf8"));
    if (n > 0) found[relative(SETTINGS, file).split(sep).join("/")] = n;
  }
  return found;
}

describe("settings form kit guards", () => {
  it("counts a bare compact attribute on an Input only", () => {
    expect(countCompactInputs('<Input compact value="a" />')).toBe(1);
    expect(countCompactInputs('<Input\n  aria-label="x"\n  compact\n  value="a"\n/>')).toBe(1);
    expect(countCompactInputs('<Input value="a" />')).toBe(0);
    expect(countCompactInputs('<Input className="compact-row" />')).toBe(0);
    expect(countCompactInputs("<Button compact />")).toBe(0);
  });

  it("puts no compact Input in a settings form beyond the pending list", () => {
    expect(measure(countCompactInputs)).toEqual(COMPACT_INPUT_PENDING);
  });

  it("puts no 10 px or 11 px text in a settings section beyond the pending list", () => {
    expect(measure(countMicroText)).toEqual(MICRO_TEXT_PENDING);
  });

  it("counts content-caption outside a badge or a table cell only", () => {
    expect(countCaptionText('<p className="content-caption">a</p>')).toBe(1);
    expect(countCaptionText('<span\n  className="flex content-caption"\n>')).toBe(1);
    expect(countCaptionText('<Badge variant="outline" className="content-caption">a</Badge>')).toBe(0);
    expect(countCaptionText('<TableCell className="content-caption">a</TableCell>')).toBe(0);
    expect(countCaptionText('<p className="content-compact">a</p>')).toBe(0);
  });

  it("puts no 10 px caption text in a settings section beyond the pending list", () => {
    expect(measure(countCaptionText)).toEqual(CAPTION_TEXT_PENDING);
  });

  it("counts a save bar opened right inside a div only", () => {
    expect(countWrappedSaveBars('<div className="-mx-4 -mb-4 mt-6">\n  <SettingsSaveBar dirty />')).toBe(1);
    expect(countWrappedSaveBars("{editing && <SettingsSaveBar dirty />}")).toBe(0);
    expect(countWrappedSaveBars("</section>\n{editing && (\n  <SettingsSaveBar dirty />")).toBe(0);
  });

  it("keeps every save bar sticky: never inside a wrapper of its own height", () => {
    expect(measure(countWrappedSaveBars)).toEqual({});
  });

  it("counts a textarea with no id and no aria-label only", () => {
    expect(countUnnamedTextareas('<textarea placeholder="PEM" />')).toBe(1);
    expect(countUnnamedTextareas("<textarea\n  id={pemId}\n  ref={ref}\n/>")).toBe(0);
    expect(countUnnamedTextareas('<textarea aria-label={name} rows={4} />')).toBe(0);
  });

  it("counts a small button inside a rowActions prop only", () => {
    expect(countSmallRowActions('rowActions={(row) => (\n  <Button\n    variant="ghost"\n    size="sm"\n    aria-label="Remove"\n  >')).toBe(1);
    expect(countSmallRowActions('rowActions={(row) => <RowRemoveButton label="Remove" onClick={() => drop(row)} />}')).toBe(0);
    expect(countSmallRowActions('<Button size="sm">Add</Button>')).toBe(0);
  });

  it("draws every settings row action at full size: no small button in a DataTable rowActions", () => {
    expect(measure(countSmallRowActions)).toEqual({});
  });

  it("names every settings textarea beyond the pending list", () => {
    expect(measure(countUnnamedTextareas)).toEqual(UNNAMED_TEXTAREA_PENDING);
  });

  it("validates the required licence PEM inline on an empty submit, never a silent return", () => {
    const source = readFileSync(join(SETTINGS, "ParametersSection.tsx"), "utf8");
    const start = source.indexOf("const handleInstall = () => {");
    expect(start).toBeGreaterThan(-1);
    const body = source.slice(start, source.indexOf("update.mutate(", start));
    expect(body).not.toMatch(/\.trim\(\)\)\s*return/);
    expect(body).toContain('setError(t_i18n("Paste the license certificate."))');
    expect(body).toContain("textareaRef.current?.focus()");
    for (const lang of ["en", "es", "fr", "ja"]) {
      const catalog = JSON.parse(
        readFileSync(join(process.cwd(), "src", "i18n", "lang", `${lang}.json`), "utf8"),
      ) as Record<string, string>;
      expect(catalog["Paste the license certificate."], lang).toBeTruthy();
    }
  });
});
