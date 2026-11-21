// SPDX-License-Identifier: Apache-2.0

// Gate: the CTEM case sync settings (OrganizationTicketingSync) stay on the
// Forms standard (Design standards s.2), as the UX round 1 of the ticketing
// sync asked: a list is a tag input, never comma-separated text, and every
// field carries its info bubble in edit and read mode.

import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, it } from "vitest";

const SOURCE = readFileSync(resolve(__dirname, "OrganizationTicketingSync.tsx"), "utf8");

describe("OrganizationTicketingSync form standard", () => {
  it("edits the labels with the shared tag input, without platform tag suggestions", () => {
    expect(SOURCE).toMatch(/<TagInput[\s\S]*?id=\{`\$\{idPrefix\}-labels`\}[\s\S]*?suggestFromPlatformTags=\{false\}/);
    expect(SOURCE).not.toMatch(/split\(","\)/);
    expect(SOURCE).not.toContain("separated by commas");
  });

  it("gives every field an info bubble in edit mode", () => {
    const fields = SOURCE.match(/<Settings(?:Switch)?Field\b[\s\S]*?>/g) ?? [];
    // create on, case reopened, record type, priority (one per severity, one in the source), labels, two switches
    expect(fields.length).toBe(7);
    for (const field of fields) expect(field).toMatch(/info=\{info\("/);
  });

  it("gives every tile an info bubble in read mode", () => {
    const tiles = SOURCE.match(/<SettingsReadField\b[\s\S]*?\/>/g) ?? [];
    expect(tiles.length).toBe(8);
    for (const tile of tiles) expect(tile).toMatch(/info=\{info\("/);
  });

  it("offers a ServiceNow target the three tables the sync writes to, each with its meaning, never free text", () => {
    expect(SOURCE).toMatch(/serviceNow \? \(\s*<SearchableSelect[\s\S]*?id=\{`\$\{idPrefix\}-record-type`\}[\s\S]*?options=\{\[\s*\.\.\.SERVICENOW_RECORD_TYPES\.map\([\s\S]*?description: t_i18n\(SERVICENOW_RECORD_TYPE_MEANINGS\[value\]\)/);
    for (const table of ["incident", "change_request", "problem"]) expect(SOURCE).toMatch(new RegExp(`\\n  ${table}: "[A-Z]`));
  });

  it("marks a refused ServiceNow table inline on the field, from aria-invalid, not in a toast", () => {
    expect(SOURCE).toMatch(/error=\{\s*recordTypeRefused\s*\?\s*t_i18n\(/);
    expect(SOURCE).toMatch(/aria-invalid=\{recordTypeRefused \|\| undefined\}/);
  });

  it("names the refused table in the error and keeps it in the trigger as a disabled option", () => {
    expect(SOURCE).toMatch(/t_i18n\("The table \{value\} has no ticketing sync\. Pick incident, change_request or problem\.", \{ value: sync\.record_type\.trim\(\) \}\)/);
    expect(SOURCE).toMatch(/recordTypeRefused \? \[\{ value: sync\.record_type, label: sync\.record_type, disabled: true \}\]/);
  });

  it("keeps the record type select at the column width: no Clear control outside the trigger", () => {
    expect(SOURCE).not.toMatch(/\bclearable\b/);
  });

  it("explains the status mapping with an info bubble", () => {
    expect(SOURCE).toMatch(/<InfoTip content=\{[\s\S]*?info\("status_map"\)/);
  });
});
