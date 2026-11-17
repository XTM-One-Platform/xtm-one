/** The external players of the deck `video` block (xtm-one #11701): the allow-list and the player address. */
import { describe, expect, it } from "vitest";

import { videoEmbed, videoEmbedPage, videoEmbedSrc } from "./video-embed";

describe("videoEmbed", () => {
  it("reads YouTube links in every common form", () => {
    for (const url of [
      "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
      "https://youtu.be/dQw4w9WgXcQ?t=10",
      "https://www.youtube.com/shorts/dQw4w9WgXcQ",
      "https://www.youtube-nocookie.com/embed/dQw4w9WgXcQ",
    ]) {
      expect(videoEmbed(url)).toEqual({ provider: "youtube", id: "dQw4w9WgXcQ" });
    }
  });

  it("reads Vimeo links", () => {
    expect(videoEmbed("https://vimeo.com/76979871")).toEqual({ provider: "vimeo", id: "76979871" });
    expect(videoEmbed("https://player.vimeo.com/video/76979871")).toEqual({ provider: "vimeo", id: "76979871" });
  });

  it("refuses every other host, scheme and a link that names no video", () => {
    expect(videoEmbed("https://cdn.example.com/tour.mp4")).toBeNull();
    expect(videoEmbed("https://www.youtube.com.evil.test/watch?v=dQw4w9WgXcQ")).toBeNull();
    expect(videoEmbed("http://www.youtube.com/watch?v=dQw4w9WgXcQ")).toBeNull();
    expect(videoEmbed("https://www.youtube.com/@filigran")).toBeNull();
    expect(videoEmbed("javascript:alert(1)")).toBeNull();
    expect(videoEmbed(null)).toBeNull();
  });
});

describe("videoEmbedSrc", () => {
  it("plays YouTube from the privacy-enhanced host with the block's options", () => {
    const src = new URL(
      videoEmbedSrc({ provider: "youtube", id: "dQw4w9WgXcQ" }, { autoplay: true, muted: true, loop: true, start: 5.4 }),
    );
    expect(src.origin).toBe("https://www.youtube-nocookie.com");
    expect(src.pathname).toBe("/embed/dQw4w9WgXcQ");
    expect(src.searchParams.get("autoplay")).toBe("1");
    expect(src.searchParams.get("mute")).toBe("1");
    expect(src.searchParams.get("playlist")).toBe("dQw4w9WgXcQ");
    expect(src.searchParams.get("start")).toBe("5");
  });

  it("plays Vimeo from its player host, do-not-track on", () => {
    const src = new URL(videoEmbedSrc({ provider: "vimeo", id: "76979871" }, { controls: false }));
    expect(src.origin).toBe("https://player.vimeo.com");
    expect(src.pathname).toBe("/video/76979871");
    expect(src.searchParams.get("dnt")).toBe("1");
    expect(src.searchParams.get("controls")).toBe("0");
  });

  it("links the poster to the video's own page", () => {
    expect(videoEmbedPage({ provider: "youtube", id: "dQw4w9WgXcQ" })).toBe("https://www.youtube.com/watch?v=dQw4w9WgXcQ");
    expect(videoEmbedPage({ provider: "vimeo", id: "76979871" })).toBe("https://vimeo.com/76979871");
  });
});
