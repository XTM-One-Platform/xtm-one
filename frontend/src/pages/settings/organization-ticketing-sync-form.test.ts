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

  it("explains the status mapping with an info bubble", () => {
    expect(SOURCE).toMatch(/<InfoTip content=\{[\s\S]*?info\("status_map"\)/);
  });
});
