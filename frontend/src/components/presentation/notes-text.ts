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
 * Speaker notes structure.
 *
 * Notes are authored as plain lines with the deck's inline markup: blank lines
 * separate paragraphs, `- ` / `* ` lines make a bullet list, `1. ` lines a
 * numbered list. This parses a notes string into that block structure so the
 * speaker view and the notes handout lay it out as a presenter reads it
 * (short paragraphs, real bullets) instead of one run of text. Inline markup
 * inside every line is rendered by `InlineMarkup`; this file never produces
 * HTML.
 */

type NotesBlock =
  | { kind: "paragraph"; lines: string[] }
  | { kind: "bullets"; items: string[] }
  | { kind: "numbered"; items: string[] };

const BULLET_RE = /^\s*[-*\u2022]\s+(.*)$/;
const NUMBERED_RE = /^\s*\d+[.)]\s+(.*)$/;

/** Split notes into paragraphs and lists. Never throws; empty input gives `[]`. */
export function parseNotesBlocks(notes: string | null | undefined): NotesBlock[] {
  if (!notes) return [];
  const blocks: NotesBlock[] = [];
  let paragraph: string[] = [];
  const flushParagraph = () => {
    if (paragraph.length) blocks.push({ kind: "paragraph", lines: paragraph });
    paragraph = [];
  };
  for (const raw of notes.replace(/\r\n/g, "\n").split("\n")) {
    const line = raw.trimEnd();
    if (!line.trim()) {
      flushParagraph();
      continue;
    }
    const bullet = BULLET_RE.exec(line);
    const numbered = bullet ? null : NUMBERED_RE.exec(line);
    if (bullet || numbered) {
      flushParagraph();
      const kind = bullet ? "bullets" : "numbered";
      const item = (bullet ?? numbered)![1].trim();
      const last = blocks[blocks.length - 1];
      if (last && last.kind === kind) {
        last.items.push(item);
      } else {
        blocks.push({ kind, items: [item] });
      }
      continue;
    }
    paragraph.push(line.trim());
  }
  flushParagraph();
  return blocks;
}
