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
 * Cancel / Reschedule of a scheduled work item, the one control the
 * chat card, Operations > Work Items, the Schedule tab and an agent's
 * "Coming up" card share. Only offered while the item has not run.
 *
 * The new time is picked as a wall time in the viewer's zone and sent WITH
 * that zone's offset on that date, so a daylight saving change is never
 * guessed at.
 *
 * In a list row (`iconOnly`) the editor opens in a small dialog (a labelled
 * field, then Keep current time and Save in a footer), so the row keeps its fixed height;
 * elsewhere it replaces the buttons inline.
 *
 * The field stays the native `datetime-local` input (decision on UX
 * advisory A3): the design system ships a date picker with no time part, and a
 * second, hand-made time control would be the workaround the invariants rule
 * out. The browser draws the native field in its own locale, so the dialog
 * repeats the chosen time below it in the app language and the viewer's zone.
 */
import { useState } from "react";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Tip } from "@/components/ui/tooltip";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { useToast } from "@/components/ui/toast";
import { useAuth } from "@/hooks/useAuth";
import {
  useCancelScheduledWorkItem,
  useRescheduleScheduledWorkItem,
} from "@/hooks/api/useWorkItemApi";
import { useFormatter } from "@/i18n";
import {
  canChangeSchedule,
  canReschedule,
  isoToWallTime,
  viewerTimeZone,
  wallTimeToOffsetIso,
} from "@/lib/scheduled-work-items";
import type { WorkItem, WorkItemScheduled } from "@/types";
import { Icon, Spinner } from "@filigran/design-system";

export function ScheduleActions({
  item,
  iconOnly = false,
}: {
  item: WorkItem & { scheduled: WorkItemScheduled };
  /** Kept for the call sites; the actions use the DS sm Button whatever the
   *  host (UX round 3 B3, #10911): no height or text override any more. */
  compact?: boolean;
  /** Icon buttons with a tooltip: the fixed-width actions column of a list row. */
  iconOnly?: boolean;
}) {
  const { t_i18n, intl } = useFormatter();
  const { user } = useAuth();
  const { toast } = useToast();
  const cancel = useCancelScheduledWorkItem();
  const reschedule = useRescheduleScheduledWorkItem();
  const zone = viewerTimeZone(user?.timezone);
  const [editing, setEditing] = useState(false);
  const [value, setValue] = useState(() =>
    isoToWallTime(item.due_at ?? new Date(Date.now() + 3_600_000).toISOString(), zone),
  );

  // A platform restart never hands an action back to the person: the
  // platform resumed it or ran it again for its agent, so neither control shows.
  if (!canChangeSchedule(item.scheduled.state) || !canReschedule(item.scheduled)) return null;

  const fail = (err: Error) =>
    toast({ title: t_i18n("The scheduled work item was not changed"), description: err.message, variant: "error" });

  const onCancel = () =>
    cancel.mutate(item.id, {
      onSuccess: () => toast({ title: t_i18n("Scheduled work item cancelled: nothing will run"), variant: "success" }),
      onError: fail,
    });

  const onSave = () => {
    const due_at = wallTimeToOffsetIso(value, zone);
    if (!due_at) return;
    reschedule.mutate(
      { id: item.id, due_at },
      {
        onSuccess: () => {
          setEditing(false);
          toast({ title: t_i18n("Scheduled work item rescheduled"), variant: "success" });
        },
        onError: fail,
      },
    );
  };

  const editor = (
    <div className="flex flex-wrap items-center gap-1.5" onClick={(e) => e.stopPropagation()}>
      <Input
        type="datetime-local"
        aria-label={t_i18n("New time ({zone})", { zone })}
        value={value}
        onChange={(e) => setValue(e.target.value)}
        className="w-auto px-2"
      />
      <span className="content-caption text-default-secondary">{zone}</span>
      <Button className="gap-1" onClick={onSave} disabled={reschedule.isPending || !value} aria-busy={reschedule.isPending || undefined}>
        {reschedule.isPending && <Spinner size="sm" tone="inherit" />}{!reschedule.isPending && <Icon name="check" size={16} aria-hidden />}
        {t_i18n("Save")}
      </Button>
      <Button variant="ghost" className="gap-1" onClick={() => setEditing(false)}>
        <Icon name="x" size={16} aria-hidden /> {t_i18n("Close")}
      </Button>
    </div>
  );
  if (editing && !iconOnly) return editor;
  if (iconOnly) {
    // The dialog lays the editor out as a form, not as a row: a labelled
    // full-width field, then the actions in a footer like the Mark done dialog
    // of the same list.
    const fieldId = `reschedule-${item.id}`;
    const chosenIso = value ? wallTimeToOffsetIso(value, zone) : null;
    const chosen = chosenIso
      ? intl.formatDate(Date.parse(chosenIso), { dateStyle: "full", timeStyle: "short", timeZone: zone })
      : null;
    return (
      <div className="flex items-center gap-1" onClick={(e) => e.stopPropagation()}>
        <Dialog open={editing} onOpenChange={setEditing}>
          <DialogContent className="max-w-md" data-testid="schedule-reschedule-dialog">
            <DialogHeader>
              <DialogTitle className="flex items-center gap-2 text-base">
                <Icon name="calendar" size={16} className="text-accent" aria-hidden /> {t_i18n("Reschedule")}
              </DialogTitle>
              <DialogDescription className="content-base">{item.title}</DialogDescription>
            </DialogHeader>
            <div className="space-y-1.5">
              <Label htmlFor={fieldId} className="text-default-secondary">
                {t_i18n("New time ({zone})", { zone })}
              </Label>
              <Input
                aria-label={t_i18n("New time ({zone})", { zone })}
                id={fieldId}
                type="datetime-local"
                value={value}
                onChange={(e) => setValue(e.target.value)}
                className="w-full"
                aria-describedby={chosen ? `${fieldId}-chosen` : undefined}
              />
              {chosen && (
                <p id={`${fieldId}-chosen`} className="content-caption text-default-secondary" data-testid="schedule-chosen-time">
                  {t_i18n("Runs {when}", { when: chosen })}
                </p>
              )}
            </div>
            <div className="flex justify-end gap-2 pt-2">
              {/* Not "Close": the dialog's corner X already has that name. */}
              <Button variant="ghost" size="sm" onClick={() => setEditing(false)}>
                {t_i18n("Keep current time")}
              </Button>
              <Button size="sm" className="gap-1" onClick={onSave} disabled={reschedule.isPending || !value} aria-busy={reschedule.isPending || undefined}>
                {reschedule.isPending && <Spinner size="sm" tone="inherit" />}{!reschedule.isPending && <Icon name="check" size={16} aria-hidden />}
                {t_i18n("Save")}
              </Button>
            </div>
          </DialogContent>
        </Dialog>
        <Tip content={t_i18n("Reschedule")}>
          <Button
            variant="ghost"
            size="icon"
            className="h-7 w-7 text-accent"
            aria-label={t_i18n("Reschedule")}
            onClick={() => setEditing(true)}
          >
            <Icon name="calendar" size={16} aria-hidden />
          </Button>
        </Tip>
        <Tip content={t_i18n("Cancel schedule")}>
          <Button
            variant="ghost"
            size="icon"
            className="h-7 w-7 text-default-secondary"
            aria-label={t_i18n("Cancel schedule")}
            onClick={onCancel}
            disabled={cancel.isPending} aria-busy={cancel.isPending || undefined}>
            {cancel.isPending && <Spinner size="sm" tone="inherit" />}{!cancel.isPending && <Icon name="circle-x" size={16} aria-hidden />}
          </Button>
        </Tip>
      </div>
    );
  }
  return (
    <div className="flex items-center gap-1" onClick={(e) => e.stopPropagation()}>
      <Button variant="ghost" size="sm" className="gap-1" onClick={() => setEditing(true)}>
        <Icon name="calendar" size={16} aria-hidden /> {t_i18n("Reschedule")}
      </Button>
      <Button
        variant="ghost"
        size="sm"
        className="gap-1"
        onClick={onCancel}
        disabled={cancel.isPending} aria-busy={cancel.isPending || undefined}>
        {cancel.isPending && <Spinner size="sm" tone="inherit" />}{!cancel.isPending && <Icon name="circle-x" size={16} aria-hidden />}
        {t_i18n("Cancel")}
      </Button>
    </div>
  );
}
