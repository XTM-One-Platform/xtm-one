/**
 * List page header - `yarn test`.
 *
 * Issue #10215 (header and hero density sweep, criteria 5 and 8): a list or
 * settings page draws its header through the shared `ListPageHeader` (title,
 * the "i" carrying its help sentence, actions on one row), never its own h1
 * and never a visible help paragraph under it. UX r1 B1 on #10382 found the
 * same title + InfoTip markup copied into five pages; this guard keeps them on
 * the shared component.
 */
import { test } from "vitest";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { join } from "node:path";

// vitest runs from frontend/ (CI and `yarn test`).
const ROOT = process.cwd();

const PAGES = [
  "src/pages/AuditLogs.tsx",
  "src/pages/Users.tsx",
  "src/pages/Groups.tsx",
  "src/pages/Settings.tsx",
  "src/pages/VideoStudio.tsx",
  // The member pages of the Users and groups and Connections families draw
  // the same header through NavFamilyHeader, which renders ListPageHeader.
  "src/pages/BrowserProfiles.tsx",
  "src/pages/Variables.tsx",
  "src/pages/Relays.tsx",
  // Fleets, its own page since #9301 N2c, takes the same header (N3b plan).
  "src/pages/Fleets.tsx",
  // The Operations hub: its help sentence moved from a paragraph under the
  // h1 into the title's "i" (#10215 slice c12, criterion 5).
  "src/pages/Operations.tsx",
  // The other Orchestration pages (#9301 N4, UX A1 of #10494): one header.
  "src/pages/Agents.tsx",
  "src/pages/Flows.tsx",
  // Concierge: its header also links to its settings (#9301 N4).
  "src/pages/Concierge.tsx",
  // The Work and Agent Studio pages that still drew their own title row (#9301 N5, UX A2 of #10508).
  "src/pages/Presentations.tsx",
  "src/pages/Templates.tsx",
  // Admin objects: its help sentence moved into the title's "i" (#10215 slice c21, criterion 5).
  "src/pages/AdminObjects.tsx",
  // Value: its subtitle moved into the title's "i" (#10215 slice c23, criterion 5).
  "src/pages/Value.tsx",
];

/** The shared list header, directly or through the nav family header that renders it. */
const HEADER = /<(ListPageHeader|NavFamilyHeader)\b/;

for (const file of PAGES) {
  test(`${file} draws its header through ListPageHeader`, () => {
    const source = readFileSync(join(ROOT, file), "utf8");
    const start = source.search(HEADER);
    assert.ok(start >= 0, `${file}: the page header is the shared ListPageHeader`);
    assert.equal(/<h1[\s>]/.test(source), false, `${file}: no page-local h1, the shared header draws it`);
    const header = source.slice(start);
    const end = header.indexOf("/>\n");
    assert.ok(end > 0, `${file}: ListPageHeader not closed`);
    assert.ok(/\binfoLabel=/.test(header) && /\binfo=/.test(header), `${file}: the header carries the page's help in its "i"`);
  });
}

test("the nav family header draws its title through ListPageHeader", () => {
  const source = readFileSync(join(ROOT, "src/components/layout/NavFamilyHeader.tsx"), "utf8");
  assert.ok(source.includes("<ListPageHeader"));
  assert.equal(/<h1[\s>]/.test(source), false);
});

test("ListPageHeader is the only list header drawing an h1 with an InfoTip", () => {
  const source = readFileSync(join(ROOT, "src/components/ui/list-page-header.tsx"), "utf8");
  assert.ok(source.includes("<h1"), "the shared header draws the title");
  assert.ok(source.includes("<InfoTip"), "the shared header draws the page's help as an InfoTip");
  assert.equal(source.includes("<p"), false, "no visible help paragraph in the shared header");
});
