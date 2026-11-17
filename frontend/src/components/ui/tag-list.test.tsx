// TagList keeps a card's tag row inside the card: every visible tag
// carries its full name as a title (the badge truncates it on a narrow card)
// and the `+N` marker names the tags it hides.

import { afterEach, describe, expect, it, vi } from "vitest";
import { screen } from "@testing-library/react";

import { renderWithProviders } from "@/testing/providers";
import { TagList } from "./tag-list";

describe("TagList", () => {
  it("renders nothing without tags", () => {
    const { container } = renderWithProviders(<TagList tags={[]} />);
    expect(container.textContent).toBe("");
  });

  it("gives every visible tag its full name as a title", () => {
    const longTag = "threat-intelligence-reporting-and-briefing";
    renderWithProviders(<TagList tags={[longTag, "cti"]} />);
    expect(screen.getByTitle(longTag).textContent).toBe(longTag);
    expect(screen.getByTitle("cti").textContent).toBe("cti");
  });

  it("collapses the tags past max into a +N that names them", () => {
    renderWithProviders(<TagList tags={["a", "b", "c", "d", "e"]} max={3} />);
    const more = screen.getByText("+2");
    expect(more.getAttribute("aria-label")).toBe("d, e");
    expect(screen.queryByTitle("d")).toBeNull();
  });

  it("makes the +N marker reachable by keyboard, with no native title", () => {
    renderWithProviders(<TagList tags={["a", "b", "c", "d"]} max={2} />);
    const more = screen.getByText("+2");
    expect(more.getAttribute("tabindex")).toBe("0");
    expect(more.getAttribute("title")).toBeNull();
  });

  describe("on a row too narrow for every badge", () => {
    afterEach(() => {
      vi.restoreAllMocks();
    });

    it("folds the badges that do not fit into a +N that names them", () => {
      // jsdom has no layout: the badge part of the row is 100 px wide and holds
      // one badge at 60 px, two overflow it.
      vi.spyOn(HTMLElement.prototype, "clientWidth", "get").mockImplementation(function (this: HTMLElement) {
        return this.hasAttribute("data-tag-row") ? 100 : 0;
      });
      vi.spyOn(HTMLElement.prototype, "scrollWidth", "get").mockImplementation(function (this: HTMLElement) {
        return this.hasAttribute("data-tag-row") ? this.children.length * 60 : 0;
      });
      renderWithProviders(<TagList tags={["ransomware", "phishing", "apt"]} />);
      expect(screen.getByTitle("ransomware").textContent).toBe("ransomware");
      expect(screen.queryByTitle("phishing")).toBeNull();
      expect(screen.getByText("+2").getAttribute("aria-label")).toBe("phishing, apt");
    });

    it("keeps the whole +N in the row when no badge fits", () => {
      // The badge part gets 10 px: even one badge at its minimum overflows it.
      vi.spyOn(HTMLElement.prototype, "clientWidth", "get").mockImplementation(function (this: HTMLElement) {
        return this.hasAttribute("data-tag-row") ? 10 : 0;
      });
      vi.spyOn(HTMLElement.prototype, "scrollWidth", "get").mockImplementation(function (this: HTMLElement) {
        return this.hasAttribute("data-tag-row") ? this.children.length * 60 : 0;
      });
      const { container } = renderWithProviders(<TagList tags={["ransomware", "phishing", "apt"]} />);
      expect(screen.queryByTitle("ransomware")).toBeNull();
      const more = screen.getByText("+3");
      expect(more.textContent).toBe("+3");
      expect(more.getAttribute("aria-label")).toBe("ransomware, phishing, apt");
      // The marker is not inside the clipping badge part of the row.
      expect(container.querySelector("[data-tag-row]")?.contains(more) ?? false).toBe(false);
    });

    it("in whole mode folds the second name rather than cutting both, and keeps the first", () => {
      // Two names at 90 px each in a 148 px row: both would be cut side by side.
      vi.spyOn(HTMLElement.prototype, "clientWidth", "get").mockImplementation(function (this: HTMLElement) {
        return this.hasAttribute("data-tag-row") ? 148 : 0;
      });
      vi.spyOn(HTMLElement.prototype, "scrollWidth", "get").mockImplementation(function (this: HTMLElement) {
        return this.hasAttribute("data-tag-row") ? this.children.length * 90 : 0;
      });
      renderWithProviders(<TagList tags={["Own organization", "Contoso Energy", "Northwind"]} max={2} badgeMin="whole" />);
      expect(screen.getByTitle("Own organization").textContent).toBe("Own organization");
      expect(screen.queryByTitle("Contoso Energy")).toBeNull();
      expect(screen.getByText("+2").getAttribute("aria-label")).toBe("Contoso Energy, Northwind");
    });

    it("folds a row of a hundred names without running into React's update limit", () => {
      // A person in about ninety groups blanked the Analytics users breakdown:
      // one badge folded per pass chained past React's 50 nested updates.
      vi.spyOn(HTMLElement.prototype, "clientWidth", "get").mockImplementation(function (this: HTMLElement) {
        return this.hasAttribute("data-tag-row") ? 160 : 0;
      });
      vi.spyOn(HTMLElement.prototype, "scrollWidth", "get").mockImplementation(function (this: HTMLElement) {
        return this.hasAttribute("data-tag-row") ? this.children.length * 70 : 0;
      });
      const tags = Array.from({ length: 100 }, (_, index) => `Group ${index}`);
      renderWithProviders(<TagList tags={tags} max={tags.length} badgeMin="readable" />);
      expect(screen.getByTitle("Group 0").textContent).toBe("Group 0");
      expect(screen.getByTitle("Group 1").textContent).toBe("Group 1");
      expect(screen.queryByTitle("Group 2")).toBeNull();
      expect(screen.getByText("+98")).toBeInTheDocument();
    });

    it("keeps every leading badge that fits when the badges have different widths", () => {
      // A 198 px row: two short names (64 px each, 4 px gap) end at 132 px, the
      // long third one overflows. A share of the row (5 x 198 / scrollWidth)
      // would keep one badge and leave about 130 px empty.
      const widths: Record<string, number> = { ctem: 64, fleet: 64, "threat-intelligence-reporting": 260, apt: 64, cti: 64 };
      const rowOf = (element: Element) => element.closest("[data-tag-row]");
      vi.spyOn(HTMLElement.prototype, "clientWidth", "get").mockImplementation(function (this: HTMLElement) {
        return this.hasAttribute("data-tag-row") ? 198 : 0;
      });
      vi.spyOn(HTMLElement.prototype, "scrollWidth", "get").mockImplementation(function (this: HTMLElement) {
        if (!this.hasAttribute("data-tag-row")) return 0;
        const badges = Array.from(this.children);
        return badges.reduce((sum, badge) => sum + (widths[badge.getAttribute("title") ?? ""] ?? 0), 0) + 4 * (badges.length - 1);
      });
      vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function (this: HTMLElement) {
        const row = this.hasAttribute("data-tag-row") ? null : rowOf(this);
        if (!row || this.parentElement !== row) return { left: 0, right: 198, width: 198 } as DOMRect;
        let left = 0;
        for (const badge of Array.from(row.children)) {
          const width = widths[badge.getAttribute("title") ?? ""] ?? 0;
          if (badge === this) return { left, right: left + width, width } as DOMRect;
          left += width + 4;
        }
        return { left: 0, right: 0, width: 0 } as DOMRect;
      });
      renderWithProviders(<TagList tags={Object.keys(widths)} max={5} badgeMin="whole" />);
      expect(screen.getByTitle("ctem").textContent).toBe("ctem");
      expect(screen.getByTitle("fleet").textContent).toBe("fleet");
      expect(screen.queryByTitle("threat-intelligence-reporting")).toBeNull();
      expect(screen.getByText("+3").getAttribute("aria-label")).toBe("threat-intelligence-reporting, apt, cti");
    });

    it("in whole mode never folds the first name, even when it alone overflows", () => {
      vi.spyOn(HTMLElement.prototype, "clientWidth", "get").mockImplementation(function (this: HTMLElement) {
        return this.hasAttribute("data-tag-row") ? 10 : 0;
      });
      vi.spyOn(HTMLElement.prototype, "scrollWidth", "get").mockImplementation(function (this: HTMLElement) {
        return this.hasAttribute("data-tag-row") ? this.children.length * 90 : 0;
      });
      renderWithProviders(<TagList tags={["Own organization", "Contoso Energy"]} max={2} badgeMin="whole" />);
      expect(screen.getByTitle("Own organization")).toBeInTheDocument();
      expect(screen.getByText("+1").getAttribute("aria-label")).toBe("Contoso Energy");
    });
  });
});
