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
// Placeholder paths in MCP server arguments
// ---------------------------------------------------------------------------
// Some presets can only ship a documentation path (`/path/to/allowed/dir`) where
// the user's own directory belongs. Created as is, the server looks configured
// and fails at its first tool call with an error about the path, not about the
// step that was skipped - so the form refuses to create it. Dependency-free so
// `node --test` can import it.

const PLACEHOLDER_PREFIX = "/path/to/";

/** The value an argument carries: after `--flag=` / `key=`, without surrounding quotes. */
function argumentValue(token: string): string {
  const eq = token.indexOf("=");
  const value = eq >= 0 ? token.slice(eq + 1) : token;
  return value.replace(/^["']+|["']+$/g, "");
}

/** The first argument value that is still a documentation placeholder, else null. */
export function findPlaceholderPath(args: string): string | null {
  for (const token of args.split(/\s+/)) {
    const value = argumentValue(token);
    if (value === PLACEHOLDER_PREFIX.slice(0, -1) || value.startsWith(PLACEHOLDER_PREFIX)) {
      return value;
    }
  }
  return null;
}
