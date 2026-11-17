/**
 * A slide link as a reader meets it: on a deck host a click moves the deck to
 * the target slide and leaves the address bar alone; while the deck is edited
 * it goes nowhere; on the export page it is a plain anchor to the slide's
 * canvas; a target that is no slide of the deck is its plain label.
 */
import { describe, expect, it, vi } from "vitest";
import { fireEvent, screen } from "@testing-library/react";

import { renderWithProviders } from "@/testing/providers";

import { EditModeContext, type SlideEditSession } from "./edit-context";
import { InlineMarkup } from "./inline-markup";
import { SlideLinkContext, type SlideLinks, slideIndexer } from "./slide-link-context";

const SLIDES = [{ id: "intro" }, { id: "q4-gw" }];

function renderLink(value: string, links: SlideLinks | null, edit: SlideEditSession | null = null) {
  return renderWithProviders(
    <EditModeContext.Provider value={edit}>
      <SlideLinkContext.Provider value={links}>
        <p>
          <InlineMarkup value={value} />
        </p>
      </SlideLinkContext.Provider>
    </EditModeContext.Provider>,
  );
}

describe("slide links in a deck", () => {
  it("moves the deck to the target slide and keeps the click from the address bar", () => {
    const follow = vi.fn();
    renderLink("See [Gateway](#/q4-gw)", { indexOf: slideIndexer(SLIDES), follow });

    const link = screen.getByRole("link", { name: "Gateway" });
    expect(link.getAttribute("href")).toBe("#/q4-gw");
    expect(link.getAttribute("title")).toBe("Go to slide 2");
    const notPrevented = fireEvent.click(link);

    expect(follow).toHaveBeenCalledTimes(1);
    expect(follow).toHaveBeenCalledWith(1);
    expect(notPrevented).toBe(false);
  });

  it("goes nowhere while the deck is edited", () => {
    const follow = vi.fn();
    const edit: SlideEditSession = {
      enabled: true,
      editingText: null,
      onLiveText: () => {},
      onCommitText: () => {},
      onCancelText: () => {},
    };
    renderLink("[Gateway](#/q4-gw)", { indexOf: slideIndexer(SLIDES), follow }, edit);

    const notPrevented = fireEvent.click(screen.getByRole("link", { name: "Gateway" }));

    expect(follow).not.toHaveBeenCalled();
    expect(notPrevented).toBe(false);
  });

  it("is a native anchor to the slide's canvas on the export page", () => {
    renderLink("[Gateway](#/q4-gw)", { indexOf: slideIndexer(SLIDES), href: (index) => `#deck-slide-${index}` });

    const link = screen.getByRole("link", { name: "Gateway" });
    expect(link.getAttribute("href")).toBe("#deck-slide-1");
    expect(link.getAttribute("data-slide-link-index")).toBe("1");
    expect(fireEvent.click(link)).toBe(true);
  });

  it("opens a web link in a new tab with no opener and no referrer, and never while editing", () => {
    renderLink("PDF ([issue 321](https://github.com/x/y/issues/321))", { indexOf: slideIndexer(SLIDES), follow: vi.fn() });

    const link = screen.getByRole("link", { name: "issue 321" });
    expect(link.getAttribute("href")).toBe("https://github.com/x/y/issues/321");
    expect(link.getAttribute("target")).toBe("_blank");
    expect(link.getAttribute("rel")).toBe("noopener noreferrer");
    expect(link.getAttribute("data-link-href")).toBe("https://github.com/x/y/issues/321");
  });

  it("keeps a web link from leaving the page while the deck is edited", () => {
    const edit: SlideEditSession = {
      enabled: true,
      editingText: null,
      onLiveText: () => {},
      onCommitText: () => {},
      onCancelText: () => {},
    };
    renderLink("[issue 321](https://github.com/x/y/issues/321)", { indexOf: slideIndexer(SLIDES) }, edit);

    expect(fireEvent.click(screen.getByRole("link", { name: "issue 321" }))).toBe(false);
  });

  it("draws a link to no slide of the deck as its plain label", () => {
    renderLink("See [Gateway](#/gone)", { indexOf: slideIndexer(SLIDES), follow: vi.fn() });

    expect(screen.queryByRole("link")).toBeNull();
    expect(screen.getByText("See Gateway")).toBeTruthy();
  });

  it("is drawn but inert where nothing hosts the deck (a thumbnail)", () => {
    renderLink("[Gateway](#/q4-gw)", null);

    expect(screen.queryByRole("link")).toBeNull();
    expect(screen.getByText("Gateway")).toBeTruthy();
  });
});
