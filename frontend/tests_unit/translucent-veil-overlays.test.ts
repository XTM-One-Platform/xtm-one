// @vitest-environment node
import { readFile, readdir } from "node:fs/promises";
import path from "node:path";
import { describe, it, expect } from "vitest";
// @ts-expect-error - a plain .mjs build script, typed by its JSDoc only
import { rules } from "../scripts/fds-migration/token-map.mjs";

/**
 * An overlay drawn over content the user must still see never paints the
 * opaque page surface.
 *
 * The design-system migration mapped `bg-background/<n>` to
 * `bg-elevation-default`, an `@utility` with no opacity modifier: every
 * translucent veil became the page colour and blanked what it covered - the
 * /flows map behind the agent drawer, the arcade playfield, the chat
 * under the drop zone, a disabled catalog card, a video poster on hover.
 * A veil dims with `bg-scrim` or keeps a translucent page surface
 * (`color-mix(... var(--bg-elevation-default) N%, transparent)`); a clear
 * backdrop is `bg-transparent`.
 */

const FRONTEND = path.resolve(__dirname, "..");
const SRC = path.join(FRONTEND, "src");

type Rule = { from: RegExp; to: string | ((...args: string[]) => string) };

function migrate(input: string): string {
  let out = input;
  for (const rule of rules as Rule[]) {
    out = out.replace(rule.from, (...args: string[]) =>
      typeof rule.to === "function" ? rule.to(...args) : rule.to.replace(/\$(\d)/g, (_, i) => args[Number(i)]),
    );
  }
  return out;
}

async function* tsxFiles(dir: string): AsyncGenerator<string> {
  for (const entry of await readdir(dir, { withFileTypes: true })) {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) yield* tsxFiles(full);
    else if (entry.name.endsWith(".tsx") && !entry.name.endsWith(".test.tsx")) yield full;
  }
}

describe("translucent veils", () => {
  it("the codemod keeps a translucent page surface translucent", () => {
    const out = migrate('className="absolute inset-0 bg-background/50 backdrop-blur-sm"');
    expect(out).toContain("bg-[color-mix(in_srgb,var(--bg-elevation-default)_50%,transparent)]");
    expect(out).not.toMatch(/\bbg-elevation-default\b(?!\))/);
  });

  it("the codemod still maps the opaque page surface onto the elevation token", () => {
    expect(migrate('className="bg-background p-4"')).toBe('className="bg-elevation-default p-4"');
  });

  it("no full-surface overlay paints the opaque page colour", async () => {
    // An inset-0 element that sits ON TOP of content (absolute/fixed) with the
    // bare page surface is the bug class; a layered surface (`layer-N
    // bg-elevation-default`) is a card or panel, not a veil.
    const overlay = /\b(?:absolute|fixed) inset-0\b[^"'`]*(?<!layer-\d )\bbg-elevation-default\b(?![-/)\]])/;
    const offenders: string[] = [];
    for await (const file of tsxFiles(SRC)) {
      const source = await readFile(file, "utf8");
      source.split("\n").forEach((line, index) => {
        if (overlay.test(line)) offenders.push(`${path.relative(FRONTEND, file)}:${index + 1}`);
      });
    }
    // PreviewStage's loading cover hides an iframe that has not painted yet:
    // there is nothing under it to keep in view, so it stays opaque.
    const allowed = new Set(["src/components/video-studio/editor/PreviewStage.tsx"]);
    expect(offenders.filter((where) => !allowed.has(where.split(":")[0]))).toEqual([]);
  });
});
