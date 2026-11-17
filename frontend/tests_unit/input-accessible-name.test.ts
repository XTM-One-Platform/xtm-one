// @vitest-environment node
import { readdir, readFile } from "node:fs/promises";
import path from "node:path";
import { describe, it, expect } from "vitest";

/**
 * Every `<Input>` carries a name the design system can see.
 *
 * The library's `Input` takes its accessible name from a `label` prop, an
 * `aria-label` or an `aria-labelledby`, and warns in development when it has
 * none. A placeholder is not a name - it disappears as soon as the field has a
 * value - and a `<label htmlFor>` beside the field, though a name in the
 * browser, is invisible to the library's check, which then warns on every
 * render. About 340 of this app's inputs had neither of the three; they were
 * named from the label each already showed (`scripts/fds-migration/
 * name-inputs.mjs`, then by hand where the script would not guess).
 *
 * So this check reads the library's rule, not the browser's: no credit for an
 * `id`, a wrapping `<label>` or a `title`. A `{...spread}` counts when its
 * object is declared in the same file with an `aria-label` or
 * `aria-labelledby`, as the switch guard allows.
 *
 * Static, like the switch and icon-button guards: a field behind a permission
 * or a rare state never renders in a test, and source has no such blind spot.
 */

const SRC = path.resolve(__dirname, "../src");

async function* walk(dir: string): AsyncGenerator<string> {
  for (const entry of await readdir(dir, { withFileTypes: true })) {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) yield* walk(full);
    else if (full.endsWith(".tsx") && !full.includes(".test.")) yield full;
  }
}

/** End of the JSX opening tag at `start`, tracking braces and strings. */
function openingTagEnd(source: string, start: number): number {
  let depth = 0;
  let inString: string | null = null;
  for (let i = start; i < source.length; i++) {
    const c = source[i];
    if (inString) {
      if (c === inString) inString = null;
      continue;
    }
    if (c === '"' || c === "'" || c === "`") {
      inString = c;
      continue;
    }
    if (c === "{") depth += 1;
    else if (c === "}") depth -= 1;
    else if (c === ">" && depth === 0) return i;
  }
  return -1;
}

async function inputs(): Promise<{ total: number; unnamed: string[] }> {
  let total = 0;
  const unnamed: string[] = [];
  for await (const file of walk(SRC)) {
    const source = await readFile(file, "utf8");
    for (const match of source.matchAll(/<Input[\s/>]/g)) {
      total += 1;
      const end = openingTagEnd(source, match.index);
      if (end === -1) continue;
      const tag = source.slice(match.index, end + 1);
      if (/\slabel=|aria-label=|aria-labelledby=/.test(tag)) continue;
      const spread = tag.match(/\{\.\.\.(\w+)\}/)?.[1];
      if (spread) {
        const declared = source.match(
          new RegExp(`(?:const|let)\\s+${spread}\\s*[:=][^;]*?(?:"aria-label"|"aria-labelledby")`, "s"),
        );
        if (declared) continue;
      }
      unnamed.push(`${path.relative(SRC, file)}:${source.slice(0, match.index).split("\n").length}`);
    }
  }
  return { total, unnamed };
}

describe("Input accessible names", () => {
  it("gives every Input a label, an aria-label or an aria-labelledby", async () => {
    const { unnamed } = await inputs();
    expect(unnamed, `inputs the design system sees no name on:\n  ${unnamed.join("\n  ")}`).toEqual([]);
  });

  it("checks a meaningful number of inputs, so a broken matcher cannot pass", async () => {
    expect((await inputs()).total).toBeGreaterThan(300);
  });
});
