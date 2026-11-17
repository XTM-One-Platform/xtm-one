/**
 * Present to a meeting (xtm-one #10552, slice A2): click to advance ignores
 * interactive content, the audience window is opened once and brought back
 * without a reload, and the audience stage mirrors the deck's other stages.
 */
import { afterEach, describe, expect, it, vi } from "vitest";

import { clickAdvanceStep, DECK_KEY_CODES } from "./deck-keys";
import { mirrorsMove } from "./speaker-sync";
import {
  audienceFeaturesForScreens,
  audienceFeaturesOnOtherScreen,
  audienceViewUrl,
  audienceWindowName,
  openAudienceWindow,
} from "./speaker-url";

function click(target: Element, init: Partial<MouseEvent> = {}) {
  return {
    button: 0,
    shiftKey: false,
    ctrlKey: false,
    metaKey: false,
    altKey: false,
    defaultPrevented: false,
    target,
    ...init,
  } as Pick<MouseEvent, "button" | "shiftKey" | "ctrlKey" | "metaKey" | "altKey" | "target" | "defaultPrevented">;
}

function inSlide(html: (slide: HTMLElement) => Element): Element {
  const slide = document.createElement("section");
  document.body.appendChild(slide);
  return html(slide);
}

afterEach(() => {
  document.body.replaceChildren();
  vi.restoreAllMocks();
});

describe("clickAdvanceStep", () => {
  it("advances on a plain left click on the slide and goes back with Shift", () => {
    const text = inSlide((slide) => slide.appendChild(document.createElement("p")));
    expect(clickAdvanceStep(click(text))).toBe("next");
    expect(clickAdvanceStep(click(text, { shiftKey: true }))).toBe("prev");
  });

  it.each(["a", "button", "input", "textarea", "video", "iframe", "label"])("ignores a click on <%s>", (tag) => {
    const element = inSlide((slide) => {
      const el = document.createElement(tag);
      if (tag === "a") el.setAttribute("href", "https://example.com");
      return slide.appendChild(el);
    });
    expect(clickAdvanceStep(click(element))).toBeNull();
  });

  it("ignores a click inside a button (a poll control's icon)", () => {
    const icon = inSlide((slide) => {
      const button = slide.appendChild(document.createElement("button"));
      return button.appendChild(document.createElement("span"));
    });
    expect(clickAdvanceStep(click(icon))).toBeNull();
  });

  it("ignores the editor and owned regions", () => {
    const inEditor = inSlide((slide) => {
      const editor = slide.appendChild(document.createElement("div"));
      editor.setAttribute("data-deck-keys", "off");
      return editor.appendChild(document.createElement("p"));
    });
    expect(clickAdvanceStep(click(inEditor))).toBeNull();
  });

  it("ignores other buttons, modifiers and handled events", () => {
    const text = inSlide((slide) => slide.appendChild(document.createElement("p")));
    expect(clickAdvanceStep(click(text, { button: 2 }))).toBeNull();
    expect(clickAdvanceStep(click(text, { ctrlKey: true }))).toBeNull();
    expect(clickAdvanceStep(click(text, { metaKey: true }))).toBeNull();
    expect(clickAdvanceStep(click(text, { altKey: true }))).toBeNull();
    expect(clickAdvanceStep(click(text, { defaultPrevented: true }))).toBeNull();
  });
});

describe("mirrorsMove", () => {
  it("lets the audience window follow every stage and every stage follow it", () => {
    expect(mirrorsMove("audience", "stage")).toBe(true);
    expect(mirrorsMove("audience", undefined)).toBe(true);
    expect(mirrorsMove("stage", "audience")).toBe(true);
  });

  it("keeps two ordinary stages of one deck independent", () => {
    expect(mirrorsMove("stage", "stage")).toBe(false);
    expect(mirrorsMove("stage", undefined)).toBe(false);
  });
});

describe("openAudienceWindow", () => {
  it("points a fresh window at the audience route, by the deck's window name", () => {
    const opened = { location: { pathname: "/blank", href: "" }, focus: vi.fn() };
    const open = vi.spyOn(window, "open").mockReturnValue(opened as unknown as Window);
    expect(openAudienceWindow("deck-1")).toBe(true);
    expect(open).toHaveBeenCalledWith("", audienceWindowName("deck-1"), expect.stringContaining("popup=yes"));
    expect(opened.location.href).toContain(audienceViewUrl("deck-1"));
    expect(opened.focus).toHaveBeenCalledOnce();
  });

  it("brings back an open audience window without reloading it", () => {
    const opened = { location: { pathname: audienceViewUrl("deck-2"), href: "kept" }, focus: vi.fn() };
    vi.spyOn(window, "open").mockReturnValue(opened as unknown as Window);
    expect(openAudienceWindow("deck-2")).toBe(true);
    expect(opened.location.href).toBe("kept");
    expect(opened.focus).toHaveBeenCalledOnce();
  });

  it("reports a blocked pop-up", () => {
    vi.spyOn(window, "open").mockReturnValue(null);
    expect(openAudienceWindow("deck-3")).toBe(false);
  });
});

describe("audience window on the other screen (slice A3)", () => {
  const laptop = { availLeft: 0, availTop: 0, availWidth: 1440, availHeight: 900 };
  const tv = { availLeft: 1440, availTop: 0, availWidth: 1920, availHeight: 1080 };

  it("places the window over the whole of a screen the speaker view is not on", () => {
    const features = audienceFeaturesForScreens(laptop, [laptop, tv]);
    expect(features).toContain("left=1440");
    expect(features).toContain("width=1920");
    expect(features).toContain("height=1080");
    expect(features).toContain("popup=yes");
  });

  it("keeps the plain window on one screen", () => {
    expect(audienceFeaturesForScreens(laptop, [laptop])).toBeNull();
  });

  it("asks nothing without an extended desktop or the Window Management API", async () => {
    expect(await audienceFeaturesOnOtherScreen()).toBeNull();
  });

  it("opens the audience window with the placement it is given", () => {
    const opened = { location: { pathname: "/blank", href: "" }, focus: vi.fn() };
    const open = vi.spyOn(window, "open").mockReturnValue(opened as unknown as Window);
    openAudienceWindow("deck-4", "popup=yes,left=1440");
    expect(open).toHaveBeenCalledWith("", audienceWindowName("deck-4"), "popup=yes,left=1440");
  });
});

describe("deck keys", () => {
  it("binds A to the audience window, apart from the speaker view's S", () => {
    expect(DECK_KEY_CODES.A).toBe(65);
    expect(DECK_KEY_CODES.A).not.toBe(DECK_KEY_CODES.S);
  });
});
