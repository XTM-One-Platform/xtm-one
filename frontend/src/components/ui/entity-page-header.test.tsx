// The shared header of the workspace and fleet pages, and the chip linking
// them (rules R3 and R4 of the workspace-fleet UX audit).

import { readdirSync, readFileSync, statSync } from "node:fs";
import { join, resolve } from "node:path";

import { useState } from "react";
import { describe, expect, it } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { Boxes } from "lucide-react";

import { Dialog, DialogContent, DialogDescription, DialogTitle } from "@/components/ui/dialog";
import { EntityHeaderMoreActions, EntityHeaderWideOnly, EntityPageHeader } from "@/components/ui/entity-page-header";
import { FleetWorkspaceChip } from "@/components/workspaces/FleetWorkspaceChip";
import { fleetWorkspaceChipLink } from "@/lib/fleet-workspace-chip";
import { renderWithProviders } from "@/testing/providers";

const PAGES_ON_THE_SHARED_HEADER = [
  "src/pages/WorkspaceProject.tsx",
  "src/pages/flows/FleetPage.tsx",
  "src/pages/AgentDetail.tsx",
  "src/pages/GroupDetail.tsx",
  "src/pages/KnowledgeBaseDetail.tsx",
  // Every other entity detail page.
  "src/pages/IntegrationDetail.tsx",
  "src/pages/MCPServerDetail.tsx",
  "src/pages/CustomToolDetail.tsx",
  "src/pages/custom-tool-detail/BuiltinToolDetail.tsx",
  "src/pages/SkillDetail.tsx",
  "src/pages/PromptDetail.tsx",
  "src/pages/TemplateDetail.tsx",
  "src/pages/UserDetail.tsx",
  "src/pages/RunnerDetail.tsx",
  "src/pages/RelayGroupDetail.tsx",
  "src/pages/EvaluationDetail.tsx",
  "src/pages/DatasetDetail.tsx",
  "src/pages/EvaluationRunDetail.tsx",
  "src/pages/EvaluationRunCompare.tsx",
  "src/pages/TraceDetail.tsx",
  "src/pages/AssignmentDetail.tsx",
  "src/pages/InvestigationPackDetail.tsx",
  // The CTEM pages draw the CTEM header band instead (CtemPageHeader, #11660).
  "src/pages/profile/agent-accounts/AgentAccountPage.tsx",
  "src/pages/settings/TrackedOrganizationsAiDraftReview.tsx",
];

function source(path: string): string {
  return readFileSync(resolve(process.cwd(), path), "utf8");
}

function sourcesUnder(dir: string): string[] {
  const root = resolve(process.cwd(), dir);
  const out: string[] = [];
  const walk = (at: string) => {
    for (const name of readdirSync(at)) {
      const full = join(at, name);
      if (statSync(full).isDirectory()) walk(full);
      else if (/\.tsx?$/.test(name) && !/\.test\.tsx?$/.test(name)) out.push(full);
    }
  };
  walk(root);
  return out;
}

// A back label written as a literal: `backLabel="..."`, `backLabel: "..."`,
// `backLabel ?? "..."`, `backLabel={t_i18n("...")}`, `<BackButton ... label="..."`.
const BACK_LABEL_LITERALS = [
  /backLabel\s*=\s*"([^"]*)"/g,
  /backLabel\s*=\s*\{t_i18n\("([^"]*)"\)\}/g,
  /backLabel\s*:\s*"([^"]*)"/g,
  /backLabel\s*\?\?\s*"([^"]*)"/g,
  /<BackButton\b[^>]*?\slabel="([^"]*)"/g,
];

/** Every word of the parent's name starts with a capital letter. */
function isTitleCaseParent(parent: string): boolean {
  return parent.split(" ").every((word) => /^[A-Z0-9]/.test(word));
}

function backLabelsIn(text: string): string[] {
  return BACK_LABEL_LITERALS.flatMap((pattern) => [...text.matchAll(pattern)].map((m) => m[1]));
}

describe("EntityPageHeader gate", () => {
  it.each(PAGES_ON_THE_SHARED_HEADER)("%s renders the shared header and no title block of its own", (path) => {
    const text = source(path);
    expect(text).toContain("<EntityPageHeader");
    // Nothing drawn beside the header in a row pushes the back link off the
    // content edge (the CTEM gauge): a picture goes in `leading`.
    expect(text).not.toMatch(/<div className="flex\b[^"]*">\s*<[A-Z]\w*[^>]*\/>\s*<EntityPageHeader/);
    // Its own h1 next to a BackButton is the drift the shared header removed.
    expect(text).not.toMatch(/<h1\b/);
  });

  // One label form on every page: "Back to <Parent>", never the bare
  // parent name the fleet and workspace pages used. A plain "Back" stays for a
  // history step (`to={-1}`), whose parent is not known.
  it("every back label of the pages and components reads Back to <Parent>", () => {
    const offenders = [...sourcesUnder("src/pages"), ...sourcesUnder("src/components")].flatMap((file) =>
      backLabelsIn(readFileSync(file, "utf8"))
        .filter((label) => label !== "Back" && !label.startsWith("Back to "))
        .map((label) => `${file}: "${label}"`),
    );
    expect(offenders).toEqual([]);
  });

  // One case on every page: "Back to <Parent>" names the parent page
  // as the navigation does, in title case ("Back to Command Center", never
  // "Back to command center"). "Back to the <thing>" names one entity, not a
  // page, and stays in sentence case.
  it("every Back to <Parent> label is in title case", () => {
    const offenders = [...sourcesUnder("src/pages"), ...sourcesUnder("src/components")].flatMap((file) =>
      backLabelsIn(readFileSync(file, "utf8"))
        .filter((label) => label.startsWith("Back to ") && !label.startsWith("Back to the "))
        .filter((label) => !isTitleCaseParent(label.slice("Back to ".length)))
        .map((label) => `${file}: "${label}"`),
    );
    expect(offenders).toEqual([]);
  });

  it("reads every literal form of a back label", () => {
    expect(
      backLabelsIn(
        'backLabel="Fleets" backLabel: "A" backLabel ?? "B" backLabel={t_i18n("D")} <BackButton to="/x" label="C" className="mb-4" />',
      ),
    ).toEqual(["Fleets", "D", "A", "B", "C"]);
  });

  it("tells a title-case parent from a lower-case one", () => {
    expect(isTitleCaseParent("Command Center")).toBe(true);
    expect(isTitleCaseParent("XTM One Desktop")).toBe(true);
    expect(isTitleCaseParent("command center")).toBe(false);
    expect(isTitleCaseParent("Investigation packs")).toBe(false);
  });
});

describe("EntityPageHeader", () => {
  it("draws the back link, the h1, the hint and the links row", () => {
    renderWithProviders(
      <MemoryRouter>
        <EntityPageHeader
          backTo="/fleets"
          backLabel="Back to Fleets"
          icon={Boxes}
          title="CEO Office"
          hint="A fleet is a team of agents."
          hintLabel="What a fleet is"
          description="Runs the office"
          links={<span>chip</span>}
          actions={<button type="button">Settings</button>}
        />
      </MemoryRouter>,
    );
    expect(screen.getByRole("heading", { level: 1, name: "CEO Office" })).toBeTruthy();
    expect(screen.getByRole("img", { name: "What a fleet is" })).toBeTruthy();
    expect(screen.getByText("Runs the office")).toBeTruthy();
    expect(screen.getByText("chip")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Settings" })).toBeTruthy();
  });

  // A page with its own picture (a provider logo, a status tone) or
  // none at all keeps it inside the shared header.
  it("draws a node icon as given in the tile, with the page's tone, and no tile without an icon", () => {
    const { unmount } = renderWithProviders(
      <MemoryRouter>
        <EntityPageHeader
          backTo="/integrations"
          backLabel="Back to Integrations"
          icon={<img alt="logo" src="data:," />}
          tileClassName="bg-feedback-success-primary/10"
          title="GitHub"
        />
      </MemoryRouter>,
    );
    const tile = screen.getByTestId("entity-page-header-tile");
    expect(tile.contains(screen.getByAltText("logo"))).toBe(true);
    expect(tile.getAttribute("data-tone")).toBe("page");
    unmount();

    renderWithProviders(
      <MemoryRouter>
        <EntityPageHeader backTo="/observability/traces" backLabel="Back to Traces" title="chat" />
      </MemoryRouter>,
    );
    expect(screen.queryByTestId("entity-page-header-tile")).toBeNull();
    expect(screen.getByRole("heading", { level: 1, name: "chat" })).toBeTruthy();
  });

  // A long assignment name truncated in fr at 1024. A page that asks
  // for it gets a title that wraps, and the full name stays on hover either way.
  it("wraps the title when the page asks for it, the full name on hover in both forms", () => {
    const name = "Surveillance quotidienne des vulnérabilités critiques exposées sur Internet";
    const { unmount } = renderWithProviders(
      <MemoryRouter>
        <EntityPageHeader backTo="/agents/a" backLabel="Back to Agent" title={name} wrapTitle />
      </MemoryRouter>,
    );
    const wrapped = screen.getByRole("heading", { level: 1, name });
    expect(wrapped.getAttribute("data-wrap")).toBe("true");
    expect(wrapped.getAttribute("title")).toBe(name);
    unmount();

    renderWithProviders(
      <MemoryRouter>
        <EntityPageHeader backTo="/agents/a" backLabel="Back to Agent" title={name} />
      </MemoryRouter>,
    );
    const oneLine = screen.getByRole("heading", { level: 1, name });
    expect(oneLine.getAttribute("data-wrap")).toBeNull();
    expect(oneLine.getAttribute("title")).toBe(name);
  });

  // A picture of the page's own (the CTEM posture gauge) sits in
  // the header row in the tile's place, so the back link stays the header's
  // first child, at the content edge, on every page.
  it("keeps the back link first and draws a leading picture in the row, without a tile", () => {
    renderWithProviders(
      <MemoryRouter>
        <EntityPageHeader
          backTo="/"
          backLabel="Back to Command Center"
          leading={<img alt="posture gauge" src="data:," />}
          title="Prioritization"
        />
      </MemoryRouter>,
    );
    const header = screen.getByTestId("entity-page-header");
    const back = screen.getByRole("button", { name: /Back to Command Center/ });
    expect(header.firstElementChild).toBe(back);
    const leading = screen.getByTestId("entity-page-header-leading");
    expect(screen.getByTestId("entity-page-header-text").contains(leading)).toBe(true);
    expect(leading.contains(screen.getByAltText("posture gauge"))).toBe(true);
    expect(screen.queryByTestId("entity-page-header-tile")).toBeNull();
  });

  // A long description pushed the actions under the title block. They
  // sit in the header row, after the text column, in a row that never wraps.
  it("keeps the actions in the header row beside the description, never under it", () => {
    const long = "A fleet description long enough to wrap over several lines. ".repeat(12);
    renderWithProviders(
      <MemoryRouter>
        <EntityPageHeader
          backTo="/fleets"
          backLabel="Back to Fleets"
          icon={Boxes}
          title="CEO Office"
          description={long}
          actions={<button type="button">Settings</button>}
        />
      </MemoryRouter>,
    );
    const row = screen.getByTestId("entity-page-header-row");
    const text = screen.getByTestId("entity-page-header-text");
    const actions = screen.getByTestId("entity-page-header-actions");
    expect([...row.children]).toEqual([text, actions]);
    expect(text.contains(screen.getByText(long.trim()))).toBe(true);
    expect(actions.contains(screen.getByRole("button", { name: "Settings" }))).toBe(true);
  });

  it("puts the actions on their own row under the text with stackActions, the description still clamped", () => {
    const long = "A fleet description long enough to wrap over several lines. ".repeat(12);
    renderWithProviders(
      <MemoryRouter>
        <EntityPageHeader
          backTo="/fleets"
          backLabel="Back to Fleets"
          icon={Boxes}
          title="CEO Office"
          description={long}
          stackActions
          actions={<button type="button">Settings</button>}
        />
      </MemoryRouter>,
    );
    const row = screen.getByTestId("entity-page-header-row");
    const text = screen.getByTestId("entity-page-header-text");
    const actions = screen.getByTestId("entity-page-header-actions");
    // The text first, the actions after it on their own row: the title gets the full width.
    expect([...row.children]).toEqual([text, actions]);
    expect(row).toHaveAttribute("data-actions-placement", "stacked");
    expect(screen.getByText(long.trim())).toHaveAttribute("data-clamped", "true");
  });

  it("keeps the actions in the title row by default", () => {
    renderWithProviders(
      <MemoryRouter>
        <EntityPageHeader backTo="/fleets" backLabel="Back to Fleets" title="CEO Office" description="Short" actions={<span />} />
      </MemoryRouter>,
    );
    expect(screen.getByTestId("entity-page-header-row")).toHaveAttribute("data-actions-placement", "row");
  });

  it("keeps the actions on the title's line with actionsBesideTitle, the description and meta under both (UX r4 B1 of #10181)", () => {
    const long = "A fleet description long enough to wrap over several lines. ".repeat(12);
    renderWithProviders(
      <MemoryRouter>
        <EntityPageHeader
          backTo="/fleets"
          backLabel="Back to Fleets"
          icon={Boxes}
          title="A fork of the CEO Office fleet with a long name"
          description={long}
          badges={<span>Orchestrator</span>}
          actionsBesideTitle
          actions={<button type="button">Settings</button>}
        />
      </MemoryRouter>,
    );
    const row = screen.getByTestId("entity-page-header-row");
    expect(row).toHaveAttribute("data-actions-placement", "title");
    // One grid: the text wrapper is `contents`, so the tile, title line, description and meta row are its cells.
    const cells = [...row.querySelectorAll(":scope > *")].map((el) => el.getAttribute("data-testid"));
    expect(cells).toEqual(["entity-page-header-text", "entity-page-header-actions"]);
    expect(screen.getByText(long.trim())).toHaveAttribute("data-clamped", "true");
    expect(screen.getByRole("heading", { level: 1 })).not.toHaveAttribute("data-wrap");
  });

  it("lets stackActions win over actionsBesideTitle", () => {
    renderWithProviders(
      <MemoryRouter>
        <EntityPageHeader backTo="/fleets" backLabel="Back to Fleets" title="CEO Office" stackActions actionsBesideTitle actions={<span />} />
      </MemoryRouter>,
    );
    expect(screen.getByTestId("entity-page-header-row")).toHaveAttribute("data-actions-placement", "stacked");
  });

  // #10215: a long description is clamped to two lines; the toggle
  // shows only when the clamp cuts the text and opens the full text in place.
  it("clamps the description and opens it with a keyboard-reachable toggle only when cut", async () => {
    const user = userEvent.setup();
    const long = "An AI-written fleet description long enough to need more than two lines. ".repeat(10);
    const heights = Object.getOwnPropertyDescriptors(HTMLElement.prototype);
    Object.defineProperty(HTMLElement.prototype, "scrollHeight", { configurable: true, get: () => 120 });
    Object.defineProperty(HTMLElement.prototype, "clientHeight", { configurable: true, get: () => 40 });
    try {
      renderWithProviders(
        <MemoryRouter>
          <EntityPageHeader backTo="/fleets" backLabel="Back to Fleets" icon={Boxes} title="CEO Office" description={long} />
        </MemoryRouter>,
      );
      const paragraph = screen.getByText(long.trim());
      expect(paragraph.getAttribute("data-clamped")).toBe("true");
      const more = screen.getByRole("button", { name: "Read more" });
      expect(more.getAttribute("aria-expanded")).toBe("false");
      // UX round 1 B1: the toggle covers the end of the second line, never a row of its own.
      expect(more.getAttribute("data-placement")).toBe("line-end");
      expect(more.parentElement).toBe(screen.getByTestId("entity-page-header-description"));
      expect(more.getAttribute("aria-controls")).toBe(paragraph.id);
      // UX round 2 B1: the visible box is one line tall (line two only), never over line one,
      // and a fade leads into it so the cut word is not shaved.
      expect(more.getAttribute("data-box")).toBe("one-line");
      expect(more.querySelector('[data-testid="entity-page-header-description-fade"]')).not.toBeNull();
      more.focus();
      await user.keyboard("{Enter}");
      expect(paragraph.getAttribute("data-clamped")).toBe("false");
      const less = screen.getByRole("button", { name: "Show less" });
      expect(less.getAttribute("aria-expanded")).toBe("true");
      // Expanded, it follows the last word inline (the paragraph turns inline).
      expect(less.getAttribute("data-placement")).toBe("inline");
      // UX round 2 B2: one button instance in both states, so the focus stays on it.
      expect(less).toBe(more);
      expect(document.activeElement).toBe(less);
      await user.keyboard("{Enter}");
      expect(paragraph.getAttribute("data-clamped")).toBe("true");
      expect(document.activeElement).toBe(screen.getByRole("button", { name: "Read more" }));
      await user.click(screen.getByRole("button", { name: "Read more" }));
      await user.click(screen.getByRole("button", { name: "Show less" }));
      expect(paragraph.getAttribute("data-clamped")).toBe("true");
    } finally {
      for (const key of ["scrollHeight", "clientHeight"] as const) {
        const original = heights[key];
        if (original) Object.defineProperty(HTMLElement.prototype, key, original);
        else delete (HTMLElement.prototype as unknown as Record<string, unknown>)[key];
      }
    }
  });

  it("draws no toggle for a description that fits", () => {
    renderWithProviders(
      <MemoryRouter>
        <EntityPageHeader backTo="/fleets" backLabel="Back to Fleets" title="CEO Office" description="Runs the office" />
      </MemoryRouter>,
    );
    expect(screen.getByText("Runs the office").getAttribute("data-clamped")).toBe("true");
    expect(screen.queryByRole("button", { name: "Read more" })).toBeNull();
  });

  // #10215: badges, facts and cross links share one meta row instead of
  // a row each, in that order.
  it("puts badges, meta and links on one meta row", () => {
    renderWithProviders(
      <MemoryRouter>
        <EntityPageHeader
          backTo="/fleets"
          backLabel="Back to Fleets"
          title="CEO Office"
          badges={<span>On</span>}
          meta={<span>Owner Sam</span>}
          links={<span>Works in workspace</span>}
        />
      </MemoryRouter>,
    );
    const rows = screen.getAllByTestId("entity-page-header-meta");
    expect(rows).toHaveLength(1);
    const slots = [...rows[0].children].map((el) => el.getAttribute("data-slot"));
    expect(slots).toEqual(["badges", "meta", "links"]);
    for (const text of ["On", "Owner Sam", "Works in workspace"]) expect(rows[0].contains(screen.getByText(text))).toBe(true);
    // Wraps by default: the one-line form is the page's choice.
    expect(rows[0].getAttribute("data-one-line")).toBeNull();
  });

  // UX round 2 B1 on #10287: the fleet page's meta row stays one line at
  // 1024 by construction, its facts truncating instead of wrapping.
  it("keeps the meta row on one line when metaOneLine is set", () => {
    renderWithProviders(
      <MemoryRouter>
        <EntityPageHeader
          backTo="/fleets"
          backLabel="Back to Fleets"
          title="CEO Office"
          metaOneLine
          badges={<span>On</span>}
          links={<span>Works in workspace</span>}
        />
      </MemoryRouter>,
    );
    const row = screen.getByTestId("entity-page-header-meta");
    expect(row.getAttribute("data-one-line")).toBe("true");
  });

  // UX R2 A4 on #10287: the agent page meta row sat on two lines (the slug on
  // its own line, 46 px). It is one line now, the owner and slug truncating
  // with their full text in a keyboard-reachable Tip. jsdom does not lay out,
  // so the guard reads the header block of the page source.
  it("keeps the agent page meta row on one line with the full owner and slug in a Tip", () => {
    const page = source("src/pages/AgentDetail.tsx");
    const header = page.slice(page.indexOf("<EntityPageHeader"), page.indexOf("actions={", page.indexOf("<EntityPageHeader")));
    expect(header).toMatch(/\n\s+metaOneLine\n/);
    for (const id of ["agent-header-owner", "agent-header-slug"]) {
      const fact = header.slice(header.lastIndexOf("<Tip content=", header.indexOf(id)), header.indexOf(id));
      expect(fact, id).toMatch(/t_i18n\("(Owner|Agent slug): \{/);
      expect(fact, id).toContain("tabIndex={0}");
      expect(fact, id).toContain("min-w-0");
    }
    expect(header.match(/className="truncate"/g)).toHaveLength(2);
    // UX R1 B1 on #10553: a short fact never shrinks below its first word, the
    // longest fact takes the shrink first. The owner keeps its width (capped),
    // the slug shrinks.
    const owner = header.slice(header.lastIndexOf("<span", header.indexOf("agent-header-owner")), header.indexOf("agent-header-owner"));
    expect(owner).toContain("shrink-0");
    expect(owner).toContain("max-w-[12rem]");
    const slug = header.slice(header.lastIndexOf("<span", header.indexOf("agent-header-slug")), header.indexOf("agent-header-slug"));
    expect(slug).toContain("shrink-[4]");
    expect(slug).not.toContain("shrink-0");
  });
});

// Below 1280 px the secondary actions are rows of a "More actions"
// menu; the buttons and the menu are both in the row, switched by the
// breakpoint, and every row runs its action.
describe("EntityHeaderMoreActions", () => {
  it("puts the menu and the wide-only buttons in the actions slot and runs a row from the menu", async () => {
    const user = userEvent.setup();
    const ran: string[] = [];
    renderWithProviders(
      <MemoryRouter>
        <EntityPageHeader
          backTo="/agents"
          backLabel="Back to Agents"
          icon={Boxes}
          title="Analyst"
          actions={
            <>
              <button type="button">Chat with Agent</button>
              <EntityHeaderWideOnly>
                <button type="button">Duplicate</button>
              </EntityHeaderWideOnly>
              <EntityHeaderMoreActions
                items={[
                  { id: "duplicate", label: "Duplicate", onSelect: () => ran.push("duplicate") },
                  { id: "delete", label: "Delete agent", destructive: true, onSelect: () => ran.push("delete") },
                ]}
              />
            </>
          }
        />
      </MemoryRouter>,
    );
    const actions = screen.getByTestId("entity-page-header-actions");
    expect(actions.contains(screen.getByTestId("entity-page-header-wide-only"))).toBe(true);
    const trigger = screen.getByRole("button", { name: "More actions" });
    expect(actions.contains(trigger)).toBe(true);
    await user.click(trigger);
    await user.click(await screen.findByRole("menuitem", { name: "Delete agent" }));
    expect(ran).toEqual(["delete"]);
  });

  // The consistency gate: every page with more than three header
  // actions folds its secondary ones the same way.
  it.each([
    "src/pages/AgentDetail.tsx",
    "src/pages/TemplateDetail.tsx",
    "src/pages/KnowledgeBaseDetail.tsx",
    "src/pages/AssignmentDetail.tsx",
    "src/pages/UserDetail.tsx",
    "src/pages/IntegrationDetail.tsx",
  ])("%s folds its secondary header actions below 1280 px", (path) => {
    const text = source(path);
    expect(text).toContain("<EntityHeaderWideOnly>");
    expect(text).toContain("<EntityHeaderMoreActions");
    // The More actions trigger is the last control
    // of the row, so a Read-only badge comes before it.
    const badge = text.lastIndexOf("<ReadOnlyBadge");
    if (badge !== -1) expect(badge).toBeLessThan(text.indexOf("<EntityHeaderMoreActions"));
  });

  // The destructive row names its object, as
  // "Delete agent" or "Delete template" do on the other pages.
  it("names the user in the User detail delete row", () => {
    const text = source("src/pages/UserDetail.tsx");
    expect(text).toContain('id: "delete", label: t_i18n("Delete user")');
    expect(text).not.toContain('id: "delete", label: t_i18n("Delete")');
  });

  // A dialog opened from a menu row returns focus to the
  // "More actions" trigger on close; the row is unmounted with its menu, so
  // focus fell to document.body.
  it.each(["keyboard", "pointer"])("returns focus to the More actions trigger when a dialog opened from a row closes (%s)", async (mode) => {
    const user = userEvent.setup();
    function Page() {
      const [open, setOpen] = useState(false);
      return (
        <>
          <EntityHeaderMoreActions items={[{ id: "delete", label: "Delete agent", destructive: true, onSelect: () => setOpen(true) }]} />
          <Dialog open={open} onOpenChange={setOpen}>
            <DialogContent>
              <DialogTitle>Delete agent</DialogTitle>
              <DialogDescription>Confirm</DialogDescription>
              <button type="button">Confirm</button>
            </DialogContent>
          </Dialog>
        </>
      );
    }
    renderWithProviders(
      <MemoryRouter>
        <Page />
      </MemoryRouter>,
    );
    const trigger = screen.getByRole("button", { name: "More actions" });
    if (mode === "keyboard") {
      trigger.focus();
      await user.keyboard("{Enter}");
      const row = await screen.findByRole("menuitem", { name: "Delete agent" });
      await waitFor(() => expect(document.activeElement).toBe(row));
      await user.keyboard("{Enter}");
    } else {
      await user.click(trigger);
      await user.click(await screen.findByRole("menuitem", { name: "Delete agent" }));
    }
    await screen.findByRole("dialog");
    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    await waitFor(() => expect(document.activeElement).toBe(trigger));
  });

  it("draws no menu without an item", () => {
    renderWithProviders(
      <MemoryRouter>
        <EntityHeaderMoreActions items={[]} />
      </MemoryRouter>,
    );
    expect(screen.queryByRole("button", { name: "More actions" })).toBeNull();
  });

  // A row with no button in the wide row stays reachable at 1280 px and above (#1508 UX round 1 B1).
  it("keeps the menu-only rows in a menu at 1280 px and above, and every row below", async () => {
    const user = userEvent.setup();
    const ran: string[] = [];
    renderWithProviders(
      <MemoryRouter>
        <EntityHeaderMoreActions
          items={[{ id: "settings", label: "Settings", onSelect: () => ran.push("settings") }]}
          menuOnly={[{ id: "reset", label: "Reset to factory state", onSelect: () => ran.push("reset") }]}
        />
      </MemoryRouter>,
    );
    const wide = screen.getByTestId("entity-page-header-more-actions-wide");
    expect(wide.getAttribute("data-widths")).toBe("xl");
    expect(screen.getByTestId("entity-page-header-more-actions").getAttribute("data-widths")).toBe("below-xl");
    const triggers = screen.getAllByRole("button", { name: "More actions" });
    expect(triggers).toHaveLength(2);
    const wideTrigger = triggers.find((t) => wide.contains(t))!;
    await user.click(wideTrigger);
    expect(screen.queryByRole("menuitem", { name: "Settings" })).toBeNull();
    await user.click(await screen.findByRole("menuitem", { name: "Reset to factory state" }));
    expect(ran).toEqual(["reset"]);
  });

  it("puts the fleet page's reset and setup rows in the menu kept at 1280 px and above", () => {
    const text = source("src/pages/flows/FleetPage.tsx");
    expect(text).toContain("menuOnly={menuOnlyActions}");
    const menuOnly = text.slice(text.indexOf("const menuOnlyActions"));
    expect(menuOnly.indexOf('id: "reset-factory"')).toBeGreaterThan(-1);
    expect(menuOnly.indexOf('id: "run-setup-again"')).toBeGreaterThan(-1);
  });

  it("stays at every width with always (a docked panel took the row's room)", () => {
    renderWithProviders(
      <MemoryRouter>
        <EntityHeaderMoreActions always items={[{ id: "settings", label: "Settings", onSelect: () => undefined }]} />
      </MemoryRouter>,
    );
    expect(screen.getByTestId("entity-page-header-more-actions").getAttribute("data-widths")).toBe("all");
  });
});

describe("FleetWorkspaceChip", () => {
  const fromFleet = { path: "/fleets/f-1", label: "Back to the fleet" };

  it("links a fleet to its workspace with the way back", () => {
    expect(fleetWorkspaceChipLink({ kind: "workspace", id: "w-1", name: "CEO Office" }, fromFleet)).toEqual({
      to: "/tasks/workspaces/w-1",
      state: { backTo: "/fleets/f-1", backLabel: "Back to the fleet" },
    });
    expect(
      fleetWorkspaceChipLink({ kind: "fleet", id: "f-1", name: "CEO Office" }, { path: "/tasks/workspaces/w-1", label: "x" }).to,
    ).toBe("/fleets/f-1");
  });

  it("reads the same both ways, the full name kept as the title", () => {
    renderWithProviders(
      <MemoryRouter>
        <FleetWorkspaceChip target={{ kind: "workspace", id: "w-1", name: "CEO Office" }} from={fromFleet} />
        <FleetWorkspaceChip
          target={{ kind: "fleet", id: "f-1", name: "CEO Office" }}
          from={{ path: "/tasks/workspaces/w-1", label: "Back to the workspace" }}
        />
      </MemoryRouter>,
    );
    // UX round 1 B1 (#10287): the prefix is the icon's tooltip and the link's
    // accessible name, never visible text, so the chip fits the one meta row.
    expect(screen.queryByText("Works in workspace")).toBeNull();
    expect(screen.queryByText("Hosts fleet")).toBeNull();
    expect(screen.getByRole("img", { name: "Works in workspace" })).toBeTruthy();
    expect(screen.getByRole("img", { name: "Hosts fleet" })).toBeTruthy();
    const links = screen.getAllByRole("link");
    expect(links.map((a) => a.getAttribute("href"))).toEqual(["/tasks/workspaces/w-1", "/fleets/f-1"]);
    expect(links.map((a) => a.getAttribute("aria-label"))).toEqual([
      "Works in workspace: CEO Office",
      "Hosts fleet: CEO Office",
    ]);
    for (const link of links) expect(link.querySelector("[title='CEO Office']")).toBeTruthy();
  });

  it("names an archived fleet in the link's accessible name from one catalog string", () => {
    // UX R3 A2 on #10287: the name is a catalog string, never a concatenation,
    // so each locale writes its own colon and its own word for archived.
    renderWithProviders(
      <MemoryRouter>
        <FleetWorkspaceChip
          target={{ kind: "fleet", id: "f-1", name: "CEO Office", archived: true }}
          from={{ path: "/tasks/workspaces/w-1", label: "Back to the workspace" }}
        />
      </MemoryRouter>,
    );
    expect(screen.getByRole("link").getAttribute("aria-label")).toBe("Hosts fleet: CEO Office (archived)");
  });

  it("draws the badge alone when the prefix is empty (a list of fleets)", () => {
    renderWithProviders(
      <MemoryRouter>
        <FleetWorkspaceChip
          target={{ kind: "fleet", id: "f-1", name: "CEO Office", archived: true }}
          from={{ path: "/tasks/workspaces/w-1", label: "Back to the workspace" }}
          prefix=""
        />
      </MemoryRouter>,
    );
    expect(screen.queryByTestId("fleet-workspace-chip-icon")).toBeNull();
    expect(screen.getByRole("link").getAttribute("aria-label")).toBeNull();
    expect(screen.getByText("(archived)")).toBeTruthy();
  });
});
