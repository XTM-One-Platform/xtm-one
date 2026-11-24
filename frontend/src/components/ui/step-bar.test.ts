// SPDX-License-Identifier: Apache-2.0

/**
 * One step bar (UX round 1 B1 on #1092): the CTEM loop setup had copied the new
 * agent wizard's bar and dropped its tooltips. A guided flow renders
 * `StepBar`; a second `aria-current="step"` bar written anywhere else fails.
 */
import { readdirSync, readFileSync } from "node:fs";
import { join, relative, resolve } from "node:path";
import { describe, expect, it } from "vitest";

const SRC = resolve(__dirname, "..", "..");
const OWNER = join("components", "ui", "step-bar.tsx");

function* sources(dir: string): Generator<string> {
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const full = join(dir, entry.name);
    if (entry.isDirectory()) {
      if (entry.name !== "node_modules") yield* sources(full);
    } else if (entry.name.endsWith(".tsx") && !entry.name.includes(".test.")) {
      yield full;
    }
  }
}

describe("step bar", () => {
  it('is drawn by StepBar alone: no other file sets aria-current to "step"', () => {
    const offenders = [...sources(SRC)]
      .filter((file) => relative(SRC, file) !== OWNER)
      .filter((file) => /aria-current=\{[^}]*"step"|aria-current="step"/.test(readFileSync(file, "utf8")))
      .map((file) => relative(SRC, file));
    expect(offenders).toEqual([]);
  });

  it("is used by the new agent wizard and the CTEM loop setup", () => {
    for (const file of [join("pages", "NewAgentWizard.tsx"), join("components", "fleets", "CtemLoopSetupDialog.tsx")]) {
      expect(readFileSync(join(SRC, file), "utf8")).toMatch(/<StepBar\b/);
    }
  });

  it("keeps the per-step tooltip and the hit area", () => {
    const src = readFileSync(join(SRC, OWNER), "utf8");
    expect(src).toMatch(/<Tip content=\{step\.title\}>/);
    expect(src).toContain("py-2");
  });
});
