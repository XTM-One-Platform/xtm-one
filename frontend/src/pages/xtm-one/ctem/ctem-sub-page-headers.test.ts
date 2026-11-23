// Every CTEM sub page draws the one CTEM header band (#11660): the back link
// on the breadcrumb line, never a BackButton row of its own, and never a
// page-local h1 (#12273, the exposure briefing was left out of the sweep).

import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

const PAGES = ["ExposureBriefing.tsx", "CtemStageDetail.tsx", "CtemGovernance.tsx"];

/** The pages on the app's standard headers since #1367 (Sam, 23 Nov 2026: no CTEM band with a surface and a border). */
const LIST_PAGES = ["CtemCases.tsx", "CtemInventory.tsx"];
const ENTITY_PAGES = ["CtemCaseDetail.tsx", "CtemAssetDetail.tsx", "CtemSoftwareDetail.tsx"];

describe("CTEM sub page headers", () => {
  it.each(LIST_PAGES)("%s renders the standard ListPageHeader, never the CTEM band", (file) => {
    const text = readFileSync(join(process.cwd(), "src", "pages", "xtm-one", file), "utf8");
    expect(text).toMatch(/<ListPageHeader\b/);
    expect(text).not.toMatch(/CtemPageHeader/);
    expect(text).not.toMatch(/<h1\b/);
  });

  it.each(ENTITY_PAGES)("%s renders the standard EntityPageHeader with a Back to link, never the CTEM band", (file) => {
    const text = readFileSync(join(process.cwd(), "src", "pages", "xtm-one", file), "utf8");
    expect(text).toMatch(/<EntityPageHeader\b/);
    expect(text).toMatch(/backLabel=("Back to [^"]+"|\{[^}]*"Back to [^"]+")/);
    expect(text).not.toMatch(/CtemPageHeader/);
    expect(text).not.toMatch(/<h1\b/);
  });

  it("CtemCases.tsx, a row of the CTEM menu group: no back link, no Recompute", () => {
    const text = readFileSync(join(process.cwd(), "src", "pages", "xtm-one", "CtemCases.tsx"), "utf8");
    expect(text).not.toMatch(/Recompute/);
    expect(text).not.toMatch(/useToast/);
    expect(text).toMatch(/<CasesFreshness\b/);
    expect(text).not.toMatch(/Back to Remediation/);
    expect(text).not.toMatch(/<BackButton\b/);
    expect(text).not.toMatch(/<h1\b/);
  });

  it("CtemSignals.tsx, a row of the CTEM menu group since #1401, renders the standard ListPageHeader with no back link", () => {
    const text = readFileSync(join(process.cwd(), "src", "pages", "xtm-one", "CtemSignals.tsx"), "utf8");
    expect(text).toMatch(/<ListPageHeader\b/);
    expect(text).not.toMatch(/CtemPageHeader/);
    expect(text).not.toMatch(/Back to Command Center/);
    expect(text).not.toMatch(/<h1\b/);
  });

  it.each(PAGES)("%s renders CtemPageHeader with a back link and no BackButton or h1 of its own", (file) => {
    const text = readFileSync(join(process.cwd(), "src", "pages", "xtm-one", file), "utf8");
    expect(text).toMatch(/<CtemPageHeader\b/);
    expect(text).toMatch(/back=\{\{ to: "[^"]+", label: "Back to [^"]+" \}\}/);
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
