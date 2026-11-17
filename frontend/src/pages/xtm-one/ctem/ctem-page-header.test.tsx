// The one header band of the CTEM pages (#11660): back link and current page
// on the breadcrumb line, the title with its "i" (no paragraph), one cell per
// figure, the actions and the chips in their grid areas, an opening cell as a
// named button.

import { describe, expect, it, vi } from "vitest";
import { fireEvent, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

import { renderWithProviders } from "@/testing/providers";
import en from "@/i18n/lang/en.json";
import es from "@/i18n/lang/es.json";
import fr from "@/i18n/lang/fr.json";
import ja from "@/i18n/lang/ja.json";
import { CtemPageHeader, HEADER_MEDIUM_PX, HEADER_WIDE_PX, STAT_ROW_STYLE, headerGrid, headerLayout } from "./CtemPageHeader";

const CATALOGS: Record<string, Record<string, string>> = { en, fr, es, ja };
/** The widest stat row of the CTEM pages (the command center's five cells). */
const WIDEST_ROW = ["Exposure posture", "Organization", "Top risk", "Stages covered", "Agents"];

function draw(props: Partial<Parameters<typeof CtemPageHeader>[0]> = {}) {
  return renderWithProviders(
    <MemoryRouter>
      <CtemPageHeader title="Discovery" {...props} />
    </MemoryRouter>,
  );
}

describe("CtemPageHeader", () => {
  it("draws the back link, the current page and one h1 with its hint, no paragraph", () => {
    draw({ back: { to: "/", label: "Back to Command Center" }, crumb: "Stage 2 of 5", hint: "Threat-informed defense coverage", hintLabel: "About Discovery" });
    expect(screen.getByTestId("ctem-page-header-back").getAttribute("href")).toBe("/");
    expect(screen.getByText("Stage 2 of 5").getAttribute("aria-current")).toBe("page");
    expect(screen.getAllByRole("heading", { level: 1 })).toHaveLength(1);
    expect(screen.getByRole("img", { name: "About Discovery" })).toBeTruthy();
    expect(screen.getByTestId("ctem-page-header").querySelector("p")).toBeNull();
  });

  it("draws the eyebrow when there is no back link", () => {
    draw({ eyebrow: <span>CTEM Command Center</span> });
    expect(screen.getByText("CTEM Command Center")).toBeTruthy();
    expect(screen.queryByTestId("ctem-page-header-back")).toBeNull();
  });

  it("draws one cell per figure and opens the cell that has a target", () => {
    const onOpen = vi.fn();
    draw({
      stats: [
        { id: "score", label: "Score", value: 3, sub: "Critical" },
        { id: "top-risk", label: "Top risk", value: 0, title: "Top risk: 0 known-exploited", onOpen },
      ],
    });
    expect(screen.getByTestId("ctem-header-stat-score-value").textContent).toBe("3");
    expect(screen.getAllByTestId(/^ctem-header-stat-[a-z-]+-value$/)).toHaveLength(2);
    fireEvent.click(screen.getByRole("button", { name: "Top risk: 0 known-exploited" }));
    expect(onOpen).toHaveBeenCalledTimes(1);
  });

  it("leaves out the stats, actions and chips areas it is not given", () => {
    draw();
    expect(screen.queryByTestId("ctem-page-header-stats")).toBeNull();
    expect(screen.queryByTestId("ctem-page-header-actions")).toBeNull();
    expect(screen.queryByTestId("ctem-page-header-chips")).toBeNull();
  });

  // UX round 1 on #11677, B1 and B2: at the band widths of a 1080 and a 1280
  // window (852 and 1052 px) the cells get a full row, never a track shared
  // with the chips; the one-row layout starts only where everything fits.
  it("gives the stat cells a row of their own on a narrow band, and the chips the row under it", () => {
    for (const px of [852, 1052, HEADER_MEDIUM_PX - 1]) expect(headerLayout(px)).toBe("narrow");
    expect(headerLayout(HEADER_MEDIUM_PX)).toBe("medium");
    expect(headerLayout(1728)).toBe("medium");
    expect(headerLayout(2048)).toBe("medium");
    expect(headerLayout(HEADER_WIDE_PX)).toBe("wide");
    expect(headerLayout(null)).toBe("narrow");
    const narrow = headerGrid("narrow", { stats: true, chips: true });
    expect(narrow.areas).toBe("'title acts' 'stats stats' 'chips chips'");
    expect(narrow.columns.startsWith("minmax(min(14rem,100%),1fr)")).toBe(true);
    expect(headerGrid("medium", { stats: true, chips: false }).areas).toBe("'title stats acts'");
    expect(headerGrid("medium", { stats: true, chips: true }).areas).toBe("'title stats acts' 'chips chips chips'");
    expect(headerGrid("narrow", { stats: false, chips: false }).areas).toBe("'title acts'");
  });

  // UX round 1 on #11677, B3: the height reported is the band's own, not the
  // stretched cell around it.
  it("reports the height of the band itself", () => {
    const original = HTMLElement.prototype.getBoundingClientRect;
    HTMLElement.prototype.getBoundingClientRect = function (this: HTMLElement) {
      const tall = this.tagName === "HEADER" ? 66 : 380;
      return { width: 1600, height: tall, top: 0, left: 0, right: 1600, bottom: tall, x: 0, y: 0, toJSON: () => ({}) } as DOMRect;
    };
    try {
      const onContentHeight = vi.fn();
      draw({ onContentHeight, stats: [{ id: "score", label: "Score", value: 3 }] });
      expect(onContentHeight).toHaveBeenCalledWith(66);
      expect(onContentHeight).not.toHaveBeenCalledWith(380);
      expect(screen.getByTestId("ctem-page-header").getAttribute("data-layout")).toBe("medium");
    } finally {
      HTMLElement.prototype.getBoundingClientRect = original;
    }
  });

  // Production retest 195b5f64 of #11677: at 1080 the French and Spanish rows
  // were 870 and 887 px in an 810 px band, the Agents cell clipped and the page
  // scrolling sideways. jsdom has no layout, so the rule is pinned where it
  // lives: in every language the row wraps and never clips, and every label
  // keeps its full text for the hover when its cell truncates it.
  for (const lang of Object.keys(CATALOGS)) {
    it(`never clips the widest stat row in ${lang}: it wraps whole cells instead`, () => {
      const labels = WIDEST_ROW.map((key) => CATALOGS[lang][key] ?? key);
      draw({ stats: labels.map((label, i) => ({ id: `cell-${i}`, label, value: i })) });
      const row = screen.getByRole("group", { name: "Key figures" });
      expect(row.style.flexWrap).toBe(STAT_ROW_STYLE.flexWrap);
      expect(row.style.overflow).toBe("visible");
      for (const label of labels) expect(screen.getByTitle(label).textContent).toBe(label);
      expect(screen.getAllByTestId(/^ctem-header-stat-cell-\d-value$/)).toHaveLength(WIDEST_ROW.length);
    });
  }

  it("keeps the current page's words whole on the breadcrumb line", () => {
    draw({ back: { to: "/", label: "Back to Command Center" }, crumb: "Stage 2 of 5", crumbExtras: <span>Sample</span> });
    expect(screen.getByText("Stage 2 of 5").textContent).toBe("Stage 2 of 5");
  });

  it("draws the actions and the chips when given", () => {
    draw({ actions: <button type="button">Export</button>, chips: <span>OpenCTI</span> });
    expect(screen.getByTestId("ctem-page-header-actions").textContent).toContain("Export");
    expect(screen.getByTestId("ctem-page-header-chips").textContent).toContain("OpenCTI");
  });
});
