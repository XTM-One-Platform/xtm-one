/**
 * Counts styling that still bypasses the design system.
 *
 *   node scripts/fds-migration/count-legacy.mjs            # summary
 *   node scripts/fds-migration/count-legacy.mjs --list     # every occurrence
 *   node scripts/fds-migration/count-legacy.mjs --check    # non-zero exit past the baseline
 *
 * The migration ships this counter rather than a number quoted in a commit
 * message, so the figure can be re-derived instead of trusted. `--check` is what
 * makes it a guard, run by CI: no category may exceed its figure in
 * `count-legacy-baseline.json`. Most are zero; the colour literals are the
 * documented ones (LIBRARY-FEEDBACK F24), which only shrink - a category below
 * its baseline is reported so the baseline can be lowered with it, and can
 * then not silently regrow.
 */
import { readdir, readFile } from "node:fs/promises";
import path from "node:path";
import { readFileSync } from "node:fs";
import { legacyPatterns } from "./token-map.mjs";

const SRC = path.resolve("src");
const EXTENSIONS = new Set([".ts", ".tsx", ".css"]);
const list = process.argv.includes("--list");
const check = process.argv.includes("--check");

/**
 * Files whose colour values are legitimately not the app's theme.
 *
 * `index.css` is the token layer: it names the values everything else must not.
 *
 * `report.css` is a printed-document design with its own palette — an orange
 * accent and four risk tones that exist nowhere in the product UI. Pointing it
 * at product tokens would change what a delivered report looks like, which is a
 * design decision rather than a migration one.
 *
 * Everything else still counted is real, known debt: chart series palettes,
 * third-party provider brand colours (which are correctly NOT design-system
 * colours), and neutral canvas fills whose meaning depends on their call site.
 */
const EXCLUDE = new Set([
  path.join(SRC, "index.css"),
  path.join(SRC, "components", "report", "report.css"),
]);

async function* walk(dir) {
  for (const entry of await readdir(dir, { withFileTypes: true })) {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) yield* walk(full);
    else if (EXTENSIONS.has(path.extname(entry.name))) yield full;
  }
}

const findings = new Map(legacyPatterns.map(({ label }) => [label, []]));

for await (const file of walk(SRC)) {
  if (EXCLUDE.has(file)) continue;
  const source = await readFile(file, "utf8");
  const lines = source.split("\n");
  for (const { label, pattern, find } of legacyPatterns) {
    lines.forEach((line, index) => {
      const texts = find ? find(line) : [...line.matchAll(pattern)].map((match) => match[0]);
      for (const text of texts) {
        findings.get(label).push({
          file: path.relative(process.cwd(), file),
          line: index + 1,
          text,
        });
      }
    });
  }
}

let total = 0;
for (const [label, hits] of findings) {
  total += hits.length;
  const files = new Set(hits.map((hit) => hit.file)).size;
  console.log(`${String(hits.length).padStart(5)}  ${label}${hits.length ? ` (${files} files)` : ""}`);
  if (list && hits.length) {
    const byText = new Map();
    for (const hit of hits) byText.set(hit.text, (byText.get(hit.text) ?? 0) + 1);
    for (const [text, count] of [...byText].sort((a, b) => b[1] - a[1])) {
      console.log(`         ${String(count).padStart(4)} × ${text}`);
    }
    for (const hit of hits) console.log(`         ${hit.file}:${hit.line}  ${hit.text}`);
  }
}
console.log(`${String(total).padStart(5)}  total`);

if (check) {
  const baseline = JSON.parse(readFileSync(new URL("./count-legacy-baseline.json", import.meta.url), "utf8"));
  let over = false;
  for (const [label, hits] of findings) {
    const allowed = baseline[label] ?? 0;
    if (hits.length > allowed) {
      over = true;
      console.error(`over the baseline: ${label} - ${hits.length} found, ${allowed} allowed (--list shows where)`);
    } else if (hits.length < allowed) {
      console.log(`below the baseline: ${label} - ${hits.length} found, ${allowed} allowed: lower it in count-legacy-baseline.json`);
    }
  }
  if (over) process.exit(1);
}
