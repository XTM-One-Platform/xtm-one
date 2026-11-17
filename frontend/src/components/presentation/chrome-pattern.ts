/*
Copyright (c) 2025-2026 Filigran SAS

This file is part of the XTM One Enterprise Edition ("EE") and is
licensed under the XTM One Enterprise Edition License (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
*/

/**
 * The footer / header a TEMPLATE defines, filled once from the deck.
 *
 * TypeScript half of `backend/app/services/presentation_chrome.py` — the same
 * placeholder syntax, the same built-ins, the same collapse rule, so the
 * footer the agent's tool result quotes is the footer the screen and the
 * exports draw. A template declares, in `extra_metadata.chrome`:
 *
 *     { "footer": "{{event}} - {{title}} - {{classification}}",
 *       "header": null,
 *       "variables": [{ "key": "classification", "label": "Classification",
 *                       "default": "Internal" }] }
 *
 * `{{title}}`, `{{subtitle}}` and `{{event}}` are always available and read
 * the deck itself (its title, the opening slide's subtitle, `chrome.event_label`);
 * every other placeholder must be one of the declared `variables`, filled from
 * the deck's `chrome.fields[key]` (its `default` otherwise). The values are
 * DECK settings: renaming the deck changes the footer on every slide, and
 * "mark it Confidential" is one field, never an edit of every slide.
 *
 * COLLAPSE RULE (`renderChromePattern`): an empty placeholder takes its
 * separator with it. The literal text before the first placeholder and after
 * the last one is kept as soon as ANY placeholder has a value; the text between
 * two placeholders is kept only when the placeholder on its left has a value
 * AND some placeholder to its right has one. `"{{event}} - {{title}}"` with no
 * event therefore reads `"title"`, not `" - title"`; a pattern whose every
 * placeholder is empty reads nothing at all, and a pattern with no
 * placeholder is printed as written.
 *
 * Without a template pattern the footer keeps reading `chrome.event_label` and
 * `chrome.footer_text` as separate segments.
 */
import type { DeckChrome, PresentationSpec, TemplateChrome, TemplateChromeVariable } from "@/types";
import { markupToPlainText } from "./markup";

/** Where a template keeps its declaration — `CHROME_KEY` on the backend. */
export const CHROME_KEY = "chrome";

/** The declaration's body-slide watermark switch — `WATERMARK_KEY` on the backend. */
export const WATERMARK_KEY = "watermark";

/** The declaration's corner-emblem switch — `EMBLEM_KEY` on the backend. */
export const EMBLEM_KEY = "emblem";

/** The footer's right-corner pattern — `FOOTER_RIGHT_KEY` on the backend. */
const FOOTER_RIGHT_KEY = "footer_right";
/** The emblem placement keys — `EMBLEM_SIZE_KEY` / `EMBLEM_ALIGN_KEY` on the backend. */
const EMBLEM_SIZE_KEY = "emblem_size";
const EMBLEM_ALIGN_KEY = "emblem_align";
const EMBLEM_ALIGNS = ["margin", "title"] as const;
/** How the emblem is inked — `EMBLEM_INK_KEY` on the backend. */
const EMBLEM_INK_KEY = "emblem_ink";
const EMBLEM_INKS = ["mark", "accent"] as const;
const EMBLEM_SIZE_MIN = 24;
const EMBLEM_SIZE_MAX = 96;

/** Read from the deck itself; never declared as variables. */
export const BUILTIN_PLACEHOLDERS = ["title", "subtitle", "event"] as const;
/** The placeholders only `footer_right` reads, filled per slide — `SLIDE_PLACEHOLDERS` on the backend. */
const SLIDE_PLACEHOLDERS = ["page", "year"] as const;
/** `{{key}}`, spaces inside the braces tolerated — `PLACEHOLDER_RE` on the backend. */
const PLACEHOLDER_RE = /\{\{\s*([A-Za-z0-9_]+)\s*\}\}/g;
/** Anything between a `{{` and the next `}}` — what the validator inspects. */
const BRACES_RE = /\{\{(.*?)\}\}/g;
/** A variable key: a lowercase identifier the pattern spells `{{key}}`. */
export const VARIABLE_KEY_RE = /^[a-z][a-z0-9_]{0,31}$/;

export const MAX_CHROME_VARIABLES = 8;
export const MAX_PATTERN_CHARS = 200;
export const MAX_VARIABLE_LABEL_CHARS = 60;
export const MAX_VARIABLE_HINT_CHARS = 160;
/** A field value is one short line — `MAX_LABEL_CHARS` of the spec. */
export const MAX_FIELD_VALUE_CHARS = 120;

const PATTERN_SLOTS = ["footer", "header"] as const;
type ChromeSlot = (typeof PATTERN_SLOTS)[number];

type Token = { kind: "lit"; text: string } | { kind: "ph"; key: string };

function tokenize(pattern: string): Token[] {
  const tokens: Token[] = [];
  let cursor = 0;
  for (const match of pattern.matchAll(PLACEHOLDER_RE)) {
    const start = match.index ?? 0;
    if (start > cursor) tokens.push({ kind: "lit", text: pattern.slice(cursor, start) });
    tokens.push({ kind: "ph", key: match[1] });
    cursor = start + match[0].length;
  }
  if (cursor < pattern.length) tokens.push({ kind: "lit", text: pattern.slice(cursor) });
  return tokens;
}

/** The placeholder names a pattern reads, in order of first appearance. */
function patternPlaceholders(pattern: string | null | undefined): string[] {
  const names: string[] = [];
  if (!pattern) return names;
  for (const match of pattern.matchAll(PLACEHOLDER_RE)) {
    if (!names.includes(match[1])) names.push(match[1]);
  }
  return names;
}

/** Every placeholder a declaration reads — footer first, then header. */
function chromePlaceholders(chrome: Pick<TemplateChrome, "footer" | "header">): string[] {
  const names = patternPlaceholders(chrome.footer);
  for (const name of patternPlaceholders(chrome.header)) {
    if (!names.includes(name)) names.push(name);
  }
  return names;
}

/**
 * Fill `pattern` with `values`; an empty value takes its separator with it —
 * `render_chrome_pattern` on the backend, rule for rule. Anything missing or
 * blank counts as empty; the result is trimmed.
 */
export function renderChromePattern(
  pattern: string | null | undefined,
  values: Record<string, string | null | undefined>,
): string {
  if (!pattern || !pattern.trim()) return "";
  const tokens = tokenize(pattern);
  const resolved = tokens.map((token) =>
    token.kind === "ph" ? (values[token.key] ?? "").trim() : null,
  );
  const placeholderPositions = tokens
    .map((token, index) => (token.kind === "ph" ? index : -1))
    .filter((index) => index >= 0);
  if (placeholderPositions.length === 0) return pattern.trim();
  if (!placeholderPositions.some((index) => resolved[index])) return "";
  const first = placeholderPositions[0];
  const last = placeholderPositions[placeholderPositions.length - 1];
  const out: string[] = [];
  tokens.forEach((token, index) => {
    if (token.kind === "ph") {
      out.push(resolved[index] ?? "");
      return;
    }
    if (index < first || index > last) {
      out.push(token.text);
      return;
    }
    // A separator: the placeholder right before it must have a value, and so
    // must one of the placeholders after it.
    const before = Math.max(...placeholderPositions.filter((position) => position < index));
    const after = placeholderPositions.some((position) => position > index && resolved[position]);
    if (resolved[before] && after) out.push(token.text);
  });
  return out.join("").trim();
}

// ── Reading a template's declaration (lenient, like `template_chrome`) ────────

function cleanString(value: unknown, limit: number): string | null {
  if (typeof value !== "string") return null;
  const text = value.trim();
  return text ? text.slice(0, limit) : null;
}

function readVariable(raw: unknown): TemplateChromeVariable | null {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return null;
  const item = raw as Record<string, unknown>;
  const key = typeof item.key === "string" ? item.key.trim().toLowerCase() : "";
  if (!VARIABLE_KEY_RE.test(key) || (BUILTIN_PLACEHOLDERS as readonly string[]).includes(key)) return null;
  const label =
    cleanString(item.label, MAX_VARIABLE_LABEL_CHARS) ??
    key.replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase());
  const variable: TemplateChromeVariable = { key, label };
  const hint = cleanString(item.hint, MAX_VARIABLE_HINT_CHARS);
  const fallback = cleanString(item.default, MAX_FIELD_VALUE_CHARS);
  if (hint) variable.hint = hint;
  if (fallback) variable.default = fallback;
  return variable;
}

/**
 * The footer / header a template declares in `extra_metadata.chrome`, or
 * `null` when it declares none — the READ side, which never throws and renders
 * what it can (a malformed variable is skipped, a duplicate key keeps its first
 * entry). What the deck editor previews for a template being TRIED before its
 * deck response exists; a saved deck reads `presentation.template_chrome`.
 */
export function templateChromeOf(
  template: { extra_metadata?: Record<string, unknown> | null } | null | undefined,
): TemplateChrome | null {
  const raw = template?.extra_metadata?.[CHROME_KEY];
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return null;
  const declaration = raw as Record<string, unknown>;
  const footer = cleanString(declaration.footer, MAX_PATTERN_CHARS);
  const header = cleanString(declaration.header, MAX_PATTERN_CHARS);
  const watermark = declaration[WATERMARK_KEY] === true;
  const emblem = declaration[EMBLEM_KEY] === true;
  const footerRight = cleanString(declaration[FOOTER_RIGHT_KEY], MAX_PATTERN_CHARS);
  const size = declaration[EMBLEM_SIZE_KEY];
  const align = declaration[EMBLEM_ALIGN_KEY];
  const ink = declaration[EMBLEM_INK_KEY];
  if (!footer && !header && !watermark && !emblem && !footerRight) return null;
  const variables: TemplateChromeVariable[] = [];
  if (Array.isArray(declaration.variables)) {
    for (const item of declaration.variables) {
      const variable = readVariable(item);
      if (variable && !variables.some((v) => v.key === variable.key)) variables.push(variable);
    }
  }
  const chrome: TemplateChrome = { footer, header, variables: variables.slice(0, MAX_CHROME_VARIABLES) };
  if (watermark) chrome.watermark = true;
  if (emblem) chrome.emblem = true;
  if (footerRight) chrome.footer_right = footerRight;
  if (typeof size === "number" && Number.isInteger(size) && size >= EMBLEM_SIZE_MIN && size <= EMBLEM_SIZE_MAX) {
    chrome.emblem_size = size;
  }
  if ((EMBLEM_ALIGNS as readonly unknown[]).includes(align)) chrome.emblem_align = align as "margin" | "title";
  if ((EMBLEM_INKS as readonly unknown[]).includes(ink)) chrome.emblem_ink = ink as "mark" | "accent";
  return chrome;
}

/** One line of the template's read-only "Header & footer" summary.
 *  `label` is an i18n key; `value` is shown as it is when `literal`, else it
 *  is an i18n key too. */
interface ChromeSummaryLine {
  label: string;
  value: string;
  literal: boolean;
  values?: Record<string, string | number>;
}

/** The summary's labels and values, read by `t_i18n(line.label)` / `t_i18n(line.value)`. @i18n-keys */
const CHROME_SUMMARY_KEYS = {
  footerRight: "Right footer",
  emblemSize: "Emblem size",
  emblemSizeValue: "{n} px",
  emblemAlign: "Emblem placement",
  alignTitle: "Level with the slide title",
  alignMargin: "In the top margin",
  emblemInk: "Emblem ink",
  inkAccent: "One colour, the slide accent",
  inkMark: "The mark's own colours",
} as const;

/** The chrome keys beyond the footer and header patterns: the right
 *  footer pattern, the emblem size, placement and ink, in that order. A key
 *  the template does not declare is left out. */
export function chromeExtraSummary(chrome: TemplateChrome | null | undefined): ChromeSummaryLine[] {
  if (!chrome) return [];
  const k = CHROME_SUMMARY_KEYS;
  const lines: ChromeSummaryLine[] = [];
  if (chrome.footer_right) lines.push({ label: k.footerRight, value: chrome.footer_right, literal: true });
  if (chrome.emblem_size) {
    lines.push({ label: k.emblemSize, value: k.emblemSizeValue, literal: false, values: { n: chrome.emblem_size } });
  }
  if (chrome.emblem_align) {
    lines.push({
      label: k.emblemAlign,
      value: chrome.emblem_align === "title" ? k.alignTitle : k.alignMargin,
      literal: false,
    });
  }
  if (chrome.emblem_ink) {
    lines.push({
      label: k.emblemInk,
      value: chrome.emblem_ink === "accent" ? k.inkAccent : k.inkMark,
      literal: false,
    });
  }
  return lines;
}

/** `{{page}}` and the one separator after it (`chromeFooterRight`). */
const PAGE_SLOT_RE = /\{\{\s*page\s*\}\}\s*[\u00b7|-]?\s*/g;

/**
 * The `{{page}}` value of slide *index*: two digits, counted from the
 * first slide after the cover - a deck opening on a `title` slide numbers its
 * second slide 01 - and empty when the deck hides its slide numbers.
 */
export function chromePage(
  slides: ReadonlyArray<{ layout: string }>,
  index: number,
  showNumbers: boolean,
): string {
  if (!showNumbers) return "";
  const page = slides[0]?.layout === "title" ? index : index + 1;
  return page > 0 ? String(page).padStart(2, "0") : "";
}

/**
 * What the footer's right corner reads on slide *index*: the
 * template's `footer_right` pattern filled with the deck's values, the slide's
 * `{{page}}` and the current `{{year}}` (computed when the slide is drawn, so a
 * PDF or PPTX export prints the year it was exported in). `null` when the
 * template declares none: the slide keeps its `n / total` number.
 */
export function chromeFooterRight(
  spec: Pick<PresentationSpec, "title" | "slides" | "chrome">,
  templateChrome: TemplateChrome | null | undefined,
  index: number,
  year: number = new Date().getFullYear(),
): string | null {
  const declared = templateChrome?.footer_right;
  if (!declared) return null;
  const page = chromePage(spec.slides ?? [], index, spec.chrome?.show_slide_numbers ?? true);
  // No page: the slot leaves with ITS separator only (a middle dot, a hyphen
  // or a pipe), so "{{page}} · © {{year}} ..." keeps its "©" - the generic
  // empty-placeholder rule would drop the whole literal up to {{year}}.
  const pattern = page ? declared : declared.replace(PAGE_SLOT_RE, "");
  const values = { ...chromeValues(spec, templateChrome), page, year: String(year) };
  return renderChromePattern(pattern, values);
}

/** Whether a template declaration draws a footer or header pattern (`declares_pattern`). */
export function declaresPattern(chrome: TemplateChrome | null | undefined): chrome is TemplateChrome {
  return !!chrome && !!(chrome.footer || chrome.header);
}

// ── Filling a pattern from a deck ────────────────────────────────────────────

/**
 * The `{{subtitle}}` value: the opening (title) slide's subtitle as PLAIN text.
 * The subtitle is inline markup drawn by the slide; the footer draws text, so
 * the markers are stripped (`markupToPlainText`, the tokenizer the backend's
 * `markup_to_plain_text_lenient` mirrors) and the line is folded onto one line.
 */
function openingSubtitle(spec: Pick<PresentationSpec, "slides">): string {
  for (const slide of spec.slides ?? []) {
    if (slide.layout === "title") {
      const subtitle = (slide as { subtitle?: unknown }).subtitle;
      if (typeof subtitle !== "string") return "";
      return markupToPlainText(subtitle).split(/\s+/).filter(Boolean).join(" ");
    }
  }
  return "";
}

/** The one spelling of a `chrome.fields` key — `canonical_field_key` on the backend. */
export function canonicalFieldKey(raw: string): string {
  return raw.trim().toLowerCase();
}

/** The deck's `chrome.fields` as a clean `{ key: value }` map (blanks dropped). */
function deckFields(chrome: DeckChrome | undefined): Record<string, string> {
  const out: Record<string, string> = {};
  for (const [key, value] of Object.entries(chrome?.fields ?? {})) {
    if (typeof value === "string" && value.trim()) out[canonicalFieldKey(key)] = value.trim();
  }
  return out;
}

/**
 * Every value a pattern may draw from the deck — `chrome_values` on the
 * backend: the built-ins from the deck itself, each declared variable from
 * `chrome.fields[key]` with the variable's `default` as fallback.
 * @public
 */
export function chromeValues(
  spec: Pick<PresentationSpec, "title" | "slides" | "chrome">,
  chrome: TemplateChrome | null | undefined,
): Record<string, string> {
  const values: Record<string, string> = {
    title: (spec.title ?? "").trim(),
    subtitle: openingSubtitle(spec),
    event: (spec.chrome?.event_label ?? "").trim(),
  };
  const fields = deckFields(spec.chrome);
  for (const variable of chrome?.variables ?? []) {
    values[variable.key] = fields[variable.key] || variable.default || "";
  }
  return values;
}

/**
 * What the chrome bars read on this deck. `footer` is a list of segments the
 * renderer separates with a middle dot: ONE segment when a template pattern
 * fills it (the pattern carries its own separators), the v1 `event_label` /
 * `footer_text` pair otherwise. `header` is only ever a template pattern.
 */
export interface ChromeText {
  footer: string[];
  header: string | null;
}

export function chromeText(
  spec: Pick<PresentationSpec, "title" | "slides" | "chrome">,
  templateChrome: TemplateChrome | null | undefined,
): ChromeText {
  if (declaresPattern(templateChrome)) {
    const values = chromeValues(spec, templateChrome);
    const footer = renderChromePattern(templateChrome.footer, values);
    const header = renderChromePattern(templateChrome.header, values);
    return { footer: footer ? [footer] : [], header: header || null };
  }
  const footer = [spec.chrome?.event_label, spec.chrome?.footer_text]
    .map((part) => (typeof part === "string" ? part.trim() : ""))
    .filter((part) => part.length > 0);
  return { footer, header: null };
}

/**
 * Whether the deck's body slides carry the template's watermark: the template
 * declares it and the deck shows its logo - the watermark is the same brand
 * furniture, so `chrome.show_logo: false` removes both.
 */
export function chromeWatermark(
  spec: Pick<PresentationSpec, "chrome">,
  templateChrome: TemplateChrome | null | undefined,
): boolean {
  return templateChrome?.watermark === true && (spec.chrome?.show_logo ?? true);
}

/**
 * Whether the deck's content and section slides carry the template's corner
 * emblem: the template declares it and the deck shows its logo - furniture
 * like the watermark, under the same `chrome.show_logo` switch.
 */
export function chromeEmblem(
  spec: Pick<PresentationSpec, "chrome">,
  templateChrome: TemplateChrome | null | undefined,
): boolean {
  return templateChrome?.emblem === true && (spec.chrome?.show_logo ?? true);
}

/** Placeholders the deck's `chrome.fields` set that the template never reads. */
export function undeclaredFields(
  chrome: DeckChrome | undefined,
  templateChrome: TemplateChrome | null | undefined,
): string[] {
  const declared = new Set((templateChrome?.variables ?? []).map((variable) => variable.key));
  return Object.keys(deckFields(chrome))
    .filter((key) => !declared.has(key))
    .sort();
}

// ── Validating a declaration as an author writes it (strict) ─────────────────

/**
 * One thing wrong with a declaration, as a CODE the template form translates
 * (the backend's `normalize_template_chrome` says the same things in English
 * and is what a save is checked against — this mirror only lets the form
 * refuse before the round-trip). `detail` carries the names involved.
 */
export interface ChromeProblem {
  code:
    | "pattern_too_long"
    | "pattern_unbalanced"
    | "pattern_malformed_slot"
    | "too_many_variables"
    | "variable_key_invalid"
    | "variable_key_builtin"
    | "variable_key_duplicate"
    | "variable_label_missing"
    | "variable_label_too_long"
    | "variable_hint_too_long"
    | "variable_default_too_long"
    | "variables_without_pattern"
    | "placeholder_undeclared"
    | "variable_unused";
  slot?: ChromeSlot;
  /** Variable index (0-based) for a per-variable problem. */
  index?: number;
  detail?: string;
}

/** A declaration as the template form holds it: strings the author is typing. */
export interface TemplateChromeDraft {
  footer: string;
  header: string;
  variables: TemplateChromeVariable[];
  /** The body-slide watermark switch; omitted reads as off. */
  watermark?: boolean;
  /** The corner-emblem switch; omitted reads as off. */
  emblem?: boolean;
  /** The right footer and the emblem placement, edited in the form. */
  footer_right?: string;
  emblem_size?: number;
  emblem_align?: "margin" | "title";
  emblem_ink?: "mark" | "accent";
}

/** The furniture switches a draft turns on, as the stored declaration spells them. */
function draftFurniture(
  draft: TemplateChromeDraft,
): Pick<TemplateChrome, "watermark" | "emblem" | "footer_right" | "emblem_size" | "emblem_align" | "emblem_ink"> {
  return {
    ...(draft.watermark ? { watermark: true } : {}),
    ...(draft.emblem ? { emblem: true } : {}),
    ...(draft.footer_right ? { footer_right: draft.footer_right } : {}),
    ...(draft.emblem_size ? { emblem_size: draft.emblem_size } : {}),
    ...(draft.emblem_align ? { emblem_align: draft.emblem_align } : {}),
    ...(draft.emblem_ink ? { emblem_ink: draft.emblem_ink } : {}),
  };
}

/**
 * Validate a draft the way the backend will, and shape what it stores:
 * `canonical` is the object to write under `extra_metadata.chrome` — `null`
 * when the draft says nothing (two blank patterns, no variable), which is how
 * "no pattern, v1 footer" is spelled and means the key is DROPPED. With any
 * problem nothing is stored.
 */
export function validateTemplateChrome(draft: TemplateChromeDraft): {
  canonical: TemplateChrome | null;
  problems: ChromeProblem[];
} {
  const problems: ChromeProblem[] = [];
  const patterns: Record<ChromeSlot, string | null> = { footer: null, header: null };
  for (const slot of PATTERN_SLOTS) {
    const text = draft[slot].trim();
    if (text.length > MAX_PATTERN_CHARS) problems.push({ code: "pattern_too_long", slot });
    const opens = (text.match(/\{\{/g) ?? []).length;
    const closes = (text.match(/\}\}/g) ?? []).length;
    if (opens !== closes) problems.push({ code: "pattern_unbalanced", slot });
    const malformed = [...text.matchAll(BRACES_RE)]
      .map((match) => `{{${match[1]}}}`)
      .filter((slotText) => !new RegExp(`^${PLACEHOLDER_RE.source}$`).test(slotText));
    if (malformed.length > 0) {
      problems.push({ code: "pattern_malformed_slot", slot, detail: malformed.join(", ") });
    }
    patterns[slot] = text || null;
  }

  const declared: string[] = [];
  const variables: TemplateChromeVariable[] = [];
  if (draft.variables.length > MAX_CHROME_VARIABLES) {
    problems.push({ code: "too_many_variables", detail: String(draft.variables.length) });
  }
  draft.variables.forEach((item, index) => {
    const key = item.key.trim().toLowerCase();
    if (!VARIABLE_KEY_RE.test(key)) {
      problems.push({ code: "variable_key_invalid", index, detail: item.key });
      return;
    }
    if ((BUILTIN_PLACEHOLDERS as readonly string[]).includes(key)) {
      problems.push({ code: "variable_key_builtin", index, detail: key });
      return;
    }
    if (declared.includes(key)) {
      problems.push({ code: "variable_key_duplicate", index, detail: key });
      return;
    }
    // A valid key DECLARES its placeholder even when the rest of the row is
    // wrong (mirrors the backend): a missing label is reported once, as a
    // label problem, never a second time as an undeclared placeholder.
    declared.push(key);
    const label = item.label.trim();
    if (!label) {
      problems.push({ code: "variable_label_missing", index, detail: key });
      return;
    }
    if (label.length > MAX_VARIABLE_LABEL_CHARS) {
      problems.push({ code: "variable_label_too_long", index, detail: key });
      return;
    }
    const entry: TemplateChromeVariable = { key, label };
    const hint = (item.hint ?? "").trim();
    const fallback = (item.default ?? "").trim();
    if (hint.length > MAX_VARIABLE_HINT_CHARS) {
      problems.push({ code: "variable_hint_too_long", index, detail: key });
      return;
    }
    if (fallback.length > MAX_FIELD_VALUE_CHARS) {
      problems.push({ code: "variable_default_too_long", index, detail: key });
      return;
    }
    if (hint) entry.hint = hint;
    if (fallback) entry.default = fallback;
    variables.push(entry);
  });

  // The right footer is a pattern too: a variable it alone reads is
  // used, and a right footer with no footer or header still fills variables
  // (backend `validate_template_chrome`, which walks footer, header, footer_right).
  const right = (draft.footer_right ?? "").trim() || null;
  if (!patterns.footer && !patterns.header && !right) {
    if (variables.length > 0) problems.push({ code: "variables_without_pattern" });
    const furniture = draftFurniture(draft);
    if (problems.length > 0 || Object.keys(furniture).length === 0) return { canonical: null, problems };
    return { canonical: { footer: null, header: null, variables: [], ...furniture }, problems };
  }

  // `{{page}}` and `{{year}}` are filled per slide, so only the right footer
  // may read them undeclared; in the footer or header they stay undeclared.
  const slotUsed = chromePlaceholders(patterns);
  const rightUsed = patternPlaceholders(right).filter(
    (name) => !(SLIDE_PLACEHOLDERS as readonly string[]).includes(name),
  );
  const used = [...slotUsed, ...rightUsed.filter((name) => !slotUsed.includes(name))];
  const undeclared = used.filter(
    (name) => !(BUILTIN_PLACEHOLDERS as readonly string[]).includes(name) && !declared.includes(name),
  );
  if (undeclared.length > 0) {
    problems.push({
      code: "placeholder_undeclared",
      detail: undeclared.map((name) => `{{${name}}}`).join(", "),
    });
  }
  const unused = declared.filter((key) => !used.includes(key));
  if (unused.length > 0) {
    problems.push({ code: "variable_unused", detail: unused.map((key) => `{{${key}}}`).join(", ") });
  }
  if (problems.length > 0) return { canonical: null, problems };
  const canonical: TemplateChrome = {
    footer: patterns.footer,
    header: patterns.header,
    variables,
    ...draftFurniture(draft),
  };
  return { canonical, problems: [] };
}
