/**
 * The template's right footer: `{{page}}` two digits from the first
 * slide after the cover, `{{year}}` computed at render time, nothing when the
 * template declares no `footer_right` (the slide keeps its `n / total`).
 */
import { describe, expect, it } from "vitest";

import type { PresentationSpec, TemplateChrome } from "@/types";

import { chromeFooterRight, chromePage, templateChromeOf } from "./chrome-pattern";

const FILIGRAN: TemplateChrome = {
  footer: "{{title}} \u00b7 {{event}}",
  header: null,
  variables: [],
  emblem: true,
  footer_right: "{{page}} \u00b7 \u00a9 {{year}} Filigran. All rights reserved.",
  emblem_size: 48,
  emblem_align: "title",
  emblem_ink: "accent",
};

function spec(layouts: string[], showNumbers?: boolean): Pick<PresentationSpec, "title" | "slides" | "chrome"> {
  return {
    title: "Q3 Review",
    slides: layouts.map((layout, i) => ({ id: `s${i}`, layout })) as PresentationSpec["slides"],
    chrome: showNumbers === undefined ? {} : { show_slide_numbers: showNumbers },
  };
}

describe("chromePage", () => {
  it("counts from the first slide after the cover, two digits", () => {
    const slides = [{ layout: "title" }, { layout: "content" }, { layout: "agenda" }];
    expect(chromePage(slides, 0, true)).toBe("");
    expect(chromePage(slides, 1, true)).toBe("01");
    expect(chromePage(slides, 2, true)).toBe("02");
  });

  it("counts from the first slide when the deck opens without a cover", () => {
    expect(chromePage([{ layout: "content" }], 0, true)).toBe("01");
  });

  it("is empty when the deck hides its slide numbers", () => {
    expect(chromePage([{ layout: "title" }, { layout: "content" }], 1, false)).toBe("");
  });
});

describe("chromeFooterRight", () => {
  it("fills the page and the year", () => {
    expect(chromeFooterRight(spec(["title", "content", "content"]), FILIGRAN, 2, 2027)).toBe(
      "02 \u00b7 \u00a9 2027 Filigran. All rights reserved.",
    );
  });

  it("drops the page and its separator when slide numbers are off", () => {
    expect(chromeFooterRight(spec(["title", "content"], false), FILIGRAN, 1, 2026)).toBe(
      "\u00a9 2026 Filigran. All rights reserved.",
    );
  });

  it("is null for a template that declares no right footer", () => {
    expect(chromeFooterRight(spec(["content"]), { ...FILIGRAN, footer_right: undefined }, 0)).toBeNull();
    expect(chromeFooterRight(spec(["content"]), null, 0)).toBeNull();
  });

  it("defaults the year to the current one", () => {
    const text = chromeFooterRight(spec(["content"]), FILIGRAN, 0);
    expect(text).toContain(String(new Date().getFullYear()));
  });
});

describe("templateChromeOf", () => {
  it("reads the right footer and the emblem placement, leniently", () => {
    const read = templateChromeOf({ extra_metadata: { chrome: FILIGRAN as unknown as Record<string, unknown> } });
    expect(read?.footer_right).toBe(FILIGRAN.footer_right);
    expect([read?.emblem_size, read?.emblem_align, read?.emblem_ink]).toEqual([48, "title", "accent"]);
    const bad = templateChromeOf({
      extra_metadata: { chrome: { emblem: true, emblem_size: 500, emblem_align: "left", emblem_ink: "blue" } },
    });
    expect(bad?.emblem_size).toBeUndefined();
    expect(bad?.emblem_align).toBeUndefined();
    expect(bad?.emblem_ink).toBeUndefined();
  });
});
