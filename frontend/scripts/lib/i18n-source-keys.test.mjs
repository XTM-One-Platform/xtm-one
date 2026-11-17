/**
 * The source scan behind validate-i18n's "every key the UI asks for is in every
 * catalog" check - `yarn test:unit`.
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, relative } from "node:path";

import { keyTables, literalKeys, sourceFiles, sourceKeys } from "./i18n-source-keys.mjs";

const read = (path) => readFileSync(new URL(path, import.meta.url), "utf8");
const keys = (items) => items.map(({ key }) => key);

test("a literal passed to t_i18n is a key, in every quote style and across lines", () => {
  const source = [
    'const a = t_i18n("Save");',
    "const b = t_i18n('Don\\'t save');",
    "const c = t_i18n(`No substitution`);",
    "const d = t_i18n(",
    '  "{count, plural, one {# run} other {# runs}}",',
    "  { count },",
    ");",
    'const e = t_i18n("Say \\"hi\\"");',
    'const f = formatter.t_i18n("As a method");',
    'const g = t_i18n?.("Optional call");',
    'const h = <Button aria-label={t_i18n("In an attribute")}>{t_i18n("As a child")}</Button>;',
  ].join("\n");
  assert.deepEqual(keys(literalKeys(source)), [
    "Save",
    "Don't save",
    "No substitution",
    "{count, plural, one {# run} other {# runs}}",
    'Say "hi"',
    "As a method",
    "Optional call",
    "In an attribute",
    "As a child",
  ]);
});

test("a key is decoded like JavaScript does it, whatever its escapes", () => {
  // JSON.parse refuses every one of these; a legal key must never crash the validator.
  const source = [
    'const a = t_i18n("\\x41\\u{1F600}\\v\\0");',
    "const b = t_i18n(\"It\\'s\");",
    'const c = t_i18n("A\tB");',
  ].join("\n");
  assert.deepEqual(keys(literalKeys(source)), ["A\u{1F600}\v\0", "It's", "A\tB"]);
});

test("a dynamic argument is not a key, and a documented example is not asked for", () => {
  const source = [
    "/**",
    ' * `t_i18n("Some English string")` returns the translation.',
    " */",
    '// t_i18n("Commented out")',
    "const a = t_i18n(label);",
    "const b = t_i18n(STAGE_LABELS[key]);",
    "const c = t_i18n(`${prefix} suffix`);",
    'const d = t_i18n(label ?? "Fallback");',
    'const e = t_i18n(pluralize(count, "agent"));',
    'const url = `https://example.com/${t_i18n("In a template")}`;',
  ].join("\n");
  assert.deepEqual(keys(literalKeys(source)), ["In a template"]);
});

test("each literal branch of a conditional argument is a key", () => {
  const source = [
    'const a = t_i18n(count === 1 ? "item" : "items");',
    'const b = t_i18n(kind === "event" ? "Event" : kind === "work_item" ? "Work item" : kind);',
    'const c = t_i18n((pinned) ? `Pinned` : ("Unpinned"));',
  ].join("\n");
  assert.deepEqual(keys(literalKeys(source)), ["item", "items", "Event", "Work item", "Pinned", "Unpinned"]);
});

test("a call written inside a string, template text, JSX text or a regex asks for nothing", () => {
  const source = [
    "const a = 't_i18n(\"Not executed\")';",
    "const b = \"t_i18n('Not executed either')\";",
    'const c = `docs: t_i18n("Not executed in template text")`;',
    'const d = /t_i18n\\("In a regex"\\)/;',
    'const e = <p>Call t_i18n("In JSX text") to translate</p>;',
    'const f = `${t_i18n("In a substitution")} and t_i18n("Not in a substitution")`;',
  ].join("\n");
  assert.deepEqual(keys(literalKeys(source)), ["In a substitution"]);
});

test("a comment is never a key, whatever its layout, and code around it still is", () => {
  const source = [
    "/*",
    '  t_i18n("Unstarred block line")',
    "*/",
    'const a = t_i18n("Before") /* t_i18n("Inline block") */ + t_i18n("After");',
    'const b = <div>{/* t_i18n("JSX comment") */}{t_i18n("JSX child")}</div>;',
    'const url = "https://example.com/* not a comment */"; t_i18n("After a URL string");',
    'const c = <><a href={x}>See https://example.com</a> {t_i18n("After JSX text with a URL")}</>;',
    'const re = /["\'\\/]+/g; t_i18n("After a regex");',
    'const s = `${a ? t_i18n("Nested") : `${t_i18n("Deeper")}`} // text`; // t_i18n("Trailing comment")',
    'const d = <p><b>x</b>{/* t_i18n("Comment after a closing tag") */}{t_i18n("After a closing tag")}</p>;',
    "const e = {",
    '  label: // t_i18n("Line comment after a colon")',
    '    t_i18n("After a colon"),',
    "};",
  ].join("\n");
  assert.deepEqual(keys(literalKeys(source)), [
    "Before",
    "After",
    "JSX child",
    "After a URL string",
    "After JSX text with a URL",
    "After a regex",
    "Nested",
    "Deeper",
    "After a closing tag",
    "After a colon",
  ]);
});

test("a .ts file is parsed as TypeScript, a .tsx file as TSX", () => {
  // `<T>x` is a type assertion in a .ts file and an element in a .tsx one.
  const source = 'const label = <string>t_i18n("Behind a type assertion");\n';
  assert.deepEqual(keys(sourceKeys(source, "labels.ts").keys), ["Behind a type assertion"]);
  assert.deepEqual(keys(sourceKeys(source, "labels.tsx").keys), []);
});

test("a commented-out entry of an @i18n-keys table is not a key", () => {
  const source = [
    "/** @i18n-keys */",
    "const LABELS = {",
    '  kept: "Kept",',
    '  // gone: "Commented out",',
    '  /* old: "Block commented", */',
    "} as const;",
  ].join("\n");
  assert.deepEqual(keys(keyTables(source).tables[0].keys), ["Kept"]);
});

test("an @i18n-keys table yields its string values, never its property names", () => {
  const source = [
    "/** Mode copy. @i18n-keys */",
    "const MODES = {",
    '  wizard: { title: "Wizard", description: "Step by step" },',
    '  "quoted-name": { title: "Import" },',
    '  [COMPUTED]: `Computed name`,',
    "} as const;",
    "/** @i18n-keys */",
    'export const TIPS = ["First tip", ["Nested tip"]] as const;',
    "/** @i18n-keys */",
    'const STAGES = { parse: "Reading" } as const satisfies Record<Stage, string>;',
    'const OTHER = { label: "Not a table" };',
  ].join("\n");
  const { tables, problems } = keyTables(source);
  assert.deepEqual(problems, []);
  assert.deepEqual(
    tables.map((table) => [table.name, keys(table.keys)]),
    [
      ["MODES", ["Wizard", "Step by step", "Import", "Computed name"]],
      ["TIPS", ["First tip", "Nested tip"]],
      ["STAGES", ["Reading"]],
    ],
  );
});

test("an @i18n-keys table value that is not a string literal is a problem, not a silent skip", () => {
  const source = [
    "/** @i18n-keys */",
    "const LABELS = {",
    '  kept: "Kept",',
    "  count: 3,",
    "  prefixed: `${PREFIX} label`,",
    '  either: flag ? "A" : "B",',
    '  joined: "x" + "y",',
    "  shorthand,",
    "  ...OTHER,",
    "} as const;",
  ].join("\n");
  const { tables, problems } = keyTables(source);
  assert.deepEqual(keys(tables[0].keys), ["Kept"]);
  assert.deepEqual(
    problems.map(({ message }) => message.match(/`(.*)` is not a string literal/)?.[1]),
    ["3", "`${PREFIX} label`", 'flag ? "A" : "B"', '"x" + "y"', "shorthand", "...OTHER"],
  );
  assert.ok(problems.every(({ message }) => message.startsWith("@i18n-keys table LABELS: ")));
});

test("an @i18n-keys tag without a module-scope `as const` table is a problem, not a silent pass", () => {
  const notConst = keyTables('/** @i18n-keys */\nconst LABELS = { a: "A" };\n');
  assert.equal(notConst.tables.length, 0);
  assert.match(notConst.problems[0].message, /closed by `as const`/);
  const notTable = keyTables("/** @i18n-keys */\nfunction labels() {}\n");
  assert.match(notTable.problems[0].message, /must tag a `const NAME/);
  const notConstBinding = keyTables('/** @i18n-keys */\nlet LABELS = { a: "A" } as const;\n');
  assert.match(notConstBinding.problems[0].message, /must tag a `const NAME/);
  const nested = keyTables('function f() {\n  /** @i18n-keys */\n  const LABELS = { a: "A" } as const;\n}\n');
  assert.deepEqual([nested.tables.length, nested.problems.length], [0, 1]);
  assert.match(nested.problems[0].message, /at module scope/);
  const lineComment = keyTables('// @i18n-keys\nconst LABELS = { a: "A" } as const;\n');
  assert.deepEqual([lineComment.tables.length, lineComment.problems.length], [0, 1]);
});

test("keys carry the line of their literal", () => {
  const { keys: found } = sourceKeys('\n\nt_i18n("Third line");\nt_i18n(\n  "Fifth line",\n);\n');
  assert.deepEqual(found, [
    { key: "Third line", line: 3 },
    { key: "Fifth line", line: 5 },
  ]);
});

test("the scanned files are the source files, never tests, stories or node_modules", () => {
  const dir = mkdtempSync(join(tmpdir(), "i18n-source-keys-"));
  try {
    for (const file of ["a.tsx", "b.ts", "c.test.tsx", "d.spec.ts", "e.stories.tsx", "f.js", "node_modules/g.ts", "nested/h.tsx"]) {
      mkdirSync(join(dir, file, ".."), { recursive: true });
      writeFileSync(join(dir, file), "");
    }
    const found = sourceFiles(dir).map((file) => relative(dir, file).replace(/\\/g, "/")).sort();
    assert.deepEqual(found, ["a.tsx", "b.ts", "nested/h.tsx"]);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("the two dynamically keyed sites are readable tables", () => {
  const mode = sourceKeys(read("../../src/pages/NewAgentMode.tsx"));
  for (const key of ["Wizard", "Advanced", "Import"]) assert.ok(keys(mode.keys).includes(key), key);
  const importPage = sourceKeys(read("../../src/pages/NewAgentImport.tsx"));
  for (const key of ["Reading the file", "Detecting the format", "Validating against the schema", "Resolving references"]) {
    assert.ok(keys(importPage.keys).includes(key), key);
  }
  assert.deepEqual([...mode.problems, ...importPage.problems], []);
});

test("the gate runs: validate-i18n scans the source, CI and yarn check run it", () => {
  assert.match(read("../validate-i18n.mjs"), /scanSource\(sourceFiles\(/);
  const pkg = JSON.parse(read("../../package.json"));
  assert.match(pkg.scripts["validate:i18n"], /node scripts\/validate-i18n\.mjs/);
  assert.match(pkg.scripts.check, /node scripts\/validate-i18n\.mjs/);
  const ci = read("../../../.github/workflows/ci.yml");
  assert.match(ci, /^\s+run: yarn validate:i18n$/m);
  assert.match(ci, /^\s+run: yarn validate:commands$/m);
});
