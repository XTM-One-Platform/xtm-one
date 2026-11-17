/** The `video` block (xtm-one #11686): the player on screen, the poster in the static render. */
import { readFileSync } from "node:fs";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { renderWithProviders } from "@/testing/providers";
import type { DeckVideoBlock } from "@/types";

import { DeckAssetProvider } from "../asset-context";
import { VideoBlockView, videoFragment } from "./media";

const resolve = (fileId: string) => `https://assets.test/${fileId}`;
const stylesheet = readFileSync(`${__dirname}/../presentation.css`, "utf8");

/** The declarations of the first rule whose selector is exactly `selector`. */
function rule(selector: string): string {
  const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  return new RegExp(`(?:^|\\n)${escaped}\\s*\\{([^}]*)\\}`).exec(stylesheet)?.[1] ?? "";
}

function renderVideo(block: DeckVideoBlock, { isStatic = false } = {}) {
  return renderWithProviders(
    <DeckAssetProvider value={resolve}>
      <div className="deck-root" data-deck-static={isStatic ? "true" : undefined}>
        <VideoBlockView block={block} />
      </div>
    </DeckAssetProvider>,
  );
}

describe("videoFragment", () => {
  it("is empty without a window", () => {
    expect(videoFragment(null, undefined)).toBe("");
    expect(videoFragment(0, 0)).toBe("");
  });

  it("clips the start and the end", () => {
    expect(videoFragment(5, null)).toBe("#t=5");
    expect(videoFragment(null, 30)).toBe("#t=0,30");
    expect(videoFragment(5, 30)).toBe("#t=5,30");
  });
});

/** An observer that reports the block fully on screen, as the active slide is. */
class OnScreenObserver {
  constructor(private readonly callback: IntersectionObserverCallback) {}
  observe(target: Element) {
    const entry = { isIntersecting: true, intersectionRatio: 1, target } as unknown as IntersectionObserverEntry;
    this.callback([entry], this as unknown as IntersectionObserver);
  }
  disconnect() {}
  unobserve() {}
  takeRecords() {
    return [];
  }
}

describe("VideoBlockView", () => {
  beforeEach(() => {
    vi.stubGlobal("IntersectionObserver", OnScreenObserver);
    vi.spyOn(HTMLMediaElement.prototype, "play").mockResolvedValue(undefined);
    vi.spyOn(HTMLMediaElement.prototype, "pause").mockReturnValue(undefined);
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("plays the file with its poster, captions and controls", () => {
    const { container } = renderVideo({
      type: "video",
      file_id: "movie",
      poster_file_id: "poster",
      captions_file_id: "captions",
      start: 2,
      alt: "Product tour",
    });
    const video = container.querySelector("video");
    expect(video).not.toBeNull();
    expect(video?.getAttribute("src")).toBe("https://assets.test/movie#t=2");
    expect(video?.getAttribute("poster")).toBe("https://assets.test/poster");
    expect(video?.hasAttribute("controls")).toBe(true);
    expect(video?.muted).toBe(false);
    expect(video?.getAttribute("aria-label")).toBe("Product tour");
    expect(container.querySelector("track")?.getAttribute("src")).toBe("https://assets.test/captions");
  });

  it("mutes an autoplaying video unless told otherwise", () => {
    const muted = renderVideo({ type: "video", file_id: "movie", autoplay: true });
    expect(muted.container.querySelector("video")?.muted).toBe(true);
    muted.unmount();
    const loud = renderVideo({ type: "video", file_id: "movie", autoplay: true, muted: false });
    expect(loud.container.querySelector("video")?.muted).toBe(false);
  });

  it("leaves the captions off when the block turns them off", () => {
    const { container } = renderVideo({ type: "video", file_id: "movie", captions_file_id: "captions", captions: false });
    expect(container.querySelector("track")).toBeNull();
  });

  it("draws the poster with a play badge and a link in the static render", () => {
    const { container } = renderVideo(
      { type: "video", video_id: "video-1", file_id: "movie", poster_file_id: "poster" },
      { isStatic: true },
    );
    expect(container.querySelector("video")).toBeNull();
    const poster = container.querySelector(".deck-video-poster");
    expect(poster?.getAttribute("data-export-video")).toBe("movie");
    expect(container.querySelector(".deck-video-badge")).not.toBeNull();
    expect(container.querySelector("a.deck-video-link")?.getAttribute("data-link-href")).toMatch(/\/videos\/video-1$/);
  });

  it("keeps a 16:9 placeholder under the badge when the static render has no poster", () => {
    const { container } = renderVideo({ type: "video", video_id: "video-1", file_id: "movie" }, { isStatic: true });
    const poster = container.querySelector(".deck-video-poster");
    expect(poster?.querySelector(".deck-video-empty")).not.toBeNull();
    expect(poster?.querySelector(".deck-video-badge")).not.toBeNull();
    // The poster box spans its cell, so the placeholder's aspect ratio gives it a height (PDF page with no poster).
    expect(rule(".deck-root .deck-video-poster")).toMatch(/(^|;)\s*width:\s*100%/);
    expect(rule(".deck-root .deck-video-empty")).toMatch(/aspect-ratio:\s*16\s*\/\s*9/);
  });

  it("plays a YouTube link in the privacy-enhanced player while on screen", () => {
    const { container } = renderVideo({ type: "video", url: "https://youtu.be/dQw4w9WgXcQ", alt: "Launch" });
    expect(container.querySelector("video")).toBeNull();
    const frame = container.querySelector("iframe");
    expect(frame?.getAttribute("src")).toMatch(/^https:\/\/www\.youtube-nocookie\.com\/embed\/dQw4w9WgXcQ\?/);
    expect(frame?.getAttribute("title")).toBe("Launch");
  });

  it("keeps the player and its placeholder at 16:9 within the size bucket's height, never a full-width band", () => {
    for (const selector of [".deck-root .deck-video-embed", ".deck-root .deck-video-empty"]) {
      const body = rule(selector);
      expect(body).toMatch(/width:\s*min\(100%,\s*calc\(var\(--deck-image-h,\s*520px\)\s*\*\s*16\s*\/\s*9\)\)/);
      expect(body).toMatch(/height:\s*auto/);
      expect(body).toMatch(/aspect-ratio:\s*16\s*\/\s*9/);
    }
  });

  it("fits a full or fill player inside its room's height as well as its width", () => {
    for (const block of [
      { type: "video", url: "https://vimeo.com/76979871", size: "full" },
      { type: "video", url: "https://vimeo.com/76979871", fill: true },
    ] as DeckVideoBlock[]) {
      const { container, unmount } = renderVideo(block);
      expect(container.querySelector(".deck-video-room > iframe.deck-video-embed")).not.toBeNull();
      unmount();
    }
    const { container } = renderVideo({ type: "video", url: "https://vimeo.com/76979871", size: "md" });
    expect(container.querySelector(".deck-video-room")).toBeNull();
    const body = rule(
      '.deck-root .deck-figure[data-kind="video"]:not([data-frame]) .deck-video-room > .deck-image.deck-video.deck-video-embed',
    );
    expect(body).toMatch(/width:\s*min\(var\(--deck-room-w,\s*100%\),\s*calc\(var\(--deck-room-h,[^;]*\)\s*\*\s*16\s*\/\s*9\)\)/);
    expect(body).toMatch(/height:\s*auto/);
    expect(body).toMatch(/max-height:\s*none/);
  });

  it("fits a full or fill player link's static poster and placeholder inside the room, as the player", () => {
    for (const block of [
      { type: "video", url: "https://vimeo.com/76979871", size: "full", poster_file_id: "poster" },
      { type: "video", url: "https://vimeo.com/76979871", fill: true },
    ] as DeckVideoBlock[]) {
      const { container, unmount } = renderVideo(block, { isStatic: true });
      expect(container.querySelector("iframe")).toBeNull();
      expect(container.querySelector(".deck-video-room > .deck-video-poster")).not.toBeNull();
      unmount();
    }
    const box = rule('.deck-root .deck-figure[data-kind="video"]:not([data-frame]) .deck-video-room > .deck-video-poster');
    expect(box).toMatch(/width:\s*min\(var\(--deck-room-w,\s*100%\),\s*calc\(var\(--deck-room-h,[^;]*\)\s*\*\s*16\s*\/\s*9\)\)/);
    expect(box).toMatch(/aspect-ratio:\s*16\s*\/\s*9/);
    expect(box).toMatch(/min-height:\s*0/);
    const picture = rule(
      '.deck-root .deck-figure[data-kind="video"]:not([data-frame]) .deck-video-room > .deck-video-poster > .deck-image',
    );
    expect(picture).toMatch(/height:\s*100%/);
    expect(picture).toMatch(/max-height:\s*none/);
  });

  it("draws a player link's poster with a link to its page in the static render", () => {
    const { container } = renderVideo(
      { type: "video", url: "https://vimeo.com/76979871", poster_file_id: "poster" },
      { isStatic: true },
    );
    expect(container.querySelector("iframe")).toBeNull();
    expect(container.querySelector("a.deck-video-link")?.getAttribute("data-link-href")).toBe("https://vimeo.com/76979871");
  });

  it("plays an imported MP4 link as a platform file, never as a frame", () => {
    const { container } = renderVideo({ type: "video", url: "https://cdn.test/tour.mp4", file_id: "movie" });
    expect(container.querySelector("iframe")).toBeNull();
    expect(container.querySelector("video")?.getAttribute("src")).toBe("https://assets.test/movie");
  });

  it("draws the player and its badge in deck colours, as the image block", () => {
    expect(rule(".deck-root .deck-video")).toContain("var(--deck-surface)");
    const badge = stylesheet.slice(stylesheet.indexOf(".deck-root .deck-video-badge {"));
    const declarations = badge.slice(0, badge.indexOf("}"));
    expect(declarations).not.toMatch(/#[0-9a-f]{3,6}\b|rgba?\(/i);
  });
});
