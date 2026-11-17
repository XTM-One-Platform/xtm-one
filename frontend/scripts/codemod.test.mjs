import assert from "node:assert/strict";
import { mkdirSync, mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, relative } from "node:path";
import { test } from "node:test";

import { inside, langOf, run } from "./codemod.mjs";

/** A workspace in a temporary directory: `files` written, the request staged, the runner run from `cwd`. */
async function runIn(files, transform, { cwd = "" } = {}) {
  const root = mkdtempSync(join(tmpdir(), "codemod-"));
  for (const [path, text] of Object.entries(files)) {
    mkdirSync(join(root, path, ".."), { recursive: true });
    writeFileSync(join(root, path), text);
  }
  writeFileSync(join(root, "work/t.mjs"), transform);
  const where = join(root, cwd);
  mkdirSync(where, { recursive: true });
  const staging = join(root, ".xtm-codemod/r1");
  mkdirSync(staging, { recursive: true });
  const top = relative(where, root) || ".";
  const sources = Object.keys(files).filter((p) => p !== "work/t.mjs");
  writeFileSync(join(staging, "request.json"), JSON.stringify({ version: 1, root: top, transform: "work/t.mjs", files: sources }));
  const before = process.cwd();
  process.chdir(where);
  try {
    await run(relative(where, staging));
  } finally {
    process.chdir(before);
  }
  const result = JSON.parse(readFileSync(join(staging, "result.json"), "utf8"));
  const out = (path) => readFileSync(join(staging, "out", path), "utf8");
  return { result, out, root };
}

test("a changed file is staged with no parse error, an unchanged one is reported unchanged", async () => {
  const { result, out } = await runIn(
    { "work/t.mjs": "", "app/a.tsx": "const a = <b>{x ? <c /> : null}</b>;\n", "app/b.ts": "const y = 1;\n" },
    "export default ({ source }) => source.replace('x ? <c /> : null', 'x && <c />');\n",
    { cwd: "app" },
  );
  assert.deepEqual(result.files, [
    { path: "app/a.tsx", changed: true, parse_errors: [] },
    { path: "app/b.ts", changed: false },
  ]);
  assert.equal(out("app/a.tsx"), "const a = <b>{x && <c />}</b>;\n");
});

test("a new text that no longer parses is staged with its parse errors", async () => {
  const { result } = await runIn(
    { "work/t.mjs": "", "a.tsx": "const a = <b />;\n" },
    "export default ({ source }) => source.replace('<b />', '<b>');\n",
  );
  assert.equal(result.files[0].changed, true);
  assert.ok(result.files[0].parse_errors.length > 0);
});

test("a transform that throws is reported per file; the parse helper reads the AST", async () => {
  const { result } = await runIn(
    { "work/t.mjs": "", "a.ts": "const a = 1;\n", "b.ts": "let b = 2;\n" },
    "export default ({ path, parse, source }) => { if (path === 'a.ts') throw new Error('boom'); " +
      "return parse(source).program.body[0].kind === 'let' ? source.replace('let', 'const') : null; };\n",
  );
  assert.deepEqual(result.files, [
    { path: "a.ts", changed: false, error: "boom" },
    { path: "b.ts", changed: true, parse_errors: [] },
  ]);
});

test("a module without a default function is one error for the whole request", async () => {
  const { result } = await runIn({ "work/t.mjs": "", "a.ts": "x;\n" }, "export const nothing = 1;\n");
  assert.deepEqual(result.files, []);
  assert.match(result.error, /no default export function/);
});

test("a file outside the workspace root is refused per file and never read", async () => {
  const { result } = await runIn(
    { "work/t.mjs": "", "a.ts": "let a = 1;\n", "../escape.ts": "let e = 1;\n" },
    "export default ({ source }) => source.replace('let', 'const');\n",
  );
  assert.deepEqual(result.files[0], { path: "a.ts", changed: true, parse_errors: [] });
  assert.equal(result.files[1].path, "../escape.ts");
  assert.equal(result.files[1].changed, false);
  assert.match(result.files[1].error, /outside/);
});

test("inside refuses an absolute path and a parent segment", () => {
  assert.equal(inside("/w", "a/b.ts"), "/w/a/b.ts");
  assert.throws(() => inside("/w", "/etc/passwd"), /outside/);
  assert.throws(() => inside("/w", "a/../../etc/passwd"), /outside/);
});

test("the language follows the extension", () => {
  assert.equal(langOf("a/b.tsx"), "tsx");
  assert.equal(langOf("a/b.mjs"), "js");
  assert.equal(langOf("a/b.ts"), "ts");
});
