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
 * Speaker notes as a presenter reads them.
 *
 * `parseNotesBlocks` splits the notes into paragraphs and lists; every line is
 * rendered through `InlineMarkup`, the deck's own tokenizer — so `**42%**`
 * lands bold, never as literal asterisks, and nothing here is an HTML sink.
 * Used by the speaker window (big, dimmed type on a dark surface) and by the
 * notes handout (print type on paper); the surface sizes the type, this
 * component only shapes it.
 */
import { cn } from "@/lib/utils";

import { InlineMarkup } from "./inline-markup";
import { parseNotesBlocks } from "./notes-text";

export function SpeakerNotes({
  notes,
  empty,
  className,
}: {
  notes: string | null | undefined;
  /** What to show when there is nothing to say — the surface words it. */
  empty: string;
  className?: string;
}) {
  const blocks = parseNotesBlocks(notes);
  if (!blocks.length) {
    return <p className={cn("speaker-notes speaker-notes-empty", className)}>{empty}</p>;
  }
  return (
    <div className={cn("speaker-notes", className)}>
      {blocks.map((block, blockIndex) => {
        if (block.kind === "paragraph") {
          return (
            <p key={blockIndex}>
              {block.lines.map((line, lineIndex) => (
                <span key={lineIndex}>
                  {lineIndex > 0 && <br />}
                  <InlineMarkup value={line} />
                </span>
              ))}
            </p>
          );
        }
        const List = block.kind === "numbered" ? "ol" : "ul";
        return (
          <List key={blockIndex}>
            {block.items.map((item, itemIndex) => (
              <li key={itemIndex}>
                <InlineMarkup value={item} />
              </li>
            ))}
          </List>
        );
      })}
    </div>
  );
}
