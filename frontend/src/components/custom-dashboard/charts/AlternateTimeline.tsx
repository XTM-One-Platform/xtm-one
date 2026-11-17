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
 * A timeline drawn `alternate` (`viz/alternate.ts`): down a vertical line,
 * each event's date on one side and its card on the other, swapping sides,
 * an outlined dot in the event's colour holding its icon between them. The
 * card shows the event's label (16 px, weight 500, lower case but its first
 * letter, as the modules' headings) and its second line
 * (Markdown, cut to 150 characters). The dot opens the event's platform
 * link, built by the server and checked again, in a new tab.
 */
import { DeckIcon } from "@/components/presentation/DeckIcon";
import { useFormatter } from "@/i18n";
import { cn } from "@/lib/utils";
import type { WidgetEventsData } from "@/types";

import { alternateEntries, type AlternateEntry } from "../viz/alternate";
import { resolveColor } from "./colors";
import { formatNamedDate } from "./formats";
import MarkdownText from "./MarkdownText";
import { dataHref } from "./links";

function Dot({ entry }: { entry: AlternateEntry }) {
  const href = dataHref(entry.link);
  const color = resolveColor(entry.color) ?? "var(--text-default-secondary)";
  const dot = (
    <span
      className="my-[11.5px] flex h-9 w-9 items-center justify-center rounded-full border-2 bg-transparent"
      style={{ borderColor: color, color }}
      data-dot-color={entry.color ?? ""}
    >
      <DeckIcon name={entry.icon ?? "circle"} size={20} />
    </span>
  );
  return href ? (
    <a href={href} target="_blank" rel="noopener noreferrer" aria-label={entry.label} className="rounded-full">
      {dot}
    </a>
  ) : (
    dot
  );
}

export default function AlternateTimeline({ data, height }: { data: WidgetEventsData; height: number }) {
  const { locale } = useFormatter();
  const entries = alternateEntries(data);
  return (
    <ol className="h-full overflow-auto px-1.5 py-1.5" style={{ maxHeight: height }} data-timeline-style="alternate">
      {entries.map((entry) => {
        const date = (
          <span className={cn("min-w-0 flex-1 px-4 pt-[18px] content-compact text-default-secondary", entry.card === "right" ? "text-right" : "text-left")}>
            {formatNamedDate(entry.start, "longDateTime", locale)}
          </span>
        );
        const card = (
          <div className="min-w-0 flex-1 px-4 py-1.5" data-card-side={entry.card}>
            <div className="rounded-sm bg-[var(--bg-elevation-default-layer-0)] p-6 text-left">
              <h2 className="mb-2.5 font-heading text-base font-medium lowercase text-default-primary first-letter:uppercase">{entry.label}</h2>
              {entry.secondary && <MarkdownText text={entry.secondary} className="-mt-[5px] content-compact text-default-secondary [&_p]:my-1" />}
            </div>
          </div>
        );
        return (
          <li key={entry.index} className="flex min-h-[70px]" data-entry="">
            {entry.card === "right" ? date : card}
            <span className="flex shrink-0 flex-col items-center">
              <Dot entry={entry} />
              <span className="w-0.5 flex-1 bg-[var(--color-feedback-neutral-primary)]" data-connector="" />
            </span>
            {entry.card === "right" ? card : date}
          </li>
        );
      })}
    </ol>
  );
}
