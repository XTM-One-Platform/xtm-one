// @vitest-environment node
import { readdir, readFile } from "node:fs/promises";
import path from "node:path";
import { describe, it, expect } from "vitest";

/**
 * Every `<DialogContent>` writes its `<DialogTitle>` in its own JSX.
 *
 * The title is the dialog's accessible name: without one a screen reader
 * announces "dialog" and nothing else. The design system checks for it in
 * development, but by walking the JSX tree passed to `DialogContent` - a title
 * rendered by a child component (`<DialogContent><MyForm/></DialogContent>`,
 * with the title inside `MyForm`) is invisible to that walk, and warns although
 * the dialog is named. Two dialogs were written that way; their titles now sit
 * in the dialog itself, which is what both the library and this check read.
 *
 * Static, like the switch and icon-button guards: a dialog behind a permission
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

async function dialogs(): Promise<{ total: number; untitled: string[] }> {
  let total = 0;
  const untitled: string[] = [];
  for await (const file of walk(SRC)) {
    const source = await readFile(file, "utf8");
    for (const match of source.matchAll(/<DialogContent[\s>]/g)) {
      total += 1;
      const close = source.indexOf("</DialogContent>", match.index);
      const body = close === -1 ? source.slice(match.index) : source.slice(match.index, close);
      if (!/<DialogTitle[\s>]/.test(body)) {
        untitled.push(`${path.relative(SRC, file)}:${source.slice(0, match.index).split("\n").length}`);
      }
    }
  }
  return { total, untitled };
}

describe("Dialog titles", () => {
  it("writes a DialogTitle inside every DialogContent", async () => {
    const { untitled } = await dialogs();
    expect(untitled, `dialogs with no DialogTitle in their own JSX:\n  ${untitled.join("\n  ")}`).toEqual([]);
  });

  it("checks a meaningful number of dialogs, so a broken matcher cannot pass", async () => {
    expect((await dialogs()).total).toBeGreaterThan(90);
  });
});
