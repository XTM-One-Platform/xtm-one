/**
 * Promotes a tooltip into an accessible name, for icon-only buttons that have a
 * tooltip and nothing else.
 *
 *   node scripts/fds-migration/name-icon-buttons.mjs --dry
 *   node scripts/fds-migration/name-icon-buttons.mjs
 *
 * A tooltip is a DESCRIPTION. It is announced after the name, if at all, and
 * never instead of one — so a button whose only text is its tooltip announces as
 * "button", which is the defect. But the tooltip's text is almost always exactly
 * the name the button should have had ("Delete run", "Zoom in"), already
 * translated. Promoting it costs no new message ids and cannot drift from what
 * the user sees on hover.
 *
 * Scope, deliberately narrow — it only fires when:
 *
 *   - the button's body is PURE JSX: no text, no expression, so there is no way
 *     it has a name from its content. This is a sound test rather than a
 *     complete one; a button whose child is `{icon}` might or might not be
 *     icon-only, and guessing there produced 592 false positives on the first
 *     attempt at this;
 *   - a `<Tip>` wraps it directly, with nothing but whitespace between;
 *   - the tooltip's content cannot be empty. `content={flag ? tip : ""}` would
 *     give the control a name on some renders and not others, which is worse
 *     than a consistent miss because it cannot be found by testing.
 *
 * Everything else is reported and left alone: those need a name written from the
 * button's context, which is a judgement call and not a rewrite.
 */
import { readdir, readFile, writeFile } from "node:fs/promises";
import path from "node:path";

const SRC = path.resolve("src");
const dryRun = process.argv.includes("--dry");

async function* walk(dir) {
  for (const entry of await readdir(dir, { withFileTypes: true })) {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) yield* walk(full);
    else if (full.endsWith(".tsx")) yield full;
  }
}

/** End of the JSX opening tag at `start`, tracking braces and strings. */
function openingTagEnd(source, start) {
  let depth = 0;
  let inString = null;
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

/** True when the element's children are JSX only — no text, no expressions. */
function bodyIsPureJsx(body) {
  if (/aria-label|sr-only/.test(body)) return false;
  const withoutTags = body.replace(/<[^>]*>/g, " ");
  if (withoutTags.includes("{")) return false;
  if (withoutTags.replace(/\s+/g, " ").trim()) return false;
  return /<[A-Z]\w*|<svg/.test(body);
}

/**
 * The `content` expression of a `<Tip>` that wraps the element at `start`
 * directly, or null. "Directly" matters: a Tip further up the tree may belong to
 * a sibling control, and borrowing its text would name this button after
 * something else.
 */
function wrappingTipContent(source, start) {
  const before = source.slice(0, start);
  const tipStart = before.lastIndexOf("<Tip");
  if (tipStart === -1) return null;
  const tipEnd = openingTagEnd(source, tipStart);
  if (tipEnd === -1 || tipEnd > start) return null;
  // Only whitespace between the tooltip's opening tag and this element.
  if (source.slice(tipEnd + 1, start).trim()) return null;

  const tag = source.slice(tipStart, tipEnd + 1);
  const content = tag.match(/\scontent=\{((?:[^{}]|\{[^{}]*\})*)\}/)?.[1]?.trim();
  if (!content) return null;
  // A name that is sometimes empty is not a name.
  if (/(?:^|[?:]\s*)""|''/.test(content)) return null;
  // Markup in the tooltip is a rich tooltip, not a label.
  if (content.includes("<")) return null;
  return content;
}

const named = [];
const needsAuthoring = [];

for await (const file of walk(SRC)) {
  const source = await readFile(file, "utf8");
  const edits = [];

  for (const tag of ["Button", "button"]) {
    const regex = new RegExp(`<${tag}[\\s]`, "g");
    let match;
    while ((match = regex.exec(source))) {
      const tagEnd = openingTagEnd(source, match.index);
      if (tagEnd === -1) continue;
      const open = source.slice(match.index, tagEnd + 1);
      if (open.trimEnd().endsWith("/>")) continue;
      if (/aria-label|aria-labelledby|sr-only|title=/.test(open)) continue;

      const close = source.indexOf(`</${tag}>`, tagEnd);
      if (close === -1) continue;
      if (!bodyIsPureJsx(source.slice(tagEnd + 1, close))) continue;

      const line = source.slice(0, match.index).split("\n").length;
      const where = `${path.relative(process.cwd(), file)}:${line}`;
      const content = wrappingTipContent(source, match.index);
      if (!content) {
        needsAuthoring.push(where);
        continue;
      }
      named.push({ where, content });
      edits.push({ at: match.index + `<${tag}`.length, text: ` aria-label={${content}}` });
    }
  }

  if (!dryRun && edits.length) {
    let out = source;
    for (const edit of edits.sort((a, b) => b.at - a.at)) {
      out = out.slice(0, edit.at) + edit.text + out.slice(edit.at);
    }
    await writeFile(file, out);
  }
}

const width = Math.max(...named.map((n) => n.where.length), 0);
for (const { where, content } of named) {
  console.log(`${where.padEnd(width)}  aria-label={${content}}`);
}
console.log();
console.log(`${dryRun ? "would name" : "named"} ${named.length} icon-only buttons from their tooltip`);
console.log(`${needsAuthoring.length} need a name written by hand:`);
for (const where of needsAuthoring) console.log(`  ${where}`);
