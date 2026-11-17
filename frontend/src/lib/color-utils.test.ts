// @vitest-environment node
import { describe, it, expect } from "vitest";
import {
  ACCENT_SELECTOR,
  bestTextOn,
  buildCssVarsBlock,
  deriveBackgroundPalette,
  hexToHsl,
} from "./color-utils";

/**
 * White-label branding is the one place where an operator's runtime choice has
 * to reach tokens the design system generates from Figma. That makes it easy to
 * break in a way nothing else catches: the app keeps rendering, in the library's
 * default colours, and the operator's brand is quietly gone.
 *
 * These assertions pin the two things that would make that happen — the variable
 * names drifting away from the library's, and the selector losing to `.dark`.
 */
describe("buildCssVarsBlock", () => {
  it("returns null when no branding is configured", () => {
    expect(buildCssVarsBlock(null, null)).toBeNull();
    expect(buildCssVarsBlock(undefined, undefined)).toBeNull();
  });

  it("drives the accent through the single product token", () => {
    const block = buildCssVarsBlock("#8c3dff", null);
    expect(block).toContain("--color-accent:hsl(");
    expect(block).toContain("--color-accent-foreground:hsl(");
    // The pre-migration token names must not come back: nothing reads them.
    expect(block).not.toContain("--cyan");
    expect(block).not.toContain("--ring:");
  });

  it("reaches the library's own brand token, not just the product accent", () => {
    // The library's components paint from `--color-filigran-brand-*` and know
    // nothing about a host accent: without this an operator rebrands the app
    // and every primary button stays Filigran blue.
    expect(buildCssVarsBlock("#8c3dff", null)).toContain("--color-filigran-brand-primary:hsl(");
  });

  it("outbids the design system's own theme blocks on specificity", () => {
    // `.light {}` / `.dark {}` tie with a single `:root`, leaving stylesheet
    // order to decide. Doubling the selector removes that dependency — on both
    // halves of `ACCENT_SELECTOR`, the forced-dark subtree included.
    expect(buildCssVarsBlock("#8c3dff", null)).toMatch(/^:root:root[,{]/);
  });

  it("carries the accent into the forced-dark subtree a presented deck is", () => {
    // The stage carries the library's `dark` class, whose token set would
    // otherwise reinstate the library's own accent over one inherited from
    // `:root`. Doubled on both halves for the same specificity reason.
    expect(ACCENT_SELECTOR).toBe(':root:root,[data-mode="dark"][data-mode="dark"]');
    expect(buildCssVarsBlock("#8c3dff", null)).toMatch(/^:root:root,\[data-mode="dark"\]\[data-mode="dark"\]\{/);
  });

  it("keeps the background palette off that subtree", () => {
    // A black stage wants the dark set it carries, never surfaces derived for
    // the app's own background.
    const block = buildCssVarsBlock(null, "#ececf2");
    expect(block).toMatch(/^:root:root\{/);
    expect(block).not.toContain('[data-mode="dark"]');
  });

  it("ignores a malformed colour instead of emitting a broken rule", () => {
    expect(buildCssVarsBlock("not-a-colour", null)).toBeNull();
  });

  it("picks dark accent text on a light accent, and light text on a dark one", () => {
    expect(buildCssVarsBlock("#ffe066", null)).toContain("--color-accent-foreground:hsl(220 56% 6%)");
    expect(buildCssVarsBlock("#101820", null)).toContain("--color-accent-foreground:hsl(0 0% 100%)");
    // A dark purple takes white text.
    expect(buildCssVarsBlock("#5e1ad5", null)).toContain("--color-accent-foreground:hsl(0 0% 100%)");
  });
});

describe("bestTextOn", () => {
  it("picks the higher-contrast of the two text colours, not a luminance threshold", () => {
    // #a64dff (the XTM One dark accent): dark text reads 4.7:1 where white
    // reads 4.1:1, and a fixed threshold would pick white.
    expect(bestTextOn("#a64dff")).toBe("220 56% 6%");
    expect(bestTextOn("#009ad6")).toBe("220 56% 6%");
    expect(bestTextOn("#1a1a1a")).toBe("0 0% 100%");
    expect(bestTextOn("nope")).toBeNull();
  });
});

describe("deriveBackgroundPalette contrast", () => {
  it("recomputes the text on the highlight fill from that fill", () => {
    // The ribbon's 9px label sits on the highlight fill, not on the page
    // background, so the page foreground is not what decides its contrast.
    const dark = deriveBackgroundPalette("#070d18") ?? {};
    const light = deriveBackgroundPalette("#ececf2") ?? {};
    expect(dark["--color-on-highlight"]).toBe("hsl(0 0% 100%)");
    expect(light["--color-on-highlight"]).toBe("hsl(220 56% 6%)");
  });
});

describe("deriveBackgroundPalette", () => {
  it("derives the full four-layer elevation ladder the library expects", () => {
    const palette = deriveBackgroundPalette("#070d18");
    expect(palette).not.toBeNull();
    for (const layer of [0, 1, 2, 3]) {
      expect(palette).toHaveProperty(`--bg-elevation-default-layer-${layer}`);
      expect(palette).toHaveProperty(`--bg-elevation-hover-layer-${layer}`);
      expect(palette).toHaveProperty(`--bg-elevation-highlight-layer-${layer}`);
      expect(palette).toHaveProperty(`--border-elevation-subtle-layer-${layer}`);
    }
  });

  it("emits complete colours, not the bare HSL triplets the old tokens used", () => {
    // FDS variables hold colours; a triplet would only work wrapped in `hsl()`
    // by every consumer, which is precisely the pattern the migration removed.
    for (const value of Object.values(deriveBackgroundPalette("#070d18") ?? {})) {
      expect(value).toMatch(/^hsl\(/);
    }
  });

  it("lifts each layer above the one below it on a dark background", () => {
    const palette = deriveBackgroundPalette("#070d18") ?? {};
    const lightness = (key: string) => Number(/(\d+)%\)$/.exec(palette[key])?.[1]);
    expect(lightness("--bg-elevation-default-layer-0")).toBeLessThan(
      lightness("--bg-elevation-default-layer-1"),
    );
    expect(lightness("--bg-elevation-default-layer-1")).toBeLessThan(
      lightness("--bg-elevation-default-layer-2"),
    );
    expect(lightness("--bg-elevation-default-layer-2")).toBeLessThan(
      lightness("--bg-elevation-default-layer-3"),
    );
  });

  it("reads text colour off the background's luminance", () => {
    expect(deriveBackgroundPalette("#ffffff")?.["--text-default-primary"]).toBe("hsl(220 5% 10%)");
    expect(deriveBackgroundPalette("#070d18")?.["--text-default-primary"]).toBe("hsl(0 0% 100%)");
  });

  it("clamps rather than overflowing at the ends of the lightness range", () => {
    for (const value of Object.values(deriveBackgroundPalette("#ffffff") ?? {})) {
      const lightness = Number(/(\d+)%\)$/.exec(value)?.[1]);
      if (!Number.isNaN(lightness)) expect(lightness).toBeLessThanOrEqual(100);
    }
  });
});

describe("hexToHsl", () => {
  it("accepts the three hex lengths the backend validates", () => {
    expect(hexToHsl("#fff")).toEqual({ h: 0, s: 0, l: 100 });
    expect(hexToHsl("#ffffff")).toEqual({ h: 0, s: 0, l: 100 });
    // Alpha is accepted for parity with backend validation, then ignored.
    expect(hexToHsl("#ffffff80")).toEqual({ h: 0, s: 0, l: 100 });
  });

  it("rejects anything else", () => {
    expect(hexToHsl("#12")).toBeNull();
    expect(hexToHsl("rebeccapurple")).toBeNull();
  });
});
