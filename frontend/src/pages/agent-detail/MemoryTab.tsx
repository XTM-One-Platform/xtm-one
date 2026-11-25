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

import { useState, useRef, useEffect, useMemo } from "react";
import { useSearchParams } from "react-router-dom";
import {
  Brain,
  AlertTriangle,
  FileText,
  Sparkles,
  Tag,
  History,
  Link2,
  Trash2,
  Pencil,
  Check,
  Eraser,
  Wrench,
  Clock,
  ShieldCheck,
  AlertCircle,
} from "lucide-react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import remarkBreaks from "remark-breaks";
import { StatCard } from "@/components/ui/stat-card";
import { Button } from "@/components/ui/button";
import { useConfirm } from "@/components/ui/confirm-dialog";
import { useToast } from "@/components/ui/toast";
import { Tip } from "@/components/ui/tooltip";
import { AiDisabledTip } from "@/components/ui/ai-disabled";
import { DataTable, TOOLBAR_CONTROL_HEIGHT, type DataTableColumn, type DataTableFilter } from "@/components/ui/data-table";
import {
  useAgentMemories, useSearchAgentMemory,
  useUpdateMemoryEntry, useDeleteMemoryEntry, useClearAgentMemory,
  useAgentMemoryHealth, useTriggerMemoryMaintenance,
} from "@/hooks/api/useMemoryApi";
import { useAuth } from "@/hooks/useAuth";
import { useAiActionDisabled } from "@/hooks/useAiDisabled";
import { MEMORY_SOURCE_LABELS, MEMORY_TYPE_CONFIG } from "@/lib/constants";
import { matchesFilters, readTableState, storedPageSize, type DataTableSort } from "@/lib/data-table-state";
import { cn } from "@/lib/utils";
import { safeHttpUrl } from "@/lib/safe-url";
import { useFormatter } from "@/i18n";
import type { Agent, AgentMemoryEntry } from "@/types";
import { formatShortDateTimeIntl, formatTimeAgo } from "@/lib/relative-time";
import { MemoryConsolidationsCard } from "@/components/memory/MemoryConsolidationsCard";
import { useAgentMemoryConsolidations, useUndoAgentMemoryConsolidation } from "@/hooks/api/useMemoryConsolidationsApi";
import { Spinner } from "@filigran/design-system";

/** The Memory Steward's changes to the agent's own lane, for its managers (#8258). */
function AgentMemoryConsolidations({ agentId }: { agentId: string }) {
  const { data } = useAgentMemoryConsolidations(agentId);
  const undo = useUndoAgentMemoryConsolidation(agentId);
  const [undoingId, setUndoingId] = useState<string | null>(null);
  if (!data || data.consolidations.length === 0) return null;
  return (
    <MemoryConsolidationsCard
      items={data.consolidations}
      retentionDays={data.retention_days}
      showAgent={false}
      aiGated
      undoingId={undoingId}
      onUndo={(id, callbacks) => {
        setUndoingId(id);
        undo.mutate(id, { ...callbacks, onSettled: () => setUndoingId(null) });
      }}
    />
  );
}

// ---------------------------------------------------------------------------
// Compact markdown components — styled for memory entries (smaller scale)
// ---------------------------------------------------------------------------
const markdownComponents = {
  p({ children }: { children?: React.ReactNode }) {
    return <p className="mb-2 last:mb-0 leading-relaxed">{children}</p>;
  },
  h1({ children }: { children?: React.ReactNode }) {
    return <h1 className="content-base font-bold mb-2 mt-3 first:mt-0 text-default-primary">{children}</h1>;
  },
  h2({ children }: { children?: React.ReactNode }) {
    return <h2 className="text-[13px] font-bold mb-1.5 mt-2.5 first:mt-0 text-default-primary">{children}</h2>;
  },
  h3({ children }: { children?: React.ReactNode }) {
    return <h3 className="content-compact font-semibold mb-1.5 mt-2 first:mt-0 text-default-primary">{children}</h3>;
  },
  ul({ children }: { children?: React.ReactNode }) {
    return (
      <ul className="mb-2 ml-1 space-y-0.5 [&>li]:relative [&>li]:pl-4 [&>li]:before:absolute [&>li]:before:left-0 [&>li]:before:top-[0.55em] [&>li]:before:h-1 [&>li]:before:w-1 [&>li]:before:rounded-full [&>li]:before:bg-accent/40">
        {children}
      </ul>
    );
  },
  ol({ children }: { children?: React.ReactNode }) {
    return (
      <ol className="mb-2 ml-1 list-decimal pl-4 space-y-0.5 marker:text-accent/50 marker:font-mono-plex marker:content-caption">
        {children}
      </ol>
    );
  },
  li({ children }: { children?: React.ReactNode }) {
    return <li className="leading-relaxed">{children}</li>;
  },
  strong({ children }: { children?: React.ReactNode }) {
    return <strong className="font-semibold text-default-primary">{children}</strong>;
  },
  em({ children }: { children?: React.ReactNode }) {
    return <em className="italic text-default-secondary">{children}</em>;
  },
  code({ children }: { children?: React.ReactNode }) {
    return (
      <code className="rounded-sm bg-elevation-hover px-1 py-0.5 text-[11px] font-mono-plex text-accent/80">
        {children}
      </code>
    );
  },
  blockquote({ children }: { children?: React.ReactNode }) {
    return (
      <blockquote className="my-2 border-l-2 border-accent/30 bg-accent/[0.03] pl-3 pr-2 py-1.5 text-default-secondary/90 italic">
        {children}
      </blockquote>
    );
  },
  hr() {
    return <hr className="my-3 border-elevation-subtle-soft" />;
  },
  a({ href, children }: { href?: string; children?: React.ReactNode }) {
    // A memory is written by the agent from what it read: a link that is not
    // a web address keeps its text and loses its target.
    const safe = safeHttpUrl(href);
    if (!safe) return <span>{children}</span>;
    return (
      <a href={safe} target="_blank" rel="noopener noreferrer" className="text-accent hover:text-accent/80 underline underline-offset-2 decoration-accent/30">
        {children}
      </a>
    );
  },
  table({ children }: { children?: React.ReactNode }) {
    return (
      <div className="my-2 overflow-x-auto rounded-sm">
        <table className="w-full text-[11px]">{children}</table>
      </div>
    );
  },
  thead({ children }: { children?: React.ReactNode }) {
    return <thead className="bg-elevation-hover border-b border-elevation-subtle-soft">{children}</thead>;
  },
  th({ children }: { children?: React.ReactNode }) {
    return <th className="px-2 py-1.5 text-left content-caption font-semibold uppercase tracking-wider text-default-secondary">{children}</th>;
  },
  td({ children }: { children?: React.ReactNode }) {
    return <td className="px-2 py-1.5 border-t border-elevation-subtle-soft">{children}</td>;
  },
};

// ---------------------------------------------------------------------------
// Shared memory display constants — imported from lib/constants.ts
// ---------------------------------------------------------------------------

// ---------------------------------------------------------------------------
// Memory Tab — persistent agent memory with search
// ---------------------------------------------------------------------------
const PAGE_SIZE = 25;
const STORAGE_KEY = "agent-memory";
const URL_KEY = "mem_";
const MEANING_PARAM = `${URL_KEY}meaning`;
const DEFAULT_SORT: DataTableSort = { key: "created_at", direction: "desc" };
/** The table's sortable columns and the API's `sort` value for each (server mode: the API orders). */
const SORT_PARAM: Record<string, "date" | "type"> = { created_at: "date", memory_type: "type" };
const MEMORY_TYPES: AgentMemoryEntry["memory_type"][] = ["fact", "learned", "state", "consolidated", "style", "preference", "tradecraft"];
/** The semantic search answers its closest entries, at most this many. */
const MEANING_LIMIT = 50;

const typeConfigOf = (type: string) => MEMORY_TYPE_CONFIG[type] ?? MEMORY_TYPE_CONFIG.fact;
const typeLabelOf = (type: string) => MEMORY_TYPE_CONFIG[type]?.label ?? type;
const sourceLabelOf = (source: string) => MEMORY_SOURCE_LABELS[source]?.label ?? source;

/** The first line of a memory, without its markdown marks: what the one-line cell shows. */
function firstLine(content: string): string {
  const line = content.split("\n").find((text) => text.trim()) ?? "";
  return line.replace(/^[#>*\-\s]+/, "").replace(/[*_`]/g, "").trim();
}

/**
 * The agent's persistent memory as one data table (#9378 slice 4f): the API pages,
 * filters (type, source, state), searches (words of the content or the subject) and
 * sorts; "By meaning" asks the semantic search instead. A row opens in place on the
 * whole entry, rendered, where a manager edits it.
 */
export function MemoryTab({ agent, canManage }: { agent: Agent; canManage: boolean }) {
  const { t_i18n, intl } = useFormatter();
  const { isAdmin } = useAuth();
  const aiOff = useAiActionDisabled();
  const [openIds, setOpenIds] = useState<Set<string>>(new Set());
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editContent, setEditContent] = useState("");
  const editRef = useRef<HTMLTextAreaElement>(null);
  const [ConfirmEl, confirm] = useConfirm();
  const { toast } = useToast();

  // The table keeps its state in the URL (`mem_` keys): read it here to ask the API for that page.
  const [params, setParams] = useSearchParams();
  // The size the user kept (DataTable storageKey) is the default the API is asked with.
  const tableState = readTableState(params, URL_KEY, { sort: DEFAULT_SORT, pageSize: storedPageSize(STORAGE_KEY, PAGE_SIZE) });
  const sort = tableState.sort ?? DEFAULT_SORT;
  const words = tableState.search.trim();
  const stateFilter = tableState.filters.state ?? [];
  // The content of a company-managed agent's memory is for its administrators: no search reads it.
  const contentHidden = agent.is_company_managed && !isAdmin;
  const byMeaning = params.get(MEANING_PARAM) === "1" && !aiOff && !contentHidden;

  const listQuery = useAgentMemories(
    agent.id,
    {
      memory_type: tableState.filters.memory_type,
      source: tableState.filters.source,
      state: stateFilter.length === 1 ? (stateFilter[0] as "current" | "superseded") : undefined,
      q: contentHidden ? undefined : words || undefined,
      sort: SORT_PARAM[sort.key] ?? "date",
      direction: sort.direction,
      limit: tableState.pageSize,
      offset: tableState.page * tableState.pageSize,
    },
    { throwOnError: false },
  );
  const meaningQuery = useSearchAgentMemory(agent.id, byMeaning ? words : "", MEANING_LIMIT);

  // The counts of the cards, whatever the table shows: one row asked, its total read.
  const totalCount = useAgentMemories(agent.id, { limit: 1 }, { throwOnError: false }).data?.total_count ?? 0;
  const consolidatedCount = useAgentMemories(agent.id, { memory_type: ["consolidated"], limit: 1 }, { throwOnError: false }).data?.total_count ?? 0;
  const styleCount = useAgentMemories(agent.id, { memory_type: ["style", "preference"], limit: 1 }, { throwOnError: false }).data?.total_count ?? 0;
  const factCount = useAgentMemories(agent.id, { memory_type: ["fact", "learned"], limit: 1 }, { throwOnError: false }).data?.total_count ?? 0;

  const updateEntry = useUpdateMemoryEntry(agent.id);
  const deleteEntry = useDeleteMemoryEntry(agent.id);
  const clearAll = useClearAgentMemory(agent.id);
  const { data: health } = useAgentMemoryHealth(agent.id);
  const triggerMaintenance = useTriggerMemoryMaintenance(agent.id);

  // By meaning, the closest entries come in one list: the filters still narrow it here.
  const meaningRows = useMemo(() => {
    const found = meaningQuery.data ?? [];
    return found.filter((entry) =>
      matchesFilters(tableState.filters, (key) => {
        if (key === "memory_type") return [entry.memory_type];
        if (key === "source") return [entry.source];
        if (key === "state") return [entry.superseded ? "superseded" : "current"];
        return [];
      }),
    );
  }, [meaningQuery.data, tableState.filters]);
  const semantic = byMeaning && words.length > 0;
  const rows = semantic ? meaningRows : (listQuery.data?.entries ?? []);
  const total = semantic ? meaningRows.length : (listQuery.data?.total_count ?? 0);

  const toggleOpen = (id: string) => {
    setOpenIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const startEdit = (id: string, content: string) => {
    setOpenIds((prev) => new Set(prev).add(id));
    setEditingId(id);
    setEditContent(content);
  };

  const cancelEdit = () => {
    setEditingId(null);
    setEditContent("");
  };

  const saveEdit = () => {
    if (!editingId || !editContent.trim()) return;
    updateEntry.mutate(
      { entryId: editingId, content: editContent.trim() },
      {
        onSuccess: () => {
          toast({ title: t_i18n("Memory updated"), variant: "success" });
          cancelEdit();
        },
      },
    );
  };

  const handleDelete = async (id: string) => {
    if (await confirm({ title: "Delete memory entry?", description: "This will permanently remove this memory entry.", confirmLabel: "Delete" })) {
      deleteEntry.mutate(id, {
        onSuccess: () => toast({ title: t_i18n("Memory entry deleted"), variant: "success" }),
      });
    }
  };

  const handleClearAll = async () => {
    if (await confirm({ title: "Clear all memory?", description: "This will permanently delete all persistent memory entries and clear the Redis memory cache for this agent. This action cannot be undone.", confirmLabel: "Clear All" })) {
      clearAll.mutate(undefined, {
        onSuccess: () => toast({ title: t_i18n("All memory cleared"), variant: "success" }),
      });
    }
  };

  const toggleMeaning = () => {
    const next = new URLSearchParams(params);
    if (params.get(MEANING_PARAM) === "1") next.delete(MEANING_PARAM);
    else next.set(MEANING_PARAM, "1");
    setParams(next, { replace: true });
  };

  // Auto-focus textarea when entering edit mode
  useEffect(() => {
    if (editingId && editRef.current) {
      editRef.current.focus();
      editRef.current.setSelectionRange(editRef.current.value.length, editRef.current.value.length);
    }
  }, [editingId]);

  const columns = useMemo<DataTableColumn<AgentMemoryEntry>[]>(
    () => [
      {
        key: "created_at",
        header: "When",
        hideable: true,
        width: 128,
        cell: (mem) => (
          <Tip content={mem.created_at ? formatShortDateTimeIntl(intl, mem.created_at) : undefined}>
            <span className="whitespace-nowrap text-default-secondary">{mem.created_at ? formatTimeAgo(intl, mem.created_at) : "-"}</span>
          </Tip>
        ),
        sortValue: (mem) => mem.created_at,
        csv: (mem) => mem.created_at,
      },
      {
        key: "memory_type",
        header: "Type",
        hideable: true,
        width: 136,
        cell: (mem) => {
          const config = typeConfigOf(mem.memory_type);
          return (
            <span className={cn("inline-flex items-center gap-1 whitespace-nowrap rounded-sm border border-transparent px-2 py-0.5 content-caption font-medium", config.bg, config.text)}>
              <config.icon className="h-3 w-3 shrink-0" />
              {t_i18n(typeLabelOf(mem.memory_type))}
            </span>
          );
        },
        sortValue: (mem) => mem.memory_type,
        csv: (mem) => t_i18n(typeLabelOf(mem.memory_type)),
      },
      {
        key: "content",
        header: "Memory",
        cell: (mem) => {
          if (mem.restricted) return <span className="text-default-secondary">{t_i18n("Restricted")}</span>;
          const line = firstLine(mem.content);
          return (
            <span className="flex min-w-0 items-center gap-2">
              {mem.subject_key && (
                <Tip content={t_i18n("The object this entry describes. A newer state entry on the same subject replaces it.")}>
                  <span className="inline-flex min-w-0 max-w-[200px] shrink-0 items-center gap-1 font-mono-plex content-compact text-default-secondary">
                    <Link2 className="h-3 w-3 shrink-0" />
                    <span className="truncate">{mem.subject_key}</span>
                  </span>
                </Tip>
              )}
              <span className="min-w-0 truncate" title={mem.content}>{line}</span>
              {mem.superseded && (
                <Tip content={t_i18n("This entry no longer holds. It stays searchable and is never added to the agent's context.")}>
                  <span className="inline-flex shrink-0 items-center gap-1 whitespace-nowrap rounded-sm border border-transparent bg-elevation-highlight px-2 py-0.5 content-caption font-medium text-default-secondary">
                    <History className="h-3 w-3" />
                    {t_i18n("Superseded")}
                  </span>
                </Tip>
              )}
              {mem.score !== undefined && mem.score > 0 && (
                <span className="shrink-0 whitespace-nowrap font-mono-plex content-caption text-default-secondary">
                  {t_i18n("{percent}% match", { percent: Math.round(mem.score * 100) })}
                </span>
              )}
            </span>
          );
        },
        csv: (mem) => mem.content,
      },
      {
        key: "subject_key",
        header: "Subject",
        exportOnly: true,
        cell: () => null,
        csv: (mem) => mem.subject_key ?? "",
      },
      {
        key: "source",
        header: "Source",
        hideable: true,
        width: 176,
        hideBelow: "4xl",
        cell: (mem) => {
          const SourceIcon = MEMORY_SOURCE_LABELS[mem.source]?.icon ?? Brain;
          return (
            <span className="inline-flex min-w-0 items-center gap-1 whitespace-nowrap text-default-secondary">
              <SourceIcon className="h-3 w-3 shrink-0" />
              <span className="truncate">{t_i18n(sourceLabelOf(mem.source))}</span>
            </span>
          );
        },
        csv: (mem) => t_i18n(sourceLabelOf(mem.source)),
      },
      {
        key: "state",
        header: "State",
        exportOnly: true,
        cell: () => null,
        csv: (mem) => t_i18n(mem.superseded ? "Superseded" : "Current"),
      },
    ],
    [intl, t_i18n],
  );

  const filters = useMemo<DataTableFilter<AgentMemoryEntry>[]>(
    () => [
      {
        key: "memory_type",
        label: "Type",
        options: MEMORY_TYPES.map((type) => ({ value: type, label: t_i18n(typeLabelOf(type)) })),
      },
      {
        key: "source",
        label: "Source",
        options: Object.entries(MEMORY_SOURCE_LABELS).map(([value, config]) => ({ value, label: t_i18n(config.label) })),
      },
      {
        key: "state",
        label: "State",
        options: [
          { value: "current", label: t_i18n("Current") },
          { value: "superseded", label: t_i18n("Superseded") },
        ],
      },
    ],
    [t_i18n],
  );

  const renderEntry = (mem: AgentMemoryEntry) => {
    if (!openIds.has(mem.id) || mem.restricted) return null;
    if (editingId === mem.id) {
      return (
        <div className="space-y-2 py-1">
          <textarea
            ref={editRef}
            aria-label={t_i18n("Edit memory")}
            value={editContent}
            onChange={(e) => setEditContent(e.target.value)}
            className="w-full min-h-[100px] rounded-sm border border-elevation-subtle-soft bg-elevation-default px-3 py-2 content-compact text-default-secondary leading-relaxed resize-y focus:outline-hidden focus-visible:ring-2 focus-visible:ring-focus"
            onKeyDown={(e) => {
              if (e.key === "Escape") cancelEdit();
              if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) saveEdit();
            }}
          />
          <div className="flex items-center gap-2">
            <Button size="sm" className="gap-1" onClick={saveEdit} disabled={updateEntry.isPending || !editContent.trim()} aria-busy={updateEntry.isPending || undefined}>
              {updateEntry.isPending && <Spinner size="sm" tone="inherit" />}{!updateEntry.isPending && <Check className="h-3.5 w-3.5" />}
              {t_i18n("Save")}
            </Button>
            <Button variant="ghost" size="sm" onClick={cancelEdit}>
              {t_i18n("Cancel")}
            </Button>
            <span className="ml-auto content-caption text-default-secondary">{t_i18n("Ctrl+Enter to save, Esc to cancel")}</span>
          </div>
        </div>
      );
    }
    return (
      <div className="content-compact text-default-secondary leading-relaxed memory-markdown py-1">
        <ReactMarkdown remarkPlugins={[remarkGfm, remarkBreaks]} components={markdownComponents}>
          {mem.content}
        </ReactMarkdown>
      </div>
    );
  };

  const meaningToggle = contentHidden ? null : (
    <AiDisabledTip>
      <Button
        variant={byMeaning ? "secondary" : "ghost"}
        size="sm"
        className={cn(TOOLBAR_CONTROL_HEIGHT, "gap-1.5")}
        aria-pressed={byMeaning}
        disabled={aiOff}
        onClick={toggleMeaning}
      >
        <Sparkles className="h-3.5 w-3.5" />
        {t_i18n("By meaning")}
      </Button>
    </AiDisabledTip>
  );

  return (
    <div className="space-y-4">
      {ConfirmEl}
      {!agent.memory_short_term_enabled && !agent.memory_medium_term_enabled && !agent.memory_persistent_enabled && !agent.memory_user_enabled && (
        <div className="flex items-center gap-2 content-compact text-feedback-warning-tertiary bg-feedback-alert-primary/5 rounded-sm px-3 py-2">
          <AlertTriangle className="h-3.5 w-3.5 shrink-0" />
          {t_i18n("All memory tiers are currently disabled for this agent.")}
        </div>
      )}

      {/* Memory stats + Health */}
      {totalCount > 0 && (
        <div className="space-y-3">
          <div className="grid gap-3 sm:grid-cols-4">
            <StatCard icon={Brain} label="Total Memories" value={totalCount} subtitle="Accumulated entries" />
            <StatCard icon={FileText} label="Facts" value={health?.fact_count ?? factCount} subtitle={health ? t_i18n("Cap: {count}", { count: health.max_facts_per_agent }) : "Extracted facts"} />
            <StatCard icon={Sparkles} label="Consolidated" value={consolidatedCount} subtitle="LLM summaries" />
            <StatCard icon={Tag} label="Style / Pref" value={styleCount} subtitle="Learned patterns" />
          </div>

          {/* Health indicators */}
          {health && (health.stale_count > 0 || health.over_cap > 0 || health.no_embedding_count > 0) && (
            <div className="flex flex-wrap items-center gap-2 content-compact">
              {health.stale_count > 0 && (
                <span className="inline-flex items-center gap-1 rounded-full bg-feedback-alert-primary/10 text-feedback-warning-tertiary px-2.5 py-1 border border-feedback-alert-primary/20">
                  <Clock className="h-3 w-3" />
                  {t_i18n("{count} stale (>{days}d)", { count: health.stale_count, days: health.retention_days })}
                </span>
              )}
              {health.over_cap > 0 && (
                <span className="inline-flex items-center gap-1 rounded-full bg-feedback-error-primary/10 text-feedback-error-primary px-2.5 py-1 border border-feedback-error-primary/20">
                  <AlertCircle className="h-3 w-3" />
                  {t_i18n("{count} over cap", { count: health.over_cap })}
                </span>
              )}
              {health.no_embedding_count > 0 && (
                <span className="inline-flex items-center gap-1 rounded-full bg-elevation-highlight text-default-secondary px-2.5 py-1 border border-elevation-subtle-soft">
                  <AlertTriangle className="h-3 w-3" />
                  {t_i18n("{count} without embedding", { count: health.no_embedding_count })}
                </span>
              )}
            </div>
          )}
          {health && health.stale_count === 0 && health.over_cap === 0 && health.no_embedding_count === 0 && (
            <div className="flex items-center gap-1.5 content-compact text-feedback-success-primary">
              <ShieldCheck className="h-3 w-3" />
              {t_i18n("Memory healthy: deduplication active, retention {days}d, cap {cap}", { days: health.retention_days, cap: health.max_facts_per_agent })}
            </div>
          )}
        </div>
      )}

      {canManage && totalCount > 0 && (
        <div className="flex items-center justify-end gap-1.5">
          <Button
            variant="ghost"
            size="sm"
            className="gap-1.5 text-default-secondary hover:text-default-primary"
            onClick={() => {
              triggerMaintenance.mutate(undefined, {
                onSuccess: (result) => toast({
                  title: t_i18n("Maintenance complete"),
                  description: result.message,
                  variant: "success",
                }),
              });
            }}
            disabled={triggerMaintenance.isPending} aria-busy={triggerMaintenance.isPending || undefined}>
            {triggerMaintenance.isPending && <Spinner size="sm" tone="inherit" />}{!triggerMaintenance.isPending && <Wrench className="h-3.5 w-3.5" />}
            {t_i18n("Optimize")}
          </Button>
          <Button
            variant="ghost"
            size="sm"
            className="gap-1.5 text-feedback-error-primary hover:text-feedback-error-primary hover:bg-feedback-error-primary/10"
            onClick={handleClearAll}
            disabled={clearAll.isPending} aria-busy={clearAll.isPending || undefined}>
            {clearAll.isPending && <Spinner size="sm" tone="inherit" />}{!clearAll.isPending && <Eraser className="h-3.5 w-3.5" />}
            {t_i18n("Clear All")}
          </Button>
        </div>
      )}

      {canManage && <AgentMemoryConsolidations agentId={agent.id} />}

      <DataTable
        rows={rows}
        columns={columns}
        rowKey={(mem) => mem.id}
        server={{ total }}
        // Every entry of the agent (the cards' count): "n of N" once a filter or a search narrows it (#12039).
        totalAll={totalCount}
        paginate={!semantic}
        defaultSort={DEFAULT_SORT}
        searchPlaceholder={t_i18n("Search the memories...")}
        filters={filters}
        exportName="agent-memory"
        urlKey={URL_KEY}
        storageKey={STORAGE_KEY}
        defaultPageSize={PAGE_SIZE}
        layout="fixed"
        toolbar={meaningToggle}
        isLoading={semantic ? meaningQuery.isFetching && !meaningQuery.data : listQuery.isLoading}
        isError={semantic ? meaningQuery.isError : listQuery.isError}
        errorLabel={contentHidden && words ? t_i18n("Content search is restricted to administrators for company-managed agents.") : undefined}
        onRetry={() => void (semantic ? meaningQuery.refetch() : listQuery.refetch())}
        onRowClick={(mem) => toggleOpen(mem.id)}
        expandedRow={renderEntry}
        rowActions={
          canManage
            ? (mem) => (
                <div className="flex items-center gap-0.5">
                  <Button aria-label={t_i18n("Edit memory")} variant="ghost" size="icon" className="h-7 w-7" disabled={mem.restricted} onClick={() => startEdit(mem.id, mem.content)}>
                    <Pencil className="h-3.5 w-3.5" />
                  </Button>
                  <Button aria-label={t_i18n("Delete memory")} variant="ghost" size="icon" className="h-7 w-7 hover:text-feedback-error-primary" onClick={() => handleDelete(mem.id)}>
                    <Trash2 className="h-3.5 w-3.5" />
                  </Button>
                </div>
              )
            : undefined
        }
        emptyLabel={
          agent.memory_short_term_enabled || agent.memory_medium_term_enabled || agent.memory_persistent_enabled || agent.memory_user_enabled
            ? t_i18n("As this agent has conversations and runs assignments, it will accumulate persistent memories that appear here.")
            : t_i18n("Enable memory tiers in the Configuration tab to start building persistent agent knowledge.")
        }
        noResultsLabel={t_i18n("No matching memories")}
        rowTestId="memory-row"
      />
    </div>
  );
}
