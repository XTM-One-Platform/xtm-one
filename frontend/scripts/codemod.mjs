#!/usr/bin/env node
/**
 * The codemod runner of this repository, run by the sandbox_codemod tool through
 * the frontend-codemod check (.xtm-one/checks.toml); see
 * dev-docs/reference/sandbox-workspace.md, "sandbox_codemod".
 *
 * Usage: node scripts/codemod.mjs <staging directory, relative to this working directory>.
 * It reads <staging>/request.json:
 *   { version: 1, root: "../..", transform: "work/spinners.mjs", files: ["xo/frontend/src/a.tsx", ...] }
 * (root: the workspace root as seen from this working directory; transform and
 * files: workspace paths), imports the transform (an ES module whose default
 * export is `({ path, source, parse }) => string | null | undefined`, `parse(text)`
 * being the oxc-parser parse of `text` as that file, so the transform needs no
 * import of its own), runs it on each file, parses each new text and writes
 * <staging>/result.json:
 *   { files: [{ path, changed, error?, parse_errors? }] }   or   { error, files: [] }
 * and each changed text under <staging>/out/<path>. It never writes a source
 * file: the tool reads the staged texts back and writes them all or nothing.
 */
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, isAbsolute, join, relative, resolve } from "node:path";
import { pathToFileURL } from "node:url";
import { parseSync } from "oxc-parser";

const LANGS = { ts: "ts", mts: "ts", cts: "ts", tsx: "tsx", js: "js", mjs: "js", cjs: "js", jsx: "jsx" };

/** The oxc-parser language of `path`, by its extension (tsx when unknown). */
export function langOf(path) {
  return LANGS[path.slice(path.lastIndexOf(".") + 1)] ?? "tsx";
}

/**
 * `path` resolved under `base`; throws when it is absolute or leaves `base`
 * (a `..` segment), so a request can only name files of its own tree.
 */
export function inside(base, path) {
  const full = resolve(base, String(path));
  const rel = relative(base, full);
  if (isAbsolute(String(path)) || rel === "" || rel.startsWith("..") || isAbsolute(rel)) {
    throw new Error(`${path}: outside ${base}`);
  }
  return full;
}

function message(error) {
  return String(error?.message ?? error).split("\n")[0].slice(0, 300);
}

/** Run the request of `staging` (relative to the working directory); the result is written, never thrown. */
export async function run(staging) {
  const request = JSON.parse(readFileSync(join(staging, "request.json"), "utf8"));
  const root = resolve(request.root ?? ".");
  const stagingDir = resolve(staging);
  const resultPath = join(stagingDir, "result.json");
  let transform;
  try {
    const mod = await import(pathToFileURL(inside(root, request.transform)).href);
    transform = mod.default ?? mod.transform;
    if (typeof transform !== "function") throw new Error("the module has no default export function");
  } catch (error) {
    writeFileSync(resultPath, JSON.stringify({ error: `${request.transform}: ${message(error)}`, files: [] }));
    return;
  }
  const files = [];
  for (const path of request.files) {
    let source;
    let out;
    try {
      source = readFileSync(inside(root, path), "utf8");
      out = inside(join(stagingDir, "out"), path);
    } catch (error) {
      files.push({ path, changed: false, error: message(error) });
      continue;
    }
    const parse = (text) => parseSync(path, text, { lang: langOf(path) });
    let after;
    try {
      after = await transform({ path, source, parse });
    } catch (error) {
      files.push({ path, changed: false, error: message(error) });
      continue;
    }
    if (typeof after !== "string" || after === source) {
      files.push({ path, changed: false });
      continue;
    }
    const errors = parse(after).errors ?? [];
    mkdirSync(dirname(out), { recursive: true });
    writeFileSync(out, after);
    files.push({ path, changed: true, parse_errors: errors.map(message) });
  }
  writeFileSync(resultPath, JSON.stringify({ files }));
}

if (import.meta.url === pathToFileURL(process.argv[1] ?? "").href) {
  const staging = process.argv[2];
  if (!staging) {
    console.error("usage: node scripts/codemod.mjs <staging directory>");
    process.exit(2);
  }
  await run(staging);
}
