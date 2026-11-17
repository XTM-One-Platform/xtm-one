/**
 * Gives every `<Input>` an accessible name the design system can see.
 *
 *   node scripts/fds-migration/name-inputs.mjs --dry    # proposal table
 *   node scripts/fds-migration/name-inputs.mjs          # rewrite in place
 *
 * The library's `Input` names itself from a `label` prop, an `aria-label` or an
 * `aria-labelledby`, and warns in development when it has none. About 340 of
 * this app's inputs had none of the three: most leaned on a placeholder, which
 * disappears as soon as the field has a value and is not a reliable name; some
 * on a `<label htmlFor>` the library cannot see, so it warned on a named field.
 *
 * The name is taken, in order, from:
 *
 *   1. the `<label htmlFor>` whose target is this input's `id` - the label the
 *      field already had, now in a form the library reads too;
 *   2. the row's visible label, found as `name-switches.mjs` finds a switch's
 *      (`control-labels.mjs`): the nearest `font-medium` paragraph, span or
 *      label around the field, never past the previous input;
 *   3. a translated placeholder (`placeholder={t_i18n("Search agents...")}`) -
 *      a search field's placeholder IS its label. Listed apart, to be read.
 *
 * Anything else is reported for a human and left alone: a wrong name is worse
 * than none, because nobody sees it in review.
 */
import { readFile, writeFile } from "node:fs/promises";
import path from "node:path";
import { insideLabelElement, labelExpression, labelFromBody, openingTagEnd, walk } from "./control-labels.mjs";

const SRC = path.resolve("src");
const dryRun = process.argv.includes("--dry");

/** The value of a JSX attribute as source: `"x"` or the inside of `{...}`. */
function attribute(tag, name) {
  const match = tag.match(new RegExp(`\\s${name}=("[^"]*"|\\{((?:[^{}]|\\{[^{}]*\\})*)\\})`));
  if (!match) return null;
  return match[2] !== undefined ? match[2].trim() : match[1];
}

/** The text of the `<label htmlFor>` pointing at `id`, as an expression. */
function labelFor(src, id) {
  const escaped = id.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const target = id.startsWith('"') ? escaped : `\\{\\s*${escaped}\\s*\\}`;
  const match = src.match(new RegExp(`<([Ll]abel)[^>]*\\shtmlFor=${target}[^>]*>([\\s\\S]*?)</\\1>`));
  return match ? labelFromBody(match[2]) : null;
}

/**
 * Whether an expression found beside a field reads as its label. Help text is
 * a sentence (it ends with a full stop), an empty state says there is nothing,
 * and a bare `*` or dash is decoration; none of them names the field.
 */
function looksLikeLabel(expression) {
  const literal = expression.match(/^t_i18n\(\s*"([^"]*)"\s*\)$/)?.[1] ?? expression.match(/^"([^"]*)"$/)?.[1];
  if (literal === undefined) return /^\w+\s*\?\s*t_i18n\("[^"]{1,40}"\)\s*:\s*t_i18n\("[^"]{1,40}"\)$/.test(expression);
  return literal.length > 1 && literal.length <= 40 && !/[.!]$/.test(literal) && !/^(No |Loading)/.test(literal) && /[A-Za-z]/.test(literal);
}

/**
 * Whether a placeholder can stand as the field's name. A search field's
 * "Search agents..." can; a sample value cannot - `https://api.example.com/...`,
 * `alice@example.com`, `123456`, `e.g. 10m, 1h` or "Platform default" say what
 * to type or what is chosen, not what the field is. A dynamic placeholder is
 * unknown here, so it is left for a human too.
 */
function placeholderNamesField(placeholder) {
  const literal = placeholder.match(/^t_i18n\(\s*"([^"]*)"\s*\)$/)?.[1] ?? placeholder.match(/^"([^"]*)"$/)?.[1];
  if (literal === undefined) return false;
  if (literal.length < 2 || literal.length > 60) return false;
  if (/^e\.g\./i.test(literal) || /:\/\/|@/.test(literal) || /^[\d\s]+$/.test(literal)) return false;
  if (!/\s/.test(literal) && literal.includes("/")) return false;
  if (/\bdefault$|^No /i.test(literal)) return false;
  return /[A-Za-z]/.test(literal);
}

const proposals = [];
const fromPlaceholder = [];
const skipped = [];
const wrapped = [];

for await (const file of walk(SRC)) {
  if (file.includes(".test.")) continue;
  const src = await readFile(file, "utf8");
  let out = src;
  let shift = 0;
  const regex = /<Input[\s/>]/g;
  let match;

  while ((match = regex.exec(src))) {
    const tagEnd = openingTagEnd(src, match.index);
    if (tagEnd === -1) continue;
    const tag = src.slice(match.index, tagEnd + 1);
    if (/\slabel=|aria-label|aria-labelledby/.test(tag)) continue;
    // A forwarded spread (`{...props}`) names the field from its caller.
    if (/\{\.\.\.(props|rest|inputProps)\}/.test(tag)) continue;

    const line = src.slice(0, match.index).split("\n").length;
    const where = `${path.relative(process.cwd(), file)}:${line}`;
    if (insideLabelElement(src, match.index)) {
      wrapped.push(where);
      continue;
    }

    const id = attribute(tag, "id");
    let expression = id ? labelFor(src, id) : null;
    let source = "label[for]";
    if (!expression) {
      expression = labelExpression(src, match.index, tagEnd + 1, "<Input", { strict: true });
      source = "row label";
      // A label reads as a name: short, not a sentence, not a bare mark.
      if (expression && !looksLikeLabel(expression)) expression = null;
    }
    if (!expression) {
      const placeholder = attribute(tag, "placeholder");
      if (placeholder && placeholderNamesField(placeholder)) {
        expression = placeholder;
        source = "placeholder";
      }
    }
    if (!expression) {
      skipped.push(where);
      continue;
    }

    const proposal = { where, expression, file, source };
    (source === "placeholder" ? fromPlaceholder : proposals).push(proposal);
    if (!dryRun) {
      const insertAt = match.index + "<Input".length + shift;
      const value = expression.startsWith('"') ? expression : `{${expression}}`;
      const insertion = ` aria-label=${value}`;
      out = out.slice(0, insertAt) + insertion + out.slice(insertAt);
      shift += insertion.length;
    }
  }

  if (!dryRun && out !== src) await writeFile(file, out);
}

const print = (rows) => {
  const width = Math.max(...rows.map((p) => p.where.length), 0);
  for (const { where, expression, source } of rows) console.log(`${where.padEnd(width)}  ${source.padEnd(10)}  ${expression}`);
};
print(proposals);
console.log();
console.log(`${fromPlaceholder.length} named from their placeholder - read them:`);
print(fromPlaceholder);
console.log();
console.log(`${dryRun ? "would name" : "named"} ${proposals.length + fromPlaceholder.length} inputs`);
console.log(`${wrapped.length} inside a wrapping <label> - left alone:`);
for (const where of wrapped) console.log(`  ${where}`);
console.log(`${skipped.length} left for a human:`);
for (const where of skipped) console.log(`  ${where}`);
