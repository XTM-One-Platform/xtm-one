/**
 * The template form edits every colour the renderer honours - `yarn test:unit`.
 *
 * Its colour groups are cut from `THEME_CONFIG_KEYS` (mirrored against the
 * backend by `backend/tests/test_template_palette_form.py`): together they
 * hold every colour key exactly once, so the form offers no key the renderer
 * ignores and misses none it honours. What an empty field shows is the colour
 * a deck draws instead: the stylesheet's own fallbacks, and the base palette a
 * variant inherits through the preview's rule.
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

import {
  BRAND_HUE_KEYS,
  brandHueFallbackKey,
  CHART_SERIES_KEYS,
  CORE_COLOR_KEYS,
  PALETTE_COLOR_KEYS,
  PALETTE_KEY_LABELS,
  PALETTE_SWATCH_KEYS,
  paletteFallback,
  paletteFallbackKey,
  resolvedPaletteColor,
  THEME_CONFIG_KEYS,
  withBaseKeyTakenBack,
  withoutVariantKey,
} from "./palette-keys.ts";
import { THEME_VARIABLES } from "./theme.ts";

const read = (path: string) => readFileSync(new URL(path, import.meta.url), "utf8");
const EDITOR = read("./TemplateDesignEditor.tsx");
const PREVIEW = read("./previewSpec.ts");
const GENERATOR = read("./TemplateGeneratorDialog.tsx");
const STYLESHEET = read("./presentation.css");

/** The custom properties a scheme's token block declares, by name. */
function schemeTokens(scheme: "dark" | "light"): Map<string, string> {
  const selector = `.deck-root [data-scheme="${scheme}"] {`;
  const start = STYLESHEET.indexOf(selector);
  assert.ok(start >= 0, `presentation.css has no ${scheme} token block`);
  const body = STYLESHEET.slice(start + selector.length, STYLESHEET.indexOf("}", start));
  return new Map([...body.matchAll(/(--deck-[a-z0-9-]+):\s*([^;]+);/g)].map((m) => [m[1], m[2].trim()]));
}

test("the groups hold every colour the renderer honours, once each", () => {
  const grouped = [...CORE_COLOR_KEYS, ...CHART_SERIES_KEYS, ...BRAND_HUE_KEYS];
  assert.equal(new Set(grouped).size, grouped.length, "a key is offered twice");
  assert.deepEqual([...grouped].sort(), [...PALETTE_COLOR_KEYS].sort());
  assert.deepEqual(PALETTE_SWATCH_KEYS, grouped);
  for (const key of grouped) assert.ok(THEME_CONFIG_KEYS.includes(key), `${key} is not honoured by the renderer`);
});

test("every colour key has a readable caption, distinct from the others", () => {
  assert.deepEqual(Object.keys(PALETTE_KEY_LABELS).sort(), [...PALETTE_COLOR_KEYS].sort());
  const labels = Object.values(PALETTE_KEY_LABELS);
  assert.equal(new Set(labels).size, labels.length, "two keys share a caption");
  for (const [key, label] of Object.entries(PALETTE_KEY_LABELS)) {
    assert.ok(!label.includes("_"), `${key}: the caption is the raw key`);
  }
  // The form captions every field and names a fallback key by its caption.
  assert.match(EDITOR, /label=\{t_i18n\(PALETTE_KEY_LABELS\[key\]\)\}\s*name=\{key\}/);
  assert.match(EDITOR, /: t_i18n\(PALETTE_KEY_LABELS\[fallback\.source\]\),/);
  // The design system ColorPicker is labelled by the readable caption; the raw key is its helper line.
  assert.match(EDITOR, /<ColorPicker\s+label=\{label\}\s+helperText=\{<span className="font-mono-plex">\{name\}<\/span>\}/);
});

test("palette captions carry the colour meaning, not a generic catalog word", () => {
  // "Background" is a background process in es, "Lines" a row of text in ja:
  // the palette keeps its own colour strings for those words.
  for (const generic of ["Background", "Text", "Lines", "Accent", "Info"]) {
    assert.ok(!(Object.values(PALETTE_KEY_LABELS) as string[]).includes(generic), `caption reuses the generic "${generic}"`);
  }
});

test("a toned bullet's dot takes the text tone, as its marker and text do", () => {
  assert.match(STYLESHEET, /\.deck-list\[data-style="bullet"\] li\[data-tone\]::before \{ background: var\(--tone-text\); \}/);
  assert.doesNotMatch(STYLESHEET, /li\[data-tone\]::before \{ background: var\(--tone\); \}/);
});

test("the chart series are the six in rank order, the brand hues the four", () => {
  assert.deepEqual(CHART_SERIES_KEYS, ["chart_1", "chart_2", "chart_3", "chart_4", "chart_5", "chart_6"]);
  assert.deepEqual(BRAND_HUE_KEYS, ["brand_1", "brand_2", "brand_3", "brand_4"]);
  assert.ok(!PALETTE_COLOR_KEYS.some((key) => /font|file_id/.test(key)), "a type family or an asset id is not a colour");
});

test("an empty brand hue falls back to the chart series of the same rank", () => {
  assert.deepEqual(BRAND_HUE_KEYS.map(brandHueFallbackKey), ["chart_1", "chart_2", "chart_3", "chart_4"]);
  assert.equal(brandHueFallbackKey("accent"), null);
  assert.equal(brandHueFallbackKey("chart_1"), null);
});

test("the fallbacks the form shows are the stylesheet's, in both schemes", () => {
  // The first series follows the accent; the others keep a built-in colour.
  assert.equal(paletteFallbackKey("chart_1"), "accent");
  assert.deepEqual(CHART_SERIES_KEYS.slice(1).map(paletteFallbackKey), [null, null, null, null, null]);
  assert.deepEqual(BRAND_HUE_KEYS.map(paletteFallbackKey), ["chart_1", "chart_2", "chart_3", "chart_4"]);
  // A feedback tone as text is the tone itself until the palette names one.
  assert.deepEqual(
    (["success_text", "warning_text", "error_text"] as const).map(paletteFallbackKey),
    ["success", "warning", "error"],
  );
  for (const scheme of ["dark", "light"] as const) {
    const tokens = schemeTokens(scheme);
    for (const key of [...CHART_SERIES_KEYS, ...BRAND_HUE_KEYS]) {
      const declared = tokens.get(THEME_VARIABLES[key] as string);
      assert.ok(declared, `${scheme}: presentation.css declares no default for ${key}`);
      const next = paletteFallbackKey(key);
      if (next) assert.equal(declared, `var(${THEME_VARIABLES[next]})`, `${scheme}: ${key} falls back to ${next}`);
      else assert.doesNotMatch(declared, /var\(--deck-/, `${scheme}: ${key} takes another palette colour`);
    }
  }
});

test("an empty field shows the colour a deck draws instead", () => {
  // Down the chain: brand_1 -> chart_1 -> accent.
  assert.equal(resolvedPaletteColor("brand_1", { accent: " #123456 " }), "#123456");
  assert.equal(resolvedPaletteColor("brand_1", { accent: "#123456", chart_1: "#abcdef" }), "#abcdef");
  assert.equal(resolvedPaletteColor("chart_2", { accent: "#123456" }), null, "a built-in colour is not guessed");
  assert.deepEqual(paletteFallback("chart_1", { accent: "#42caff" }), { source: "accent", value: "#42caff" });
  assert.deepEqual(paletteFallback("brand_2", { chart_2: "#00f0bc" }), { source: "chart_2", value: "#00f0bc" });
  assert.deepEqual(paletteFallback("brand_1", { accent: "#42caff" }), { source: "chart_1", value: "#42caff" });
  assert.equal(paletteFallback("chart_2", { accent: "#42caff" }), null);
  assert.equal(paletteFallback("background", { background: "#000000" }), null);
  // A variant takes the base palette's own value first.
  assert.deepEqual(
    paletteFallback("chart_2", { chart_2: "#00f0bc" }, { chart_2: "#00f0bc" }),
    { source: "inherited", value: "#00f0bc" },
  );
  assert.deepEqual(
    paletteFallback("brand_2", { chart_2: "#009474", brand_2: "#a64dff" }, { brand_2: "#a64dff" }),
    { source: "inherited", value: "#a64dff" },
  );
});

test("a variant's fallbacks and preview read the preview's scheme rule, not a copy of it", () => {
  // `scheme_palette`: the base keeps its scheme-bound keys unless it reads as
  // the OTHER scheme - a base with no readable background hands them all over.
  assert.match(
    PREVIEW,
    /return \{ \.\.\.inheritedThemeForScheme\(template, scheme\), \.\.\.templateThemeVariant\(template, scheme\) \};/,
  );
  assert.match(PREVIEW, /if \(designedFor === null \|\| designedFor === scheme\) return base;/);
  assert.match(EDITOR, /const effective = previewThemeForDeck\(design, scheme\);/);
  assert.match(EDITOR, /const inherited = inheritedThemeForScheme\(design, scheme\);/);
  assert.match(EDITOR, /paletteFallback\(key, effective, inherited\), t_i18n\("Base palette"\)/);
  // The base palette previews what its decks draw.
  assert.match(EDITOR, /const baseEffective = previewThemeForDeck\(design, baseScheme\);/);
  assert.equal((EDITOR.match(/effective=\{baseEffective\}/g) ?? []).length, 1);
  assert.doesNotMatch(EDITOR, /SCHEME_BOUND_THEME_KEYS\.includes\(/, "the form strips the scheme-bound keys itself");
});

test("an empty base field shows what a variant stored for the base's own theme draws", () => {
  // A dark base with `theme_variants.dark.chart_1`: dark decks draw the
  // variant's chart_1, so an empty base chart_1 does not fall back to the accent.
  const shadow = { chart_1: "#ff0000", accent: "#00ff00" };
  const effective = { background: "#070d18", accent: "#00ff00", chart_1: "#ff0000" };
  assert.deepEqual(paletteFallback("chart_1", effective, shadow), { source: "inherited", value: "#ff0000" });
  // A key the variant does not set falls down the chain in the palette decks read.
  assert.deepEqual(paletteFallback("brand_1", effective, shadow), { source: "chart_1", value: "#ff0000" });
  assert.match(EDITOR, /paletteFallback\(key, baseEffective, baseShadow\), t_i18n\("Variant palette"\)/);
  assert.match(EDITOR, /<BasePaletteFields[\s\S]*?fallbackFor=\{baseFallbackFor\}/);
});

test("the series preview paints the palette background as a deck does, behind the theme guard", () => {
  const preview = EDITOR.match(/\nfunction SeriesPreview[\s\S]*?\r?\n}\r?\n/)?.[0] ?? "";
  // `background`, not `backgroundColor`: a gradient background shows.
  assert.match(preview, /const background = trimThemeValue\(str\(palette\.background\)\);/);
  assert.match(preview, /background && isSafeThemeValue\("background", background\) \? \{ background \} : undefined/);
  assert.match(preview, /style=\{paper\}/);
  assert.doesNotMatch(preview, /backgroundColor: background/);
});

test("a base field takes its key back from a variant stored for the base's own theme", () => {
  const metadata = {
    supported_schemes: ["dark"],
    theme_variants: { dark: { chart_1: "#ff0000", accent: "#ffffff" }, light: { background: "#ffffff" } },
  };
  assert.deepEqual(withoutVariantKey(metadata, "dark", "chart_1"), {
    supported_schemes: ["dark"],
    theme_variants: { dark: { accent: "#ffffff" }, light: { background: "#ffffff" } },
  });
  assert.deepEqual(metadata.theme_variants.dark, { chart_1: "#ff0000", accent: "#ffffff" }, "the input is left as it was");
  // An emptied variant goes, and so does an emptied `theme_variants`.
  assert.deepEqual(
    withoutVariantKey({ theme_variants: { dark: { chart_1: "#ff0000" }, light: { accent: "#0015a8" } } }, "dark", "chart_1"),
    { theme_variants: { light: { accent: "#0015a8" } } },
  );
  assert.deepEqual(withoutVariantKey({ deck_defaults: {}, theme_variants: { dark: { chart_1: "#ff0000" } } }, "dark", "chart_1"), {
    deck_defaults: {},
  });
  // Nothing to take back: the very same object, so the form writes nothing.
  const untouched: Record<string, unknown>[] = [
    {},
    { theme_variants: { light: { chart_1: "#ff0000" } } },
    { theme_variants: { dark: { accent: "#ffffff" } } },
    { theme_variants: "dark" },
    { theme_variants: { dark: ["chart_1"] } },
  ];
  for (const metadataWithout of untouched) assert.equal(withoutVariantKey(metadataWithout, "dark", "chart_1"), metadataWithout);
  // The form names such a variant's keys and writes every base field through it.
  assert.match(EDITOR, /const baseScheme = baseSchemeOf\(design\);/);
  assert.match(EDITOR, /return paletteScheme\(previewThemeConfig\(design\.theme_config\)\) \?\? templateSupportedSchemes\(design\)\[0\];/);
  assert.match(EDITOR, /const baseShadow = templateThemeVariant\(design, baseScheme\);/);
  assert.equal((EDITOR.match(/shadowed=\{shadowed\}/g) ?? []).length, 1, "the base palette card names the shadowed keys");
});

test("a base edit takes the key back from the variant of the theme the EDITED palette is drawn for", () => {
  const metadata = {
    supported_schemes: ["dark", "light"],
    theme_variants: { dark: { chart_1: "#ff0000" }, light: { background: "#ffffff", chart_1: "#0015a8" } },
  };
  assert.deepEqual(withBaseKeyTakenBack(metadata, "dark", "chart_1"), {
    supported_schemes: ["dark", "light"],
    theme_variants: { light: { background: "#ffffff", chart_1: "#0015a8" } },
  });
  // The background decides that theme, and a colour typed key by key passes
  // through readings of the other one (`#1a2` reads light on the way to
  // `#1a2230`): whatever the reading, a background edit takes nothing back,
  // so the light variant keeps its own background.
  for (const scheme of ["dark", "light"]) assert.equal(withBaseKeyTakenBack(metadata, scheme, "background"), metadata);
  // The theme is read from the edited palette, never from the one before the edit.
  assert.match(
    EDITOR,
    /const setBaseKey = [\s\S]*?const editedScheme = baseSchemeOf\(\{ theme_config: next, extra_metadata: extraMetadata \}\);\s*const unshadowed = withBaseKeyTakenBack\(extraMetadata, editedScheme, key\);/,
  );
  assert.doesNotMatch(EDITOR, /withoutVariantKey\(/, "a base edit goes through withBaseKeyTakenBack");
  // The notice says background is the exception whenever the variant sets it.
  assert.match(EDITOR, /shadowed\.keys\.includes\("background"\)\s*\? t_i18n\("[^"]*except background, which decides this palette's theme/);
});

test("the base palette is ONE card, first, whatever theme its background reads as", () => {
  // Typed key by key, a background passes through values that read as the
  // other theme (`#0a1` reads light on the way to `#0a1220`): a base card that
  // moved to another theme's card, or to the withdrawn-theme section, would
  // unmount the focused field and close an open colour picker.
  const render = EDITOR.slice(EDITOR.indexOf("export function TemplateDesignEditor("));
  assert.equal((render.match(/<BasePaletteFields\b/g) ?? []).length, 1, "one base palette card");
  const baseCard = render.indexOf("<BasePaletteFields");
  const variantCards = render.indexOf("{variantSchemes.map((scheme) => {");
  assert.ok(baseCard > 0 && variantCards > baseCard, "the base palette card comes first, outside the variant list");
  assert.match(render, /data-palette-scheme=\{baseUnsupported \? undefined : baseScheme\}/);
  assert.match(
    render,
    /const variantSchemes = supported\.filter\(\(scheme\) => paletteSlotFor\(scheme, designedFor, supported\) === "variant"\);/,
  );
  assert.doesNotMatch(
    render,
    /\.map\(\(scheme\) => \{\s*const slot = paletteSlotFor\(/,
    "no card switches between base and variant with the scheme its palette reads as",
  );
});

test("the form offers both groups in the base palette and in every variant", () => {
  const groups = EDITOR.match(/\nfunction PaletteGroups[\s\S]*?\r?\n}\r?\n/)?.[0] ?? "";
  assert.match(groups, /keys=\{CHART_SERIES_KEYS\}/);
  assert.match(groups, /keys=\{BRAND_HUE_KEYS\}/);
  assert.match(groups, /<SeriesPreview palette=\{effective\} \/>/);
  assert.match(EDITOR, /<PaletteGroups[\s\S]*?coreKeys=\{CORE_COLOR_KEYS\}/);
  assert.match(EDITOR, /<PaletteGroups[\s\S]*?coreKeys=\{VARIANT_CORE_KEYS\}/);
  // The lists come from palette-keys.ts, never spelled out in the form.
  assert.doesNotMatch(EDITOR, /"(chart|brand)_\d"/);
});

test("saving the form keeps every palette key it does not show", () => {
  assert.match(EDITOR, /const setBaseKey = [\s\S]*?const next = \{ \.\.\.themeConfig \};/);
  assert.match(EDITOR, /const setVariantKey = [\s\S]*?const palette = \{ \.\.\.\(variants\[scheme\] \?\? \{\}\) \};/);
});

test("the preview and the swatches read the same list", () => {
  assert.match(PREVIEW, /import \{ THEME_CONFIG_KEYS \} from "\.\/palette-keys";/);
  assert.doesNotMatch(PREVIEW, /"brand_1"/, "previewSpec.ts keeps a second copy of the list");
  assert.match(GENERATOR, /PALETTE_SWATCH_KEYS\.filter\(/);
  assert.doesNotMatch(GENERATOR, /const SWATCH_KEYS/);
});
