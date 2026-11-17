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

// ---------------------------------------------------------------------------
// The arguments an MCP server edit sends
// ---------------------------------------------------------------------------
// The API shows a stdio server's arguments with every credential masked
// (`ghp_xx***`, `--token ***`), and the form shows them on one line. An
// untouched field sends nothing and leaves the stored arguments alone; an
// edited one is read back the way a shell reads a command line, so an argument
// holding a space (`"Authorization: Bearer ***"`) stays one argument and the
// backend can put its masked value back. Dependency-free so `node --test` can
// import it.

const NEEDS_QUOTES = /[\s"'\\]/;

/** One argument as the field shows it: double-quoted when it holds a space, a quote or a backslash. */
function quoteArg(arg: string): string {
  if (arg && !NEEDS_QUOTES.test(arg)) return arg;
  return `"${arg.replace(/[\\"]/g, "\\$&")}"`;
}

/** The text the arguments field starts from. */
export function argsFieldText(args: string[] | null | undefined): string {
  return (args ?? []).map(quoteArg).join(" ");
}

/**
 * The arguments a command line holds: split on whitespace, except inside
 * `"..."` (where `\"` and `\\` stand for `"` and `\`) or `'...'`.
 */
export function splitArgs(text: string): string[] {
  const args: string[] = [];
  let current = "";
  let inArg = false;
  let quote: '"' | "'" | null = null;
  for (let i = 0; i < text.length; i++) {
    const ch = text[i];
    if (quote === "'") {
      if (ch === "'") quote = null;
      else current += ch;
    } else if (quote === '"') {
      if (ch === "\\" && (text[i + 1] === '"' || text[i + 1] === "\\")) current += text[++i];
      else if (ch === '"') quote = null;
      else current += ch;
    } else if (/\s/.test(ch)) {
      if (inArg) args.push(current);
      current = "";
      inArg = false;
    } else {
      inArg = true;
      if (ch === '"' || ch === "'") quote = ch;
      else current += ch;
    }
  }
  if (inArg) args.push(current);
  return args;
}

/** The arguments to send, or `undefined` when the field is blank or as it was read. */
export function editedArgs(field: string, original: string[] | null | undefined): string[] | undefined {
  const text = field.trim();
  if (!text || text === argsFieldText(original).trim()) return undefined;
  return splitArgs(text);
}
