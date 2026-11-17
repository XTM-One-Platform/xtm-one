// @vitest-environment node
import { readFile } from "node:fs/promises";
import path from "node:path";
import { describe, it, expect } from "vitest";

/**
 * One visible help line per field (dev-docs/reference/frontend-design-system.md,
 * "Forms"; design standards section 2; #9375, UX round 1 B2 of #11058).
 *
 * A form field says what it means under the control, and the control points at
 * that line with aria-describedby so a screen reader reads it too. The forms
 * the #9375 slices have converted are listed in DESCRIBED: in each of them,
 * every form control (Input, Textarea, TagInput, SelectTrigger, SearchableSelect,
 * DateFilterInput) carries aria-describedby, and every id it names is rendered
 * by the file. The list only grows: a slice that converts a form adds its file.
 */

const SRC = path.resolve(__dirname, "../src");

const DESCRIBED: string[] = [
  "pages/integration-detail/openrouter/OpenRouterKeysTab.tsx",
  "pages/knowledge-base-detail/SourcesTab.tsx",
];

const CONTROL = /<(Input|Textarea|TagInput|SelectTrigger|SearchableSelect|DateFilterInput)[\s/>]/g;

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

/** The form controls of a source that point at no help line, by line number. */
function undescribed(source: string): number[] {
  const lines: number[] = [];
  for (const match of source.matchAll(CONTROL)) {
    const end = openingTagEnd(source, match.index);
    const tag = source.slice(match.index, end === -1 ? undefined : end + 1);
    if (!/\baria-describedby=/.test(tag)) lines.push(source.slice(0, match.index).split("\n").length);
  }
  return lines;
}

/** aria-describedby targets (static or template prefix) that no element of the file renders as an id. */
function danglingTargets(source: string): string[] {
  const ids = new Set<string>();
  for (const m of source.matchAll(/\bid=(?:"([^"]+)"|\{`([^`]+)`\})/g)) ids.add(m[1] ?? m[2]);
  const missing: string[] = [];
  for (const m of source.matchAll(/\baria-describedby=(?:"([^"]+)"|\{`([^`]+)`\})/g)) {
    const target = m[1] ?? m[2];
    if (!ids.has(target)) missing.push(target);
  }
  return missing;
}

describe("Form help line gate (#9375)", () => {
  for (const file of DESCRIBED) {
    it(`gives every field of ${file} a help line it points at`, async () => {
      const source = await readFile(path.join(SRC, file), "utf8");
      expect(undescribed(source), `controls with no aria-describedby (lines)`).toEqual([]);
      expect(danglingTargets(source), `aria-describedby ids no element renders`).toEqual([]);
    });
  }

  it("flags a control with no help line, and a help id nothing renders", () => {
    expect(undescribed('<Input aria-label="Name" value={v} />')).toEqual([1]);
    expect(undescribed('<Input aria-describedby="n-help" />\n<p id="n-help">x</p>')).toEqual([]);
    expect(danglingTargets('<Input aria-describedby="n-help" />')).toEqual(["n-help"]);
    expect(danglingTargets("<Input aria-describedby={`h-${k}`} />\n<p id={`h-${k}`}>x</p>")).toEqual([]);
  });
});
