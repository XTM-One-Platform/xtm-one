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
 * The pure parts of a member's instruction editor (dev-docs/ctem-fleets.md
 * 10.5, R5): where "Use the fleet documentation" takes its text from, and
 * whether a new text (a prefill, an AI draft) may fill the editor at once or
 * must ask before replacing what the person typed (A4).
 */
import type { FleetDocumentation } from "@/hooks/api/useFleetApi";

/** The collaboration instruction's limit, the API's `max_length`. */
export const INSTRUCTION_MAX = 4000;

/** Strip the markdown a documentation bullet carries around a member's name. */
function plain(line: string): string {
  return line
    .replace(/^\s*(?:[-*+]|\d+[.)])\s+/, "")
    .replace(/\*\*|__|`/g, "")
    .trim();
}

/**
 * What the fleet documentation says about *memberName*: the lines of its
 * "members" section that name it (one bullet per member, as the documentation
 * draft writes them), without the bullet and the name itself. `null` when the
 * documentation has no members section or none of its lines names the member.
 */
export function documentationLines(doc: FleetDocumentation | undefined, memberName: string): string | null {
  const section = doc?.sections?.members;
  const name = memberName.trim().toLowerCase();
  if (!section || !name) return null;
  const lines = section
    .split(/\r?\n/)
    .map(plain)
    .filter((line) => line.toLowerCase().startsWith(name) || line.toLowerCase().includes(` ${name} `));
  if (lines.length === 0) return null;
  const text = lines
    .map((line) => (line.toLowerCase().startsWith(name) ? line.slice(name.length).replace(/^\s*[:\-.]\s*/, "") : line))
    .filter((line) => line !== "")
    .join("\n")
    .trim();
  return text === "" ? null : text.slice(0, INSTRUCTION_MAX);
}

/**
 * The prefill of "Use the fleet documentation": the documentation's lines
 * about the member when it has some, else the instruction the fleet gives every
 * connected member (its default), else nothing to prefill (`null`).
 */
export function instructionPrefill(
  doc: FleetDocumentation | undefined,
  memberName: string,
  fleetDefault: string | null | undefined,
): string | null {
  const fromDoc = documentationLines(doc, memberName);
  if (fromDoc) return fromDoc;
  const fallback = (fleetDefault ?? "").trim();
  return fallback === "" ? null : fallback.slice(0, INSTRUCTION_MAX);
}

/**
 * How a new text reaches the editor: `fill` an empty field (or one holding the
 * same text), `ask` before replacing typed text; never a silent overwrite (A4).
 */
export function fillDecision(current: string, next: string): "fill" | "ask" {
  const typed = current.trim();
  return typed === "" || typed === next.trim() ? "fill" : "ask";
}
