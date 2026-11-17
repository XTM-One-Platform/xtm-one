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

import { useMemo, useState } from "react";
import {
  Brain,
  Check,
  ChevronRight,
  GitCompare,
  Lock,
  Repeat,
  RotateCcw,
  Shield,
  Thermometer,
  Trash2,
  Unlock,
  User,
} from "lucide-react";
import type { LucideIcon } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { SearchInput } from "@/components/ui/search-input";
import { Tip } from "@/components/ui/tooltip";
import { useConfirm } from "@/components/ui/confirm-dialog";
import { useToast } from "@/components/ui/toast";
import { VersionDiffDialog } from "@/components/ui/version-diff-dialog";
import {
  useAgentConfigVersionDiff,
  useAgentConfigVersions,
  useAgentPersonaVersionDiff,
  useAgentPersonaVersions,
  useDeleteAgentConfigVersion,
  useDeleteAgentPersonaVersion,
  useLockAgentConfigVersion,
  useLockAgentPersonaVersion,
  useRollbackAgentConfigVersion,
  useRollbackAgentPersonaVersion,
  useUnlockAgentConfigVersion,
  useUnlockAgentPersonaVersion,
} from "@/hooks/api/useAgentApi";
import { cn, formatDateTime } from "@/lib/utils";
import { useFormatter } from "@/i18n";
import type { Agent, AgentConfigVersionField } from "@/types";
import { useAiActionDisabled } from "@/hooks/useAiDisabled";
import { useFeatureFlag } from "@/hooks/useFeatureFlag";
import { PersonaImprovementsCard } from "./PersonaImprovementsCard";

// ── Constants ─────────────────────────────────────────────────────────────────

// Per-kind page size for the "Load more" pattern.  We bump each kind's
// fetch limit by ``PAGE_SIZE`` on every Load-more click so older
// persona/config versions stay reachable from the UI on long histories
// (default retention is unlimited, so a popular agent can easily
// accumulate >100 versions per kind).  Kept smaller than the prompt
// tab's 20 because the agent tab displays five kinds simultaneously
// and we don't want to over-fetch on the first paint.
const PAGE_SIZE = 20;

type VersionKind = "persona" | AgentConfigVersionField;

interface KindMeta {
  label: string;
  icon: LucideIcon;
  badgeClass: string;
  dotClass: string;
}

const KIND_META: Record<VersionKind, KindMeta> = {
  persona: {
    label: "Persona",
    icon: User,
    badgeClass: "bg-filigran-ia-primary/10 text-filigran-ia-primary border-filigran-ia-primary/20",
    dotClass: "bg-filigran-ia-primary",
  },
  guardrails: {
    label: "Guardrails",
    icon: Shield,
    badgeClass: "bg-accent/10 text-accent/80 border-accent/20",
    dotClass: "bg-accent",
  },
  temperature: {
    label: "Temperature",
    icon: Thermometer,
    badgeClass: "bg-accent/10 text-accent/80 border-accent/20",
    dotClass: "bg-accent",
  },
  reasoning_effort: {
    label: "Reasoning Effort",
    icon: Brain,
    badgeClass: "bg-accent/10 text-accent/80 border-accent/20",
    dotClass: "bg-accent",
  },
  max_iterations: {
    label: "Max Iterations",
    icon: Repeat,
    badgeClass: "bg-accent/10 text-accent/80 border-accent/20",
    dotClass: "bg-accent",
  },
};

const ALL_KINDS: VersionKind[] = [
  "persona",
  "guardrails",
  "temperature",
  "reasoning_effort",
  "max_iterations",
];

interface UnifiedVersion {
  id: string;
  kind: VersionKind;
  version_number: number;
  author_name: string | null;
  commit_message: string | null;
  is_active: boolean;
  is_locked: boolean;
  created_at: string;
}

interface SelectedVersion {
  kind: VersionKind;
  version: number;
}

// ── Diff sub-component ────────────────────────────────────────────────────────

function DiffDialogFetcher({
  agentId,
  kind,
  fromVersion,
  toVersion,
  open,
  onOpenChange,
}: {
  agentId: string;
  kind: VersionKind | null;
  fromVersion: number | null;
  toVersion: number | null;
  open: boolean;
  onOpenChange: (o: boolean) => void;
}) {
  const { t_i18n } = useFormatter();
  const isPersona = kind === "persona";
  const configField = (!isPersona && kind != null ? kind : "guardrails") as AgentConfigVersionField;

  const personaDiff = useAgentPersonaVersionDiff(
    agentId,
    isPersona ? fromVersion : null,
    isPersona ? toVersion : null,
    open && isPersona,
  );
  const configDiff = useAgentConfigVersionDiff(
    agentId,
    configField,
    !isPersona ? fromVersion : null,
    !isPersona ? toVersion : null,
    open && !isPersona,
  );

  const diff = isPersona ? (personaDiff.data ?? null) : (configDiff.data ?? null);
  const meta = kind ? KIND_META[kind] : null;

  return (
    <VersionDiffDialog
      open={open}
      onOpenChange={onOpenChange}
      diff={diff}
      title={meta ? t_i18n("{kind} diff", { kind: t_i18n(meta.label) }) : t_i18n("Diff")}
    />
  );
}

// ── Main tab ──────────────────────────────────────────────────────────────────

export function VersioningTab({
  agent,
  canManage,
  isAdmin,
}: {
  agent: Agent;
  canManage: boolean;
  isAdmin?: boolean;
}) {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const [ConfirmEl, confirm] = useConfirm();
  // A persona rewrite is measured by an evaluation run: off with evaluation.
  const evaluation = useFeatureFlag("EVALUATION");

  const [search, setSearch] = useState("");

  // Comparison: up to 2 items of the same kind
  const [selected, setSelected] = useState<SelectedVersion[]>([]);
  const [diffOpen, setDiffOpen] = useState(false);

  // Rollback dialog
  const [rollbackTarget, setRollbackTarget] = useState<SelectedVersion | null>(null);
  const [rollbackMessage, setRollbackMessage] = useState("");

  // Tree expand/collapse per kind (all expanded by default)
  const [expandedKinds, setExpandedKinds] = useState<Set<VersionKind>>(new Set(ALL_KINDS));

  // Per-kind visible-row count for the "Load more" pattern.  Each
  // "Load more" click increments the kind's count by ``PAGE_SIZE``
  // and refetches with the larger limit (the query is keyed on
  // ``limit``).  Starts at ``PAGE_SIZE`` per kind so the initial
  // paint of five kinds fetches at most 5 ├ù PAGE_SIZE rows.
  const [visibleCounts, setVisibleCounts] = useState<Record<VersionKind, number>>(
    () =>
      Object.fromEntries(ALL_KINDS.map((k) => [k, PAGE_SIZE])) as Record<
        VersionKind,
        number
      >,
  );

  const loadMore = (kind: VersionKind) =>
    setVisibleCounts((prev) => ({ ...prev, [kind]: prev[kind] + PAGE_SIZE }));

  // ── Fetch all kinds ────────────────────────────────────────────────────────
  const personaQ    = useAgentPersonaVersions(agent.id,  { limit: visibleCounts.persona,          offset: 0 });
  const guardrailsQ = useAgentConfigVersions(agent.id, "guardrails",       { limit: visibleCounts.guardrails,       offset: 0 });
  const tempQ       = useAgentConfigVersions(agent.id, "temperature",      { limit: visibleCounts.temperature,      offset: 0 });
  const reasoningQ  = useAgentConfigVersions(agent.id, "reasoning_effort", { limit: visibleCounts.reasoning_effort, offset: 0 });
  const iterQ       = useAgentConfigVersions(agent.id, "max_iterations",   { limit: visibleCounts.max_iterations,   offset: 0 });

  const kindTotals = useMemo<Record<VersionKind, number>>(
    () =>
      ({
        persona: personaQ.data?.total ?? 0,
        guardrails: guardrailsQ.data?.total ?? 0,
        temperature: tempQ.data?.total ?? 0,
        reasoning_effort: reasoningQ.data?.total ?? 0,
        max_iterations: iterQ.data?.total ?? 0,
      }) as Record<VersionKind, number>,
    [
      personaQ.data?.total,
      guardrailsQ.data?.total,
      tempQ.data?.total,
      reasoningQ.data?.total,
      iterQ.data?.total,
    ],
  );

  // ── Mutations ──────────────────────────────────────────────────────────────
  const rollbackPersona  = useRollbackAgentPersonaVersion();
  const aiOff = useAiActionDisabled();
  const rollbackConfig   = useRollbackAgentConfigVersion();
  const deletePersona    = useDeleteAgentPersonaVersion();
  const deleteConfig     = useDeleteAgentConfigVersion();
  const lockPersona      = useLockAgentPersonaVersion();
  const lockConfig       = useLockAgentConfigVersion();
  const unlockPersona    = useUnlockAgentPersonaVersion();
  const unlockConfig     = useUnlockAgentConfigVersion();

  // ── Per-kind grouped data, sorted by version desc ──────────────────────────
  const kindVersions = useMemo<Record<VersionKind, UnifiedVersion[]>>(() => {
    const toUnified = (kind: VersionKind): UnifiedVersion[] =>
      ((kind === "persona"
        ? personaQ.data?.items
        : kind === "guardrails"
        ? guardrailsQ.data?.items
        : kind === "temperature"
        ? tempQ.data?.items
        : kind === "reasoning_effort"
        ? reasoningQ.data?.items
        : iterQ.data?.items) ?? []
      ).map((v) => ({ ...v, kind }));

    return Object.fromEntries(
      ALL_KINDS.map((k) => [
        k,
        toUnified(k).sort((a, b) => b.version_number - a.version_number),
      ]),
    ) as Record<VersionKind, UnifiedVersion[]>;
  }, [personaQ.data, guardrailsQ.data, tempQ.data, reasoningQ.data, iterQ.data]);

  // ── Handlers ───────────────────────────────────────────────────────────────

  const toggleSelect = (kind: VersionKind, version: number) => {
    const alreadyIdx = selected.findIndex((s) => s.kind === kind && s.version === version);

    if (alreadyIdx < 0 && selected.length === 1 && selected[0].kind !== kind) {
      toast({ title: t_i18n("Can only compare versions of the same type"), variant: "error" });
      return;
    }

    setSelected((prev) => {
      const idx = prev.findIndex((s) => s.kind === kind && s.version === version);
      if (idx >= 0) return prev.filter((_, i) => i !== idx);
      if (prev.length >= 2) return [prev[1], { kind, version }];
      return [...prev, { kind, version }];
    });
  };

  const toggleKind = (kind: VersionKind) => {
    setExpandedKinds((prev) => {
      const next = new Set(prev);
      if (next.has(kind)) next.delete(kind);
      else next.add(kind);
      return next;
    });
  };

  const openDiff = () => {
    if (selected.length < 2 || selected[0].kind !== selected[1].kind) {
      toast({ title: t_i18n("Select two versions of the same type to compare"), variant: "error" });
      return;
    }
    if (selected[0].version === selected[1].version) {
      toast({ title: t_i18n("Select two distinct versions"), variant: "error" });
      return;
    }
    setDiffOpen(true);
  };

  const handleRollbackConfirm = () => {
    if (!rollbackTarget) return;
    const { kind, version } = rollbackTarget;
    const onSuccess = () => {
      toast({ title: t_i18n("{kind} rolled back to v{version}", { kind: t_i18n(KIND_META[kind].label), version }), variant: "success" });
      setRollbackTarget(null);
      setRollbackMessage("");
    };
    const onError = () => toast({ title: t_i18n("Rollback failed"), variant: "error" });

    if (kind === "persona") {
      rollbackPersona.mutate(
        { agentId: agent.id, version, commit_message: rollbackMessage || undefined },
        { onSuccess, onError },
      );
    } else {
      rollbackConfig.mutate(
        { agentId: agent.id, fieldName: kind, version, commit_message: rollbackMessage || undefined },
        { onSuccess, onError },
      );
    }
  };

  const handleDelete = async (kind: VersionKind, version: number, isLocked: boolean) => {
    if (isLocked) {
      toast({ title: t_i18n("Unlock this version before deleting it"), variant: "error" });
      return;
    }
    const ok = await confirm({
      title: "Delete version",
      description: t_i18n("Permanently delete {kind} v{version}? This cannot be undone.", { kind: t_i18n(KIND_META[kind].label), version }),
      confirmLabel: "Delete",
      variant: "danger",
    });
    if (!ok) return;

    const opts = {
      onSuccess: () => toast({ title: t_i18n("v{version} deleted", { version }), variant: "success" as const }),
      onError: () => toast({ title: t_i18n("Delete failed"), variant: "error" as const }),
    };
    if (kind === "persona") {
      deletePersona.mutate({ agentId: agent.id, version }, opts);
    } else {
      deleteConfig.mutate({ agentId: agent.id, fieldName: kind, version }, opts);
    }
  };

  const handleLockToggle = (kind: VersionKind, version: number, isLocked: boolean) => {
    const onSuccess = () =>
      toast({ title: t_i18n(isLocked ? "v{version} unlocked" : "v{version} locked", { version }), variant: "success" });
    const onError = () => toast({ title: t_i18n("Failed"), variant: "error" });

    if (kind === "persona") {
      if (isLocked) unlockPersona.mutate({ agentId: agent.id, version }, { onSuccess, onError });
      else          lockPersona.mutate  ({ agentId: agent.id, version }, { onSuccess, onError });
    } else {
      if (isLocked) unlockConfig.mutate({ agentId: agent.id, fieldName: kind, version }, { onSuccess, onError });
      else          lockConfig.mutate  ({ agentId: agent.id, fieldName: kind, version }, { onSuccess, onError });
    }
  };

  const isLoading =
    personaQ.isLoading || guardrailsQ.isLoading || tempQ.isLoading ||
    reasoningQ.isLoading || iterQ.isLoading;

  const diffKind =
    selected.length === 2 && selected[0].kind === selected[1].kind
      ? selected[0].kind
      : null;

  const matchesSearch = (v: UnifiedVersion) => {
    if (!search.trim()) return true;
    const q = search.trim().toLowerCase();
    return !!(v.commit_message?.toLowerCase().includes(q) || v.author_name?.toLowerCase().includes(q));
  };

  // Only show kinds that have at least one version
  const activeKinds = ALL_KINDS.filter((k) => kindVersions[k].length > 0);

  // ── Render ─────────────────────────────────────────────────────────────────

  return (
    <div className="space-y-3">
      {evaluation && (canManage || isAdmin) && <PersonaImprovementsCard agent={agent} />}
      {ConfirmEl}

      {/* Rollback dialog */}
      <Dialog
        open={rollbackTarget !== null}
        onOpenChange={(o) => { if (!o) { setRollbackTarget(null); setRollbackMessage(""); } }}
      >
        <DialogContent className="sm:max-w-md">
          <DialogHeader>
            <DialogTitle>
              {rollbackTarget ? t_i18n("Rollback {kind}", { kind: t_i18n(KIND_META[rollbackTarget.kind].label) }) : t_i18n("Rollback")}
            </DialogTitle>
            <DialogDescription>
              {t_i18n("Restore to v{version}. The current value is preserved in history.", { version: rollbackTarget?.version ?? "" })}
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-2 py-2">
            <Label className="content-base">{t_i18n("Commit message (optional)")}</Label>
            <Input aria-label={t_i18n("Commit message (optional)")}
              value={rollbackMessage}
              onChange={(e) => setRollbackMessage(e.target.value)}
              placeholder={t_i18n("e.g. Reverting accidental change")}
              className="h-8 content-base"
            />
          </div>
          <div className="flex justify-end gap-2 pt-2">
            <Button
              variant="ghost"
              size="sm"
              onClick={() => { setRollbackTarget(null); setRollbackMessage(""); }}
            >
              {t_i18n("Cancel")}
            </Button>
            <Button
              size="sm"
              className="bg-feedback-alert-primary hover:bg-feedback-alert-primary/90 text-on-feedback-fill"
              onClick={handleRollbackConfirm}
              disabled={rollbackPersona.isPending || rollbackConfig.isPending || aiOff}
            >
              <RotateCcw className="h-3.5 w-3.5 mr-1.5" />
              {t_i18n("Rollback")}
            </Button>
          </div>
        </DialogContent>
      </Dialog>

      {/* ── Toolbar: filter chips + search + compare ── */}
      <div className="flex items-center gap-2">
        {/* Free-text search */}
        <SearchInput
          value={search}
          onChange={setSearch}
          placeholder={t_i18n("Search commit or author...")}
          className="w-56"
        />

        <div className="ml-auto flex items-center gap-2">
          {selected.length > 0 && (
            <button
              type="button"
              onClick={() => setSelected([])}
              className="text-[11px] text-default-secondary hover:text-default-primary transition-colors"
            >
              {t_i18n("Clear selection")}
            </button>
          )}
          <Button
            variant="outline"
            className="gap-1.5 shrink-0"
            onClick={openDiff}
            disabled={selected.length < 2}
          >
            <GitCompare className="h-3.5 w-3.5" />
            {t_i18n("Compare")}
            {selected.length > 0 && (
              <span className="content-caption text-default-secondary">({selected.length}/2)</span>
            )}
          </Button>
        </div>
      </div>

      {/* ── Tree view ── */}
      {isLoading ? (
        <p className="content-base text-default-secondary p-2">{t_i18n("Loading...")}</p>
      ) : activeKinds.length === 0 ? (
        <p className="content-base text-default-secondary p-2">{t_i18n("No versioning entries yet.")}</p>
      ) : (
        <div className="space-y-2">
          {activeKinds.map((kind) => {
            const meta = KIND_META[kind];
            const Icon = meta.icon;
            const versions = kindVersions[kind];
            // HEAD = active version, fallback to highest version number
            const head = versions.find((v) => v.is_active) ?? versions[0];
            const history = versions.filter((v) => v !== head);
            const isExpanded = expandedKinds.has(kind);
            const filteredHistory = search.trim() ? history.filter(matchesSearch) : history;

            // If searching and neither head nor any history matches, skip this kind
            if (search.trim() && !matchesSearch(head) && filteredHistory.length === 0) return null;

            const headIsChecked = selected.some(
              (s) => s.kind === kind && s.version === head.version_number,
            );
            const headCanSelect =
              headIsChecked ||
              selected.length < 2 ||
              (selected.length === 1 && selected[0].kind === kind);

            return (
              <Card key={kind} className="glass-card border-elevation-subtle-soft overflow-hidden">
                <CardContent className="p-0">

                  {/* ── HEAD row (kind header) ── */}
                  <div
                    className="flex items-center gap-2.5 px-3 py-2.5 cursor-pointer hover:bg-elevation-hover transition-colors select-none"
                    onClick={() => toggleKind(kind)}
                  >
                    {/* Expand/collapse chevron */}
                    <ChevronRight
                      className={cn(
                        "h-3.5 w-3.5 text-default-secondary shrink-0 transition-transform duration-150",
                        isExpanded && history.length > 0 ? "rotate-90" : "",
                        history.length === 0 ? "opacity-20" : "",
                      )}
                    />

                    {/* Kind badge */}
                    <span
                      className={cn(
                        "inline-flex items-center gap-1 content-caption font-mono-plex border rounded-sm px-1.5 py-0.5 shrink-0",
                        meta.badgeClass,
                      )}
                    >
                      <Icon className="h-2.5 w-2.5 shrink-0" />
                      {t_i18n(meta.label)}
                    </span>

                    {/* HEAD badge + version */}
                    <Badge
                      variant="outline"
                      className="h-4 content-caption px-1.5 border-feedback-success-primary/40 text-feedback-success-primary font-mono-plex shrink-0"
                    >
                      HEAD
                    </Badge>
                    <span className="content-base font-mono-plex text-default-secondary shrink-0">
                      v{head.version_number}
                    </span>

                    {head.is_locked && (
                      <Badge
                        variant="outline"
                        className="h-4 content-caption px-1 border-feedback-alert-primary/40 text-feedback-warning-primary gap-1 shrink-0"
                      >
                        <Lock className="h-2.5 w-2.5" />
                        {t_i18n("Locked")}
                      </Badge>
                    )}

                    {head.commit_message && (
                      <span className="content-compact text-default-secondary truncate flex-1 min-w-0">
                        {head.commit_message}
                      </span>
                    )}

                    <span className="text-[11px] text-default-secondary shrink-0 ml-auto">
                      {formatDateTime(head.created_at)}
                      {head.author_name ? ` · ${head.author_name}` : ""}
                    </span>

                    {history.length > 0 && (
                      <span className="content-caption text-default-disabled font-mono-plex shrink-0">
                        +{history.length}
                      </span>
                    )}

                    {/* HEAD actions – stop propagation to avoid toggling expand */}
                    <div
                      className="flex items-center gap-1 shrink-0"
                      onClick={(e) => e.stopPropagation()}
                    >
                      <button
                        type="button"
                        onClick={() => toggleSelect(kind, head.version_number)}
                        disabled={!headCanSelect}
                        aria-label={`Select HEAD v${head.version_number} for comparison`}
                        className={cn(
                          "h-4 w-4 rounded-[3px] border transition-all flex items-center justify-center shrink-0",
                          headIsChecked
                            ? "bg-elevation-highlight border-elevation-default text-default-primary"
                            : headCanSelect
                            ? "border-elevation-subtle-soft hover:border-elevation-subtle"
                            : "border-elevation-subtle-soft opacity-30 cursor-not-allowed",
                        )}
                      >
                        {headIsChecked && <Check className="h-3 w-3" />}
                      </button>

                      {isAdmin && (
                        <Tip content={head.is_locked ? t_i18n("Unlock") : t_i18n("Lock")}>
                          <Button
                            size="sm"
                            variant="ghost"
                            className="h-7 w-7 p-0 text-default-secondary hover:text-default-primary"
                            onClick={() => handleLockToggle(kind, head.version_number, head.is_locked)} disabled={aiOff}>
                            {head.is_locked ? <Unlock className="h-3.5 w-3.5" /> : <Lock className="h-3.5 w-3.5" />}
                          </Button>
                        </Tip>
                      )}
                    </div>
                  </div>

                  {/* ── History commits tree ── */}
                  {isExpanded && filteredHistory.length > 0 && (
                    <div className="ml-7 border-l border-elevation-subtle-soft pb-1.5">
                      {filteredHistory.map((v) => {
                        const isChecked = selected.some(
                          (s) => s.kind === v.kind && s.version === v.version_number,
                        );
                        const canSelect =
                          isChecked ||
                          selected.length < 2 ||
                          (selected.length === 1 && selected[0].kind === v.kind);

                        return (
                          <div
                            key={v.id}
                            className="relative flex items-center gap-2.5 pl-6 pr-3 py-2 hover:bg-elevation-hover transition-colors group"
                          >
                            {/* Horizontal tree connector */}
                            <span className="absolute left-0 top-1/2 -translate-y-px w-5 border-t border-elevation-subtle-soft" />

                            {/* Commit dot */}
                            <span className="h-1.5 w-1.5 rounded-full bg-elevation-hover shrink-0 z-10" />

                            {/* Selection checkbox */}
                            <button
                              type="button"
                              onClick={() => toggleSelect(v.kind, v.version_number)}
                              disabled={!canSelect}
                              aria-label={`Select v${v.version_number} for comparison`}
                              className={cn(
                                "h-4 w-4 rounded-[3px] border transition-all flex items-center justify-center shrink-0",
                                isChecked
                                  ? "bg-elevation-highlight border-elevation-default text-default-primary"
                                  : canSelect
                                  ? "border-elevation-subtle-soft hover:border-elevation-subtle"
                                  : "border-elevation-subtle-soft opacity-30 cursor-not-allowed",
                              )}
                            >
                              {isChecked && <Check className="h-3 w-3" />}
                            </button>

                            {/* Content */}
                            <div className="min-w-0 flex-1">
                              <div className="flex items-center gap-2 flex-wrap">
                                <span className="content-base font-mono-plex shrink-0 text-default-secondary">
                                  v{v.version_number}
                                </span>
                                {v.is_locked && (
                                  <Badge
                                    variant="outline"
                                    className="h-4 content-caption px-1 border-feedback-alert-primary/40 text-feedback-warning-primary gap-1 shrink-0"
                                  >
                                    <Lock className="h-2.5 w-2.5" />
                                    {t_i18n("Locked")}
                                  </Badge>
                                )}
                                {v.commit_message && (
                                  <span className="content-compact text-default-secondary truncate">
                                    {v.commit_message}
                                  </span>
                                )}
                              </div>
                              <p className="text-[11px] text-default-secondary mt-0.5">
                                {formatDateTime(v.created_at)}
                                {v.author_name ? ` · ${v.author_name}` : ""}
                              </p>
                            </div>

                            {/* Actions */}
                            <div className="flex items-center gap-1 shrink-0 opacity-0 group-hover:opacity-100 transition-opacity">
                              {canManage && (
                                <Tip content={t_i18n("Rollback to v{version}", { version: v.version_number })}>
                                  <Button aria-label={t_i18n("Rollback to v{version}", { version: v.version_number })}
                                    size="sm"
                                    variant="ghost"
                                    className="h-7 w-7 p-0 text-feedback-warning-primary hover:text-feedback-warning-primary"
                                    onClick={() => {
                                      setRollbackTarget({ kind: v.kind, version: v.version_number });
                                      setRollbackMessage("");
                                    }}
                                  >
                                    <RotateCcw className="h-3.5 w-3.5" />
                                  </Button>
                                </Tip>
                              )}
                              {isAdmin && (
                                <>
                                  <Tip content={v.is_locked ? t_i18n("Unlock") : t_i18n("Lock")}>
                                    <Button
                                      size="sm"
                                      variant="ghost"
                                      className="h-7 w-7 p-0 text-default-secondary hover:text-default-primary"
                                      onClick={() => handleLockToggle(v.kind, v.version_number, v.is_locked)} disabled={aiOff}>
                                      {v.is_locked ? <Unlock className="h-3.5 w-3.5" /> : <Lock className="h-3.5 w-3.5" />}
                                    </Button>
                                  </Tip>
                                  <Tip content={t_i18n("Delete version")}>
                                    <Button aria-label={t_i18n("Delete version")}
                                      size="sm"
                                      variant="ghost"
                                      className="h-7 w-7 p-0 text-feedback-error-primary hover:text-feedback-error-primary"
                                      onClick={() => void handleDelete(v.kind, v.version_number, v.is_locked)} disabled={aiOff}>
                                      <Trash2 className="h-3.5 w-3.5" />
                                    </Button>
                                  </Tip>
                                </>
                              )}
                            </div>
                          </div>
                        );
                      })}
                    </div>
                  )}
      {/* Diff dialog – lazy, kind-aware */}
                  {/* No-history hint when expanded */}
                  {isExpanded && history.length === 0 && (
                    <p className="text-[11px] text-default-disabled ml-10 pb-2.5">
                      {t_i18n("No previous versions")}
                    </p>
                  )}

                  {/* Load more — bumps the kind's fetch limit by
                      PAGE_SIZE so older versions stay reachable on
                      long histories.  Only rendered when the server
                      reports a total greater than what we have
                      locally AND the user hasn't searched-and-narrowed
                      the rendered set (the search filter operates on
                      already-fetched rows, so a Load-more is what
                      actually pulls older rows into the client). */}
                  {isExpanded && kindTotals[kind] > versions.length && (
                    <div className="ml-7 px-3 pb-2.5 pt-1">
                      <Button
                        size="sm"
                        variant="outline"
                        className="h-7 text-[11px]"
                        onClick={() => loadMore(kind)}
                      >
                        {t_i18n("Load more")}
                        <span className="text-default-secondary ml-1.5">
                          ({versions.length} / {kindTotals[kind]})
                        </span>
                      </Button>
                    </div>
                  )}
                </CardContent>
              </Card>
            );
          })}
        </div>
      )}
      <DiffDialogFetcher
        agentId={agent.id}
        kind={diffKind}
        fromVersion={selected[0]?.version ?? null}
        toVersion={selected[1]?.version ?? null}
        open={diffOpen}
        onOpenChange={setDiffOpen}
      />
    </div>
  );
}
