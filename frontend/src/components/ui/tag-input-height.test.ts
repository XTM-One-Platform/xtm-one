/** TagInput shares the Input's height and focus ring, set in the component.
 *
 *  A Tags field sits beside a Name `Input` on every create and detail form. The
 *  box used to be 40 px (`min-h-10`), 41 px with chips on the `min-h-8` detail
 *  callers, next to a 36 px Input, and drew its focus with no offset plus an
 *  accent border where the Input draws a 2 px offset then a 2 px ring. The
 *  height and the ring now live in `tag-input.tsx`; no page passes a height.
 */
import { describe, expect, it } from "vitest";
import { readdirSync, readFileSync, statSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
const SRC = join(HERE, "..", "..");
const component = readFileSync(join(HERE, "tag-input.tsx"), "utf8");

/** Every `<TagInput ...>` element of a source, read up to its self-closing `/>`
 *  (or its closing tag), so an arrow-function prop such as
 *  `onChange={(v) => ...}` does not end the match at the arrow's `>`. */
function tagInputElements(text: string): string[] {
  return text.match(/<TagInput\b[\s\S]*?(?:\/>|<\/TagInput>)/g) ?? [];
}

/** The elements of a source whose `className` (a string or an expression) sets a height. */
function tagInputHeightOffenders(text: string): string[] {
  return tagInputElements(text).filter((tag) =>
    (tag.match(/className=(?:"[^"]*"|\{[^}]*\})/g) ?? []).some((cls) => /\b(min-)?h-\d/.test(cls)),
  );
}

function tsxFiles(dir: string): string[] {
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name);
    if (statSync(path).isDirectory()) return tsxFiles(path);
    return name.endsWith(".tsx") && !name.endsWith(".test.tsx") ? [path] : [];
  });
}

describe("TagInput height and focus match the Input", () => {
  it("uses the Input's 36 px height as its base, with a chip row that fits inside", () => {
    expect(component).toContain('"flex min-h-9 flex-wrap items-center');
    expect(component).toContain("px-3 py-1 content-base");
    expect(component).not.toMatch(/\bmin-h-10\b/);
    expect(component).toContain("content-compact leading-4 font-medium");
    expect(component).not.toContain("text-[11px]");
  });

  it("gives the chip remove button a 20 px target", () => {
    expect(component).toContain("inline-flex h-5 w-5 shrink-0 items-center justify-center");
  });

  it("draws the Input's focus ring: 2 px offset, 2 px ring, no accent border", () => {
    expect(component).toContain("focus-within:ring-2 focus-within:ring-focus focus-within:ring-offset-2");
    expect(component).not.toContain("focus-within:border-accent");
  });

  it("is given no height by any page", () => {
    const offenders = tsxFiles(SRC).flatMap((path) => {
      const text = readFileSync(path, "utf8");
      return tagInputHeightOffenders(text).map((tag) => `${path.slice(SRC.length)}: ${tag}`);
    });
    expect(offenders).toEqual([]);
  });

  it("catches a height set after an arrow-function prop, on one line or several", () => {
    const oneLine = '<TagInput value={tags} onChange={(v) => setTags(v)} className="h-8" />';
    const multiLine = [
      "<TagInput",
      "  value={tags}",
      "  onChange={(next) => {",
      "    setTags(next);",
      "  }}",
      '  className={cn("min-h-10", wide && "w-full")}',
      "/>",
    ].join("\n");
    expect(tagInputHeightOffenders(oneLine)).toHaveLength(1);
    expect(tagInputHeightOffenders(multiLine)).toHaveLength(1);
  });

  it("leaves a caller without a height alone", () => {
    const clean = '<TagInput value={tags} onChange={(v) => setTags(v)} className="w-full" />\n<Input className="h-8" />';
    expect(tagInputElements(clean)).toHaveLength(1);
    expect(tagInputHeightOffenders(clean)).toEqual([]);
  });
});
