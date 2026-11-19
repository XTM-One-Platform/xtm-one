/**
 * Validate the ⌘K command manifests (`src/**\/*.commands.ts`):
 *  1. every command id is globally unique, including nested children;
 *  2. ids are dot-namespaced with camelCase segments, so they read as
 *     `area.action` (`operations.backgroundTasks`, `skills.importPackage`);
 *  3. `when` / `whenNot` only reference known context keys;
 *  4. every `title` and `category` exists in en.json (they are i18n keys);
 *  5. a command does something — `navigate`, `handler`, or `children`;
 *  6. `navigate` targets an absolute path.
 *
 * This is what a codegen step would otherwise have enforced: the registry is
 * assembled by `import.meta.glob` at build time, so there is no generated file
 * to drift, and these are the invariants TypeScript alone cannot express.
 *
 * Parsing is deliberately regex-based rather than AST-based: manifests are
 * flat literal data by contract (see `lib/commands/types.ts`), so there is no
 * dynamic construction to resolve, and this keeps the script dependency-free
 * like `validate-i18n.mjs`.
 *
 * Usage: node scripts/validate-commands.mjs
 */
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

// `URL.pathname` yields `/C:/...` on Windows (a doubled-drive path once joined);
// `fileURLToPath` resolves to a native path on every OS.
const SRC = fileURLToPath(new URL("../src/", import.meta.url));

/** Keep in sync with CONTEXT_KEYS in src/lib/commands/types.ts. */
const CONTEXT_KEYS = new Set([
  "isAdmin", "chatEnabled", "chatSendEnabled", "agenticAI", "customVariables",
  "xtmOne", "aiAdoption", "catalogEnabled", "flowBuilder", "deepInvestigation", "evaluation", "runnersEnabled",
  "arcadeEnabled", "relaysEnabled", "intents",
]);

/** Keep in sync with `CommandDef["requires"]` in src/lib/commands/types.ts. */
const LICENCE_REQUIREMENTS = new Set(["agenticAI"]);

const en = JSON.parse(readFileSync(new URL("../src/i18n/lang/en.json", import.meta.url), "utf8"));

function walk(dir) {
  const out = [];
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry);
    if (statSync(full).isDirectory()) out.push(...walk(full));
    else if (entry.endsWith(".commands.ts")) out.push(full);
  }
  return out;
}

const files = walk(SRC);
let errors = 0;
const fail = (file, message) => {
  console.log(`${file.replace(SRC, "src/")}: ${message}`);
  errors += 1;
};

const seen = new Map();

for (const file of files) {
  const source = readFileSync(file, "utf8");

  if (!/export default defineCommands\(\[/.test(source)) {
    fail(file, "missing `export default defineCommands([...])`");
    continue;
  }

  /* A command's fields run from its own `id:` to the next one, whether the
     object is spread over lines or written inline as a child. A parent's block
     therefore ends at its first child, which still contains everything that
     belongs to the parent (its `children: [` opener included). */
  const marks = [...source.matchAll(/\bid:\s*"([^"]+)"/g)];

  for (const [index, mark] of marks.entries()) {
    const block = source.slice(mark.index, marks[index + 1]?.index ?? source.length);
    const id = mark[1];

    if (seen.has(id)) fail(file, `duplicate id "${id}" (also in ${seen.get(id).replace(SRC, "src/")})`);
    else seen.set(id, file);

    if (!/^[a-z][a-zA-Z0-9]*(\.[a-zA-Z0-9]+)+$/.test(id)) {
      fail(file, `id "${id}" should be dot-namespaced, e.g. "agents.create"`);
    }

    for (const field of ["title", "category"]) {
      const value = block.match(new RegExp(`\\b${field}:\\s*"([^"]+)"`))?.[1];
      if (!value) { fail(file, `"${id}" has no ${field}`); continue; }
      if (!(value in en)) fail(file, `"${id}" ${field} ${JSON.stringify(value)} is missing from en.json`);
    }

    for (const clause of ["when", "whenNot"]) {
      const raw = block.match(new RegExp(`\\b${clause}:\\s*\\[([^\\]]*)\\]`))?.[1];
      if (!raw) continue;
      for (const key of raw.match(/"([^"]+)"/g) ?? []) {
        const name = key.slice(1, -1);
        if (!CONTEXT_KEYS.has(name)) fail(file, `"${id}" ${clause} references unknown context key "${name}"`);
      }
    }

    /* `requires` is a licence gate (badge + upsell), not a `when` key — a typo
       here would silently make a paid feature look free. */
    const requires = block.match(/\brequires:\s*"([^"]+)"/)?.[1];
    if (requires && !LICENCE_REQUIREMENTS.has(requires)) {
      fail(file, `"${id}" requires unknown licence flag "${requires}"`);
    }
    if (/\bwhen:\s*\[[^\]]*"agenticAI"/.test(block)) {
      fail(file, `"${id}" gates the Enterprise Edition licence with \`when\` — use \`requires: "agenticAI"\` so the palette badges it instead of hiding it`);
    }

    const navigate = block.match(/\bnavigate:\s*"([^"]+)"/)?.[1];
    if (navigate && !navigate.startsWith("/")) {
      fail(file, `"${id}" navigate ${JSON.stringify(navigate)} must be an absolute path`);
    }
    const acts = navigate || /\bhandler:\s*"/.test(block) || /\bchildren:\s*\[/.test(block);
    if (!acts) fail(file, `"${id}" declares no navigate, handler or children — selecting it would do nothing`);
  }
}

console.log(
  errors === 0
    ? `OK — ${seen.size} commands across ${files.length} manifests`
    : `\n${errors} problem(s) found`,
);
process.exit(errors === 0 ? 0 : 1);
