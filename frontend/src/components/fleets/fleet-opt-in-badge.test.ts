/**
 * One component per pattern (UX round 1 on #9835, B2; design round 1 B4 of
 * #9769): a fleet's opt-in state (Not set up, Partially on, On, Paused) is
 * rendered by `FleetOptInBadge` alone, so the header pill, the About hero and
 * the CTEM fleet activity tiles never say or colour the same state differently.
 */
import { readdirSync, readFileSync } from "node:fs";
import { join, relative } from "node:path";

import { describe, expect, it } from "vitest";

// vitest runs from frontend/ (CI and `yarn test`), as ImageLightbox.test.tsx.
const SRC = join(process.cwd(), "src");
const OWNER = "components/fleets/FleetOptInBadge.tsx";

function* sourceFiles(dir: string): Generator<string> {
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const full = join(dir, entry.name);
    if (entry.isDirectory()) {
      if (entry.name !== "node_modules" && entry.name !== "i18n") yield* sourceFiles(full);
    } else if (/\.tsx?$/.test(entry.name) && !/\.test\.tsx?$/.test(entry.name)) {
      yield full;
    }
  }
}

describe("one fleet opt-in badge", () => {
  it("the opt-in labels live in FleetOptInBadge.tsx only", () => {
    const labels = [/"Partially on"/, /"On \(fleet opt-in\)"/, /"Paused \(fleet opt-in\)"/];
    const owners = new Set<string>();
    for (const file of sourceFiles(SRC)) {
      const text = readFileSync(file, "utf8");
      if (labels.some((label) => label.test(text))) owners.add(relative(SRC, file).replace(/\\/g, "/"));
    }
    expect([...owners]).toEqual([OWNER]);
  });

  it("no other fleet component keeps its own status label or variant map", () => {
    const dir = join(SRC, "components/fleets");
    const offenders = [...sourceFiles(dir)]
      .filter((file) => /\b(STATUS_LABELS|STATUS_VARIANT|FLEET_OPT_IN_LABELS|FLEET_OPT_IN_VARIANTS)\b/.test(readFileSync(file, "utf8")))
      .map((file) => relative(SRC, file).replace(/\\/g, "/"))
      .filter((file) => file !== OWNER && file !== "components/fleets/FleetMembersPanel.tsx");
    expect(offenders).toEqual([]);
  });
});
