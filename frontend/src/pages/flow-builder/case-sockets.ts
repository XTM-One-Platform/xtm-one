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
 * Per-case output sockets: a node with a `case` output - a Switch, a
 * Classify (CLM) node - grows one socket per entry of its `cases` config
 * (`case:<name>`, the Blueprint shape) instead of a single `case` handle
 * routed by edge label. Mirrors the server's `get_port` /
 * `PER_CASE_SOCKET_KINDS`, which derive the same family from the same port.
 *
 * No React and no `@/` import, so `case-sockets.test.ts` runs under
 * `node --test`.
 */
import type { FlowPort } from "../../types/index.ts";

/**
 * *outputs* with the `case` port replaced by one socket per case name. The
 * legacy `case` handle stays only while an old-style edge still uses it, and
 * a node with no cases yet keeps it as the drag affordance.
 */
export function withCaseSockets(
  outputs: FlowPort[],
  cases: unknown,
  connectedSources: ReadonlySet<string>,
): FlowPort[] {
  const casePort = outputs.find((p) => p.id === "case");
  if (!casePort) return outputs;
  const names =
    cases && typeof cases === "object" && !Array.isArray(cases)
      ? Object.keys(cases as Record<string, unknown>).filter(Boolean)
      : [];
  if (names.length === 0) return outputs;
  return [
    ...(connectedSources.has("case") ? [casePort] : []),
    ...names.map((name) => ({ ...casePort, id: `case:${name}`, label: name })),
    ...outputs.filter((p) => p.id !== "case"),
  ];
}
