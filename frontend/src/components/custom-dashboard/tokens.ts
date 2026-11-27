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
 * The tokens of a dashboard page.
 *
 * A definition refers to a token as `$name$`; `$name|f$` says how its value
 * is written: `s` quoted, `h` HTML-escaped, `u` URL-encoded, `n` as is. The
 * page holds the tokens, from the weakest to the strongest:
 *
 * - the viewer's environment (`env:user`, `env:user_name`, `env:timezone`,
 *   `env:locale`, `env:platform`), which nothing overrides;
 * - the definition's defaults (`defaults.tokens.default.<name>.value`) and
 *   each input's `defaultValue`;
 * - what the person sets (inputs, buttons);
 * - computed tokens (`expressions.tokens.<name>`: an expression on the
 *   others, read by :func:`evaluate` - no `eval`, no `Function`);
 * - a data source's result (`<source>:result.<field>`, `<source>:job.status`,
 *   `<source>:job.lastUpdated`).
 *
 * The data sources get theirs on the server, as filter values
 * (`dashboard_tokens`); here they fill titles, texts and links.
 * Pure: `tokens.test.ts` runs it on `node --test`.
 */
import type { WidgetData } from "../../types";

export type TokenValue = string | string[];
export type Tokens = Record<string, TokenValue>;

/** `$name$` or `$name|f$` (`dashboard_tokens.PLACEHOLDER_RE`). */
const PLACEHOLDER = /\$([A-Za-z_][A-Za-z0-9_.:]{0,99})(?:\|([shun]))?\$/g;

const isSet = (value: TokenValue | undefined): value is TokenValue =>
  value !== undefined && value !== "" && !(Array.isArray(value) && value.length === 0);

/** Every token name a value of the definition (any JSON) refers to. */
export function namesIn(value: unknown, into: Set<string> = new Set()): Set<string> {
  if (typeof value === "string") {
    for (const match of value.matchAll(PLACEHOLDER)) into.add(match[1]);
  } else if (Array.isArray(value)) {
    for (const item of value) namesIn(item, into);
  } else if (typeof value === "object" && value !== null) {
    for (const item of Object.values(value)) namesIn(item, into);
  }
  return into;
}

function written(value: TokenValue, filter: string | undefined, destination: "text" | "url"): string {
  const text = Array.isArray(value) ? value.join(",") : value;
  const applied = filter ?? (destination === "url" ? "u" : "n");
  if (applied === "s") return `"${text.replace(/\\/g, "\\\\").replace(/"/g, '\\"')}"`;
  if (applied === "h") return text.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  if (applied === "u") return encodeURIComponent(text);
  return text;
}

/**
 * `template` with its tokens written in, or `null` while one has no value
 * (the element waits, like a source). A URL takes each token URL-encoded unless
 * its filter says otherwise.
 */
export function fill(template: string, tokens: Tokens, destination: "text" | "url" = "text"): string | null {
  let missing = false;
  const out = template.replace(PLACEHOLDER, (_, name: string, filter: string | undefined) => {
    const value = tokens[name];
    if (!isSet(value)) {
      missing = true;
      return "";
    }
    return written(value, filter, destination);
  });
  return missing ? null : out;
}

/** `template` filled, or as written with its unset tokens left visible: for a title, which never waits. */
export function fillOrKeep(template: string, tokens: Tokens): string {
  return template.replace(PLACEHOLDER, (whole, name: string, filter: string | undefined) => {
    const value = tokens[name];
    return isSet(value) ? written(value, filter, "text") : whole;
  });
}

/** The tokens a source sends with its reads: the ones its definition (and its parents') names, set or not. */
export function tokensFor(definition: unknown, tokens: Tokens): Tokens {
  const out: Tokens = {};
  for (const name of [...namesIn(definition)].sort()) if (isSet(tokens[name])) out[name] = tokens[name];
  return out;
}

// ── Environment and results ─────────────────────────────────────────────────

export interface Environment {
  user?: string;
  userName?: string;
  timezone?: string;
  locale?: string;
  platform?: string;
}

export function environmentTokens(env: Environment): Tokens {
  const out: Tokens = {};
  const entries: [string, string | undefined][] = [
    ["env:user", env.user],
    ["env:user_name", env.userName],
    ["env:timezone", env.timezone],
    ["env:locale", env.locale],
    ["env:platform", env.platform],
  ];
  for (const [name, value] of entries) if (value) out[name] = value;
  return out;
}

/** A source's result and status as tokens: `<sid>:result.<field>`, `<sid>:job.status`, `<sid>:job.lastUpdated`. */
export function resultTokens(sourceId: string, data: WidgetData | undefined, fetching: boolean): Tokens {
  const out: Tokens = {};
  const at = (field: string, value: unknown) => {
    if (value !== null && value !== undefined && value !== "") out[`${sourceId}:result.${field}`] = String(value);
  };
  if (!data) {
    out[`${sourceId}:job.status`] = fetching ? "running" : "none";
    return out;
  }
  if (data.status === "error") {
    out[`${sourceId}:job.status`] = data.code;
    return out;
  }
  out[`${sourceId}:job.status`] = "done";
  out[`${sourceId}:job.lastUpdated`] = data.fetched_at;
  switch (data.shape) {
    case "number":
      at("value", data.data.value);
      at("change", data.data.change);
      break;
    case "distribution":
      at("label", data.data.items[0]?.label);
      at("value", data.data.items[0]?.value);
      at("count", data.data.items.length);
      break;
    case "series": {
      const first = data.data.series[0];
      const last = first?.points[first.points.length - 1];
      at("x", last?.x);
      at("y", last?.y);
      break;
    }
    case "rows":
      for (const [key, value] of Object.entries(data.data.rows[0] ?? {})) if (key !== "_url") at(key, value);
      at("count", data.data.total ?? data.data.rows.length);
      break;
    case "text":
      at("text", data.data.text);
      break;
  }
  return out;
}

// ── Computed tokens ─────────────────────────────────────────────────────────

type Value = string | number | boolean;
type Node =
  | { kind: "literal"; value: Value }
  | { kind: "token"; name: string }
  | { kind: "unary"; op: "-" | "!"; arg: Node }
  | { kind: "binary"; op: string; left: Node; right: Node }
  | { kind: "call"; name: string; args: Node[] };

const FUNCTIONS: Record<string, [number, number]> = {
  if: [3, 3],
  isSet: [1, 1],
  in: [2, 21],
  len: [1, 1],
  lower: [1, 1],
  upper: [1, 1],
  round: [1, 2],
  coalesce: [1, 5],
  number: [1, 1],
};
const LEVELS = [["||"], ["&&"], ["==", "!=", "="], ["<", "<=", ">", ">="], ["+", "-"], ["*", "/"]];
const TOKEN_RE = /\s*(?:(\$[A-Za-z_][A-Za-z0-9_.:]{0,99}\$)|("(?:[^"\\]|\\.)*")|(\d+(?:\.\d+)?)|([A-Za-z_]\w*)|(\|\||&&|==|!=|<=|>=|[-+*/<>!(),=]))/y;

/** An expression as a tree, or an `Error` naming what is wrong (no JavaScript is ever run). */
export function parse(source: string): Node {
  if (source.length > 300) throw new Error("an expression is at most 300 characters");
  const tokens: string[] = [];
  TOKEN_RE.lastIndex = 0;
  let index = 0;
  while (index < source.length) {
    if (!source.slice(index).trim()) break;
    TOKEN_RE.lastIndex = index;
    const match = TOKEN_RE.exec(source);
    if (!match) throw new Error(`unexpected '${source.slice(index).trim()[0]}'`);
    tokens.push(match[1] ?? match[2] ?? match[3] ?? match[4] ?? match[5]);
    index = TOKEN_RE.lastIndex;
  }
  let pos = 0;
  const peek = () => tokens[pos];
  const take = () => {
    if (pos >= tokens.length) throw new Error("the expression ends too early");
    return tokens[pos++];
  };
  const level = (n: number): Node => {
    if (n === LEVELS.length) return unary();
    let node = level(n + 1);
    while (peek() !== undefined && LEVELS[n].includes(peek())) {
      const op = take();
      node = { kind: "binary", op, left: node, right: level(n + 1) };
    }
    return node;
  };
  const unary = (): Node => {
    if (peek() === "-" || peek() === "!") {
      const op = take() as "-" | "!";
      return { kind: "unary", op, arg: unary() };
    }
    return atom();
  };
  const atom = (): Node => {
    const tok = take();
    if (tok.startsWith("$")) return { kind: "token", name: tok.slice(1, -1) };
    if (tok.startsWith('"')) return { kind: "literal", value: JSON.parse(tok) as string };
    if (/^\d/.test(tok)) return { kind: "literal", value: Number(tok) };
    if (tok === "true" || tok === "false") return { kind: "literal", value: tok === "true" };
    if (tok === "(") {
      const node = level(0);
      if (take() !== ")") throw new Error("a ')' is missing");
      return node;
    }
    if (/^[A-Za-z_]/.test(tok)) {
      const arity = FUNCTIONS[tok];
      if (!arity || take() !== "(") throw new Error(`unknown function '${tok}'`);
      const args: Node[] = [];
      if (peek() !== ")") {
        args.push(level(0));
        while (peek() === ",") {
          take();
          args.push(level(0));
        }
      }
      if (take() !== ")") throw new Error("a ')' is missing");
      if (args.length < arity[0] || args.length > arity[1]) throw new Error(`${tok} takes ${arity[0]}-${arity[1]} arguments`);
      return { kind: "call", name: tok, args };
    }
    throw new Error(`unexpected '${tok}'`);
  };
  const tree = level(0);
  if (pos !== tokens.length) throw new Error(`unexpected '${tokens[pos]}'`);
  return tree;
}

const toNumber = (value: Value): number => (typeof value === "number" ? value : typeof value === "boolean" ? Number(value) : Number(value));
const toText = (value: Value): string => (typeof value === "string" ? value : String(value));
const truthy = (value: Value): boolean => (typeof value === "string" ? value !== "" && value !== "false" && value !== "0" : Boolean(value));

function run(node: Node, tokens: Tokens): Value {
  switch (node.kind) {
    case "literal":
      return node.value;
    case "token": {
      const value = tokens[node.name];
      if (!isSet(value)) throw new Error(`token ${node.name} is not set`);
      return Array.isArray(value) ? value.join(",") : value;
    }
    case "unary": {
      const arg = run(node.arg, tokens);
      return node.op === "!" ? !truthy(arg) : -toNumber(arg);
    }
    case "binary": {
      if (node.op === "&&") return truthy(run(node.left, tokens)) && truthy(run(node.right, tokens));
      if (node.op === "||") return truthy(run(node.left, tokens)) || truthy(run(node.right, tokens));
      const left = run(node.left, tokens);
      const right = run(node.right, tokens);
      const numeric = !Number.isNaN(toNumber(left)) && !Number.isNaN(toNumber(right)) && left !== "" && right !== "";
      switch (node.op) {
        case "+":
          return numeric ? toNumber(left) + toNumber(right) : toText(left) + toText(right);
        case "-":
          return toNumber(left) - toNumber(right);
        case "*":
          return toNumber(left) * toNumber(right);
        case "/":
          return toNumber(right) === 0 ? Number.NaN : toNumber(left) / toNumber(right);
        case "==":
        case "=":
          return numeric ? toNumber(left) === toNumber(right) : toText(left) === toText(right);
        case "!=":
          return numeric ? toNumber(left) !== toNumber(right) : toText(left) !== toText(right);
        default: {
          const [a, b] = numeric ? [toNumber(left), toNumber(right)] : [toText(left), toText(right)];
          return node.op === "<" ? a < b : node.op === "<=" ? a <= b : node.op === ">" ? a > b : a >= b;
        }
      }
    }
    case "call": {
      // Whether a token has a value: the one question about an unset token that does not fail.
      if (node.name === "isSet") {
        const arg = node.args[0];
        if (arg.kind !== "token") throw new Error("isSet takes a token");
        return isSet(tokens[arg.name]);
      }
      if (node.name === "in") {
        const [first, ...rest] = node.args.map((arg) => toText(run(arg, tokens)));
        return rest.includes(first);
      }
      if (node.name === "if") return truthy(run(node.args[0], tokens)) ? run(node.args[1], tokens) : run(node.args[2], tokens);
      if (node.name === "coalesce") {
        for (const arg of node.args) {
          try {
            const value = run(arg, tokens);
            if (value !== "") return value;
          } catch {
            // an unset token: try the next one
          }
        }
        throw new Error("no value");
      }
      const args = node.args.map((arg) => run(arg, tokens));
      if (node.name === "len") return toText(args[0]).length;
      if (node.name === "lower") return toText(args[0]).toLowerCase();
      if (node.name === "upper") return toText(args[0]).toUpperCase();
      if (node.name === "number") return toNumber(args[0]);
      const digits = args.length > 1 ? Math.max(0, Math.min(10, Math.trunc(toNumber(args[1])))) : 0;
      return Math.round(toNumber(args[0]) * 10 ** digits) / 10 ** digits;
    }
  }
}

/** An expression's value as a token (text), or `undefined` while a token it reads is unset or it is malformed. */
export function evaluate(expression: string, tokens: Tokens): string | undefined {
  try {
    const value = run(parse(expression), tokens);
    if (typeof value === "number" && !Number.isFinite(value)) return undefined;
    return toText(value);
  } catch {
    return undefined;
  }
}

/** Whether a condition holds: its expression is true; one reading an unset token (but through `isSet`) does not. */
export function holds(expression: string, tokens: Tokens): boolean {
  try {
    return truthy(run(parse(expression), tokens));
  } catch {
    return false;
  }
}

/** The page's tokens, strongest last: environment, defaults, what was set, computed ones, results. */
export function resolveTokens({
  environment,
  defaults,
  set,
  expressions,
  results,
}: {
  environment: Tokens;
  defaults: Tokens;
  set: Tokens;
  expressions: Record<string, string>;
  results: Tokens;
}): Tokens {
  const tokens: Tokens = { ...defaults, ...set, ...results, ...environment };
  // Computed tokens may read each other: a few passes settle a chain; a cycle stops after them.
  for (let pass = 0; pass < 4; pass += 1) {
    let changed = false;
    for (const [name, expression] of Object.entries(expressions)) {
      const value = evaluate(expression, tokens);
      if (value !== undefined && tokens[name] !== value) {
        tokens[name] = value;
        changed = true;
      }
    }
    if (!changed) break;
  }
  return tokens;
}
