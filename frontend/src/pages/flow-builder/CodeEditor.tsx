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
 * Dependency-free code editor with syntax highlighting.
 *
 * No highlighter library exists in the bundle (checked: no shiki / prism /
 * highlight.js / codemirror, even transitively), so this is the classic
 * overlay trick: a highlighted <pre> renders the tokens and SIZES the box,
 * a transparent <textarea> with identical font metrics sits on top and
 * carries the caret, selection and input. Both wrap identically and share
 * one scrolling container, so nothing needs scroll syncing.
 *
 * The tokenizer is a small per-language regex — comments, strings,
 * keywords, numbers, decorators (JSON: keys, strings, literals, numbers) — deliberately approximate: it colours
 * keywords for the human eye, it does not parse. Correctness lives in the
 * Validate button (server-side lint), not here.
 */

import type React from "react";
import { useMemo } from "react";
import { cn } from "@/lib/utils";
import type { CustomToolLanguage } from "@/types";

const PY_KEYWORDS =
  "False|None|True|and|as|assert|async|await|break|class|continue|def|del|" +
  "elif|else|except|finally|for|from|global|if|import|in|is|lambda|not|or|" +
  "pass|raise|return|try|while|with|yield|match|case";
const JS_KEYWORDS =
  "async|await|break|case|catch|class|const|continue|debugger|default|" +
  "delete|do|else|export|extends|finally|for|function|if|import|in|" +
  "instanceof|let|new|of|return|static|super|switch|this|throw|try|typeof|" +
  "undefined|null|true|false|var|void|while|with|yield";

/** The languages it colours: a custom tool's, and JSON (the dashboard source editor). */
type CodeEditorLanguage = CustomToolLanguage | "json";

/** One alternation per language; group ORDER is the precedence. */
const TOKENIZERS: Record<CodeEditorLanguage, RegExp> = {
  python: new RegExp(
    [
      String.raw`(#[^\n]*)`, // 1 comment
      String.raw`("""[\s\S]*?"""|'''[\s\S]*?'''|"(?:[^"\\\n]|\\.)*"|'(?:[^'\\\n]|\\.)*')`, // 2 string
      String.raw`(@\w[\w.]*)`, // 3 decorator
      String.raw`\b(${PY_KEYWORDS})\b`, // 4 keyword
      String.raw`\b(\d[\d_]*(?:\.\d[\d_]*)?)\b`, // 5 number
    ].join("|"),
    "g",
  ),
  javascript: new RegExp(
    [
      String.raw`(\/\/[^\n]*|\/\*[\s\S]*?\*\/)`, // 1 comment
      // 2 string — template / double / single quoted (plain string: the
      // pattern contains backticks, which template literals cannot carry raw)
      "(`(?:[^`\\\\]|\\\\[\\s\\S])*`|\"(?:[^\"\\\\\\n]|\\\\.)*\"|'(?:[^'\\\\\\n]|\\\\.)*')",
      String.raw`(\$\w+)`, // 3 injected input ($var)
      String.raw`\b(${JS_KEYWORDS})\b`, // 4 keyword
      String.raw`\b(\d[\d_]*(?:\.\d[\d_]*)?)\b`, // 5 number
    ].join("|"),
    "g",
  ),
  json: new RegExp(
    [
      String.raw`([^\s\S])`, // 1 (JSON has no comment)
      String.raw`("(?:[^"\\\n]|\\.)*"(?!\s*:))`, // 2 string value
      String.raw`("(?:[^"\\\n]|\\.)*")`, // 3 key (a string before a colon)
      String.raw`\b(true|false|null)\b`, // 4 literal
      String.raw`(-?\b\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)\b`, // 5 number
    ].join("|"),
    "g",
  ),
};

const TOKEN_CLASSES = [
  "", // unused (full match)
  "text-feedback-success-primary/80", // comment
  "text-feedback-warning-primary light:text-feedback-warning-primary", // string
  "text-filigran-tonic-primary light:text-filigran-tonic-primary", // decorator / $input
  "text-feedback-info-primary light:text-feedback-info-primary", // keyword
  "text-filigran-ia-primary light:text-filigran-ia-primary", // number
];

function highlight(code: string, language: CodeEditorLanguage): React.ReactNode[] {
  const out: React.ReactNode[] = [];
  const re = new RegExp(TOKENIZERS[language].source, "g");
  let last = 0;
  let key = 0;
  for (let m = re.exec(code); m; m = re.exec(code)) {
    if (m.index > last) out.push(code.slice(last, m.index));
    const group = m.slice(1).findIndex((g) => g !== undefined) + 1;
    out.push(
      <span key={key++} className={TOKEN_CLASSES[group] || undefined}>
        {m[0]}
      </span>,
    );
    last = m.index + m[0].length;
    if (m[0].length === 0) re.lastIndex += 1; // safety against empty matches
  }
  if (last < code.length) out.push(code.slice(last));
  return out;
}

interface CodeEditorProps {
  value: string;
  language: CodeEditorLanguage;
  onChange: (value: string) => void;
  placeholder?: string;
  disabled?: boolean;
  className?: string;
  /** The textarea's accessible name. */
  ariaLabel?: string;
}

/** Shared metrics — the overlay only works if BOTH layers use exactly these. */
const METRICS =
  "whitespace-pre-wrap break-words p-3 font-mono-plex content-compact leading-5";

export function CodeEditor({
  value,
  language,
  onChange,
  placeholder,
  disabled,
  className,
  ariaLabel,
}: CodeEditorProps) {
  const tokens = useMemo(() => highlight(value, language), [value, language]);
  return (
    <div
      className={cn(
        "relative overflow-auto rounded-sm border border-elevation-subtle bg-elevation-default",
        className,
      )}
    >
      <pre aria-hidden className={cn("pointer-events-none m-0 min-h-full", METRICS)}>
        {tokens}
        {"\n" /* trailing line so the caret's last row has height */}
      </pre>
      <textarea
        className={cn(
          "absolute inset-0 h-full w-full resize-none bg-transparent text-transparent",
          "caret-filigran-tonic-primary outline-none selection:bg-accent/20",
          "placeholder:text-default-secondary",
          METRICS,
        )}
        aria-label={ariaLabel}
        spellCheck={false}
        autoCapitalize="off"
        autoComplete="off"
        value={value}
        placeholder={value ? undefined : placeholder}
        disabled={disabled}
        onChange={(e: React.ChangeEvent<HTMLTextAreaElement>) =>
          onChange(e.target.value)
        }
      />
    </div>
  );
}
