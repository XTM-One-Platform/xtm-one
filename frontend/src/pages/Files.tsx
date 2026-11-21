// SPDX-License-Identifier: Apache-2.0

/**
 * Work > Files — the cross-context file repository.
 *
 * Every file the caller may READ, wherever it was put: uploads and outputs of
 * conversations (own, shared, company-managed), outputs of assignment runs the
 * caller may inspect, and the loose files of datasets / tool tests / flows.
 * Files are still written by the surfaces that produce them — the chat upload,
 * `generate_file`, run outputs; this page is where they are found afterwards.
 *
 * The list is server-filtered and server-paginated (`GET /files`), so it stays
 * responsive on a busy platform. Filters live in the URL, which is what lets a
 * run page or a conversation link straight into "its" files
 * (`/files?run=<id>`, `/files?conversation=<id>`) and what makes the back
 * button restore the view.
 *
 * Grouping is by CONTEXT (the conversation or run a file belongs to), on the
 * current page only: files come newest-first, so a group's position is that of
 * its most recent file — the conversation an agent just wrote into is at the
 * top, not the one whose title sorts first.
 */
import { useCallback, useEffect, useId, useMemo, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import {
  ClipboardList,
  Download,
  ExternalLink,
  FolderOpen,
  MessageSquare,
  Sparkles,
  Trash2,
  Upload,
  X,
} from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { useConfirm } from "@/components/ui/confirm-dialog";
import { EmptyState } from "@/components/ui/empty-state";
import { FiltersPill } from "@/components/ui/filter-pill";
import { Label } from "@/components/ui/label";
import { OverflowMenu, type OverflowMenuItem } from "@/components/ui/overflow-menu";
import { Pagination } from "@/components/ui/pagination";
import { TotalChip } from "@/components/ui/total-chip";
import { SearchInput } from "@/components/ui/search-input";
import { SegmentedFilter, type SegmentedFilterOption } from "@/components/ui/segmented-filter";
import { ListSkeleton } from "@/components/ui/skeleton";
import { Switch } from "@/components/ui/switch";
import { useToast } from "@/components/ui/toast";
import { Tip } from "@/components/ui/tooltip";
import { VisibilityEmptyHint, VisibilityFilter } from "@/components/ui/visibility-filter";
import { FILES_KEY, useDeleteFile, useFiles } from "@/hooks/api/useFileApi";
import { useSegmentCounts } from "@/hooks/api/useSegmentCounts";
import { useDebounce } from "@/hooks/useDebounce";
import { useVisibilityParam, visibilityQuery } from "@/hooks/useVisibilityParam";
import { useFormatter } from "@/i18n";
import type {
  ConversationFileOrigin,
  FileContext,
  RepositoryFile,
  RepositoryFilesQuery,
} from "@/types";
import {
  authenticatedDownload,
  extensionFromFilename,
  formatFileSize,
  mimeLabel,
  mimeVisual,
  resolveAttachmentMime,
} from "./chat/fileVisuals";
import { formatTimeAgo } from "@/lib/relative-time";
import { cn } from "@/lib/utils";
import { useDeletedFileNotice } from "@/pages/chat/deleted-file-notice";

const PAGE_SIZE = 50;

type ContextFilter = FileContext | "all";
type OriginFilter = ConversationFileOrigin | "all";
/** Whether the file's content is kept (#396): every file, kept ones, deleted ones. */
type StatusFilter = "all" | "live" | "deleted";
const STATUS_VALUES: readonly StatusFilter[] = ["all", "live", "deleted"];
const PARAM_STATUS = "status";

/* URL parameter names. Short on purpose — these are shared as links. */
const PARAM_SEARCH = "q";
const PARAM_CONTEXT = "context";
const PARAM_ORIGIN = "origin";
const PARAM_CONVERSATION = "conversation";
const PARAM_RUN = "run";
const PARAM_PAGE = "page";
const PARAM_FLAT = "flat";

const CONTEXT_VALUES: readonly ContextFilter[] = ["all", "conversation", "run", "other"];
const ORIGIN_VALUES: readonly OriginFilter[] = ["all", "uploaded", "generated"];

function pick<T extends string>(raw: string | null, allowed: readonly T[], fallback: T): T {
  return raw !== null && (allowed as readonly string[]).includes(raw) ? (raw as T) : fallback;
}

/** Labels for the `other` context, keyed by the raw backend `source`. */
function sourceLabel(source: string, t: (s: string) => string): string {
  if (source === "dataset") return t("Dataset");
  if (source === "tool_test") return t("Tool test");
  if (source === "flow") return t("Flow");
  if (source.startsWith("channel:")) return t("Channel");
  return t("Other");
}

/** The stable key a page is grouped by, and what its header shows. */
interface FileGroup {
  key: string;
  context: FileContext;
  title: string;
  subtitle: string | null;
  href: string | null;
  files: RepositoryFile[];
}

function groupFiles(files: RepositoryFile[], t: (s: string) => string): FileGroup[] {
  const groups = new Map<string, FileGroup>();
  for (const file of files) {
    let key: string;
    let title: string;
    let subtitle: string | null = null;
    let href: string | null = null;
    if (file.context === "conversation" && file.conversation_id) {
      key = `conversation:${file.conversation_id}`;
      title = file.conversation_title ?? t("Untitled conversation");
      href = `/chat?c=${file.conversation_id}`;
    } else if (file.context === "run" && file.assignment_run_id) {
      key = `run:${file.assignment_run_id}`;
      title = file.run_label ?? t("Run");
      subtitle = file.agent_name;
      href = `/operations/executions/${file.assignment_run_id}`;
    } else {
      key = `other:${file.source}`;
      title = sourceLabel(file.source, t);
    }
    const existing = groups.get(key);
    if (existing) existing.files.push(file);
    else groups.set(key, { key, context: file.context, title, subtitle, href, files: [file] });
  }
  return [...groups.values()];
}

function ContextIcon({ context, className }: { context: FileContext; className?: string }) {
  if (context === "conversation") return <MessageSquare className={className} />;
  if (context === "run") return <ClipboardList className={className} />;
  return <FolderOpen className={className} />;
}

function FileRow({
  file,
  showContext,
  onDelete,
}: {
  file: RepositoryFile;
  /** In the flat view every row names its context; in a group the header does. */
  showContext: boolean;
  onDelete: (file: RepositoryFile) => void;
}) {
  const { t_i18n, intl } = useFormatter();
  const navigate = useNavigate();
  const extension = extensionFromFilename(file.filename);
  const visualLike = { type: extension, content_type: file.content_type };
  const mime = resolveAttachmentMime(visualLike);
  const { icon: Icon, color } = mimeVisual(mime);
  const typeLabel = mimeLabel(visualLike, mime);

  const contextHref =
    file.context === "conversation" && file.conversation_id
      ? `/chat?c=${file.conversation_id}`
      : file.context === "run" && file.assignment_run_id
        ? `/operations/executions/${file.assignment_run_id}`
        : null;
  const contextLabel =
    file.context === "conversation"
      ? file.conversation_title ?? t_i18n("Untitled conversation")
      : file.context === "run"
        ? (file.run_label ?? t_i18n("Run"))
        : sourceLabel(file.source, t_i18n);

  const download = () => void authenticatedDownload(file.download_url, file.filename);
  // A deleted file (#396): its row stays, muted, without a download.
  const deleted = !!file.deleted_at;
  const deletedNotice = useDeletedFileNotice();

  const items: OverflowMenuItem[] = [
    ...(deleted ? [] : [{ id: "download", label: t_i18n("Download"), icon: Download, onSelect: download }]),
    ...(contextHref
      ? [
          {
            id: "open",
            label: file.context === "run" ? t_i18n("Open run") : t_i18n("Open conversation"),
            icon: ExternalLink,
            onSelect: () => navigate(contextHref),
          },
        ]
      : []),
    ...(file.can_delete
      ? [
          {
            id: "delete",
            label: t_i18n("Delete"),
            icon: Trash2,
            onSelect: () => onDelete(file),
            destructive: true,
          },
        ]
      : []),
  ];

  return (
    <li className="group flex items-center gap-3 px-4 py-2.5 transition-colors hover:bg-elevation-hover">
      <div className={`flex h-9 w-9 shrink-0 items-center justify-center rounded-sm bg-elevation-hover ${color}`}>
        <Icon className="h-4 w-4" />
      </div>
      <button
        type="button"
        onClick={deleted ? undefined : download}
        disabled={deleted}
        className="min-w-0 flex-1 text-left disabled:cursor-default"
        aria-label={
          deleted
            ? t_i18n("{filename}: {why}", { filename: file.filename, why: deletedNotice(file.deleted_at!, file.deleted_reason) })
            : t_i18n("Download {name}", { name: file.filename })
        }
        data-testid={deleted ? "files.row.deleted" : undefined}
      >
        <p className={cn("truncate content-base font-medium", deleted && "text-default-secondary")}>{file.filename}</p>
        <p className="flex flex-wrap items-center gap-x-1.5 truncate text-[11px] text-default-secondary">
          <span>{formatFileSize(file.size)}</span>
          <span aria-hidden>·</span>
          <span>{extension ? extension.toUpperCase() : typeLabel}</span>
          <span aria-hidden>·</span>
          <span>{formatTimeAgo(intl, file.created_at)}</span>
          {deleted ? (
            <>
              <span aria-hidden>·</span>
              <span>{deletedNotice(file.deleted_at!, file.deleted_reason)}</span>
            </>
          ) : file.expires_at ? (
            <>
              <span aria-hidden>·</span>
              <span title={intl.formatDate(new Date(file.expires_at), { dateStyle: "medium", timeStyle: "short" })}>
                {t_i18n("Expires {date} if unused", { date: intl.formatDate(new Date(file.expires_at), { dateStyle: "medium" }) })}
              </span>
            </>
          ) : null}
          {showContext && (
            <>
              <span aria-hidden>·</span>
              <span className="inline-flex items-center gap-1">
                <ContextIcon context={file.context} className="h-3 w-3" />
                <span className="truncate">{contextLabel}</span>
              </span>
            </>
          )}
        </p>
      </button>
      <div className="flex shrink-0 items-center gap-1.5">
        {file.origin === "generated" ? (
          <Badge variant="cyan" className="gap-1 px-1.5 py-0 content-caption">
            <Sparkles className="h-2.5 w-2.5" /> {t_i18n("Generated")}
          </Badge>
        ) : file.origin === "uploaded" ? (
          <Badge variant="outline" className="gap-1 px-1.5 py-0 content-caption">
            <Upload className="h-2.5 w-2.5" /> {t_i18n("Uploaded")}
          </Badge>
        ) : null}
        <div className="flex items-center gap-0.5 opacity-0 transition-opacity group-hover:opacity-100 group-focus-within:opacity-100">
          {/* A deleted file offers no download (UX r3 B2 on #405, Standards s.5). */}
          {!deleted && (
            <Tip content={t_i18n("Download")}>
              <Button
                variant="ghost"
                size="sm"
                className="h-7 w-7 p-0"
                onClick={download}
                aria-label={t_i18n("Download")}
              >
                <Download className="h-3.5 w-3.5" />
              </Button>
            </Tip>
          )}
          {contextHref && (
            <Tip content={file.context === "run" ? t_i18n("Open run") : t_i18n("Open conversation")}>
              <Button
                variant="ghost"
                size="sm"
                className="h-7 w-7 p-0"
                onClick={() => navigate(contextHref)}
                aria-label={file.context === "run" ? t_i18n("Open run") : t_i18n("Open conversation")}
              >
                <ExternalLink className="h-3.5 w-3.5" />
              </Button>
            </Tip>
          )}
        </div>
        <OverflowMenu items={items} label={t_i18n("File actions")} />
      </div>
    </li>
  );
}

function GroupCard({ group, onDelete }: { group: FileGroup; onDelete: (file: RepositoryFile) => void }) {
  const { t_i18n } = useFormatter();
  const navigate = useNavigate();
  const header = (
    <>
      <ContextIcon context={group.context} className="h-4 w-4 shrink-0 text-accent" />
      <span className="truncate content-base font-medium">{group.title}</span>
      {group.subtitle && (
        <span className="truncate content-compact text-default-secondary">{group.subtitle}</span>
      )}
      <span className="ml-auto shrink-0 rounded-sm bg-elevation-hover px-2 py-0.5 content-caption font-medium tabular-nums text-default-secondary">
        {group.files.length}
      </span>
    </>
  );
  return (
    <Card className="border-elevation-subtle">
      <CardContent className="p-0">
        {group.href ? (
          <button
            type="button"
            onClick={() => navigate(group.href!)}
            className="flex w-full items-center gap-2 rounded-t-sm border-b border-elevation-subtle-soft px-4 py-2.5 text-left transition-colors hover:bg-elevation-hover"
            aria-label={t_i18n("Open {name}", { name: group.title })}
          >
            {header}
          </button>
        ) : (
          <div className="flex items-center gap-2 border-b border-elevation-subtle-soft px-4 py-2.5">{header}</div>
        )}
        <ul className="divide-y divide-[var(--border-elevation-subtle)]">
          {group.files.map((file) => (
            <FileRow key={file.id} file={file} showContext={false} onDelete={onDelete} />
          ))}
        </ul>
      </CardContent>
    </Card>
  );
}

export default function Files() {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const [searchParams, setSearchParams] = useSearchParams();
  const [confirmEl, confirm] = useConfirm();

  /* URL is the single source of truth for every filter; the search box keeps
     a local echo so typing is not throttled by the URL round-trip. */
  const urlSearch = searchParams.get(PARAM_SEARCH) ?? "";
  const [searchDraft, setSearchDraft] = useState(urlSearch);
  const debouncedSearch = useDebounce(searchDraft, 250);

  const context = pick(searchParams.get(PARAM_CONTEXT), CONTEXT_VALUES, "all");
  const origin = pick(searchParams.get(PARAM_ORIGIN), ORIGIN_VALUES, "all");
  const status = pick(searchParams.get(PARAM_STATUS), STATUS_VALUES, "all");
  const conversationId = searchParams.get(PARAM_CONVERSATION) ?? undefined;
  const runId = searchParams.get(PARAM_RUN) ?? undefined;
  // Each segment's count under the other filters, the numbers on the filter.
  const segmentCounts = useSegmentCounts(FILES_KEY, "/files", {
    search: debouncedSearch || undefined,
    context: context === "all" ? undefined : context,
    origin: origin === "all" ? undefined : origin,
    conversation_id: conversationId,
    assignment_run_id: runId,
  });
  // Mine | Company | Shared | All (`?vis=`); with none named, the first segment
  // holding a file once the counts are in. The [DEPRECATED] `?owner=` is
  // read once as an alias (`?owner=shared`, "Shared with me", opens on All) and
  // dropped, with the page number, on the next change.
  const [visibility, setVisibility, landing] = useVisibilityParam(undefined, undefined, { counts: segmentCounts });
  const page = Math.max(0, Number(searchParams.get(PARAM_PAGE) ?? "0") || 0);
  const flat = searchParams.get(PARAM_FLAT) === "1";

  const setParam = useCallback(
    (key: string, value: string | null, { keepPage = false }: { keepPage?: boolean } = {}) => {
      setSearchParams(
        (prev) => {
          const next = new URLSearchParams(prev);
          if (value === null || value === "" || value === "all") next.delete(key);
          else next.set(key, value);
          if (!keepPage) next.delete(PARAM_PAGE);
          return next;
        },
        { replace: true },
      );
    },
    [setSearchParams],
  );

  /* Commit the debounced draft to the URL (and reset the page) once it settles. */
  useEffect(() => {
    if (debouncedSearch !== urlSearch) setParam(PARAM_SEARCH, debouncedSearch);
  }, [debouncedSearch, urlSearch, setParam]);

  const query = useMemo<RepositoryFilesQuery>(
    () => ({
      limit: PAGE_SIZE,
      offset: page * PAGE_SIZE,
      search: debouncedSearch || undefined,
      context: context === "all" ? undefined : context,
      origin: origin === "all" ? undefined : origin,
      vis: visibilityQuery(visibility),
      conversation_id: conversationId,
      assignment_run_id: runId,
      status: status === "all" ? undefined : status,
    }),
    [page, debouncedSearch, context, origin, visibility, conversationId, runId, status],
  );
  const { data, isLoading, isFetching } = useFiles(query, { enabled: !landing });
  // The same filters over every segment, asked on an empty Mine only: the hint reads it.
  const { data: allSegments } = useFiles(
    { ...query, vis: undefined, limit: 1, offset: 0 },
    { enabled: visibility === "mine" && data?.total === 0 },
  );
  const deleteFile = useDeleteFile();

  const files = useMemo(() => data?.files ?? [], [data]);
  const total = data?.total ?? 0;
  const groups = useMemo(() => groupFiles(files, t_i18n), [files, t_i18n]);
  // The filters besides the segment: with none, an empty page is an empty repository.
  // Context, Origin and State: the count badge of the Filters pill.
  const secondaryFilterCount = [context, origin, status].filter((v) => v !== "all").length;
  const filtersHeadingId = useId();
  const otherFiltersActive =
    Boolean(debouncedSearch) || context !== "all" || origin !== "all" || status !== "all"
    || Boolean(conversationId) || Boolean(runId);
  const repositoryEmpty = total === 0 && !otherFiltersActive
    && (visibility === "all" || (visibility === "mine" && allSegments?.total === 0));

  /* The scope chip names what a deep link narrowed the page to. The first
     file on the page carries the title; the id is the fallback while loading. */
  const scopeLabel = conversationId
    ? (files.find((f) => f.conversation_id === conversationId)?.conversation_title
      ?? t_i18n("Untitled conversation"))
    : runId
      ? (files.find((f) => f.assignment_run_id === runId)?.run_label ?? t_i18n("Run"))
      : null;

  const clearScope = () => {
    setSearchParams(
      (prev) => {
        const next = new URLSearchParams(prev);
        next.delete(PARAM_CONVERSATION);
        next.delete(PARAM_RUN);
        next.delete(PARAM_PAGE);
        return next;
      },
      { replace: true },
    );
  };

  const handleDelete = async (file: RepositoryFile) => {
    const ok = await confirm({
      title: t_i18n("Delete this file?"),
      description: t_i18n(
        "\"{name}\" is removed from storage for everyone who could read it. This cannot be undone.",
        { name: file.filename },
      ),
      confirmLabel: t_i18n("Delete"),
      variant: "danger",
    });
    if (!ok) return;
    try {
      await deleteFile.mutateAsync(file.id);
      toast({ title: t_i18n("File deleted"), variant: "success" });
    } catch (e) {
      toast({
        title: t_i18n("Could not delete file"),
        description: e instanceof Error ? e.message : undefined,
        variant: "error",
      });
    }
  };

  /* `SegmentedFilter` translates labels itself — pass the source strings. */
  const contextOptions: SegmentedFilterOption<ContextFilter>[] = [
    { key: "all", label: "Everywhere" },
    { key: "conversation", label: "Conversations" },
    { key: "run", label: "Runs" },
    { key: "other", label: "Other" },
  ];
  const originOptions: SegmentedFilterOption<OriginFilter>[] = [
    { key: "all", label: "Any origin" },
    { key: "uploaded", label: "Uploaded" },
    { key: "generated", label: "Generated" },
  ];
  const statusOptions: SegmentedFilterOption<StatusFilter>[] = [
    { key: "all", label: "Any state" },
    { key: "live", label: "Available" },
    { key: "deleted", label: "Deleted" },
  ];

  return (
    <div className="space-y-6">
      <div className="flex items-start justify-between">
        <div>
          <h1 className="text-3xl font-bold">{t_i18n("Files")}</h1>
          <p className="mt-2 text-default-secondary">
            {t_i18n("Every file you and your agents exchanged: uploads and outputs, across conversations and runs.")}
          </p>
        </div>
      </div>

      {/* The toolbar stays mounted while a new filter combination loads, so the Filters pill keeps
          its panel open and the focus (UX r3 B1 on #405): only the list area shows the skeleton. */}
      {landing ? (
        <ListSkeleton label={t_i18n("Loading...")} rows={3} variant="card" data-testid="list-skeleton" />
      ) : (
        <>
          <div className="flex flex-wrap items-center gap-3">
            <SearchInput toolbar placeholder={t_i18n("Search files...")} value={searchDraft} onChange={setSearchDraft} />
            {/* Context, Origin and State sit in one Filters pill so the toolbar keeps one row at 1080
                (UX r2 B1 on #405, Standards s.7). */}
            <FiltersPill label={t_i18n("Filters")} count={secondaryFilterCount} testId="files-filters">
              <div role="group" aria-labelledby={`${filtersHeadingId}-context`} className="space-y-1">
                <Label id={`${filtersHeadingId}-context`}>{t_i18n("Context")}</Label>
                <SegmentedFilter
                  value={context}
                  onChange={(v) => setParam(PARAM_CONTEXT, v)}
                  options={contextOptions}
                  ariaLabel={t_i18n("Where the file lives")}
                />
              </div>
              <div role="group" aria-labelledby={`${filtersHeadingId}-origin`} className="space-y-1">
                <Label id={`${filtersHeadingId}-origin`}>{t_i18n("Origin")}</Label>
                <SegmentedFilter
                  value={origin}
                  onChange={(v) => setParam(PARAM_ORIGIN, v)}
                  options={originOptions}
                  ariaLabel={t_i18n("How the file got there")}
                />
              </div>
              <div role="group" aria-labelledby={`${filtersHeadingId}-state`} className="space-y-1">
                <Label id={`${filtersHeadingId}-state`}>{t_i18n("State")}</Label>
                <SegmentedFilter
                  value={status}
                  onChange={(v) => setParam(PARAM_STATUS, v)}
                  options={statusOptions}
                  ariaLabel={t_i18n("Whether the file's content is kept")}
                />
              </div>
            </FiltersPill>
            <VisibilityFilter value={visibility} onChange={setVisibility} counts={segmentCounts} />
            <div className="ml-auto flex items-center gap-3">
              <label className="flex items-center gap-2 content-compact text-default-secondary">
                <Switch
                  checked={!flat}
                  onCheckedChange={(checked) => setParam(PARAM_FLAT, checked ? null : "1", { keepPage: true })}
                  aria-label={t_i18n("Group by conversation or run")}
                />
                {t_i18n("Group")}
              </label>
              <TotalChip total={total} className={isFetching ? "opacity-60" : undefined} />
            </div>
          </div>

          {scopeLabel && (
            <div className="flex items-center gap-2 content-compact text-default-secondary">
              <span>{t_i18n("Showing files of")}</span>
              <Badge variant="cyan" className="gap-1 pr-1">
                <ContextIcon context={conversationId ? "conversation" : "run"} className="h-3 w-3" />
                <span className="max-w-[24rem] truncate">{scopeLabel}</span>
                <button
                  type="button"
                  onClick={clearScope}
                  className="ml-0.5 rounded-sm p-0.5 hover:bg-elevation-hover"
                  aria-label={t_i18n("Show all files")}
                >
                  <X className="h-3 w-3" />
                </button>
              </Badge>
            </div>
          )}

          {isLoading ? (
            <ListSkeleton label={t_i18n("Loading...")} rows={3} variant="card" data-testid="list-skeleton" />
          ) : repositoryEmpty ? (
            <EmptyState
              icon={FolderOpen}
              title="No files yet"
              description="Files you upload in a conversation and files your agents generate (reports, exports, decks) are collected here, wherever they were made."
            />
          ) : files.length === 0 ? (
            <VisibilityEmptyHint
              value={visibility}
              counts={data && allSegments ? { mine: data.total, all: allSegments.total } : undefined}
              onShowAll={() => setVisibility("all")}
            >
              <EmptyState icon={FolderOpen} title="No files match your filters." />
            </VisibilityEmptyHint>
          ) : flat ? (
            <Card className="border-elevation-subtle">
              <CardContent className="p-0">
                <ul className="divide-y divide-[var(--border-elevation-subtle)]">
                  {files.map((file) => (
                    <FileRow key={file.id} file={file} showContext onDelete={handleDelete} />
                  ))}
                </ul>
              </CardContent>
            </Card>
          ) : (
            <div className="space-y-4">
              {groups.map((group) => (
                <GroupCard key={group.key} group={group} onDelete={handleDelete} />
              ))}
            </div>
          )}

          {total > PAGE_SIZE && (
            <Pagination
              page={page}
              pageSize={PAGE_SIZE}
              total={total}
              onChange={(p) => setParam(PARAM_PAGE, p > 0 ? String(p) : null, { keepPage: true })}
            />
          )}
        </>
      )}
      {confirmEl}
    </div>
  );
}
