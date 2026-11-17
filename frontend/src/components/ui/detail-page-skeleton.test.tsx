// An entity detail page's skeleton is the loaded page with its content
// greyed out (#11345): the same header box, icon tile, title line and tab
// triggers, so nothing moves when the entity lands, and no page that renders
// the shared entity header goes back to the generic PageSkeleton.
import { readFileSync, readdirSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it, vi } from "vitest";
import { screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { Activity, BarChart3, Bot, Settings2 } from "lucide-react";
import { DetailPageSkeleton } from "@/components/ui/detail-page-skeleton";
import { DetailTabs } from "@/components/ui/detail-tabs";
import { EntityPageHeader } from "@/components/ui/entity-page-header";
import { renderWithProviders } from "@/testing/providers";

vi.mock("@/lib/commands/CommandsProvider", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/commands/CommandsProvider")>()),
  useCommandSections: () => {},
}));

const TABS = [
  { key: "overview", label: "Overview", icon: BarChart3 },
  { key: "config", label: "Configuration", icon: Settings2 },
  { key: "activity", label: "Activity", icon: Activity },
] as const;

describe("DetailPageSkeleton", () => {
  it("draws the loaded page's slots: back link, tile, title, description, meta, actions, one trigger per tab", () => {
    const { container: loading } = renderWithProviders(
      <MemoryRouter>
        <DetailPageSkeleton backTo="/agents" backLabel="Back to Agents" tabs={3} descriptionLines={1} actions={2} />
      </MemoryRouter>,
    );
    const { container: loaded } = renderWithProviders(
      <MemoryRouter initialEntries={["/agents/1"]}>
        <div className="space-y-6">
          <EntityPageHeader backTo="/agents" backLabel="Back to Agents" icon={Bot} title="An agent" description="What it does" badges={<span>Active</span>} />
          <DetailTabs tabs={[...TABS]} active="overview" />
        </div>
      </MemoryRouter>,
    );

    // Each slot of the loaded header has its greyed counterpart.
    expect(loaded.querySelector('[data-testid="entity-page-header-tile"]')).not.toBeNull();
    expect(loading.querySelector('[data-testid="entity-page-header-skeleton"] [data-slot="tile"]')).not.toBeNull();
    expect(loaded.querySelector("h1")).not.toBeNull();
    expect(loading.querySelector('[data-slot="title"]')).not.toBeNull();
    expect(loaded.querySelector('[data-testid="entity-page-header-description"]')).not.toBeNull();
    expect(loading.querySelectorAll('[data-slot="description"]')).toHaveLength(1);
    expect(loaded.querySelector('[data-testid="entity-page-header-meta"]')).not.toBeNull();
    expect(loading.querySelector('[data-slot="meta"]')).not.toBeNull();
    expect(loading.querySelectorAll('[data-slot="actions"] > *')).toHaveLength(2);

    // The back link is the real one, with the loaded label.
    expect(screen.getAllByRole("button", { name: /Back to Agents/ })).toHaveLength(2);

    // The tab bar is the library's own list: as many triggers as the loaded bar, none reachable.
    const triggers = Array.from(loading.querySelectorAll('[data-testid="detail-tabs-skeleton"] [role="tab"]'));
    expect(triggers).toHaveLength(loaded.querySelectorAll('[role="tab"]').length);
    for (const trigger of triggers) expect(trigger).toHaveAttribute("tabindex", "-1");
    expect(loading.querySelector('[data-testid="detail-tabs-skeleton"]')).toHaveAttribute("aria-hidden", "true");

    expect(screen.getByRole("status", { name: "Loading..." })).toHaveAttribute("aria-busy", "true");
  });

  it("draws no tab bar for a page without tabs", () => {
    const { container } = renderWithProviders(
      <MemoryRouter>
        <DetailPageSkeleton backTo="/desktop" backLabel="Back to XTM One Desktop" tabs={0} />
      </MemoryRouter>,
    );
    expect(container.querySelector('[data-testid="detail-tabs-skeleton"]')).toBeNull();
  });

  it("keeps every entity detail page off the generic PageSkeleton", () => {
    // vitest runs from frontend/, as entity-table.test.tsx.
    const pages = join(process.cwd(), "src", "pages");
    const offenders = readdirSync(pages)
      .filter((name) => name.endsWith("Detail.tsx"))
      .filter((name) => {
        const source = readFileSync(join(pages, name), "utf8");
        return source.includes("<EntityPageHeader") && source.includes("<DetailTabs") && source.includes("<PageSkeleton");
      })
      .map((name) => name);
    // Every detail page with the shared header and tab bar loads on DetailPageSkeleton.
    expect(offenders).toEqual([]);
  });
});
