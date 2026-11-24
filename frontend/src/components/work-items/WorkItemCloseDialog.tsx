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
 * Closing a work item by hand: "Mark done" or "Cancel work item",
 * confirmed, with an optional result recorded on the item. Both used to be
 * one-click buttons side by side, and "Cancel" read as "close this dialog".
 */
import { useRef, useState } from "react";
import { Icon, Textarea } from "@filigran/design-system";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { useToast } from "@/components/ui/toast";
import { useCompleteWorkItem } from "@/hooks/api/useWorkItemApi";
import { useFormatter } from "@/i18n";
import { cn } from "@/lib/utils";
import type { WorkItem } from "@/types";

export type WorkItemCloseMode = "done" | "cancelled";

/** The wording of each way to close an item. @i18n-keys */
const COPY = {
  done: {
    title: "Mark this work item done?",
    description: "It leaves the queue: its owner agent does not run it again, and its completion still reaches the agents it impacts. Say what was done so the trail is complete. If the problem is gone but the agent should check, keep it open and use Ask the agent > It's solved.",
    label: "What was done (optional)",
    placeholder: "e.g. Imported the 120 pages into the knowledge base by hand.",
    confirm: "Mark done",
    success: "Work item marked done",
  },
  cancelled: {
    title: "Cancel this work item?",
    description: "It leaves the queue without being done: its owner agent does not run it again and no completion work follows. Say why it is no longer needed.",
    label: "Why it is no longer needed (optional)",
    placeholder: "e.g. Duplicate of another request.",
    confirm: "Cancel work item",
    success: "Work item cancelled",
  },
} as const;

export function WorkItemCloseDialog({
  item,
  mode,
  onOpenChange,
  returnFocusTo,
  onCompleted,
}: {
  item: WorkItem | null;
  mode: WorkItemCloseMode;
  onOpenChange: (open: boolean) => void;
  /**
   * Where focus goes once the item is closed: the button that opened
   * the dialog ("Mark done") leaves the row when the item is done, so a list
   * names the row's actions button, or the next row's when the row left.
   * Also the fallback when the opener is gone on a plain dismiss.
   */
  returnFocusTo?: () => HTMLElement | null | undefined;
  /** Runs once the item is closed (not on a plain dismiss): the detail dialog under it closes too (#774). */
  onCompleted?: (item: WorkItem) => void;
}) {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const complete = useCompleteWorkItem();
  const [result, setResult] = useState("");
  const confirmed = useRef(false);
  const copy = COPY[mode];

  // After a confirmed close the opener is about to unmount: focus the list's
  // target at once instead of a button that leaves the document next render.
  const onCloseAutoFocus = (event: Event) => {
    const wasConfirmed = confirmed.current;
    confirmed.current = false;
    if (!wasConfirmed) return;
    const target = returnFocusTo?.();
    if (!target || !target.isConnected) return;
    event.preventDefault();
    target.focus();
  };

  const close = (open: boolean) => {
    if (!open) setResult("");
    onOpenChange(open);
  };

  const submit = async () => {
    if (!item) return;
    try {
      await complete.mutateAsync({ id: item.id, status: mode, result: result.trim() || undefined });
      toast({ title: t_i18n(copy.success), variant: "success" });
      confirmed.current = true;
      close(false);
      onCompleted?.(item);
    } catch (e) {
      toast({
        title: t_i18n("Failed to update work item"),
        description: e instanceof Error ? e.message : undefined,
        variant: "error",
      });
    }
  };

  return (
    <Dialog open={item !== null} onOpenChange={close}>
      <DialogContent className="max-w-md" onCloseAutoFocus={onCloseAutoFocus} returnFocusFallback={returnFocusTo}>
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2 text-base">
            {mode === "done" ? (
              <Icon name="circle-check" size={16} className="text-feedback-success-primary" />
            ) : (
              <Icon name="circle-x" size={16} className="text-default-secondary" />
            )}
            {t_i18n(copy.title)}
          </DialogTitle>
          <DialogDescription className="content-base">{t_i18n(copy.description)}</DialogDescription>
        </DialogHeader>
        {item && <p className="content-compact font-medium text-default-primary">{item.title}</p>}
        {/* The kit Label, body size: the design-system Textarea's own label is caption size (UX round 1 of #9890, B3). */}
        <Label id="work-item-close-result-label" htmlFor="work-item-close-result">{t_i18n(copy.label)}</Label>
        <Textarea
          id="work-item-close-result"
          aria-labelledby="work-item-close-result-label"
          placeholder={t_i18n(copy.placeholder)}
          value={result}
          onChange={(e) => setResult(e.target.value)}
          minRows={3}
          maxRows={8}
          resize="vertical"
          data-testid="work-item-close-result"
        />
        <div className="flex justify-end gap-2 pt-2">
          <Button variant="ghost" size="sm" onClick={() => close(false)}>
            {t_i18n("Keep it open")}
          </Button>
          <Button
            size="sm"
            variant={mode === "done" ? "default" : "secondary"}
            className={cn("gap-1")}
            disabled={complete.isPending}
            onClick={submit}
            data-testid="work-item-close-confirm"
          >
            {t_i18n(copy.confirm)}
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}
