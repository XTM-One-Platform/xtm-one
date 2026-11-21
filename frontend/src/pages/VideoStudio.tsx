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
 * Video Studio - `/videos`: the library around the editor.
 *
 * Both libraries come a page at a time, searched and filtered by the server:
 * AI generation fills them fast, and every row stays reachable. The
 * search, the filters and the page live in the URL (`useLibraryParams`, shared
 * with the presentation library), and the page bar sits above and below the
 * grid, as on the other paginated pages.
 *
 * - Videos: every video the user can see, with its newest render and the
 *   template it wears, searchable and filtered like the deck library (mine,
 *   shared with me, company). A card opens the editor (`/videos/:id`,
 *   `pages/VideoEditor.tsx`): the assistant beside the stage, the timeline
 *   and the post-production in the person's hands - or, for a reader, offers to
 *   view the video (`videoOpenAction`): the same page plays its render.
 * A rendered video's Play opens its current render in the theater.
 * Its menu plays and shares the video,
 *   copies its link, downloads its current render, duplicates or deletes it.
 * "New video" starts one with the assistant, blank, or from an AI
 *   shot.
 * - Media: what videos are made of - the recordings of the platform's
 *   browser and the AI shots - with their media retention: when the files
 *   expire, the videos that use them, and the switch that keeps them. A
 *   ready one plays in the theater. Filtered by kind, by use (in a video,
 *   anyone's, or in none) and by when they were made, and deleted in bulk:
 * picked card by card, or every one the filters match - `Select
 *   all unused` selects every medium no video uses in one click.
 * Recordings sign in with a browser profile, managed on its own page
 * (`/connections`, Tools & Integrations): every agent browses with them.
 */
import { useId, useMemo, useRef, useState } from "react";
import { Link, useLocation, useNavigate } from "react-router-dom";
import {
  Clapperboard,
  Copy,
  Eye,
  Globe,
  KeyRound,
  LayoutTemplate,
  Link2,
  ListChecks,
  MonitorPlay,
  Palette,
  Pencil,
  Play,
  Share2,
  SlidersHorizontal,
  Sparkles,
  Trash2,
  Wand2,
} from "lucide-react";

import { RetentionBadge } from "@/components/video-studio/editor/MediaPanel";
import { JobProgress, JobStatusBadge } from "@/components/video-studio/JobStatusBadge";
import { NewVideoDialog } from "@/components/video-studio/NewVideoDialog";
import { VideoPoster } from "@/components/video-studio/VideoPlayer";
import { VideoShareDialog } from "@/components/video-studio/VideoShareDialog";
import { VideoTheater } from "@/components/video-studio/VideoTheater";
import { renderDownloadItems, useRenderDownload } from "@/components/video-studio/video-downloads";
import { videoLink } from "@/components/video-studio/video-links";
import {
  STUDIO_TAB_LABELS,
  type StudioTab,
  VIDEO_OPEN_LABELS,
  formatDuration,
  isUnfinished,
  renderCaption,
  videoOpenAction,
} from "@/components/video-studio/video-format";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { CompanyManagedIcon } from "@/components/ui/company-managed-badge";
import { useConfirm } from "@/components/ui/confirm-dialog";
import {
  DataTable,
  type DataTableColumn,
  type DataTableFilter,
  type DataTableState,
  type SortDirection,
  TOOLBAR_CONTROL_HEIGHT,
} from "@/components/ui/data-table";
import { DateRangeFilter } from "@/components/ui/date-range-filter";
import { EmptyState } from "@/components/ui/empty-state";
import { FilterClearButton, FilterPillPanel, FilterPillTrigger, isInsideFloatingPicker, useFilterPopover } from "@/components/ui/filter-pill";
import { InfoTip } from "@/components/ui/info-tip";
import { AgenticEEBadgeForButton, AgenticGateDialog } from "@/components/ui/license-gate-dialog";
import { SelectionBox } from "@/components/ui/list-control-bar";
import { ListPageHeader } from "@/components/ui/list-page-header";
import { OverflowMenu, type OverflowMenuItem } from "@/components/ui/overflow-menu";
import { SegmentedFilter } from "@/components/ui/segmented-filter";
import { SharedIcon } from "@/components/ui/shared-badge";
import { Switch } from "@/components/ui/switch";
import { useToast } from "@/components/ui/toast";
import { IconMark } from "@/components/ui/icon-mark";
import { Tip } from "@/components/ui/tooltip";
import { TruncatedText } from "@/components/ui/truncated-text";
import { VisibilityEmptyHint } from "@/components/ui/visibility-filter";
import { useSegmentCounts } from "@/hooks/api/useSegmentCounts";
import {
  PROJECT_PAGES_KEY,
  useBulkDeleteVideoAssets,
  useDeleteVideoClip,
  useDeleteVideoProject,
  useDeleteVideoTake,
  useDuplicateVideoProject,
  useUpdateVideoAsset,
  useVideoAssets,
  useVideoProjects,
} from "@/hooks/api/useVideoStudioApi";
import { useAuth } from "@/hooks/useAuth";
import { useViewMode } from "@/hooks/useViewMode";
import { useVisibilityParam } from "@/hooks/useVisibilityParam";
import { LIBRARY_PARAM_SEARCH, readChoice, useClampPage, useLibraryParams } from "@/hooks/useLibraryParams";
import { useAgenticAIEnabled } from "@/hooks/usePlatformConfig";
import { useFormatter } from "@/i18n";
import { type DateRange, hasDateRange, resolveDateRange } from "@/lib/date-range";
import { cn, isEntityShared } from "@/lib/utils";
import type {
  VideoAssetFilters,
  VideoAssetKind,
  VideoAssetSortKey,
  VideoAssetUsage,
  VideoAssetUse,
  VideoClip,
  VideoMediaInUse,
  VideoProject,
  VideoProjectRenderFilter,
  VideoProjectSortKey,
  VideoTake,
} from "@/types";
import { formatTimeAgo } from "@/lib/relative-time";

const TABS: StudioTab[] = ["videos", "media"];

/** The address of a studio tab: the library is `/videos`, the media `/videos/media`. */
function studioTabPath(tab: StudioTab): string {
  return tab === "videos" ? "/videos" : `/videos/${tab}`;
}
/** The media filter, by key. @i18n-keys */
const MEDIA_FILTER_LABELS = { all: "All", recordings: "Recordings", shots: "AI shots" } as const;
type MediaFilter = keyof typeof MEDIA_FILTER_LABELS;
const MEDIA_FILTERS = Object.keys(MEDIA_FILTER_LABELS) as MediaFilter[];
/** What each media filter asks the server for. */
const MEDIA_KINDS: Record<MediaFilter, VideoAssetKind> = { all: "all", recordings: "take", shots: "clip" };
/** The use filter, by key: whether any video uses the medium, anyone's. @i18n-keys */
const MEDIA_USAGE_LABELS = { all: "All", used: "In a video", unused: "In no video" } as const;
const MEDIA_USAGES = Object.keys(MEDIA_USAGE_LABELS) as VideoAssetUsage[];

/** Agents produce videos, recordings and shots by the dozen: the libraries are dense grids of small cards. */
const VIDEO_GRID = "grid gap-3 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 2xl:grid-cols-5";
const MEDIA_GRID = "grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-4 xl:grid-cols-5 2xl:grid-cols-6";
const PARAM_USAGE = "usage";
/** The media's time range in the URL, the audit logs' names. */
const PARAM_RANGE = { preset: "dp", from: "df", to: "dt" } as const;

function errorText(err: unknown): string | undefined {
  return err instanceof Error && err.message ? err.message : undefined;
}

/** The refusal of deleting a medium videos use (409 `media_in_use`), else `null`. */
function mediaInUse(err: unknown): VideoMediaInUse | null {
  const detail = (err as { detail?: unknown } | null)?.detail;
  return detail && typeof detail === "object" && (detail as { code?: unknown }).code === "media_in_use"
    ? (detail as VideoMediaInUse)
    : null;
}

function DurationChip({ ms }: { ms: number | null | undefined }) {
  if (!ms) return null;
  return (
    <span className="absolute bottom-1.5 right-1.5 rounded-sm bg-elevation-default px-1 py-0.5 content-caption font-medium tabular-nums">
      {formatDuration(ms)}
    </span>
  );
}

// ---------------------------------------------------------------------------
// Videos
// ---------------------------------------------------------------------------

function TemplateBadge({ video }: { video: VideoProject }) {
  const { t_i18n } = useFormatter();
  return (
    <Badge variant="outline" className="gap-1 content-caption">
      {video.template ? <LayoutTemplate className="h-2.5 w-2.5" /> : <Palette className="h-2.5 w-2.5" />}
      {video.template ? video.template.name : t_i18n("Free style")}
    </Badge>
  );
}

/** A public link plays the video for anyone, without an account. */
function PublicLinkIcon() {
  const { t_i18n } = useFormatter();
  const text = t_i18n("Public link: anyone with the link can watch it");
  return (
    <IconMark label={text} testId="videos.card.publicLink">
      <Globe className="h-3.5 w-3.5 shrink-0 text-accent" aria-hidden="true" />
    </IconMark>
  );
}

/** Shared with the reader without an edit grant. */
function ViewOnlyIcon() {
  const { t_i18n } = useFormatter();
  const text = t_i18n("View only");
  return (
    <IconMark label={text}>
      <Eye className="h-3.5 w-3.5 shrink-0 text-default-secondary" aria-hidden="true" />
    </IconMark>
  );
}

interface VideoHandlers {
  onPlay: () => void;
  onShare: () => void;
  onDuplicate: () => void;
  onDelete: () => void;
}

/** What a video offers, the same on its card and on its row: play, open, share, link, downloads, duplicate, delete. */
function useVideoMenu(video: VideoProject, { onPlay, onShare, onDuplicate, onDelete }: VideoHandlers): OverflowMenuItem[] {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const navigate = useNavigate();
  const { download } = useRenderDownload();
  const ready = video.ready_render;
  const playable = ready?.status === "ready" && !!ready.file_id;
  const opens = videoOpenAction(video);
  const OpenIcon = opens === "edit" ? Pencil : Eye;
  const copyLink = () =>
    void navigator.clipboard
      ?.writeText(videoLink(video.id))
      .then(() => toast({ title: t_i18n("Link copied"), description: t_i18n("It opens the video for the people it is shared with."), variant: "success" }))
      .catch(() => toast({ title: t_i18n("The link was not copied"), variant: "error" }));
  const items: OverflowMenuItem[] = [
    ...(playable ? [{ id: "play", label: t_i18n("Play"), icon: Play, onSelect: onPlay }] : []),
    { id: "open", label: t_i18n(VIDEO_OPEN_LABELS[opens]), icon: OpenIcon, onSelect: () => navigate(`/videos/${video.id}`) },
    ...(video.can_manage ? [{ id: "share", label: t_i18n("Share"), icon: Share2, onSelect: onShare }] : []),
    { id: "copy-link", label: t_i18n("Copy link"), icon: Link2, onSelect: copyLink },
  ];
  if (ready?.status === "ready") {
    const downloads = renderDownloadItems(ready, download, t_i18n);
    if (downloads[0]) downloads[0] = { ...downloads[0], separatorBefore: true };
    items.push(...downloads);
  }
  items.push({ id: "duplicate", label: t_i18n("Duplicate"), icon: Copy, onSelect: onDuplicate, separatorBefore: true });
  if (video.can_manage) {
    items.push({ id: "delete", label: t_i18n("Delete"), icon: Trash2, onSelect: onDelete, destructive: true });
  }
  return items;
}

/** The video's public link, sharing, company management and view-only marks, as icons. */
function VideoStatusIcons({ video, userId }: { video: VideoProject; userId?: string }) {
  return (
    <div className="flex shrink-0 items-center gap-1.5">
      {video.has_public_link && <PublicLinkIcon />}
      {isEntityShared(video, userId) && <SharedIcon ownerName={video.owner_name} />}
      {video.is_company_managed && <CompanyManagedIcon />}
      {!video.can_manage && !video.is_company_managed && <ViewOnlyIcon />}
    </div>
  );
}

function VideoProjectCard({ video, userId, handlers }: { video: VideoProject; userId?: string; handlers: VideoHandlers }) {
  const { t_i18n, intl } = useFormatter();
  const latest = video.latest_render;
  const ready = video.ready_render;
  const playable = ready?.status === "ready" && !!ready.file_id;
  const editorUrl = `/videos/${video.id}`;
  const opens = videoOpenAction(video);
  const OpenIcon = opens === "edit" ? Pencil : Eye;
  const items = useVideoMenu(video, handlers);
  return (
    <Card className="group h-full overflow-hidden transition-colors hover:border-accent/30" data-testid="videos.card">
      <div className="relative">
        <Link
          to={editorUrl}
          className="relative block"
          aria-label={opens === "edit" ? t_i18n("Open {title} in the editor", { title: video.title }) : t_i18n("View {title}", { title: video.title })}
        >
          {ready?.poster_file_id ? (
            <VideoPoster kind="render" id={ready.id} file={ready.poster_file_id} alt={video.title} className="rounded-none" />
          ) : (
            <div className="flex aspect-video w-full items-center justify-center bg-elevation-hover">
              <Clapperboard className="h-6 w-6 text-default-secondary" />
            </div>
          )}
          <DurationChip ms={ready?.duration_ms ?? video.estimated_duration_ms} />
          <span className="absolute inset-0 flex items-center justify-center bg-[color-mix(in_srgb,var(--bg-elevation-default)_50%,transparent)] opacity-0 transition-opacity group-hover:opacity-100">
            <span className="inline-flex items-center gap-1.5 rounded-full bg-elevation-default px-3 py-1.5 content-compact font-medium">
              <OpenIcon className="h-3.5 w-3.5 text-accent" /> {t_i18n(VIDEO_OPEN_LABELS[opens])}
            </span>
          </span>
        </Link>
        {playable && (
          <Tip content={t_i18n("Play it full screen")}>
            <Button
              variant="ghost"
              size="sm"
              onClick={handlers.onPlay}
              aria-label={t_i18n("Play {title}", { title: video.title })}
              className="absolute bottom-1.5 left-1.5 h-8 w-8 rounded-full bg-scrim px-0 text-on-scrim backdrop-blur-sm hover:bg-scrim-strong hover:text-accent"
              data-testid="videos.card.play"
            >
              <Play className="h-4 w-4 translate-x-px" />
            </Button>
          </Tip>
        )}
      </div>
      <CardContent className="space-y-1.5 p-3">
        <div className="flex items-start gap-1.5">
          <Link to={editorUrl} className="line-clamp-2 flex-1 content-base font-semibold leading-snug hover:text-accent">
            {video.title}
          </Link>
          <div className="pt-0.5">
            <VideoStatusIcons video={video} userId={userId} />
          </div>
          <OverflowMenu items={items} label={t_i18n("Video actions")} className="-mr-1 -mt-1 h-7 w-7 shrink-0" />
        </div>
        {video.owner_name && video.user_id !== userId && (
          <p className="truncate content-caption text-default-secondary">{t_i18n("By {name}", { name: video.owner_name })}</p>
        )}
        <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-[11px] text-default-secondary">
          <TemplateBadge video={video} />
          <span>{t_i18n("{count} scenes", { count: video.scene_count })}</span>
          {ready?.width ? (
            <span className="tabular-nums">
              {ready.width}x{ready.height} - {ready.fps} fps
            </span>
          ) : null}
          {video.updated_at && <span>{formatTimeAgo(intl, video.updated_at)}</span>}
        </div>
        {latest && (isUnfinished(latest.status) || latest.status === "failed") ? (
          <div className="space-y-1.5">
            <JobStatusBadge status={latest.status} progress={latest.progress} />
            {isUnfinished(latest.status) && <JobProgress progress={latest.progress} stage={latest.stage} />}
          </div>
        ) : !latest ? (
          <Badge variant="outline">{t_i18n("Not rendered yet")}</Badge>
        ) : null}
      </CardContent>
    </Card>
  );
}

/** The row's menu: the card's actions, one 28 px button (a hook per row, so its own component). */
function VideoRowActions({ video, handlers }: { video: VideoProject; handlers: VideoHandlers }) {
  const { t_i18n } = useFormatter();
  const items = useVideoMenu(video, handlers);
  return <OverflowMenu items={items} label={t_i18n("Video actions")} className="h-7 w-7" />;
}

/** The newest render's state, as on the card: running or failed, else not rendered yet, else its length. */
function RenderCell({ video }: { video: VideoProject }) {
  const { t_i18n } = useFormatter();
  const latest = video.latest_render;
  if (latest && (isUnfinished(latest.status) || latest.status === "failed")) {
    return <JobStatusBadge status={latest.status} progress={latest.progress} />;
  }
  if (!latest) return <span className="text-default-secondary">{t_i18n("Not rendered yet")}</span>;
  const ms = video.ready_render?.duration_ms ?? video.estimated_duration_ms;
  return <span className="tabular-nums text-default-secondary">{ms ? formatDuration(ms) : "-"}</span>;
}

/** Fixed column widths: the Name column takes the rest and truncates, so the row menu stays on screen at 1080. */
const VIDEO_COLUMN_WIDTHS = {
  scene_count: 104,
  template: 200,
  render: 136,
  sharing: 112,
  updated_at: 136,
  created_at: 128,
} as const;
/** The menu: one 28 px button and the cell padding. */
const VIDEO_ROW_ACTIONS_WIDTH = 56;

/** The server's orders (`GET /video-studio/projects?sort=`); each is the key of its column. */
const VIDEO_SORT_KEYS: VideoProjectSortKey[] = ["updated_at", "created_at", "title", "scene_count"];
const DIRECTIONS: SortDirection[] = ["asc", "desc"];
/** The last changed first: the video just edited is the one looked for. */
const DEFAULT_VIDEO_SORT: VideoProjectSortKey = "updated_at";
const DEFAULT_DIR: SortDirection = "desc";
/** A size of the table's page size select (PAGE_SIZES in data-table.tsx). */
const DEFAULT_VIDEO_PAGE_SIZE = 25;
const PAGE_SIZE_CHOICES = ["10", "25", "50", "100"] as const;
const PARAM_SORT = "sort";
const PARAM_DIR = "dir";
const PARAM_LIMIT = "limit";
/** The Render filter (`GET /video-studio/projects?render=`): with a ready render or not. */
const PARAM_RENDER = "render";
const RENDER_STATES: VideoProjectRenderFilter[] = ["rendered", "not_rendered"];
const RENDER_STATE_LABEL: Record<VideoProjectRenderFilter, string> = {
  rendered: "Rendered",
  // A video whose renders all failed or still run has no ready render either: the label says what the filter keeps.
  not_rendered: "No ready render",
};

/** The library's columns: the video (title, owner), its scenes, design, render, sharing and dates. */
function useVideoColumns(userId: string | undefined): DataTableColumn<VideoProject>[] {
  const { t_i18n, intl } = useFormatter();
  return useMemo<DataTableColumn<VideoProject>[]>(
    () => [
      {
        key: "title",
        header: "Name",
        sortValue: (video) => video.title.toLowerCase(),
        firstSort: "asc",
        csv: (video) => video.title,
        cell: (video) => (
          <div className="flex min-w-0 items-center gap-2">
            <Clapperboard className="h-4 w-4 shrink-0 text-accent" aria-hidden="true" />
            <div className="flex min-w-0 flex-1 flex-col">
              <TruncatedText text={video.title} className="font-medium" />
              <TruncatedText
                text={
                  video.owner_name && video.user_id !== userId
                    ? t_i18n("By {name}", { name: video.owner_name })
                    : t_i18n("Yours")
                }
                className="content-caption text-default-secondary"
              />
            </div>
          </div>
        ),
      },
      {
        key: "scene_count",
        header: "Scenes",
        align: "right",
        hideable: true,
        width: VIDEO_COLUMN_WIDTHS.scene_count,
        sortValue: (video) => video.scene_count,
        csv: (video) => video.scene_count,
        cell: (video) => <span className="tabular-nums">{video.scene_count}</span>,
      },
      {
        key: "template",
        header: "Template",
        hideable: true,
        hideBelow: "4xl",
        width: VIDEO_COLUMN_WIDTHS.template,
        csv: (video) => video.template?.name ?? "",
        cell: (video) => <TruncatedText text={video.template ? video.template.name : t_i18n("Free style")} />,
      },
      {
        key: "render",
        header: "Render",
        hideable: true,
        width: VIDEO_COLUMN_WIDTHS.render,
        csv: (video) => video.latest_render?.status ?? "",
        cell: (video) => <RenderCell video={video} />,
      },
      {
        key: "sharing",
        header: "Sharing",
        hideable: true,
        width: VIDEO_COLUMN_WIDTHS.sharing,
        cell: (video) => <VideoStatusIcons video={video} userId={userId} />,
      },
      {
        key: "updated_at",
        header: "Updated",
        width: VIDEO_COLUMN_WIDTHS.updated_at,
        sortValue: (video) => video.updated_at ?? "",
        firstSort: "desc",
        csv: (video) => video.updated_at ?? "",
        cell: (video) => (
          <span className="text-default-secondary">{video.updated_at ? formatTimeAgo(intl, video.updated_at) : "-"}</span>
        ),
      },
      {
        key: "created_at",
        header: "Created",
        hideable: true,
        defaultHidden: true,
        width: VIDEO_COLUMN_WIDTHS.created_at,
        sortValue: (video) => video.created_at ?? "",
        firstSort: "desc",
        csv: (video) => video.created_at ?? "",
        cell: (video) => (
          <span className="text-default-secondary">{video.created_at ? formatTimeAgo(intl, video.created_at) : "-"}</span>
        ),
      },
    ],
    [t_i18n, intl, userId],
  );
}

function VideosTab({ onNew }: { onNew: () => void }) {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const navigate = useNavigate();
  const { user } = useAuth();
  const agenticAIEnabled = useAgenticAIEnabled();
  const [ConfirmEl, confirm] = useConfirm();
  const { searchDraft, setSearchDraft, search, page, params, update } = useLibraryParams();
  const sortKey = readChoice(params.get(PARAM_SORT), VIDEO_SORT_KEYS, DEFAULT_VIDEO_SORT);
  const sortDir = readChoice(params.get(PARAM_DIR), DIRECTIONS, DEFAULT_DIR);
  const pageSize = Number(readChoice(params.get(PARAM_LIMIT), PAGE_SIZE_CHOICES, String(DEFAULT_VIDEO_PAGE_SIZE)));
  const renderFilter = readChoice<VideoProjectRenderFilter | "all">(params.get(PARAM_RENDER), [...RENDER_STATES, "all"], "all");
  const [viewMode, setViewMode] = useViewMode("videos");
  // Each segment's count under the search and the Render filter, as on /presentations.
  const segmentCounts = useSegmentCounts(PROJECT_PAGES_KEY, "/video-studio/projects", {
    search: search || undefined,
    render: renderFilter === "all" ? undefined : renderFilter,
  });
  // The URL's segment (the former `?visibility=` read as a [DEPRECATED] alias),
  // else the first one holding a video once the counts are in.
  const [visibility, setVisibility, landing] = useVisibilityParam(undefined, undefined, { counts: segmentCounts });
  const isDefaultSort = sortKey === DEFAULT_VIDEO_SORT && sortDir === DEFAULT_DIR;
  // The server searches, filters, sorts and pages the whole library; the last changed first by default.
  // Read errors stay on the page: the table draws its error line with Retry.
  const { data, isLoading, isError, refetch } = useVideoProjects(
    {
      search: search || undefined,
      vis: visibility === "all" ? undefined : visibility,
      limit: pageSize,
      offset: page * pageSize,
      sort: isDefaultSort ? undefined : sortKey,
      direction: isDefaultSort ? undefined : sortDir,
      render: renderFilter === "all" ? undefined : renderFilter,
    },
    !landing,
    { inlineError: true },
  );
  const setPage = (next: number) => update({ page: next > 0 ? String(next) : null });
  useClampPage(page, pageSize, data?.total, setPage);
  const duplicate = useDuplicateVideoProject();
  const remove = useDeleteVideoProject();
  const [sharing, setSharing] = useState<VideoProject | null>(null);
  const [playing, setPlaying] = useState<VideoProject | null>(null);
  const videos = data?.items ?? [];
  const total = data?.total ?? 0;
  // The library's size across every segment: the empty state and the "n of N" total read it.
  const { data: everything } = useVideoProjects({ limit: 1, offset: 0 }, true, { inlineError: true });
  const allCount = everything?.total;
  const filtering = !!search || renderFilter !== "all";
  const columns = useVideoColumns(user?.id);
  const renderFilters = useMemo<DataTableFilter<VideoProject>[]>(
    () => [
      {
        key: PARAM_RENDER,
        label: "Render",
        options: RENDER_STATES.map((state) => ({ value: state, label: t_i18n(RENDER_STATE_LABEL[state]) })),
      },
    ],
    [t_i18n],
  );
  // The dialog follows the list: a save, or the assistant sharing it, refreshes what it shows.
  const sharedVideo = sharing ? (videos.find((v) => v.id === sharing.id) ?? sharing) : null;
  // The table's state is the tab's own URL keys (q, render, sort, dir, page, limit), so every link keeps working.
  const tableState: DataTableState = {
    search,
    sort: { key: sortKey, direction: sortDir },
    page,
    pageSize,
    filters: renderFilter === "all" ? {} : { [PARAM_RENDER]: [renderFilter] },
  };
  const onTableState = (next: DataTableState) => {
    const renders = next.filters[PARAM_RENDER] ?? [];
    const nextSort = next.sort ?? { key: DEFAULT_VIDEO_SORT, direction: DEFAULT_DIR };
    const isDefault = nextSort.key === DEFAULT_VIDEO_SORT && nextSort.direction === DEFAULT_DIR;
    update({
      // One state narrows; both (or none) is every video.
      [PARAM_RENDER]: renders.length === 1 ? renders[0] : null,
      [PARAM_SORT]: isDefault ? null : nextSort.key,
      [PARAM_DIR]: isDefault ? null : nextSort.direction,
      [PARAM_LIMIT]: next.pageSize === DEFAULT_VIDEO_PAGE_SIZE ? null : String(next.pageSize),
      page: next.page > 0 ? String(next.page) : null,
    });
  };

  const onDuplicate = async (video: VideoProject) => {
    try {
      const copy = await duplicate.mutateAsync(video.id);
      navigate(`/videos/${copy.id}`);
    } catch (err) {
      toast({ title: t_i18n("The video was not duplicated"), description: errorText(err), variant: "error" });
    }
  };
  const onDelete = async (video: VideoProject) => {
    const ok = await confirm({
      title: t_i18n("Delete this video?"),
      description: t_i18n('"{title}" and all its renders will be removed. Its recordings and clips are kept.', { title: video.title }),
      confirmLabel: t_i18n("Delete"),
      variant: "danger",
    });
    if (!ok) return;
    try {
      await remove.mutateAsync(video.id);
    } catch (err) {
      toast({ title: t_i18n("It was not deleted"), description: errorText(err), variant: "error" });
    }
  };
  const handlersFor = (video: VideoProject): VideoHandlers => ({
    onPlay: () => setPlaying(video),
    onShare: () => setSharing(video),
    onDuplicate: () => void onDuplicate(video),
    onDelete: () => void onDelete(video),
  });

  if (total === 0 && !filtering && allCount === 0 && !landing && !isLoading && !isError) {
    return (
      <EmptyState
        icon={Clapperboard}
        title="No videos yet"
        description="Start one with your assistant - it records the flow in the platform's browser, writes the narration and cuts the video - or start blank and edit every scene yourself, in the editor."
        actionLabel={agenticAIEnabled ? "New video" : undefined}
        actionIcon={Sparkles}
        actionVariant="default"
        onAction={agenticAIEnabled ? onNew : undefined}
      />
    );
  }
  return (
    <>
      {/* One records table (dev-docs/reference/data-tables.md): the server searches, sorts and
          pages; the grid of cards stays one toggle away and shares the toolbar and pager. */}
      <DataTable<VideoProject>
        rows={videos}
        columns={columns}
        rowKey={(video) => video.id}
        rowHref={(video) => `/videos/${video.id}`}
        rowTestId="video-row"
        rowLines={2}
        layout="fixed"
        rowActionsWidth={VIDEO_ROW_ACTIONS_WIDTH}
        server={{ total }}
        totalAll={allCount}
        state={tableState}
        onStateChange={onTableState}
        search={{ value: searchDraft, onChange: setSearchDraft, placeholder: t_i18n("Search videos") }}
        scope={{ value: visibility, onChange: setVisibility, counts: segmentCounts }}
        filters={renderFilters}
        exportName="videos"
        onClearAll={() => {
          setSearchDraft("");
          update({ [LIBRARY_PARAM_SEARCH]: null, [PARAM_RENDER]: null });
        }}
        view={{ value: viewMode, onChange: setViewMode }}
        gridClassName={VIDEO_GRID}
        renderGrid={
          viewMode === "grid"
            ? (rows) => (
                <div className={VIDEO_GRID}>
                  {rows.map((video) => (
                    <VideoProjectCard key={video.id} video={video} userId={user?.id} handlers={handlersFor(video)} />
                  ))}
                </div>
              )
            : undefined
        }
        rowActions={(video) => <VideoRowActions video={video} handlers={handlersFor(video)} />}
        storageKey="videos"
        isLoading={landing || isLoading || (videos.length === 0 && total > 0)}
        isError={isError}
        errorLabel={t_i18n("The videos could not be loaded.")}
        onRetry={() => void refetch()}
        emptyLabel={t_i18n("No videos match your filters.")}
        noResultsLabel={t_i18n("No videos match your filters.")}
        emptyContent={
          <VisibilityEmptyHint value={filtering ? "all" : visibility} counts={{ mine: total, all: allCount }} onShowAll={() => setVisibility("all")}>
            <p className="py-8 text-center content-base text-default-secondary">{t_i18n("No videos match your filters.")}</p>
          </VisibilityEmptyHint>
        }
      />
      {sharedVideo && (
        <VideoShareDialog
          video={sharedVideo}
          open
          onOpenChange={(open) => {
            if (!open) setSharing(null);
          }}
        />
      )}
      {playing?.ready_render && (
        <VideoTheater
          media={{ kind: "render", render: playing.ready_render }}
          title={playing.title}
          subtitle={renderCaption(playing.ready_render, t_i18n)}
          onClose={() => setPlaying(null)}
        />
      )}
      {ConfirmEl}
    </>
  );
}

// ---------------------------------------------------------------------------
// Media: recordings and AI shots
// ---------------------------------------------------------------------------

type MediaItem = { kind: "take"; item: VideoTake } | { kind: "clip"; item: VideoClip };

function UsedIn({ item }: { item: VideoTake | VideoClip }) {
  const { t_i18n } = useFormatter();
  const uses = item.used_in ?? [];
  if (uses.length === 0) {
    return (
      <span className="content-compact text-default-secondary">
        {item.in_use ? t_i18n("In a video you cannot open") : t_i18n("In no video")}
      </span>
    );
  }
  return (
    <span className="flex flex-wrap items-center gap-1 content-compact text-default-secondary">
      {t_i18n("Used in")}
      {uses.slice(0, 2).map((use) => (
        // The link is cut at 120 px: the tooltip carries the whole title (UX round 1 on #11198, B2).
        <Tip key={use.id} content={use.title}>
          <Link to={`/videos/${use.id}`} className="max-w-[120px] truncate text-accent hover:underline">
            {use.title}
          </Link>
        </Tip>
      ))}
      {uses.length > 2 && <span>{t_i18n("and {count} more", { count: uses.length - 2 })}</span>}
    </span>
  );
}

function MediaCard({
  media,
  selected,
  onToggleSelect,
  onPlay,
  onDelete,
}: {
  media: MediaItem;
  selected: boolean;
  onToggleSelect: () => void;
  onPlay: () => void;
  onDelete: () => void;
}) {
  const { t_i18n, intl } = useFormatter();
  const { toast } = useToast();
  const update = useUpdateVideoAsset();
  const { kind, item } = media;
  const ready = item.status === "ready" && !item.expired_at;
  const title = kind === "take" ? media.item.name : media.item.name || media.item.prompt;
  const Icon = kind === "take" ? MonitorPlay : Wand2;
  const setKeep = async (keep: boolean) => {
    try {
      await update.mutateAsync({ kind, id: item.id, keep });
    } catch (err) {
      toast({ title: t_i18n("The change was not saved"), description: errorText(err), variant: "error" });
    }
  };
  return (
    <Card className={cn("overflow-hidden transition-colors", selected && "border-accent/50")} data-testid="media.card">
      <button type="button" className="relative block w-full" disabled={!ready} onClick={onPlay} aria-label={t_i18n("Play")}>
        {item.poster_file_id ? (
          <VideoPoster kind={kind} id={item.id} file={item.poster_file_id} alt={title} className="rounded-none" />
        ) : (
          <div className="flex aspect-video w-full items-center justify-center bg-elevation-hover">
            <Icon className="h-6 w-6 text-default-secondary" />
          </div>
        )}
        <DurationChip ms={item.duration_ms} />
        <span className="absolute left-1.5 top-1.5 flex gap-1">
          <Badge variant="secondary" className="gap-1 content-caption">
            <Icon className="h-2.5 w-2.5" /> {kind === "take" ? t_i18n("Recording") : t_i18n("AI shot")}
          </Badge>
          {item.status !== "ready" && <JobStatusBadge status={item.status} progress={item.progress} />}
        </span>
      </button>
      <CardContent className="space-y-1.5 p-2.5">
        <div className="flex items-start gap-1.5">
          <SelectionBox
            checked={selected}
            onToggle={onToggleSelect}
            label={selected ? t_i18n("Deselect {title}", { title }) : t_i18n("Select {title}", { title })}
            className="mt-0.5"
          />
          <p className="line-clamp-2 flex-1 content-compact font-semibold leading-snug">{title}</p>
          <Tip content={t_i18n("Delete")}>
            <Button variant="ghost" size="icon" className="-mr-1 -mt-1 h-7 w-7 shrink-0" onClick={onDelete} aria-label={t_i18n("Delete")}>
              <Trash2 className="h-3.5 w-3.5" />
            </Button>
          </Tip>
        </div>
        <div className="flex flex-wrap items-center gap-1">
          <RetentionBadge item={item} />
          {item.created_at && <span className="text-[11px] text-default-secondary">{formatTimeAgo(intl, item.created_at)}</span>}
        </div>
        <UsedIn item={item} />
        {isUnfinished(item.status) && <JobProgress progress={item.progress} stage={kind === "take" ? media.item.stage : null} />}
        {item.status === "failed" && item.error && (
          <Tip content={item.error}>
            <p className="line-clamp-2 text-[11px] text-feedback-error-primary">{item.error}</p>
          </Tip>
        )}
        {!item.expired_at && item.status === "ready" && (
          <label className="flex items-center justify-between gap-2 pt-0.5 text-[11px] text-default-secondary">
            {t_i18n("Keep its media")}
            <Switch checked={item.keep} disabled={update.isPending} onCheckedChange={(value) => void setKeep(value)} aria-label={t_i18n("Keep its media")} />
          </label>
        )}
      </CardContent>
    </Card>
  );
}

/** A medium the person selected: what a bulk deletion names, and whether a video used it when it was listed. */
interface PickedMedia {
  kind: MediaItem["kind"];
  id: string;
  inUse: boolean;
}

const mediaKey = (media: MediaItem) => `${media.kind}:${media.item.id}`;
const pickedOf = (media: MediaItem): PickedMedia => ({ kind: media.kind, id: media.item.id, inUse: !!media.item.in_use });
/** The filters a selection belongs to: another set of filters starts it again. */
const filtersKey = (filters: VideoAssetFilters) =>
  [filters.kind, filters.search, filters.usage, filters.date_from, filters.date_to].map((value) => value ?? "").join("|");

/**
 * The "Filters" pill of the media toolbar: the use filter (in a video, in none)
 * and the time range behind one button, so the toolbar keeps one filter row at
 * 1024 too (#10215, UX r1 B1 on #10312). The badge counts what narrows the list;
 * the choices stay in the URL. The date pickers open in a portal, so a press in
 * their calendar is not a press outside the panel (`isInsideFloatingPicker`).
 */
function MediaFilters({
  usage,
  onUsageChange,
  range,
  onRangeChange,
}: {
  usage: VideoAssetUsage;
  onUsageChange: (key: VideoAssetUsage) => void;
  range: DateRange;
  onRangeChange: (next: DateRange) => void;
}) {
  const { t_i18n } = useFormatter();
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);
  const panelId = useId();
  useFilterPopover(rootRef, open, setOpen, isInsideFloatingPicker);
  const count = (usage === "all" ? 0 : 1) + (hasDateRange(range) ? 1 : 0);
  return (
    <div ref={rootRef} className="relative shrink-0">
      <FilterPillTrigger
        icon={SlidersHorizontal}
        label={t_i18n("Filters")}
        open={open}
        count={count}
        aria-controls={panelId}
        onClick={() => setOpen((v) => !v)}
      />
      {open && (
        <FilterPillPanel id={panelId} role="group" aria-label={t_i18n("Filters")} className="w-auto space-y-3 p-3">
          <SegmentedFilter
            options={MEDIA_USAGES.map((key) => ({ key, label: MEDIA_USAGE_LABELS[key] }))}
            value={usage}
            onChange={onUsageChange}
            ariaLabel={t_i18n("Use in videos")}
          />
          <DateRangeFilter value={range} onChange={onRangeChange} stacked />
        </FilterPillPanel>
      )}
    </div>
  );
}

/** The keep switch of a medium (card and row): a kept medium never expires. */
function KeepSwitch({ media }: { media: MediaItem }) {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const update = useUpdateVideoAsset();
  const { kind, item } = media;
  if (item.expired_at || item.status !== "ready") return <span className="text-default-secondary">-</span>;
  const setKeep = async (keep: boolean) => {
    try {
      await update.mutateAsync({ kind, id: item.id, keep });
    } catch (err) {
      toast({ title: t_i18n("The change was not saved"), description: errorText(err), variant: "error" });
    }
  };
  return <Switch checked={item.keep} disabled={update.isPending} onCheckedChange={(value) => void setKeep(value)} aria-label={t_i18n("Keep its media")} />;
}

const mediaTitle = (media: MediaItem) => (media.kind === "take" ? media.item.name : media.item.name || media.item.prompt);

/** The server's orders (`GET /video-studio/assets?sort=`); each is the key of its column. */
const MEDIA_SORT_KEYS: VideoAssetSortKey[] = ["created_at", "title"];
const DEFAULT_MEDIA_SORT: VideoAssetSortKey = "created_at";
/** A size of the table's page size select (PAGE_SIZES in data-table.tsx): a few rows of cards. */
const DEFAULT_MEDIA_PAGE_SIZE = 50;
const PARAM_KIND = "kind";
/** The column widths of the media list (layout fixed); the title takes the rest. */
const MEDIA_COLUMN_WIDTHS = { kind: 120, status: 140, used_in: 220, keep: 72, duration: 96, created_at: 128 } as const;
const MEDIA_ROW_ACTIONS_WIDTH = 96;

/** The media list's columns: the title, its kind, state, the videos using it, keep, duration, date. */
function useMediaColumns(): DataTableColumn<MediaItem>[] {
  const { t_i18n, intl } = useFormatter();
  return useMemo<DataTableColumn<MediaItem>[]>(
    () => [
      {
        key: "title",
        header: "Name",
        sortValue: (media) => mediaTitle(media).toLowerCase(),
        firstSort: "asc",
        csv: (media) => mediaTitle(media),
        cell: (media) => {
          const Icon = media.kind === "take" ? MonitorPlay : Wand2;
          return (
            <div className="flex min-w-0 items-center gap-2">
              <Icon className="h-4 w-4 shrink-0 text-accent" aria-hidden="true" />
              <TruncatedText text={mediaTitle(media)} className="font-medium" />
            </div>
          );
        },
      },
      {
        key: "kind",
        header: "Kind",
        hideable: true,
        width: MEDIA_COLUMN_WIDTHS.kind,
        csv: (media) => media.kind,
        cell: (media) => <span>{media.kind === "take" ? t_i18n("Recording") : t_i18n("AI shot")}</span>,
      },
      {
        key: "status",
        header: "Status",
        hideable: true,
        width: MEDIA_COLUMN_WIDTHS.status,
        csv: (media) => media.item.status,
        cell: (media) =>
          media.item.status !== "ready" ? (
            <JobStatusBadge status={media.item.status} progress={media.item.progress} />
          ) : (
            <RetentionBadge item={media.item} />
          ),
      },
      {
        key: "used_in",
        header: "Used in",
        hideable: true,
        hideBelow: "4xl",
        width: MEDIA_COLUMN_WIDTHS.used_in,
        // Its links open a video, never the row's player (UX round 1 on #11198, B1).
        interactive: true,
        csv: (media) => (media.item.used_in ?? []).map((use) => use.title).join("; "),
        cell: (media) => <UsedIn item={media.item} />,
      },
      {
        key: "keep",
        header: "Keep",
        hideable: true,
        width: MEDIA_COLUMN_WIDTHS.keep,
        // A press on the switch toggles keep only, never opens the player (UX round 1 on #11198, B1).
        interactive: true,
        cell: (media) => <KeepSwitch media={media} />,
      },
      {
        key: "duration",
        header: "Duration",
        align: "right",
        hideable: true,
        defaultHidden: true,
        width: MEDIA_COLUMN_WIDTHS.duration,
        csv: (media) => media.item.duration_ms ?? "",
        cell: (media) => (
          <span className="tabular-nums text-default-secondary">
            {media.item.duration_ms ? `${Math.round(media.item.duration_ms / 1000)} s` : "-"}
          </span>
        ),
      },
      {
        key: "created_at",
        header: "Created",
        width: MEDIA_COLUMN_WIDTHS.created_at,
        sortValue: (media) => media.item.created_at ?? "",
        firstSort: "desc",
        csv: (media) => media.item.created_at ?? "",
        cell: (media) => (
          <span className="text-default-secondary">{media.item.created_at ? formatTimeAgo(intl, media.item.created_at) : "-"}</span>
        ),
      },
    ],
    [t_i18n, intl],
  );
}

function MediaTab() {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const [ConfirmEl, confirm] = useConfirm();
  const { searchDraft, setSearchDraft, search, filter, page, setPage, params, update } = useLibraryParams(
    PARAM_KIND,
    MEDIA_FILTERS,
    "all",
  );
  const usage = readChoice(params.get(PARAM_USAGE), MEDIA_USAGES, "all");
  const sortKey = readChoice(params.get(PARAM_SORT), MEDIA_SORT_KEYS, DEFAULT_MEDIA_SORT);
  const sortDir = readChoice(params.get(PARAM_DIR), DIRECTIONS, DEFAULT_DIR);
  const pageSize = Number(readChoice(params.get(PARAM_LIMIT), PAGE_SIZE_CHOICES, String(DEFAULT_MEDIA_PAGE_SIZE)));
  const isDefaultSort = sortKey === DEFAULT_MEDIA_SORT && sortDir === DEFAULT_DIR;
  const [viewMode, setViewMode] = useViewMode("media");
  const rangePreset = params.get(PARAM_RANGE.preset) ?? "";
  const rangeFrom = params.get(PARAM_RANGE.from) ?? "";
  const rangeTo = params.get(PARAM_RANGE.to) ?? "";
  // Resolved once per range: a preset counts back from the moment it was picked, not from every render or poll.
  const dates = useMemo(
    () => resolveDateRange({ preset: rangePreset, from: rangeFrom, to: rangeTo }),
    [rangePreset, rangeFrom, rangeTo],
  );
  const filters = useMemo<VideoAssetFilters>(
    () => ({
      kind: filter === "all" ? undefined : MEDIA_KINDS[filter],
      search: search || undefined,
      usage: usage === "all" ? undefined : usage,
      ...dates,
    }),
    [filter, search, usage, dates],
  );
  // The server searches, filters, sorts and pages both kinds together; newest first by default.
  // A failed or cancelled recording or shot made nothing and is never listed.
  const { data, isLoading, isError, refetch, isPlaceholderData } = useVideoAssets(
    {
      ...filters,
      sort: isDefaultSort ? undefined : sortKey,
      direction: isDefaultSort ? undefined : sortDir,
      limit: pageSize,
      offset: page * pageSize,
    },
    true,
    { inlineError: true },
  );
  useClampPage(page, pageSize, data?.total, setPage);
  const removeTake = useDeleteVideoTake();
  const removeClip = useDeleteVideoClip();
  const bulkDelete = useBulkDeleteVideoAssets();
  const [playing, setPlaying] = useState<MediaItem | null>(null);
  const items = useMemo<MediaItem[]>(
    () => (data?.items ?? []).map((asset) => (asset.kind === "take" ? { kind: "take", item: asset } : { kind: "clip", item: asset })),
    [data],
  );
  const total = data?.total ?? 0;
  const range: DateRange = { preset: rangePreset, from: rangeFrom, to: rangeTo };
  const outsideFilters = (usage === "all" ? 0 : 1) + (hasDateRange(range) ? 1 : 0);
  const filtering = !!search || filter !== "all" || outsideFilters > 0;
  const columns = useMediaColumns();
  const kindFilters = useMemo<DataTableFilter<MediaItem>[]>(
    () => [
      {
        key: PARAM_KIND,
        label: "Kind",
        options: MEDIA_FILTERS.filter((key) => key !== "all").map((key) => ({ value: key, label: t_i18n(MEDIA_FILTER_LABELS[key]) })),
      },
    ],
    [t_i18n],
  );

  // The selection: media picked one by one (across pages), or every one the filters match.
  const [picked, setPicked] = useState<Map<string, PickedMedia>>(() => new Map());
  const [allMatching, setAllMatching] = useState(false);
  /** The filters `Select all unused` selects everything of, once their first page is in. */
  const [selectAllFor, setSelectAllFor] = useState<string | null>(null);
  const currentKey = filtersKey(filters);
  const [selectionKey, setSelectionKey] = useState(currentKey);
  if (selectionKey !== currentKey) {
    setSelectionKey(currentKey);
    setPicked(new Map());
    setAllMatching(false);
    if (selectAllFor !== null && selectAllFor !== currentKey) setSelectAllFor(null);
  }
  if (selectAllFor === currentKey && data && !isPlaceholderData) {
    setSelectAllFor(null);
    if (data.total > 0) {
      setAllMatching(true);
      setPicked(new Map(items.map((media) => [mediaKey(media), pickedOf(media)])));
    }
  }
  const allVisibleSelected = items.length > 0 && items.every((media) => picked.has(mediaKey(media)));
  const isSelected = (media: MediaItem) => allMatching || picked.has(mediaKey(media));
  const toggleOne = (media: MediaItem) => {
    const key = mediaKey(media);
    setAllMatching(false);
    setPicked((prev) => {
      const next = new Map(allMatching ? items.map((visible) => [mediaKey(visible), pickedOf(visible)]) : prev);
      if (next.has(key)) next.delete(key);
      else next.set(key, pickedOf(media));
      return next;
    });
  };
  /** The table's own boxes (a row, the page): the page's keys it gives, the other pages' picks kept. */
  const onSelectionChange = (keys: string[]) => {
    const pageKeys = new Set(items.map(mediaKey));
    setAllMatching(false);
    setPicked((prev) => {
      const next = new Map([...prev].filter(([key]) => !pageKeys.has(key)));
      for (const media of items) if (keys.includes(mediaKey(media))) next.set(mediaKey(media), pickedOf(media));
      return next;
    });
  };
  const clearSelection = () => {
    setPicked(new Map());
    setAllMatching(false);
  };
  const selectAllUnused = () => {
    const unused = { ...filters, usage: "unused" as const };
    if (usage !== "unused") update({ [PARAM_USAGE]: "unused" });
    setSelectAllFor(filtersKey(unused));
  };
  const setRange = (next: DateRange) =>
    update({ [PARAM_RANGE.preset]: next.preset, [PARAM_RANGE.from]: next.from, [PARAM_RANGE.to]: next.to });
  const clearFilters = () => {
    setSearchDraft("");
    update({
      [LIBRARY_PARAM_SEARCH]: null,
      [PARAM_KIND]: null,
      [PARAM_USAGE]: null,
      [PARAM_RANGE.preset]: null,
      [PARAM_RANGE.from]: null,
      [PARAM_RANGE.to]: null,
    });
  };
  // The table's state is the tab's own URL keys (q, kind, sort, dir, page, limit), so every link keeps working.
  const tableState: DataTableState = {
    search,
    sort: { key: sortKey, direction: sortDir },
    page,
    pageSize,
    filters: filter === "all" ? {} : { [PARAM_KIND]: [filter] },
  };
  const onTableState = (next: DataTableState) => {
    const kinds = next.filters[PARAM_KIND] ?? [];
    const nextSort = next.sort ?? { key: DEFAULT_MEDIA_SORT, direction: DEFAULT_DIR };
    const isDefault = nextSort.key === DEFAULT_MEDIA_SORT && nextSort.direction === DEFAULT_DIR;
    update({
      // One kind narrows; both (or none) is every medium.
      [PARAM_KIND]: kinds.length === 1 ? kinds[0] : null,
      [PARAM_SORT]: isDefault ? null : nextSort.key,
      [PARAM_DIR]: isDefault ? null : nextSort.direction,
      [PARAM_LIMIT]: next.pageSize === DEFAULT_MEDIA_PAGE_SIZE ? null : String(next.pageSize),
      page: next.page > 0 ? String(next.page) : null,
    });
  };

  const remove = (media: MediaItem, force: boolean) =>
    media.kind === "take"
      ? removeTake.mutateAsync({ id: media.item.id, force })
      : removeClip.mutateAsync({ id: media.item.id, force });
  // A medium videos use is never deleted on a slip: the videos are named and a code is typed back.
  const confirmInUse = (media: MediaItem, videos: VideoAssetUse[], others: number) => {
    const total = videos.length + others;
    const shown = videos.slice(0, 3).map((video) => video.title);
    const rest = total - shown.length;
    const names = rest > 0 ? `${shown.join(", ")} ${t_i18n("and {count} more", { count: rest })}` : shown.join(", ");
    const usedIn = shown.length
      ? t_i18n("{count, plural, one {It is used in # video: {names}.} other {It is used in # videos: {names}.}}", { count: total, names })
      : t_i18n("{count, plural, one {It is used in # video you cannot open.} other {It is used in # videos you cannot open.}}", { count: total });
    return confirm({
      title: media.kind === "take" ? t_i18n("Delete a recording that videos use?") : t_i18n("Delete an AI shot that videos use?"),
      description: `${usedIn} ${t_i18n("The scenes that play it are left without it, and those videos cannot be rendered until each scene is given another one. This cannot be undone.")}`,
      confirmLabel: t_i18n("Delete anyway"),
      variant: "danger",
      challenge: true,
    });
  };
  const unpick = (media: MediaItem) =>
    setPicked((prev) => {
      if (!prev.has(mediaKey(media))) return prev;
      const next = new Map(prev);
      next.delete(mediaKey(media));
      return next;
    });
  // A bulk deletion never deletes a medium a video uses: those are kept, and forced one at a time.
  const onBulkDelete = async () => {
    const count = allMatching ? total : picked.size;
    const inUse = allMatching ? null : [...picked.values()].filter((media) => media.inUse).length;
    const description =
      usage === "unused" || inUse === 0
        ? t_i18n("No video uses them. Their files are deleted for good.")
        : inUse === null
          ? t_i18n("The ones a video uses are kept: delete those one at a time from their card. The rest are deleted for good, files included.")
          : t_i18n(
              "{count, plural, one {# of them is in a video and is kept} other {# of them are in videos and are kept}}: delete those one at a time from their card. The rest are deleted for good, files included.",
              { count: inUse },
            );
    const ok = await confirm({
      title: allMatching
        ? t_i18n("Delete the {count, plural, one {# recording or AI shot} other {# recordings and AI shots}} matching your filters?", { count })
        : t_i18n("Delete {count, plural, one {the selected recording or AI shot} other {the # selected recordings and AI shots}}?", { count }),
      description,
      confirmLabel: t_i18n("Delete"),
      variant: "danger",
    });
    if (!ok) return;
    try {
      const { deleted, kept_in_use: kept } = await bulkDelete.mutateAsync(
        allMatching
          ? { all_matching_filters: true, ...filters }
          : { items: [...picked.values()].map((media) => ({ kind: media.kind, id: media.id })) },
      );
      clearSelection();
      toast({
        title: t_i18n("{count, plural, one {# recording or AI shot deleted} other {# recordings and AI shots deleted}}", { count: deleted }),
        description: kept ? t_i18n("{count, plural, one {# kept: a video uses it.} other {# kept: videos use them.}}", { count: kept }) : undefined,
        variant: deleted || !kept ? "success" : "info",
      });
    } catch (err) {
      toast({ title: t_i18n("The media were not deleted"), description: errorText(err), variant: "error" });
    }
  };
  const onDelete = async (media: MediaItem) => {
    const uses = media.item.used_in ?? [];
    const forced = uses.length > 0;
    const ok = forced
      ? await confirmInUse(media, uses, 0)
      : await confirm({
          title: media.kind === "take" ? t_i18n("Delete this recording?") : t_i18n("Delete this AI shot?"),
          description: t_i18n("No video uses it. Its media cannot be recovered."),
          confirmLabel: t_i18n("Delete"),
          variant: "danger",
        });
    if (!ok) return;
    try {
      await remove(media, forced);
      unpick(media);
    } catch (err) {
      const refusal = forced ? null : mediaInUse(err);
      if (!refusal) {
        toast({ title: t_i18n("It was not deleted"), description: errorText(err), variant: "error" });
        return;
      }
      // A video the page did not know of uses it (one shared with others, or saved meanwhile).
      if (!(await confirmInUse(media, refusal.videos, refusal.others))) return;
      try {
        await remove(media, true);
        unpick(media);
      } catch (again) {
        toast({ title: t_i18n("It was not deleted"), description: errorText(again), variant: "error" });
      }
    }
  };
  const selectedKeys = allMatching ? items.map(mediaKey) : [...picked.keys()];
  const selectedCount = allMatching ? total : picked.size;
  return (
    <div className="space-y-4">
      {total === 0 && !filtering && !isLoading && !isError ? (
        <EmptyState
          icon={MonitorPlay}
          title="No media yet"
          description="A video is made of recordings - flows captured in the platform's own browser, in high definition - and AI shots generated by the video models of your providers. Make them from a video's editor, with the assistant or by hand."
        />
      ) : (
        /* One records table (dev-docs/reference/data-tables.md): the server searches, filters, sorts and
           pages both kinds; the grid of cards stays one toggle away and shares the toolbar and pager. */
        <DataTable<MediaItem>
          rows={items}
          columns={columns}
          rowKey={mediaKey}
          onRowClick={(media) => {
            if (media.item.status === "ready" && !media.item.expired_at) setPlaying(media);
          }}
          rowTestId="media-row"
          layout="fixed"
          rowActionsWidth={MEDIA_ROW_ACTIONS_WIDTH}
          server={{ total }}
          state={tableState}
          onStateChange={onTableState}
          search={{ value: searchDraft, onChange: setSearchDraft, placeholder: t_i18n("Search your recordings and AI shots") }}
          compactSearch
          filters={kindFilters}
          filterPills={
            <MediaFilters
              usage={usage}
              onUsageChange={(key) => update({ [PARAM_USAGE]: key === "all" ? null : key })}
              range={range}
              onRangeChange={setRange}
            />
          }
          externalFilterCount={outsideFilters}
          onClearAll={clearFilters}
          toolbar={
            <>
              {/* The retention rule is the tip of the toolbar, not a line under it (UX r1 B2 on #10312). */}
              <InfoTip
                label={t_i18n("About media retention")}
                content={t_i18n("Media expire after their retention once no video needs them; a rendered video keeps its own copy. Keep one to exempt it.")}
              />
              <Tip content={t_i18n("Show the recordings and AI shots that no video at all uses, and select them all to delete them")}>
                <Button
                  variant="outline"
                  className={cn(TOOLBAR_CONTROL_HEIGHT, "w-8 px-0")}
                  onClick={selectAllUnused}
                  aria-label={t_i18n("Select all unused")}
                  data-testid="media.selectUnused"
                >
                  <ListChecks className="h-4 w-4" aria-hidden />
                </Button>
              </Tip>
            </>
          }
          selection={{
            selected: selectedKeys,
            onChange: onSelectionChange,
            count: selectedCount,
            actions: (
              <>
                {!allMatching && allVisibleSelected && total > items.length && (
                  <Button
                    variant="ghost"
                    className={TOOLBAR_CONTROL_HEIGHT}
                    onClick={() => {
                      setAllMatching(true);
                      setPicked(new Map(items.map((media) => [mediaKey(media), pickedOf(media)])));
                    }}
                    data-testid="media.selectAllMatching"
                  >
                    {t_i18n("select all {count}", { count: total })}
                  </Button>
                )}
                <Button
                  variant="destructive"
                  className={TOOLBAR_CONTROL_HEIGHT}
                  disabled={bulkDelete.isPending}
                  onClick={() => void onBulkDelete()}
                  data-testid="media.bulkDelete"
                >
                  <Trash2 className="h-4 w-4" aria-hidden />
                  {t_i18n("Delete")}
                </Button>
                <FilterClearButton className={TOOLBAR_CONTROL_HEIGHT} onClick={clearSelection}>
                  {t_i18n("Clear selection")}
                </FilterClearButton>
              </>
            ),
          }}
          view={{ value: viewMode, onChange: setViewMode }}
          gridClassName={MEDIA_GRID}
          renderGrid={
            viewMode === "grid"
              ? (rows) => (
                  <div className={MEDIA_GRID}>
                    {rows.map((media) => (
                      <MediaCard
                        key={mediaKey(media)}
                        media={media}
                        selected={isSelected(media)}
                        onToggleSelect={() => toggleOne(media)}
                        onPlay={() => setPlaying(media)}
                        onDelete={() => void onDelete(media)}
                      />
                    ))}
                  </div>
                )
              : undefined
          }
          rowActions={(media) => (
            <div className="flex items-center justify-end gap-1">
              <Tip content={t_i18n("Play")}>
                <Button
                  variant="ghost"
                  size="icon"
                  className="h-7 w-7"
                  disabled={media.item.status !== "ready" || !!media.item.expired_at}
                  onClick={() => setPlaying(media)}
                  aria-label={t_i18n("Play")}
                >
                  <Play className="h-3.5 w-3.5" />
                </Button>
              </Tip>
              <Tip content={t_i18n("Delete")}>
                <Button variant="ghost" size="icon" className="h-7 w-7" onClick={() => void onDelete(media)} aria-label={t_i18n("Delete")}>
                  <Trash2 className="h-3.5 w-3.5" />
                </Button>
              </Tip>
            </div>
          )}
          exportName="media"
          storageKey="media"
          isLoading={isLoading || (items.length === 0 && total > 0)}
          isError={isError}
          errorLabel={t_i18n("The media could not be loaded.")}
          onRetry={() => void refetch()}
          emptyLabel={t_i18n("No media match your filters.")}
          noResultsLabel={
            usage === "unused" && !search && filter === "all" && !hasDateRange(range)
              ? t_i18n("Every recording and AI shot is in a video.")
              : t_i18n("No media match your filters.")
          }
        />
      )}
      {playing && (
        <VideoTheater
          media={{ kind: playing.kind, id: playing.item.id }}
          title={playing.kind === "take" ? playing.item.name : playing.item.name || playing.item.prompt}
          subtitle={playing.kind === "take" ? t_i18n("Recording") : t_i18n("AI shot")}
          onClose={() => setPlaying(null)}
        />
      )}
      {ConfirmEl}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------

export default function VideoStudio() {
  const { t_i18n } = useFormatter();
  const { pathname } = useLocation();
  const navigate = useNavigate();
  // The tab is the path (#10203): `/videos` is the library, `/videos/media` the media.
  const tab: StudioTab = pathname.replace(/\/+$/, "") === "/videos/media" ? "media" : "videos";
  // The former `?tab=` query is not read: no redirect (#797).
  const [creating, setCreating] = useState(false);
  const options = useMemo(() => TABS.map((key) => ({ key, label: STUDIO_TAB_LABELS[key] })), []);

  return (
    <div className="space-y-6">
      {/* One header row (#10215): the page's description is the title's "i". */}
      <ListPageHeader
        title={t_i18n("Videos")}
        infoLabel={t_i18n("About Video Studio")}
        info={t_i18n("Brand films, event loops, explainers and product demos: AI shots, big statements, real footage and recordings, made with your assistant or by hand.")}
        actions={
          <>
            {/* The designs a video is dressed with live in Agent Studio (#9301 N4 cross-link). */}
            <Button variant="outline" className="gap-1.5" asChild>
              <Link to="/templates">
                <LayoutTemplate className="h-4 w-4" /> {t_i18n("Templates")}
              </Link>
            </Button>
            <Tip content={t_i18n("The sign-ins recordings start with, shared with every agent's browser")}>
              <Button variant="outline" className="gap-1.5" asChild>
                <Link to="/connections">
                  <KeyRound className="h-4 w-4" /> {t_i18n("Browser profiles")}
                </Link>
              </Button>
            </Tip>
            <AgenticGateDialog feature={t_i18n("Producing videos")}>
              <Button className="gap-1.5" onClick={() => setCreating(true)}>
                <Sparkles className="h-4 w-4" /> {t_i18n("New video")}
                <AgenticEEBadgeForButton />
              </Button>
            </AgenticGateDialog>
          </>
        }
      />

      <SegmentedFilter
        options={options}
        value={tab}
        onChange={(key) => navigate(studioTabPath(key))}
        ariaLabel={t_i18n("Video Studio sections")}
      />

      {tab === "videos" && <VideosTab onNew={() => setCreating(true)} />}
      {tab === "media" && <MediaTab />}

      <NewVideoDialog open={creating} onOpenChange={setCreating} />
    </div>
  );
}
