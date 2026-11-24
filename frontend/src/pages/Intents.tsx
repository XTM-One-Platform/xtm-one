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
  Zap, Plus, ChevronDown, ChevronUp, ChevronRight, Bot,
  Trash2, Layers, AlertTriangle, Globe, Pencil, X, Check,
} from "lucide-react";
import { Card, CardContent } from "@/components/ui/card";
import { Badge, BadgeFocusTrigger } from "@/components/ui/badge";
import { CountBadge } from "@/components/ui/count-badge";
import { Button } from "@/components/ui/button";
import { DataTable, type DataTableColumn, type DataTableFilter } from "@/components/ui/data-table";
import { EmptyState } from "@/components/ui/empty-state";
import { InfoTip } from "@/components/ui/info-tip";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { SearchInput } from "@/components/ui/search-input";
import { Tip } from "@/components/ui/tooltip";
import {
  Dialog, DialogTrigger, DialogContent, DialogHeader, DialogTitle,
} from "@/components/ui/dialog";
import { useAuth } from "@/hooks/useAuth";
import { useAgents } from "@/hooks/api/useAgentApi";
import {
  useIntents, useVerticals, useCreateIntent, useUpdateIntent,
  useBindIntent, useAddIntentBindings, useChangeIntentBindingVertical,
  useDeleteIntentBinding, useCreateVertical, useRenameVertical, useDeleteVertical,
} from "@/hooks/api/useIntentApi";
import { cn } from "@/lib/utils";
import { useFormatter } from "@/i18n";
import type { Agent, IntentBinding, IntentItem, VerticalItem } from "@/types";
import { useAiActionDisabled } from "@/hooks/useAiDisabled";

/** Get effective priority for a binding in a given vertical context */
function getEffectivePriority(b: IntentBinding, vertical?: string | null): number {
  if (vertical && b.priority_overrides?.[vertical] !== undefined) {
    return b.priority_overrides[vertical];
  }
  return b.priority;
}

// ── Intent bindings ─────────────────────────────────────────────────────────

/** Column headers and filter labels, translated by the table and its CSV. @i18n-keys */
const HEADERS = { intent: "Intent", platform: "Platform", bindings: "Bindings", usage: "Usage", name: "Name" } as const;

type IntentUsage = "used" | "unused" | "unbound";

/** Used = bound or declared by a platform; unbound = declared by a platform with no agent bound (as the API's `status`). */
function intentUsage(intent: IntentItem): IntentUsage {
  if (intent.bindings.length === 0 && intent.platforms.length > 0) return "unbound";
  return intent.bindings.length > 0 || intent.platforms.length > 0 ? "used" : "unused";
}

/** The bindings of one intent, grouped by vertical, highest priority first. */
function IntentBindingsList({ intent, verticals }: { intent: IntentItem; verticals: VerticalItem[] }) {
  const { t_i18n } = useFormatter();
  const deleteMutation = useDeleteIntentBinding();
  if (intent.bindings.length === 0) {
    return (
      <p className="content-compact text-default-secondary py-4 text-center">
        {t_i18n("No agents bound to this intent.")}
      </p>
    );
  }
  // Separate universal (null vertical) from specific bindings
  const universalBindings = intent.bindings.filter((b) => !b.vertical);
  const specificGroups = new Map<string, IntentBinding[]>();
  for (const b of intent.bindings.filter((b) => !!b.vertical)) {
    const arr = specificGroups.get(b.vertical!) || [];
    arr.push(b);
    specificGroups.set(b.vertical!, arr);
  }
  const sortDesc = (arr: IntentBinding[], vertical?: string | null) =>
    [...arr].sort((a, b) => getEffectivePriority(b, vertical) - getEffectivePriority(a, vertical));

  // A specific vertical lists its own bindings with the universal ones merged in; else one "All verticals" group.
  const groups: { key: string; vertical: string | null; bindings: IntentBinding[] }[] =
    specificGroups.size > 0
      ? [...specificGroups.entries()].map(([vertical, vBindings]) => ({
          key: vertical,
          vertical,
          bindings: sortDesc([...vBindings, ...universalBindings], vertical),
        }))
      : [{ key: "*", vertical: null, bindings: sortDesc(universalBindings, null) }];

  return (
    <div className="space-y-3">
      {groups.map((group) => (
        <div key={group.key} className="rounded-sm border border-elevation-subtle-soft bg-elevation-hover overflow-hidden">
          <div className="flex items-center gap-2 px-3 py-2 border-b border-elevation-subtle-soft bg-elevation-hover">
            {group.vertical ? (
              <Layers className="h-3 w-3 text-filigran-ia-primary" />
            ) : (
              <Globe className="h-3 w-3 text-default-secondary" />
            )}
            <span className={cn("content-caption font-medium", group.vertical ? "text-filigran-ia-primary" : "text-default-secondary")}>
              {group.vertical ?? t_i18n("All verticals")}
            </span>
            <span className="ml-auto content-caption text-default-secondary tabular-nums">
              {t_i18n("{count, plural, one {# agent} other {# agents}}", { count: group.bindings.length })}
            </span>
          </div>
          <div className="divide-y divide-[var(--border-elevation-subtle)]">
            {group.bindings.map((b) => (
              <BindingRow
                key={b.id}
                binding={b}
                intentId={intent.id}
                verticals={verticals}
                deleteMutation={deleteMutation}
                showPriority={group.bindings.length > 1}
                isUniversalInGroup={group.vertical !== null && !b.vertical}
                groupVertical={group.vertical ?? undefined}
              />
            ))}
          </div>
        </div>
      ))}
    </div>
  );
}

/** The row's detail: its bindings, with the intent's edit and add-binding actions (a dialog, never a drawer). */
function IntentBindingsDialog({
  intent,
  agents,
  verticals,
}: {
  intent: IntentItem;
  agents: Agent[];
  verticals: VerticalItem[];
}) {
  return (
    <DialogContent className="max-w-2xl">
      <DialogHeader>
        <DialogTitle className="font-mono-plex break-all">{intent.name}</DialogTitle>
      </DialogHeader>
      {intent.description && <p className="content-compact text-default-secondary">{intent.description}</p>}
      <div className="max-h-[60vh] overflow-y-auto">
        <IntentBindingsList intent={intent} verticals={verticals} />
      </div>
      <div className="flex justify-end gap-2 pt-2">
        <IntentActions intent={intent} agents={agents} verticals={verticals} />
      </div>
    </DialogContent>
  );
}

/** Edit the intent, add a binding: the same two actions in the row and in its dialog. */
function IntentActions({
  intent,
  agents,
  verticals,
  iconOnly = false,
}: {
  intent: IntentItem;
  agents: Agent[];
  verticals: VerticalItem[];
  iconOnly?: boolean;
}) {
  const { t_i18n } = useFormatter();
  const [showBindDialog, setShowBindDialog] = useState(false);
  const [showEditDialog, setShowEditDialog] = useState(false);
  return (
    <>
      <Dialog open={showEditDialog} onOpenChange={setShowEditDialog}>
        <DialogTrigger asChild>
          {iconOnly ? (
            <Button variant="ghost" size="icon" aria-label={t_i18n("Edit Intent")} disabled={intent.is_builtin}>
              <Pencil className="h-4 w-4" />
            </Button>
          ) : (
            <Button variant="outline" size="sm" className="gap-1" disabled={intent.is_builtin}>
              <Pencil className="h-3 w-3" /> {t_i18n("Edit Intent")}
            </Button>
          )}
        </DialogTrigger>
        <EditIntentDialog intent={intent} onClose={() => setShowEditDialog(false)} />
      </Dialog>
      <Dialog open={showBindDialog} onOpenChange={setShowBindDialog}>
        <DialogTrigger asChild>
          {iconOnly ? (
            <Button variant="ghost" size="icon" aria-label={t_i18n("Add Binding")}>
              <Plus className="h-4 w-4" />
            </Button>
          ) : (
            <Button variant="outline" size="sm" className="gap-1">
              <Plus className="h-3 w-3" /> {t_i18n("Add Binding")}
            </Button>
          )}
        </DialogTrigger>
        <AddBindingDialog
          key={String(showBindDialog)}
          intentId={intent.id}
          intent={intent}
          agents={agents}
          verticals={verticals}
          onClose={() => setShowBindDialog(false)}
        />
      </Dialog>
    </>
  );
}

// ── Binding Row

function BindingRow({
  binding: b,
  intentId,
  verticals,
  deleteMutation,
  showPriority,
  isUniversalInGroup,
  groupVertical,
}: {
  binding: IntentBinding;
  intentId: string;
  verticals: VerticalItem[];
  deleteMutation: ReturnType<typeof useDeleteIntentBinding>;
  showPriority: boolean;
  isUniversalInGroup: boolean;
  groupVertical?: string | null;
}) {
  const { t_i18n } = useFormatter();
  const [showEdit, setShowEdit] = useState(false);

  // Compute effective priority for this binding in the current group context
  const effectivePriority = getEffectivePriority(b, groupVertical);

  const priorityMutation = useBindIntent();
  const aiOff = useAiActionDisabled();
  const setPriority = (newPriority: number) =>
    priorityMutation.mutate({
      intentId,
      agent_id: b.agent_id,
      vertical: b.vertical,
      priority: newPriority,
      priority_override_vertical: groupVertical || undefined,
    });

  return (
    <div className="flex items-center gap-3 px-3 py-2 content-compact">
      {/* Agent */}
      <div className="flex items-center gap-1.5 flex-1 min-w-0">
        <Bot className="h-3 w-3 text-default-secondary shrink-0" />
        <span className="font-medium truncate">{b.agent_name || b.agent_id}</span>
        {b.agent_slug && (
          <span className="text-default-secondary font-mono-plex text-[9px] shrink-0">{b.agent_slug}</span>
        )}
        {isUniversalInGroup && (
          <Badge variant="outline" className="text-[8px] gap-0.5 px-1 py-0 h-4 text-default-secondary border-elevation-subtle-soft">
            <Globe className="h-2 w-2" /> {t_i18n("all verticals")}
          </Badge>
        )}
        {b.is_builtin && (
          <Badge variant="outline" className="text-[8px] gap-0.5 px-1 py-0 h-4 text-default-secondary border-feedback-alert-primary/30 text-feedback-warning-primary">
            {t_i18n("builtin")}
          </Badge>
        )}
      </div>
      {/* Priority (only when multiple agents in same vertical) */}
      {showPriority && (
        <div className="flex items-center gap-0.5 shrink-0">
          <button aria-label={t_i18n("Decrease priority")}
            className="text-default-secondary hover:text-default-primary transition-colors p-0.5 disabled:opacity-30"
            disabled={b.is_builtin || effectivePriority <= 0 || priorityMutation.isPending || aiOff}
            onClick={() => setPriority(Math.max(0, effectivePriority - 1))}
          >
            <ChevronDown className="h-3 w-3" />
          </button>
          <span className="text-[11px] tabular-nums w-5 text-center">{effectivePriority}</span>
          <button aria-label={t_i18n("Increase priority")}
            className="text-default-secondary hover:text-default-primary transition-colors p-0.5 disabled:opacity-30"
            disabled={b.is_builtin || priorityMutation.isPending || aiOff}
            onClick={() => setPriority(effectivePriority + 1)}
          >
            <ChevronUp className="h-3 w-3" />
          </button>
        </div>
      )}
      {/* Actions */}
      <div className="flex items-center gap-1 shrink-0">
        <Dialog open={showEdit} onOpenChange={setShowEdit}>
          <DialogTrigger asChild>
            <button aria-label={t_i18n("Edit binding")}
              className="text-default-secondary hover:text-default-primary transition-colors p-0.5 disabled:opacity-30 disabled:cursor-not-allowed disabled:hover:text-default-secondary"
              disabled={b.is_builtin}
            >
              <Pencil className="h-3 w-3" />
            </button>
          </DialogTrigger>
          <EditBindingDialog
            key={b.updated_at}
            binding={b}
            intentId={intentId}
            verticals={verticals}
            onClose={() => setShowEdit(false)}
          />
        </Dialog>
        <button aria-label={t_i18n("Delete binding")}
          className="text-default-secondary hover:text-feedback-error-primary transition-colors p-0.5 disabled:opacity-30 disabled:cursor-not-allowed disabled:hover:text-default-secondary"
          disabled={b.is_builtin}
          onClick={() => deleteMutation.mutate({ intentId, bindingId: b.id })}
        >
          <Trash2 className="h-3 w-3" />
        </button>
      </div>
    </div>
  );
}

// ── Edit Binding Dialog ─────────────────────────────────────────────────────

function EditBindingDialog({
  binding,
  intentId,
  verticals,
  onClose,
}: {
  binding: IntentBinding;
  intentId: string;
  verticals: VerticalItem[];
  onClose: () => void;
}) {
  const { t_i18n } = useFormatter();
  const [editVertical, setEditVertical] = useState(binding.vertical ?? "");

  const saveMutation = useChangeIntentBindingVertical();
  const aiOff = useAiActionDisabled();
  const handleSave = () => {
    const newVertical = editVertical || null;
    if (newVertical === binding.vertical) {
      onClose();
      return;
    }
    saveMutation.mutate(
      {
        intentId,
        bindingId: binding.id,
        agentId: binding.agent_id,
        vertical: newVertical,
        priority: binding.priority,
      },
      { onSuccess: onClose },
    );
  };

  return (
    <DialogContent className="max-w-sm">
      <DialogHeader>
        <DialogTitle>{t_i18n("Edit Binding")}</DialogTitle>
      </DialogHeader>
      <div className="space-y-4 py-2">
        {/* Agent (read-only) */}
        <div>
          <label className="content-compact font-medium text-default-secondary block mb-1.5">{t_i18n("Agent")}</label>
          <div className="flex items-center gap-2 rounded-sm border border-elevation-subtle bg-elevation-hover px-3 py-2">
            <Bot className="h-3.5 w-3.5 text-accent shrink-0" />
            <span className="content-base truncate">{binding.agent_name || binding.agent_id}</span>
          </div>
        </div>

        {/* Vertical */}
        <div>
          <label className="content-compact font-medium text-default-secondary block mb-1.5">{t_i18n("Vertical")}</label>
          <div className="rounded-sm border border-elevation-subtle-soft bg-elevation-hover p-2 space-y-1">
            {/* Universal option */}
            <button
              type="button"
              className={`flex items-center gap-2 w-full px-2.5 py-1.5 rounded-sm content-compact transition-colors ${
                editVertical === ""
                  ? "bg-accent/10 text-accent"
                  : "hover:bg-elevation-hover text-default-secondary"
              }`}
              onClick={() => setEditVertical("")}
            >
              <div className={`h-3.5 w-3.5 rounded-full border flex items-center justify-center shrink-0 ${
                editVertical === "" ? "bg-accent border-accent" : "border-elevation-subtle-soft"
              }`}>
                {editVertical === "" && <Check className="h-2.5 w-2.5 text-negative-primary" />}
              </div>
              <Globe className="h-3 w-3" />
              <span>{t_i18n("All verticals")}</span>
            </button>

            {verticals.length > 0 && (
              <div className="border-t border-elevation-subtle-soft mx-1 my-1" />
            )}

            {verticals.map((v) => {
              const isSelected = editVertical === v.name;
              return (
                <button
                  key={v.id}
                  type="button"
                  className={`flex items-center gap-2 w-full px-2.5 py-1.5 rounded-sm content-compact transition-colors ${
                    isSelected
                      ? "bg-filigran-ia-primary/10 text-filigran-ia-primary"
                      : "hover:bg-elevation-hover text-default-secondary"
                  }`}
                  onClick={() => setEditVertical(v.name)}
                >
                  <div className={`h-3.5 w-3.5 rounded-full border flex items-center justify-center shrink-0 ${
                    isSelected ? "bg-filigran-ia-primary border-filigran-ia-primary" : "border-elevation-subtle-soft"
                  }`}>
                    {isSelected && <Check className="h-2.5 w-2.5 text-negative-primary" />}
                  </div>
                  <Layers className="h-3 w-3" />
                  <span>{v.name}</span>
                </button>
              );
            })}

            {verticals.length === 0 && (
              <p className="content-caption text-default-secondary text-center py-1">
                {t_i18n("No verticals defined. Only \"All verticals\" is available.")}
              </p>
            )}
          </div>
        </div>

      </div>
      <div className="flex justify-end gap-2 pt-2">
        <Button variant="outline" size="sm" onClick={onClose}>{t_i18n("Cancel")}</Button>
        <Button
          size="sm"
          disabled={saveMutation.isPending || aiOff}
          onClick={handleSave}
        >
          {saveMutation.isPending ? t_i18n("Saving…") : t_i18n("Save")}
        </Button>
      </div>
      {saveMutation.isError && (
        <p className="content-compact text-feedback-error-primary mt-1">{(saveMutation.error as Error).message}</p>
      )}
    </DialogContent>
  );
}

// ── Add Binding Dialog ──────────────────────────────────────────────────────

function AddBindingDialog({
  intentId,
  intent,
  agents,
  verticals,
  onClose,
}: {
  intentId: string;
  intent: IntentItem;
  agents: Agent[];
  verticals: VerticalItem[];
  onClose: () => void;
}) {
  const { t_i18n } = useFormatter();
  const [agentId, setAgentId] = useState("");
  const [agentName, setAgentName] = useState("");
  const [agentSearch, setAgentSearch] = useState("");
  const [pickerOpen, setPickerOpen] = useState(false);
  const [selectedVerticals, setSelectedVerticals] = useState<string[]>([]);
  const [universalSelected, setUniversalSelected] = useState(false);

  const filteredAgents = agents.filter(
    (a) => !agentSearch || a.name.toLowerCase().includes(agentSearch.toLowerCase())
      || (a.slug && a.slug.toLowerCase().includes(agentSearch.toLowerCase())),
  );

  // Determine already-bound verticals for the selected agent on this intent
  const alreadyBound = new Set(
    intent.bindings
      .filter((b) => b.agent_id === agentId)
      .map((b) => b.vertical ?? "__universal__"),
  );

  const toggleVertical = (name: string) => {
    if (universalSelected) return;
    setSelectedVerticals((prev) =>
      prev.includes(name) ? prev.filter((v) => v !== name) : [...prev, name],
    );
  };

  const toggleUniversal = () => {
    setUniversalSelected((prev) => !prev);
    if (!universalSelected) setSelectedVerticals([]);
  };

  const mutation = useAddIntentBindings();
  const aiOff = useAiActionDisabled();
  const handleAdd = () =>
    mutation.mutate(
      { intentId, agentId, verticals: universalSelected ? [null] : selectedVerticals },
      { onSuccess: onClose },
    );

  const selectionCount = universalSelected ? 1 : selectedVerticals.length;
  const canSubmit = !aiOff && agentId && (universalSelected || selectedVerticals.length > 0) && !mutation.isPending;

  return (
    <DialogContent className="max-w-md">
      <DialogHeader>
        <DialogTitle>{t_i18n("Add Agent Binding")}</DialogTitle>
      </DialogHeader>
      <div className="space-y-4 py-2">
        {/* Agent picker */}
        <div>
          <Label htmlFor="binding-agent-search" className="block mb-1.5">{t_i18n("Agent")}</Label>
          {agentId ? (
            <div className="flex items-center gap-2.5 rounded-sm border border-elevation-subtle bg-elevation-default px-3 py-2">
              <Bot className="h-3.5 w-3.5 text-accent shrink-0" />
              <span className="content-base flex-1 truncate">{agentName}</span>
              <button aria-label={t_i18n("Clear selection")}
                type="button"
                className="text-default-secondary hover:text-default-primary transition-colors"
                onClick={() => {
                  setAgentId(""); setAgentName("");
                  setSelectedVerticals([]); setUniversalSelected(false);
                }}
              >
                <X className="h-3.5 w-3.5" />
              </button>
            </div>
          ) : (
            <div className="relative">
              <SearchInput
                id="binding-agent-search"
                className="max-w-none"
                placeholder={t_i18n("Search agents…")}
                value={agentSearch}
                onChange={(value) => { setAgentSearch(value); setPickerOpen(true); }}
                onClick={() => setPickerOpen(true)}
              />
              {pickerOpen && (
                <div className="absolute z-50 mt-1 w-full rounded-sm border border-elevation-subtle layer-2 bg-elevation-default shadow-overlay max-h-52 overflow-y-auto">
                  {filteredAgents.length > 0 ? (
                    filteredAgents.map((agent) => (
                      <button
                        key={agent.id}
                        type="button"
                        className="flex items-center gap-2.5 w-full px-3 py-2 text-left content-compact transition-colors hover:bg-elevation-hover"
                        onClick={() => {
                          setAgentId(agent.id);
                          setAgentName(agent.name);
                          setAgentSearch("");
                          setPickerOpen(false);
                        }}
                      >
                        <Bot className="h-3.5 w-3.5 text-default-secondary shrink-0" />
                        <span className="truncate flex-1">{agent.name}</span>
                        {agent.slug && <code className="content-caption text-default-secondary font-mono-plex shrink-0">{agent.slug}</code>}
                      </button>
                    ))
                  ) : (
                    <p className="px-3 py-3 text-[11px] text-default-secondary text-center">
                      {agentSearch ? t_i18n("No matching agents.") : t_i18n("No agents available.")}
                    </p>
                  )}
                </div>
              )}
            </div>
          )}
        </div>

        {/* Vertical multi-select */}
        {agentId && (
          <div>
            <label className="content-compact font-medium text-default-secondary block mb-1.5">{t_i18n("Verticals")}</label>
            <div className="rounded-sm border border-elevation-subtle-soft bg-elevation-hover p-2 space-y-1">
              {/* Universal option */}
              <button
                type="button"
                disabled={alreadyBound.has("__universal__")}
                className={`flex items-center gap-2 w-full px-2.5 py-1.5 rounded-sm content-compact transition-colors ${
                  alreadyBound.has("__universal__")
                    ? "opacity-40 cursor-default"
                    : universalSelected
                    ? "bg-accent/10 text-accent"
                    : "hover:bg-elevation-hover text-default-secondary"
                }`}
                onClick={toggleUniversal}
              >
                <div className={`h-3.5 w-3.5 rounded-sm border flex items-center justify-center shrink-0 ${
                  universalSelected ? "bg-accent border-accent" : "border-elevation-subtle-soft"
                }`}>
                  {universalSelected && <Check className="h-2.5 w-2.5 text-negative-primary" />}
                </div>
                <Globe className="h-3 w-3" />
                <span>{t_i18n("All verticals")}</span>
                {alreadyBound.has("__universal__") && (
                  <span className="text-[9px] text-default-secondary ml-auto">{t_i18n("already bound")}</span>
                )}
              </button>

              {verticals.length > 0 && (
                <div className="border-t border-elevation-subtle-soft mx-1 my-1" />
              )}

              {verticals.map((v) => {
                const isSelected = selectedVerticals.includes(v.name);
                const isBound = alreadyBound.has(v.name);
                return (
                  <button
                    key={v.id}
                    type="button"
                    disabled={isBound || universalSelected}
                    className={`flex items-center gap-2 w-full px-2.5 py-1.5 rounded-sm content-compact transition-colors ${
                      isBound || universalSelected
                        ? "opacity-40 cursor-default"
                        : isSelected
                        ? "bg-filigran-ia-primary/10 text-filigran-ia-primary"
                        : "hover:bg-elevation-hover text-default-secondary"
                    }`}
                    onClick={() => toggleVertical(v.name)}
                  >
                    <div className={`h-3.5 w-3.5 rounded-sm border flex items-center justify-center shrink-0 ${
                      isSelected ? "bg-filigran-ia-primary border-filigran-ia-primary" : "border-elevation-subtle-soft"
                    }`}>
                      {isSelected && <Check className="h-2.5 w-2.5 text-negative-primary" />}
                    </div>
                    <Layers className="h-3 w-3" />
                    <span>{v.name}</span>
                    {isBound && (
                      <span className="text-[9px] text-default-secondary ml-auto">{t_i18n("already bound")}</span>
                    )}
                  </button>
                );
              })}

              {verticals.length === 0 && (
                <p className="content-caption text-default-secondary text-center py-1">
                  {t_i18n("No verticals defined. Only \"All verticals\" is available.")}
                </p>
              )}
            </div>
          </div>
        )}

      </div>
      <div className="flex items-center justify-between pt-2">
        <span className="content-caption text-default-secondary">
          {selectionCount > 0 ? t_i18n("{count, plural, one {# binding} other {# bindings}} will be created", { count: selectionCount }) : ""}
        </span>
        <div className="flex gap-2">
          <Button variant="outline" size="sm" onClick={onClose}>
            {t_i18n("Cancel")}
          </Button>
          <Button
            size="sm"
            disabled={!canSubmit}
            onClick={handleAdd}
          >
            {mutation.isPending
              ? t_i18n("Binding…")
              : selectionCount > 0
                ? t_i18n("Add {count, plural, one {# Binding} other {# Bindings}}", { count: selectionCount })
                : t_i18n("Add Binding")}
          </Button>
        </div>
      </div>
      {mutation.isError && (
        <p className="content-compact text-feedback-error-primary mt-1">{(mutation.error as Error).message}</p>
      )}
    </DialogContent>
  );
}

// ── Edit Intent Dialog ──────────────────────────────────────────────────────

function EditIntentDialog({
  intent,
  onClose,
}: {
  intent: IntentItem;
  onClose: () => void;
}) {
  const aiOff = useAiActionDisabled();
  const { t_i18n } = useFormatter();
  const [name, setName] = useState(intent.name);
  const [description, setDescription] = useState(intent.description || "");

  const mutation = useUpdateIntent();
  const handleSave = () =>
    mutation.mutate({ intentId: intent.id, name, description }, { onSuccess: onClose });

  return (
    <DialogContent className="max-w-md">
      <DialogHeader>
        <DialogTitle>{t_i18n("Edit Intent")}</DialogTitle>
      </DialogHeader>
      <div className="space-y-4 py-2">
        <div className="space-y-1.5">
          <Label htmlFor="edit-intent-name" className="block">
            {t_i18n("Name")} <span className="font-normal text-default-secondary">{t_i18n("(dotted notation)")}</span>
          </Label>
          <Input
            id="edit-intent-name"
            aria-label={t_i18n("Name")}
            className="[&_input]:font-mono-plex"
            value={name}
            onChange={(e) => setName(e.target.value)}
          />
        </div>
        <div className="space-y-1.5">
          <Label htmlFor="edit-intent-description" className="block">{t_i18n("Description")}</Label>
          <Input
            id="edit-intent-description"
            aria-label={t_i18n("Description")}
            value={description}
            onChange={(e) => setDescription(e.target.value)}
          />
        </div>
      </div>
      <div className="flex justify-end gap-2 pt-2">
        <Button variant="outline" size="sm" onClick={onClose}>{t_i18n("Cancel")}</Button>
        <Button size="sm" disabled={!name || mutation.isPending || aiOff} onClick={handleSave}>
          {mutation.isPending ? t_i18n("Saving…") : t_i18n("Save")}
        </Button>
      </div>
      {mutation.isError && (
        <p className="content-compact text-feedback-error-primary mt-1">{(mutation.error as Error).message}</p>
      )}
    </DialogContent>
  );
}

// ── Create Intent Dialog ────────────────────────────────────────────────────

function CreateIntentDialog({ onClose }: { onClose: () => void }) {
  const { t_i18n } = useFormatter();
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");

  const mutation = useCreateIntent();
  const handleCreate = () =>
    mutation.mutate({ name, description }, { onSuccess: onClose });

  return (
    <DialogContent className="max-w-md">
      <DialogHeader>
        <DialogTitle>{t_i18n("Create Intent")}</DialogTitle>
      </DialogHeader>
      <div className="space-y-4 py-2">
        <div className="space-y-1.5">
          <Label htmlFor="create-intent-name" className="block">
            {t_i18n("Name")} <span className="font-normal text-default-secondary">{t_i18n("(dotted notation)")}</span>
          </Label>
          <Input
            id="create-intent-name"
            aria-label={t_i18n("Name")}
            className="[&_input]:font-mono-plex"
            placeholder="product.feature.action"
            value={name}
            onChange={(e) => setName(e.target.value)}
          />
        </div>
        <div className="space-y-1.5">
          <Label htmlFor="create-intent-description" className="block">{t_i18n("Description")}</Label>
          <Input
            id="create-intent-description"
            aria-label={t_i18n("Description")}
            placeholder={t_i18n("What this intent does…")}
            value={description}
            onChange={(e) => setDescription(e.target.value)}
          />
        </div>
      </div>
      <div className="flex justify-end gap-2 pt-2">
        <Button variant="outline" size="sm" onClick={onClose}>{t_i18n("Cancel")}</Button>
        <Button size="sm" disabled={!name || mutation.isPending} onClick={handleCreate}>
          {mutation.isPending ? t_i18n("Creating…") : t_i18n("Create")}
        </Button>
      </div>
      {mutation.isError && (
        <p className="content-compact text-feedback-error-primary mt-1">{(mutation.error as Error).message}</p>
      )}
    </DialogContent>
  );
}

// ── Vertical name (inline editable) ──────────────────────────────────────────

function VerticalNameCell({ vertical: v, editing, onDone }: { vertical: VerticalItem; editing: boolean; onDone: () => void }) {
  const { t_i18n } = useFormatter();
  const [editName, setEditName] = useState(v.name);
  const renameMutation = useRenameVertical();
  const aiOff = useAiActionDisabled();
  const cancel = () => {
    setEditName(v.name);
    onDone();
  };
  const handleRename = () => renameMutation.mutate({ id: v.id, name: editName }, { onSuccess: onDone });

  if (!editing) {
    return (
      <span className="flex min-w-0 items-center gap-1.5">
        <span className="truncate font-medium" title={v.name}>{v.name}</span>
        {v.source === "builtin" && (
          <Badge variant="outline" className="shrink-0 text-default-secondary">{t_i18n("builtin")}</Badge>
        )}
      </span>
    );
  }
  return (
    <div className="space-y-1">
      <div className="flex items-center gap-1.5">
        <Input
          aria-label={t_i18n("Name")}
          className="flex-1"
          value={editName}
          onChange={(e) => setEditName(e.target.value)}
          autoFocus
          onKeyDown={(e) => {
            if (e.key === "Enter" && editName) handleRename();
            if (e.key === "Escape") cancel();
          }}
        />
        <Button variant="outline" onClick={cancel}>
          {t_i18n("Cancel")}
        </Button>
        <Button disabled={!editName || editName === v.name || renameMutation.isPending || aiOff} onClick={handleRename}>
          {renameMutation.isPending ? t_i18n("Saving…") : t_i18n("Save")}
        </Button>
      </div>
      {renameMutation.isError && (
        <p className="content-compact text-feedback-error-primary">{(renameMutation.error as Error).message}</p>
      )}
    </div>
  );
}

// ── Vertical Panel ──────────────────────────────────────────────────────────

function VerticalPanel({
  verticals,
  isLoading,
  isError,
  onRetry,
}: {
  verticals: VerticalItem[];
  isLoading: boolean;
  isError: boolean;
  onRetry: () => void;
}) {
  const { t_i18n } = useFormatter();
  const [expanded, setExpanded] = useState(false);
  const [showCreate, setShowCreate] = useState(false);
  const [editingId, setEditingId] = useState<string | null>(null);

  const deleteMutation = useDeleteVertical();

  const [newName, setNewName] = useState("");
  const createMutation = useCreateVertical();
  const aiOff = useAiActionDisabled();
  const handleCreate = () =>
    createMutation.mutate(newName, {
      onSuccess: () => {
        setNewName("");
        setShowCreate(false);
      },
    });

  const columns = useMemo<DataTableColumn<VerticalItem>[]>(
    () => [
      {
        key: "name",
        header: HEADERS.name,
        className: "w-full max-w-0",
        interactive: true,
        cell: (v) => <VerticalNameCell vertical={v} editing={editingId === v.id} onDone={() => setEditingId(null)} />,
        sortValue: (v) => v.name,
        csv: (v) => v.name,
      },
      {
        key: "bindings",
        header: HEADERS.bindings,
        cell: (v) => v.binding_count,
        sortValue: (v) => v.binding_count,
        csv: (v) => v.binding_count,
        align: "right",
        className: "tabular-nums",
        hideable: true,
      },
    ],
    [editingId],
  );

  return (
    <Card className="border-elevation-subtle">
      <CardContent className="p-0">
        <button
          className="flex items-center gap-3 w-full p-4 text-left hover:bg-elevation-hover transition-colors"
          aria-expanded={expanded}
          onClick={() => setExpanded(!expanded)}
        >
          <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-sm bg-filigran-ia-primary/10">
            <Layers className="h-4 w-4 text-filigran-ia-primary" />
          </div>
          <div className="flex-1">
            <p className="content-base font-medium">{t_i18n("Verticals")}</p>
            <p className="content-caption text-default-secondary">
              {t_i18n("Business verticals for intent routing")}
            </p>
          </div>
          <CountBadge variant="outline" className="mr-2">
            {verticals.length}
          </CountBadge>
          {expanded ? (
            <ChevronDown className="h-3.5 w-3.5 text-default-secondary" />
          ) : (
            <ChevronRight className="h-3.5 w-3.5 text-default-secondary" />
          )}
        </button>

        {expanded && (
          <div className="border-t border-elevation-subtle-soft p-4 space-y-3">
            <DataTable
              rows={verticals}
              columns={columns}
              rowKey={(v) => v.id}
              defaultSort={{ key: "name", direction: "asc" }}
              searchFields={(v) => [v.name]}
              searchPlaceholder={t_i18n("Search verticals")}
              noFilters="One row per vertical, a name and a count: the search is the only narrowing it needs."
              urlKey="iv_"
              storageKey="intent-verticals"
              exportName="verticals"
              framed={false}
              isLoading={isLoading}
              isError={isError}
              onRetry={onRetry}
              emptyLabel={t_i18n("No verticals defined yet.")}
              rowActions={(v) => {
                const isBuiltin = v.source === "builtin";
                return (
                  <span className="inline-flex gap-1">
                    <Button
                      variant="ghost"
                      size="icon"
                      aria-label={t_i18n("Edit")}
                      disabled={isBuiltin || editingId === v.id}
                      onClick={() => setEditingId(v.id)}
                    >
                      <Pencil className="h-4 w-4" />
                    </Button>
                    <Button
                      variant="ghost"
                      size="icon"
                      aria-label={t_i18n("Delete")}
                      disabled={isBuiltin || v.binding_count > 0 || deleteMutation.isPending || aiOff}
                      onClick={() => deleteMutation.mutate(v.id)}
                    >
                      <Trash2 className="h-4 w-4" />
                    </Button>
                  </span>
                );
              }}
            />

            <div className="flex items-center gap-2">
              {showCreate ? (
                <div className="w-full space-y-1.5">
                  <div className="flex items-center gap-1.5">
                    <Label htmlFor="new-vertical-name" className="flex items-center gap-1">
                      {t_i18n("Vertical name")}
                      <span className="text-feedback-error-primary" aria-label={t_i18n("Required")}>
                        *
                      </span>
                    </Label>
                    <InfoTip
                      content={
                        <div className="max-w-xs content-compact">
                          {t_i18n(
                            "A vertical is a business domain, for example fimi. Intent routing picks the bindings of the caller's vertical. With no vertical, the bindings set to All verticals apply.",
                          )}
                        </div>
                      }
                      label={t_i18n("About this field")}
                    />
                  </div>
                  <div className="flex items-center gap-2">
                    <Input
                      id="new-vertical-name"
                      aria-label={t_i18n("Vertical name")}
                      className="flex-1"
                      aria-describedby="new-vertical-name-help"
                      aria-required="true"
                      placeholder="fimi"
                      value={newName}
                      onChange={(e) => setNewName(e.target.value)}
                      autoFocus
                    />
                    <Button variant="outline" onClick={() => { setShowCreate(false); setNewName(""); }}>
                      {t_i18n("Cancel")}
                    </Button>
                    <Button disabled={!newName || createMutation.isPending || aiOff} onClick={handleCreate}>
                      {t_i18n("Add")}
                    </Button>
                  </div>
                  <p id="new-vertical-name-help" className="content-compact text-default-secondary">
                    {t_i18n("Lowercase id, used to route intents, for example fimi")}
                  </p>
                </div>
              ) : (
                <Button variant="outline" size="sm" className="gap-1" onClick={() => setShowCreate(true)}>
                  <Plus className="h-3 w-3" /> {t_i18n("Add Vertical")}
                </Button>
              )}
            </div>
            {(createMutation.isError || deleteMutation.isError) && (
              <p className="content-compact text-feedback-error-primary">
                {((createMutation.error || deleteMutation.error) as Error)?.message}
              </p>
            )}
          </div>
        )}
      </CardContent>
    </Card>
  );
}

// ── Main Page ───────────────────────────────────────────────────────────────

/**
 * Every intent in the shared data table, client mode: the list is bounded by the
 * platforms' declarations (hundreds at most) and the API returns it whole, so search,
 * the Platform and Usage filters, sort and paging run on the loaded rows, kept in the
 * URL under `in_`. A row opens its bindings in a dialog.
 */
export default function Intents() {
  const { t_i18n } = useFormatter();
  const { isAdmin } = useAuth();
  const [showCreateIntent, setShowCreateIntent] = useState(false);
  const [openIntentId, setOpenIntentId] = useState<string | null>(null);

  // The table draws the error with Retry: a failed read never takes the page down.
  const intentsQuery = useIntents(undefined, undefined, { throwOnError: false });
  const verticalsQuery = useVerticals({ throwOnError: false });
  const { data: agents = [] } = useAgents();
  const intents = useMemo(() => intentsQuery.data ?? [], [intentsQuery.data]);
  const verticals = useMemo(() => verticalsQuery.data ?? [], [verticalsQuery.data]);
  const openIntent = intents.find((intent) => intent.id === openIntentId) ?? null;

  const usageLabels = useMemo<Record<IntentUsage, string>>(
    () => ({ used: t_i18n("Used"), unused: t_i18n("Unused"), unbound: t_i18n("Unbound") }),
    [t_i18n],
  );

  const filters = useMemo<DataTableFilter<IntentItem>[]>(
    () => [
      { key: "platform", label: HEADERS.platform, values: (intent) => intent.platforms },
      {
        key: "usage",
        label: HEADERS.usage,
        // Unbound intents are used (a platform declares them) and also listed on their own.
        values: (intent) => (intentUsage(intent) === "unbound" ? ["used", "unbound"] : [intentUsage(intent)]),
        options: (Object.keys(usageLabels) as IntentUsage[]).map((value) => ({ value, label: usageLabels[value] })),
      },
    ],
    [usageLabels],
  );

  const columns = useMemo<DataTableColumn<IntentItem>[]>(
    () => [
      {
        key: "name",
        header: HEADERS.intent,
        className: "w-full max-w-0",
        // Plain text: the row is the one target and the one tab stop, Enter on it opens the
        // bindings dialog (#12070, UX round 2 A1). One line per row: the description lives
        // in the tooltip.
        cell: (intent) => (
          <Tip
            content={
              <span className="block max-w-[360px]">
                <span className="block font-mono-plex">{intent.name}</span>
                {intent.description && <span className="block">{intent.description}</span>}
              </span>
            }
          >
            <span data-testid="intent-name" className="block w-full truncate font-medium font-mono-plex">
              {intent.name}
            </span>
          </Tip>
        ),
        sortValue: (intent) => intent.name,
        csv: (intent) => intent.name,
      },
      {
        key: "description",
        header: "Description",
        cell: (intent) => intent.description ?? "",
        csv: (intent) => intent.description ?? "",
        exportOnly: true,
      },
      {
        key: "platforms",
        header: HEADERS.platform,
        hideBelow: "4xl",
        hideable: true,
        cell: (intent) =>
          intent.platforms.length === 0 ? (
            "-"
          ) : (
            <span className="flex items-center gap-1 whitespace-nowrap">
              <Badge variant="outline">{intent.platforms[0]}</Badge>
              {intent.platforms.length > 1 && (
                <Tip content={intent.platforms.slice(1).join(", ")}>
                  <BadgeFocusTrigger>
                    <Badge variant="outline" className="tabular-nums">
                      {`+${intent.platforms.length - 1}`}
                    </Badge>
                  </BadgeFocusTrigger>
                </Tip>
              )}
            </span>
          ),
        sortValue: (intent) => intent.platforms.join(", "),
        csv: (intent) => intent.platforms.join(", "),
      },
      {
        key: "usage",
        header: HEADERS.usage,
        cell: (intent) => {
          const usage = intentUsage(intent);
          return usage === "unbound" ? (
            <Badge variant="outline" className="gap-1 text-feedback-warning-primary border-feedback-alert-primary/30">
              <AlertTriangle className="h-3 w-3" /> {usageLabels.unbound}
            </Badge>
          ) : (
            <span className="text-default-secondary">{usageLabels[usage]}</span>
          );
        },
        sortValue: (intent) => intentUsage(intent),
        csv: (intent) => usageLabels[intentUsage(intent)],
      },
      {
        key: "bindings",
        header: HEADERS.bindings,
        cell: (intent) => intent.bindings.length,
        sortValue: (intent) => intent.bindings.length,
        csv: (intent) => intent.bindings.length,
        align: "right",
        className: "tabular-nums",
        firstSort: "desc",
      },
    ],
    [usageLabels],
  );

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-start justify-between">
        <div>
          <h1 className="text-3xl font-bold">{t_i18n("Intents")}</h1>
          <p className="mt-2 text-default-secondary">
            {t_i18n("Map AI capabilities to agents: platforms declare intents, you bind agents.")}
          </p>
        </div>
        {isAdmin && (
          <Dialog open={showCreateIntent} onOpenChange={setShowCreateIntent}>
            <DialogTrigger asChild>
              <Button className="gap-1.5">
                <Plus className="h-4 w-4" /> {t_i18n("Create Intent")}
              </Button>
            </DialogTrigger>
            <CreateIntentDialog onClose={() => setShowCreateIntent(false)} />
          </Dialog>
        )}
      </div>

      {/* Vertical panel */}
      <VerticalPanel
        verticals={verticals}
        isLoading={verticalsQuery.isLoading}
        isError={verticalsQuery.isError}
        onRetry={() => void verticalsQuery.refetch()}
      />

      {/* Intent list */}
      {!intentsQuery.isLoading && !intentsQuery.isError && intents.length === 0 ? (
        <EmptyState
          icon={Zap}
          title="No intents yet"
          requiresAi
          description="Intents appear when platforms register, or you can create them manually."
          actionLabel={isAdmin ? "Create Intent" : undefined}
          onAction={isAdmin ? () => setShowCreateIntent(true) : undefined}
        />
      ) : (
        <DataTable
          rows={intents}
          columns={columns}
          rowKey={(intent) => intent.id}
          defaultSort={{ key: "name", direction: "asc" }}
          searchFields={(intent) => [intent.name, intent.description, ...intent.platforms]}
          searchPlaceholder={t_i18n("Search intents…")}
          filters={filters}
          urlKey="in_"
          storageKey="intents"
          exportName="intents"
          defaultPageSize={25}
          isLoading={intentsQuery.isLoading}
          isError={intentsQuery.isError}
          onRetry={() => void intentsQuery.refetch()}
          emptyLabel={t_i18n("No intents yet")}
          noResultsLabel={t_i18n("No intents match your filter.")}
          onRowClick={(intent) => setOpenIntentId(intent.id)}
          rowClassName={() => "cursor-pointer"}
          rowTestId="intent-row"
          rowActions={(intent) => (
            <span className="inline-flex gap-1">
              <IntentActions intent={intent} agents={agents} verticals={verticals} iconOnly />
            </span>
          )}
        />
      )}

      <Dialog open={openIntent !== null} onOpenChange={(open) => !open && setOpenIntentId(null)}>
        {openIntent && <IntentBindingsDialog intent={openIntent} agents={agents} verticals={verticals} />}
      </Dialog>
    </div>
  );
}
