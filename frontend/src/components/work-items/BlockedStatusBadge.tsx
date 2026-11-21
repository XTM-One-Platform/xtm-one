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
 * The status chip of a blocked work item. A blocked item reads one of
 * two ways: it waits for items that are not finished yet and goes back to the
 * queue by itself ("Waiting", neutral tone), or nothing it waits for is open
 * and someone has to act on it ("Blocked", error tone: a scheduled leftover, a
 * run cut by a restart, a blocked set by hand, a cancelled dependency). The
 * tooltip says which, with the titles it waits for, the cancelled ones, or the
 * result written on it; never a platform claim note.
 */
import { Badge } from "@/components/ui/badge";
import { Tip } from "@/components/ui/tooltip";
import { useFormatter } from "@/i18n";
import { formatList } from "@/lib/format-list";
import { cn } from "@/lib/utils";
import { BADGE_TIP_FOCUS } from "@/lib/work-item-context";

const TONE = {
  waiting: "text-default-secondary bg-elevation-hover",
  action: "text-feedback-error-primary bg-feedback-error-primary/10",
} as const;

export function BlockedStatusBadge({
  kind,
  blockers = [],
  cancelled = [],
  reason = null,
}: {
  kind: "waiting" | "action";
  /** `waiting`: the titles of the unfinished items it waits for. */
  blockers?: string[];
  /** `action`: the titles of the items it waits for that were cancelled; they win over `reason`. */
  cancelled?: string[];
  /** `action`: why it needs one (the result written on it), when known. */
  reason?: string | null;
}) {
  const { t_i18n, locale } = useFormatter();
  const tip =
    kind === "waiting" && !blockers.length
      ? // Waits for references or a person's answer (#861): released by itself.
        t_i18n("Goes back to the queue by itself once what it waits for is released: nothing to do.")
      : kind === "waiting"
      ? t_i18n("Reopens by itself when {count, plural, one {# item completes} other {# items complete}}: {titles}", {
          count: blockers.length,
          titles: formatList(blockers, locale),
        })
      : cancelled.length
        ? t_i18n(
            "Needs an action: {count, plural, one {the item it waits for was cancelled ({titles}), so it does not reopen by itself.} other {# items it waits for were cancelled ({titles}), so it does not reopen by itself.}}",
            { count: cancelled.length, titles: formatList(cancelled, locale) },
          )
        : reason
          ? t_i18n("Needs an action: {reason}", { reason })
          : t_i18n("Needs an action: put it back in the queue or close it.");
  return (
    <Tip content={tip} wrap>
      <Badge
        variant="secondary"
        tabIndex={0}
        className={cn("content-compact", BADGE_TIP_FOCUS, TONE[kind])}
        data-testid="work-item-blocked-status"
        data-kind={kind}
      >
        {kind === "waiting" ? t_i18n("Waiting") : t_i18n("Blocked")}
      </Badge>
    </Tip>
  );
}
