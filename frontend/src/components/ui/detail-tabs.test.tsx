// The detail tab bar moves the tabs that do not fit into a "More tabs" menu:
// every tab stays reachable without the scroll chevron, in every
// catalog, and the active tab is always in the bar.
import { afterEach, describe, expect, it, vi } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes, useLocation, useParams } from "react-router-dom";
import { Activity, Boxes, Clock, Folder, ListChecks, Workflow } from "lucide-react";
import { DetailTabs } from "@/components/ui/detail-tabs";
import { renderWithProviders } from "@/testing/providers";

// The tabs register palette commands; the palette is not under test here.
vi.mock("@/lib/commands/CommandsProvider", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/commands/CommandsProvider")>()),
  useCommandSections: () => {},
}));

const TABS = [
  { key: "executions", label: "Executions", icon: Activity },
  { key: "queue", label: "Queue", icon: ListChecks },
  { key: "schedules", label: "Schedules", icon: Clock },
  { key: "flows", label: "Flows", icon: Workflow },
  { key: "fleets", label: "Fleets", icon: Boxes },
  { key: "workspaces", label: "Workspaces", icon: Folder },
] as const;
type Key = (typeof TABS)[number]["key"];

/** happy-dom lays nothing out: every trigger is 120 px wide and the bar `barWidth`. */
function stubLayout(barWidth: number) {
  vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function (this: HTMLElement) {
    const width = this.getAttribute("role") === "tab" ? 120 : 0;
    return { width, height: 32, top: 0, left: 0, right: width, bottom: 32, x: 0, y: 0, toJSON: () => ({}) } as DOMRect;
  });
  Object.defineProperty(HTMLElement.prototype, "clientWidth", { configurable: true, get: () => barWidth });
}

function Where() {
  return <span data-testid="where">{useLocation().pathname}</span>;
}

function Page() {
  const active = (useParams<{ tab?: string }>().tab ?? "executions") as Key;
  return (
    <>
      <DetailTabs tabs={TABS.map((tab) => ({ ...tab }))} active={active} />
      <Where />
    </>
  );
}

function renderBar(path: string, locale = "en") {
  return renderWithProviders(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path="/operations" element={<Page />} />
        <Route path="/operations/:tab" element={<Page />} />
      </Routes>
    </MemoryRouter>,
    { locale },
  );
}

describe("DetailTabs overflow", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    delete (HTMLElement.prototype as { clientWidth?: number }).clientWidth;
    delete (HTMLElement.prototype as { scrollWidth?: number }).scrollWidth;
  });

  it("shows every tab and no menu when the bar is wide enough", () => {
    stubLayout(2000);
    renderBar("/operations");
    expect(screen.getAllByRole("tab")).toHaveLength(6);
    expect(screen.queryByRole("button", { name: "More tabs" })).toBeNull();
  });

  it("moves the trailing tabs into the More tabs menu, and a row navigates", async () => {
    const user = userEvent.setup();
    // 540 px less the 40 px menu: four 120 px tabs (480 px) fit, five do not.
    stubLayout(540);
    renderBar("/operations");
    expect(screen.getAllByRole("tab").map((tab) => tab.textContent)).toEqual(["Executions", "Queue", "Schedules", "Flows"]);
    await user.click(screen.getByRole("button", { name: "More tabs" }));
    expect(screen.getAllByRole("menuitem").map((item) => item.textContent)).toEqual(["Fleets", "Workspaces"]);
    await user.click(screen.getByRole("menuitem", { name: "Workspaces" }));
    expect(screen.getByTestId("where")).toHaveTextContent("/operations/workspaces");
  });

  it("keeps the active tab in the bar when it is one of the trailing tabs", () => {
    stubLayout(540);
    renderBar("/operations/workspaces");
    const tabs = screen.getAllByRole("tab");
    expect(tabs.map((tab) => tab.textContent)).toEqual(["Executions", "Queue", "Schedules", "Workspaces"]);
    expect(screen.getByRole("tab", { name: "Workspaces" })).toHaveAttribute("data-state", "active");
  });

  it("drops one more tab when the committed list still scrolls, so no chevron sits beside the menu", () => {
    // The library's scroll chevron (32 px) stays after the measuring pass and
    // narrows the list: fitTabs keeps four 120 px tabs for 540 - 40 = 500 px,
    // but the list only gets 540 - 40 - 32 = 468 px, so 480 px still scrolls
    // (es agent bar, 1149 in 1124 px).
    stubLayout(540);
    Object.defineProperty(HTMLElement.prototype, "clientWidth", {
      configurable: true,
      get(this: HTMLElement) { return this.getAttribute("role") === "tablist" ? 468 : 540; },
    });
    Object.defineProperty(HTMLElement.prototype, "scrollWidth", {
      configurable: true,
      get(this: HTMLElement) {
        return this.getAttribute("role") === "tablist" ? this.querySelectorAll('[role="tab"]').length * 120 : 0;
      },
    });
    renderBar("/operations", "es");
    expect(screen.getAllByRole("tab")).toHaveLength(3);
    expect(screen.getByRole("button", { name: "Más pestañas" })).toBeInTheDocument();
  });

  it("keeps the largest set that fits once no chevron narrows the list, with no empty gap", () => {
    // A chevron left from the measuring pass narrowed
    // the list by 32 px, one more tab went to the menu, and once the chevron
    // was gone the room came back unused. The layout below plays the browser:
    // the measuring pass lays the list out at its natural width (no overflow),
    // otherwise the list gets 500 px (540 less the 40 px menu), 32 px less
    // while the library shows its chevron. Four 120 px tabs (480 px) fit.
    stubLayout(540);
    Object.defineProperty(HTMLElement.prototype, "clientWidth", {
      configurable: true,
      get(this: HTMLElement) {
        if (this.getAttribute("role") !== "tablist") return 540;
        if (this.closest("[data-measuring]")) return this.scrollWidth;
        return document.querySelector('[aria-label="Scroll tabs right"]') ? 468 : 500;
      },
    });
    Object.defineProperty(HTMLElement.prototype, "scrollWidth", {
      configurable: true,
      get(this: HTMLElement) {
        return this.getAttribute("role") === "tablist" ? this.querySelectorAll('[role="tab"]').length * 120 : 0;
      },
    });
    renderBar("/operations", "es");
    expect(screen.getAllByRole("tab")).toHaveLength(4);
    expect(screen.queryByRole("button", { name: /Scroll tabs/ })).toBeNull();
  });

  it("settles when the measuring pass itself narrows the bar (a page scrollbar it causes)", () => {
    // With always-visible scrollbars, the natural-width list of the measuring
    // pass gave the page a scrollbar for that pass only: the bar measured
    // 534 px while measuring and 540 px once committed. Keyed on the width
    // read during the pass, every commit started a new pass until React threw
    // "Maximum update depth exceeded" (agent Versioning tab at 1440 px).
    stubLayout(540);
    Object.defineProperty(HTMLElement.prototype, "clientWidth", {
      configurable: true,
      get(this: HTMLElement) {
        if (this.getAttribute("role") === "tablist") return this.scrollWidth;
        return this.closest("[data-measuring]") ? 534 : 540;
      },
    });
    Object.defineProperty(HTMLElement.prototype, "scrollWidth", {
      configurable: true,
      get(this: HTMLElement) {
        return this.getAttribute("role") === "tablist" ? this.querySelectorAll('[role="tab"]').length * 120 : 0;
      },
    });
    const consoleError = vi.spyOn(console, "error").mockImplementation(() => {});
    renderBar("/operations/schedules");
    // 534 - 40 for the menu: four 120 px tabs fit.
    expect(screen.getAllByRole("tab").map((tab) => tab.textContent)).toEqual(["Executions", "Queue", "Schedules", "Flows"]);
    expect(screen.getByRole("tab", { name: "Schedules" })).toHaveAttribute("data-state", "active");
    expect(screen.getByRole("button", { name: "More tabs" })).toBeInTheDocument();
    expect(consoleError).not.toHaveBeenCalled();
  });

  it("moves focus to the picked tab after a keyboard pick in the menu", async () => {
    const user = userEvent.setup();
    stubLayout(540);
    renderBar("/operations");
    screen.getByRole("button", { name: "More tabs" }).focus();
    await user.keyboard("{Enter}");
    await waitFor(() => expect(screen.getAllByRole("menuitem")).toHaveLength(2));
    await user.keyboard("{ArrowDown}");
    await waitFor(() => expect(screen.getByRole("menuitem", { name: "Workspaces" })).toHaveFocus());
    await user.keyboard("{Enter}");
    expect(screen.getByTestId("where")).toHaveTextContent("/operations/workspaces");
    await waitFor(() => expect(screen.getByRole("tab", { name: "Workspaces" })).toHaveFocus());
  });

  it("names the menu in the catalog of the page", () => {
    stubLayout(540);
    renderBar("/operations", "es");
    expect(screen.getByRole("button", { name: "Más pestañas" })).toBeInTheDocument();
  });
});

// A nav family (#9301 N3, UX r3 A1): the tabs are different routes. Without
// `to`, a tab "outcomes" on /traces would link to /traces/outcomes, which is
// a trace detail page.
describe("DetailTabs across routes", () => {
  afterEach(() => vi.restoreAllMocks());

  const FAMILY = [
    { key: "/observability", label: "Model usage", icon: Activity, to: "/observability" },
    { key: "/observability/outcomes", label: "Outcomes", icon: ListChecks, to: "/observability/outcomes" },
    { key: "/observability/traces", label: "Traces", icon: Clock, to: "/observability/traces" },
  ];

  it("links each tab to its own route and marks the page's tab active", () => {
    stubLayout(2000);
    renderWithProviders(
      <MemoryRouter initialEntries={["/observability/traces"]}>
        <Routes>
          <Route path="/observability/traces" element={<DetailTabs tabs={FAMILY} active="/observability/traces" />} />
        </Routes>
      </MemoryRouter>,
    );
    expect(screen.getByRole("tab", { name: "Outcomes" })).toHaveAttribute("href", "/observability/outcomes");
    expect(screen.getByRole("tab", { name: "Model usage" })).toHaveAttribute("href", "/observability");
    expect(screen.getByRole("tab", { name: "Traces" })).toHaveAttribute("data-state", "active");
  });
});
