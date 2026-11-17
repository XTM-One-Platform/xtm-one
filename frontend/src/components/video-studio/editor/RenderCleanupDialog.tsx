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
 * "Clean up renders": delete a video's renders with their files - the
 * MP4, the poster, the captions, the frames and the inserts' crops - by the
 * selectors `delete_video_renders` takes. What goes is previewed as the
 * selectors change (the server's dry run, the same list the cleanup answers):
 * each render the cleanup deletes, keeps or refuses, and why. The render the
 * public link plays, and with "Everything" the newest ready renders, go only
 * once the confirmation is on.
 */
import { useState } from "react";
import { Trash2 } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { DateFilterInput } from "@/components/ui/date-filter-input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { SettingsField, settingsFieldHintId, settingsFieldLabelId } from "@/components/ui/settings-form";
import { Switch } from "@/components/ui/switch";
import { useToast } from "@/components/ui/toast";
import { useCleanupRenders, useRenderCleanupPreview } from "@/hooks/api/useVideoStudioApi";
import { useFormatter } from "@/i18n";
import { cn } from "@/lib/utils";
import type {
  VideoProject,
  VideoRenderCleanupItem,
  VideoRenderCleanupReason,
  VideoRenderCleanupRequest,
  VideoRenderCleanupSelector,
  VideoRenderQuality,
} from "@/types";

import { QUALITY_LABELS, formatSize } from "../video-format";
import { Spinner } from "@filigran/design-system";

/** The English label of each selector (`render_cleanup.SELECTORS`). @i18n-keys */
const SELECTOR_LABELS = {
  all_but_latest_ready: "Everything but the newest ready render of each quality",
  failed_and_cancelled: "Failed and cancelled renders",
  previews: "Scene previews",
  all: "Everything, the newest ready renders too",
} as const satisfies Record<VideoRenderCleanupSelector, string>;

/** The one-line meaning of each selector, read in the list under its label. @i18n-keys */
const SELECTOR_MEANINGS = {
  all_but_latest_ready: "Frees the most space and the video still plays.",
  failed_and_cancelled: "Only renders that never finished: nothing that plays goes.",
  previews: "The short clips drawn for one scene while you edit.",
  all: "The video has nothing to play until it is rendered again.",
} as const satisfies Record<VideoRenderCleanupSelector, string>;

/** The English sentence of why a render is not deleted (`render_cleanup.REASONS`). @i18n-keys */
const REASON_LABELS = {
  in_progress: "Still in progress: cancel it first",
  not_yours: "Someone else's render, on a video you may only watch",
  public_link: "The public link plays it",
  handing_over: "The render that replaced it is still drawing from it",
  latest_ready: "The newest ready render at its quality",
  kept_by_hand: "Kept by hand",
  busy: "Being changed right now: try again",
} as const satisfies Record<VideoRenderCleanupReason, string>;

/** The keep choice of the default selector: every quality's newest ready render, or one quality's. */
const EACH_QUALITY = "each";

function ItemRow({ item }: { item: VideoRenderCleanupItem }) {
  const { t_i18n } = useFormatter();
  return (
    <li className="flex items-center gap-2 py-1 content-caption" data-testid="video.editor.cleanup.item">
      <Badge
        variant={item.action === "delete" ? "destructive" : item.action === "refuse" ? "warning" : "outline"}
        className="shrink-0 content-caption"
      >
        {item.action === "delete" ? t_i18n("Deleted") : item.action === "refuse" ? t_i18n("Refused") : t_i18n("Kept")}
      </Badge>
      <span className="min-w-0 truncate">
        {item.scene_id
          ? t_i18n("Scene {scene} preview", { scene: item.scene_id })
          : t_i18n(QUALITY_LABELS[item.quality])}{" "}
        - {t_i18n("revision {revision}", { revision: item.revision })}
        {item.reason ? ` - ${t_i18n(REASON_LABELS[item.reason])}` : ""}
      </span>
      <span className="ml-auto shrink-0 tabular-nums text-default-secondary">{formatSize(item.size_bytes)}</span>
    </li>
  );
}

export function RenderCleanupDialog({
  video,
  open,
  onOpenChange,
}: {
  video: VideoProject;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const [selector, setSelector] = useState<VideoRenderCleanupSelector>("all_but_latest_ready");
  const [keep, setKeep] = useState<string>(EACH_QUALITY);
  const [before, setBefore] = useState("");
  const [confirm, setConfirm] = useState(false);
  const cleanup = useCleanupRenders();

  const ask: VideoRenderCleanupRequest = {
    select: selector,
    ...(selector === "all_but_latest_ready" && keep !== EACH_QUALITY ? { keep_quality: keep as VideoRenderQuality } : {}),
    ...(before ? { before } : {}),
    confirm,
  };
  // "Everything" is previewed as it would go: the confirmation only gates the deletion.
  const preview = useRenderCleanupPreview(video.id, selector === "all" ? { ...ask, confirm: true } : ask, open);
  const items = preview.data?.renders ?? [];
  const going = items.filter((item) => item.action === "delete");
  const freeing = preview.data?.would_free?.bytes ?? 0;
  const linked = items.some((item) => item.reason === "public_link") || (confirm && !!preview.data?.serves.public_link);
  const needsConfirm = selector === "all" && !confirm;

  const submit = () =>
    cleanup.mutate(
      { projectId: video.id, ...ask },
      {
        onSuccess: (done) => {
          toast({
            title: t_i18n("{count, plural, one {# render deleted} other {# renders deleted}}", { count: done.freed?.renders ?? 0 }),
            description: t_i18n("{size} freed", { size: formatSize(done.freed?.bytes ?? 0) || "0 kB" }),
            variant: "success",
          });
          onOpenChange(false);
        },
        onError: (err) =>
          toast({
            title: t_i18n("The renders were not deleted"),
            description: err instanceof Error ? err.message : undefined,
            variant: "error",
          }),
      },
    );

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-lg" data-testid="video.editor.cleanup">
        <DialogHeader>
          <DialogTitle className="text-base">{t_i18n("Clean up renders")}</DialogTitle>
          <DialogDescription>
            {t_i18n(
              "Delete renders with their files: the MP4, the poster, the captions, the frames and the insert crops. Files saved in a conversation stay.",
            )}
          </DialogDescription>
        </DialogHeader>
        <div className="space-y-3">
          <SettingsField
            id="render-cleanup-selector"
            label={t_i18n("What goes")}
            helper={t_i18n("The renders the cleanup deletes: the list below shows each one and why.")}
            info={t_i18n(
              "Which renders the cleanup deletes, with their files. Example: failed and cancelled renders. The default keeps the newest ready render of each quality, so the video still plays.",
            )}
          >
            <Select value={selector} onValueChange={(value) => setSelector(value as VideoRenderCleanupSelector)}>
              <SelectTrigger
                id="render-cleanup-selector"
                className="w-full"
                aria-labelledby={settingsFieldLabelId("render-cleanup-selector")}
                aria-describedby={settingsFieldHintId("render-cleanup-selector")}
              >
                <SelectValue>
                  <span className="block truncate">{t_i18n(SELECTOR_LABELS[selector])}</span>
                </SelectValue>
              </SelectTrigger>
              <SelectContent>
                {(Object.keys(SELECTOR_LABELS) as VideoRenderCleanupSelector[]).map((key) => (
                  <SelectItem key={key} value={key}>
                    <span className="flex flex-col">
                      <span className="font-medium">{t_i18n(SELECTOR_LABELS[key])}</span>
                      <span className="content-compact text-default-secondary">{t_i18n(SELECTOR_MEANINGS[key])}</span>
                    </span>
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </SettingsField>
          {selector === "all_but_latest_ready" && (
            <SettingsField
              id="render-cleanup-keep"
              label={t_i18n("Keep")}
              helper={t_i18n("The render left to play after the cleanup.")}
              info={t_i18n(
                "The render the cleanup spares. Example: keep only the newest 1080p and the other qualities go. The default keeps the newest ready render of each quality.",
              )}
            >
              <Select value={keep} onValueChange={setKeep}>
                <SelectTrigger
                  id="render-cleanup-keep"
                  className="w-full"
                  aria-labelledby={settingsFieldLabelId("render-cleanup-keep")}
                  aria-describedby={settingsFieldHintId("render-cleanup-keep")}
                >
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value={EACH_QUALITY}>{t_i18n("Keep the newest ready render of each quality")}</SelectItem>
                  {(Object.keys(QUALITY_LABELS) as VideoRenderQuality[]).map((quality) => (
                    <SelectItem key={quality} value={quality}>
                      {t_i18n("Keep only the newest {quality}", { quality: t_i18n(QUALITY_LABELS[quality]) })}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </SettingsField>
          )}
          <SettingsField
            id="render-cleanup-before"
            label={t_i18n("Only renders made before")}
            helper={t_i18n("Empty: renders of every date.")}
            info={t_i18n(
              "Limits the cleanup to renders made before this day. Example: the first of the month spares this month's renders. Empty: renders of every date.",
            )}
          >
            <DateFilterInput
              id="render-cleanup-before"
              aria-labelledby={settingsFieldLabelId("render-cleanup-before")}
              aria-describedby={settingsFieldHintId("render-cleanup-before")}
              value={before}
              onChange={setBefore}
              className="w-full"
            />
          </SettingsField>
          {(selector === "all" || linked) && (
            <label className="flex items-start gap-2 rounded-sm border border-alert-warning bg-alert-warning p-2 content-compact">
              <Switch aria-label={t_i18n("Confirm")} checked={confirm} onCheckedChange={setConfirm} />
              <span>
                {selector === "all"
                  ? t_i18n("Also delete the newest ready renders: the video will have nothing to play until it is rendered again.")
                  : t_i18n("Also delete the render the public link plays: the link then plays the newest ready render left.")}
              </span>
            </label>
          )}
          <div className="max-h-64 overflow-y-auto rounded-sm border border-elevation-subtle-soft px-2" data-testid="video.editor.cleanup.preview">
            {preview.isLoading ? (
              <p className="flex items-center gap-2 py-2 content-caption text-default-secondary">
                <Spinner size="sm" tone="inherit" /> {t_i18n("Listing what goes...")}
              </p>
            ) : preview.error ? (
              <p className="py-2 content-caption text-feedback-error-primary">
                {preview.error instanceof Error ? preview.error.message : t_i18n("The preview failed")}
              </p>
            ) : items.length ? (
              <ul className="divide-y divide-[var(--border-elevation-subtle)]">
                {items.map((item) => (
                  <ItemRow key={item.render_id} item={item} />
                ))}
              </ul>
            ) : (
              <p className="py-2 content-caption text-default-secondary">{t_i18n("No render matches.")}</p>
            )}
          </div>
          <p className={cn("content-caption", going.length ? "text-default-primary" : "text-default-secondary")} data-testid="video.editor.cleanup.total">
            {t_i18n("{count, plural, one {# render goes} other {# renders go}}, freeing {size}.", {
              count: going.length,
              size: formatSize(freeing) || "0 kB",
            })}
          </p>
        </div>
        <div className="flex justify-end gap-2 pt-2">
          <Button variant="secondary" onClick={() => onOpenChange(false)}>
            {t_i18n("Cancel")}
          </Button>
          <Button
            variant="destructive"
            className="gap-1.5"
            disabled={!going.length || needsConfirm || cleanup.isPending || preview.isFetching}
            onClick={submit}
            data-testid="video.editor.cleanup.delete" aria-busy={cleanup.isPending || undefined}>
            {cleanup.isPending && <Spinner size="sm" tone="inherit" />}{!cleanup.isPending && <Trash2 className="h-3.5 w-3.5" />}
            {t_i18n("{count, plural, one {Delete # render} other {Delete # renders}}", { count: going.length })}
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}
