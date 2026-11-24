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
 * A work item's description as a person reads it (#776, scope F): the notes
 * the platform stamps on top ("YYYY-MM-DD HH:MMZ (run <uuid>): ...", newest
 * first, `stamped_note` in backend/app/services/work_item.py) split from the
 * brief they were written over, and the long ids of a note body turned into
 * compact chips. Presentation only: the stored text agents read is never
 * rewritten.
 */

/** One stamped note: when, which run wrote it (when one did), what it says. */
export interface WorkItemNote {
  /** ISO-8601 UTC instant of the stamp (minute precision). */
  at: string;
  runId: string | null;
  body: string;
}

interface SplitDescription {
  /** Newest first, as stored. */
  notes: WorkItemNote[];
  /** The text the notes were written over; "" when there is none. */
  brief: string;
}

const UUID = "[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}";
// A stamp opens a paragraph: "2026-11-20 22:21Z" or "2026-11-20T22:21Z",
// optionally "(run <uuid>)" or "(run <uuid>, anything)", then ": ".
const STAMP = new RegExp(
  `^(\\d{4}-\\d{2}-\\d{2})[ T](\\d{2}:\\d{2})(?::\\d{2})?Z(?: \\(run (${UUID})(?:[,;][^)]*)?\\))?: `,
  "i",
);

function stampOf(paragraph: string): { at: string; runId: string | null; rest: string } | null {
  const m = STAMP.exec(paragraph);
  if (!m) return null;
  return { at: `${m[1]}T${m[2]}:00Z`, runId: m[3] ? m[3].toLowerCase() : null, rest: paragraph.slice(m[0].length) };
}

/**
 * A stamp repeated inside the body (an agent that typed the time and its run
 * after the platform stamped them) is dropped, at its start and at its end.
 */
function withoutRepeatedStamp(body: string, runId: string | null): string {
  let text = body;
  // Leading: as many nested stamps as were typed.
  for (let inner = stampOf(text); inner; inner = stampOf(text)) text = inner.rest;
  // Leading bare time a writer typed: "22:21Z: " or "22:21Z ".
  text = text.replace(/^\d{2}:\d{2}Z:? /, "");
  if (runId) {
    // Trailing "(run 8f240eba...)" naming the stamp's own run.
    const prefix = runId.slice(0, 8);
    text = text.replace(new RegExp(`\\s*\\(run ${prefix}[0-9a-f-]*(?:\\.{3}|\u2026)?[^)]*\\)\\.?\\s*$`, "i"), "");
  }
  return text.trim();
}

/**
 * Split *description* into its stamped notes and its brief.
 *
 * Notes are paragraphs opening with a stamp; the paragraphs after a stamp
 * belong to its note until the next stamp. The platform writes no marker
 * between the oldest note and the brief, so the oldest note keeps its first
 * paragraph only and the rest is the brief (a one-paragraph note, the
 * "started" line, in practice).
 */
export function splitDescription(description: string | null | undefined): SplitDescription {
  const text = (description ?? "").replace(/\r\n/g, "\n");
  if (!text.trim()) return { notes: [], brief: "" };
  const paragraphs = text.split(/\n{2,}/);
  const notes: WorkItemNote[] = [];
  let i = 0;
  while (i < paragraphs.length) {
    const stamp = stampOf(paragraphs[i]);
    if (!stamp) break;
    const parts = [stamp.rest];
    let j = i + 1;
    // Continuation paragraphs, only when another stamp follows them.
    let next = j;
    while (next < paragraphs.length && !stampOf(paragraphs[next])) next += 1;
    if (next < paragraphs.length) {
      parts.push(...paragraphs.slice(j, next));
      j = next;
    }
    notes.push({ at: stamp.at, runId: stamp.runId, body: withoutRepeatedStamp(parts.join("\n\n"), stamp.runId) });
    i = j;
  }
  return { notes, brief: paragraphs.slice(i).join("\n\n").trim() };
}

const CODE = /(```[\s\S]*?```|`[^`\n]*`|\[[^\]\n]*\]\([^)\n]*\))/g;
const REPO_REF = /\b([A-Za-z0-9][\w.-]*\/[A-Za-z0-9][\w.-]*)#(\d+)\b/g;
const SHA40 = /\b[0-9a-f]{40}\b/gi;
const UUID_G = new RegExp(`\\b${UUID}\\b`, "gi");
// Teams / Graph message ids, check-run and workflow-run ids: 11+ digits.
const LONG_DIGITS = /(?<![\w.,/:-])\d{11,}(?![\w.,/:-])/g;
const BARE_REF = /(^|[\s(])#(\d{1,6})\b/g;

/**
 * The marker kind an id chip is written as: `[1a2b3c4d](#inline-marker/ID/<full>)`,
 * the inline-marker link ChatMarkdown already draws through
 * `InlineMarkerContext` (#11697), so no new link scheme reaches the renderer.
 * The values are hex or digits only: nothing to escape.
 */
export const ID_CHIP_KIND = "ID";
const ID_CHIP_PREFIX = `#inline-marker/${ID_CHIP_KIND}/`;

/**
 * *markdown* with its long ids as chip links (`[1a2b3c4d](#xtm-id=<full>)`)
 * and its pull request / issue refs and full commit SHAs as GitHub links.
 * Code spans, fenced blocks and existing links are left as written. *repo*
 * (owner/name) links a bare `#N` and a SHA; without it they stay text / chip.
 */
export function linkifyIds(markdown: string, repo: string | null = null): string {
  return markdown
    .split(CODE)
    .map((part, index) => {
      if (index % 2 === 1) return part; // a code span, a fence or a link
      let out = part.replace(REPO_REF, (_m, r: string, n: string) => `[${r}#${n}](https://github.com/${r}/issues/${n})`);
      out = out.replace(SHA40, (sha: string) =>
        repo ? `[${sha.slice(0, 8)}](https://github.com/${repo}/commit/${sha})` : `[${sha.slice(0, 8)}](${ID_CHIP_PREFIX}${sha})`,
      );
      out = out.replace(UUID_G, (id: string) => `[${id.slice(0, 8)}](${ID_CHIP_PREFIX}${id})`);
      out = out.replace(LONG_DIGITS, (id: string) => `[${id.slice(0, 8)}\u2026](${ID_CHIP_PREFIX}${id})`);
      if (repo) out = out.replace(BARE_REF, (_m, lead: string, n: string) => `${lead}[#${n}](https://github.com/${repo}/issues/${n})`);
      return out;
    })
    .join("");
}

/** Lines a note shows before "Show more". */
const NOTE_FOLD_LINES = 6;

/** Whether a note body is long enough to be folded. */
export function isLongNote(body: string): boolean {
  const lines = body.split("\n").reduce((n, line) => n + Math.max(1, Math.ceil(line.length / 90)), 0);
  return lines > NOTE_FOLD_LINES;
}
