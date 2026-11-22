// Every CTEM sub page draws the one CTEM header band (#11660): the back link
// on the breadcrumb line, never a BackButton row of its own, and never a
// page-local h1 (#12273, the exposure briefing was left out of the sweep).

import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

const PAGES = ["ExposureBriefing.tsx", "CtemStageDetail.tsx", "CtemGovernance.tsx", "CtemCaseDetail.tsx"];

describe("CTEM sub page headers", () => {
  it.each(PAGES)("%s renders CtemPageHeader with a back link and no BackButton or h1 of its own", (file) => {
    const text = readFileSync(join(process.cwd(), "src", "pages", "xtm-one", file), "utf8");
    expect(text).toMatch(/<CtemPageHeader\b/);
    expect(text).toMatch(/back=\{\{ to: "[^"]+", label: "Back to [^"]+" \}\}/);
    expect(text).not.toMatch(/<BackButton\b/);
    expect(text).not.toMatch(/<h1\b/);
  });

  // Cases is a top-level menu entry (#1176): its header has no back link, never "Back to Remediation".
  it("CtemCases.tsx renders CtemPageHeader with no back link", () => {
    const text = readFileSync(join(process.cwd(), "src", "pages", "xtm-one", "CtemCases.tsx"), "utf8");
    expect(text).toMatch(/<CtemPageHeader\b/);
    expect(text).not.toMatch(/back=\{\{/);
    expect(text).not.toContain("Back to Remediation");
    expect(text).not.toMatch(/<BackButton\b/);
    expect(text).not.toMatch(/<h1\b/);
  });

  it("the exposure briefing draws its AI mark with a type token and the DS icon, no raw size", () => {
    const text = readFileSync(join(process.cwd(), "src", "pages", "xtm-one", "ExposureBriefing.tsx"), "utf8");
    const mark = text.indexOf('data-testid="ctem-briefing-ai-mark"');
    expect(mark).toBeGreaterThan(-1);
    const tag = text.slice(text.lastIndexOf("<span", mark), text.indexOf("</span>", mark));
    expect(tag).toContain("content-caption");
    expect(tag).toContain('<Icon name="sparkles"');
    expect(tag).not.toMatch(/text-\[\d+px\]/);
  });
});
