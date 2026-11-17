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
 * What a pause watches: each background task, assignment run or Video
 * Studio job with its state, and how long it has been running - so the user
 * can see the wait is for something, and when it ends. Shown in the
 * transcript's footer while a turn waits (`transcriptFooter`).
 */
import { useEffect, useState } from "react";
import { useFormatter } from "@/i18n";
import { cn, formatDurationShort } from "@/lib/utils";
import {
  WAIT_ITEM_KIND_LABELS,
  shortWaitItemId,
  waitItemRunningSeconds,
  waitItemStatusKey,
} from "@/lib/wait-items";
import type { WaitItem } from "@/types";

/** Items the wait card lists before it counts the rest. */
const WAIT_ITEMS_SHOWN = 4;

/** The clock the running times are read at, ticking every second. */
function useNow(): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(id);
  }, []);
  return now;
}

export function WaitItemList({ items, className }: { items: WaitItem[]; className?: string }) {
  const { t_i18n } = useFormatter();
  const now = useNow();
  const hidden = items.length - WAIT_ITEMS_SHOWN;
  return (
    <ul data-testid="wait-items" className={cn("relative space-y-0.5", className)}>
      {items.slice(0, WAIT_ITEMS_SHOWN).map((item) => {
        const running = waitItemRunningSeconds(item, now);
        const name = item.label ?? (item.kind ? t_i18n(WAIT_ITEM_KIND_LABELS[item.kind]) : null);
        return (
          <li key={item.id} className="flex items-center gap-1.5 content-caption text-default-secondary">
            {name && <span className="truncate max-w-[18rem]">{name}</span>}
            <span className="font-mono text-default-disabled shrink-0">{shortWaitItemId(item.id)}</span>
            <span className="text-default-disabled shrink-0">·</span>
            <span className="shrink-0">{t_i18n(waitItemStatusKey(item.status))}</span>
            {running != null && (
              <span className="tabular-nums text-default-disabled shrink-0">{formatDurationShort(running)}</span>
            )}
          </li>
        );
      })}
      {hidden > 0 && (
        <li className="content-caption text-default-disabled">{t_i18n("+{count} more", { count: hidden })}</li>
      )}
    </ul>
  );
}
