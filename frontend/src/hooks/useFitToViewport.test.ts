import { describe, expect, it } from "vitest";
import { FIT_FLOOR_PX, fitRoom, stickyBarBottom, stickyShift } from "./useFitToViewport";

// One scrollbar per list page (#11233): the room left to the table's scroll area.
describe("fitRoom", () => {
  it("is the window height minus what sits above and below the area", () => {
    // 900 px window, area top at 260 px, pager and main padding 88 px under it.
    expect(fitRoom({ viewport: 900, top: 260, below: 88 })).toBe(552);
  });

  it("never lets a negative below grow the room past the window", () => {
    expect(fitRoom({ viewport: 720, top: 200, below: -40 })).toBe(520);
  });

  it("drops the bound on a long-form page, so the page scrolls alone", () => {
    // A table under a long header: the room left is under the floor.
    expect(fitRoom({ viewport: 720, top: 600, below: 60 })).toBeNull();
    expect(fitRoom({ viewport: 720, top: 720 - 60 - FIT_FLOOR_PX + 1, below: 60 })).toBeNull();
    expect(fitRoom({ viewport: 720, top: 720 - 60 - FIT_FLOOR_PX, below: 60 })).toBe(FIT_FLOOR_PX);
  });

  it("rounds down so the page never overflows by a fraction of a pixel", () => {
    expect(fitRoom({ viewport: 900, top: 260.4, below: 88.3 })).toBe(551);
  });

  it("keeps a list under a tall header bounded down to the header and three rows", () => {
    expect(FIT_FLOOR_PX).toBe(160);
    expect(fitRoom({ viewport: 720, top: 480, below: 60 })).toBe(180);
  });
});

// Long-form page (#11241 UX round 1, B1): the header follows the page under the top bar.
describe("stickyShift", () => {
  it("does not move the header while the table's top is below the top bar", () => {
    expect(stickyShift(68, 300, 1200, 40)).toBe(0);
  });

  it("moves the header under the top bar once the table's top scrolled past it", () => {
    expect(stickyShift(68, -596, 2000, 40)).toBe(664);
  });

  it("never moves the header past the table's last row", () => {
    expect(stickyShift(68, -2000, 1200, 40)).toBe(1160);
  });

  // #12271 UX r3 B1: the horizontal SectionIndex sticks at 68-117 under the top bar.
  it("stops the header under a sticky page bar below the top bar", () => {
    const barBottom = stickyBarBottom([68, 117]);
    expect(barBottom).toBe(117);
    expect(stickyShift(barBottom, -596, 2000, 40)).toBe(713);
    expect(stickyShift(barBottom, 100, 2000, 40)).toBe(17);
  });

  it("keeps the top bar's edge when no page bar is there", () => {
    expect(stickyBarBottom([68])).toBe(68);
    expect(stickyBarBottom([])).toBe(0);
  });
});
