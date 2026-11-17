/**
 * Finding the visible label of a form control in JSX source, shared by the
 * scripts that name controls from it (`name-switches.mjs`, `name-inputs.mjs`).
 *
 * Both reuse the label's own expression as the control's `aria-label`: no new
 * message id, a name that cannot drift from the visible text, and WCAG 2.5.3
 * ("Label in Name") satisfied by construction. Both refuse to guess - a site
 * whose label cannot be told is reported for a human.
 */
import { readdir } from "node:fs/promises";
import path from "node:path";

/** How far back to look for the row's label. Rows are small; 40 lines is slack. */
export const LOOKBACK_LINES = 40;
/** Forward is tighter: a label after the control is always immediately after. */
export const LOOKFORWARD_LINES = 6;

export async function* walk(dir) {
  for (const entry of await readdir(dir, { withFileTypes: true })) {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) yield* walk(full);
    else if (full.endsWith(".tsx")) yield full;
  }
}

/**
 * End index of the JSX opening tag starting at `start`, tracking braces and
 * strings so a `>` inside an expression or a string does not end it early.
 */
export function openingTagEnd(src, start) {
  let depth = 0;
  let inString = null;
  for (let i = start; i < src.length; i++) {
    const c = src[i];
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

/**
 * True when the switch sits inside a `<label>`. Those are ALREADY named: a
 * `<button>` is a labelable element, so the label's own text names the first
 * labelable thing inside it. Counting them as unnamed was the first thing this
 * script got wrong, and it would have added a duplicate name on top of a
 * working one.
 */
export function insideLabelElement(src, switchStart) {
  const before = src.slice(0, switchStart);
  const opens = (before.match(/<label[\s>]/g) ?? []).length;
  const closes = (before.match(/<\/label>/g) ?? []).length;
  return opens > closes;
}

/**
 * The label text inside an element's body, as JSX expression source. The body
 * is not always one clean expression: a label may be plain text, or an icon
 * followed by text, or several expressions. A `t_i18n(…)` call wins when there
 * is one, because that is the translated user-facing string; otherwise a single
 * expression or a single text run is unambiguous, and anything else is not
 * something to guess at.
 */
export function labelFromBody(body) {
  const withoutTags = body.replace(/<[^>]*>/g, " ");
  const expressions = [...withoutTags.matchAll(/\{([^{}]*(?:\{[^{}]*\}[^{}]*)*)\}/g)].map((m) => m[1].trim());
  const translated = expressions.filter((e) => e.startsWith("t_i18n("));
  if (translated.length === 1) return translated[0];
  if (expressions.length === 1) return expressions[0];
  if (expressions.length === 0) {
    const text = withoutTags.replace(/\s+/g, " ").trim();
    // A bare text label. Untranslated in the source, so it stays untranslated
    // here — matching the visible text is the point, and fixing the missing
    // translation is a separate change.
    if (text && !text.includes("{")) return JSON.stringify(text);
  }
  return null;
}

/**
 * The visible label for the switch at `switchStart`, as JSX expression source,
 * or null when it cannot be told. Two shapes carry it in this codebase:
 *
 *   - a paragraph in the same row, marked `font-medium` — the help text below it
 *     is not, which is the only thing separating the two;
 *   - an `<InfoRow label={…}>` wrapping the control, where the label is a prop
 *     and there is no paragraph at all.
 */
/**
 * `strict` (the input namer) accepts only a `<label>` or an emphasised element
 * as the label: a form field's neighbours are far more often help text, an
 * empty state or a section heading than a switch row's are, and the lenient
 * fallback to the nearest text named a URL field after a "Company-wide"
 * toggle above it.
 */
export function labelExpression(src, switchStart, switchEnd, tag = "<Switch", { strict = false } = {}) {
  const before = src.slice(0, switchStart);
  // Never look back past the PREVIOUS switch: a label on the far side of it
  // belongs to that row, not this one. Without this bound, three consecutive
  // SSO toggles all claimed the name of the first one — the kind of wrong that
  // a proposal table shows you only if you read every line of it.
  const previousSwitch = before.lastIndexOf(tag);
  const lines = before.split("\n");
  const lookbackStart = lines.slice(0, Math.max(0, lines.length - LOOKBACK_LINES)).join("\n").length;
  const windowStart = Math.max(previousSwitch + 1, lookbackStart);
  const backward = before.slice(windowStart);

  const after = src.slice(switchEnd);
  const nextSwitch = after.indexOf(tag);
  const forwardLines = after.split("\n").slice(0, LOOKFORWARD_LINES).join("\n");
  const forward = after.slice(0, Math.min(nextSwitch === -1 ? Infinity : nextSwitch, forwardLines.length));

  // <p>, <span> and <label> all carry labels here, and a label may run over
  // several lines or hold an icon beside its text. (A <label> in this codebase
  // is styled text without a `for`, which is why it needs this treatment at all
  // rather than simply naming the control the way a real label would.)
  const ELEMENT = strict
    ? /<(p|span|label|Label)(\s+(?:htmlFor=(?:"[^"]*"|\{[^{}]*\})\s+)?className=("[^"]*"|\{(?:[^{}]|\{[^{}]*\})*\}))?\s*>([\s\S]*?)<\/\1>/g
    : /<(p|span|label)(\s+className=("[^"]*"|\{(?:[^{}]|\{[^{}]*\})*\}))?\s*>([\s\S]*?)<\/\1>/g;
  // Every string literal in the className, not just one: the label's class is
  // routinely built with `cn("content-base font-medium", active && "text-accent")`,
  // and a lazy read of that expression finds the wrong string.
  const isLabelClass = (className) =>
    [...(className ?? "").matchAll(/"([^"]*)"/g)].some((m) => m[1].includes("font-medium"));

  /**
   * Candidates from both directions, ranked by DISTANCE to the control rather
   * than by direction. Direction-first ordering picked a section heading eight
   * lines up over the word sitting against the switch; proximity is the rule
   * that matches how someone reads the row.
   */
  const candidates = [];
  for (const m of backward.matchAll(ELEMENT)) {
    candidates.push({ distance: backward.length - (m.index + m[0].length), body: m[4], className: m[3], emphasised: isLabelClass(m[3]), element: m[1] });
  }
  // A field's label is written BEFORE it; reading forward named fields after
  // the label of the next one.
  for (const m of strict ? [] : forward.matchAll(ELEMENT)) {
    candidates.push({ distance: m.index, body: m[4], className: m[3], emphasised: isLabelClass(m[3]), element: m[1] });
  }
  candidates.sort((a, b) => a.distance - b.distance);

  // An emphasised label wins over a nearer unemphasised one only when both sit
  // in the same row; `font-medium` marks the label where a row has both a label
  // and help text, and help text is always the nearer of the two.
  // Strict: the NEAREST label-like element, emphasis or not - an emphasised
  // section heading above a plain `<label>Name *</label>` is not the field's
  // label.
  const emphasisedFirst = strict
    ? candidates
    : [...candidates].sort((a, b) => Number(b.emphasised) - Number(a.emphasised) || a.distance - b.distance);
  for (const candidate of emphasisedFirst) {
    if (strict && !candidate.emphasised && candidate.element.toLowerCase() !== "label") continue;
    const expression = labelFromBody(candidate.body);
    if (expression) return expression;
  }

  const infoRows = [...backward.matchAll(/<InfoRow[^>]*\slabel=\{([^{}]*(?:\{[^{}]*\}[^{}]*)*)\}/g)];
  const row = infoRows.at(-1);
  if (row && !row[1].includes("<")) return row[1].trim();

  return null;
}

