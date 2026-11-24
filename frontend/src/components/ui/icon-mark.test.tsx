// Every icon-only mark of an entity card is a tab stop with its own name:
// the shared IconMark trigger,
// never a bare svg with an aria-label inside a Tip, which Tab skips and whose
// tooltip opens on pointer hover only.

import type { ReactElement } from "react";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";
import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";

import { CompanyManagedIcon } from "@/components/ui/company-managed-badge";
import { IconMark } from "@/components/ui/icon-mark";
import { OwnerDeactivatedIcon } from "@/components/ui/owner-deactivated-notice";
import { SharedIcon } from "@/components/ui/shared-badge";
import { renderWithProviders } from "@/testing/providers";
import { FlowCard } from "@/pages/flows/LibraryTab";
import type { FlowSummary } from "@/types";

describe("IconMark", () => {
  it("takes keyboard focus and opens its tooltip on focus", async () => {
    const user = userEvent.setup();
    renderWithProviders(
      <IconMark label="View only">
        <svg aria-hidden="true" />
      </IconMark>,
    );

    await user.tab();
    const mark = screen.getByRole("img", { name: "View only" });
    expect(document.activeElement).toBe(mark);
    expect((await screen.findByRole("tooltip")).textContent).toContain("View only");
  });

  it("shows the richer tip while keeping the short name", async () => {
    const user = userEvent.setup();
    renderWithProviders(
      <IconMark label="Paused" tip="Paused because the owner left">
        <svg aria-hidden="true" />
      </IconMark>,
    );

    await user.tab();
    expect(document.activeElement).toBe(screen.getByRole("img", { name: "Paused" }));
    expect((await screen.findByRole("tooltip")).textContent).toContain("Paused because the owner left");
  });
});

describe("card icon marks", () => {
  const marks: Array<[string, () => ReactElement]> = [
    ["Company-managed", () => <CompanyManagedIcon />],
    ["Shared", () => <SharedIcon />],
    ["Owned and shared by Ada", () => <SharedIcon ownerName="Ada" />],
    ["Paused: the owner's account is deactivated", () => <OwnerDeactivatedIcon entity="flow" />],
  ];

  it.each(marks)("%s is a named tab stop whose glyph is hidden", (name, render) => {
    renderWithProviders(render());
    const mark = screen.getByRole("img", { name });
    expect(mark.getAttribute("tabindex")).toBe("0");
    const svg = mark.querySelector("svg");
    expect(svg?.getAttribute("aria-hidden")).toBe("true");
    expect(svg?.hasAttribute("aria-label")).toBe(false);
  });

  // The pages keep their marks private (ViewOnlyIcon, PublicLinkIcon,
  // AgentGateIcon, AgentStatusIcon...), so their sources are read: no bare
  // self-closing icon with an aria-label as the whole child of a Tip.
  const BARE_ICON_IN_TIP = /<Tip\b[^>]*>\s*<[A-Z]\w*\b[^<>]*\baria-label=[^<>]*\/>\s*<\/Tip>/;
  const cardSources = [
    "src/components/ui/company-managed-badge.tsx",
    "src/components/ui/shared-badge.tsx",
    "src/components/ui/owner-deactivated-notice.tsx",
    "src/pages/Agents.tsx",
    "src/pages/Presentations.tsx",
    "src/pages/VideoStudio.tsx",
    "src/pages/flows/LibraryTab.tsx",
  ];

  it.each(cardSources)("%s renders no bare svg inside a Tip", (path) => {
    const source = readFileSync(resolve(process.cwd(), path), "utf8");
    expect(source).not.toMatch(BARE_ICON_IN_TIP);
  });

  it("the gate pattern catches the drift it guards", () => {
    expect(`<Tip content={text}>\n  <Eye className="h-3.5 w-3.5" aria-label={text} />\n</Tip>`).toMatch(BARE_ICON_IN_TIP);
    expect(`<Tip content={t}>\n  <Button aria-label={t}>\n    <X />\n  </Button>\n</Tip>`).not.toMatch(BARE_ICON_IN_TIP);
  });

  // A focusable mark inside an <a> is invalid markup and the link absorbs its
  // name: the flow card keeps its marks
  // outside the link, as the agent and presentation cards do.
  it("a flow card keeps its focusable marks outside any link", () => {
    const flow: FlowSummary = {
      id: "f1",
      user_id: "someone-else",
      owner_name: "Ada",
      name: "Triage",
      description: "Routes alerts",
      status: "draft",
      version_number: 1,
      node_count: 3,
      tags: [],
      is_company_managed: true,
      shared_group_ids: ["g1"],
      paused_by_owner_deactivation: true,
      created_at: "2026-11-03T00:00:00Z",
      updated_at: "2026-11-03T00:00:00Z",
    };
    renderWithProviders(
      <MemoryRouter>
        <FlowCard flow={flow} userId="me" />
      </MemoryRouter>,
    );

    const link = screen.getByRole("link");
    expect(link.textContent).toContain("Triage");
    expect(link.getAttribute("href")).toBe("/flows/library/f1");

    const marks = screen.getAllByRole("img").filter((el) => el.getAttribute("tabindex") === "0");
    expect(marks.length).toBeGreaterThanOrEqual(3);
    for (const mark of marks) {
      expect(mark.closest("a")).toBeNull();
    }
  });

  it("IconMark draws the shared focus ring token in its source", () => {
    const source = readFileSync(resolve(process.cwd(), "src/components/ui/icon-mark.tsx"), "utf8");
    expect(source).toContain("focus-visible:ring-focus");
  });
});
