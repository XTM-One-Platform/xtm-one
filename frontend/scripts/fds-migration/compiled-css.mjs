/**
 * The selectors a build emits, and the design-system classes that never got one.
 *
 * `verify-classes.mjs` used to read them from `dist/assets` only, so the check
 * needed `yarn build` (`tsc -b && vite build`) first: minutes, and in a sandbox
 * without a recent build it was skipped, the push went out, and CI's
 * "Design-system guards" step went red one round later on a class the library
 * never emits. `compileStylesheets` produces the same CSS without the bundle:
 * the Tailwind entry (`src/index.css`) through the PostCSS plugin `yarn build`
 * uses (`postcss.config.js`), with the frontend directory as its base, plus
 * every other stylesheet under `src` as written (Vite copies those, Tailwind
 * never touches them). Seconds instead of a build.
 */
import { readdir, readFile } from "node:fs/promises";
import path from "node:path";

/** Class shapes that are the design system's, and must therefore resolve. */
const DESIGN_SYSTEM_SHAPED =
  /^(?:[a-z-]+:)*(?:bg|text|border|ring|ring-offset|outline|fill|stroke|divide|from|via|to|shadow|font|rounded|placeholder|caret|decoration)-(?:elevation|default|negative|input|alert|feedback|filigran|entities|accent|scrim|overlay|gradient|tooltip|sans|mono|focus)[a-z0-9-]*$|^layer-[0-3]$|^(?:title-(?:jumbo|2xl|xl|lg|md|sm|xs)|content-(?:base|compact|caption|highlight|button|code)(?:-(?:bold|medium|link))?)$/;

async function* stylesheets(dir) {
  for (const entry of await readdir(dir, { withFileTypes: true })) {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) yield* stylesheets(full);
    else if (path.extname(entry.name) === ".css") yield full;
  }
}

/**
 * The CSS a build of the frontend at `frontend` would emit, without building it.
 *
 * @param {string} frontend the frontend directory (where `src/index.css` and `node_modules` are)
 * @returns {Promise<string[]>} the compiled entry, then every other stylesheet under `src`
 */
export async function compileStylesheets(frontend = process.cwd()) {
  const { default: postcss } = await import("postcss");
  const { default: tailwind } = await import("@tailwindcss/postcss");
  const entry = path.resolve(frontend, "src/index.css");
  const compiled = await postcss([tailwind({ base: frontend, optimize: false })]).process(await readFile(entry, "utf8"), {
    from: entry,
  });
  const sheets = [compiled.css];
  for await (const file of stylesheets(path.resolve(frontend, "src"))) {
    if (file !== entry) sheets.push(await readFile(file, "utf8"));
  }
  return sheets;
}

/**
 * Every class name a selector of `sheets` defines, with its bare forms.
 *
 * @param {Iterable<string>} sheets CSS texts
 * @returns {Set<string>}
 */
export function selectorsOf(sheets) {
  const selectors = new Set();
  for (const css of sheets) {
    // Tailwind escapes `/`, `[`, `:` and friends in selectors; only the leading
    // identifier segment is needed to answer "did a rule get emitted".
    for (const [, selector] of css.matchAll(/\.((?:\\.|[\w-])+)/g)) {
      const unescaped = selector.replace(/\\/g, "");
      selectors.add(unescaped);
      // A utility used with an opacity modifier compiles to
      // `.text-accent\/30`, so record the base name too — otherwise every
      // token only ever used with a modifier reads as missing.
      const slash = unescaped.indexOf("/");
      const withoutModifier = slash === -1 ? unescaped : unescaped.slice(0, slash);
      selectors.add(withoutModifier);
      // Variants are part of the selector as well
      // (`.focus-visible\:ring-accent\/50`), and the lookup is done on the bare
      // utility, so register that form too.
      selectors.add(withoutModifier.replace(/^(?:[a-z-]+:)+/, ""));
    }
  }
  return selectors;
}

/**
 * The design-system-shaped classes of `used` that no selector of `built` defines, most used first.
 *
 * @param {Map<string, string[]>} used class token to the files it appears in
 * @param {Set<string>} built what {@link selectorsOf} returned
 * @returns {{ token: string, count: number, example: string }[]}
 */
export function unresolvedClasses(used, built) {
  const byToken = new Map();
  for (const [token, files] of used) {
    // Only judge classes that claim to be part of the design language. A variant
    // prefix (`hover:`) is stripped: the emitted selector carries the base name.
    const bare = token.replace(/^(?:[a-z-]+:)+/, "").replace(/\/.*$/, "");
    if (!DESIGN_SYSTEM_SHAPED.test(bare)) continue;
    // `--bg-elevation-default-layer-0` and friends are CSS custom properties the
    // library reads, not classes; the leading dashes are lost when a literal like
    // `var(--bg-…)` is split, so the remainder looks exactly like a utility.
    // Layer-suffixed elevation names are only ever variables.
    if (/-layer-[0-3]$/.test(bare)) continue;
    if (built.has(bare)) continue;
    const seen = byToken.get(bare);
    byToken.set(bare, { token: bare, count: (seen?.count ?? 0) + files.length, example: seen?.example ?? files[0] });
  }
  return [...byToken.values()].sort((a, b) => b.count - a.count);
}
