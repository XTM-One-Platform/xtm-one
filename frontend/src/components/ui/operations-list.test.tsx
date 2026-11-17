/**
 * The Operations list contract: the shared row grid, its tokens, the
 * updated cell and the hover card. Geometry (same height, same x) is measured
 * by tests_e2e/operations-list-grid.spec.ts; here the structure and content.
 */
import { describe, expect, it, vi } from "vitest";
import { fireEvent, screen, within } from "@testing-library/react";
import { Hash, Trash2 } from "lucide-react";

import { renderWithProviders } from "@/testing/providers";

vi.mock("@/hooks/useAuth", () => ({ useAuth: () => ({ user: { id: "u1", timezone: "Europe/Paris" }, isAdmin: false }) }));

const {
  OPS_ROW_ALIGN, OPS_ROW_HEIGHT, OpsChipStrip, OpsHoverCard, OpsListSkeleton, OpsRowAction, OpsRowGrid, OpsRowSkeleton, OpsToken, OpsUpdated,
  opsSkeletonRowCount,
} = await import("./operations-list");
const { Badge } = await import("./badge");

describe("OpsChipStrip", () => {
  // happy-dom lays nothing out: tests_e2e/operations-list-grid.spec.ts measures
  // every chip against the strip's 20 px line, border included (#9774).
  it("renders every badge it holds, nested or not", () => {
    renderWithProviders(
      <OpsChipStrip testId="strip">
        <Badge variant="outline" className="content-compact">Direct</Badge>
        <span>
          <Badge variant="outline" className="content-compact">Nested</Badge>
        </span>
      </OpsChipStrip>,
    );
    const strip = screen.getByTestId("strip");
    expect(within(strip).getByText("Direct")).toBeTruthy();
    expect(within(strip).getByText("Nested")).toBeTruthy();
    expect(strip.querySelectorAll('[data-slot="badge"]')).toHaveLength(2);
  });
});

describe("OpsRowSkeleton (#11345)", () => {
  /** The grid's five slots, in order, as the loaded row draws them. */
  const slots = (row: HTMLElement) => {
    const grid = row.querySelector("[data-testid='ops-meta']")?.parentElement;
    expect(grid).toBeTruthy();
    return Array.from(grid!.children) as HTMLElement[];
  };

  it("draws the loaded row's grid: the same five slots, the actions at the tab's width", () => {
    renderWithProviders(
      <div>
        <div data-testid="loaded">
          <OpsRowGrid
            lead={<><span>box</span><span>icon</span></>}
            main={<><span>title</span><span>summary</span></>}
            meta={<><span>owner</span><span>key</span></>}
            updated={<span>2 min ago</span>}
            actions={<><button type="button">a</button><button type="button">b</button></>}
            actionsClassName="w-32"
            actionsTestId="loaded-actions"
          />
        </div>
        <div data-testid="skeleton">
          <OpsRowSkeleton actionsClassName="w-32" actions={2} />
        </div>
      </div>,
    );
    const loaded = slots(screen.getByTestId("loaded"));
    const skeleton = slots(screen.getByTestId("skeleton"));
    expect(skeleton).toHaveLength(loaded.length);
    // The skeleton IS the row grid (OpsRowGrid), each slot holding as many
    // placeholders as the loaded row holds pieces: a loaded row lands on it.
    skeleton.forEach((slot, i) => {
      expect(slot.children.length).toBe(loaded[i].children.length);
    });
  });

  it("sits in a row card at the loaded row's height floor", () => {
    renderWithProviders(<OpsRowSkeleton actionsClassName="w-16" />);
    const card = screen.getByTestId("ops-row-skeleton");
    // tests_e2e/operations-list-grid.spec.ts measures the heights; here the floor is the row's own.
    // OPS_ROW_HEIGHT is several classes (the floor and the centring): every one on one element.
    expect(card.querySelector(OPS_ROW_HEIGHT.split(" ").map((name) => `.${name}`).join(""))).not.toBeNull();
  });

  it("fills the window, never more than a page", () => {
    expect(opsSkeletonRowCount(20, 1080)).toBe(15);
    expect(opsSkeletonRowCount(20, 3000)).toBe(20);
    expect(opsSkeletonRowCount(20, 0)).toBe(20);
    expect(opsSkeletonRowCount(5, 1080)).toBe(5);
  });

  it("announces the list as one busy status and draws the rows it is given", () => {
    renderWithProviders(<OpsListSkeleton label="Loading..." pageSize={20} rows={4} actionsClassName="w-16" />);
    const region = screen.getByRole("status", { name: "Loading..." });
    expect(region.getAttribute("aria-busy")).toBe("true");
    expect(within(region).getAllByTestId("ops-row-skeleton")).toHaveLength(4);
  });
});

describe("OpsRowGrid", () => {
  it("lays every slot in its own column, the actions at the width the tab sets", () => {
    renderWithProviders(
      <div className={OPS_ROW_HEIGHT} data-testid="row">
        <OpsRowGrid
          lead={<span>lead</span>}
          main={<span>main</span>}
          meta={<span>meta</span>}
          updated={<span>updated</span>}
          actions={<button type="button">act</button>}
          actionsClassName="w-32"
          actionsTestId="row-actions"
        />
      </div>,
    );
    expect(within(screen.getByTestId("row-actions")).getByRole("button", { name: "act" })).toBeTruthy();
    expect(within(screen.getByTestId("ops-meta")).getByText("meta")).toBeTruthy();
    // The meta column never holds the updated cell or the actions.
    expect(screen.getByTestId("ops-meta").textContent).toBe("meta");
  });

  it("gives the row a height floor, never a fixed height that clips the summary line (#8922)", () => {
    // A fixed `h-*` with the card's overflow-hidden cut line 2 of every
    // Executions card; the floor lets a row grow to its two lines.
    const classes = OPS_ROW_HEIGHT.split(" ");
    expect(classes.filter((c) => /^min-h-\d+$/.test(c))).toHaveLength(1);
    expect(classes.some((c) => /^h-/.test(c))).toBe(false);
  });

  it("centres every slot on the row's vertical axis, a one-line and a two-line block alike (Sam, 12 Nov 2026)", () => {
    // The date and the kebab once sat at the top of a card whose owner and key
    // formed a two-line stack: the grid aligned its slots to the start.
    expect(OPS_ROW_ALIGN).toBe("items-center");
    // The row centres the grid it holds when the floor makes it taller. The
    // grid's own markup is gated from source (operations-row-actions.test.ts)
    // and its geometry measured within 1 px (tests_e2e/operations-list-grid.spec.ts).
    expect(OPS_ROW_HEIGHT.split(" ")).toEqual(expect.arrayContaining(["flex", "flex-col", "justify-center"]));
  });

  it("keeps a click on an action from reaching the row", () => {
    const onRow = vi.fn();
    renderWithProviders(
      <div onClick={onRow}>
        <OpsRowGrid lead={null} main={null} updated={null} actions={<button type="button">act</button>} actionsClassName="w-32" />
      </div>,
    );
    fireEvent.click(screen.getByText("act"));
    expect(onRow).not.toHaveBeenCalled();
  });
});

describe("OpsRowAction", () => {
  it("is one named button and the only tab stop while it is enabled", () => {
    const onClick = vi.fn();
    const { container } = renderWithProviders(<OpsRowAction icon={Trash2} tone="danger" label="Delete" onClick={onClick} />);
    const button = screen.getByRole("button", { name: "Delete" });
    fireEvent.click(button);
    expect(onClick).toHaveBeenCalledTimes(1);
    // No focusable wrapper before it: Tab lands on the button itself.
    expect(container.querySelectorAll("[tabindex]")).toHaveLength(0);
    expect(container.querySelector('[aria-disabled="true"]')).toBeNull();
  });

  it("names its tooltip with the same word on keyboard focus", async () => {
    renderWithProviders(<OpsRowAction icon={Trash2} label="Delete" onClick={() => undefined} />);
    fireEvent.focus(screen.getByRole("button", { name: "Delete" }));
    expect((await screen.findByRole("tooltip")).textContent).toBe("Delete");
  });

  it("while busy, is disabled and its wrapper keeps the tooltip reachable", () => {
    const { container } = renderWithProviders(<OpsRowAction icon={Trash2} label="Delete" onClick={() => undefined} busy />);
    expect((screen.getByRole("button", { name: "Delete" }) as HTMLButtonElement).disabled).toBe(true);
    const wrapper = container.querySelector('[aria-disabled="true"]');
    expect(wrapper?.getAttribute("tabindex")).toBe("0");
  });
});

describe("OpsToken", () => {
  it("renders one line, cut with an ellipsis, with a copy button when asked", () => {
    renderWithProviders(
      <OpsToken icon={Hash} copy="dev:operations-lists-best-in-class" copyLabel="Copy the key" testId="token">
        dev:operations-lists-best-in-class
      </OpsToken>,
    );
    expect(screen.getByTestId("token").textContent).toBe("dev:operations-lists-best-in-class");
    expect(screen.getByRole("button", { name: "Copy the key" })).toBeTruthy();
  });
});

describe("OpsUpdated", () => {
  it("shows the relative time and gives the absolute time in the viewer's zone on focus", async () => {
    const at = new Date(Date.now() - 7 * 60_000).toISOString();
    renderWithProviders(<OpsUpdated at={at} testId="updated" />);
    const cell = screen.getByTestId("updated");
    expect(cell.textContent).toMatch(/7 minutes ago|7 min/);
    fireEvent.focus(cell);
    expect((await screen.findByRole("tooltip")).textContent).toMatch(/^Updated /);
  });

  it("keeps its column when the time is unknown", () => {
    const { container } = renderWithProviders(<OpsUpdated at={null} />);
    expect(container.querySelector('[aria-hidden="true"]')).not.toBeNull();
    expect(container.textContent).toBe("");
  });
});

describe("OpsHoverCard", () => {
  it("opens on keyboard focus with the title and every fact whole, and closes on blur", async () => {
    const long = "[CEO Office] Executive Assistant with a very long name that a row would cut";
    renderWithProviders(
      <OpsHoverCard
        title="A long work item title the row truncates"
        facts={[
          { label: "Owner", value: long },
          { label: "Key", value: "dev:operations-lists-best-in-class" },
        ]}
      >
        <button type="button">open</button>
      </OpsHoverCard>,
    );
    fireEvent.focus(screen.getByText("open"));
    const card = await screen.findByTestId("ops-hover-card");
    expect(card.textContent).toContain("A long work item title the row truncates");
    expect(within(card).getAllByRole("term").map((t) => t.textContent)).toEqual(["Owner", "Key"]);
    expect(within(card).getAllByRole("definition")[0].textContent).toBe(long);
    fireEvent.blur(screen.getByText("open"));
    fireEvent.keyDown(document.body, { key: "Escape" });
  });
});
