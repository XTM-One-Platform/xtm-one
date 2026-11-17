/**
 * The i18n keys the UI source asks for, read statically.
 *
 * `validate-i18n.mjs` checks that every key found here exists in every
 * catalog. Two kinds of key are readable without running the code:
 *
 *  1. a string literal passed straight to `t_i18n(...)` - any quote style, a
 *     template literal without `${}` included - and each literal branch of a
 *     conditional passed to it (`t_i18n(count === 1 ? "item" : "items")`);
 *  2. every string VALUE of a module-scope table marked with a `@i18n-keys`
 *     JSDoc tag and closed by `as const` - the form dynamically-keyed copy
 *     takes (`t_i18n(STAGE_LABELS[stage.key])`) so it stays checkable. Every
 *     value of such a table must be a string literal: anything else in it is
 *     reported, never skipped.
 *
 * A key built at runtime anywhere else cannot be read and is not checked.
 * The source is read by a real parser (oxc, the one knip uses), so only a
 * real call counts: a `t_i18n("...")` written in a comment, a string,
 * template text, JSX text or a regex asks for nothing at runtime.
 */
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";
import { parseSync } from "oxc-parser";

/** Source files of `dir`, tests and stories excluded (they may ask for keys on purpose). */
export function sourceFiles(dir) {
  const out = [];
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry);
    if (statSync(full).isDirectory()) {
      if (entry !== "node_modules") out.push(...sourceFiles(full));
    } else if (/\.(ts|tsx)$/.test(entry) && !/\.(test|spec|stories)\.(ts|tsx)$/.test(entry)) {
      out.push(full);
    }
  }
  return out;
}

/**
 * `text` parsed as the file it comes from (JSX is only valid in a `.tsx`),
 * with its comments. A syntax error is reported: a file the parser cannot
 * read would otherwise ask for no key at all.
 */
function parse(text, fileName) {
  const { program, comments, errors } = parseSync(fileName, text, { lang: fileName.endsWith(".tsx") ? "tsx" : "ts" });
  return {
    text,
    program,
    comments,
    errors: errors.map((error) => ({ index: error.labels?.[0]?.start ?? 0, message: `syntax error: ${error.message}` })),
  };
}

/** The string `node` is when the source spells it out: a quoted or `${}`-free template literal. */
function literalText(node) {
  if (node?.type === "Literal" && typeof node.value === "string") return node.value;
  if (node?.type === "TemplateLiteral" && node.expressions.length === 0) return node.quasis[0].value.cooked;
  return null;
}

const TRANSPARENT = new Set([
  "ParenthesizedExpression",
  "TSAsExpression",
  "TSSatisfiesExpression",
  "TSNonNullExpression",
  "TSTypeAssertion",
]);

/** `node` without the wrappers that leave its value unchanged: `(x)`, `x as T`, `x satisfies T`, `x!`, `<T>x`. */
function unwrap(node) {
  while (node && TRANSPARENT.has(node.type)) node = node.expression;
  return node;
}

/** The literals an argument of `t_i18n` can evaluate to: itself, or each branch of a conditional. */
function argumentLiterals(node, out = []) {
  const value = unwrap(node);
  if (literalText(value) !== null) out.push(value);
  else if (value?.type === "ConditionalExpression") {
    argumentLiterals(value.consequent, out);
    argumentLiterals(value.alternate, out);
  }
  return out;
}

/** Whether `node` calls `t_i18n`, directly or as a method (`formatter.t_i18n(...)`). */
function callsTranslate(node) {
  if (node.type !== "CallExpression") return false;
  const callee = node.callee;
  if (callee.type === "Identifier") return callee.name === "t_i18n";
  return callee.type === "MemberExpression" && !callee.computed && callee.property.name === "t_i18n";
}

/** Every node under `node`, depth first. */
function walk(node, visit) {
  visit(node);
  for (const key in node) {
    const child = node[key];
    if (Array.isArray(child)) {
      for (const item of child) if (item && typeof item.type === "string") walk(item, visit);
    } else if (child && typeof child.type === "string") {
      walk(child, visit);
    }
  }
}

function literalKeysIn(source) {
  const out = [];
  walk(source.program, (node) => {
    if (callsTranslate(node) && node.arguments.length > 0) {
      for (const literal of argumentLiterals(node.arguments[0])) out.push({ key: literalText(literal), index: literal.start });
    }
  });
  return out.sort((a, b) => a.index - b.index);
}

/** Keys passed as a literal first argument to `t_i18n`, with the offset of each literal. */
export function literalKeys(text, fileName = "source.tsx") {
  return literalKeysIn(parse(text, fileName));
}

const TABLE_SHAPE = "a `const NAME = {...} as const` (or `[...]`) table at module scope";

/** Offsets of the `@i18n-keys` tags in the JSDoc comments between `from` and `statement`. */
function tagsBefore(source, from, statement) {
  const offsets = [];
  for (const comment of source.comments) {
    if (comment.type !== "Block" || comment.start < from || comment.end > statement.start) continue;
    if (!source.text.startsWith("/**", comment.start)) continue;
    for (const tag of source.text.slice(comment.start, comment.end).matchAll(/@i18n-keys\b/g)) {
      offsets.push(comment.start + tag.index);
    }
  }
  return offsets;
}

function notAKey(source, table, node, at = node?.start) {
  const text = node ? source.text.slice(node.start, node.end).replace(/\s+/g, " ") : "";
  const shown = text ? `\`${text.length > 60 ? `${text.slice(0, 57)}...` : text}\`` : "an empty array slot";
  return {
    index: at,
    message: `@i18n-keys table ${table}: ${shown} is not a string literal - every value of the table is a catalog key`,
  };
}

/** Every value of a table literal into `keys`; a value that is not a string literal into `problems`. */
function tableValues(source, table, node, keys, problems) {
  const value = unwrap(node);
  const text = literalText(value);
  if (text !== null) {
    keys.push({ key: text, index: value.start });
  } else if (value.type === "ObjectExpression") {
    // A property NAME is never a key: only what it maps to is read.
    for (const entry of value.properties) {
      if (entry.type === "Property" && entry.kind === "init" && !entry.method && !entry.shorthand) {
        tableValues(source, table, entry.value, keys, problems);
      } else {
        problems.push(notAKey(source, table, entry));
      }
    }
  } else if (value.type === "ArrayExpression") {
    let previousEnd = value.start;
    for (const element of value.elements) {
      // A hole (`[a, , b]`) has no node: it is reported where it sits.
      if (element) tableValues(source, table, element, keys, problems);
      else problems.push(notAKey(source, table, null, previousEnd));
      if (element) previousEnd = element.end;
    }
  } else {
    problems.push(notAKey(source, table, value));
  }
}

/** The variable declaration `statement` makes, exported or not. */
function declarationOf(statement) {
  if (statement.type === "ExportNamedDeclaration") return statement.declaration;
  return statement;
}

/** The table a tagged statement declares, or the reason it is not one. */
function readTable(source, statement) {
  const list = declarationOf(statement);
  if (list?.type !== "VariableDeclaration") return { problem: `@i18n-keys must tag ${TABLE_SHAPE}` };
  const [declaration] = list.declarations;
  if (list.kind !== "const" || list.declarations.length !== 1 || declaration.id.type !== "Identifier" || !declaration.init) {
    return { problem: `@i18n-keys must tag ${TABLE_SHAPE}` };
  }
  const name = declaration.id.name;
  let value = declaration.init;
  // `{...} as const satisfies T` checks the table's shape without widening it.
  while (value.type === "TSSatisfiesExpression" || value.type === "ParenthesizedExpression") value = value.expression;
  const isConst =
    value.type === "TSAsExpression" &&
    value.typeAnnotation.type === "TSTypeReference" &&
    value.typeAnnotation.typeName.type === "Identifier" &&
    value.typeAnnotation.typeName.name === "const";
  let literal = isConst ? value.expression : value;
  while (literal.type === "ParenthesizedExpression") literal = literal.expression;
  if (!isConst || !(literal.type === "ObjectExpression" || literal.type === "ArrayExpression")) {
    return { problem: `@i18n-keys table ${name} must be a literal closed by \`as const\`` };
  }
  const keys = [];
  const problems = [];
  tableValues(source, name, literal, keys, problems);
  return { name, keys, problems };
}

function keyTablesIn(source) {
  const tables = [];
  const problems = [...source.errors];
  const claimed = new Set();
  let previousEnd = 0;
  for (const statement of source.program.body) {
    const from = previousEnd;
    previousEnd = statement.end;
    const tags = tagsBefore(source, from, statement);
    if (tags.length === 0) continue;
    for (const tag of tags) claimed.add(tag);
    const table = readTable(source, statement);
    if (table.problem) {
      problems.push({ index: tags[0], message: table.problem });
      continue;
    }
    tables.push({ name: table.name, index: tags[0], keys: table.keys });
    problems.push(...table.problems);
  }
  // A tag anywhere else - on a function, in a function body, in a line comment - is an error, never a silent pass.
  for (const tag of source.text.matchAll(/@i18n-keys\b/g)) {
    if (!claimed.has(tag.index)) problems.push({ index: tag.index, message: `@i18n-keys must tag ${TABLE_SHAPE}` });
  }
  return { tables, problems };
}

/**
 * The string values of every `@i18n-keys` table: the object or array literal
 * a tagged module-scope `const` is initialised with, closed by `as const`.
 * A tag on anything else, and a value that is not a string literal, is a problem.
 */
export function keyTables(text, fileName = "source.tsx") {
  return keyTablesIn(parse(text, fileName));
}

/** The 1-based line of an offset of `text`: one pass over its line breaks, then a binary search per offset. */
function lineIndex(text) {
  const breaks = [];
  for (let at = text.indexOf("\n"); at !== -1; at = text.indexOf("\n", at + 1)) breaks.push(at);
  return (index) => {
    let low = 0;
    let high = breaks.length;
    while (low < high) {
      const mid = (low + high) >> 1;
      if (breaks[mid] < index) low = mid + 1;
      else high = mid;
    }
    return low + 1;
  };
}

/** Every statically readable key of a source file, with its line. */
export function sourceKeys(text, fileName = "source.tsx") {
  if (!text.includes("t_i18n") && !text.includes("@i18n-keys")) return { keys: [], problems: [] };
  const source = parse(text, fileName);
  const { tables, problems } = keyTablesIn(source);
  const lineOf = lineIndex(text);
  const keys = [...literalKeysIn(source), ...tables.flatMap((table) => table.keys)].map(({ key, index }) => ({
    key,
    line: lineOf(index),
  }));
  return { keys, problems: problems.map(({ index, message }) => ({ line: lineOf(index), message })) };
}

/** Read and scan `files`; `relative` names a file in messages. */
export function scanSource(files, relative = (f) => f) {
  const keys = [];
  const problems = [];
  for (const file of files) {
    const result = sourceKeys(readFileSync(file, "utf8"), file);
    for (const { key, line } of result.keys) keys.push({ key, where: `${relative(file)}:${line}` });
    for (const { line, message } of result.problems) problems.push(`${relative(file)}:${line}: ${message}`);
  }
  return { keys, problems };
}
