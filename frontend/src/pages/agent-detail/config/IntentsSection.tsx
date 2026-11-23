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

import { useState } from "react";
import { Zap, Layers, Globe, Plus, Check, X } from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { CountBadge } from "@/components/ui/count-badge";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { SearchInput } from "@/components/ui/search-input";
import {
  Dialog, DialogContent, DialogHeader, DialogTitle, DialogTrigger,
} from "@/components/ui/dialog";
import {
  useIntents, useVerticals, useAddIntentBindings, useDeleteIntentBinding,
} from "@/hooks/api/useIntentApi";
import { useFormatter } from "@/i18n";
import type { Agent, IntentBinding } from "@/types";
import { useAiActionDisabled } from "@/hooks/useAiDisabled";
import { AiDisabledTip } from "@/components/ui/ai-disabled";

// ── Component ───────────────────────────────────────────────────────────────

export function IntentsSection({ agent, canManage }: { agent: Agent; canManage: boolean }) {
  return <IntentBindingsCard agent={agent} canManage={canManage} />;
}

// ── Intent × Vertical Bindings Card ─────────────────────────────────────────

function IntentBindingsCard({ agent, canManage }: { agent: Agent; canManage: boolean }) {
  const { t_i18n } = useFormatter();
  const [showAddDialog, setShowAddDialog] = useState(false);

  const { data: allIntents = [] } = useIntents();

  // Get this agent's bindings from all intents
  const agentBindings: (IntentBinding & { intentName: string })[] = [];
  for (const intent of allIntents) {
    for (const b of intent.bindings) {
      if (b.agent_id === agent.id) {
        agentBindings.push({ ...b, intentName: intent.name });
      }
    }
  }

  // Group bindings by intent for display
  const grouped = new Map<string, { intentName: string; bindings: (IntentBinding & { intentName: string })[] }>();
  for (const b of agentBindings) {
    if (!grouped.has(b.intentName)) {
      grouped.set(b.intentName, { intentName: b.intentName, bindings: [] });
    }
    grouped.get(b.intentName)!.bindings.push(b);
  }

  const deleteMutation = useDeleteIntentBinding();
  const aiOff = useAiActionDisabled();

  return (
    <Card>
      <CardHeader className="pb-2">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-2">
            <Zap className="h-3.5 w-3.5 text-accent" />
            <CardTitle className="text-base">{t_i18n("Intent Bindings")}</CardTitle>
            <CountBadge variant="outline">
              {agentBindings.length}
            </CountBadge>
          </div>
          {canManage && (
            <Dialog open={showAddDialog} onOpenChange={setShowAddDialog}>
              <DialogTrigger asChild>
                <Button variant="outline" size="sm" className="content-compact gap-1">
                  <Plus className="h-3 w-3" /> {t_i18n("Add")}
                </Button>
              </DialogTrigger>
              <AddBindingDialog
                key={String(showAddDialog)}
                agentId={agent.id}
                existingBindings={agentBindings}
                onClose={() => setShowAddDialog(false)}
              />
            </Dialog>
          )}
        </div>
      </CardHeader>
      <CardContent>
        {agentBindings.length === 0 ? (
          <p className="content-compact text-default-secondary py-4 text-center">
            {t_i18n("No intent bindings yet. Add bindings to declare which intents this agent handles.")}
          </p>
        ) : (
          <div className="space-y-2">
            {[...grouped.entries()].map(([intentName, group]) => (
              <div key={intentName} className="rounded-sm border border-elevation-subtle-soft bg-elevation-hover p-3">
                <div className="flex items-center gap-2 mb-2">
                  <Zap className="h-3 w-3 text-accent" />
                  <span className="content-compact font-mono-plex font-medium text-default-primary">{intentName}</span>
                </div>
                <div className="flex flex-wrap gap-1.5">
                  {group.bindings.map((b) => (
                    <div
                      key={b.id}
                      className="inline-flex items-center gap-1 px-2 py-1 rounded-sm text-[11px] border bg-elevation-hover border-elevation-subtle-soft"
                    >
                      {b.vertical ? (
                        <>
                          <Layers className="h-2.5 w-2.5 text-filigran-ia-primary" />
                          <span className="text-filigran-ia-primary">{b.vertical}</span>
                        </>
                      ) : (
                        <>
                          <Globe className="h-2.5 w-2.5 text-default-secondary" />
                          <span className="text-default-secondary">{t_i18n("All verticals")}</span>
                        </>
                      )}
                      {canManage && (
                        <AiDisabledTip>
                        <button aria-label={t_i18n("Delete binding")}
                          className="ml-0.5 text-default-secondary hover:text-feedback-error-primary transition-colors"
                          onClick={() => deleteMutation.mutate({ intentId: b.intent_id, bindingId: b.id, agentId: agent.id })}
                          disabled={deleteMutation.isPending || aiOff}
                        >
                          <X className="h-2.5 w-2.5" />
                        </button>
                        </AiDisabledTip>
                      )}
                    </div>
                  ))}
                </div>
              </div>
            ))}
          </div>
        )}
        <p className="content-caption text-default-secondary mt-3">
          {t_i18n("Each binding declares that this agent handles a specific intent, optionally scoped to one or more verticals.")}
        </p>
      </CardContent>
    </Card>
  );
}

// ── Add Binding Dialog ──────────────────────────────────────────────────────

function AddBindingDialog({
  agentId,
  existingBindings,
  onClose,
}: {
  agentId: string;
  existingBindings: (IntentBinding & { intentName: string })[];
  onClose: () => void;
}) {
  const { t_i18n } = useFormatter();
  const [intentId, setIntentId] = useState("");
  const [intentSearch, setIntentSearch] = useState("");
  const [intentName, setIntentName] = useState("");
  const [pickerOpen, setPickerOpen] = useState(false);
  const [selectedVerticals, setSelectedVerticals] = useState<string[]>([]);
  const [universalSelected, setUniversalSelected] = useState(false);

  const { data: allIntents = [] } = useIntents();
  const { data: allVerticals = [] } = useVerticals();

  const filteredIntents = allIntents.filter(
    (i) => !intentSearch || i.name.toLowerCase().includes(intentSearch.toLowerCase()),
  );

  // Determine which verticals are already bound for the selected intent
  const alreadyBound = new Set(
    existingBindings
      .filter((b) => b.intent_id === intentId)
      .map((b) => b.vertical ?? "__universal__"),
  );

  const toggleVertical = (name: string) => {
    if (universalSelected) return; // can't pick verticals when universal is on
    setSelectedVerticals((prev) =>
      prev.includes(name) ? prev.filter((v) => v !== name) : [...prev, name],
    );
  };

  const toggleUniversal = () => {
    setUniversalSelected((prev) => !prev);
    if (!universalSelected) setSelectedVerticals([]); // clear specific verticals
  };

  // Create bindings: one per selected vertical (or one with null for universal)
  const mutation = useAddIntentBindings();
  const handleAdd = () =>
    mutation.mutate(
      { intentId, agentId, verticals: universalSelected ? [null] : selectedVerticals },
      { onSuccess: onClose },
    );

  const canSubmit =
    intentId &&
    (universalSelected || selectedVerticals.length > 0) &&
    !mutation.isPending;

  const selectionCount = universalSelected ? 1 : selectedVerticals.length;

  return (
    <DialogContent className="max-w-md">
      <DialogHeader>
        <DialogTitle>{t_i18n("Add Intent Binding")}</DialogTitle>
      </DialogHeader>
      <div className="space-y-4 py-2">
        {/* Intent picker */}
        <div>
          <Label htmlFor="binding-intent-search" className="block mb-1.5">{t_i18n("Intent")}</Label>
          {intentId ? (
            <div className="flex items-center gap-2.5 rounded-sm border border-elevation-subtle bg-elevation-default px-3 py-2">
              <Zap className="h-3.5 w-3.5 text-accent shrink-0" />
              <span className="content-base flex-1 truncate font-mono-plex">{intentName}</span>
              <button aria-label={t_i18n("Clear selection")}
                type="button"
                className="text-default-secondary hover:text-default-primary transition-colors"
                onClick={() => {
                  setIntentId("");
                  setIntentName("");
                  setSelectedVerticals([]);
                  setUniversalSelected(false);
                }}
              >
                <X className="h-3.5 w-3.5" />
              </button>
            </div>
          ) : (
            <div className="relative">
              <SearchInput
                id="binding-intent-search"
                className="max-w-none"
                inputClassName="[&_input]:font-mono-plex"
                placeholder={t_i18n("Search intents…")}
                value={intentSearch}
                onChange={(value) => { setIntentSearch(value); setPickerOpen(true); }}
                onClick={() => setPickerOpen(true)}
              />
              {pickerOpen && (
                <div className="absolute z-50 mt-1 w-full rounded-sm border border-elevation-subtle layer-2 bg-elevation-default shadow-overlay max-h-52 overflow-y-auto">
                  {filteredIntents.length > 0 ? (
                    filteredIntents.map((intent) => (
                      <button
                        key={intent.id}
                        type="button"
                        className="flex items-center gap-2.5 w-full px-3 py-2 text-left content-compact transition-colors hover:bg-elevation-hover"
                        onClick={() => {
                          setIntentId(intent.id);
                          setIntentName(intent.name);
                          setIntentSearch("");
                          setPickerOpen(false);
                        }}
                      >
                        <Zap className="h-3.5 w-3.5 text-default-secondary shrink-0" />
                        <span className="truncate flex-1 font-mono-plex">{intent.name}</span>
                        {intent.description && (
                          <span className="content-caption text-default-secondary shrink-0 truncate max-w-[120px]">
                            {intent.description}
                          </span>
                        )}
                      </button>
                    ))
                  ) : (
                    <p className="px-3 py-3 text-[11px] text-default-secondary text-center">
                      {intentSearch ? t_i18n("No matching intents.") : t_i18n("No intents defined.")}
                    </p>
                  )}
                </div>
              )}
            </div>
          )}
        </div>

        {/* Vertical multi-select */}
        {intentId && (
          <div>
            <label className="content-compact font-medium text-default-secondary block mb-1.5">
              {t_i18n("Verticals")}
            </label>
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
                  universalSelected
                    ? "bg-accent border-accent"
                    : "border-elevation-subtle-soft"
                }`}>
                  {universalSelected && <Check className="h-2.5 w-2.5 text-negative-primary" />}
                </div>
                <Globe className="h-3 w-3" />
                <span>{t_i18n("All verticals")}</span>
                {alreadyBound.has("__universal__") && (
                  <span className="text-[9px] text-default-secondary ml-auto">{t_i18n("already bound")}</span>
                )}
              </button>

              {/* Separator */}
              {allVerticals.length > 0 && (
                <div className="border-t border-elevation-subtle-soft mx-1 my-1" />
              )}

              {/* Individual verticals */}
              {allVerticals.map((v) => {
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
                      isSelected
                        ? "bg-filigran-ia-primary border-filigran-ia-primary"
                        : "border-elevation-subtle-soft"
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

              {allVerticals.length === 0 && (
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
              ? t_i18n("Saving…")
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
