/**
 * The template form's pattern inputs leave room for their character counter
 * (every counter input does).
 *
 * The design-system Input draws its "n/200" counter under the field, inside
 * the wrapper that receives `className`. A fixed height on that wrapper
 * (`h-8`, 32 px for a 36 px field) lets the counter spill onto the help line
 * or the next label: 8.5 px of overlap at 1440 and 1024 in the UX lab. Every
 * input of the editor that shows a counter (a `maxLength`) therefore carries
 * no fixed height in its `className`.
 */
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

// vitest runs from frontend/; under jsdom `import.meta.url` is not a file: URL,
// so the editor is found from the working directory.
const EDITOR = readFileSync(
  join(process.cwd(), "src", "components", "presentation", "TemplateDesignEditor.tsx"),
  "utf8",
);

/** Every `<Input ... />` element of the editor, as written in the source. */
function inputElements(source: string): string[] {
  return source.match(/<Input\b[\s\S]*?\/>/g) ?? [];
}

describe("pattern inputs of the template form", () => {
  const patternInputs = inputElements(EDITOR).filter((el) => el.includes("maxLength={MAX_PATTERN_CHARS}"));

  it("finds the footer, header and right footer pattern inputs", () => {
    expect(patternInputs.length).toBe(3);
    for (const id of ["template-chrome-footer", "template-chrome-header", "template-chrome-footer-right"]) {
      expect(patternInputs.some((el) => el.includes(`id="${id}"`))).toBe(true);
    }
  });

  it("gives the wrapper no fixed height, so the counter stays above the next line", () => {
    for (const el of patternInputs) {
      expect(fixedHeights(el)).toEqual([]);
    }
  });
});

/** The fixed height classes (`h-8`, `h-[34px]`) of an element's `className`. */
function fixedHeights(el: string): string[] {
  const className = el.match(/className="([^"]*)"/)?.[1] ?? "";
  return className.split(/\s+/).filter((token) => /^h-\S+$/.test(token));
}

describe("every counter input of the template editor", () => {
  const counterInputs = inputElements(EDITOR).filter((el) => /\bmaxLength=/.test(el));

  it("finds the pattern inputs and the four variable row inputs", () => {
    expect(counterInputs.length).toBeGreaterThanOrEqual(7);
    for (const field of ["key", "label", "hint", "default"]) {
      expect(counterInputs.some((el) => el.includes(`"Variable {n} ${field}"`))).toBe(true);
    }
  });

  it("gives none of them a fixed height", () => {
    const offenders = counterInputs.filter((el) => fixedHeights(el).length > 0);
    expect(offenders).toEqual([]);
  });

  it("would catch a fixed height (the guard's own pattern)", () => {
    expect(fixedHeights('<Input className="h-8 content-compact" maxLength={32} />')).toEqual(["h-8"]);
    expect(fixedHeights('<Input className="font-mono-plex content-compact" maxLength={32} />')).toEqual([]);
  });
});
