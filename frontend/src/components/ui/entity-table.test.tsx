import { afterEach, describe, expect, it, vi } from "vitest";
import { act, screen } from "@testing-library/react";
import { useEffect, useState } from "react";
import { MemoryRouter } from "react-router-dom";
import { readFileSync, readdirSync, statSync } from "node:fs";
import { join } from "node:path";

import { renderWithProviders } from "@/testing/providers";
import { EntityNameCell, EntityTable, type EntityTableColumn } from "./entity-table";

const badges = (
  <>
    <span>Company-managed</span>
    <span>Built-in</span>
    <span>Shared</span>
  </>
);

describe("EntityNameCell", () => {
  it("keeps the badges on the name's line by default", () => {
    renderWithProviders(<EntityNameCell name="Filigran mailbox" description="Outlook" badges={badges} />);
    const cell = screen.getByTestId("entity-name-cell");
    const row = screen.getByTestId("entity-name-badges");
    // Trailing: a sibling of the text block, never shrunk.
    expect(row.parentElement).toBe(cell);
    expect(row.className).toContain("shrink-0");
    expect(cell.className).not.toContain("min-w-[12rem]");
  });

  // Three badges trailing a name took its whole width, and the name
  // was gone at 1024 px. Below, they go to the second line, before the
  // description, which never wraps: standard v2, never a third line.
  it("puts the badges on the second line before the description with badgesPlacement below", () => {
    renderWithProviders(
      <EntityNameCell name="Filigran mailbox" description="Outlook" badges={badges} badgesPlacement="below" />,
    );
    const cell = screen.getByTestId("entity-name-cell");
    const name = screen.getByTestId("entity-name");
    const line2 = screen.getByTestId("entity-name-line2");
    const row = screen.getByTestId("entity-name-badges");
    expect(name.textContent).toBe("Filigran mailbox");
    expect(name.className).toContain("truncate");
    expect(row.parentElement).toBe(line2);
    expect(line2.parentElement).toBe(name.parentElement);
    expect(line2.className).toContain("flex-nowrap");
    expect(row.nextElementSibling).toBe(screen.getByTestId("entity-description"));
    expect(cell.className).toContain("min-w-[12rem]");
    expect(screen.getByText("Shared")).toBeTruthy();
  });

  // A truncated name is still readable in full on hover.
  it("gives the name its full text as a title", () => {
    const long = "Filigran shared mailbox for the threat intelligence team in Paris";
    renderWithProviders(<EntityNameCell name={long} badges={badges} badgesPlacement="below" />);
    expect(screen.getByTestId("entity-name").getAttribute("title")).toBe(long);
  });

  // UX r1 B1 of #10493: /skills cut 77 of 79 descriptions with no way to read them.
  it("gives the description its full text as a title", () => {
    const long = "Reads the threat intelligence reports of the week and drafts a summary for the analysts";
    renderWithProviders(<EntityNameCell name="Weekly digest" description={long} />);
    const description = screen.getByTestId("entity-description");
    expect(description.textContent).toBe(long);
    expect(description.getAttribute("title")).toBe(long);
  });

  it("renders no badge row when there is no badge", () => {
    renderWithProviders(<EntityNameCell name="Plain" badgesPlacement="below" />);
    expect(screen.queryByTestId("entity-name-badges")).toBeNull();
    expect(screen.getByTestId("entity-name").textContent).toBe("Plain");
  });

  // A page that builds its badges conditionally hands
  // over an empty set for a row with none. It drew an empty row with a 4 px
  // top margin; an empty set is no badge, in both placements.
  it.each([
    ["an empty array", []],
    ["an array of only false and null", [false, null, undefined]],
  ])("renders no badge row for %s", (_label, empty) => {
    renderWithProviders(
      <>
        <EntityNameCell name="Below" badges={empty} badgesPlacement="below" />
        <EntityNameCell name="Trailing" badges={empty} />
      </>,
    );
    expect(screen.queryByTestId("entity-name-badges")).toBeNull();
    expect(screen.getAllByTestId("entity-name").map((n) => n.textContent)).toEqual(["Below", "Trailing"]);
  });

  // Standard v2 (Sam, 10 Nov 2026): two lines at most, one row height. The
  // name is line 1; line 2 holds the badges and the description, and stays
  // there (an empty slot) when the row has neither, so every row is two lines.
  it("draws every below row on two lines and never a third", () => {
    renderWithProviders(
      <>
        <EntityNameCell name="Full" description="Outlook" badges={badges} badgesPlacement="below" />
        <EntityNameCell name="Plain" description="Outlook" badgesPlacement="below" />
        <EntityNameCell name="Bare" badges={badges} badgesPlacement="below" />
        <EntityNameCell name="Alone" badgesPlacement="below" />
      </>,
    );
    for (const text of screen.getAllByTestId("entity-name-text")) {
      expect(text.children).toHaveLength(2);
    }
    expect(screen.getAllByTestId("entity-name-line2")).toHaveLength(4);
    expect(screen.queryByTestId("entity-name-sizer")).toBeNull();
  });

  it("keeps an empty second line on a below row with no badge and no description", () => {
    renderWithProviders(<EntityNameCell name="Alone" badgesPlacement="below" />);
    const slot = screen.getByTestId("entity-description-slot");
    expect(slot.getAttribute("aria-hidden")).toBe("true");
    expect(slot.parentElement).toBe(screen.getByTestId("entity-name-line2"));
  });

  it("draws no empty slot when the row has a badge or a description, or trails them", () => {
    renderWithProviders(
      <>
        <EntityNameCell name="Bare" badges={badges} badgesPlacement="below" />
        <EntityNameCell name="Plain" description="Outlook" badgesPlacement="below" />
        <EntityNameCell name="Trailing" />
      </>,
    );
    expect(screen.queryByTestId("entity-description-slot")).toBeNull();
  });

  // UX round 2 on #10920, B1: three badges needed 345 px in a 322 px line 2, the
  // last one was cut mid-word and the description squeezed to 0 px. A badge is
  // never cut: the ones that do not fit fold into a "+N" badge with a tooltip.
  describe("line 2 fold", () => {
    afterEach(() => vi.restoreAllMocks());

    it("folds the badges that do not fit into a +N badge, never cutting one", () => {
      // The line is 200 px, 64 kept for the description: 136 px for the badges,
      // each 60 px wide, the marker included.
      vi.spyOn(HTMLElement.prototype, "clientWidth", "get").mockImplementation(function (this: HTMLElement) {
        return this.dataset.testid === "entity-name-line2" ? 200 : 0;
      });
      vi.spyOn(HTMLElement.prototype, "offsetWidth", "get").mockImplementation(function (this: HTMLElement) {
        return this.dataset.testid === "entity-name-badges" ? this.children.length * 60 : 0;
      });
      renderWithProviders(
        <EntityNameCell name="Filigran AI" description="OpenAI" badges={[<span key="a">Company-managed</span>, <span key="b">Shared</span>, <span key="c">Shared - 1 way</span>]} badgesPlacement="below" />,
      );
      const row = screen.getByTestId("entity-name-badges");
      expect(row.textContent).toContain("Company-managed");
      expect(row.textContent).not.toContain("Shared - 1 way");
      const more = screen.getByTestId("entity-name-badges-more");
      expect(more.textContent).toBe("+2");
      expect(more.getAttribute("tabindex")).toBe("0");
      expect(more.getAttribute("aria-label")).toBe("2 more");
      expect(screen.getByTestId("entity-description").textContent).toBe("OpenAI");
      // The hidden badges render only in a display:none source the tooltip
      // reads as plain text (UX round 4 on #10920): never as nested triggers.
      const source = screen.getByTestId("entity-name-badges-hidden-source");
      expect(source.getAttribute("aria-hidden")).toBe("true");
      expect(source.className).toContain("hidden");
      expect(Array.from(source.children, (c) => c.textContent)).toEqual(["Shared", "Shared - 1 way"]);
    });

    // UX round 5 on #10920, B1: a hidden badge that changes its own text after
    // mount (a relay badge's fallback, then its name once its query answers on a
    // cold load) left the tooltip on the fallback. The tooltip follows the source.
    it("keeps the +N tooltip labels equal to the hidden badges when one changes after mount", async () => {
      vi.spyOn(HTMLElement.prototype, "clientWidth", "get").mockImplementation(function (this: HTMLElement) {
        return this.dataset.testid === "entity-name-line2" ? 200 : 0;
      });
      vi.spyOn(HTMLElement.prototype, "offsetWidth", "get").mockImplementation(function (this: HTMLElement) {
        return this.dataset.testid === "entity-name-badges" ? this.children.length * 60 : 0;
      });
      let answer: (label: string) => void = () => {};
      const query = new Promise<string>((done) => {
        answer = done;
      });
      // Its fallback first, then its own query's answer: the line does not re-render.
      function LateBadge() {
        const [label, setLabel] = useState("via a relay group");
        useEffect(() => {
          void query.then(setLabel);
        }, []);
        return <span>{label}</span>;
      }
      renderWithProviders(
        <EntityNameCell name="LM Studio" description="Local" badges={[<span key="a">Company-managed</span>, <span key="b">Shared</span>, <LateBadge key="c" />]} badgesPlacement="below" />,
      );
      await act(async () => {
        answer("via Lab sample relay group");
        await query;
      });
      const more = screen.getByTestId("entity-name-badges-more");
      act(() => more.focus());
      await act(() => new Promise<void>((done) => requestAnimationFrame(() => done())));
      const source = screen.getByTestId("entity-name-badges-hidden-source");
      const sourceLabels = Array.from(source.children, (c) => (c.textContent ?? "").trim());
      expect(sourceLabels).toContain("via Lab sample relay group");
      const list = await screen.findAllByTestId("entity-name-badges-more-list");
      const labels = Array.from(list[0].querySelectorAll("li"), (li) => li.textContent);
      expect(labels).toEqual(sourceLabels);
    });

    it("shows every badge and no marker when they fit", () => {
      vi.spyOn(HTMLElement.prototype, "clientWidth", "get").mockReturnValue(400);
      vi.spyOn(HTMLElement.prototype, "offsetWidth", "get").mockReturnValue(180);
      renderWithProviders(<EntityNameCell name="Fits" description="Outlook" badges={badges} badgesPlacement="below" />);
      expect(screen.queryByTestId("entity-name-badges-more")).toBeNull();
      expect(screen.getByText("Shared")).toBeTruthy();
    });
  });

  it("keeps the badges of an array that holds some", () => {
    renderWithProviders(
      <EntityNameCell
        name="Mixed"
        badgesPlacement="below"
        badges={[false, <span key="b">Built-in</span>, null]}
      />,
    );
    const row = screen.getByTestId("entity-name-badges");
    expect(row.textContent).toBe("Built-in");
  });
});

interface Item {
  id: string;
  name: string;
}

const ITEM_COLUMNS: EntityTableColumn<Item>[] = [
  { key: "name", header: "Name", cell: (item) => item.name, sortValue: (item) => item.name },
  { key: "owner", header: "Owner", cell: () => "me" },
];

// #11345: a list page loads on its own table, never on a generic page skeleton.
describe("EntityTable loading", () => {
  it("draws the real head and skeleton rows of the loaded row geometry while loading", () => {
    const search = { value: "", onChange: vi.fn(), placeholder: "Search items..." };
    const { unmount } = renderWithProviders(
      <MemoryRouter>
        <EntityTable items={[]} columns={ITEM_COLUMNS} rowKey={(item) => item.id} search={search} isLoading />
      </MemoryRouter>,
    );
    // The real toolbar stays mounted while loading.
    expect(screen.getAllByLabelText("Search items...").length).toBeGreaterThan(0);
    const skeletonRows = screen.getAllByTestId("data-table-skeleton-row");
    expect(skeletonRows.length).toBeGreaterThan(0);
    expect(skeletonRows[0].children).toHaveLength(ITEM_COLUMNS.length);
    const skeletonSlots = skeletonRows[0].children.length;
    unmount();

    renderWithProviders(
      <MemoryRouter>
        <EntityTable items={[{ id: "a", name: "Alpha" }]} columns={ITEM_COLUMNS} rowKey={(item) => item.id} search={search} />
      </MemoryRouter>,
    );
    expect(screen.queryAllByTestId("data-table-skeleton-row")).toHaveLength(0);
    const row = screen.getByText("Alpha").closest("tr");
    expect(row).not.toBeNull();
    // The same slots as the skeleton row (one per drawn column), so nothing moves when the rows land.
    expect(row?.children).toHaveLength(skeletonSlots);
  });

  it("keeps every page that renders an EntityTable off the generic PageSkeleton", () => {
    // vitest runs from frontend/, as no-class-name-assertions.test.ts.
    const pages = join(process.cwd(), "src", "pages");
    const offenders: string[] = [];
    const walk = (dir: string) => {
      for (const name of readdirSync(dir)) {
        const path = join(dir, name);
        if (statSync(path).isDirectory()) walk(path);
        else if (name.endsWith(".tsx") && !name.includes(".test.")) {
          const source = readFileSync(path, "utf8");
          if (source.includes("<EntityTable") && source.includes("<PageSkeleton")) offenders.push(path);
        }
      }
    };
    walk(pages);
    expect(offenders).toEqual([]);
  });
});
