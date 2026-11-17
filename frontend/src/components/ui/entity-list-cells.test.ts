/**
 * Shared list cells - `yarn test`.
 *
 * The type and icon sizes of these cells follow the design-system component
 * they render (Badge, Chip, Icon own theirs); the product sets no floor of its
 * own above the library's scale (dev-docs/reference/frontend-design-system.md,
 * "Order of authority"). What stays guarded here is what the library does not
 * decide: the Tags column width and the full text of a truncated cell.
 */
import { expect, test } from "vitest";
import { readFileSync } from "node:fs";
import { join } from "node:path";

// vitest runs from frontend/ (CI and `yarn test`).
const SRC = join(process.cwd(), "src");

/**
 * At its natural width the Tags column pushed the /skills table past its card
 * (914 px in 778 at 1024; UX r1 B1 on #10517): a list whose Tags sits beside a
 * long Name keeps it at 120 px, the rest folded into "+N", as /agents does.
 */
const CAPPED_TAGS_FILES = [join("pages", "Skills.tsx"), join("pages", "Agents.tsx")];
const TAGS_COLUMN = /key: "tags",[^}]*?className: "[^"]*\bw-\[120px\] max-w-\[120px\]/;

test.each(CAPPED_TAGS_FILES)("%s caps its Tags column at 120 px", (file) => {
  const source = readFileSync(join(SRC, file), "utf8");
  expect(source).toMatch(TAGS_COLUMN);
});

/** A truncated card name or description carries its full text (UX r1 B2 on #10517). */
test("Skills.tsx card name and description carry their full text as a title", () => {
  const source = readFileSync(join(SRC, "pages", "Skills.tsx"), "utf8");
  expect(source).toContain("title={skill.name}");
  expect(source).toContain("title={skill.description || undefined}");
});

/** The list Compatibility badge stops at 160 px: its full text sits in a Tip (UX r2 B1 on #10517). */
test("Skills.tsx list Compatibility badge carries its full text in a Tip", () => {
  const source = readFileSync(join(SRC, "pages", "Skills.tsx"), "utf8");
  expect(source).toMatch(/<Tip content=\{s\.compatibility\}>\s*<Badge[^>]*max-w-\[160px\]/);
});
