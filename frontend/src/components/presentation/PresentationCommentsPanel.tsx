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
 * Review comments on a deck — the side panel behind the toolbar's
 * `Comments` button, for decks and pages alike.
 *
 * A note is left on a SLIDE (anchored by the slide's stable id, so it follows
 * the slide through a reorder and is flagged `detached` when the slide is
 * deleted) or on the deck as a whole. Threads are flat: a root and its replies,
 * one level. Resolving is a thread-level act — the root carries it, replies
 * follow — and the toolbar badge counts the open roots.
 *
 * Who may do what is decided server-side and comes back as `can_manage` on
 * every comment (its author, or a deck manager): the panel only shows the
 * verbs it will be allowed to complete. Reading and commenting need read
 * access only — a read-only grantee reviews; the manager resolves.
 *
 * The list is polled while the panel is open (`live`), because a reviewer
 * comments from their own session, which no stream of ours observes. The
 * agent reads the OPEN threads through `get_presentation`, so a note here is
 * also an instruction the owner's assistant will pick up on its next edit —
 * and, asked to, it can answer or close one through the comment tools, which
 * run as the person asking under these same rules. The composer's
 * "Generate with AI" drafts a note against the slide on screen; the draft
 * lands in the textarea and is posted (or not) as the reviewer's own words.
 */
import { useEffect, useMemo, useState, type KeyboardEvent } from "react";
import {
  Check,
  CornerDownRight,
  MessageSquare,
  MessageSquareDashed,
  Pencil,
  RotateCcw,
  Send,
  Trash2,
  Unlink,
  X,
} from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { useConfirm } from "@/components/ui/confirm-dialog";
import { PromptGenerator } from "@/components/ui/prompt-generator";
import { SegmentedFilter } from "@/components/ui/segmented-filter";
import { useToast } from "@/components/ui/toast";
import { Tip } from "@/components/ui/tooltip";
import {
  useCreatePresentationComment,
  useDeletePresentationComment,
  useDraftPresentationComment,
  usePresentationComments,
  useUpdatePresentationComment,
} from "@/hooks/api/usePresentationApi";
import { useFormatter } from "@/i18n";
import { cn, timeAgo } from "@/lib/utils";
import type { Presentation, PresentationComment } from "@/types";

import { DECK_KEYS_OFF_ATTR } from "./deck-keys";
import { markupToPlainText } from "./markup";
import { UserInitials } from "./PresenceAvatars";
import { Spinner } from "@filigran/design-system";

/** `COMMENT_MAX_CHARS` in `models/presentation.py`. */
const COMMENT_MAX_CHARS = 4000;

type ThreadFilter = "open" | "resolved";

interface Thread {
  root: PresentationComment;
  replies: PresentationComment[];
}

/**
 * Roots with their replies, in DECK order: whole-deck notes first, then by the
 * slide they sit on, detached ones last; oldest first within a slide. Reading
 * order is the deck's own — a reviewer walks the slides, not the clock.
 */
function buildThreads(comments: PresentationComment[]): Thread[] {
  const replies = new Map<string, PresentationComment[]>();
  for (const comment of comments) {
    if (comment.parent_id) {
      const list = replies.get(comment.parent_id) ?? [];
      list.push(comment);
      replies.set(comment.parent_id, list);
    }
  }
  const rank = (c: PresentationComment) =>
    c.slide_id === null ? -1 : c.detached ? Number.MAX_SAFE_INTEGER : (c.slide_position ?? 0);
  return comments
    .filter((c) => c.parent_id === null)
    .sort((a, b) => rank(a) - rank(b) || (a.created_at ?? "").localeCompare(b.created_at ?? ""))
    .map((root) => ({ root, replies: replies.get(root.id) ?? [] }));
}

const TEXTAREA_CLASS =
  "flex w-full resize-none rounded-sm border border-elevation-subtle bg-elevation-default px-3 py-2 content-compact leading-relaxed " +
  "placeholder:text-default-secondary focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-focus";

/** A textarea that sends on `Ctrl`/`Cmd` + `Enter` and cancels on `Escape`. */
function CommentBox({
  value,
  onChange,
  onSubmit,
  onCancel,
  placeholder,
  autoFocus,
  rows = 2,
}: {
  value: string;
  onChange: (value: string) => void;
  onSubmit: () => void;
  onCancel?: () => void;
  placeholder?: string;
  autoFocus?: boolean;
  rows?: number;
}) {
  const onKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) {
      event.preventDefault();
      onSubmit();
    } else if (event.key === "Escape" && onCancel) {
      event.preventDefault();
      onCancel();
    }
  };
  return (
    <textarea
      className={TEXTAREA_CLASS}
      rows={rows}
      value={value}
      onChange={(e) => onChange(e.target.value)}
      onKeyDown={onKeyDown}
      placeholder={placeholder}
      maxLength={COMMENT_MAX_CHARS}
      // A reply / edit box opens on demand, so it takes the caret at once.
      autoFocus={autoFocus}
    />
  );
}

function CommentRow({
  comment,
  isReply,
  onReply,
  onEdit,
  onDelete,
  onResolve,
  busy,
}: {
  comment: PresentationComment;
  isReply: boolean;
  onReply?: () => void;
  onEdit: (body: string) => Promise<void>;
  onDelete: () => void;
  /** Root only: resolve (open thread) or reopen (resolved thread). */
  onResolve?: () => void;
  busy: boolean;
}) {
  const { t_i18n } = useFormatter();
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(comment.body);

  const author = comment.author_name?.trim() || t_i18n("Someone");
  const when = comment.created_at ? timeAgo(comment.created_at) : "";
  // Edited = saved again after it was written. Compared as instants with a
  // second of slack, not as strings: the two stamps are written by separate
  // clock reads and may differ by microseconds on a comment nobody touched.
  const edited =
    !!comment.updated_at &&
    !!comment.created_at &&
    new Date(comment.updated_at).getTime() - new Date(comment.created_at).getTime() > 1000;

  const submitEdit = async () => {
    const text = draft.trim();
    if (!text || text === comment.body) {
      setEditing(false);
      setDraft(comment.body);
      return;
    }
    await onEdit(text);
    setEditing(false);
  };

  return (
    <div className={cn("group/comment flex gap-2", isReply && "pl-6")}>
      {isReply ? (
        <CornerDownRight className="mt-1 h-3.5 w-3.5 shrink-0 text-default-secondary" aria-hidden="true" />
      ) : null}
      <UserInitials userId={comment.author_id} name={comment.author_name} size="sm" className="mt-0.5" />
      <div className="min-w-0 flex-1">
        <div className="flex items-baseline gap-1.5 content-compact">
          <span className="truncate font-medium text-default-primary">{author}</span>
          {when && <span className="shrink-0 text-default-secondary">{when}</span>}
          {edited && <span className="shrink-0 text-default-secondary">{t_i18n("(edited)")}</span>}
        </div>
        {editing ? (
          <div className="mt-1 space-y-1.5">
            <CommentBox value={draft} onChange={setDraft} onSubmit={() => void submitEdit()} onCancel={() => {
              setEditing(false);
              setDraft(comment.body);
            }} autoFocus rows={3} />
            <div className="flex justify-end gap-1.5">
              <Button variant="ghost" size="sm" className="h-7 content-compact" onClick={() => {
                setEditing(false);
                setDraft(comment.body);
              }}>
                {t_i18n("Cancel")}
              </Button>
              <Button size="sm" className="h-7 content-compact" onClick={() => void submitEdit()} disabled={busy || !draft.trim()}>
                {t_i18n("Save")}
              </Button>
            </div>
          </div>
        ) : (
          <p className="mt-0.5 whitespace-pre-wrap break-words text-[12.5px] leading-relaxed text-default-secondary">
            {comment.body}
          </p>
        )}
        {!editing && (
          <div className="mt-1 flex items-center gap-0.5 opacity-0 transition-opacity group-hover/comment:opacity-100 group-focus-within/comment:opacity-100">
            {onReply && (
              <Button variant="ghost" size="sm" className="h-6 px-1.5 content-compact text-default-secondary" onClick={onReply}>
                {t_i18n("Reply")}
              </Button>
            )}
            {onResolve && comment.can_manage && (
              <Button
                variant="ghost"
                size="sm"
                className="h-6 gap-1 px-1.5 content-compact text-default-secondary"
                onClick={onResolve}
                disabled={busy}
              >
                {comment.resolved ? (
                  <>
                    <RotateCcw className="h-3 w-3" /> {t_i18n("Reopen")}
                  </>
                ) : (
                  <>
                    <Check className="h-3 w-3" /> {t_i18n("Resolve")}
                  </>
                )}
              </Button>
            )}
            {comment.can_manage && (
              <>
                <Tip content={t_i18n("Edit")}>
                  <Button
                    variant="ghost"
                    size="sm"
                    className="h-6 w-6 p-0 text-default-secondary"
                    onClick={() => setEditing(true)}
                    aria-label={t_i18n("Edit")}
                    disabled={busy}
                  >
                    <Pencil className="h-3 w-3" />
                  </Button>
                </Tip>
                <Tip content={t_i18n("Delete")}>
                  <Button
                    variant="ghost"
                    size="sm"
                    className="h-6 w-6 p-0 text-default-secondary hover:text-feedback-error-primary"
                    onClick={onDelete}
                    aria-label={t_i18n("Delete")}
                    disabled={busy}
                  >
                    <Trash2 className="h-3 w-3" />
                  </Button>
                </Tip>
              </>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

export function PresentationCommentsPanel({
  presentation,
  activeIndex,
  onSelectSlide,
  onClose,
  className,
}: {
  presentation: Presentation;
  /** The slide on screen — the default anchor of a new note, and the
   *  "this slide" filter. */
  activeIndex: number;
  /** Jump the deck to a slide when its chip is clicked. */
  onSelectSlide?: (index: number) => void;
  onClose: () => void;
  className?: string;
}) {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const [ConfirmEl, confirm] = useConfirm();
  const isPage = presentation.kind === "page";

  const { data: comments, isLoading } = usePresentationComments(presentation.id, { live: true });
  const create = useCreatePresentationComment();
  const update = useUpdatePresentationComment();
  const remove = useDeletePresentationComment();
  const draftNote = useDraftPresentationComment();
  const busy = create.isPending || update.isPending || remove.isPending;

  const [filter, setFilter] = useState<ThreadFilter>("open");
  const [thisSlideOnly, setThisSlideOnly] = useState(false);
  const [replyTo, setReplyTo] = useState<string | null>(null);
  const [reply, setReply] = useState("");
  const [draft, setDraft] = useState("");
  // Where a new note goes: the slide on screen, or the deck as a whole.
  const [anchorDeck, setAnchorDeck] = useState(false);

  const activeSlide = presentation.spec.slides[activeIndex];
  // A deck stored before slides had ids cannot anchor a note to one.
  const canAnchorSlide = !!activeSlide?.id;

  const threads = useMemo(() => buildThreads(comments ?? []), [comments]);
  const openCount = threads.filter((t) => !t.root.resolved).length;
  const resolvedCount = threads.length - openCount;
  const visible = threads.filter((t) => {
    if ((filter === "open") === t.root.resolved) return false;
    if (thisSlideOnly) return !!activeSlide?.id && t.root.slide_id === activeSlide.id;
    return true;
  });

  // The thread being replied to may vanish under a live refresh.
  useEffect(() => {
    if (replyTo && !threads.some((t) => t.root.id === replyTo)) setReplyTo(null);
  }, [replyTo, threads]);

  const fail = (err: unknown, title: string) =>
    toast({ title, description: err instanceof Error ? err.message : undefined, variant: "error" });

  // "Generate with AI": the brief and the anchor go up, the server reads the
  // slide (or the thread, for a reply) from the deck and drafts the note. The
  // draft only replaces the textarea; posting stays the reviewer's click.
  const draftWith = (parentId: string | null, currentText: string) => async (brief: string) => {
    try {
      const { body } = await draftNote.mutateAsync({
        presentationId: presentation.id,
        brief,
        slide_id: parentId || anchorDeck || !canAnchorSlide ? null : activeSlide.id,
        parent_id: parentId,
        current_text: currentText.trim() || null,
      });
      return body;
    } catch (err) {
      fail(err, t_i18n("Could not draft the comment"));
      throw err;
    }
  };

  const submitNew = async () => {
    const body = draft.trim();
    if (!body) return;
    try {
      await create.mutateAsync({
        presentationId: presentation.id,
        body,
        slide_id: anchorDeck || !canAnchorSlide ? null : activeSlide.id,
      });
      setDraft("");
      // A note just left is read on the open list, whatever was showing.
      setFilter("open");
    } catch (err) {
      fail(err, t_i18n("Could not add the comment"));
    }
  };

  const submitReply = async (rootId: string) => {
    const body = reply.trim();
    if (!body) return;
    try {
      await create.mutateAsync({ presentationId: presentation.id, body, parent_id: rootId });
      setReply("");
      setReplyTo(null);
    } catch (err) {
      fail(err, t_i18n("Could not add the comment"));
    }
  };

  const edit = async (comment: PresentationComment, body: string) => {
    try {
      await update.mutateAsync({ presentationId: presentation.id, commentId: comment.id, body });
    } catch (err) {
      fail(err, t_i18n("Could not update the comment"));
    }
  };

  const resolve = async (root: PresentationComment) => {
    try {
      await update.mutateAsync({
        presentationId: presentation.id,
        commentId: root.id,
        resolved: !root.resolved,
      });
    } catch (err) {
      fail(err, t_i18n("Could not update the comment"));
    }
  };

  const destroy = async (comment: PresentationComment, replyCount: number) => {
    const ok = await confirm({
      title: t_i18n("Delete this comment?"),
      description:
        !comment.parent_id && replyCount > 0
          ? t_i18n(
              "{count, plural, one {Its reply goes} other {Its # replies go}} with it. This cannot be undone.",
              { count: replyCount },
            )
          : t_i18n("This cannot be undone."),
      confirmLabel: t_i18n("Delete"),
      variant: "danger",
    });
    if (!ok) return;
    try {
      await remove.mutateAsync({ presentationId: presentation.id, commentId: comment.id });
    } catch (err) {
      fail(err, t_i18n("Could not delete the comment"));
    }
  };

  const anchorLabel = (root: PresentationComment): string => {
    if (root.slide_id === null) return isPage ? t_i18n("Whole document") : t_i18n("Whole deck");
    if (root.detached) return isPage ? t_i18n("Page removed") : t_i18n("Slide removed");
    return isPage
      ? t_i18n("Page {position}", { position: root.slide_position ?? 0 })
      : t_i18n("Slide {position}", { position: root.slide_position ?? 0 });
  };

  const unitPosition = activeIndex + 1;
  const composerTarget =
    anchorDeck || !canAnchorSlide
      ? isPage
        ? t_i18n("Whole document")
        : t_i18n("Whole deck")
      : isPage
        ? t_i18n("Page {position}", { position: unitPosition })
        : t_i18n("Slide {position}", { position: unitPosition });

  return (
    <aside
      className={cn(
        "flex h-full w-full min-w-0 flex-col border-l border-elevation-subtle-soft layer-1 bg-elevation-default backdrop-blur-sm",
        className,
      )}
      aria-label={t_i18n("Comments")}
      // Typing a note must not step the deck next to it.
      {...DECK_KEYS_OFF_ATTR}
    >
      <div className="flex items-center justify-between border-b border-elevation-subtle-soft px-4 py-3">
        <div>
          <h2 className="content-base font-semibold">{t_i18n("Comments")}</h2>
          <p className="content-compact text-default-secondary">
            {isPage
              ? t_i18n("Review notes on pages. Your assistant reads the open ones and closes them when you ask.")
              : t_i18n("Review notes on slides. Your assistant reads the open ones and closes them when you ask.")}
          </p>
        </div>
        <Button variant="ghost" size="sm" onClick={onClose} aria-label={t_i18n("Close")}>
          <X className="h-4 w-4" />
        </Button>
      </div>

      <div className="flex items-center justify-between gap-2 border-b border-elevation-subtle-soft px-4 py-2">
        <div className="flex items-center gap-2">
          <SegmentedFilter<ThreadFilter>
            options={[
              { key: "open", label: "Open" },
              { key: "resolved", label: "Resolved" },
            ]}
            value={filter}
            onChange={setFilter}
            ariaLabel={t_i18n("Comment status")}
          />
          <span className="content-compact tabular-nums text-default-secondary">
            {filter === "open" ? openCount : resolvedCount}
          </span>
        </div>
        <button
          type="button"
          onClick={() => setThisSlideOnly((v) => !v)}
          aria-pressed={thisSlideOnly}
          disabled={!canAnchorSlide}
          className={cn(
            "inline-flex h-7 items-center gap-1.5 rounded-sm border px-2 content-compact font-medium transition-colors disabled:opacity-40",
            thisSlideOnly
              ? "border-accent/30 bg-accent/15 text-accent"
              : "border-elevation-subtle-soft bg-elevation-hover text-default-secondary hover:bg-elevation-highlight hover:text-default-primary",
          )}
        >
          {isPage ? t_i18n("This page") : t_i18n("This slide")}
        </button>
      </div>

      <div className="flex-1 space-y-2 overflow-y-auto px-4 py-3">
        {isLoading ? (
          <div className="flex justify-center py-8">
            <Spinner size="md" tone="inherit" className="text-accent" label={t_i18n("Loading...")} />
          </div>
        ) : visible.length === 0 ? (
          <div className="flex flex-col items-center gap-2 px-4 py-10 text-center">
            {filter === "open" ? (
              <MessageSquare className="h-6 w-6 text-default-secondary" aria-hidden="true" />
            ) : (
              <MessageSquareDashed className="h-6 w-6 text-default-secondary" aria-hidden="true" />
            )}
            <p className="text-[12.5px] text-default-secondary">
              {filter === "open"
                ? thisSlideOnly
                  ? isPage
                    ? t_i18n("No open comments on this page.")
                    : t_i18n("No open comments on this slide.")
                  : t_i18n("No open comments. Leave a note below - collaborators and your assistant will see it.")
                : t_i18n("Nothing resolved yet.")}
            </p>
          </div>
        ) : (
          visible.map(({ root, replies }) => {
            // 1-based on the wire (`slide_position`), resolved against the
            // CURRENT spec on every read, so a reorder moves the chip's number.
            const target = root.slide_id !== null && !root.detached ? root.slide_position : null;
            const jumpable = target !== null;
            // The title is authored in inline markup (`**bold**`, tones): the
            // chip shows the words, not the syntax.
            const heading = markupToPlainText(root.slide_title);
            return (
              <article
                key={root.id}
                className={cn(
                  "rounded-sm border border-elevation-subtle-soft bg-elevation-default p-3",
                  root.resolved && "opacity-80",
                  target === activeIndex + 1 && "border-accent/40",
                )}
              >
                <div className="mb-2 flex items-center gap-1.5">
                  <button
                    type="button"
                    onClick={() => {
                      if (target !== null) onSelectSlide?.(target - 1);
                    }}
                    disabled={!jumpable}
                    className={cn(
                      "inline-flex min-w-0 items-center gap-1 rounded-sm border px-1.5 py-0.5 content-compact font-medium",
                      root.detached
                        ? "border-feedback-alert-primary/30 bg-feedback-alert-primary/10 text-feedback-warning-tertiary"
                        : jumpable
                          ? "border-accent/30 bg-accent/10 text-accent hover:bg-accent/20"
                          : "border-elevation-subtle-soft text-default-secondary",
                    )}
                    aria-label={jumpable ? t_i18n("Go to {anchor}", { anchor: anchorLabel(root) }) : undefined}
                  >
                    {root.detached && <Unlink className="h-2.5 w-2.5" aria-hidden="true" />}
                    <span className="shrink-0">{anchorLabel(root)}</span>
                    {heading && <span className="truncate font-normal opacity-80">· {heading}</span>}
                  </button>
                  {root.resolved && (
                    <Badge variant="success" className="ml-auto shrink-0 gap-1 px-1.5 py-0">
                      <Check className="h-2.5 w-2.5" />
                      {root.resolved_by_name
                        ? t_i18n("Resolved by {name}", { name: root.resolved_by_name })
                        : t_i18n("Resolved")}
                    </Badge>
                  )}
                </div>

                <div className="space-y-2.5">
                  <CommentRow
                    comment={root}
                    isReply={false}
                    onReply={() => {
                      setReplyTo(replyTo === root.id ? null : root.id);
                      setReply("");
                    }}
                    onEdit={(body) => edit(root, body)}
                    onDelete={() => void destroy(root, replies.length)}
                    onResolve={() => void resolve(root)}
                    busy={busy}
                  />
                  {replies.map((item) => (
                    <CommentRow
                      key={item.id}
                      comment={item}
                      isReply
                      onEdit={(body) => edit(item, body)}
                      onDelete={() => void destroy(item, 0)}
                      busy={busy}
                    />
                  ))}
                </div>

                {replyTo === root.id && (
                  <div className="mt-2 space-y-1.5 pl-6">
                    <CommentBox
                      value={reply}
                      onChange={setReply}
                      onSubmit={() => void submitReply(root.id)}
                      onCancel={() => setReplyTo(null)}
                      placeholder={t_i18n("Reply…")}
                      autoFocus
                    />
                    <PromptGenerator
                      contextType="presentation_comment"
                      currentText={reply}
                      onGenerated={setReply}
                      generate={draftWith(root.id, reply)}
                    />
                    <div className="flex justify-end gap-1.5">
                      <Button variant="ghost" size="sm" className="h-7 content-compact" onClick={() => setReplyTo(null)}>
                        {t_i18n("Cancel")}
                      </Button>
                      <Button
                        size="sm"
                        className="h-7 gap-1 content-compact"
                        onClick={() => void submitReply(root.id)}
                        disabled={busy || !reply.trim()}
                      >
                        <Send className="h-3 w-3" /> {t_i18n("Reply")}
                      </Button>
                    </div>
                  </div>
                )}
              </article>
            );
          })
        )}
      </div>

      <div className="space-y-2 border-t border-elevation-subtle-soft px-4 py-3">
        <div className="flex items-center gap-1" role="group" aria-label={t_i18n("Comment on")}>
          <span className="mr-1 content-compact text-default-secondary">{t_i18n("On")}</span>
          <button
            type="button"
            onClick={() => setAnchorDeck(false)}
            aria-pressed={!anchorDeck && canAnchorSlide}
            disabled={!canAnchorSlide}
            className={cn(
              "inline-flex h-6 items-center rounded-sm border px-2 content-compact font-medium transition-colors disabled:opacity-40",
              !anchorDeck && canAnchorSlide
                ? "border-accent/30 bg-accent/15 text-accent"
                : "border-elevation-subtle-soft bg-elevation-hover text-default-secondary hover:bg-elevation-highlight hover:text-default-primary",
            )}
          >
            {isPage
              ? t_i18n("Page {position}", { position: unitPosition })
              : t_i18n("Slide {position}", { position: unitPosition })}
          </button>
          <button
            type="button"
            onClick={() => setAnchorDeck(true)}
            aria-pressed={anchorDeck || !canAnchorSlide}
            className={cn(
              "inline-flex h-6 items-center rounded-sm border px-2 content-compact font-medium transition-colors",
              anchorDeck || !canAnchorSlide
                ? "border-accent/30 bg-accent/15 text-accent"
                : "border-elevation-subtle-soft bg-elevation-hover text-default-secondary hover:bg-elevation-highlight hover:text-default-primary",
            )}
          >
            {isPage ? t_i18n("Whole document") : t_i18n("Whole deck")}
          </button>
        </div>
        <CommentBox
          value={draft}
          onChange={setDraft}
          onSubmit={() => void submitNew()}
          placeholder={t_i18n("Leave a note on {target}…", { target: composerTarget })}
        />
        {/* Drafts against the composer's anchor — the slide on screen or the
            whole deck — and improves a note already started. */}
        <PromptGenerator
          contextType="presentation_comment"
          currentText={draft}
          onGenerated={setDraft}
          generate={draftWith(null, draft)}
        />
        <div className="flex items-center justify-between gap-2">
          <span className="content-compact text-default-secondary">{t_i18n("Ctrl + Enter to send")}</span>
          <Button size="sm" className="h-7 gap-1.5 content-compact" onClick={() => void submitNew()} disabled={busy || !draft.trim()} aria-busy={create.isPending || undefined}>
            {create.isPending && <Spinner size="sm" tone="inherit" />}{!create.isPending && <Send className="h-3 w-3" />}
            {t_i18n("Comment")}
          </Button>
        </div>
      </div>
      {ConfirmEl}
    </aside>
  );
}
