// Template design tokens on the blocks as they ship.
//
// The real stylesheet and the real components: a callout, a metrics grid and
// pricing tiers render `data-density="normal"` when the spec leaves the
// density out, and the spacing tokens a template declares must reach them.
// The light ribbon and gradient banner keep white text on a band dark enough
// for it (WCAG contrast read off the colours the stylesheet paints).

import { readFileSync } from "node:fs";
import { beforeAll, describe, expect, it, vi } from "vitest";

import { designTokenStylesheet } from "@/testing/design-tokens";
import { renderWithProviders } from "@/testing/providers";
import type { DeckBlock } from "@/types";
import { BlockList } from "./blocks/index";
import { designRootAttributes, draftTemplateDesign } from "./design-tokens";

vi.mock("./deck-icons", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./deck-icons")>();
  const { icons } = await import("lucide-react");
  return { ...actual, getDeckIconCatalog: () => icons, loadDeckIconCatalog: () => Promise.resolve(icons) };
});

const stylesheet = readFileSync(`${__dirname}/presentation.css`, "utf8");

beforeAll(() => {
  const style = document.createElement("style");
  style.textContent = designTokenStylesheet() + stylesheet;
  document.head.appendChild(style);
});

/** The spacing the seeded Filigran Brand Deck declares. */
const FLAT = designRootAttributes({
  shadows: false,
  glow: false,
  spacing: { tile_gap: 24, card_padding: 24, kpi_padding: 20 },
});

const BLOCKS: DeckBlock[] = [
  { type: "callout", title: "Decision", content: "Consolidate on one platform by Q2." },
  {
    type: "metrics_grid",
    columns: 3,
    items: [
      { value: "3", label: "Products" },
      { value: "1", label: "Platform" },
      { value: "0", label: "Silos" },
    ],
  },
  {
    type: "pricing_tiers",
    tiers: [
      { name: "Community", price: "Free" },
      { name: "Enterprise", price: "Contact us", highlight: true, badge: "Most popular" },
    ],
  },
] as DeckBlock[];

function renderUnder(design: ReturnType<typeof designRootAttributes>) {
  const { style, ...attributes } = design;
  return renderWithProviders(
    <div className="deck-root">
      <div className="deck-slide" data-scheme="light" {...attributes} style={style}>
        <BlockList blocks={BLOCKS} className="deck-blocks deck-blocks-slide" path="blocks" />
      </div>
    </div>,
  );
}

describe("spacing tokens on the default density", () => {
  it("renders each block with the density it ships with", () => {
    const { container } = renderUnder(FLAT);
    for (const selector of [".deck-callout", ".deck-metrics", ".deck-tiers"]) {
      expect((container.querySelector(selector) as HTMLElement).dataset.density).toBe("normal");
    }
  });

  it("reads the card padding, the KPI padding and the tile gap", () => {
    const { container } = renderUnder(FLAT);
    const callout = container.querySelector(".deck-callout") as HTMLElement;
    const metrics = container.querySelector(".deck-metrics") as HTMLElement;
    const tiers = container.querySelector(".deck-tiers") as HTMLElement;
    const tier = container.querySelector(".deck-tier") as HTMLElement;
    expect(getComputedStyle(callout).paddingTop).toBe("24px");
    expect(getComputedStyle(callout).paddingLeft).toBe("24px");
    expect(getComputedStyle(metrics).getPropertyValue("--deck-metric-pad-x").trim()).toBe("20px");
    expect(getComputedStyle(metrics).getPropertyValue("--deck-metric-pad-y").trim()).toBe("20px");
    expect(getComputedStyle(metrics).getPropertyValue("--deck-metric-gap").trim()).toBe("24px");
    expect(getComputedStyle(tiers).rowGap || getComputedStyle(tiers).gap).toContain("24px");
    expect(getComputedStyle(tier).paddingTop).toBe("24px");
  });

  it("leaves a deck without design tokens on today's spacing", () => {
    const { container } = renderUnder(designRootAttributes(null));
    const metrics = container.querySelector(".deck-metrics") as HTMLElement;
    expect(getComputedStyle(metrics).getPropertyValue("--deck-metric-pad-x").trim()).not.toBe("20px");
    expect(getComputedStyle(container.querySelector(".deck-tier") as HTMLElement).paddingTop).not.toBe("24px");
  });
});

/** A computed length in px: `23px`, or the `calc(24px - 1px)` jsdom leaves unresolved. */
function px(value: string): number {
  const terms = value.replace(/^calc\((.*)\)$/, "$1").match(/[-+]?\s*\d+(?:\.\d+)?px/g) ?? [];
  expect(terms.length, value).toBeGreaterThan(0);
  return terms.reduce((sum, term) => sum + Number.parseFloat(term.replace(/\s/g, "")), 0);
}

describe("highlighted pricing tier content offset", () => {
  // jsdom resolves the padding but not a border shorthand built on a variable,
  // so the border widths are read off the stylesheet's own rules.
  const padding = (tier: Element | null) => {
    const style = getComputedStyle(tier as HTMLElement);
    return { top: px(style.paddingTop), left: px(style.paddingLeft) };
  };
  const plainBorder = () => {
    expect(rule(".deck-root .deck-tier")).toContain("border: var(--deck-hairline) solid");
    const hairline = stylesheet.match(/--deck-hairline:\s*(\d+)px;/);
    return Number(hairline?.[1]);
  };
  const flatHighlightBorder = () => {
    // The selector holds two rules (no lift, then the gradient frame): read every one.
    const selector = '.deck-root [data-design-glow="false"] .deck-tier[data-highlight="true"] {';
    const widths = stylesheet
      .split(selector)
      .slice(1)
      .map((rest) => rest.slice(0, rest.indexOf("}")).match(/border:\s*(\d+)px solid/)?.[1])
      .filter(Boolean);
    expect(widths).toHaveLength(1);
    return Number(widths[0]);
  };

  it("gives back the pixel of its 2 px gradient border so the row's text starts together", () => {
    const { container } = renderUnder(FLAT);
    const plain = padding(container.querySelector('.deck-tier:not([data-highlight="true"])'));
    const highlighted = padding(container.querySelector('.deck-tier[data-highlight="true"]'));
    expect(plainBorder()).toBe(1);
    expect(flatHighlightBorder()).toBe(2);
    expect(plain).toEqual({ top: 24, left: 24 });
    expect(highlighted).toEqual({ top: 23, left: 23 });
    // Border plus padding: where the text of each tier starts inside its box.
    expect(flatHighlightBorder() + highlighted.top).toBe(plainBorder() + plain.top);
    expect(flatHighlightBorder() + highlighted.left).toBe(plainBorder() + plain.left);
  });

  it("keeps the full padding when the template keeps its glow (1 px border, no gradient frame)", () => {
    const { container } = renderUnder(designRootAttributes({ shadows: false, spacing: { card_padding: 24 } }));
    expect(padding(container.querySelector('.deck-tier[data-highlight="true"]'))).toEqual({ top: 24, left: 24 });
  });
});

/** WCAG 2 relative luminance of a #rrggbb colour. */
function luminance(hex: string): number {
  const channels = [1, 3, 5].map((at) => Number.parseInt(hex.slice(at, at + 2), 16) / 255);
  const [r, g, b] = channels.map((c) => (c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4));
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}
function contrast(a: string, b: string): number {
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x);
  return (hi + 0.05) / (lo + 0.05);
}
/** The declarations of the one rule whose selector is exactly `selector`. */
function rule(selector: string): string {
  const at = stylesheet.indexOf(`${selector} {`);
  expect(at, selector).toBeGreaterThanOrEqual(0);
  return stylesheet.slice(at, stylesheet.indexOf("}", at));
}
const colours = (body: string, property: string): string[] => {
  const line = body.split("\n").find((text) => text.trim().startsWith(`${property}:`)) ?? "";
  return (line.match(/#[0-9a-f]{6}/gi) ?? []).map((hex) => hex.toLowerCase());
};

describe("white text on the light flat bands", () => {
  it("draws the light ribbon solid, at 4.5:1 or more", () => {
    const body = rule(
      '.deck-root [data-scheme="light"][data-design-glow="false"] .deck-tier[data-highlight="true"] .deck-tier-badge',
    );
    const [text] = colours(body, "color");
    const stops = colours(body, "background");
    expect(stops).toHaveLength(1);
    expect(contrast(text, stops[0])).toBeGreaterThanOrEqual(4.5);
  });

  it("keeps every stop of the light banner gradient at 4.5:1 or more under its text", () => {
    const body = rule('.deck-root [data-scheme="light"][data-design-glow="false"] .deck-banner[data-variant="gradient"]');
    const [text] = colours(body, "color");
    const stops = colours(body, "background");
    expect(stops.length).toBeGreaterThanOrEqual(2);
    for (const stop of stops) expect(contrast(text, stop), stop).toBeGreaterThanOrEqual(4.5);
  });
});

describe("light tinted surfaces on the brand tint", () => {
  const LIGHT_TINT = [
    '.deck-root [data-scheme="light"][data-design-glow="false"] .deck-callout:not([data-variant])',
    '.deck-root [data-scheme="light"][data-design-glow="false"] .deck-callout[data-variant="tinted"]',
    '.deck-root [data-scheme="light"][data-design-glow="false"] .deck-banner[data-variant="soft"][data-tone="accent"]',
    '.deck-root [data-scheme="light"][data-design-glow="false"] .deck-banner[data-variant="soft"]:not([data-tone])',
  ].join(",\n");
  const TINT = ["#daf1ff", "rgb(218,241,255)"];
  const paint = (element: Element | null) =>
    getComputedStyle(element as HTMLElement).backgroundColor.replace(/\s/g, "").toLowerCase();

  it("fills the callout and the soft banner flat with the --deck-tint-surface token, never the violet Royal Blue mix", () => {
    const body = rule(LIGHT_TINT);
    expect(body).toContain("background: var(--deck-tint-surface);");
    expect(body).not.toContain("color-mix");
    expect(body).not.toMatch(/#[0-9a-f]{6}/i);
  });

  it("declares the light tint once, as #DAF1FF on the light palette", () => {
    expect(stylesheet.match(/--deck-tint-surface:\s*#[0-9a-f]{6}/gi)).toEqual(["--deck-tint-surface: #daf1ff"]);
    // No declaration paints the literal: the comments may name it, a value never.
    expect(stylesheet.match(/:\s*#daf1ff\s*;/gi)).toEqual([": #daf1ff;"]);
  });

  it("keeps the body text and the Royal Blue title at 4.5:1 or more on the tint", () => {
    for (const ink of ["#00020c", "#001bda"]) expect(contrast(ink, "#daf1ff"), ink).toBeGreaterThanOrEqual(4.5);
  });

  it("paints the tint on a light callout and leaves the dark one on its 12% mix", () => {
    const { container } = renderUnder(FLAT);
    const callout = container.querySelector(".deck-callout") as HTMLElement;
    // jsdom echoes the declared colour, a browser resolves it to rgb().
    expect(["#daf1ff", "rgb(218,241,255)"]).toContain(
      getComputedStyle(callout).backgroundColor.replace(/\s/g, "").toLowerCase(),
    );
    expect(rule('.deck-root [data-design-glow="false"] .deck-callout[data-variant="tinted"]')).toContain(
      "color-mix(in srgb, var(--deck-gradient-from) 12%, var(--deck-surface))",
    );
  });

  it("paints the tint on the accent soft banner only: success, warning and error keep their tone mix (UX B1)", () => {
    const { style, ...attributes } = FLAT;
    const tones = ["accent", "success", "warning", "error"] as const;
    const banners = tones.map((tone) => ({ type: "banner", variant: "soft", tone, title: `Band ${tone}` }));
    const { container } = renderWithProviders(
      <div className="deck-root">
        <div className="deck-slide" data-scheme="light" {...attributes} style={style}>
          <BlockList blocks={banners as DeckBlock[]} className="deck-blocks deck-blocks-slide" path="blocks" />
        </div>
      </div>,
    );
    const byTone = (tone: string) => container.querySelector(`.deck-banner[data-variant="soft"][data-tone="${tone}"]`);
    expect(TINT).toContain(paint(byTone("accent")));
    for (const tone of tones.slice(1)) {
      expect(byTone(tone), tone).not.toBeNull();
      expect(TINT, tone).not.toContain(paint(byTone(tone)));
    }
    expect(rule('.deck-root .deck-banner[data-variant="soft"]')).toContain(
      "color-mix(in srgb, var(--tone) 12%, var(--deck-surface))",
    );
  });
});

describe("toned flat callouts on light take their own tone tint", () => {
  const TONED =
    '.deck-root [data-scheme="light"][data-design-glow="false"] .deck-callout:is(:not([data-variant]), [data-variant="tinted"]):is([data-tone="success"], [data-tone="warning"], [data-tone="error"])';
  /** `color-mix(in srgb, tone p%, surface)` in sRGB, as the browser paints it. */
  const mix = (tone: string, share: number, surface: string) => {
    const channel = (hex: string, at: number) => Number.parseInt(hex.slice(at, at + 2), 16);
    return `#${[1, 3, 5]
      .map((at) => Math.round(channel(tone, at) * share + channel(surface, at) * (1 - share)))
      .map((value) => value.toString(16).padStart(2, "0"))
      .join("")}`;
  };

  it("paints a success, warning or error callout with its tone at 12% over the surface", () => {
    expect(rule(TONED)).toContain("background: color-mix(in srgb, var(--tone) 12%, var(--deck-surface));");
    // After the accent tint rule, so it wins at equal specificity or above.
    expect(stylesheet.indexOf(`${TONED} {`)).toBeGreaterThan(stylesheet.indexOf("background: var(--deck-tint-surface);"));
  });

  it("keeps the accent callout on the brand tint in the render", () => {
    const { style, ...attributes } = FLAT;
    const tones = ["accent", "success", "warning", "error"] as const;
    const callouts = tones.map((tone) => ({ type: "callout", tone, title: `Note ${tone}`, content: "Body." }));
    const { container } = renderWithProviders(
      <div className="deck-root">
        <div className="deck-slide" data-scheme="light" {...attributes} style={style}>
          <BlockList blocks={callouts as DeckBlock[]} className="deck-blocks deck-blocks-slide" path="blocks" />
        </div>
      </div>,
    );
    const byTone = (tone: string) => container.querySelector(`.deck-callout[data-tone="${tone}"]`) as HTMLElement;
    for (const tone of tones) expect(byTone(tone), tone).not.toBeNull();
    expect(byTone("accent").matches(TONED)).toBe(false);
    for (const tone of tones.slice(1)) expect(byTone(tone).matches(TONED), tone).toBe(true);
  });

  it("reads every tone title and the body at 4.5:1 or more on the Filigran Brand Deck light tints, warning above the floor", () => {
    // The seeded light palette (template_container_seed.py): surface #FFFFFF,
    // tone and `*_text` pairs.
    const tones = { success: ["#4caf50", "#256628"], warning: ["#f9a825", "#a05a00"], error: ["#d32f2f", "#a82626"] };
    for (const [name, [tone, text]] of Object.entries(tones)) {
      const tint = mix(tone, 0.12, "#ffffff");
      expect(contrast(text, tint), name).toBeGreaterThanOrEqual(4.5);
      expect(contrast("#00020c", tint), name).toBeGreaterThanOrEqual(4.5);
    }
    const warning = contrast("#a05a00", mix("#f9a825", 0.12, "#ffffff"));
    expect(warning).toBeGreaterThan(contrast("#a05a00", "#daf1ff"));
    expect(warning).toBeGreaterThanOrEqual(4.8);
  });
});

describe("draftTemplateDesign", () => {
  const stored = { glow: false } as const;
  const flatRow = { extra_metadata: { design: { glow: false, shadows: false } } };
  const plainRow = { extra_metadata: {} };

  it("keeps the stored design while the template is unchanged", () => {
    expect(draftTemplateDesign({ templateContainerId: "t", templateChanged: false, stored, tried: plainRow })).toBe(
      stored,
    );
  });

  it("previews the tried template's design, flat or not, before the save", () => {
    expect(draftTemplateDesign({ templateContainerId: "x", templateChanged: true, stored, tried: plainRow })).toBeNull();
    expect(
      draftTemplateDesign({ templateContainerId: "f", templateChanged: true, stored: null, tried: flatRow }),
    ).toEqual({ glow: false, shadows: false });
  });

  it("draws no design without a template", () => {
    expect(draftTemplateDesign({ templateContainerId: null, templateChanged: true, stored, tried: flatRow })).toBeNull();
  });
});
