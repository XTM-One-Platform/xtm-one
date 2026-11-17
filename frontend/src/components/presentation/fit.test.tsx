// The shrink-to-fit pass (`fit.ts`) re-runs when what the body is drawn with
// lands after the first pass - an image loading, the deck fonts - even though
// no observed box changes size: a body fitted before the fonts landed stayed
// shrunk with its `fill` tables short of the floor, while the same slide
// reached by navigation rendered unfitted (#10602). The pass measures with the
// fitted floor lifted (`data-fit-measuring`), so `fill` growth never decides
// that a body overflows.
//
// happy-dom lays nothing out: the host's room and the content's height are
// stubbed, which is what the pass reads.

import { readFileSync } from "node:fs";

import { act, render } from "@testing-library/react";
import { useRef } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { FIT_MEASURING_ATTR, useFitToHeight } from "./fit";

const ROOM = 800;

/** The content's natural height, and how many reads the pass made with the fitted floor lifted. */
const state = { natural: 0, measuredReads: 0 };

function Body() {
  const hostRef = useRef<HTMLDivElement>(null);
  const contentRef = useRef<HTMLDivElement>(null);
  useFitToHeight(hostRef, contentRef);
  return (
    <div className="deck-fit" data-testid="fit-host" ref={hostRef}>
      <div className="deck-fit-content" data-testid="fit-content" ref={contentRef}>
        <img alt="" src="data:," />
      </div>
    </div>
  );
}

/** The fit host of a rendered body. */
function hostOf(container: HTMLElement): HTMLElement {
  const host = container.querySelector<HTMLElement>(".deck-fit");
  if (!host) throw new Error("no .deck-fit host");
  return host;
}

const nextFrame = () => new Promise<void>((resolve) => window.requestAnimationFrame(() => resolve()));

describe("the shrink-to-fit pass", () => {
  beforeEach(() => {
    state.natural = 0;
    state.measuredReads = 0;
    vi.spyOn(HTMLElement.prototype, "clientHeight", "get").mockImplementation(function (this: HTMLElement) {
      return this.dataset.testid === "fit-host" ? ROOM : 0;
    });
    vi.spyOn(HTMLElement.prototype, "offsetHeight", "get").mockImplementation(function (this: HTMLElement) {
      if (this.dataset.testid !== "fit-content") return 0;
      if (this.parentElement?.hasAttribute(FIT_MEASURING_ATTR)) state.measuredReads += 1;
      return state.natural;
    });
    if (typeof ResizeObserver === "undefined") {
      vi.stubGlobal(
        "ResizeObserver",
        class {
          observe() {}
          disconnect() {}
        },
      );
    }
  });

  afterEach(() => {
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it("lets go of a ratio once an image that loads late makes the body fit", async () => {
    state.natural = 880;
    const { container } = render(<Body />);
    const host = hostOf(container);
    expect(host.dataset.fit).toBe("0.909");

    // The image lands and the content is shorter than the room: no observed box changes size.
    state.natural = 700;
    await act(async () => {
      container.querySelector("img")?.dispatchEvent(new Event("load"));
      await nextFrame();
      await nextFrame();
    });
    expect(host.dataset.fit).toBeUndefined();
    expect(host.hasAttribute(FIT_MEASURING_ATTR)).toBe(false);
  });

  it("measures with the fitted floor lifted and leaves it set afterwards", () => {
    state.natural = 1000;
    const { container } = render(<Body />);
    const host = hostOf(container);
    expect(host.dataset.fit).toBe("0.800");
    // The search read the content with the fitted floor lifted, at its minimum height.
    expect(state.measuredReads).toBeGreaterThan(0);
    expect(host.hasAttribute(FIT_MEASURING_ATTR)).toBe(false);
  });

  it("is floored by the stylesheet at the room divided by the ratio, outside a pass", () => {
    const stylesheet = readFileSync(`${__dirname}/presentation.css`, "utf8");
    expect(stylesheet).toMatch(
      /\.deck-fit\[data-fit\]:not\(\[data-fit-measuring\]\) > \.deck-fit-content \{\s*min-height: calc\(100% \/ var\(--deck-fit, 1\)\);/,
    );
  });
});
