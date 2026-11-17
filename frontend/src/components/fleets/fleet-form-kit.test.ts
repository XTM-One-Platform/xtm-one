/**
 * Fleet forms are built from the settings form kit (#9174, slice 6b; UX round 2
 * of #9432) - `yarn test`.
 *
 * A fleet form never builds its own action bar or field anatomy: the sticky
 * Save and Cancel bar is `SettingsSaveBar` and each field is `SettingsField`
 * (label, helper, aria-describedby, error in place of the helper), from
 * `components/ui/settings-form.tsx`. This guard fails on a page-local
 * `sticky bottom-0` bar under components/fleets (test files excluded).
 */
import { describe, expect, it } from "vitest";
import { readdirSync, readFileSync } from "node:fs";
import { join, relative, sep } from "node:path";

// vitest runs from frontend/ (CI and `yarn test`).
const FLEETS = join(process.cwd(), "src", "components", "fleets");

function* sources(dir: string): Generator<string> {
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const full = join(dir, entry.name);
    if (entry.isDirectory()) yield* sources(full);
    else if (/\.tsx?$/.test(entry.name) && !/\.test\.tsx?$/.test(entry.name)) yield full;
  }
}

const read = (name: string) => readFileSync(join(FLEETS, name), "utf8");

describe("fleet forms use the settings form kit", () => {
  it("has no page-local sticky action bar under components/fleets", () => {
    const local: string[] = [];
    for (const file of sources(FLEETS)) {
      if (/\bsticky\b[^"'`]*\bbottom-0\b|\bbottom-0\b[^"'`]*\bsticky\b/.test(readFileSync(file, "utf8"))) {
        local.push(relative(FLEETS, file).split(sep).join("/"));
      }
    }
    expect(local).toEqual([]);
  });

  it("the About page has no hand editor: the documentation is AI generated only (#9768 S2)", () => {
    const about = read("FleetAboutView.tsx");
    expect(about).not.toMatch(/<Textarea\b/);
    expect(about).not.toMatch(/<SettingsSaveBar\b/);
    expect(about).toMatch(/useRegenerateFleetDocumentation/);
  });
});
