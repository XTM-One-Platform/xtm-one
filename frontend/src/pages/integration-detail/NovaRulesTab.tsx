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

import { useEffect, useId, useMemo, useState } from "react";
import {
  Shield,
  Plus,
  Pencil,
  Trash2,
  Check,
  Ban,
  Eye,
  AlertTriangle,
  RefreshCw,
  Github,
  Sparkles,
} from "lucide-react";
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
  CardDescription,
} from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Badge } from "@/components/ui/badge";
import { Switch } from "@/components/ui/switch";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
} from "@/components/ui/dialog";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { SearchableSelect } from "@/components/ui/searchable-select";
import { useConfirm } from "@/components/ui/confirm-dialog";
import { useToast } from "@/components/ui/toast";
import { useFormatter } from "@/i18n";
import {
  useAvailableEmbeddingModels,
  useAvailableModels,
} from "@/hooks/api/useSettingsApi";
import {
  useNovaRules,
  useCreateNovaRule,
  useUpdateNovaRule,
  useDeleteNovaRule,
  useValidateNovaRule,
  useNovaEngineStatus,
  useUpdateNovaModels,
  useNovaRuleSources,
  useUpdateNovaRuleSource,
  useSyncNovaRuleSource,
} from "@/hooks/api/useNovaRuleApi";
import type {
  NovaRule,
  NovaRuleAction,
  NovaRuleValidation,
} from "@/types";
import { useAiActionDisabled } from "@/hooks/useAiDisabled";
import { Spinner } from "@filigran/design-system";

const RULE_TEMPLATE = `rule MyRule {
  meta:
    category = "Jailbreak"
    description = "What this rule detects"
  keywords:
    $a = "do anything now"
    $b = /ignore (all|previous) instructions/i
  condition:
    any of them
}`;

interface DraftState {
  id: string | null;
  name: string;
  category: string;
  action: NovaRuleAction;
  enabled: boolean;
  rule_text: string;
}

const BLANK_DRAFT: DraftState = {
  id: null,
  name: "",
  category: "",
  action: "block",
  enabled: true,
  rule_text: RULE_TEMPLATE,
};

// ---------------------------------------------------------------------------
// Nova prompt-safety rules — admin sub-page under /integrations/:id/rules.
// Rules managed here are company-managed: the guard loads only those, so they
// apply platform-wide before any agent's LLM call. The tab also surfaces the
// GitHub rule-sync source and the engine status.
// ---------------------------------------------------------------------------
export function NovaRulesTab(_props: { integration?: unknown }) {
  const { t_i18n } = useFormatter();
  const fieldId = useId();
  const { toast } = useToast();
  const [ConfirmEl, confirm] = useConfirm();

  const { data: rules, isLoading } = useNovaRules();
  const createRule = useCreateNovaRule();
  const aiOff = useAiActionDisabled();
  const updateRule = useUpdateNovaRule();
  const deleteRule = useDeleteNovaRule();
  const validate = useValidateNovaRule();

  const { data: engineStatus } = useNovaEngineStatus();
  // The rules' semantics: thresholds were written for another model than the
  // one scoring them. An unknown status, or no embedding model at all,
  // flags nothing.
  const calibration = engineStatus?.semantic_calibration;
  const semanticUncalibrated =
    !!engineStatus?.engine_available &&
    !!calibration?.configured_model &&
    calibration.calibrated === false;
  const { data: availableModels } = useAvailableModels({ scope: "all" });
  const { data: availableEmbeddingModels } = useAvailableEmbeddingModels();
  const updateModels = useUpdateNovaModels();
  const { data: sources } = useNovaRuleSources();
  const updateSource = useUpdateNovaRuleSource();
  const syncSource = useSyncNovaRuleSource();
  const source = sources?.[0];

  const [open, setOpen] = useState(false);
  const [draft, setDraft] = useState<DraftState>(BLANK_DRAFT);
  const [validation, setValidation] = useState<NovaRuleValidation | null>(null);

  // Live-validate the rule text (debounced) so the author sees parse errors and
  // which tiers the rule uses before saving.
  useEffect(() => {
    if (!open) return;
    const text = draft.rule_text.trim();
    if (!text) {
      setValidation(null);
      return;
    }
    const handle = setTimeout(() => {
      validate.mutate(text, { onSuccess: (res) => setValidation(res) });
    }, 400);
    return () => clearTimeout(handle);
    // validate.mutate identity is stable; re-run only when the text changes.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [draft.rule_text, open]);

  const platformRules = useMemo(
    () => (rules ?? []).filter((r) => r.is_company_managed),
    [rules],
  );

  const openCreate = () => {
    setDraft(BLANK_DRAFT);
    setValidation(null);
    setOpen(true);
  };

  const openEdit = (rule: NovaRule) => {
    setDraft({
      id: rule.id,
      name: rule.name,
      category: rule.category,
      action: rule.action,
      enabled: rule.enabled,
      rule_text: rule.rule_text,
    });
    setValidation(null);
    setOpen(true);
  };

  const handleSave = () => {
    const payload = {
      name: draft.name.trim(),
      category: draft.category.trim(),
      action: draft.action,
      enabled: draft.enabled,
      rule_text: draft.rule_text,
    };
    const onError = (err: unknown) => {
      const detail =
        (err as { response?: { data?: { detail?: string } } })?.response?.data
          ?.detail ?? t_i18n("Could not save the rule.");
      toast({ title: t_i18n("Save failed"), description: detail, variant: "error" });
    };
    const onSuccess = () => {
      toast({ title: t_i18n("Rule saved"), variant: "success" });
      setOpen(false);
    };
    if (draft.id) {
      updateRule.mutate({ id: draft.id, ...payload }, { onSuccess, onError });
    } else {
      // Admin-authored rules are platform rules — the guard loads only these.
      createRule.mutate(
        { ...payload, is_company_managed: true },
        { onSuccess, onError },
      );
    }
  };

  const handleToggleEnabled = (rule: NovaRule) => {
    updateRule.mutate({ id: rule.id, enabled: !rule.enabled });
  };

  const handleDelete = async (rule: NovaRule) => {
    const ok = await confirm({
      title: t_i18n("Delete this Nova rule?"),
      description: t_i18n(
        "'{name}' will stop guarding prompts platform-wide. This cannot be undone.",
        { name: rule.name },
      ),
      confirmLabel: t_i18n("Delete"),
      variant: "danger",
    });
    if (!ok) return;
    deleteRule.mutate(rule.id, {
      onSuccess: () => toast({ title: t_i18n("Rule deleted"), variant: "success" }),
    });
  };

  /* No `aiOff`: nova-rule create/update take `require_agentic_ai_licensed`, so
     the server permits them with AI off, and `saving` renders the spinner —
     which meant a permanent one (/code-review, 2026-08-28). */
  const saving = createRule.isPending || updateRule.isPending;
  const canSave = draft.name.trim().length > 0 && validation?.valid === true;

  return (
    <div className="space-y-6">
      {/* Engine status: which tiers actually run right now. */}
      <div className="rounded-sm border border-elevation-subtle-soft bg-elevation-hover p-4 flex items-start gap-3">
        <Sparkles className="h-4 w-4 text-feedback-error-primary mt-0.5 shrink-0" />
        <div className="content-base">
          {engineStatus?.engine_available ? (
            <p className="text-default-primary">
              {t_i18n(
                "All tiers run: keyword/regex locally, semantics and llm on the models selected below.",
              )}
            </p>
          ) : (
            <p className="text-default-secondary">
              {t_i18n(
                "Keyword tier only. Install the Nova engine (nova-hunting) to run the semantics: and llm: rule tiers. No ML dependency is added: they run on the platform's own models.",
              )}
            </p>
          )}
        </div>
      </div>

      {/* Models the advanced tiers run on. */}
      {engineStatus?.engine_available && (
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <Sparkles className="h-4 w-4 text-feedback-error-primary" />
              {t_i18n("Models")}
            </CardTitle>
            <CardDescription>
              {t_i18n(
                "Which models the advanced rule tiers use. The keyword tier never calls a model.",
              )}
            </CardDescription>
          </CardHeader>
          <CardContent className="grid gap-4 sm:grid-cols-2">
            <div className="space-y-1.5">
              <Label id={`${fieldId}-llm-model-label`}>{t_i18n("LLM model (llm: tier)")}</Label>
              <SearchableSelect
                aria-label={t_i18n("LLM model (llm: tier)")}
                aria-labelledby={`${fieldId}-llm-model-label`}
                options={(availableModels ?? []).map((m) => ({ value: m.model, label: m.model, keywords: [m.provider] }))}
                pinned={[{ value: "__default__", label: t_i18n("Platform default") }]}
                value={engineStatus?.llm_model || "__default__"}
                onValueChange={(v) =>
                  updateModels.mutate({
                    nova_llm_model: v === "__default__" ? "" : v,
                  })
                }
                placeholder={t_i18n("Search models...")}
              />
            </div>
            <div className="space-y-1.5">
              <Label id={`${fieldId}-semantic-model-label`}>{t_i18n("Semantic model (semantics: tier)")}</Label>
              <SearchableSelect
                aria-label={t_i18n("Semantic model (semantics: tier)")}
                aria-labelledby={`${fieldId}-semantic-model-label`}
                options={(availableEmbeddingModels ?? []).map((m) => ({ value: m.model, label: m.model, keywords: [m.provider] }))}
                pinned={[{ value: "__default__", label: t_i18n("Platform embedding model") }]}
                value={engineStatus?.semantic_model || "__default__"}
                onValueChange={(v) =>
                  updateModels.mutate(
                    { nova_semantic_model: v === "__default__" ? "" : v },
                    {
                      // The platform embedding provider must serve the model;
                      // `api` throws an Error whose message is the refusal.
                      onError: (err: unknown) =>
                        toast({
                          title: t_i18n("Save failed"),
                          description:
                            err instanceof Error && err.message ? err.message : t_i18n("Failed to save"),
                          variant: "error",
                        }),
                    },
                  )
                }
                placeholder={t_i18n("Search models...")}
              />
            </div>
            {semanticUncalibrated && calibration && (
              <p className="sm:col-span-2 flex items-start gap-2 content-compact text-feedback-warning-primary">
                <AlertTriangle className="h-3.5 w-3.5 mt-0.5 shrink-0" />
                {t_i18n(
                  "The rules' semantic thresholds were written for {reference}, not for {model}, so their semantics: matches may be too loose or too strict. Keep those rules on Log until this model is calibrated.",
                  {
                    reference: calibration.calibrated_for,
                    model: calibration.configured_model ?? "",
                  },
                )}
              </p>
            )}
            {engineStatus?.engine_available && calibration?.override_ignored && (
              <p className="sm:col-span-2 flex items-start gap-2 content-compact text-feedback-warning-primary">
                <AlertTriangle className="h-3.5 w-3.5 mt-0.5 shrink-0" />
                {t_i18n(
                  "The semantic model {override} is not used: the platform embedding provider cannot embed with it, so the semantics: tier scores on {model}. Pick a model of the platform embedding provider.",
                  {
                    override: calibration.override_ignored,
                    model: calibration.configured_model ?? "",
                  },
                )}
              </p>
            )}
          </CardContent>
        </Card>
      )}

      {/* Rule sync source (GitHub). */}
      {source && (
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <Github className="h-4 w-4" /> {t_i18n("Rule source")}
            </CardTitle>
            <CardDescription>
              {t_i18n("Community rules pulled from a GitHub repo and refreshed on a schedule.")}
            </CardDescription>
          </CardHeader>
          <CardContent>
            <div className="flex flex-wrap items-center gap-3 justify-between">
              <div className="min-w-0">
                <p className="content-base font-medium truncate">{source.repo}</p>
                <span className="content-compact text-default-secondary">
                  {source.last_synced_at
                    ? t_i18n("Last synced: {status} · {count} rules", {
                        status: source.last_sync_status ?? "—",
                        count: source.last_sync_rules,
                      })
                    : t_i18n("Not synced yet")}
                  {source.last_sync_error ? ` · ${source.last_sync_error}` : ""}
                </span>
              </div>
              <div className="flex items-center gap-2 shrink-0">
                <Select
                  value={source.sync_interval}
                  onValueChange={(v) =>
                    updateSource.mutate({ id: source.id, sync_interval: v })
                  }
                >
                  <SelectTrigger aria-label={t_i18n("Sync Schedule")} className="w-36"><SelectValue /></SelectTrigger>
                  <SelectContent>
                    <SelectItem value="manual">{t_i18n("Manual")}</SelectItem>
                    <SelectItem value="6h">{t_i18n("Every 6h")}</SelectItem>
                    <SelectItem value="12h">{t_i18n("Every 12h")}</SelectItem>
                    <SelectItem value="daily">{t_i18n("Daily")}</SelectItem>
                    <SelectItem value="weekly">{t_i18n("Weekly")}</SelectItem>
                  </SelectContent>
                </Select>
                <Button
                  variant="secondary"
                  className="gap-1.5"
                  disabled={syncSource.isPending || aiOff}
                  onClick={() =>
                    syncSource.mutate(source.id, {
                      onSuccess: (d) =>
                        toast({
                          title: t_i18n("Sync complete"),
                          description: t_i18n("{count} rules synced.", { count: d.synced_rules }),
                          variant: "success",
                        }),
                      onError: () =>
                        toast({ title: t_i18n("Sync failed"), variant: "error" }),
                    })
                  } aria-busy={syncSource.isPending || undefined}>
                  {syncSource.isPending && <Spinner size="sm" tone="inherit" />}{!syncSource.isPending && <RefreshCw className="h-3.5 w-3.5" />}
                  {t_i18n("Sync now")}
                </Button>
              </div>
            </div>
          </CardContent>
        </Card>
      )}

    <Card>
      <CardHeader>
        <div className="flex items-start justify-between gap-4">
          <div>
            <CardTitle className="flex items-center gap-2">
              <Shield className="h-4 w-4 text-feedback-error-primary" />
              {t_i18n("Prompt Safety (Nova rules)")}
            </CardTitle>
            <CardDescription>
              {t_i18n(
                "Active pre-LLM guard: each rule scans a user's prompt before any agent LLM call. A 'block' match refuses the prompt; a 'log' match only flags it in the audit trail. Rules use the Nova .nov format (keyword/regex tier).",
              )}
            </CardDescription>
          </div>
          <Button size="sm" className="gap-1.5 shrink-0" onClick={openCreate}>
            <Plus className="h-3.5 w-3.5" /> {t_i18n("New rule")}
          </Button>
        </div>
      </CardHeader>
      <CardContent>
        {isLoading ? (
          <div className="flex items-center gap-2 content-base text-default-secondary py-6">
            <Spinner size="sm" tone="inherit" /> {t_i18n("Loading rules…")}
          </div>
        ) : platformRules.length === 0 ? (
          <div className="rounded-sm border border-elevation-subtle-soft bg-elevation-hover p-6 text-center">
            <Shield className="h-6 w-6 mx-auto text-default-secondary" />
            <p className="content-base text-default-secondary mt-2">
              {t_i18n("No platform prompt-safety rules yet.")}
            </p>
            <p className="content-compact text-default-secondary mt-1">
              {t_i18n("Create a rule to start guarding prompts across every agent.")}
            </p>
          </div>
        ) : (
          <div className="space-y-2">
            {platformRules.map((rule) => (
              <div
                key={rule.id}
                className="flex items-center justify-between gap-3 rounded-sm border border-elevation-subtle-soft bg-elevation-default p-3"
              >
                <div className="min-w-0">
                  <div className="flex items-center gap-2">
                    <p className="content-base font-medium truncate">{rule.name}</p>
                    {rule.action === "block" ? (
                      <Badge variant="outline" className="gap-1 content-caption border-feedback-error-primary/40 bg-feedback-error-primary/10 text-feedback-error-primary">
                        <Ban className="h-2.5 w-2.5" /> {t_i18n("Block")}
                      </Badge>
                    ) : (
                      <Badge variant="outline" className="gap-1 content-caption border-feedback-alert-primary/40 bg-feedback-alert-primary/10 text-feedback-warning-tertiary">
                        <Eye className="h-2.5 w-2.5" /> {t_i18n("Log")}
                      </Badge>
                    )}
                    {rule.category && (
                      <Badge variant="outline" className="content-caption">
                        {rule.category}
                      </Badge>
                    )}
                    {rule.has_semantics && semanticUncalibrated && (
                      <Badge variant="outline" className="gap-1 content-caption border-feedback-alert-primary/40 text-feedback-warning-primary">
                        <AlertTriangle className="h-2.5 w-2.5" /> {t_i18n("Uncalibrated semantics")}
                      </Badge>
                    )}
                    {!rule.enabled && (
                      <span className="content-caption text-default-secondary uppercase tracking-wider">
                        {t_i18n("disabled")}
                      </span>
                    )}
                  </div>
                  {rule.description && (
                    <p className="content-compact text-default-secondary truncate mt-0.5">
                      {rule.description}
                    </p>
                  )}
                </div>
                <div className="flex items-center gap-2 shrink-0">
                  <Switch
                    checked={rule.enabled}
                    onCheckedChange={() => handleToggleEnabled(rule)}
                    aria-label={t_i18n("Enable rule")}
                  />
                  <Button variant="ghost" size="icon" aria-label={t_i18n("Edit")} className="h-8 w-8" onClick={() => openEdit(rule)}>
                    <Pencil className="h-3.5 w-3.5" />
                  </Button>
                  <Button aria-label={t_i18n("Delete")} variant="ghost" size="icon" className="h-8 w-8 text-feedback-error-primary hover:text-feedback-error-primary" onClick={() => handleDelete(rule)} disabled={aiOff}>
                    <Trash2 className="h-3.5 w-3.5" />
                  </Button>
                </div>
              </div>
            ))}
          </div>
        )}
      </CardContent>

      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent className="max-w-2xl">
          <DialogHeader>
            <DialogTitle>
              {draft.id ? t_i18n("Edit Nova rule") : t_i18n("New Nova rule")}
            </DialogTitle>
            <DialogDescription>
              {t_i18n("Platform rule: it guards prompts for every agent before the LLM call.")}
            </DialogDescription>
          </DialogHeader>

          <div className="space-y-4">
            <div className="grid grid-cols-2 gap-3">
              <div className="space-y-1.5">
                <Label htmlFor={`${fieldId}-name`}>{t_i18n("Name")}</Label>
                <Input id={`${fieldId}-name`} aria-label={t_i18n("Name")}
                  value={draft.name}
                  onChange={(e) => setDraft((d) => ({ ...d, name: e.target.value }))}
                  placeholder={t_i18n("Jailbreak guard")}
                />
              </div>
              <div className="space-y-1.5">
                <Label htmlFor={`${fieldId}-category`}>{t_i18n("Category")}</Label>
                <Input id={`${fieldId}-category`} aria-label={t_i18n("Category")}
                  value={draft.category}
                  onChange={(e) => setDraft((d) => ({ ...d, category: e.target.value }))}
                  placeholder={t_i18n("Defaults to the rule's meta.category")}
                />
              </div>
            </div>

            <div className="flex items-center gap-6">
              <div className="space-y-1.5">
                <Label id={`${fieldId}-action-label`}>{t_i18n("Action on match")}</Label>
                <Select
                  value={draft.action}
                  onValueChange={(v) => setDraft((d) => ({ ...d, action: v as NovaRuleAction }))}
                >
                  <SelectTrigger className="w-40" aria-labelledby={`${fieldId}-action-label`}><SelectValue /></SelectTrigger>
                  <SelectContent>
                    <SelectItem value="block">{t_i18n("Block the prompt")}</SelectItem>
                    <SelectItem value="log">{t_i18n("Log only")}</SelectItem>
                  </SelectContent>
                </Select>
              </div>
              <div className="space-y-1.5">
                <Label htmlFor={`${fieldId}-enabled`}>{t_i18n("Enabled")}</Label>
                <div className="h-9 flex items-center">
                  <Switch
                    id={`${fieldId}-enabled`}
                    aria-label={t_i18n("Enabled")}
                    checked={draft.enabled}
                    onCheckedChange={(v) => setDraft((d) => ({ ...d, enabled: v }))}
                  />
                </div>
              </div>
            </div>

            <div className="space-y-1.5">
              <Label htmlFor={`${fieldId}-rule-text`}>{t_i18n("Rule (.nov)")}</Label>
              <textarea
                id={`${fieldId}-rule-text`}
                value={draft.rule_text}
                onChange={(e) => setDraft((d) => ({ ...d, rule_text: e.target.value }))}
                spellCheck={false}
                rows={12}
                className="w-full rounded-sm border border-elevation-subtle bg-elevation-default p-3 font-mono-plex content-compact leading-relaxed text-default-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus"
              />
              {validation && (
                validation.valid ? (
                  <div className="flex items-center gap-2 content-compact text-feedback-success-primary">
                    <Check className="h-3.5 w-3.5" />
                    {t_i18n("Valid rule")}
                    {validation.tiers.length > 0 && (
                      <span className="text-default-secondary">
                        · {t_i18n("tiers")}: {validation.tiers.join(", ")}
                      </span>
                    )}
                    {validation.tiers.some((tier) => tier === "semantics" || tier === "llm") && (
                      <span className="text-feedback-warning-primary">
                        · {t_i18n("semantics/llm tiers need the optional engine")}
                      </span>
                    )}
                  </div>
                ) : (
                  <div className="flex items-center gap-2 content-compact text-feedback-error-primary">
                    <AlertTriangle className="h-3.5 w-3.5" />
                    {validation.error}
                  </div>
                )
              )}
              {validation?.valid && validation.tiers.includes("llm") && (
                <p className="content-compact text-default-secondary">
                  {t_i18n(
                    "In an llm: pattern, the number in parentheses is the temperature of the model's yes/no call (0 to 1), not a confidence threshold.",
                  )}
                </p>
              )}
            </div>
          </div>

          <div className="flex justify-end gap-2 pt-2">
            <Button variant="ghost" onClick={() => setOpen(false)}>{t_i18n("Cancel")}</Button>
            <Button disabled={!canSave || saving || aiOff} onClick={handleSave} className="gap-1.5" aria-busy={saving || undefined}>
              {saving && <Spinner size="sm" tone="inherit" />}{!saving && <Check className="h-3.5 w-3.5" />}
              {t_i18n("Save rule")}
            </Button>
          </div>
        </DialogContent>
      </Dialog>
      {ConfirmEl}
    </Card>
    </div>
  );
}
