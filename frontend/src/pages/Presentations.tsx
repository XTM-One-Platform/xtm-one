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
 * Presentation library — `/presentations`.
 *
 * Every deck the user can see: their own, the ones shared with them (person or
 * group grant) and the company-published ones, newest change first. The
 * library comes a page at a time, searched and filtered by the server:
 * agents keep adding decks, and every one stays reachable. The search, the
 * filters and the page live in the URL (`useLibraryParams`, the Videos tab's),
 * and the page bar sits above and below the grid. A deck is
 * an OUTPUT — the agent generated it in a chat — so there is no "create" form:
 * the primary action hands the user to their assistant with the composer
 * primed, and a deck lands here the moment the tool writes it.
 *
 * Two kinds live here, told apart by the server's `kind`: DECKS (16:9 / 4:3,
 * presented) and PAGES (A4 / letter — a product brief, a one-pager, read and
 * printed). A page card says "2 pages", not "2 slides", carries its paper
 * format and opens rather than presents; the kind filter splits the two.
 *
 * The one way a deck enters the library WITHOUT an agent is "Import": a JSON
 * package written by the export menu on another install (or a backup of this
 * one). It is deliberately not behind the AI gate — it re-creates something
 * the user already owns, through the same validator a manual edit uses.
 */
import { useMemo, useRef, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import {
  BookOpen,
  Copy,
  Eye,
  FileText,
  Globe,
  Layers,
  LayoutTemplate,
  MessageSquare,
  NotebookPen,
  Palette,
  Play,
  Presentation as PresentationIcon,
  Printer,
  Share2,
  Sparkles,
  Trash2,
  Upload,
} from "lucide-react";

import { PresentationShareDialog } from "@/components/presentation/PresentationShareDialog";
import { NewPresentationDialog } from "@/components/presentation/NewPresentationDialog";
import { useSchemeLabel } from "@/components/presentation/scheme-label";
import { openNotesHandout, openSpeakerView, speakerViewUrl } from "@/components/presentation/speaker-url";
import { usePresentationActions } from "@/components/presentation/usePresentationActions";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { ListPageHeader } from "@/components/ui/list-page-header";
import { Card, CardContent } from "@/components/ui/card";
import { CompanyManagedIcon } from "@/components/ui/company-managed-badge";
import {
  DataTable,
  type DataTableColumn,
  type DataTableFilter,
  type DataTableState,
  type SortDirection,
} from "@/components/ui/data-table";
import { EmptyState } from "@/components/ui/empty-state";
import { AgenticEEBadgeForButton, AgenticGateDialog } from "@/components/ui/license-gate-dialog";
import { OverflowMenu, type OverflowMenuItem } from "@/components/ui/overflow-menu";
import { SharedIcon } from "@/components/ui/shared-badge";
import { useToast } from "@/components/ui/toast";
import { IconMark } from "@/components/ui/icon-mark";
import { Tip } from "@/components/ui/tooltip";
import { TruncatedText } from "@/components/ui/truncated-text";
import { VisibilityEmptyHint } from "@/components/ui/visibility-filter";
import { PRESENTATIONS_KEY, useImportPresentation, usePresentations } from "@/hooks/api/usePresentationApi";
import { useSegmentCounts } from "@/hooks/api/useSegmentCounts";
import { useAuth } from "@/hooks/useAuth";
import { useViewMode } from "@/hooks/useViewMode";
import { useVisibilityParam } from "@/hooks/useVisibilityParam";
import { LIBRARY_PARAM_SEARCH, readChoice, useClampPage, useLibraryParams } from "@/hooks/useLibraryParams";
import { useAgenticAIEnabled } from "@/hooks/usePlatformConfig";
import { useFormatter } from "@/i18n";
import { isEntityShared, pluralize } from "@/lib/utils";
import type {
  PresentationImportReport,
  PresentationKind,
  PresentationSortKey,
  PresentationSummary,
} from "@/types";
import { formatTimeAgo } from "@/lib/relative-time";

type KindFilter = PresentationKind | "all";

const KINDS: KindFilter[] = ["all", "deck", "page"];
const PARAM_KIND = "kind";
const PARAM_SORT = "sort";
const PARAM_DIR = "dir";
const PARAM_LIMIT = "limit";
/** The server's orders (`GET /presentations?sort=`); each is the key of its column. */
const SORT_KEYS: PresentationSortKey[] = ["updated_at", "created_at", "title", "slide_count"];
const DIRECTIONS: SortDirection[] = ["asc", "desc"];
/** The last changed first: the deck the agent just rewrote is the one looked for. */
const DEFAULT_SORT: PresentationSortKey = "updated_at";
const DEFAULT_DIR: SortDirection = "desc";
/** A size of the table's page size select (PAGE_SIZES in data-table.tsx). */
const DEFAULT_PAGE_SIZE = 25;
const PAGE_SIZE_CHOICES = ["10", "25", "50", "100"] as const;

/** The label of each kind, read by the Kind filter through `t_i18n`. @i18n-keys */
const KIND_LABEL = { deck: "Decks", page: "Pages" } as const satisfies Record<PresentationKind, string>;

/**
 * Icon-only "view only" — the status slot of the card's icon row, where the
 * text badge would eat the width the attributes need (the counterpart to
 * `SharedIcon` / `CompanyManagedIcon`). Shown on a deck shared with the user
 * without an edit grant; the tooltip and aria-label carry the wording.
 */
function ViewOnlyIcon() {
  const { t_i18n } = useFormatter();
  const text = t_i18n("View only");
  return (
    <IconMark label={text}>
      <Eye className="h-3.5 w-3.5 shrink-0 text-default-secondary" aria-hidden="true" />
    </IconMark>
  );
}

/** A public link opens the deck for anyone, without an account. */
function PublicLinkIcon() {
  const { t_i18n } = useFormatter();
  const text = t_i18n("Public link: anyone with the link can view it");
  return (
    <IconMark label={text} testId="presentations.card.publicLink">
      <Globe className="h-3.5 w-3.5 shrink-0 text-accent" aria-hidden="true" />
    </IconMark>
  );
}

interface DeckHandlers {
  onShare: () => void;
  onEditWithAssistant: () => void;
  onDuplicate: () => void;
  onDelete: () => void;
}

/** What a deck offers, the same on its card and on its row: the open verb, the assistant and the menu. */
function useDeckMenu(deck: PresentationSummary, { onShare, onEditWithAssistant, onDuplicate, onDelete }: DeckHandlers) {
  const { t_i18n } = useFormatter();
  const navigate = useNavigate();
  const viewerUrl = `/presentations/${deck.id}`;
  // A page document is read, not presented: its primary verb is "Open".
  const isPage = deck.kind === "page";
  const openLabel = isPage ? t_i18n("Open") : t_i18n("Present");
  const OpenIcon = isPage ? BookOpen : Play;
  const assistantLabel = deck.can_manage
    ? t_i18n("Edit with my assistant")
    : t_i18n("Copy and edit with my assistant");

  const items: OverflowMenuItem[] = [
    { id: "open", label: openLabel, icon: OpenIcon, onSelect: () => navigate(viewerUrl) },
    // A deck is spoken over: the speaker window and the printable
    // notes are one click from the library, so a presenter arriving at the
    // room does not have to open the deck first. A page has neither.
    ...(isPage
      ? []
      : [
          {
            id: "speaker",
            label: t_i18n("Speaker view"),
            icon: NotebookPen,
            // A blocked popup must not be a silent no-op. Unlike the stage,
            // which has a deck on screen to keep (it toasts instead), the
            // library has nothing to lose: the speaker view opens in THIS
            // tab and runs in rehearsal mode until a presentation window
            // connects.
            onSelect: () => {
              if (!openSpeakerView(deck.id)) navigate(speakerViewUrl(deck.id));
            },
          },
          {
            id: "handout",
            label: t_i18n("Notes handout"),
            icon: Printer,
            onSelect: () => openNotesHandout(deck.id),
          },
        ]),
    { id: "assistant", label: assistantLabel, icon: Sparkles, onSelect: onEditWithAssistant },
    ...(deck.can_manage
      ? [{ id: "share", label: t_i18n("Share"), icon: Share2, onSelect: onShare }]
      : []),
    { id: "duplicate", label: t_i18n("Duplicate"), icon: Copy, onSelect: onDuplicate },
    ...(deck.can_manage
      ? [{ id: "delete", label: t_i18n("Delete"), icon: Trash2, onSelect: onDelete, destructive: true }]
      : []),
  ];
  return { viewerUrl, isPage, openLabel, OpenIcon, assistantLabel, items };
}

/** The buttons of a deck: open (present), the assistant, and the menu. They never open the deck twice. */
function DeckButtons({ deck, handlers }: { deck: PresentationSummary; handlers: DeckHandlers }) {
  const { t_i18n } = useFormatter();
  const navigate = useNavigate();
  const { viewerUrl, openLabel, OpenIcon, assistantLabel, items } = useDeckMenu(deck, handlers);
  return (
    <div
      className="ml-auto flex shrink-0 items-center gap-1"
      onClick={(e) => {
        e.preventDefault();
        e.stopPropagation();
      }}
    >
      <Tip content={openLabel}>
        <Button variant="ghost" size="sm" className="h-7 w-7 p-0" onClick={() => navigate(viewerUrl)} aria-label={openLabel}>
          <OpenIcon className="h-3.5 w-3.5" />
        </Button>
      </Tip>
      <Tip content={assistantLabel}>
        <Button
          variant="ghost"
          size="sm"
          className="h-7 w-7 p-0 text-accent hover:bg-accent/10 hover:text-accent"
          onClick={handlers.onEditWithAssistant}
          aria-label={assistantLabel}
        >
          <Sparkles className="h-3.5 w-3.5" />
        </Button>
      </Tip>
      <OverflowMenu items={items} label={t_i18n("Presentation actions")} className="h-7 w-7" />
    </div>
  );
}

/** When the deck last changed, and by whom when it was someone else, as one sentence. */
function updatedText(
  deck: PresentationSummary,
  userId: string | undefined,
  t_i18n: ReturnType<typeof useFormatter>["t_i18n"],
  intl: ReturnType<typeof useFormatter>["intl"],
): string {
  if (!deck.updated_at) return t_i18n("Revision {revision}", { revision: deck.revision });
  return deck.updated_by_name && deck.updated_by_user_id !== userId
    ? t_i18n("Edited {when} by {name}", { when: formatTimeAgo(intl, deck.updated_at), name: deck.updated_by_name })
    : t_i18n("Updated {when}", { when: formatTimeAgo(intl, deck.updated_at) });
}

/** When the deck last changed, and by whom when it was someone else. */
function UpdatedLine({ deck, userId }: { deck: PresentationSummary; userId?: string }) {
  const { t_i18n, intl } = useFormatter();
  return <>{updatedText(deck, userId, t_i18n, intl)}</>;
}

/**
 * Column widths in px under the fixed layout: the Name column takes what is left and cuts
 * a long title with the whole title in the tooltip, so the row actions stay on screen at
 * 1080 (UX r1 B1 on #11075). Template steps aside below 56rem (`hideBelow`).
 */
const COLUMN_WIDTHS = {
  slide_count: 96,
  template: 200,
  comments: 112,
  sharing: 104,
  updated_at: 176,
  created_at: 128,
} as const;
/** Open, assistant and the menu: three 28 px buttons, their gaps and the cell padding. */
const ROW_ACTIONS_WIDTH = 112;

/** The deck's sharing, company management, public link and view-only marks, as icons. */
function StatusIcons({ deck, userId }: { deck: PresentationSummary; userId?: string }) {
  return (
    <div className="flex shrink-0 items-center gap-1.5">
      {deck.has_public_link && <PublicLinkIcon />}
      {isEntityShared(deck, userId) && <SharedIcon ownerName={deck.owner_name} />}
      {deck.is_company_managed && <CompanyManagedIcon />}
      {!deck.can_manage && !deck.is_company_managed && <ViewOnlyIcon />}
    </div>
  );
}

function DeckCard({
  deck,
  userId,
  handlers,
}: {
  deck: PresentationSummary;
  userId?: string;
  handlers: DeckHandlers;
}) {
  const { t_i18n } = useFormatter();
  const schemeLabel = useSchemeLabel();
  const navigate = useNavigate();
  const viewerUrl = `/presentations/${deck.id}`;
  const isPage = deck.kind === "page";
  const KindIcon = isPage ? FileText : PresentationIcon;

  return (
    <Card
      data-testid="presentation-card"
      className="h-full cursor-pointer border-elevation-subtle transition-all hover:bg-elevation-hover"
      onClick={() => navigate(viewerUrl)}
    >
      <CardContent className="flex h-full flex-col p-4">
        <div className="flex items-center gap-3">
          {/* Only the identity cluster is the anchor — the split every entity
              card makes (see `AgentCard`). The card's own onClick keeps the
              whole surface clickable, while the row actions stay OUTSIDE the
              <a>: a <button> nested in an anchor is invalid markup and gives
              unreliable keyboard/AT semantics. */}
          <Link to={viewerUrl} className="flex min-w-0 flex-1 items-center gap-3" onClick={(e) => e.stopPropagation()}>
            <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-sm bg-accent/10">
              <KindIcon className="h-5 w-5 text-accent" />
            </div>
            <div className="min-w-0 flex-1">
              <p className="truncate content-base font-medium">{deck.title}</p>
              <p className="truncate content-caption text-default-secondary">
                {deck.owner_name && deck.user_id !== userId
                  ? t_i18n("By {name}", { name: deck.owner_name })
                  : t_i18n("Generated by your assistant")}
              </p>
            </div>
          </Link>
        </div>

        {/* Attributes — fixed-height row for consistent card sizing, the same
            18px the agent card gives its tags. Sharing, company-management and
            view-only ride along on the right as icons rather than badges, so
            the row above keeps its full width for the title. */}
        <div className="mt-3 flex h-[18px] items-center gap-1">
          <div className="flex min-w-0 items-center gap-1.5 overflow-hidden">
            <Badge variant="cyan" className="shrink-0 gap-1 px-1.5 py-0 content-caption">
              {isPage ? <FileText className="h-2.5 w-2.5" /> : <Layers className="h-2.5 w-2.5" />}
              {deck.slide_count} {t_i18n(pluralize(deck.slide_count, isPage ? "page" : "slide"))}
            </Badge>
            {isPage && (
              // The paper the document prints on — what makes it a page.
              <Badge variant="outline" className="shrink-0 px-1.5 py-0 font-mono-plex content-caption uppercase">
                {deck.aspect_ratio}
              </Badge>
            )}
            {/* The DESIGN the deck wears: its template, or the plain
                look, then the colour scheme it is shown in. */}
            <Tip
              content={
                deck.template_name
                  ? t_i18n("Template: {template} - {theme} theme", {
                      template: deck.template_name,
                      theme: schemeLabel(deck.theme),
                    })
                  : t_i18n("No template - plain look, {theme} theme", {
                      theme: schemeLabel(deck.theme),
                    })
              }
            >
              <Badge variant="outline" className="min-w-0 shrink gap-1 px-1.5 py-0 content-caption">
                <Palette className="h-2.5 w-2.5 shrink-0" />
                <span className="truncate">
                  {deck.template_name ?? t_i18n("No template")}
                </span>
                <span className="shrink-0 text-default-secondary">
                  · {schemeLabel(deck.theme)}
                </span>
              </Badge>
            </Tip>
            {deck.open_comment_count > 0 && (
              // Open review threads are work waiting on someone: surfaced on
              // the card, and one click lands in the viewer with the panel
              // open. It stops the click so the card's own navigation does not
              // then drop the `?panel=`.
              <Tip content={t_i18n("Open review comments")}>
                <button
                  type="button"
                  onClick={(e) => {
                    e.stopPropagation();
                    navigate(`${viewerUrl}?panel=comments`);
                  }}
                  aria-label={t_i18n("Open review comments")}
                  className="inline-flex shrink-0 items-center gap-1 rounded-sm border border-feedback-alert-primary/30 bg-feedback-alert-primary/10 px-1.5 py-0 content-caption font-medium text-feedback-warning-tertiary transition-colors hover:bg-feedback-alert-primary/20"
                >
                  <MessageSquare className="h-2.5 w-2.5" />
                  {deck.open_comment_count}
                </button>
              </Tip>
            )}
          </div>
          <div className="ml-auto">
            <StatusIcons deck={deck} userId={userId} />
          </div>
        </div>

        {/* Metadata — pushed to bottom; the actions swallow the click so they
            never also open the deck. */}
        <div className="mt-auto flex items-center gap-3 border-t border-elevation-subtle-soft pt-3">
          <span className="min-w-0 truncate content-caption text-default-secondary">
            <UpdatedLine deck={deck} userId={userId} />
          </span>
          <DeckButtons deck={deck} handlers={handlers} />
        </div>
      </CardContent>
    </Card>
  );
}

/**
 * One line the import toast can show as-is: what was re-used, what was copied
 * in, and which template the deck ended up on. The server's warnings (a
 * template replaced by the default, a file that could not be read on export)
 * follow, because a silent substitution is exactly what an importer would
 * want to know about before presenting.
 */
function describeImport(
  report: PresentationImportReport,
  t_i18n: (id: string, values?: Record<string, string | number>) => string,
): string {
  const parts: string[] = [];
  if (report.assets_total > 0) {
    parts.push(
      t_i18n("{matched} assets matched, {copied} copied", {
        matched: report.assets_matched,
        copied: report.assets_copied,
      }),
    );
  }
  if (report.template === "matched" && report.template_name) {
    parts.push(t_i18n("template: {name}", { name: report.template_name }));
  } else if (report.template === "default" && report.template_name) {
    parts.push(t_i18n("default template used: {name}", { name: report.template_name }));
  }
  return [...parts, ...report.warnings].join(" - ");
}

/**
 * The library's columns: the deck (kind icon, title, owner), its size, design, open
 * comments, sharing and dates. Every sortable column is a server order (SORT_KEYS).
 */
function usePresentationColumns(userId: string | undefined): DataTableColumn<PresentationSummary>[] {
  const { t_i18n, intl } = useFormatter();
  const schemeLabel = useSchemeLabel();
  return useMemo<DataTableColumn<PresentationSummary>[]>(
    () => [
      {
        key: "title",
        header: "Name",
        sortValue: (deck) => deck.title.toLowerCase(),
        firstSort: "asc",
        csv: (deck) => deck.title,
        cell: (deck) => {
          const KindIcon = deck.kind === "page" ? FileText : PresentationIcon;
          return (
            <div className="flex min-w-0 items-center gap-2">
              <KindIcon className="h-4 w-4 shrink-0 text-accent" aria-hidden="true" />
              <div className="flex min-w-0 flex-1 flex-col">
                <TruncatedText text={deck.title} className="font-medium" />
                <TruncatedText
                  text={
                    deck.owner_name && deck.user_id !== userId
                      ? t_i18n("By {name}", { name: deck.owner_name })
                      : t_i18n("Generated by your assistant")
                  }
                  className="content-caption text-default-secondary"
                />
              </div>
            </div>
          );
        },
      },
      {
        key: "slide_count",
        header: "Size",
        align: "right",
        hideable: true,
        width: COLUMN_WIDTHS.slide_count,
        sortValue: (deck) => deck.slide_count,
        csv: (deck) => deck.slide_count,
        cell: (deck) => (
          <span className="tabular-nums">
            {deck.slide_count} {t_i18n(pluralize(deck.slide_count, deck.kind === "page" ? "page" : "slide"))}
          </span>
        ),
      },
      {
        key: "template",
        header: "Template",
        hideable: true,
        hideBelow: "4xl",
        width: COLUMN_WIDTHS.template,
        csv: (deck) => deck.template_name ?? "",
        cell: (deck) => (
          <TruncatedText
            text={[
              deck.template_name ?? t_i18n("No template"),
              schemeLabel(deck.theme),
              ...(deck.kind === "page" ? [deck.aspect_ratio] : []),
            ].join(" · ")}
          />
        ),
      },
      {
        key: "comments",
        header: "Comments",
        align: "right",
        hideable: true,
        width: COLUMN_WIDTHS.comments,
        csv: (deck) => deck.open_comment_count,
        // Plain text: the row opens the deck, no cell draws a link (#12070).
        cell: (deck) =>
          deck.open_comment_count > 0 ? (
            // The shared tooltip, never a native title (UX round 1 A1 on #12090); no tab stop,
            // the row is the one stop.
            <Tip content={t_i18n("Open review comments")}>
              <span
                className="inline-flex items-center gap-1 tabular-nums text-feedback-warning-tertiary"
              >
                <MessageSquare className="h-3.5 w-3.5" aria-hidden="true" />
                {deck.open_comment_count}
              </span>
            </Tip>
          ) : (
            <span className="text-default-secondary">-</span>
          ),
      },
      {
        key: "sharing",
        header: "Sharing",
        hideable: true,
        width: COLUMN_WIDTHS.sharing,
        cell: (deck) => <StatusIcons deck={deck} userId={userId} />,
      },
      {
        key: "updated_at",
        header: "Updated",
        width: COLUMN_WIDTHS.updated_at,
        sortValue: (deck) => deck.updated_at ?? "",
        firstSort: "desc",
        csv: (deck) => deck.updated_at ?? "",
        cell: (deck) => (
          <TruncatedText text={updatedText(deck, userId, t_i18n, intl)} className="text-default-secondary" />
        ),
      },
      {
        key: "created_at",
        header: "Created",
        hideable: true,
        defaultHidden: true,
        width: COLUMN_WIDTHS.created_at,
        sortValue: (deck) => deck.created_at ?? "",
        firstSort: "desc",
        csv: (deck) => deck.created_at ?? "",
        cell: (deck) => (
          <span className="text-default-secondary">{deck.created_at ? formatTimeAgo(intl, deck.created_at) : "-"}</span>
        ),
      },
    ],
    [t_i18n, intl, schemeLabel, userId],
  );
}

export default function Presentations() {
  const navigate = useNavigate();
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const { user } = useAuth();
  const agenticAIEnabled = useAgenticAIEnabled();
  const { searchDraft, setSearchDraft, search, page, setPage, params, update } = useLibraryParams();
  const kindFilter = readChoice(params.get(PARAM_KIND), KINDS, "all");
  const sortKey = readChoice(params.get(PARAM_SORT), SORT_KEYS, DEFAULT_SORT);
  const sortDir = readChoice(params.get(PARAM_DIR), DIRECTIONS, DEFAULT_DIR);
  const pageSize = Number(readChoice(params.get(PARAM_LIMIT), PAGE_SIZE_CHOICES, String(DEFAULT_PAGE_SIZE)));
  const [viewMode, setViewMode] = useViewMode("presentations");
  // Each segment's count under the search and the kind, the numbers on the filter.
  const segmentCounts = useSegmentCounts([...PRESENTATIONS_KEY, "list"], "/presentations", {
    search: search || undefined,
    kind: kindFilter === "all" ? undefined : kindFilter,
  });
  // The URL's segment, else the first one holding a deck once the counts are in.
  const [visibility, setVisibility, landing] = useVisibilityParam(undefined, undefined, { counts: segmentCounts });
  // The server searches, filters, sorts and pages the whole library; the last changed first by default.
  // Read errors stay on the page: the table draws its error line with Retry (UX r1 B2 on #11075).
  const { data, isLoading, isError, refetch } = usePresentations(
    {
      search: search || undefined,
      vis: visibility === "all" ? undefined : visibility,
      kind: kindFilter === "all" ? undefined : kindFilter,
      limit: pageSize,
      offset: page * pageSize,
      sort: sortKey === DEFAULT_SORT && sortDir === DEFAULT_DIR ? undefined : sortKey,
      direction: sortKey === DEFAULT_SORT && sortDir === DEFAULT_DIR ? undefined : sortDir,
    },
    { enabled: !landing, inlineError: true },
  );
  useClampPage(page, pageSize, data?.total, setPage);
  const decks = data?.items ?? [];
  const total = data?.total ?? 0;
  // The library's size across every segment: the empty state and the "n of N" total read it.
  const { data: everything } = usePresentations({ limit: 1, offset: 0 }, { inlineError: true });
  const allCount = everything?.total;
  const filtering = !!search || kindFilter !== "all";
  const columns = usePresentationColumns(user?.id);
  const kindFilters = useMemo<DataTableFilter<PresentationSummary>[]>(
    () => [
      {
        key: PARAM_KIND,
        label: "Kind",
        options: (Object.keys(KIND_LABEL) as PresentationKind[]).map((kind) => ({ value: kind, label: t_i18n(KIND_LABEL[kind]) })),
      },
    ],
    [t_i18n],
  );
  // The table's state is the page's own URL keys (q, kind, sort, dir, page, limit), so every link keeps working.
  const tableState: DataTableState = {
    search,
    sort: { key: sortKey, direction: sortDir },
    page,
    pageSize,
    filters: kindFilter === "all" ? {} : { [PARAM_KIND]: [kindFilter] },
  };
  const onTableState = (next: DataTableState) => {
    const kinds = next.filters[PARAM_KIND] ?? [];
    const nextSort = next.sort ?? { key: DEFAULT_SORT, direction: DEFAULT_DIR };
    const isDefault = nextSort.key === DEFAULT_SORT && nextSort.direction === DEFAULT_DIR;
    update({
      // One kind narrows; both (or none) is every deck and page.
      [PARAM_KIND]: kinds.length === 1 ? kinds[0] : null,
      [PARAM_SORT]: isDefault ? null : nextSort.key,
      [PARAM_DIR]: isDefault ? null : nextSort.direction,
      [PARAM_LIMIT]: next.pageSize === DEFAULT_PAGE_SIZE ? null : String(next.pageSize),
      page: next.page > 0 ? String(next.page) : null,
    });
  };
  const actions = usePresentationActions();
  const handlersFor = (deck: PresentationSummary): DeckHandlers => ({
    onShare: () => setSharing(deck),
    onEditWithAssistant: () => void actions.editWithAssistant(deck),
    onDuplicate: () => void actions.duplicate(deck),
    onDelete: () => void actions.remove(deck),
  });
  const importDeck = useImportPresentation();
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [sharing, setSharing] = useState<PresentationSummary | null>(null);

  // A hidden file input rather than a dialog (same as the template import):
  // one file, no options. The backend refuses a malformed or unresolvable
  // package with a readable reason, which is what the error toast shows; a
  // successful import lands on the deck with a report of what was matched,
  // copied and substituted.
  const handleImport = async (file: File | undefined) => {
    if (!file) return;
    try {
      const imported = await importDeck.mutateAsync({ file });
      toast({
        title: t_i18n("Presentation imported"),
        description: describeImport(imported.import_report, t_i18n) || imported.title,
        variant: imported.import_report.warnings.length > 0 ? "info" : "success",
      });
      navigate(`/presentations/${imported.id}`);
    } catch (err) {
      toast({
        title: t_i18n("Import failed"),
        description:
          err instanceof Error && err.message
            ? err.message
            : t_i18n("The file is not a presentation package this platform can read."),
        variant: "error",
      });
    }
  };

  // `New presentation` settles the design first: the dialog picks the
  // template and the theme, then hands the chat a message that names them.
  const [creating, setCreating] = useState(false);
  const startWithAssistant = () => setCreating(true);

  return (
    <div className="space-y-6">
      {/* One header row (#9301 N5, UX A2 of #10508): the shared ListPageHeader, the
          description in its "i", the total beside the title so a three-digit count never
          wraps the toolbar row at 1024. */}
      <ListPageHeader
        title={t_i18n("Presentations")}
        infoLabel={t_i18n("About presentations")}
        info={t_i18n("Decks and pages your agents generated: yours, shared with you, and published to the company.")}
        meta={
          !(landing || isLoading) && (
            <span
              data-testid="presentations-total"
              className="shrink-0 rounded-sm bg-elevation-hover px-2.5 py-1 content-compact font-medium tabular-nums text-default-secondary"
            >
              {total}
            </span>
          )
        }
        actions={
          <>
            <input
              ref={fileInputRef}
              type="file"
              accept=".json,application/json"
              className="hidden"
              onChange={(event) => {
                void handleImport(event.target.files?.[0]);
                // Reset so re-picking the same file fires `change` again.
                event.target.value = "";
              }}
            />
            {/* The designs a deck is dressed with live in Agent Studio (#9301 N4 cross-link). */}
            <Button variant="outline" className="gap-1.5" asChild>
              <Link to="/templates">
                <LayoutTemplate className="h-4 w-4" /> {t_i18n("Templates")}
              </Link>
            </Button>
            <Tip content={t_i18n("Import a JSON package exported from the presentation's export menu on another platform")}>
              <Button
                variant="outline"
                className="gap-1.5"
                disabled={importDeck.isPending}
                onClick={() => fileInputRef.current?.click()}
              >
                <Upload className="h-4 w-4" /> {importDeck.isPending ? t_i18n("Importing...") : t_i18n("Import")}
              </Button>
            </Tip>
            <AgenticGateDialog feature={t_i18n("Generating presentations")}>
              <Button className="gap-1.5" onClick={startWithAssistant}>
                <Sparkles className="h-4 w-4" /> {t_i18n("New presentation")}
                <AgenticEEBadgeForButton />
              </Button>
            </AgenticGateDialog>
          </>
        }
      />

      {total === 0 && !filtering && allCount === 0 && !landing && !isLoading && !isError ? (
        <EmptyState
          icon={PresentationIcon}
          title="No presentations yet"
          requiresAi
          description="Ask your assistant for a deck. It lands here, ready to present, share with colleagues and export to PDF or PowerPoint."
          actionLabel={agenticAIEnabled ? "Create with your assistant" : undefined}
          actionIcon={Sparkles}
          actionVariant="default"
          onAction={agenticAIEnabled ? startWithAssistant : undefined}
        />
      ) : (
        // One records table (dev-docs/reference/data-tables.md): the server searches, filters,
        // sorts and pages; the grid of cards stays one toggle away and shares the toolbar and pager.
        <DataTable<PresentationSummary>
          rows={decks}
          columns={columns}
          rowKey={(deck) => deck.id}
          rowHref={(deck) => `/presentations/${deck.id}`}
          rowTestId="presentation-row"
          rowLines={2}
          layout="fixed"
          rowActionsWidth={ROW_ACTIONS_WIDTH}
          server={{ total }}
          totalAll={allCount}
          state={tableState}
          onStateChange={onTableState}
          search={{ value: searchDraft, onChange: setSearchDraft, placeholder: t_i18n("Search presentations") }}
          scope={{ value: visibility, onChange: setVisibility, counts: segmentCounts }}
          filters={kindFilters}
          onClearAll={() => {
            setSearchDraft("");
            update({ [LIBRARY_PARAM_SEARCH]: null, [PARAM_KIND]: null });
          }}
          view={{ value: viewMode, onChange: setViewMode }}
          renderGrid={
            viewMode === "grid"
              ? (page) => (
                  <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-3">
                    {page.map((deck) => (
                      <DeckCard key={deck.id} deck={deck} userId={user?.id} handlers={handlersFor(deck)} />
                    ))}
                  </div>
                )
              : undefined
          }
          rowActions={(deck) => <DeckButtons deck={deck} handlers={handlersFor(deck)} />}
          storageKey="presentations"
          isLoading={landing || isLoading || (decks.length === 0 && total > 0)}
          isError={isError}
          errorLabel={t_i18n("The presentations could not be loaded.")}
          onRetry={() => void refetch()}
          emptyLabel={t_i18n("No presentations matching your filters")}
          noResultsLabel={t_i18n("No presentations matching your filters")}
          emptyContent={
            <VisibilityEmptyHint value={visibility} counts={{ mine: total, all: allCount }} onShowAll={() => setVisibility("all")}>
              <p className="py-8 text-center content-base text-default-secondary">
                {t_i18n("No presentations matching your filters")}
              </p>
            </VisibilityEmptyHint>
          }
        />
      )}

      {sharing && (
        <PresentationShareDialog
          presentation={sharing}
          open
          onOpenChange={(open) => {
            if (!open) setSharing(null);
          }}
        />
      )}
      <NewPresentationDialog open={creating} onOpenChange={setCreating} />
      {actions.ConfirmEl}
    </div>
  );
}
