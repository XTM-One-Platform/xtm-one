// @vitest-environment node
import { readdir, readFile } from "node:fs/promises";
import path from "node:path";
import { describe, it, expect } from "vitest";

/**
 * XTM One mode paints info notices in the product primary, and they still read at AA.
 *
 * The design system draws every neutral notice from three alert tokens: the Snackbar's info
 * wash, rule and icon (every info and default toast, the "A new version is available"
 * notice), the dashboard info notices and the inline info callouts (`bg-alert-info
 * border-alert-info`). index.css repoints those three at the purple scale in XTM One mode
 * and leaves the `--color-feedback-info-*` family to the library, because charts, swatches,
 * legends and the flow builder use it as a category hue that must stay distinct from the
 * accent. This test reads the purple steps index.css chose and the library's own tokens, then
 * computes on the page surface (`--bg-elevation-default-layer-0`) and the card surface
 * (`layer-1`), in both themes: the toast title (`--text-alert`), description
 * (`--text-default-secondary`) and a callout's `text-default-primary` on the composited wash
 * at 4.5:1, the icon and rule at 3:1. It also holds that success, warning and error are left
 * alone, and that no inline callout paints its text in the category hue.
 */

const FRONTEND = path.resolve(__dirname, "..");
const APP_CSS = path.join(FRONTEND, "src/index.css");
const THEME = path.join(FRONTEND, "node_modules/@filigran/design-system/dist/tokens/theme.css");

type Rgb = [number, number, number];
type Scope = Record<string, string>;

function block(css: string, selector: string): Scope {
  const start = css.indexOf(`\n${selector} {`);
  expect(start, `${selector} is no longer declared`).toBeGreaterThan(-1);
  const end = css.indexOf("\n}", start);
  return Object.fromEntries(
    [...css.slice(start, end).matchAll(/--([\w-]+):\s*([^;]+);/g)].map(([, name, value]) => [name, value.trim()]),
  );
}

/** The declarations of the rule whose selector list (comma-separated lines) starts with `first`. */
function ruleStartingWith(css: string, first: string): { selectors: string; scope: Scope } {
  const start = css.indexOf(`\n${first}`);
  expect(start, `index.css no longer has a rule starting with ${first}`).toBeGreaterThan(-1);
  const open = css.indexOf("{", start);
  const end = css.indexOf("\n}", open);
  return {
    selectors: css.slice(start, open),
    scope: Object.fromEntries(
      [...css.slice(open, end).matchAll(/--([\w-]+):\s*([^;]+);/g)].map(([, name, value]) => [name, value.trim()]),
    ),
  };
}

const hex = (value: string): Rgb => [0, 2, 4].map((i) => parseInt(value.replace("#", "").slice(i, i + 2), 16)) as Rgb;
const blend = (colour: Rgb, over: Rgb, share: number): Rgb => colour.map((c, i) => c * share + over[i] * (1 - share)) as Rgb;

function resolve(value: string, scope: Scope, primitives: Scope, over: Rgb): Rgb {
  const v = value.trim();
  const variable = v.match(/^var\(--([\w-]+)\)$/);
  if (variable) {
    const name = variable[1];
    if (primitives[name]) return hex(primitives[name]);
    expect(scope[name], `--${name} is not declared`).toBeDefined();
    return resolve(scope[name], scope, primitives, over);
  }
  const mix = v.match(/^color-mix\(in srgb,\s*(.+?)\s+(\d+(?:\.\d+)?)%,\s*transparent\)$/);
  if (mix) return blend(resolve(mix[1], scope, primitives, over), over, Number(mix[2]) / 100);
  if (v.startsWith("#")) return hex(v);
  throw new Error(`cannot resolve ${v}`);
}

function luminance([r, g, b]: Rgb): number {
  const linear = (c: number) => {
    const s = c / 255;
    return s <= 0.03928 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
  };
  return 0.2126 * linear(r) + 0.7152 * linear(g) + 0.0722 * linear(b);
}

function contrast(a: Rgb, b: Rgb): number {
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x);
  return (hi + 0.05) / (lo + 0.05);
}

async function themes() {
  const theme = await readFile(THEME, "utf8");
  const app = await readFile(APP_CSS, "utf8");
  const primitives: Scope = Object.fromEntries(
    [...theme.matchAll(/--([a-z]+-\d+):\s*(#[0-9a-fA-F]{6})/g)].map(([, name, value]) => [name, value]),
  );
  const root = block(theme, ":root");
  const dark = ruleStartingWith(app, '[data-theme="xtm-one"],\n.dark[data-theme="xtm-one"]');
  const light = ruleStartingWith(app, '.light[data-theme="xtm-one"]');
  return {
    primitives,
    dark,
    light,
    copilot: { dark: { ...root, ...block(theme, ".dark") }, light: { ...root, ...block(theme, ".light") } },
    xtm: {
      dark: { ...root, ...block(theme, ".dark"), ...dark.scope },
      light: { ...root, ...block(theme, ".light"), ...light.scope },
    },
  };
}

const SURFACES = ["var(--bg-elevation-default-layer-0)", "var(--bg-elevation-default-layer-1)"];
const ALERT_KEYS = ["bg-alert-info", "border-alert-info", "icon-info"];

describe("info notices in XTM One mode", () => {
  it("repoint the info alert tokens at the purple scale, and nothing else", async () => {
    const { dark, light } = await themes();
    for (const rule of [dark, light]) {
      expect(Object.keys(rule.scope).sort()).toEqual([...ALERT_KEYS].sort());
      for (const value of Object.values(rule.scope)) expect(value).toMatch(/var\(--purple-\d00\)/);
    }
    // The nested forms cover a forced dark or light subtree (deck stage, flow canvas).
    expect(dark.selectors).toContain(':where([data-theme="xtm-one"]) .dark');
    expect(light.selectors).toContain(':where([data-theme="xtm-one"]) .light');
  });

  it("are purple, not the library blue", async () => {
    const { primitives, xtm } = await themes();
    for (const scope of Object.values(xtm)) {
      const [r, , b] = resolve("var(--icon-info)", scope, primitives, [0, 0, 0]);
      expect(b, "blue channel").toBeGreaterThan(r);
      expect(r, "red channel: purple, not cyan").toBeGreaterThan(100);
    }
  });

  it("leave the info category hue distinct from the accent", async () => {
    const { primitives, xtm, copilot } = await themes();
    for (const mode of ["dark", "light"] as const) {
      const hue = resolve("var(--color-feedback-info-primary)", xtm[mode], primitives, [0, 0, 0]);
      const library = resolve("var(--color-feedback-info-primary)", copilot[mode], primitives, [0, 0, 0]);
      const icon = resolve("var(--icon-info)", xtm[mode], primitives, [0, 0, 0]);
      expect(hue, `--color-feedback-info-primary (${mode}) is the library's`).toEqual(library);
      expect(hue, `the category hue (${mode}) differs from the notice purple`).not.toEqual(icon);
    }
  });

  for (const mode of ["dark", "light"] as const) {
    it(`read at AA on the wash (${mode})`, async () => {
      const { primitives, xtm } = await themes();
      const scope = xtm[mode];
      for (const surfaceToken of SURFACES) {
        const surface = resolve(surfaceToken, scope, primitives, [0, 0, 0]);
        const wash = resolve("var(--bg-alert-info)", scope, primitives, surface);
        const title = resolve("var(--text-alert)", scope, primitives, wash);
        const description = resolve("var(--text-default-secondary)", scope, primitives, wash);
        const callout = resolve("var(--text-default-primary)", scope, primitives, wash);
        const icon = resolve("var(--icon-info)", scope, primitives, wash);
        const rule = resolve("var(--border-alert-info)", scope, primitives, wash);
        expect(contrast(title, wash), `title on the wash over ${surfaceToken}`).toBeGreaterThanOrEqual(4.5);
        expect(contrast(description, wash), `description on the wash over ${surfaceToken}`).toBeGreaterThanOrEqual(4.5);
        expect(contrast(callout, wash), `callout text on the wash over ${surfaceToken}`).toBeGreaterThanOrEqual(4.5);
        expect(contrast(icon, wash), `icon on the wash over ${surfaceToken}`).toBeGreaterThanOrEqual(3);
        expect(contrast(rule, surface), `rule on ${surfaceToken}`).toBeGreaterThanOrEqual(3);
      }
    });
  }

  it("paint the wash the token holds: the bg-alert-info utility reads --bg-alert-info", async () => {
    // The library utility paints the category family, not the token; without this
    // override the purple repoint never reaches the wash (UX round 2 of #12001).
    const theme = await readFile(THEME, "utf8");
    const library = theme.match(/@utility bg-alert-info \{\s*background-color:\s*([^;]+);/);
    expect(library, "the library no longer ships @utility bg-alert-info").not.toBeNull();
    const app = await readFile(APP_CSS, "utf8");
    const override = app.match(/\[data-theme="xtm-one"\] \.bg-alert-info \{\s*background-color:\s*([^;]+);/);
    expect(override, "index.css no longer scopes bg-alert-info to XTM One mode").not.toBeNull();
    expect(override![1].trim()).toBe("var(--bg-alert-info)");
    // Copilot mode keeps the library utility: the override is scoped, never global.
    expect(app).not.toMatch(/(^|\n|,)\s*\.bg-alert-info\s*[{,]/);
  });

  it("leave success, warning and error to the library", async () => {
    const app = await readFile(APP_CSS, "utf8");
    const xtmRules = [...app.matchAll(/data-theme="xtm-one"[^{]*\{([^}]*)\}/g)].map(([, body]) => body).join("\n");
    expect(xtmRules).not.toMatch(/--color-feedback-(success|warning|error|alert)-/);
    expect(xtmRules).not.toMatch(/--(bg|border)-alert-(success|warning|error)/);
  });

  it("inline info callouts use the alert tokens, not the category hue", async () => {
    const files = (await readdir(path.join(FRONTEND, "src"), { recursive: true })).filter(
      (file) => file.endsWith(".tsx") && !file.endsWith(".test.tsx"),
    );
    const offenders: string[] = [];
    for (const file of files) {
      const source = await readFile(path.join(FRONTEND, "src", file), "utf8");
      for (const [className] of source.matchAll(/className="[^"]*"/g)) {
        // A callout is a padded box; a badge or chip on the hue is a category, left alone.
        const callout = /\b(p|px)-3\b/.test(className);
        if (callout && className.includes("bg-feedback-info-primary/10") && className.includes("border-feedback-info-primary/20")) {
          offenders.push(file);
        }
      }
    }
    expect(offenders, "a callout on the info hue: use border-alert-info bg-alert-info").toEqual([]);
  });
});
