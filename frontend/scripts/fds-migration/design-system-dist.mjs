/**
 * Where the design system's compiled bundle sits, and which of its files carry
 * classes.
 *
 * The dependency has had two shapes: the published `@filigran/design-system`
 * package, whose `dist/` is at its root, and a git checkout of the monorepo,
 * which nests it under `packages/filigran-design-system/`. Resolving both keeps
 * this tooling working whichever one is installed, instead of failing with
 * "library bundle not found" the moment the dependency is re-pointed.
 *
 * The published package also ships `*.meta.mjs` next to each component: its
 * documentation metadata, whose prose ("keyboard-reachable", "min-width", a
 * spec file's path) reads as class-shaped text to a scanner and produced 466
 * phantom "library classes". Only the component modules carry real classes.
 */
import { existsSync } from "node:fs";
import path from "node:path";

const CANDIDATES = [
  "node_modules/@filigran/design-system/dist",
  "node_modules/@filigran/design-system/packages/filigran-design-system/dist",
];

/** Absolute path to the installed bundle, or `null` when nothing is installed. */
export function designSystemDist(from = ".") {
  for (const candidate of CANDIDATES) {
    const full = path.resolve(from, candidate);
    if (existsSync(full)) return full;
  }
  return null;
}

/** Whether a bundle file is component code rather than documentation metadata. */
export function carriesClasses(file) {
  return !path.basename(file).endsWith(".meta.mjs");
}

const CLASS_CHARACTERS = /^[a-z@[][a-zA-Z0-9:_\-/[\]().%,#&.*+~<>=?!]*$/;

/**
 * Whether a token from a component module's string literals could be a class.
 *
 * Wide on purpose (a missed class is an invisible control, an extra token costs
 * nothing), but not so wide that prose and addresses get in: component modules
 * carry an SVG namespace URL and the odd sentence too, and a safelist that holds
 * `http://www.w3.org/2000/svg` or `client-side.` reads as noise to every
 * reviewer of it. A class starts lower-case (`Read-only` is a word), has no
 * scheme, never ends in sentence punctuation and balances its parentheses.
 */
export function plausibleClass(token) {
  if (token.length < 2 || token.length > 90) return false;
  if (!token.includes("-") && !token.includes(":")) return false;
  if (!CLASS_CHARACTERS.test(token)) return false;
  if (token.includes("://") || /[.,;:]$/.test(token)) return false;
  return token.split("(").length === token.split(")").length;
}
