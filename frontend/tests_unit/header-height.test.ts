// @vitest-environment node
import { readFile, readdir } from "node:fs/promises";
import path from "node:path";
import { describe, it, expect } from "vitest";
import { HEADER_HEIGHT, NAVBAR_WIDTH_COLLAPSED, NAVBAR_WIDTH_EXPANDED } from "../src/components/layout/layout-metrics";

/**
 * `HEADER_HEIGHT` must equal the height the design system's `Header` actually
 * renders at.
 *
 * Raised in review, and it is the exact failure class `layout-metrics.ts` exists
 * to prevent. The app publishes this number into `--fds-header-height`, which
 * the library's Header reads — so the two agree by construction *while the
 * number is right*. If the library changes its own default and this constant
 * stays at 68, nothing breaks loudly: the bar, the content offset and the
 * assistant panel all keep using 68 and simply stop matching the design.
 *
 * The library's Header sizes itself as
 *
 *     h-[var(--fds-header-height,--spacing(17))]
 *
 * so its default is discoverable without a browser: the fallback in that
 * expression, times the spacing scale. A rendered measurement would need real
 * layout, which jsdom does not do — this reads the intent from the source
 * instead, which is what would drift.
 */

const DIST = path.resolve(
  __dirname,
  "../node_modules/@filigran/design-system/dist",
);

/** The `--spacing` step the library's theme sets, in px. */
async function spacingStepPx(): Promise<number> {
  const css = await readFile(path.join(DIST, "index.css"), "utf8");
  const match = css.match(/--spacing:\s*([\d.]+)rem/);
  expect(match, "the library's stylesheet no longer declares --spacing").not.toBeNull();
  return Number(match![1]) * 16;
}

/** The header height the library falls back to when the host publishes none. */
async function* bundleModules(dir: string): AsyncGenerator<string> {
  // The published package splits the bundle into `dist/components/<name>/`,
  // where the monorepo checkout kept it flat, and ships `*.meta.mjs`
  // documentation modules beside each component. Walk for the component code.
  for (const entry of await readdir(dir, { withFileTypes: true })) {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) yield* bundleModules(full);
    else if (entry.name.endsWith(".mjs") && !entry.name.endsWith(".meta.mjs")) yield full;
  }
}

async function libraryHeaderHeightPx(): Promise<number> {
  for await (const file of bundleModules(DIST)) {
    const source = await readFile(file, "utf8");
    const match = source.match(/h-\[var\(--fds-header-height,\s*--spacing\((\d+(?:\.\d+)?)\)\)\]/);
    if (match) return Number(match[1]) * (await spacingStepPx());
  }
  throw new Error(
    "could not find the Header's own height expression in the design-system bundle — " +
      "if the library changed how it sizes the header, this guard needs rewriting rather than deleting",
  );
}

describe("header height", () => {
  it("matches the design system's own default", async () => {
    expect(HEADER_HEIGHT).toBe(await libraryHeaderHeightPx());
  });
});

/**
 * The navbar widths the app offsets its content column by
 * (`NAVBAR_WIDTH_EXPANDED` / `_COLLAPSED`), read the same way: the Navbar
 * sizes itself `collapsed ? "w-<n>" : "w-<m>"`, a step count of the spacing
 * scale. A library change there would otherwise slide the whole content
 * column under the rail, or leave a gap beside it, with nothing failing.
 */
async function libraryNavbarWidthsPx(): Promise<{ collapsed: number; expanded: number }> {
  for await (const file of bundleModules(DIST)) {
    if (!/navbar/i.test(path.basename(path.dirname(file)))) continue;
    const source = await readFile(file, "utf8");
    const match = source.match(/collapsed\s*\?\s*"w-(\d+(?:\.\d+)?)"\s*:\s*"w-(\d+(?:\.\d+)?)"/);
    if (match) {
      const step = await spacingStepPx();
      return { collapsed: Number(match[1]) * step, expanded: Number(match[2]) * step };
    }
  }
  throw new Error(
    "could not find the Navbar's own width expression in the design-system bundle — " +
      "if the library changed how it sizes the navbar, this guard needs rewriting rather than deleting",
  );
}

describe("navbar widths", () => {
  it("match the design system's own widths, collapsed and expanded", async () => {
    const widths = await libraryNavbarWidthsPx();
    expect({ collapsed: NAVBAR_WIDTH_COLLAPSED, expanded: NAVBAR_WIDTH_EXPANDED }).toEqual(widths);
  });
});
