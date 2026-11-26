// @vitest-environment node
import { readdir, readFile } from "node:fs/promises";
import path from "node:path";
import { describe, expect, it } from "vitest";

/**
 * A "+N more" chip list under pages/settings goes through the shared
 * ExpandableChipList (UX round 1 B1 of #10124): the regions of the identity
 * header and the PIRs of Settings > Organization each carried a copy of the
 * slice, the expanded state and the toggle, and their toggles already drew
 * differently. Static, like the other design-system guards.
 */

const SETTINGS = path.resolve(__dirname, "../src/pages/settings");
/** A local copy: an expanded state that toggles between "Show fewer" and "+N more". */
const COPY = /t_i18n\("Show fewer"\)\s*:\s*t_i18n\("\+\{count\} more"/;

async function sources(dir: string): Promise<string[]> {
  const entries = await readdir(dir, { withFileTypes: true });
  const files = await Promise.all(
    entries.map((entry) => {
      const full = path.join(dir, entry.name);
      if (entry.isDirectory()) return sources(full);
      return Promise.resolve(/\.tsx?$/.test(entry.name) && !/\.test\.tsx?$/.test(entry.name) ? [full] : []);
    }),
  );
  return files.flat();
}

describe("expandable chip lists under pages/settings", () => {
  it("use the shared ExpandableChipList, never a local +N more toggle", async () => {
    const offenders: string[] = [];
    for (const file of await sources(SETTINGS)) {
      if (COPY.test(await readFile(file, "utf8"))) offenders.push(path.relative(SETTINGS, file));
    }
    expect(offenders, "use ExpandableChipList from @/components/ui/expandable-chip-list").toEqual([]);
  });

  it("the regions of Settings > Organization use it (the PIRs are the Intelligence policy table since #2435)", async () => {
    for (const name of ["OrganizationSection.tsx"]) {
      const source = await readFile(path.join(SETTINGS, name), "utf8");
      expect(source, name).toContain('from "@/components/ui/expandable-chip-list"');
      expect(source, name).toContain("<ExpandableChipList");
    }
  });
});
