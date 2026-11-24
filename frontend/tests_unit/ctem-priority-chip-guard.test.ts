// @vitest-environment node
import { readFile } from "node:fs/promises";
import path from "node:path";
import { describe, expect, it } from "vitest";

/**
 * The High / Medium / Low priority of a CTEM briefing next step or recommended
 * action is drawn by ToneBadge (#1354), so it wears the same single chip style
 * as the case chips (design system 1.13.0, #1595), never again by a
 * hand-written wash pill from a class string in CTEM_PRIORITY_CONFIG.
 */

const PAGES = path.resolve(__dirname, "../src/pages/xtm-one");
const CALLERS = ["ctem/ActNowCard.tsx", "ctem/BriefingCard.tsx", "ExposureBriefing.tsx", "CtemCommandCenter.tsx"];

describe("CTEM priority chips", () => {
  it("map each priority to a HotTone, not to a class string", async () => {
    const source = await readFile(path.resolve(__dirname, "../src/lib/constants.ts"), "utf8");
    expect(source).toMatch(/CTEM_PRIORITY_CONFIG: Record<string, \{ label: string; tone: HotTone \}>/);
  });

  it("label through the priority context keys, never the bare Medium (medium term in fr, es, ja)", async () => {
    const source = await readFile(path.resolve(__dirname, "../src/lib/constants.ts"), "utf8");
    for (const level of ["High", "Medium", "Low"]) {
      expect(source).toContain(`label: "${level} (priority)"`);
    }
  });

  it("render through ToneBadge on every caller", async () => {
    for (const file of CALLERS) {
      const source = await readFile(path.join(PAGES, file), "utf8");
      expect(source, file).toMatch(/<ToneBadge tone=\{(prio|CTEM_PRIORITY_CONFIG\[[^\]]+\])\.tone\}/);
      expect(source, file).not.toMatch(/prio\.cls|CTEM_PRIORITY_CONFIG\[[^\]]+\]\.cls/);
    }
  });
});
