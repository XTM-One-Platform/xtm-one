/**
 * Gives every `<Switch>` an accessible name, taken from the label already on
 * screen beside it.
 *
 *   node scripts/fds-migration/name-switches.mjs --dry    # proposal table
 *   node scripts/fds-migration/name-switches.mjs          # rewrite in place
 *
 * Why a script rather than 111 hand edits: the fix is the same every time, and
 * the label is already in the source. Every one of these rows is built as
 *
 *     <div className="flex items-center justify-between">
 *       <div>
 *         <p className="… font-medium">{t_i18n("Allow email & password login")}</p>
 *         <p className="…">{t_i18n("Disable to force all users to sign in via SSO")}</p>
 *       </div>
 *       <Switch checked={…} onCheckedChange={…} />
 *     </div>
 *
 * so the name is the row's LABEL, not the nearest text to the switch — the
 * nearest is the help text below it. `font-medium` is what separates the two,
 * and it is on the label at every site this fires on. Labels also appear as
 * `<span>`, spread over several lines, wrapped around an icon, or as plain
 * untranslated text; all four shapes are handled, and anything else is left
 * alone rather than guessed at.
 *
 * Reusing the label's own expression, rather than authoring a new string, buys
 * three things: no new message ids to translate, a name that cannot drift from
 * the visible text, and WCAG 2.5.3 ("Label in Name") satisfied by construction
 * instead of by review. Where the label is a computed expression
 * (`CHANNEL_TYPE_LABELS[chType]`) that works too — it is the same value the eye
 * reads.
 *
 * What it deliberately does NOT do: guess. A site whose label it cannot
 * identify is reported and left alone, for a human to name by hand. A silent
 * wrong label is worse than a missing one — a screen reader announcing the
 * wrong control is a bug you cannot see in review.
 */
import { readFile, writeFile } from "node:fs/promises";
import path from "node:path";
import { insideLabelElement, labelExpression, openingTagEnd, walk } from "./control-labels.mjs";

const SRC = path.resolve("src");
const dryRun = process.argv.includes("--dry");

const proposals = [];
const skipped = [];
const wrapped = [];

for await (const file of walk(SRC)) {
  const src = await readFile(file, "utf8");
  let out = src;
  let shift = 0;
  const regex = /<Switch[\s/>]/g;
  let match;

  while ((match = regex.exec(src))) {
    const tagEnd = openingTagEnd(src, match.index);
    if (tagEnd === -1) continue;
    const tag = src.slice(match.index, tagEnd + 1);
    if (/aria-label|aria-labelledby|\bid=/.test(tag)) continue;

    const line = src.slice(0, match.index).split("\n").length;
    const where = `${path.relative(process.cwd(), file)}:${line}`;
    if (insideLabelElement(src, match.index)) {
      wrapped.push(where);
      continue;
    }
    const expression = labelExpression(src, match.index, tagEnd + 1);
    if (!expression) {
      skipped.push(where);
      continue;
    }

    proposals.push({ where, expression, file });
    if (!dryRun) {
      const insertAt = match.index + "<Switch".length + shift;
      const insertion = ` aria-label={${expression}}`;
      out = out.slice(0, insertAt) + insertion + out.slice(insertAt);
      shift += insertion.length;
    }
  }

  if (!dryRun && out !== src) await writeFile(file, out);
}

/**
 * Two switches in one file resolving to the same name means at least one of
 * them borrowed a neighbour's label, so neither is trustworthy. Both go to the
 * manual list. A name that is merely repeated across FILES is fine — the same
 * toggle on two screens legitimately reads the same.
 */
const byFileAndName = new Map();
for (const p of proposals) {
  const key = `${p.file}\u0000${p.expression}`;
  byFileAndName.set(key, (byFileAndName.get(key) ?? 0) + 1);
}
const ambiguous = proposals.filter((p) => byFileAndName.get(`${p.file}\u0000${p.expression}`) > 1);
if (ambiguous.length) {
  console.log(`${ambiguous.length} proposals share a name with a sibling in the same file — REVIEW BEFORE APPLYING:`);
  for (const p of ambiguous) console.log(`  ${p.where}  ${p.expression}`);
  console.log();
}

const width = Math.max(...proposals.map((p) => p.where.length), 0);
for (const { where, expression } of proposals) {
  console.log(`${where.padEnd(width)}  aria-label={${expression}}`);
}
console.log();
console.log(`${dryRun ? "would name" : "named"} ${proposals.length} switches`);
console.log(`${wrapped.length} already named by a wrapping <label> — left alone`);
if (skipped.length) {
  console.log(`${skipped.length} left for a human — no label paragraph identifiable:`);
  for (const where of skipped) console.log(`  ${where}`);
}
