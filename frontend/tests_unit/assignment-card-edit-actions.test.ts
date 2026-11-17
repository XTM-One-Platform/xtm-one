// @vitest-environment node
import { readdir, readFile } from "node:fs/promises";
import path from "node:path";
import { describe, it, expect } from "vitest";

/**
 * The card headers of the assignment detail page share one Edit / Cancel /
 * Save implementation, the shared components/ui/card-edit-actions (#9375,
 * UX rounds 1 and 2 B2 of #10976): default size, 16 px icons, Cancel before
 * the primary Save. No page keeps a local copy of it.
 *
 * Static source gate: inside a `<CardHeader>` of pages/assignment-detail, a
 * `<Button>` sized down (`h-7`, `content-compact` or `size="sm"`) fails, and
 * so does an Edit or Save button written by hand instead of CardEditActions.
 */

const DIR = path.resolve(__dirname, "../src/pages/assignment-detail");
const SRC = path.resolve(__dirname, "../src");
const SHARED = "components/ui/card-edit-actions.tsx";

function headers(source: string): string[] {
  return [...source.matchAll(/<CardHeader\b[\s\S]*?<\/CardHeader>/g)].map((m) => m[0]);
}

function buttons(block: string): string[] {
  return [...block.matchAll(/<Button\b[^>]*>/g)].map((m) => m[0]);
}

describe("assignment detail card edit actions", () => {
  it("no header button is sized down and no Edit/Save is hand-written", async () => {
    const files = (await readdir(DIR)).filter((f) => f.endsWith(".tsx") && !f.endsWith(".test.tsx"));
    const offenders: string[] = [];
    for (const file of files) {
      const source = await readFile(path.join(DIR, file), "utf8");
      for (const header of headers(source)) {
        for (const tag of buttons(header)) {
          if (/\bh-7\b|\bcontent-compact\b|size="sm"/.test(tag)) offenders.push(`${file}: ${tag}`);
        }
        if (/<Button\b[\s\S]*?t_i18n\("(Edit|Save)"\)/.test(header)) offenders.push(`${file}: hand-written Edit/Save, use CardEditActions`);
      }
    }
    expect(offenders).toEqual([]);
  });

  it("no second CardEditActions: the shared component is the only one", async () => {
    const offenders: string[] = [];
    const walk = async (dir: string): Promise<void> => {
      for (const entry of await readdir(dir, { withFileTypes: true })) {
        const full = path.join(dir, entry.name);
        if (entry.isDirectory()) {
          if (entry.name !== "node_modules") await walk(full);
          continue;
        }
        if (!/\.tsx?$/.test(entry.name) || /\.test\.tsx?$/.test(entry.name)) continue;
        const source = await readFile(full, "utf8");
        const rel = path.relative(SRC, full).split(path.sep).join("/");
        if (rel !== SHARED && /export function CardEditActions\b/.test(source)) offenders.push(`${rel}: second CardEditActions`);
        if (/from "\.\/CardEditActions"/.test(source)) offenders.push(`${rel}: imports ./CardEditActions`);
      }
    };
    await walk(SRC);
    expect(offenders).toEqual([]);
  });

  it("the gate sees a sized-down header button", () => {
    const sample = `<CardHeader><Button size="sm" className="h-7 content-compact">{t_i18n("Edit")}</Button></CardHeader>`;
    const [header] = headers(sample);
    expect(buttons(header).some((t) => /\bh-7\b/.test(t))).toBe(true);
  });
});
