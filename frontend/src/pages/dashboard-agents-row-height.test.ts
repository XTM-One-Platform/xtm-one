// Gate: every row of the home dashboard Agents widget keeps one height with nothing clipped
// (Design standards s.4; the "Company-managed" badge wrapped onto two lines in the 325 px
// widget at 1728, the row content grew to 83 px inside the 56 px row and the stats line was cut).

import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, it } from "vitest";

const SOURCE = readFileSync(resolve(__dirname, "Dashboard.tsx"), "utf8");
const AGENT_CARD = SOURCE.slice(SOURCE.indexOf("function AgentCard("), SOURCE.indexOf("function ConversationCard("));

describe("Home dashboard Agents widget rows", () => {
  it("reads the AgentCard source", () => {
    expect(AGENT_CARD.length).toBeGreaterThan(0);
  });

  it("marks a company-managed agent with the icon, not the labelled badge", () => {
    expect(AGENT_CARD).not.toMatch(/\bCompanyManagedBadge\b/);
    expect(AGENT_CARD).toMatch(/agent\.is_company_managed && <CompanyManagedIcon label="Company-managed agent" \/>/);
  });

  it("names a cut agent name with the design-system tooltip", () => {
    expect(AGENT_CARD).toMatch(/<TruncatedText text=\{agent\.name\}/);
  });

  it("centres the content in the 56 px row instead of padding it from the top", () => {
    expect(AGENT_CARD).toMatch(/overflow-hidden h-14/);
    expect(AGENT_CARD).toMatch(/className="relative flex h-full items-center px-3"/);
    expect(AGENT_CARD).not.toMatch(/className="relative p-3"/);
  });
});
