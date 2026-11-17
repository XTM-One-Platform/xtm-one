/**
 * Form fields: error state - `yarn test`.
 *
 * The design system's `Input` (components/ui/input.tsx over
 * `@filigran/design-system`) and `Textarea` put their `className` on the
 * wrapper `div`, not on the field. A feedback tone written there
 * (`border-feedback-error-primary`, `focus-visible:ring-feedback-…`) paints a
 * 0 px border on a wrapper that never takes focus: the field keeps its own
 * border and the invalid state is not seen.
 *
 * The field's error state is `aria-invalid` on the field (announced). The
 * adapter in components/ui/input.tsx draws the invalid border from it, in one
 * strength, on every field (`invalidTone="alert"` for the amber variant): a call
 * site writes no feedback tone at all, on the wrapper or reached through it
 * with `[&_input]:` (19 hand-written copies had drifted to 4
 * strengths). The library's `error` prop stays available where its icon and
 * message are wanted. A raw `<input>`, `<textarea>` or `<select>` that sets
 * `aria-invalid` takes `INVALID_FIELD_CLASS` (components/ui/invalid-field.ts)
 * and no tone of its own either. Amber is drawn with the warning token: the
 * alert token measured 1.72:1 on the light card. Rule:
 * "Form fields: error state" in dev-docs/reference/frontend-design-system.md.
 */
import { test } from "vitest";
import assert from "node:assert/strict";
import { readdirSync, readFileSync } from "node:fs";
import { join, relative, sep } from "node:path";

// vitest runs from frontend/ (CI and `yarn test`), as no-hand-rolled-menu.test.ts.
const SRC = join(process.cwd(), "src");

function* sourceFiles(dir: string): Generator<string> {
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const full = join(dir, entry.name);
    if (entry.isDirectory()) {
      if (entry.name !== "node_modules") yield* sourceFiles(full);
    } else if (entry.name.endsWith(".tsx")) {
      yield full;
    }
  }
}

/** The design-system fields, and the raw fields that announce an invalid state. */
const DESIGN_SYSTEM_FIELD = /<(Input|Textarea)\b/g;
const RAW_FIELD = /<(input|textarea|select)\b/g;

/** The JSX opening tags a pattern opens in a source, with their line. */
function fieldTags(source: string, opening: RegExp = DESIGN_SYSTEM_FIELD): { line: number; tag: string }[] {
  const tags: { line: number; tag: string }[] = [];
  opening.lastIndex = 0;
  for (let match = opening.exec(source); match; match = opening.exec(source)) {
    let depth = 0;
    let end = match.index + match[0].length;
    for (; end < source.length; end += 1) {
      const char = source[end];
      if (char === "{") depth += 1;
      else if (char === "}") depth -= 1;
      else if (char === ">" && depth === 0 && source[end - 1] !== "=") break;
    }
    tags.push({ line: source.slice(0, match.index).split("\n").length, tag: source.slice(match.index, end + 1) });
  }
  return tags;
}

/** The feedback tones a tag's `className` writes itself, on the wrapper or reached through it. */
function siteFeedbackTokens(tag: string): string[] {
  const at = tag.search(/\bclassName=/);
  if (at < 0) return [];
  return tag.slice(at).match(/[^\s"'`{}()]*(?:border|ring|outline)-feedback-[\w/.-]*/g) ?? [];
}

test("the detector finds a site tone, on the wrapper or through it", () => {
  assert.deepEqual(siteFeedbackTokens('<Input className={cn("h-8", bad && "border-feedback-error-primary/60")} />'), [
    "border-feedback-error-primary/60",
  ]);
  assert.deepEqual(siteFeedbackTokens('<Input className="focus-visible:ring-feedback-error-primary/30" />'), [
    "focus-visible:ring-feedback-error-primary/30",
  ]);
  assert.deepEqual(
    siteFeedbackTokens(
      '<Input aria-invalid className="[&_input]:border-feedback-error-primary [&_input:focus-visible]:ring-feedback-error-primary/30" />',
    ),
    ["[&_input]:border-feedback-error-primary", "[&_input:focus-visible]:ring-feedback-error-primary/30"],
  );
  assert.deepEqual(siteFeedbackTokens('<Textarea className="[&_textarea]:border-feedback-alert-primary" />'), [
    "[&_textarea]:border-feedback-alert-primary",
  ]);
  assert.deepEqual(siteFeedbackTokens('<Input aria-invalid invalidTone="alert" className="h-8 content-compact" />'), []);
  assert.equal(fieldTags('<Input onKeyDown={(e) => e.key === "Enter"} className="x" />\n<input className="y" />').length, 1);
  assert.equal(fieldTags('<Input className="x" />\n<textarea aria-invalid className="y" />', RAW_FIELD).length, 1);
  assert.deepEqual(siteFeedbackTokens('<textarea aria-invalid={bad} className={cn(BASE, bad && "border-feedback-alert-primary")} />'), [
    "border-feedback-alert-primary",
  ]);
  assert.deepEqual(siteFeedbackTokens('<textarea aria-invalid={bad} className={cn(BASE, INVALID_FIELD_CLASS.alert)} />'), []);
});

test("the adapter is the only source of a field's invalid border", () => {
  const adapter = readFileSync(join(SRC, "components", "ui", "input.tsx"), "utf8");
  // One strength, full tone (3:1 on the card in both themes), drawn from aria-invalid.
  assert.match(adapter, /"\[&_input\[aria-invalid=true\]\]:border-feedback-error-primary[ "]/);
  // Amber is the warning token: the alert token (#F2BE3A) is 1.72:1 on the light card.
  assert.match(adapter, /"\[&_input\[aria-invalid=true\]\]:border-feedback-warning-primary[ "]/);
  assert.match(adapter, /\[&_input\[aria-invalid=true\]:hover\]:border-feedback-warning-primary/);
  assert.doesNotMatch(adapter, /border-feedback-alert-primary/);
  assert.doesNotMatch(adapter, /feedback-(?:error|warning|alert)-primary\/\d+/);
  const raw = readFileSync(join(SRC, "components", "ui", "invalid-field.ts"), "utf8");
  assert.match(raw, /"aria-\[invalid=true\]:border-feedback-error-primary"/);
  assert.match(raw, /"aria-\[invalid=true\]:border-feedback-warning-primary"/);
  assert.doesNotMatch(raw, /border-feedback-alert-primary|feedback-(?:error|warning|alert)-primary\/\d+/);
});

test("no design-system field takes a feedback tone of its own", () => {
  const offenders: string[] = [];
  for (const file of sourceFiles(SRC)) {
    const source = readFileSync(file, "utf8");
    const raw = fieldTags(source, RAW_FIELD).filter(({ tag }) => /\baria-invalid\b/.test(tag));
    for (const { line, tag } of [...fieldTags(source), ...raw]) {
      const tokens = siteFeedbackTokens(tag);
      if (tokens.length) offenders.push(`${relative(SRC, file).split(sep).join("/")}:${line} ${tokens.join(" ")}`);
    }
  }
  assert.deepEqual(
    offenders,
    [],
    `feedback tone written at the call site (set aria-invalid, and invalidTone="alert" for amber; the adapter draws the border, a raw field takes INVALID_FIELD_CLASS, see "Form fields: error state"):\n${offenders.join("\n")}`,
  );
});
