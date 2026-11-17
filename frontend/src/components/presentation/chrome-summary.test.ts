/**
 * The template Configuration tab's "Header & footer" summary: the
 * right footer pattern and the emblem size, placement and ink are listed
 * when the template declares them, and left out when it does not.
 */
import { describe, expect, it } from "vitest";

import { chromeExtraSummary, templateChromeOf } from "./chrome-pattern";

describe("chromeExtraSummary", () => {
  it("lists the four keys a template declares, in order", () => {
    const chrome = templateChromeOf({
      extra_metadata: {
        chrome: {
          footer: "{{title}}",
          emblem: true,
          footer_right: "{{page}} \u00b7 \u00a9 {{year}} Filigran",
          emblem_size: 48,
          emblem_align: "title",
          emblem_ink: "accent",
        },
      },
    });
    expect(chromeExtraSummary(chrome)).toEqual([
      { label: "Right footer", value: "{{page}} \u00b7 \u00a9 {{year}} Filigran", literal: true },
      { label: "Emblem size", value: "{n} px", literal: false, values: { n: 48 } },
      { label: "Emblem placement", value: "Level with the slide title", literal: false },
      { label: "Emblem ink", value: "One colour, the slide accent", literal: false },
    ]);
  });

  it("names the margin placement and the mark's own colours", () => {
    const chrome = templateChromeOf({
      extra_metadata: { chrome: { emblem: true, emblem_align: "margin", emblem_ink: "mark" } },
    });
    expect(chromeExtraSummary(chrome).map((line) => line.value)).toEqual([
      "In the top margin",
      "The mark's own colours",
    ]);
  });

  it("is empty for a template without the keys or without chrome", () => {
    expect(chromeExtraSummary(templateChromeOf({ extra_metadata: { chrome: { footer: "{{title}}" } } }))).toEqual([]);
    expect(chromeExtraSummary(null)).toEqual([]);
  });
});
