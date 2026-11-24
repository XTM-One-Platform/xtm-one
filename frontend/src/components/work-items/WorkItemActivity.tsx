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
 * A work item's description as a person reads it (#776, scope F): the notes
 * the platform stamped on top as an "Activity" timeline (each entry headed by
 * its time in the reader's zone and the agent that wrote it, the run a small
 * link, the body as Markdown with its long ids as copyable chips, long notes
 * folded), then the "Brief" they were written over. Presentation only: the
 * stored text is not rewritten.
 */
import { useCallback, useLayoutEffect, useMemo, useRef, useState, type FocusEvent, type ReactNode } from "react";
import { Link } from "react-router-dom";
import { Icon } from "@filigran/design-system";
import { ChatMarkdown } from "@/components/chat/ChatMarkdown";
import { InlineMarkerContext } from "@/components/chat/inline-marker-context";
import { writeToClipboard } from "@/components/ui/copy-icon-button";
import { Tip } from "@/components/ui/tooltip";
import { useFormatter } from "@/i18n";
import { cn } from "@/lib/utils";
import { ID_CHIP_KIND, isLongNote, linkifyIds, splitDescription, type WorkItemNote } from "@/lib/work-item-notes";
import type { WorkItemNoteRun } from "@/types";

/** An id shown as its first 8 characters; a click copies it whole. */
function IdChip({ value }: { value: string }) {
  const { t_i18n } = useFormatter();
  const [copied, setCopied] = useState(false);
  const copy = useCallback(async () => {
    try {
      await writeToClipboard(value);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      setCopied(false);
    }
  }, [value]);
  return (
    <Tip content={copied ? t_i18n("Copied!") : value}>
      <button
        type="button"
        onClick={copy}
        aria-label={t_i18n("Copy {value}", { value })}
        data-testid="work-item-id-chip"
        className="mx-0.5 inline-flex items-center rounded-sm bg-elevation-hover px-1 font-mono-plex content-compact text-default-secondary align-baseline hover:text-accent focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-focus"
      >
        {value.length > 8 ? value.slice(0, 8) : value}
      </button>
    </Tip>
  );
}

function renderMarker(kind: string, args: string): ReactNode {
  if (kind !== ID_CHIP_KIND || !args) return null;
  return <IdChip value={args} />;
}

/** A body or brief as Markdown, its ids as chips and its refs as links. */
function NoteMarkdown({ text, repo }: { text: string; repo: string | null }) {
  const linked = useMemo(() => linkifyIds(text, repo), [text, repo]);
  return (
    <InlineMarkerContext.Provider value={renderMarker}>
      <ChatMarkdown content={linked} className="chat-markdown content-base" remoteImages={false} />
    </InlineMarkerContext.Provider>
  );
}

function NoteEntry({
  note,
  run,
  repo,
}: {
  note: WorkItemNote;
  run: WorkItemNoteRun | undefined;
  repo: string | null;
}) {
  const { t_i18n, intl } = useFormatter();
  const long = isLongNote(note.body);
  const [open, setOpen] = useState(false);
  const folded = long && !open;
  const bodyRef = useRef<HTMLDivElement>(null);
  // A folded body always shows its first lines: nothing may leave it scrolled
  // (UX round 3, B1: "Show less" after a keyboard visit kept scrollTop 405 px).
  useLayoutEffect(() => {
    if (folded && bodyRef.current) bodyRef.current.scrollTop = 0;
  }, [folded]);
  const openOnFocus = useCallback((event: FocusEvent<HTMLDivElement>) => {
    const target = event.target;
    event.currentTarget.scrollTop = 0;
    setOpen(true);
    // Once open, bring the focused link or chip into the dialog's visible area.
    requestAnimationFrame(() => target.scrollIntoView?.({ block: "nearest" }));
  }, []);
  const when = intl.formatDate(note.at, { weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
  const author = run?.agent?.name ?? (note.runId ? t_i18n("An agent run") : t_i18n("The platform"));
  return (
    <li className="relative border-l border-elevation-subtle pl-3" data-testid="work-item-note">
      <div className="flex flex-wrap items-center gap-x-2 gap-y-0.5 content-caption text-default-secondary">
        <time dateTime={note.at} className="text-default-primary">{when}</time>
        <span data-testid="work-item-note-author">{author}</span>
        {note.runId &&
          (run?.accessible ? (
            <Link
              to={`/operations/executions/${note.runId}`}
              className="inline-flex items-center gap-1 font-mono-plex text-accent hover:underline"
              aria-label={t_i18n("Open the run")}
            >
              <Icon name="history" size={16} className="shrink-0" aria-hidden />
              {note.runId.slice(0, 8)}
            </Link>
          ) : (
            <IdChip value={note.runId} />
          ))}
      </div>
      {/* A height cap, not line-clamp: the body is block Markdown (div > p), which
          -webkit-line-clamp on a wrapper does not cut (UX round 1, B1). 10.5em of
          the list item's caption font (126 px): about 5 lines of content-base,
          faded at the bottom (UX round 2, A6). Focus
          never lands in the hidden part: a link or chip below the fold opens the
          note when it takes focus (UX round 2, B1). overflow-clip, not
          overflow-hidden: a clip box cannot be scrolled, so the browser's focus
          scroll never moves the folded text (UX round 3, B1). */}
      <div
        ref={bodyRef}
        className={cn("relative pt-1", folded && "max-h-[10.5em] overflow-clip")}
        data-testid="work-item-note-body"
        data-folded={folded ? "true" : undefined}
        onFocusCapture={folded ? openOnFocus : undefined}
      >
        <NoteMarkdown text={note.body} repo={repo} />
        {folded && (
          <div
            className="pointer-events-none absolute inset-x-0 bottom-0 h-8 bg-gradient-to-t from-[var(--bg-elevation-default)] to-transparent"
            data-testid="work-item-note-fade"
            aria-hidden
          />
        )}
      </div>
      {long && (
        <button
          type="button"
          onClick={() => setOpen((v) => !v)}
          aria-expanded={open}
          className="mt-1 rounded-sm content-compact text-accent hover:underline focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-focus"
        >
          {open ? t_i18n("Show less") : t_i18n("Show more")}
        </button>
      )}
    </li>
  );
}

/**
 * The description of *description*: its notes as an "Activity" timeline,
 * newest first, then its "Brief". A description without notes renders as its
 * Markdown, as before.
 */
export function WorkItemActivity({
  description,
  runs,
  repo = null,
}: {
  description: string;
  runs?: Record<string, WorkItemNoteRun>;
  /** owner/name the item's PRs live in: links a bare #N and a commit SHA. */
  repo?: string | null;
}) {
  const { t_i18n } = useFormatter();
  const { notes, brief } = useMemo(() => splitDescription(description), [description]);
  if (!notes.length) return <NoteMarkdown text={description} repo={repo} />;
  return (
    <div className="space-y-4">
      <div>
        <h4 className="mb-2 content-caption font-semibold uppercase tracking-wide text-default-secondary">{t_i18n("Activity")}</h4>
        <ol className="space-y-3" data-testid="work-item-activity">
          {notes.map((note, index) => (
            <NoteEntry key={`${note.at}-${index}`} note={note} run={note.runId ? runs?.[note.runId] : undefined} repo={repo} />
          ))}
        </ol>
      </div>
      {brief && (
        <div className="border-t border-elevation-subtle pt-3" data-testid="work-item-brief">
          <h4 className="mb-2 content-caption font-semibold uppercase tracking-wide text-default-secondary">{t_i18n("Brief")}</h4>
          <NoteMarkdown text={brief} repo={repo} />
        </div>
      )}
    </div>
  );
}
