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
 * The file chips of an agent's reply: the download card of a
 * deliverable and the compact chip of a working file. Shared by the chat
 * panel's `[[FILE:…]]` marker cards and the run detail, whose working files
 * sit behind one collapsed line.
 */
import type React from "react";
import { useCallback, useEffect, useId, useRef, useState } from "react";
import { Icon as DsIcon, IconButton } from "@filigran/design-system";
import { downloadErrorDescription } from "@/lib/download-error";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { Tip } from "@/components/ui/tooltip";
import { useToast } from "@/components/ui/toast";
import { useAuthenticatedSrcState } from "@/hooks/useAuthenticatedSrc";
import { useFormatter } from "@/i18n";
import { cn, formatTime } from "@/lib/utils";
import { inlineChipClass } from "./inline-chip";
import { fileCardFacts, newerRevisionId, type FileCardFacts } from "@/pages/chat/file-revisions";
import { useFileRevision } from "@/pages/chat/file-revisions-context";
import { useDeletedFileNotice } from "@/pages/chat/deleted-file-notice";
import {
  authenticatedDownload,
  formatFileSize,
  mimeLabel,
  mimeVisual,
  resolveAttachmentMime,
} from "@/pages/chat/fileVisuals";
import type { ChatMessageAttachment } from "@/types";

/**
 * Downloads a chip's file with the session. ``authenticatedDownload`` throws on
 * a non-2xx answer or a network failure: the toast keeps that from failing silently.
 */
function useChipDownload(att: ChatMessageAttachment) {
  const { toast } = useToast();
  const { t_i18n } = useFormatter();
  return useCallback(
    async (e?: React.MouseEvent) => {
      if (!att.download_url) return;
      e?.preventDefault();
      try {
        await authenticatedDownload(att.download_url, att.filename);
      } catch (err) {
        toast({
          title: t_i18n("Download failed"),
          description: downloadErrorDescription(err, att.filename, t_i18n),
          variant: "error",
        });
      }
    },
    [att.download_url, att.filename, toast, t_i18n],
  );
}

/**
 * What tells two files of the same name apart beyond what their card shows:
 * the id and the content hash, plus the time and exact size for a compact
 * chip, which has no room for them.
 */
function FileDetails({ facts, chip = false }: { facts: FileCardFacts; chip?: boolean }) {
  const { t_i18n } = useFormatter();
  return (
    <div className="space-y-0.5 text-[11px]">
      {chip && facts.createdAt && <div>{formatTime(facts.createdAt)}</div>}
      {chip && facts.exactBytes !== null && (
        <div>{t_i18n("{size, plural, one {# byte} other {# bytes}}", { size: facts.exactBytes })}</div>
      )}
      {facts.fileId && <div className="font-mono-plex">{t_i18n("File id: {id}", { id: facts.fileId })}</div>}
      {facts.shaPrefix && <div className="font-mono-plex">{t_i18n("SHA-256: {hash}", { hash: facts.shaPrefix })}</div>}
    </div>
  );
}

/** The v1, v2... badge of a file whose name and type another file of the conversation shares. */
function RevisionBadge({ revision }: { revision: number | null }) {
  const { t_i18n } = useFormatter();
  if (revision === null) return null;
  return (
    <span className="shrink-0 rounded-sm border border-elevation-subtle-soft bg-elevation-hover px-1 content-caption font-medium leading-4 tabular-nums text-default-secondary">
      {t_i18n("v{revision}", { revision })}
    </span>
  );
}

/** The attribute a file chip carries so a link elsewhere in the transcript can bring it into view. */
const FILE_ANCHOR_ATTRIBUTE = "data-file-anchor";

function fileAnchor(fileId: string | null | undefined): Record<string, string> {
  return fileId ? { [FILE_ANCHOR_ATTRIBUTE]: fileId.toLowerCase() } : {};
}

/**
 * Brings the chip of a file into view and moves the focus to it. The first
 * rendered chip of that file wins (a file named again by marker shows twice);
 * false when none is on screen (its message not loaded, its list collapsed).
 */
function scrollToFile(fileId: string, root: ParentNode = document): boolean {
  const selector = `[${FILE_ANCHOR_ATTRIBUTE}="${CSS.escape(fileId.toLowerCase())}"]`;
  const target = Array.from(root.querySelectorAll<HTMLElement>(selector)).find(
    (el) => el.getClientRects().length > 0,
  );
  if (!target) return false;
  target.scrollIntoView({ behavior: "smooth", block: "center", inline: "nearest" });
  const focusable = target.matches("button, a[href]") ? target : target.querySelector<HTMLElement>("button, a[href]");
  focusable?.focus({ preventScroll: true });
  return true;
}

/**
 * "Go to the newer version" on a file a later file of the same name and type
 * superseded (the pre-#6641 transcript offered it): brings the newest one into
 * view. Nothing on the newest revision or a file whose name is unique.
 */
function NewerVersionLink({ fileId }: { fileId: string | null | undefined }) {
  const { t_i18n } = useFormatter();
  const newerId = newerRevisionId(useFileRevision(fileId));
  if (!newerId) return null;
  return (
    <Tip content={t_i18n("A later file of the same name replaces this one")}>
      {/* data-ds-exempt: an inline text link in the chip (the design system's
          link type, content-compact-link); the DS has no link-button. */}
      <button
        type="button"
        data-ds-exempt="text-link"
        onClick={() => scrollToFile(newerId)}
        // The design system's link type (12 px, medium, underlined) in the default
        // text colour: the accent read 4.49:1 on the light card surface (#10911 UX
        // round 1, B3); tests_unit/newer-version-link-contrast.test.ts holds 4.5:1.
        className="shrink-0 rounded-sm content-compact-link text-default-primary underline underline-offset-2 hover:decoration-2 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus"
      >
        {t_i18n("Go to the newer version")}
      </button>
    </Tip>
  );
}

/**
 * The card of a file whose content was deleted (#396): its icon, name and size,
 * muted, with when and why. Never asks for its bytes, so never a broken image or
 * a 410 on click. `compact` is the inline chip of a working file.
 */
function DeletedFileCard({ att, compact = false }: { att: ChatMessageAttachment; compact?: boolean }) {
  const { t_i18n } = useFormatter();
  const notice = useDeletedFileNotice();
  const mime = resolveAttachmentMime(att);
  const { icon: Icon } = mimeVisual(mime);
  const size = formatFileSize(att.size);
  const why = att.deleted_at ? notice(att.deleted_at, att.deleted_reason) : t_i18n("Deleted");
  if (compact) {
    return (
      <span
        className={cn(inlineChipClass(), "opacity-80")}
        {...fileAnchor(att.file_id)}
        data-testid="file-chip-deleted"
        aria-label={t_i18n("{filename}: {why}", { filename: att.filename, why })}
      >
        <Icon className="h-3.5 w-3.5 shrink-0 text-default-secondary" aria-hidden />
        <span className="truncate font-medium text-default-secondary line-through decoration-1">{att.filename}</span>
        <span className="shrink-0 text-default-secondary">{why}</span>
      </span>
    );
  }
  return (
    <div
      {...fileAnchor(att.file_id)}
      data-testid="file-card-deleted"
      className="inline-flex max-w-[320px] min-w-[200px] items-start gap-2.5 rounded-sm border border-dashed border-elevation-subtle-soft px-3 py-2.5"
    >
      <Icon className="mt-0.5 h-4 w-4 shrink-0 text-default-secondary" aria-hidden />
      <div className="min-w-0 flex-1">
        <div className="flex min-w-0 items-center gap-1.5">
          <span className="truncate content-base font-medium leading-tight text-default-secondary">{att.filename}</span>
          {size && <span className="shrink-0 text-[11px] text-default-secondary">{size}</span>}
        </div>
        <div className="mt-1 content-compact text-default-secondary">{why}</div>
      </div>
    </div>
  );
}

export function FileChip({
  att,
  autoFocusOnMount = false,
}: {
  att: ChatMessageAttachment;
  autoFocusOnMount?: boolean;
}) {
  if (att.deleted_at) return <DeletedFileCard att={att} />;
  return <LiveFileChip att={att} autoFocusOnMount={autoFocusOnMount} />;
}

function LiveFileChip({
  att,
  autoFocusOnMount = false,
}: {
  att: ChatMessageAttachment;
  autoFocusOnMount?: boolean;
}) {
  const mime = resolveAttachmentMime(att);
  const { icon: Icon, color } = mimeVisual(mime);
  const typeLabel = mimeLabel(att, mime);
  const cardRef = useRef<HTMLDivElement | null>(null);
  const didAutoFocusRef = useRef(false);
  const [highlight, setHighlight] = useState(false);
  const { t_i18n } = useFormatter();
  const handleClick = useChipDownload(att);

  useEffect(() => {
    if (!autoFocusOnMount || !att.download_url || didAutoFocusRef.current) return;
    didAutoFocusRef.current = true;

    const raf = requestAnimationFrame(() => {
      const el = cardRef.current;
      if (!el) return;
      el.scrollIntoView({ behavior: "smooth", block: "center", inline: "nearest" });
      setHighlight(true);
    });

    const timer = window.setTimeout(() => setHighlight(false), 2200);

    return () => {
      cancelAnimationFrame(raf);
      window.clearTimeout(timer);
    };
  }, [autoFocusOnMount, att.download_url]);

  const revision = useFileRevision(att.file_id);
  const facts = fileCardFacts(att, revision);
  // Two files of one name read the same at a rounded size: their exact size tells them apart.
  const sizeLabel =
    facts.exactBytes !== null
      ? t_i18n("{size, plural, one {# byte} other {# bytes}}", { size: facts.exactBytes })
      : formatFileSize(att.size);
  const timeLabel = facts.createdAt ? formatTime(facts.createdAt) : null;
  const hasDetails = !!(facts.fileId || facts.shaPrefix);

  // Informational card: only the download button (and the newer-version link) is interactive.
  return (
    <div
      ref={cardRef}
      {...fileAnchor(att.file_id)}
      className={cn(
        // inline-flex: the card never stretches to its parent.
        "inline-flex max-w-[300px] min-w-[200px] items-center justify-between gap-3",
        "rounded-sm border border-elevation-subtle-soft bg-elevation-hover backdrop-blur-sm px-3 py-2.5",
        "transition-colors duration-200",
        // The arrival highlight (~2 s) is the only thing that paints the whole card.
        highlight &&
          "border-accent/40 ring-1 ring-accent/40 shadow-[0_0_24px_color-mix(in_srgb,_var(--color-accent)_calc(0.25_*_100%),_transparent)] animate-pulse",
      )}
    >
      <div className="flex min-w-0 flex-1 flex-col items-start gap-1">
        <Tip content={hasDetails ? <FileDetails facts={facts} /> : undefined}>
          <div className="w-full min-w-0">
            <div className="flex min-w-0 items-center gap-1.5">
              <div className="truncate content-base font-medium leading-tight text-default-primary">
                {att.filename}
              </div>
              <RevisionBadge revision={facts.revision} />
            </div>
            <div className="mt-1 flex min-w-0 items-center gap-1.5 text-[11px] text-default-secondary">
              <Icon className={cn("h-3.5 w-3.5 shrink-0", color)} />
              {/* A long type label truncates (full text in the title) so the
                  size and time stay visible in the chip (#7902 A5). */}
              <span className="min-w-0 truncate tracking-wide" title={typeLabel} data-testid="file-chip-type">
                {typeLabel}
              </span>
              {sizeLabel && (
                <>
                  <span className="shrink-0 text-default-secondary">·</span>
                  <span className="shrink-0 whitespace-nowrap">{sizeLabel}</span>
                </>
              )}
              {timeLabel && (
                <>
                  <span className="shrink-0 text-default-secondary">·</span>
                  <span className="shrink-0 whitespace-nowrap tabular-nums">{timeLabel}</span>
                </>
              )}
            </div>
          </div>
        </Tip>
        <NewerVersionLink fileId={att.file_id} />
      </div>
      {att.download_url && (
        // The design system's icon button: a fixed square with the DS radius.
        // The hand-made `rounded-full p-2` button it replaces took the line
        // height of the DS Icon span (an inline-block) and drew an oval (#11697).
        <IconButton
          type="button"
          variant="default"
          priority="secondary"
          size="md"
          onClick={handleClick}
          aria-label={t_i18n("Download {filename}", { filename: att.filename })}
          className="shrink-0"
          icon={<DsIcon name="download" size={16} aria-hidden />}
        />
      )}
    </div>
  );
}

export function WorkingFileChip({ att }: { att: ChatMessageAttachment }) {
  if (att.deleted_at) return <DeletedFileCard att={att} compact />;
  return <LiveWorkingFileChip att={att} />;
}

function LiveWorkingFileChip({ att }: { att: ChatMessageAttachment }) {
  const mime = resolveAttachmentMime(att);
  const { icon: Icon, color } = mimeVisual(mime);
  const typeLabel = mimeLabel(att, mime);
  const { t_i18n } = useFormatter();
  const handleClick = useChipDownload(att);
  const revision = useFileRevision(att.file_id);
  const facts = fileCardFacts(att, revision);
  const details = facts.fileId || facts.shaPrefix || facts.createdAt ? <FileDetails facts={facts} chip /> : undefined;

  // A working/scratch chip is still a real, downloadable artifact — every
  // agent-generated file carries a ``download_url``. When present, make the
  // compact pill itself the click target (the prominent download *card* stays
  // reserved for ``download_file``-tagged deliverables) so the user can always
  // retrieve the file regardless of how the agent tagged it.
  if (!att.download_url) {
    return (
      <span className="inline-flex max-w-full items-center gap-2">
        <Tip content={details}>
          <div className={inlineChipClass()} {...fileAnchor(att.file_id)}>
            <Icon className={cn("h-3.5 w-3.5 shrink-0", color)} />
            <span className="truncate font-medium">{att.filename}</span>
            <RevisionBadge revision={facts.revision} />
            <span className="text-default-secondary">{typeLabel}</span>
          </div>
        </Tip>
        <NewerVersionLink fileId={att.file_id} />
      </span>
    );
  }

  return (
    <span className="inline-flex max-w-full items-center gap-2">
      <Tip content={details}>
        {/* data-ds-exempt: the whole working-file chip is the download target
            (name, badge, type); the design system has no chip button. */}
        <button
          type="button"
          data-ds-exempt="chip"
          {...fileAnchor(att.file_id)}
          onClick={handleClick}
          aria-label={t_i18n("Download {filename}", { filename: att.filename })}
          className={inlineChipClass(true)}
        >
          <Icon className={cn("h-3.5 w-3.5 shrink-0", color)} />
          <span className="truncate font-medium">{att.filename}</span>
          <RevisionBadge revision={facts.revision} />
          <span className="text-default-secondary">{typeLabel}</span>
          <DsIcon name="download" size={16} className="text-accent/70" aria-hidden />
        </button>
      </Tip>
      <NewerVersionLink fileId={att.file_id} />
    </span>
  );
}

/**
 * A working picture's thumbnail in the opened disclosure, fetched only once it
 * is shown. Always one fixed box, whatever the picture's shape and whether it
 * is loading, ready or failed, so rows keep their height and every chip after
 * it starts at the same x. Decorative: the chip beside it names the file.
 * The muted picture icon stands in when there is nothing to show: no download
 * url (the fetch never starts), a failed fetch, or bytes the browser cannot
 * decode (never its broken-image glyph).
 */
function WorkingFileThumbnail({ att }: { att: ChatMessageAttachment }) {
  const { src, status } = useAuthenticatedSrcState(att.download_url ?? null);
  // The object url whose bytes did not decode; a new url gets its own try.
  const [undecodable, setUndecodable] = useState<string | null>(null);
  const showPicture = status === "ready" && src !== null && undecodable !== src;
  return (
    <span className="flex h-12 w-24 shrink-0 items-center justify-center overflow-hidden rounded-sm border border-elevation-subtle-soft">
      {showPicture ? (
        <img
          src={src}
          alt=""
          loading="lazy"
          onError={() => setUndecodable(src)}
          className="h-full w-full object-contain"
        />
      ) : status === "loading" ? (
        <Skeleton className="h-full w-full rounded-none" />
      ) : (
        <DsIcon name="image" size={16} className="text-default-secondary" />
      )}
    </span>
  );
}

/** From this many working files, the opened list repeats its toggle at its end. */
const WORKING_FILES_FOOTER_TOGGLE_AT = 8;

/**
 * Working files (scratch files, the pictures a tool made to check the work,
 * #6878) behind one collapsed line: opened, each is listed with its thumbnail
 * (a picture) and its download.
 */
export function WorkingFilesDisclosure({ files }: { files: ChatMessageAttachment[] }) {
  const { t_i18n } = useFormatter();
  const [open, setOpen] = useState(false);
  // Thumbnails are fetched on the first opening and kept mounted after it, so
  // closing and reopening the list neither refetches nor reflows them.
  const [opened, setOpened] = useState(false);
  const listId = useId();
  const toggleRef = useRef<HTMLButtonElement>(null);

  if (files.length === 0) return null;
  return (
    <div className="mt-2">
      <Button
        ref={toggleRef}
        type="button"
        variant="ghost"
        size="sm"
        aria-expanded={open}
        aria-controls={listId}
        onClick={() => {
          setOpen((value) => !value);
          setOpened(true);
        }}
        className="gap-1"
      >
        <DsIcon name="chevron-down" size={16} className={cn("transition-transform", open && "rotate-180")} />
        {t_i18n("Working files ({count})", { count: files.length })}
      </Button>
      <ul id={listId} hidden={!open} className="mt-1.5 space-y-1.5">
        {files.map((file, index) => (
          <li key={file.file_id ?? `working-${index}`} className="flex items-center gap-1.5">
            {/* A deleted picture has no bytes to fetch (#396): its chip alone says so. */}
            {opened && !file.deleted_at && resolveAttachmentMime(file).startsWith("image/") && <WorkingFileThumbnail att={file} />}
            <WorkingFileChip att={file} />
          </li>
        ))}
      </ul>
      {open && files.length >= WORKING_FILES_FOOTER_TOGGLE_AT && (
        <Button
          type="button"
          variant="ghost"
          size="sm"
          aria-controls={listId}
          onClick={() => {
            setOpen(false);
            toggleRef.current?.focus({ preventScroll: true });
            toggleRef.current?.scrollIntoView({ block: "nearest" });
          }}
          className="mt-1.5 gap-1"
        >
          <DsIcon name="chevron-down" size={16} className="rotate-180" />
          {t_i18n("Show less")}
        </Button>
      )}
    </div>
  );
}
