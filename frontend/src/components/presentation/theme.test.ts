/**
 * Unit tests for the slide scheme resolver - `yarn test:unit`.
 *
 * `resolveSlideScheme` is the ONE reader every slide root goes through; what
 * is pinned here is the precedence rule (a slide's own theme wins over the
 * deck's, an unknown value inherits), the palette it hands the root (the
 * template's for THAT scheme, never the deck's), and the attributes the root
 * publishes for the stylesheet.
 */

import { test } from "node:test";
import assert from "node:assert/strict";

import type { Presentation, PresentationSlide } from "@/types";

import {
  resolveSlideScheme,
  SCHEME_PAPER,
  slidePaper,
  slideRootAttributes,
  slideScheme,
  themeConfigFor,
} from "./theme.ts";

const presentation = {
  theme_config: { background: "#000", accent: "#0fbcff" },
  theme_configs: {
    dark: { background: "#000", accent: "#0fbcff", mark_file_id: "mark-dark" },
    light: { background: "#fff", accent: "#0015a8" },
  },
  spec: { theme: "light" },
} as unknown as Presentation;

const slideWith = (theme: unknown): PresentationSlide =>
  ({ layout: "title", title: "T", theme }) as unknown as PresentationSlide;

test("a slide inherits the deck scheme unless it names its own", () => {
  assert.equal(slideScheme("light", undefined), "light");
  assert.equal(slideScheme("light", slideWith(undefined)), "light");
  assert.equal(slideScheme("light", slideWith("dark")), "dark");
  assert.equal(slideScheme("dark", slideWith("light")), "light");
  // The deck theme is read like `themeScheme`: anything but light is dark.
  assert.equal(slideScheme("filigran", undefined), "dark");
  assert.equal(slideScheme(undefined, undefined), "dark");
});

test("an unknown slide theme inherits and is not an override", () => {
  for (const junk of ["blue", "", null, 3, "Dark"]) {
    const resolution = resolveSlideScheme(presentation, slideWith(junk));
    assert.equal(resolution.scheme, "light", String(junk));
    assert.equal(resolution.overridden, false, String(junk));
  }
});

test("the resolution carries the template palette of the scheme the slide is drawn in", () => {
  const cover = resolveSlideScheme(presentation, slideWith("dark"));
  assert.equal(cover.scheme, "dark");
  assert.equal(cover.overridden, true);
  assert.deepEqual(cover.tokens, presentation.theme_configs?.dark);
  // The mark the gradient hero watermarks rides that palette, never the deck's.
  assert.equal(cover.tokens.mark_file_id, "mark-dark");
  const body = resolveSlideScheme(presentation, slideWith(undefined));
  assert.deepEqual(body.tokens, themeConfigFor(presentation, "light"));
  assert.equal(body.tokens.mark_file_id, undefined);
});

test("the editor's draft theme outranks the saved spec's for an inheriting slide only", () => {
  const inheriting = resolveSlideScheme(presentation, slideWith(undefined), "dark");
  assert.equal(inheriting.scheme, "dark");
  assert.equal(inheriting.overridden, false);
  const own = resolveSlideScheme(presentation, slideWith("light"), "dark");
  assert.equal(own.scheme, "light");
  assert.equal(own.overridden, true);
});

test("the root attributes publish the scheme, the override flag and the palette as inline variables", () => {
  const root = slideRootAttributes(resolveSlideScheme(presentation, slideWith("dark")));
  assert.equal(root["data-scheme"], "dark");
  assert.equal(root["data-scheme-override"], "true");
  const style = root.style as Record<string, string>;
  assert.equal(style["--deck-bg"], "#000");
  assert.equal(style["--deck-accent"], "#0fbcff");
  // A file id is not a CSS token: it never lands in a declaration.
  assert.equal(Object.values(style).includes("mark-dark"), false);
  const inheriting = slideRootAttributes(resolveSlideScheme(presentation, slideWith(undefined)));
  assert.equal(inheriting["data-scheme"], "light");
  assert.equal(inheriting["data-scheme-override"], undefined);
});

test("a slide's paper is its palette's background for its own scheme, else that scheme's", () => {
  // The paper reveal paints under the slide is the one the slide root draws.
  assert.deepEqual(slidePaper(resolveSlideScheme(presentation, slideWith("dark"))), { color: "#000" });
  assert.deepEqual(slidePaper(resolveSlideScheme(presentation, slideWith(undefined))), { color: "#fff" });
  // No template background: the scheme's own, never the deck's.
  const bare = { theme_config: {}, theme_configs: { dark: {}, light: {} }, spec: { theme: "light" } } as unknown as Presentation;
  assert.deepEqual(slidePaper(resolveSlideScheme(bare, slideWith("dark"))), { color: SCHEME_PAPER.dark });
  assert.deepEqual(slidePaper(resolveSlideScheme(bare, slideWith(undefined))), { color: SCHEME_PAPER.light });
});

test("a scheme's own paper is a reference to its token, never a copy of the colour", () => {
  // Resolved by reveal's background layer off the deck root, where the token
  // is declared once and read by the token block of each scheme.
  assert.equal(SCHEME_PAPER.dark, "var(--deck-paper-dark)");
  assert.equal(SCHEME_PAPER.light, "var(--deck-paper-light)");
});

test("a gradient background rides reveal's gradient layer over the scheme's paper", () => {
  // A gradient is no colour: as reveal's background colour it was dropped,
  // and the slide stood transparent over the stage.
  const linear = "linear-gradient(135deg, #0015a8, #42caff)";
  const radial = "  repeating-radial-gradient(circle, #ffffff, #f2f2f3 40px) ";
  const graded = {
    theme_config: {},
    theme_configs: { dark: { background: linear }, light: { background: radial } },
    spec: { theme: "light" },
  } as unknown as Presentation;
  assert.deepEqual(slidePaper(resolveSlideScheme(graded, slideWith("dark"))), { color: SCHEME_PAPER.dark, gradient: linear });
  assert.deepEqual(slidePaper(resolveSlideScheme(graded, slideWith(undefined))), {
    color: SCHEME_PAPER.light,
    gradient: radial.trim(),
  });
  // Every gradient syntax the theme sanitiser accepts, the legacy WebKit one included.
  for (const legacy of [
    "-webkit-gradient(linear, left top, right top, from(#0015a8), to(#42caff))",
    "-webkit-linear-gradient(left, #0015a8, #42caff)",
    "-moz-radial-gradient(#0015a8, #42caff)",
    "conic-gradient(#0015a8, #42caff)",
  ]) {
    const old = { theme_config: {}, theme_configs: { dark: { background: legacy }, light: {} }, spec: { theme: "dark" } } as unknown as Presentation;
    assert.deepEqual(slidePaper(resolveSlideScheme(old, slideWith(undefined))), { color: SCHEME_PAPER.dark, gradient: legacy }, legacy);
  }
  // Neither a colour nor a gradient, or a value the theme guard refuses:
  // the slide takes its scheme's paper, never transparency.
  for (const odd of ['image-set("paper.png" 1x)', "none", "cross-fade(#000, #fff, 50%)"]) {
    const other = { theme_config: {}, theme_configs: { dark: { background: odd }, light: {} }, spec: { theme: "dark" } } as unknown as Presentation;
    assert.deepEqual(slidePaper(resolveSlideScheme(other, slideWith(undefined))), { color: SCHEME_PAPER.dark }, odd);
  }
  for (const color of ["#0b1221", "rgb(7 12 24 / 90%)", "oklch(20% 0.05 250)", "var(--brand-paper)", "transparent", "navy"]) {
    const colored = { theme_config: {}, theme_configs: { dark: { background: color }, light: {} }, spec: { theme: "dark" } } as unknown as Presentation;
    assert.deepEqual(slidePaper(resolveSlideScheme(colored, slideWith(undefined))), { color }, color);
  }
  // In a browser `CSS.supports("color", ...)` also accepts the CSS-wide
  // keywords, which paint no opaque paper (`initial` is transparent).
  const supports = (globalThis as { CSS?: unknown }).CSS;
  (globalThis as { CSS?: unknown }).CSS = { supports: () => true };
  try {
    for (const keyword of ["inherit", "initial", "unset", "revert", "revert-layer", "INHERIT"]) {
      const wide = { theme_config: {}, theme_configs: { dark: { background: keyword }, light: {} }, spec: { theme: "dark" } } as unknown as Presentation;
      assert.deepEqual(slidePaper(resolveSlideScheme(wide, slideWith(undefined))), { color: SCHEME_PAPER.dark }, keyword);
    }
    const colored = { theme_config: {}, theme_configs: { dark: { background: "navy" }, light: {} }, spec: { theme: "dark" } } as unknown as Presentation;
    assert.deepEqual(slidePaper(resolveSlideScheme(colored, slideWith(undefined))), { color: "navy" });
  } finally {
    (globalThis as { CSS?: unknown }).CSS = supports;
  }
  // A plain colour stays reveal's colour, and a blank background is no background.
  const named = { theme_config: {}, theme_configs: { dark: { background: "hsl(220 54% 8%)" }, light: { background: " " } }, spec: { theme: "light" } } as unknown as Presentation;
  assert.deepEqual(slidePaper(resolveSlideScheme(named, slideWith("dark"))), { color: "hsl(220 54% 8%)" });
  assert.deepEqual(slidePaper(resolveSlideScheme(named, slideWith(undefined))), { color: SCHEME_PAPER.light });
});

test("a palette value the theme guard refuses never reaches a deck sink", () => {
  // Every read path drops such a value already; the sinks refuse it too, so
  // one regressed path cannot make a viewer's browser load a URL again.
  const leak = 'linear-gradient(red, red), image-set("//example.com/x.png" 1x)';
  const leaky = {
    theme_config: {},
    theme_configs: { dark: { background: leak, accent: "url(//example.com/x.png)", surface: "#111c2d" }, light: {} },
    spec: { theme: "dark" },
  } as unknown as Presentation;
  const resolution = resolveSlideScheme(leaky, slideWith(undefined));
  assert.deepEqual(slidePaper(resolution), { color: SCHEME_PAPER.dark });
  const style = slideRootAttributes(resolution).style as Record<string, string>;
  assert.equal(style["--deck-bg"], undefined);
  assert.equal(style["--deck-accent"], undefined);
  assert.equal(style["--deck-surface"], "#111c2d");
  // A kept value is trimmed the way the backend serves it (the shared trim set,
  // U+001C included, which the native trim() leaves).
  const padded = {
    theme_config: {},
    theme_configs: { dark: { background: "\u001c#0b1020\u001c", accent: "\u0085#42caff " }, light: {} },
    spec: { theme: "dark" },
  } as unknown as Presentation;
  const kept = resolveSlideScheme(padded, slideWith(undefined));
  assert.deepEqual(slidePaper(kept), { color: "#0b1020" });
  const keptStyle = slideRootAttributes(kept).style as Record<string, string>;
  assert.equal(keptStyle["--deck-bg"], "#0b1020");
  assert.equal(keptStyle["--deck-accent"], "#42caff");
});

test("the browser decides whether a palette background is a colour, and the fallback is no looser", () => {
  const paperOf = (background: string) =>
    slidePaper(
      resolveSlideScheme(
        { theme_config: {}, theme_configs: { dark: { background }, light: {} }, spec: { theme: "dark" } } as unknown as Presentation,
        slideWith(undefined),
      ),
    );
  // Without `CSS` (these tests): a colour call followed by anything is no
  // colour, as the browser says, while a colour nesting one call still is.
  assert.deepEqual(paperOf('rgb(0 0 0) image-set("paper.png" 1x)'), { color: SCHEME_PAPER.dark });
  for (const color of ["color-mix(in srgb, rgb(7 12 24) 60%, white)", "light-dark(#ffffff, hsl(220 56% 6%))"]) {
    assert.deepEqual(paperOf(color), { color }, color);
  }
  // In a browser its answer decides: a hex it refuses is no paper, and a
  // gradient or a CSS-wide keyword never reaches it.
  const supports = (globalThis as { CSS?: unknown }).CSS;
  const asked: [string, string][] = [];
  (globalThis as { CSS?: unknown }).CSS = {
    supports: (property: string, value: string) => {
      asked.push([property, value]);
      return false;
    },
  };
  try {
    assert.deepEqual(paperOf("#0b1221"), { color: SCHEME_PAPER.dark });
    assert.deepEqual(paperOf("linear-gradient(#070d18, #1f3965)"), { color: SCHEME_PAPER.dark, gradient: "linear-gradient(#070d18, #1f3965)" });
    assert.deepEqual(paperOf("inherit"), { color: SCHEME_PAPER.dark });
    assert.deepEqual(asked, [["color", "#0b1221"]]);
  } finally {
    (globalThis as { CSS?: unknown }).CSS = supports;
  }
});
