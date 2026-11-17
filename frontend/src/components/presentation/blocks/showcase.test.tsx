// The showcase frame of an `image` block and the `challenge_answer` block.
// The frame is read through the real stylesheet and the real
// components: a `fill` image is a filler its column stretches for, the window
// publishes the picture's ratio and the room it may take, and an unframed
// image keeps its original DOM. happy-dom lays nothing out, so the room and
// the picture are given their numbers by hand and the cascade is read through
// `getComputedStyle`.

import { readFileSync } from "node:fs";
import { fireEvent } from "@testing-library/react";
import { afterEach, beforeAll, describe, expect, it } from "vitest";

import { renderWithProviders } from "@/testing/providers";
import type { DeckBlock, DeckChallengeAnswerBlock, DeckImageBlock } from "@/types";
import { DeckAssetProvider } from "../asset-context";
import { DECK_ICON_STROKE_WIDTH } from "../DeckIcon";
import { BlockList } from "./index";
import { readShowcaseRoom, SHOWCASE_MIN_ROOM_PX, showcaseRoomStyle } from "./showcase";

const stylesheet = readFileSync(`${__dirname}/../presentation.css`, "utf8");

const FILE_ID = "11111111-2222-4333-8444-555555555555";

const image = (extra: Partial<DeckImageBlock> = {}): DeckImageBlock => ({
  type: "image",
  file_id: FILE_ID,
  alt: "The investigation workspace",
  ...extra,
});

/** The framed screenshot in the wide column, the reading beside it. */
const showcaseSlide = (frame: DeckImageBlock["frame"]): DeckBlock[] => [
  {
    type: "columns",
    ratio: "wide_left",
    columns: [
      [image({ frame, fill: true })],
      [{ type: "list", style: "arrow", items: [{ text: "The timeline" }, { text: "The graph" }] }],
    ],
  },
];

const ROOM = { width: 1000, height: 700 };

/** Every `.deck-showcase-room` measures `room`; everything else 0 (the layout happy-dom never does). */
function measureRoom(room: { width: number; height: number }): void {
  Object.defineProperty(HTMLElement.prototype, "clientWidth", {
    configurable: true,
    get(this: HTMLElement) {
      return this.classList.contains("deck-showcase-room") ? room.width : 0;
    },
  });
  Object.defineProperty(HTMLElement.prototype, "clientHeight", {
    configurable: true,
    get(this: HTMLElement) {
      return this.classList.contains("deck-showcase-room") ? room.height : 0;
    },
  });
}

/** The picture's bytes arrive: a 1600 x 1000 screenshot. */
function loadPicture(img: HTMLImageElement, width = 1600, height = 1000): void {
  Object.defineProperty(img, "naturalWidth", { configurable: true, value: width });
  Object.defineProperty(img, "naturalHeight", { configurable: true, value: height });
  fireEvent.load(img);
}

function renderBlocks(blocks: DeckBlock[]) {
  return renderWithProviders(
    <DeckAssetProvider value={(id) => `blob:${id}`}>
      <div className="deck-root">
        <BlockList blocks={blocks} className="deck-blocks deck-blocks-slide" path="blocks" />
      </div>
    </DeckAssetProvider>,
  );
}

beforeAll(() => {
  const style = document.createElement("style");
  style.textContent = stylesheet;
  document.head.appendChild(style);
});

afterEach(() => {
  Reflect.deleteProperty(HTMLElement.prototype, "clientHeight");
  Reflect.deleteProperty(HTMLElement.prototype, "clientWidth");
});

describe("the room a showcase may take (pure)", () => {
  it("publishes the width always and the height only when it is a room's", () => {
    expect(readShowcaseRoom(1000, 700)).toEqual({ width: 1000, height: 700 });
    expect(readShowcaseRoom(1000.4, SHOWCASE_MIN_ROOM_PX)).toEqual({ width: 1000, height: SHOWCASE_MIN_ROOM_PX });
    // A height under the floor is the room's own content, not a height it was handed.
    expect(readShowcaseRoom(1000, SHOWCASE_MIN_ROOM_PX - 1)).toEqual({ width: 1000, height: null });
    expect(readShowcaseRoom(0, 700)).toBeNull();
  });

  it("writes the variables the stylesheet reads, in canvas px", () => {
    expect(showcaseRoomStyle(null)).toBeUndefined();
    expect(showcaseRoomStyle({ width: 1000, height: 700 })).toEqual({ "--deck-room-w": "1000px", "--deck-room-h": "700px" });
    expect(showcaseRoomStyle({ width: 1000, height: null })).toEqual({ "--deck-room-w": "1000px" });
  });
});

describe("a framed fill image in the wide column", () => {
  it("is a filler its column stretches for, and the window takes the picture's ratio in the room", () => {
    measureRoom(ROOM);
    const { container } = renderBlocks(showcaseSlide("browser"));

    // The run hands the columns block its free height, and the figure's column stretches to the row.
    const columns = container.querySelector<HTMLElement>(".deck-blocks-slide > .deck-columns") as HTMLElement;
    expect(getComputedStyle(columns).flexGrow).toBe("1");
    // (The text column beside it hugs its content on a live deck - read by
    // `tests_e2e/presentation-showcase.spec.ts`: happy-dom matches a `:has()`
    // child chain on its prefix, so the negative cannot be read here.)
    const [wide] = Array.from(container.querySelectorAll<HTMLElement>(".deck-column"));
    expect(getComputedStyle(wide).alignSelf).toBe("stretch");

    // Inside: the figure grows in the run, the room grows in the figure.
    const figure = container.querySelector<HTMLElement>(".deck-figure") as HTMLElement;
    expect(figure.dataset.frame).toBe("browser");
    expect(figure.dataset.fill).toBe("true");
    expect(getComputedStyle(figure).flexGrow).toBe("1");
    const room = figure.querySelector<HTMLElement>(":scope > .deck-showcase-room") as HTMLElement;
    expect(getComputedStyle(room).flexGrow).toBe("1");

    // The chrome: a glow captured alone by the export, a bar of three dots and an address pill, all decoration.
    const showcase = room.querySelector<HTMLElement>(":scope > .deck-showcase") as HTMLElement;
    const glow = showcase.querySelector<HTMLElement>(":scope > .deck-showcase-glow") as HTMLElement;
    expect(glow.getAttribute("aria-hidden")).toBe("true");
    expect(glow.dataset.exportBackdrop).toBe("true");
    // Painted through a background (a picture in the editable PPTX), never a
    // shadow or a pseudo-element the exporter drops; happy-dom computes no
    // gradient, so the rule is read off the stylesheet.
    const glowRule = stylesheet.slice(stylesheet.indexOf(".deck-root .deck-showcase-glow {"));
    expect(glowRule.slice(0, glowRule.indexOf("}"))).toMatch(/background-image:\s*radial-gradient\(/);
    expect(glowRule.slice(0, glowRule.indexOf("}"))).not.toContain("box-shadow");
    const bar = showcase.querySelector<HTMLElement>(".deck-showcase-window > .deck-showcase-bar") as HTMLElement;
    expect(bar.getAttribute("aria-hidden")).toBe("true");
    expect(bar.querySelectorAll(".deck-showcase-dot")).toHaveLength(3);
    expect(bar.querySelectorAll(".deck-showcase-address")).toHaveLength(1);

    // The room was measured before the picture arrived; the ratio lands with its bytes.
    expect(showcase.style.getPropertyValue("--deck-room-w")).toBe("1000px");
    expect(showcase.style.getPropertyValue("--deck-room-h")).toBe("700px");
    expect(showcase.dataset.sized).toBeUndefined();
    const img = showcase.querySelector<HTMLImageElement>(".deck-showcase-window > img.deck-image") as HTMLImageElement;
    expect(img.dataset.deckPending).toBe("true");
    loadPicture(img);
    expect(img.dataset.deckPending).toBeUndefined();
    expect(showcase.dataset.sized).toBe("true");
    expect(showcase.style.getPropertyValue("--deck-image-ratio")).toBe("1.6");
    // The picture takes the window's width and its own height: the window hugs it.
    const picture = getComputedStyle(img);
    expect(picture.width).toBe("100%");
    expect(picture.height).toBe("auto");
    expect(picture.maxHeight).toBe("none");
  });

  it("draws the device frame with a camera dot on a bezel", () => {
    measureRoom(ROOM);
    const { container } = renderBlocks(showcaseSlide("device"));
    const figure = container.querySelector<HTMLElement>(".deck-figure") as HTMLElement;
    expect(figure.dataset.frame).toBe("device");
    const bar = figure.querySelector<HTMLElement>(".deck-showcase-bar") as HTMLElement;
    expect(bar.querySelectorAll(".deck-showcase-camera")).toHaveLength(1);
    expect(bar.querySelectorAll(".deck-showcase-dot")).toHaveLength(0);
  });

  it("draws the slim browser frame with three dots and no address pill", () => {
    measureRoom(ROOM);
    const { container } = renderBlocks(showcaseSlide("browser_slim"));
    const figure = container.querySelector<HTMLElement>(".deck-figure") as HTMLElement;
    expect(figure.dataset.frame).toBe("browser_slim");
    const bar = figure.querySelector<HTMLElement>(".deck-showcase-window > .deck-showcase-bar") as HTMLElement;
    expect(bar.getAttribute("aria-hidden")).toBe("true");
    expect(bar.querySelectorAll(".deck-showcase-dot")).toHaveLength(3);
    expect(bar.querySelectorAll(".deck-showcase-address")).toHaveLength(0);
    expect(figure.querySelector(".deck-showcase-glow")).not.toBeNull();
  });

  it("publishes the width alone when the room has no height to give", () => {
    measureRoom({ width: 640, height: 40 });
    const { container } = renderBlocks([image({ frame: "browser", fill: true })]);
    const showcase = container.querySelector<HTMLElement>(".deck-showcase") as HTMLElement;
    expect(showcase.style.getPropertyValue("--deck-room-w")).toBe("640px");
    expect(showcase.style.getPropertyValue("--deck-room-h")).toBe("");
  });

  it("measures no room for a framed image that does not fill: the bucket sizes it", () => {
    measureRoom(ROOM);
    const { container } = renderBlocks([image({ frame: "browser", size: "lg" })]);
    const figure = container.querySelector<HTMLElement>(".deck-figure") as HTMLElement;
    expect(figure.dataset.fill).toBeUndefined();
    const showcase = figure.querySelector<HTMLElement>(".deck-showcase") as HTMLElement;
    expect(showcase.style.getPropertyValue("--deck-room-w")).toBe("");
    expect(getComputedStyle(figure.querySelector(".deck-showcase-room") as HTMLElement).flexGrow).not.toBe("1");
  });

  it("leaves an unframed image exactly as it was", () => {
    measureRoom(ROOM);
    const { container } = renderBlocks([image({ fill: true })]);
    const figure = container.querySelector<HTMLElement>(".deck-figure") as HTMLElement;
    expect(figure.dataset.frame).toBeUndefined();
    expect(figure.querySelector(".deck-showcase")).toBeNull();
    expect(figure.querySelector(".deck-showcase-room")).toBeNull();
    const img = figure.querySelector<HTMLImageElement>(":scope > img.deck-image") as HTMLImageElement;
    expect(img).not.toBeNull();
    // Its own column rule still stretches it, like a chart's.
    const { container: inColumn } = renderBlocks([
      { type: "columns", columns: [[image({ fill: true })], [{ type: "text", content: "Beside it." }]] },
    ]);
    const [wide] = Array.from(inColumn.querySelectorAll<HTMLElement>(".deck-column"));
    expect(getComputedStyle(wide).alignSelf).toBe("stretch");
  });
});

describe("the challenge_answer block", () => {
  const block: DeckChallengeAnswerBlock = {
    type: "challenge_answer",
    icon: "layers",
    challenge: "Every incident is correlated **by hand**.",
    answer: "One case model for every source.",
  };

  it("stacks the challenge over the answer, error over success, in one grid, with the arrow between", () => {
    const { container, getByText } = renderBlocks([block]);
    const root = container.querySelector<HTMLElement>(".deck-challenge") as HTMLElement;
    expect(getComputedStyle(root).display).toBe("grid");
    expect(root.dataset.fill).toBeUndefined();
    const cards = Array.from(root.querySelectorAll<HTMLElement>(":scope > .deck-challenge-card"));
    expect(cards.map((card) => card.dataset.role)).toEqual(["challenge", "answer"]);
    expect(cards.map((card) => card.dataset.tone)).toEqual(["error", "success"]);
    // The connector is a row of its own between the two, and decoration.
    expect(cards[0].nextElementSibling?.classList.contains("deck-challenge-arrow")).toBe(true);
    expect(cards[0].nextElementSibling?.getAttribute("aria-hidden")).toBe("true");
    expect(cards[0].nextElementSibling?.nextElementSibling).toBe(cards[1]);
    // The model's default eyebrows, the block's icon on the challenge.
    expect(getByText("Challenge")).toBeTruthy();
    expect(getByText("Answer")).toBeTruthy();
    expect(getByText("by hand").tagName).toBe("STRONG");
    expect(cards[0].querySelectorAll(".deck-challenge-label .deck-icon, .deck-challenge-label .deck-icon-pending")).toHaveLength(1);
  });

  it("joins the cards with one stroke from edge to edge, in the arrow-down icon's stroke and head", () => {
    const { container } = renderBlocks([block]);
    const arrow = container.querySelector(".deck-challenge > .deck-challenge-arrow") as SVGSVGElement;
    expect(arrow.tagName.toLowerCase()).toBe("svg");
    // As tall as the gap the row sets between the cards, the icon's 32 px wide,
    // on the icon's 24-unit grid scaled the same both ways.
    expect(arrow.getAttribute("width")).toBe("32");
    expect(arrow.getAttribute("height")).toBe("66");
    const [, , gridWidth, gridHeight] = (arrow.getAttribute("viewBox") ?? "").split(" ").map(Number);
    expect(gridWidth).toBe(24);
    expect(gridHeight / gridWidth).toBeCloseTo(66 / 32);
    // One stroke and one head, nothing else: no second line under an icon.
    expect(arrow.querySelectorAll("line, polyline, path, rect, svg")).toHaveLength(2);
    const group = arrow.querySelector("g") as SVGGElement;
    expect(group.getAttribute("stroke")).toBe("currentColor");
    expect(group.getAttribute("stroke-width")).toBe(String(DECK_ICON_STROKE_WIDTH));
    expect(group.getAttribute("stroke-linejoin")).toBe("round");
    const shaft = arrow.querySelector("line") as SVGLineElement;
    const head = (arrow.querySelector("polyline") as SVGPolylineElement)
      .getAttribute("points")
      ?.split(" ")
      .map((point) => point.split(",").map(Number)) as number[][];
    const [left, tip, right] = head;
    // From the challenge card's edge, flush, into the tip.
    expect(Number(shaft.getAttribute("y1"))).toBe(0);
    expect(shaft.getAttribute("stroke-linecap")).toBe("butt");
    expect([Number(shaft.getAttribute("x2")), Number(shaft.getAttribute("y2"))]).toEqual(tip);
    expect(tip[0]).toBe(gridWidth / 2);
    // The tip's round join just touches the answer card.
    expect(tip[1] + DECK_ICON_STROKE_WIDTH / 2).toBeCloseTo(gridHeight);
    // The head of `arrow-down`: two arms of 7 units each way.
    expect(left).toEqual([tip[0] - 7, tip[1] - 7]);
    expect(right).toEqual([tip[0] + 7, tip[1] - 7]);
    // Touching both cards, the PPTX captures it alone.
    expect(arrow.getAttribute("data-export-alone")).toBe("true");
  });

  it("takes its own eyebrows and shares the height with fill", () => {
    const { container, getByText, queryByText } = renderBlocks([
      { ...block, challenge_label: "The problem", answer_label: "Our answer", fill: true },
    ]);
    const root = container.querySelector<HTMLElement>(".deck-challenge") as HTMLElement;
    expect(root.dataset.fill).toBe("true");
    expect(getComputedStyle(root).flexGrow).toBe("1");
    expect(getByText("The problem")).toBeTruthy();
    expect(getByText("Our answer")).toBeTruthy();
    expect(queryByText("Challenge")).toBeNull();
  });
});
