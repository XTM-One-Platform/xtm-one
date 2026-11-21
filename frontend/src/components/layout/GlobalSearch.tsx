// SPDX-License-Identifier: Apache-2.0

/**
 * The top bar search: an inline field, never a dialog (#11201, OpenAEV and
 * OpenCTI style). Typing searches the platform's data across every entity type
 * through `GET /search` (ranked, RBAC-scoped server side) and the pages of the
 * navigation; the list under the field shows the top hits grouped by type, and
 * Enter or "See all results" opens `/search?q=` (shareable, back-button safe).
 *
 * What the former command palette did stays in the field, by prefix:
 *
 *   (none)  entities and pages  - `GET /search` and `nav-model`
 *   `>`     run a command        - the screens' `*.commands.ts` manifests
 *   `@`     jump inside the page - the sections the mounted screen registered
 *
 * Ctrl K (Cmd K) and `/` focus the field from anywhere; Escape closes the list.
 */
import {
  createContext, useCallback, useContext, useEffect, useId, useMemo, useRef, useState,
  type ReactNode,
} from "react";
import { useNavigate } from "react-router-dom";
import { Icon } from "@filigran/design-system";
import { SearchInput } from "@/components/ui/search-input";
import { TruncatedText } from "@/components/ui/truncated-text";
import {
  AgenticEEBadgeForButton, AgenticGateDialogControlled,
} from "@/components/ui/license-gate-dialog";
import { cn, isMac } from "@/lib/utils";
import { useFormatter } from "@/i18n";
import { useAuth } from "@/hooks/useAuth";
import { useDebounce } from "@/hooks/useDebounce";
import { useClickOutside } from "@/hooks/useClickOutside";
import { useSearch, MIN_SEARCH_LENGTH, type SearchType } from "@/hooks/api/useSearchApi";
import { useCommands } from "@/lib/commands/CommandsProvider";
import { resolveCommands } from "@/lib/commands/registry";
import { matchCommands } from "@/lib/commands/flatten";
import type { CommandDef } from "@/lib/commands/types";
import type { LucideIcon } from "lucide-react";
import { SEARCH_TYPE_VIEW, searchPageHref, type LibraryIconName } from "@/lib/search-types";
import { navDestinations, navLabel, resolveNav } from "./nav-model";
import { useNavFlags } from "./use-nav-group";

/* ── focus request, shared by the shortcut and the field ────────── */

interface SearchFocusState {
  /** Bumped by Ctrl K or `/`: the field focuses itself on every change. */
  focusRequest: number;
  requestFocus: () => void;
}

const SearchFocusContext = createContext<SearchFocusState | null>(null);

/** `/` focuses the field unless the key is typed into another field. */
function typingElsewhere(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false;
  return target.isContentEditable || ["INPUT", "TEXTAREA", "SELECT"].includes(target.tagName);
}

export function GlobalSearchProvider({ children }: { children: ReactNode }) {
  const [focusRequest, setFocusRequest] = useState(0);
  const requestFocus = useCallback(() => setFocusRequest((n) => n + 1), []);

  useEffect(() => {
    const onKeyDown = (e: KeyboardEvent) => {
      const shortcut = e.key.toLowerCase() === "k" && (e.metaKey || e.ctrlKey);
      const slash = e.key === "/" && !e.metaKey && !e.ctrlKey && !e.altKey && !typingElsewhere(e.target);
      if (!shortcut && !slash) return;
      /* Ctrl K is intercepted even inside a text field: a global shortcut. */
      e.preventDefault();
      requestFocus();
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [requestFocus]);

  const value = useMemo(() => ({ focusRequest, requestFocus }), [focusRequest, requestFocus]);
  return <SearchFocusContext.Provider value={value}>{children}</SearchFocusContext.Provider>;
}

function useSearchFocus(): SearchFocusState {
  const ctx = useContext(SearchFocusContext);
  if (!ctx) throw new Error("useSearchFocus must be used inside <GlobalSearchProvider>");
  return ctx;
}

/* ── modes ──────────────────────────────────────────────────────── */

type Mode = "search" | "command" | "section";

function parseQuery(query: string): { mode: Mode; term: string } {
  if (query.startsWith(">")) return { mode: "command", term: query.slice(1).trimStart() };
  if (query.startsWith("@")) return { mode: "section", term: query.slice(1).trimStart() };
  return { mode: "search", term: query };
}

/** Hits of each type shown under the field; the page holds the rest. */
const TYPEAHEAD_PER_TYPE = 3;
const TYPEAHEAD_HITS = 25;
const TYPEAHEAD_PAGES = 4;

interface Row {
  key: string;
  label: string;
  hint?: string;
  /** A library icon name, or the component a nav entry or command carries. */
  icon?: LibraryIconName | LucideIcon;
  gated?: boolean;
  locked?: boolean;
  /** Group heading drawn above the row (the first of its group). */
  group?: string;
  select: () => void;
}

function RowIcon({ icon }: { icon?: LibraryIconName | LucideIcon }) {
  if (!icon) return <Icon name="arrow-right" size={16} className="shrink-0 text-default-secondary" />;
  if (typeof icon === "string") return <Icon name={icon} size={16} className="shrink-0 text-default-secondary" />;
  const Glyph = icon;
  return <Glyph className="size-4 shrink-0 text-default-secondary" aria-hidden="true" />;
}

/* ── field ──────────────────────────────────────────────────────── */

/** The inline search field of the top bar, with its typeahead list. */
export function GlobalSearch() {
  const { t_i18n } = useFormatter();
  const navigate = useNavigate();
  const { isAdmin, isGroupManager } = useAuth();
  const navFlags = useNavFlags();
  const { context, sections, runCommand } = useCommands();
  const { focusRequest } = useSearchFocus();

  const [query, setQuery] = useState("");
  const [open, setOpen] = useState(false);
  const [activeIndex, setActiveIndex] = useState(0);
  /** The arrows moved onto a row: Enter then opens that row, not the results page. */
  const [arrowed, setArrowed] = useState(false);
  const [gate, setGate] = useState<string | null>(null);
  const boxRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const listId = useId();

  useEffect(() => {
    if (focusRequest === 0) return;
    inputRef.current?.focus();
    inputRef.current?.select();
    setOpen(true);
  }, [focusRequest]);

  useClickOutside(boxRef, () => setOpen(false), open);

  const { mode, term } = parseQuery(query);
  const debounced = useDebounce(mode === "search" ? term : "", 200);
  const hits = useSearch(debounced, { limit: TYPEAHEAD_HITS });
  const hitPage = hits.data;

  const close = useCallback(() => setOpen(false), []);
  const reset = useCallback(() => { setOpen(false); setQuery(""); inputRef.current?.blur(); }, []);
  const go = useCallback((to: string) => { reset(); navigate(to); }, [reset, navigate]);

  const seeAll = useCallback(() => {
    if (term.trim().length < MIN_SEARCH_LENGTH) return;
    go(searchPageHref(term));
  }, [term, go]);

  const rows = useMemo<Row[]>(() => {
    const needle = term.trim().toLowerCase();
    if (mode === "command") {
      // A parent's children are listed in its place ("Tasks: Work Items"), never dropped (#785, B2).
      return matchCommands(resolveCommands(context), needle)
        .map((command: CommandDef) => {
          const gated = command.requires === "agenticAI" && !context.agenticAI;
          return {
            key: `command-${command.id}`,
            label: `${t_i18n(command.category)}: ${t_i18n(command.title)}`,
            icon: command.icon,
            gated,
            select: () => {
              reset();
              if (gated) { setGate(`${command.category}: ${command.title}`); return; }
              runCommand(command);
            },
          };
        });
    }
    if (mode === "section") {
      return sections
        .filter((section) => t_i18n(section.title).toLowerCase().includes(needle))
        .map((section, index) => ({
          key: `section-${index}-${section.id}`,
          label: t_i18n(section.title),
          select: () => { close(); section.select(); },
        }));
    }
    if (!needle) return [];

    const pages: Row[] = navDestinations(resolveNav(navFlags, isAdmin, isGroupManager))
      .filter((dest) => `${navLabel(dest, t_i18n)} ${dest.label} ${dest.parent ?? ""} ${(dest.keywords ?? []).join(" ")}`
        .toLowerCase().includes(needle))
      .slice(0, TYPEAHEAD_PAGES)
      .map((dest, index) => ({
        key: `page-${dest.to}`,
        label: navLabel(dest, t_i18n),
        hint: dest.parent ? t_i18n(dest.parent) : undefined,
        icon: dest.icon,
        locked: dest.locked,
        group: index === 0 ? t_i18n("Pages") : undefined,
        select: () => go(dest.to),
      }));

    const perType = new Map<SearchType, number>();
    const entities: Row[] = [];
    const seenType = new Set<SearchType>();
    for (const hit of hitPage?.items ?? []) {
      const shown = perType.get(hit.type) ?? 0;
      if (shown >= TYPEAHEAD_PER_TYPE) continue;
      perType.set(hit.type, shown + 1);
      const view = SEARCH_TYPE_VIEW[hit.type];
      entities.push({
        key: `hit-${hit.type}-${hit.id}`,
        label: hit.name,
        hint: hit.excerpt ?? undefined,
        icon: view.icon,
        group: seenType.has(hit.type) ? undefined : t_i18n(view.plural),
        select: () => go(view.href(hit)),
      });
      seenType.add(hit.type);
    }
    /* Grouped by type in the order of their best hit. */
    const order = [...seenType];
    entities.sort((a, b) => order.indexOf(typeOf(a.key)) - order.indexOf(typeOf(b.key)));

    /* Nothing found anywhere: no see-all row, so the list says "No match" (UX r1 B3). */
    const nothing = hitPage?.total === 0 && entities.length === 0 && pages.length === 0;
    const all: Row[] = term.trim().length >= MIN_SEARCH_LENGTH && !nothing
      ? [{
        key: "see-all",
        label: t_i18n("See all results"),
        hint: hitPage ? String(hitPage.total) : undefined,
        icon: "arrow-right",
        select: seeAll,
      }]
      : [];
    return [...entities, ...pages, ...all];
  }, [mode, term, context, sections, navFlags, isAdmin, isGroupManager, hitPage, t_i18n, runCommand, reset, close, go, seeAll]);

  useEffect(() => { setActiveIndex(0); setArrowed(false); }, [query]);

  const onKeyDown = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      setOpen(true);
      if (rows.length === 0) return;
      const delta = e.key === "ArrowDown" ? 1 : -1;
      setArrowed(true);
      setActiveIndex((i) => (i + delta + rows.length) % rows.length);
      return;
    }
    if (e.key === "Enter") {
      e.preventDefault();
      const row = rows[activeIndex];
      /* A typed term with no row picked by the arrows opens the results page. */
      if (mode === "search" && !arrowed && term.trim().length >= MIN_SEARCH_LENGTH) {
        seeAll();
        return;
      }
      row?.select();
      return;
    }
    if (e.key === "Escape") {
      e.preventDefault();
      if (open) setOpen(false);
      else inputRef.current?.blur();
    }
  };

  const showList = open && (rows.length > 0 || mode !== "search" || debounced.trim().length >= MIN_SEARCH_LENGTH);
  const shortcut = isMac() ? "⌘K" : "Ctrl K";

  return (
    <>
      <AgenticGateDialogControlled
        open={gate !== null}
        onOpenChange={(next) => { if (!next) setGate(null); }}
        feature={gate ?? undefined}
      />
      <div ref={boxRef} className="relative w-[min(40vw,480px)] min-w-48" data-testid="global-search">
        <SearchInput
          value={query}
          onChange={(next) => { setQuery(next); setOpen(true); }}
          placeholder={t_i18n("Search the platform ({shortcut})", { shortcut })}
          aria-label={t_i18n("Search the platform")}
          inputRef={inputRef}
          onFocus={() => setOpen(true)}
          onKeyDown={onKeyDown}
          className="max-w-none"
          combobox={{
            expanded: showList,
            controls: listId,
            activeDescendant: showList && arrowed && rows[activeIndex] ? `${listId}-option-${activeIndex}` : undefined,
          }}
        />
        {showList && (
          <div
            id={listId}
            role="listbox"
            aria-label={t_i18n("Search results")}
            className="absolute left-0 right-0 top-full z-50 mt-1 max-h-[60vh] overflow-y-auto rounded-sm border border-elevation-subtle bg-elevation-default py-1 shadow-overlay"
          >
            {rows.length === 0 && (
              <p className="px-4 py-3 content-compact text-default-secondary">
                {mode === "section" && sections.length === 0
                  ? t_i18n("This page has no sections to jump to")
                  : hits.isFetching ? t_i18n("Searching...") : t_i18n("No match")}
              </p>
            )}
            {rows.map((row, index) => (
              <div key={row.key}>
                {row.group && (
                  <div className="px-4 pb-1 pt-2 content-caption font-medium text-default-secondary">{row.group}</div>
                )}
                <button
                  type="button"
                  id={`${listId}-option-${index}`}
                  tabIndex={-1}
                  role="option"
                  aria-selected={index === activeIndex}
                  onMouseMove={() => { setActiveIndex(index); setArrowed(true); }}
                  onMouseDown={(e) => e.preventDefault()}
                  onClick={row.select}
                  className={cn(
                    "flex h-9 w-full items-center gap-3 px-4 text-left content-base transition-colors",
                    index === activeIndex ? "bg-elevation-hover text-default-primary" : "text-default-primary",
                    row.key === "see-all" && "border-t border-elevation-subtle-soft",
                  )}
                >
                  <RowIcon icon={row.icon} />
                  {/* The name has priority over the hint (UX r1 B2): up to 60% of the row when a hint
                      follows, the hint takes the rest; both name their full text when cut. */}
                  <TruncatedText
                    text={row.label}
                    className={cn(row.hint ? "max-w-[60%] shrink-0" : "flex-1", row.locked && "opacity-60")}
                  />
                  {row.gated && <AgenticEEBadgeForButton />}
                  {row.locked && <Icon name="lock" size={16} className="shrink-0 text-feedback-warning-primary" />}
                  {row.hint && (
                    <TruncatedText text={row.hint} className="ml-auto content-caption text-default-secondary" />
                  )}
                </button>
              </div>
            ))}
          </div>
        )}
      </div>
    </>
  );
}

function typeOf(key: string): SearchType {
  return key.split("-")[1] as SearchType;
}
