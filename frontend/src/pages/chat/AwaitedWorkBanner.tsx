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

import { Hourglass } from "lucide-react";
import { Tip } from "@/components/ui/tooltip";
import { useFormatter } from "@/i18n";
import { cn } from "@/lib/utils";
import type { AwaitedWorkItem } from "@/types";
import { Spinner } from "@filigran/design-system";

/** Work named in the banner; the rest is counted. */
const SHOWN_ITEMS = 3;
const NAME_CHARS = 32;

function clamp(text: string): string {
  return text.length > NAME_CHARS ? `${text.slice(0, NAME_CHARS)}...` : text;
}

function useKindLabel(): (kind: string) => string {
  const { t_i18n } = useFormatter();
  return (kind) => {
    switch (kind) {
      case "video_take":
        return t_i18n("Recording");
      case "video_render":
        return t_i18n("Render");
      case "video_clip":
        return t_i18n("AI shot");
      case "video_music":
        return t_i18n("AI music");
      default:
        return t_i18n("Background work");
    }
  };
}

function WorkPill({ item }: { item: AwaitedWorkItem }) {
  const { t_i18n } = useFormatter();
  const kindLabel = useKindLabel();
  const running = item.status === "running";
  const label = item.name ? `${kindLabel(item.kind)} · ${clamp(item.name)}` : kindLabel(item.kind);
  return (
    <Tip
      content={
        <div className="max-w-[260px] space-y-0.5 py-0.5 text-[11px]">
          <p className="font-medium text-default-primary">{label}</p>
          <p className="text-default-secondary">
            {running ? t_i18n("Running, {percent}% done", { percent: item.percent }) : t_i18n("Queued")}
          </p>
          {item.stage && <p className="text-default-secondary">{item.stage}</p>}
        </div>
      }
    >
      <span className="inline-flex items-center gap-1 rounded-sm border border-transparent bg-accent/10 px-2 py-0.5 text-[9px] text-accent/70">
        {running && <Spinner size="sm" tone="inherit" className="shrink-0" />}{!running && <Hourglass className="h-2.5 w-2.5 shrink-0" aria-hidden />}
        <span className="max-w-[180px] truncate">{label}</span>
        {running && <span className="tabular-nums">{item.percent}%</span>}
      </span>
    </Tip>
  );
}

/**
 * The work a conversation waits on: a turn handed it to the
 * background and ended, and the assistant resumes the conversation once it
 * finishes. Without it, the conversation reads as finished while a render is
 * still on its way.
 */
export function AwaitedWorkBanner({ items, className }: { items: AwaitedWorkItem[]; className?: string }) {
  const { t_i18n } = useFormatter();
  if (items.length === 0) return null;
  return (
    <div
      role="status"
      data-testid="awaited-work-banner"
      className={cn("border-b border-elevation-subtle-soft bg-accent/[0.02] px-4 py-2", className)}
    >
      <div className="mx-auto flex max-w-3xl flex-wrap items-center gap-x-2 gap-y-1">
        <Hourglass className="h-3.5 w-3.5 shrink-0 text-accent" aria-hidden />
        <p className="content-caption font-medium text-accent/80">
          {t_i18n("The assistant resumes this conversation when this work finishes")}
        </p>
        <div className="flex flex-wrap items-center gap-1.5">
          {items.slice(0, SHOWN_ITEMS).map((item) => (
            <WorkPill key={`${item.kind}:${item.id}`} item={item} />
          ))}
          {items.length > SHOWN_ITEMS && (
            <span className="text-[9px] text-accent/50">
              {t_i18n("+{count} more", { count: items.length - SHOWN_ITEMS })}
            </span>
          )}
        </div>
      </div>
    </div>
  );
}
