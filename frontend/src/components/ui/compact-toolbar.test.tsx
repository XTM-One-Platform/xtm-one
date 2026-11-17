// The compact Operations toolbar: icon-only visibility pills, a collapsed
// segmented filter, the shared Fleet + Agent picker group, and the media query.

import { afterEach, describe, expect, it, vi } from "vitest";
import { act, renderHook, screen } from "@testing-library/react";

import { renderWithProviders } from "@/testing/providers";
import { VisibilityFilter } from "./visibility-filter";
import { SegmentedFilter } from "./segmented-filter";
import { FleetAgentPickerGroup } from "@/components/fleets/FleetAgentPickerGroup";
import { COMPACT_TOOLBAR_QUERY, useCompactToolbar, useMediaQuery } from "@/hooks/useCompactToolbar";

vi.mock("@/hooks/api/useFleetApi", () => ({
  useAccessibleFleets: () => ({ fleets: [], access: "none" }),
}));

const STATUS = [
  { key: "", label: "All" },
  { key: "running", label: "Running" },
  { key: "failed", label: "Failed" },
];

describe("VisibilityFilter iconOnly", () => {
  it("keeps every segment's accessible name, tooltip and count", () => {
    renderWithProviders(
      <VisibilityFilter value="mine" onChange={() => {}} counts={{ mine: 2, all: 5 }} iconOnly />,
    );
    const radios = screen.getAllByRole("radio");
    expect(radios.map((radio) => radio.getAttribute("aria-checked"))).toEqual(["true", "false", "false", "false"]);
    for (const name of [/^Mine/, /^Company/, /^Shared/, /^All/]) {
      const radio = screen.getByRole("radio", { name });
      expect(radio.getAttribute("title")).toBeTruthy();
    }
    expect(screen.getByRole("radio", { name: /^Mine/ }).textContent).toContain("2");
    expect(screen.getByRole("radio", { name: /^All/ }).textContent).toContain("5");
  });
});

describe("SegmentedFilter collapsed", () => {
  it("draws one named picker in place of the pills", () => {
    renderWithProviders(
      <SegmentedFilter ariaLabel="Status filter" options={STATUS} value="running" onChange={() => {}} collapsed />,
    );
    expect(screen.queryByRole("group", { name: "Status filter" })).toBeNull();
    const picker = screen.getByRole("combobox", { name: "Status filter" });
    expect(picker.textContent).toContain("Running");
  });

  it("shows the option keyed \"\" as selected", () => {
    renderWithProviders(
      <SegmentedFilter ariaLabel="Status filter" options={STATUS} value="" onChange={() => {}} collapsed />,
    );
    expect(screen.getByRole("combobox", { name: "Status filter" }).textContent).toContain("All");
  });

  it("keeps the pills when not collapsed", () => {
    renderWithProviders(<SegmentedFilter ariaLabel="Status filter" options={STATUS} value="" onChange={() => {}} />);
    expect(screen.getByRole("group", { name: "Status filter" })).toBeInTheDocument();
    expect(screen.getAllByRole("button")).toHaveLength(3);
  });

  it("marks a dense row and keeps every label whole (UX r3 B2 of #10181)", () => {
    renderWithProviders(
      <SegmentedFilter ariaLabel="Status filter" options={STATUS} value="" onChange={() => {}} dense />,
    );
    const group = screen.getByRole("group", { name: "Status filter" });
    expect(group.getAttribute("data-dense")).toBe("true");
    expect(screen.getAllByRole("button").map((button) => button.textContent)).toEqual(["All", "Running", "Failed"]);
  });

  it("is not dense by default", () => {
    renderWithProviders(<SegmentedFilter ariaLabel="Status filter" options={STATUS} value="" onChange={() => {}} />);
    expect(screen.getByRole("group", { name: "Status filter" }).hasAttribute("data-dense")).toBe(false);
  });
});

describe("SegmentedFilter overflow menu (UX r4 B3 of #10181)", () => {
  const rect = Element.prototype.getBoundingClientRect;
  afterEach(() => {
    // The own property set by `layout` goes; jsdom's inherited getter answers again.
    delete (HTMLElement.prototype as { clientWidth?: number }).clientWidth;
    Element.prototype.getBoundingClientRect = rect;
  });

  // Every pill 100 px wide, the bar 290 px: the three (300 px) do not fit; two pills and the menu (40 px) do.
  const layout = (bar = 290) => {
    Object.defineProperty(HTMLElement.prototype, "clientWidth", { configurable: true, get: () => bar });
    Element.prototype.getBoundingClientRect = () => ({ width: 100 }) as DOMRect;
  };

  it("keeps one row and moves what does not fit into More tabs", async () => {
    layout();
    renderWithProviders(
      <SegmentedFilter ariaLabel="Fleet view" options={STATUS} value="" onChange={() => {}} overflow="menu" />,
    );
    const group = screen.getByRole("group", { name: "Fleet view" });
    expect(group.getAttribute("data-overflow")).toBe("menu");
    expect(await screen.findByRole("button", { name: "More tabs" })).toBeInTheDocument();
    expect([...group.querySelectorAll("[data-segment]")].map((button) => button.textContent)).toEqual(["All", "Running"]);
  });

  it("keeps the selected option in the row when it would fall in the menu", async () => {
    layout();
    renderWithProviders(
      <SegmentedFilter ariaLabel="Fleet view" options={STATUS} value="failed" onChange={() => {}} overflow="menu" />,
    );
    await screen.findByRole("button", { name: "More tabs" });
    const group = screen.getByRole("group", { name: "Fleet view" });
    expect([...group.querySelectorAll("[data-segment]")].map((button) => button.textContent)).toEqual(["All", "Failed"]);
  });

  it("settles when the bar narrows with the options it moved to the menu (E2E of #10402)", async () => {
    layout();
    // A content-sized bar: 290 px while every option is drawn, 240 px once one went to the menu.
    Object.defineProperty(HTMLElement.prototype, "clientWidth", {
      configurable: true,
      get(this: HTMLElement) {
        return this.querySelectorAll("[data-segment]").length === STATUS.length ? 290 : 240;
      },
    });
    renderWithProviders(
      <SegmentedFilter ariaLabel="Fleet view" options={STATUS} value="" onChange={() => {}} overflow="menu" />,
    );
    expect(await screen.findByRole("button", { name: "More tabs" })).toBeInTheDocument();
    const group = screen.getByRole("group", { name: "Fleet view" });
    expect([...group.querySelectorAll("[data-segment]")].map((button) => button.textContent)).toEqual(["All", "Running"]);
  });

  it("brings the folded options back when the row gains room (UX r3 B1 on #10402)", async () => {
    Element.prototype.getBoundingClientRect = () => ({ width: 100 }) as DOMRect;
    let room = 290;
    // A content-sized bar in a row: while measuring it takes the room the row
    // gives it; once fitted it is as wide as what it draws (100 px a pill, 40 px
    // the menu), whatever room the row has.
    Object.defineProperty(HTMLElement.prototype, "clientWidth", {
      configurable: true,
      get(this: HTMLElement) {
        if (this.dataset.testid === "row") return room;
        if (!this.firstElementChild?.hasAttribute("data-overflow")) return 0;
        if (this.dataset.measuring) return room;
        const pills = this.querySelectorAll("[data-segment]").length;
        return pills * 100 + (pills < STATUS.length ? 40 : 0);
      },
    });
    const callbacks: (() => void)[] = [];
    const original = globalThis.ResizeObserver;
    globalThis.ResizeObserver = class {
      constructor(callback: () => void) {
        callbacks.push(callback);
      }
      observe() {}
      unobserve() {}
      disconnect() {}
    } as unknown as typeof ResizeObserver;
    try {
      renderWithProviders(
        <div data-testid="row">
          <SegmentedFilter ariaLabel="Fleet view" options={STATUS} value="" onChange={() => {}} overflow="menu" />
        </div>,
      );
      expect(await screen.findByRole("button", { name: "More tabs" })).toBeInTheDocument();
      // The window widens: the row has room for every option again.
      room = 400;
      act(() => callbacks.forEach((callback) => callback()));
      expect(screen.queryByRole("button", { name: "More tabs" })).toBeNull();
      const group = screen.getByRole("group", { name: "Fleet view" });
      expect([...group.querySelectorAll("[data-segment]")].map((button) => button.textContent)).toEqual(["All", "Running", "Failed"]);
      // And narrows again: the last option folds back into the menu.
      room = 290;
      act(() => callbacks.forEach((callback) => callback()));
      expect(await screen.findByRole("button", { name: "More tabs" })).toBeInTheDocument();
    } finally {
      globalThis.ResizeObserver = original;
    }
  });

  it("shows no menu when every option fits", () => {
    layout(400);
    renderWithProviders(
      <SegmentedFilter ariaLabel="Fleet view" options={STATUS} value="" onChange={() => {}} overflow="menu" />,
    );
    expect(screen.queryByRole("button", { name: "More tabs" })).toBeNull();
    expect(screen.getAllByRole("button")).toHaveLength(3);
  });
});

describe("FleetAgentPickerGroup", () => {
  it("wraps the agent picker in one group, under the caller's test id", () => {
    renderWithProviders(
      <FleetAgentPickerGroup
        testId="executions-picker-group"
        showFleet
        fleet={undefined}
        onFleetChange={() => {}}
        agentPicker={<button type="button">Agent picker</button>}
      />,
    );
    const group = screen.getByTestId("executions-picker-group");
    expect(group).toContainElement(screen.getByRole("button", { name: "Agent picker" }));
  });

  it("renders nothing with neither picker", () => {
    renderWithProviders(
      <FleetAgentPickerGroup testId="g" showFleet={false} fleet={undefined} onFleetChange={() => {}} agentPicker={null} />,
    );
    expect(screen.queryByTestId("g")).toBeNull();
  });
});

describe("useCompactToolbar", () => {
  const original = window.matchMedia;
  afterEach(() => {
    window.matchMedia = original;
  });

  it("is false where matchMedia is missing", () => {
    // @ts-expect-error jsdom without matchMedia
    window.matchMedia = undefined;
    const { result } = renderHook(() => useCompactToolbar());
    expect(result.current).toBe(false);
  });

  it("follows the compact query", () => {
    const queries: string[] = [];
    window.matchMedia = ((query: string) => {
      queries.push(query);
      return {
        matches: true,
        media: query,
        addEventListener: () => {},
        removeEventListener: () => {},
      } as unknown as MediaQueryList;
    }) as typeof window.matchMedia;
    const { result } = renderHook(() => useMediaQuery(COMPACT_TOOLBAR_QUERY));
    expect(result.current).toBe(true);
    expect(queries).toContain("(max-width: 1439px)");
  });
});
