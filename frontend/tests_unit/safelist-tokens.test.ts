// @vitest-environment node
import { readFile } from "node:fs/promises";
import path from "node:path";
import { describe, it, expect } from "vitest";
// @ts-expect-error - a plain .mjs build script, typed by its JSDoc only
import { plausibleClass } from "../scripts/fds-migration/design-system-dist.mjs";

/**
 * The safelist holds classes, not the prose the library's bundle also carries.
 *
 * It is generated from every class-shaped string in the bundle and carried
 * forward between runs, so whatever a scan once let in stayed: figma links, a
 * backlog file's name, sentence fragments - 1 577 entries, about 700 of them
 * words. `plausibleClass` is the one filter both `generate-safelist.mjs` and
 * `verify-classes.mjs` read; this holds what it refuses and what it must keep,
 * and that the committed list passes it.
 */

const FRONTEND = path.resolve(__dirname, "..");

describe("safelist tokens", () => {
  it.each([
    "group-data-[state=checked]:bg-filigran-brand-primary-transparency-50",
    "size-5",
    "[&>*]:min-w-0",
    "hover:bg-elevation-hover",
    "w-[calc(100%-2rem)]",
    "bg-[#0F1A2B]",
    "@md:flex-row",
    "p-4!",
  ])("keeps the class %s", (token) => {
    expect(plausibleClass(token)).toBe(true);
  });

  it.each([
    ["a URL", "http://www.w3.org/2000/svg"],
    ["a figma link", "https://www.figma.com/design/abc?node-id=1-2"],
    ["a word ending a sentence", "client-side."],
    ["a word before a colon", "NavbarItem:"],
    ["a capitalised word", "Read-only"],
    ["a TLP marking", "TLP:AMBER"],
    ["an unbalanced parenthesis", "linear-gradient(to"],
    ["a closing fragment", "RULE-01)."],
    ["a lone word", "button"],
  ])("refuses %s", (_label, token) => {
    expect(plausibleClass(token)).toBe(false);
  });

  it("the committed safelist holds nothing the filter refuses", async () => {
    const entry = await readFile(path.join(FRONTEND, "src/index.css"), "utf8");
    const block = entry.slice(entry.indexOf("/* fds-safelist:start"), entry.indexOf("/* fds-safelist:end */"));
    const tokens = [...block.matchAll(/@source inline\("([^"]+)"\)/g)].flatMap(([, list]) => list.split(/\s+/).filter(Boolean));
    expect(tokens.length).toBeGreaterThan(0);
    expect(tokens.filter((token) => !plausibleClass(token))).toEqual([]);
  });
});
