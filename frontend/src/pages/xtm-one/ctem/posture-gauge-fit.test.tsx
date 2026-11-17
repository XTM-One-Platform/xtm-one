// The stage page gauge (104 px) draws the qualitative inner ring: its centre
// text must stay inside the ring's inner edge (UX round 1 of #11469, B1: the
// band caption was painted on the ring).

import { describe, expect, it } from "vitest";
import { screen } from "@testing-library/react";

import { renderWithProviders } from "@/testing/providers";
import { GAUGE_FULL_CENTER_MIN_EDGE, PostureGauge, gaugeInnerEdge } from "./ctem-viz";

// Measured in the UX lab (#11471 round 2): the band caption box reaches
// 37.6 px from the centre. The 24 px "100" draws a 48 x 32 line box (reach
// 28.8 px) whose glyph ink is 43 x 19 (reach 23.5 px).
const CAPTION_REACH = 37.6;
const COMPACT_LINE_BOX_REACH = Math.hypot(24, 16);
const COMPACT_INK_REACH = Math.hypot(21.5, 9.5);

describe("PostureGauge centre fit", () => {
  it("keeps the caption reach plus 2 px under the threshold", () => {
    expect(CAPTION_REACH + 2).toBeLessThanOrEqual(GAUGE_FULL_CENTER_MIN_EDGE);
  });

  it("stage page size 104: compact centre, caption dropped, number inside the inner edge", () => {
    const edge = gaugeInnerEdge(104, 9);
    expect(edge).toBeLessThan(GAUGE_FULL_CENTER_MIN_EDGE);
    // The line box stays inside the ring; the ink keeps 2 px of air.
    expect(COMPACT_LINE_BOX_REACH).toBeLessThan(edge);
    expect(COMPACT_INK_REACH + 2).toBeLessThan(edge);
    renderWithProviders(
      <PostureGauge score={0} band="critical" size={104} strokeWidth={9} qualitative={{ score: null, band: "unknown" }} />,
    );
    expect(screen.queryByTestId("ctem-gauge-caption")).toBeNull();
    expect(screen.getByTestId("ctem-gauge-score").dataset.center).toBe("compact");
  });

  it("hero size 152: the full centre fits, caption kept", () => {
    expect(gaugeInnerEdge(152, 12)).toBeGreaterThanOrEqual(GAUGE_FULL_CENTER_MIN_EDGE);
    renderWithProviders(
      <PostureGauge score={0} band="critical" size={152} qualitative={{ score: null, band: "unknown" }} />,
    );
    expect(screen.getByTestId("ctem-gauge-caption")).toBeTruthy();
    expect(screen.getByTestId("ctem-gauge-score").dataset.center).toBe("full");
  });

  it("no inner ring: the caption stays at any size", () => {
    renderWithProviders(<PostureGauge score={40} band="critical" size={88} strokeWidth={8} />);
    expect(screen.getByTestId("ctem-gauge-caption")).toBeTruthy();
  });
});
