/**
 * The resolved light and dark values of the design-system tokens, read from the
 * installed `@filigran/design-system` (`dist/tokens/theme.css`).
 *
 * The stylesheet declares primitives in `@theme` (`--red-400: #f14337`), the
 * semantic aliases in `:root` (dark by default), `.light` and `.dark`
 * (`--color-feedback-error-primary: var(--red-400)`), and derived tokens as
 * `color-mix(...)` over other tokens. A reader who needs the colour a token
 * paints (a UX mock, a contrast check) had to follow those `var()` chains by
 * hand through a 60 KB generated file; `resolveTokens` follows them for each
 * scheme, so a token answers its final value, or the `color-mix` expression
 * with its operands resolved.
 *
 * A scheme is the cascade `@theme` < `:root` < `.light` | `.dark`; the
 * `.layer-N` blocks (elevation context) are left out: they only alias tokens
 * the schemes already declare.
 */

/** Blocks a scheme reads, lowest precedence first. */
const SCHEMES = {
  light: ["@theme", ":root", ".light"],
  dark: ["@theme", ":root", ".dark"],
};

/**
 * The custom-property declarations of each top-level block of `css`, keyed by
 * its selector (`@theme`, `:root`, `.light`...). Comments are dropped; a nested
 * block (an `@utility` body) is not a token block and is skipped.
 */
export function tokenBlocks(css) {
  const text = css.replace(/\/\*[\s\S]*?\*\//g, "");
  const blocks = {};
  const opener = /(^|\n)([^{}\n][^{}]*?)\s*\{/g;
  let match;
  while ((match = opener.exec(text))) {
    const selector = match[2].trim();
    let depth = 1;
    let i = opener.lastIndex;
    const start = i;
    for (; i < text.length && depth > 0; i++) {
      if (text[i] === "{") depth++;
      else if (text[i] === "}") depth--;
    }
    opener.lastIndex = i;
    if (selector.startsWith("@utility")) continue;
    const body = text.slice(start, i - 1);
    const decls = (blocks[selector] ??= {});
    for (const [, name, value] of body.matchAll(/(--[A-Za-z0-9_-]+)\s*:\s*([^;]+);/g)) {
      decls[name] = value.trim();
    }
  }
  return blocks;
}

/**
 * `{ token: { light, dark } }` for every token the schemes declare, each value
 * with its `var(--x)` references replaced by their own resolved values. A
 * reference to an unknown token, or a cycle, stays as written.
 */
export function resolveTokens(css) {
  const blocks = tokenBlocks(css);
  const result = {};
  for (const [scheme, selectors] of Object.entries(SCHEMES)) {
    const decls = Object.assign({}, ...selectors.map((s) => blocks[s] ?? {}));
    const seen = new Map();
    const resolve = (name, stack) => {
      if (seen.has(name)) return seen.get(name);
      if (!(name in decls) || stack.has(name)) return `var(${name})`;
      stack.add(name);
      const value = decls[name].replace(/var\(\s*(--[A-Za-z0-9_-]+)\s*(?:,[^)]*)?\)/g, (_, ref) => resolve(ref, stack));
      stack.delete(name);
      seen.set(name, value);
      return value;
    };
    for (const name of Object.keys(decls)) {
      if (decls[name] === "initial") continue;
      (result[name] ??= {})[scheme] = resolve(name, new Set());
    }
  }
  return result;
}

/** The rows of `resolved` whose token name matches `filter` (a RegExp), sorted. */
export function tokenRows(resolved, filter) {
  return Object.keys(resolved)
    .filter((name) => !filter || filter.test(name))
    .sort()
    .map((name) => ({ token: name, light: resolved[name].light ?? "", dark: resolved[name].dark ?? "" }));
}
