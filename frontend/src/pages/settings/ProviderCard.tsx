// SPDX-License-Identifier: Apache-2.0

import { useEffect, useId, useRef, useState } from "react";
import { Link } from "react-router-dom";
import {
  AlertTriangle,
  Ban,
  Bot,
  Check,
  Lock,
  Link2,
  Pencil,
  RefreshCw,
  Trash2,
  Sparkles,
  ScanSearch,
  Search,
  UsersRound,
} from "lucide-react";
import { AiDisabledTip } from "@/components/ui/ai-disabled";
import { Badge } from "@/components/ui/badge";
import { EnvLockBadge } from "@/components/ui/env-lock-badge";
import { PrincipalSharingCard } from "@/components/ui/principal-sharing-card";
import { Tip } from "@/components/ui/tooltip";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Switch } from "@/components/ui/switch";
import { useToast } from "@/components/ui/toast";
import { VaultSecretsNotice } from "@/components/ui/vault-secrets";
import { useProviderModels, useRefreshProviderModels, useScanContextWindows } from "@/hooks/api/useSettingsApi";
import { useAiActionDisabled } from "@/hooks/useAiDisabled";
import { cn, pluralize, providerConfigName, toggleArrayItem } from "@/lib/utils";
import { formatTokenCount, parseTokenCount } from "@/lib/token-count";
import { useFormatter } from "@/i18n";
import type { ContextWindowScan, ContextWindowSource, LLMProvider, LLMProviderAccess } from "@/types";
import { PROVIDER_DISPLAY_NAMES } from "@/lib/constants";
import { Spinner } from "@filigran/design-system";

/** The restriction's wording on a provider, translated by the card. @i18n-keys */
const PROVIDER_RESTRICTION_COPY = {
  title: "Restrict to people and groups",
  restricted:
    "Only the people and groups below can use this provider: their chats, and the agents and assignments run for them, use it first. Everyone else never reaches it, administrators included.",
  open: "Everyone on the platform can use this provider. Toggle on to reserve it for specific people and groups, for example to bill a team's usage to its own API key.",
  empty: "No one is listed yet, so no one can use this provider.",
} as const;

/** The exclusion list's wording, on the same card as the restriction. */
const PROVIDER_EXCLUSION_COPY = {
  title: "Never use for people and groups",
  restricted:
    "This provider is never used for the people and groups below, whatever the restriction grants: their chats, agents, assignments, memory and knowledge base content go to the providers left to them, or are refused when none is left. If it serves embeddings or image generation, those are refused to them too.",
  open: "No one is excluded. Toggle on to keep specific people and groups off this provider, for example where a regulation forbids its vendor for some staff.",
  empty: "No one is listed yet, so no one is excluded.",
} as const;

/** Where a scanned context window came from, as its field's tooltip says it. @i18n-keys */
const CONTEXT_WINDOW_SOURCE_COPY = {
  provider: "Found in the provider's model list.",
  catalog: "Found in the model catalog.",
  family: "Estimated for the model family: check it against the provider's documentation.",
} as const satisfies Record<ContextWindowSource, string>;

/** A context-window field: the text typed, and where a scanned value came from. */
interface WindowDraft {
  text: string;
  source?: ContextWindowSource;
}

/** The stored overrides as the fields show them: `1050000` reads `1.05M`. */
function contextWindowDrafts(p: LLMProvider): Record<string, WindowDraft> {
  return Object.fromEntries(
    Object.entries(p.context_windows ?? {}).map(([k, v]) => [k, { text: formatTokenCount(v) }]),
  );
}

/**
 * The draft of *model*, read as an own key only: a model id is free text, and
 * `constructor` or `toString` must not read what `Object.prototype` holds.
 */
function draftOf(drafts: Record<string, WindowDraft>, model: string): WindowDraft | undefined {
  return Object.prototype.hasOwnProperty.call(drafts, model) ? drafts[model] : undefined;
}

const isUnset = (draft: WindowDraft | undefined) =>
  typeof draft?.text !== "string" || !draft.text.trim();

function providerAccess(p: LLMProvider): LLMProviderAccess {
  return {
    access_restricted: !!p.access_restricted,
    shared_group_ids: p.shared_group_ids ?? [],
    shared_user_ids: p.shared_user_ids ?? [],
    excluded_group_ids: p.excluded_group_ids ?? [],
    excluded_user_ids: p.excluded_user_ids ?? [],
  };
}

function excludesSomeone(access: Pick<LLMProviderAccess, "excluded_group_ids" | "excluded_user_ids">): boolean {
  return access.excluded_group_ids.length + access.excluded_user_ids.length > 0;
}

function sameAccess(a: LLMProviderAccess, b: LLMProviderAccess): boolean {
  const same = (x: string[], y: string[]) =>
    x.length === y.length && [...x].sort().every((id, i) => id === [...y].sort()[i]);
  return (
    a.access_restricted === b.access_restricted
    && same(a.shared_group_ids, b.shared_group_ids)
    && same(a.shared_user_ids, b.shared_user_ids)
    && same(a.excluded_group_ids, b.excluded_group_ids)
    && same(a.excluded_user_ids, b.excluded_user_ids)
  );
}

// ---------------------------------------------------------------------------
// Provider Card — individual LLM provider with inline model management
// ---------------------------------------------------------------------------
export function ProviderCard({
  provider: p,
  allProviders,
  isAdmin,
  isEditingModels,
  onEditModels,
  onCloseEditModels,
  onDelete,
  onRenameDisplayName,
  onUpdateModels,
  onUpdateAccess,
  isUpdating,
  platformDefaultModel,
  canSetPlatformDefault,
  onSetPlatformDefault,
}: {
  provider: LLMProvider;
  /** Every config, so an unnamed one can show which of its namesakes it is. */
  allProviders?: LLMProvider[] | null;
  isAdmin: boolean;
  isEditingModels: boolean;
  onEditModels: () => void;
  onCloseEditModels: () => void;
  onDelete: () => void;
  onRenameDisplayName: (displayName: string | null) => void;
  onUpdateModels: (available: string[], contextWindows?: Record<string, number> | null) => void;
  /** Saves who may use the provider; `onSaved` runs once the server accepted it. */
  onUpdateAccess: (access: LLMProviderAccess, onSaved: () => void) => void;
  isUpdating: boolean;
  platformDefaultModel: string | null;
  /** False when the platform default is env-managed (read-only) or a default-save is in flight — suppresses the set-default affordance. */
  canSetPlatformDefault: boolean;
  onSetPlatformDefault: (provider: string, providerId: string, model: string) => void;
}) {
  const { t_i18n, intl } = useFormatter();
  // Every provider renders a card, so the ids tying labels to fields are per card.
  const fieldId = useId();
  const renameInputId = `${fieldId}-rename`;
  const modelsHeadingId = `${fieldId}-models`;
  const { data: allModelsData, isLoading: loadingModels } = useProviderModels(
    isEditingModels ? p.id : undefined,
  );
  const models = [...(p.available_models ?? [])].sort((a, b) => a.localeCompare(b));
  // What the admin has ticked, retired models included: they are filtered out
  // below, not stored apart.
  const [chosen, setChosen] = useState<string[]>(models);
  const [isRenaming, setIsRenaming] = useState(false);
  const [renameDraft, setRenameDraft] = useState(p.display_name || "");
  const [modelSearch, setModelSearch] = useState("");
  const [isEditingAccess, setIsEditingAccess] = useState(false);
  const [accessDraft, setAccessDraft] = useState<LLMProviderAccess>(() => providerAccess(p));
  // The exclusion switch is the card's own state: the API stores the lists
  // alone, and an empty list switched on is "nobody excluded yet".
  const [excluding, setExcluding] = useState(() => excludesSomeone(providerAccess(p)));
  const [windowDrafts, setWindowDrafts] = useState<Record<string, WindowDraft>>(
    () => contextWindowDrafts(p),
  );
  const { toast } = useToast();
  const scanContextWindows = useScanContextWindows(p.id);
  const refreshModels = useRefreshProviderModels(p.id);
  // The scan reads the provider: unavailable, and not run on enabling a model, with AI off.
  const aiOff = useAiActionDisabled();

  // Each opening or closing of the editor is a new generation: a scan answers
  // only the generation that asked, so a cancelled edit's lookup neither fills
  // the next edit's fields nor toasts.
  const editGeneration = useRef(0);
  // Scans in flight for this generation: Save waits for them, or a window
  // found after the click would never be saved.
  const [pendingScans, setPendingScans] = useState(0);
  useEffect(() => {
    editGeneration.current += 1;
    setPendingScans(0);
  }, [isEditingModels]);

  // A model selected earlier but absent from the provider's live list is no
  // longer offered: it leaves the selection (saved only on Save). Only the
  // gateway says what it offers across chat, embedding and image; the other
  // providers filter their list or fall back to a fixed one. The query keeps no
  // cache between openings, so a stale list is never judged; neither is an empty one.
  const offeredModels = allModelsData?.offered_models;
  const retiredModels =
    isEditingModels && p.provider === "filigran_gateway" && offeredModels?.length
      ? chosen.filter((m) => !offeredModels.includes(m))
      : [];
  const localSelected = retiredModels.length
    ? chosen.filter((m) => !retiredModels.includes(m))
    : chosen;

  const setModelContextWindow = (model: string, value: string) =>
    setWindowDrafts((prev) => ({ ...prev, [model]: { text: value } }));

  /** Fills the empty fields with what the scan found; a typed value is never replaced. */
  const fillScannedWindows = (scan: ContextWindowScan) =>
    setWindowDrafts((prev) => {
      let next = prev;
      for (const r of scan.results) {
        if (r.context_window === null || r.source === null || !isUnset(draftOf(next, r.model))) continue;
        next = { ...next, [r.model]: { text: formatTokenCount(r.context_window), source: r.source } };
      }
      return next;
    });

  /**
   * Looks *models* up for the current generation. Resolves to `null` once that
   * generation is over (the editor was closed or reopened meanwhile).
   */
  const runScan = (models: string[]): Promise<ContextWindowScan | null> => {
    const generation = editGeneration.current;
    const current = () => generation === editGeneration.current;
    setPendingScans((n) => n + 1);
    const settle = () => { if (current()) setPendingScans((n) => Math.max(0, n - 1)); };
    return scanContextWindows.mutateAsync(models).then(
      (scan) => { settle(); return current() ? scan : null; },
      (error: unknown) => { settle(); if (current()) throw error; return null; },
    );
  };

  /** A model just enabled gets its window looked up, quietly: the field stays typeable. */
  const scanNewlyEnabled = (enabled: string[]) => {
    const unset = enabled.filter((m) => isUnset(draftOf(windowDrafts, m))).slice(0, 1000);
    if (unset.length === 0 || aiOff) return;
    runScan(unset).then((scan) => { if (scan) fillScannedWindows(scan); }, () => {});
  };

  const handleScanSelected = () => {
    const selected = localSelected.slice(0, 1000);
    const unset = selected.filter((m) => isUnset(draftOf(windowDrafts, m)));
    runScan(selected).then(
      (scan) => {
        if (!scan) return;
        fillScannedWindows(scan);
        const filled = scan.results.filter((r) => r.context_window !== null && unset.includes(r.model)).length;
        const unknown = scan.results.filter((r) => r.context_window === null).length;
        const parts = [
          t_i18n("{filled, plural, one {# context window filled in} other {# context windows filled in}}.", { filled }),
          ...(selected.length > unset.length
            ? [t_i18n("{kept, plural, one {# value already set was kept} other {# values already set were kept}}.", { kept: selected.length - unset.length })]
            : []),
          ...(unknown > 0
            ? [t_i18n("{unknown, plural, one {# model is unknown: type its window} other {# models are unknown: type their window}}.", { unknown })]
            : []),
          ...(scan.live_lookup === "failed"
            ? [t_i18n("The provider's model list could not be read: the values come from the model catalog.")]
            : []),
        ];
        toast({
          title: t_i18n("Context windows scanned"),
          description: parts.join(" "),
          variant: scan.live_lookup === "failed" || unknown > 0 ? "info" : "success",
        });
      },
      () => {
        toast({
          title: t_i18n("Context windows not scanned"),
          description: t_i18n("The context windows could not be looked up. Type them instead."),
          variant: "error",
        });
      },
    );
  };

  // Its catalog is its own listing (no `*_MODELS`): read it again, as a restart would.
  const handleRefreshModels = () => {
    refreshModels.mutate(undefined, {
      onSuccess: (fresh) =>
        toast({
          title: t_i18n("Models refreshed"),
          description: t_i18n("{count, plural, one {# model} other {# models}} listed.", {
            count: fresh.available_models?.length ?? 0,
          }),
          variant: "success",
        }),
    });
  };

  // Sync local state when entering edit mode
  const handleStartEdit = () => {
    setChosen([...models]);
    setModelSearch("");
    setWindowDrafts(contextWindowDrafts(p));
    onEditModels();
  };

  const toggleModel = (m: string) => {
    if (!localSelected.includes(m)) scanNewlyEnabled([m]);
    setChosen((prev) => toggleArrayItem(prev, m));
  };

  const handleEnableAll = (all: string[]) => {
    scanNewlyEnabled(all.filter((m) => !localSelected.includes(m)));
    setChosen([...all]);
  };

  const parsedContextWindows = Object.fromEntries(
    localSelected.map((m) => [m, parseTokenCount(draftOf(windowDrafts, m)?.text ?? "")]),
  );
  const invalidContextWindows = localSelected.filter((m) => parsedContextWindows[m].kind === "invalid");

  const contextWindowTip = (m: string) => {
    const parsed = parsedContextWindows[m];
    if (!parsed || parsed.kind === "empty") {
      return t_i18n("Context window override in tokens, for example 128K or 1.05M. Leave empty for the provider default.");
    }
    if (parsed.kind === "invalid") {
      return t_i18n("Not a token count: type a whole number of tokens, or a shorthand such as 128K or 1.05M.");
    }
    const source = draftOf(windowDrafts, m)?.source;
    const tokens = t_i18n("{tokens} tokens", { tokens: intl.formatNumber(parsed.tokens) });
    return source ? `${tokens}. ${t_i18n(CONTEXT_WINDOW_SOURCE_COPY[source])}` : tokens;
  };

  const handleSave = () => {
    if (localSelected.length === 0 || invalidContextWindows.length > 0 || pendingScans > 0) return;
    const ctxWindows: Record<string, number> = Object.fromEntries(
      localSelected.flatMap((m) => {
        const parsed = parsedContextWindows[m];
        return parsed.kind === "tokens" ? [[m, parsed.tokens]] : [];
      }),
    );
    onUpdateModels(localSelected, Object.keys(ctxWindows).length > 0 ? ctxWindows : null);
  };

  const handleSaveRename = () => {
    const trimmed = renameDraft.trim();
    onRenameDisplayName(trimmed || null);
    setIsRenaming(false);
  };

  const handleToggleAccess = () => {
    if (!isEditingAccess) {
      setAccessDraft(providerAccess(p));
      setExcluding(excludesSomeone(providerAccess(p)));
    }
    setIsEditingAccess((v) => !v);
  };

  const handleToggleExcluding = (value: boolean) => {
    setExcluding(value);
    // Switched off = nobody excluded, as the saved lists will say.
    if (!value) setAccessDraft((d) => ({ ...d, excluded_group_ids: [], excluded_user_ids: [] }));
  };

  const accessChanged = !sameAccess(accessDraft, providerAccess(p));
  const restrictedTip = !isAdmin
    ? t_i18n("Reserved for specific people and groups, and you are one of them.")
    : (p.shared_group_ids?.length ?? 0) + (p.shared_user_ids?.length ?? 0) === 0
      ? t_i18n("Restricted with no one listed: no one can use this provider.")
      : t_i18n(
          "Only {groups, plural, one {# group} other {# groups}} and {people, plural, one {# person} other {# people}} can use this provider.",
          { groups: p.shared_group_ids?.length ?? 0, people: p.shared_user_ids?.length ?? 0 },
        );
  const excludedCount = (p.excluded_group_ids?.length ?? 0) + (p.excluded_user_ids?.length ?? 0);
  const excludedTip = t_i18n(
    "Never used for {groups, plural, one {# group} other {# groups}} and {people, plural, one {# person} other {# people}}, whatever it grants.",
    { groups: p.excluded_group_ids?.length ?? 0, people: p.excluded_user_ids?.length ?? 0 },
  );

  // An env-seeded provider can never be deleted from the UI (the boot seed
  // would recreate it) and its API key is env-managed. Its model catalog and
  // display name, however, are admin-curated once the operator opts in with
  // LLM_PROVIDER_MODELS_EDITABLE=true — the backend reports that as
  // `models_env_locked = false` and accepts the PUT. Gate the editors
  // on that flag, not on `is_from_env`, so the UI never hides a control the
  // API allows (or shows one the seed would silently revert).
  const canCurate = isAdmin && !p.models_env_locked;
  const canDelete = isAdmin && !p.is_from_env;
  // A provider that does not chat (a voice generator, an embedding server)
  // offers no chat model: its models are chosen in its capability's section.
  const chats = !p.capabilities?.length || p.capabilities.includes("chat");
  const servesOnly = chats ? null : p.capabilities?.[0];

  return (
    <div className="rounded-sm bg-elevation-default overflow-hidden">
      {/* Header */}
      <div className="flex items-center justify-between p-3.5">
        <div className="flex items-center gap-3">
          <div className="flex h-8 w-8 items-center justify-center rounded-sm bg-filigran-ia-primary/10">
            <Bot className="h-4 w-4 text-filigran-ia-primary" />
          </div>
          <div>
            <div className="flex items-center gap-1.5">
              <p className="content-base font-medium">{providerConfigName(p, allProviders)}</p>
              {canCurate && (
                <Button
                  variant="ghost"
                  size="sm"
                  className="gap-1 text-default-secondary hover:text-accent"
                  onClick={() => {
                    setRenameDraft(p.display_name || "");
                    setIsRenaming((v) => !v);
                  }}
                >
                  <Pencil className="h-3 w-3" />
                  {isRenaming ? t_i18n("Close") : t_i18n("Rename")}
                </Button>
              )}
            </div>
            {p.gateway_url && (
              <p className="truncate content-caption text-default-secondary" title={p.gateway_url}>
                {p.gateway_url}
              </p>
            )}
            <span className="content-caption text-default-secondary" data-testid={servesOnly ? "settings.provider-serves-only" : undefined}>
              {servesOnly === "speech"
                ? t_i18n("No chat: its models are offered in the generation sections below.")
                : servesOnly === "video"
                  ? t_i18n("Video only: its models are chosen under Video Generation below.")
                  : servesOnly === "embedding"
                    ? t_i18n("Embeddings only: its model is chosen under Embedding Provider below.")
                    : servesOnly
                      ? t_i18n("Serves {capability} only: its models are chosen in that section below.", { capability: servesOnly })
                      : t_i18n("{count, plural, one {# model available} other {# models available}}", { count: models.length })}
            </span>
            {/* Overlap is legitimate — a gateway fronting a vendor beside that
                vendor's own key — but it is what makes a bare model id
                ambiguous, so say so here rather than let it surface as a
                mis-billed call. */}
            {(p.model_overlaps?.length ?? 0) > 0 && (
              <Tip
                content={(() => {
                  // Name the ids as well as the other configs: with a large
                  // catalog, "shared with X" alone does not say which choices
                  // are ambiguous (US3 acceptance scenario).
                  const named = (o: { model: string; provider_ids: string[] }) => {
                    const others = o.provider_ids
                      .map((id) => {
                        const other = allProviders?.find((q) => q.id === id);
                        return other ? providerConfigName(other, allProviders) : id;
                      })
                      .join(", ");
                    return `${o.model} (${others})`;
                  };
                  const all = p.model_overlaps ?? [];
                  const shown = all.slice(0, 8).map(named).join("\n");
                  const rest = all.length - Math.min(all.length, 8);
                  const text = rest > 0
                    ? `${shown}\n${t_i18n("and {count} more", { count: rest })}`
                    : shown;
                  // TooltipContent sets no `whitespace-pre-*`, so the newlines
                  // would collapse and the entries run together on one line.
                  return <span className="whitespace-pre-line">{text}</span>;
                })()}
              >
                <span className="flex w-fit items-center gap-1 content-caption text-feedback-warning-primary">
                  <AlertTriangle className="h-3 w-3" />
                  {t_i18n("{count} {noun} shared with another provider", {
                    count: (p.model_overlaps ?? []).length,
                    noun: t_i18n(
                      pluralize((p.model_overlaps ?? []).length, "model"),
                    ),
                  })}
                </span>
              </Tip>
            )}
          </div>
        </div>
        <div className="flex items-center gap-1.5">
          {p.source_integration_id && (
            /* Fed from an integration (OpenRouter): its API key follows the
               integration's, and its models can be picked there too. */
            <Tip content={t_i18n("Fed by an integration: its API key follows the integration's. Open it to pick models from its catalog.")}>
              <Link to={`/integrations/${p.source_integration_id}/models`}>
                <Badge variant="cyan" className="content-caption"><Link2 className="h-3 w-3 mr-1" />{t_i18n("Integration")}</Badge>
              </Link>
            </Tip>
          )}
          {p.is_from_env && (
            <EnvLockBadge
              tooltip={
                p.models_env_locked
                  ? undefined
                  : t_i18n("API key configured via environment variable. Models can be curated here.")
              }
            />
          )}
          {p.access_restricted && (
            <Tip content={restrictedTip}>
              <Badge variant="outline" className="content-caption border-accent/30 text-accent">
                <Lock className="h-3 w-3 mr-1" />{t_i18n("Restricted")}
              </Badge>
            </Tip>
          )}
          {isAdmin && excludedCount > 0 && (
            <Tip content={excludedTip}>
              <Badge variant="outline" className="content-caption border-feedback-alert-primary/30 text-feedback-warning-primary">
                <Ban className="h-3 w-3 mr-1" />{t_i18n("Exclusions")}
              </Badge>
            </Tip>
          )}
          <Badge variant="success" className="content-caption"><Check className="h-3 w-3 mr-1" />{t_i18n("Active")}</Badge>
          {/* Who may use a provider is not part of its env-managed catalog,
              so an env-seeded provider can be restricted like any other. */}
          {isAdmin && (
            <Button
              variant="ghost"
              size="sm"
              className="gap-1 content-compact text-default-secondary hover:text-accent"
              aria-expanded={isEditingAccess}
              onClick={handleToggleAccess}
            >
              <UsersRound className="h-3 w-3" />
              {isEditingAccess ? t_i18n("Close") : t_i18n("Access")}
            </Button>
          )}
          {isAdmin && p.models_listed && (
            <AiDisabledTip>
              <Tip content={aiOff ? undefined : t_i18n("Read the provider's model list again, as a restart would.")}>
                <Button
                  variant="ghost"
                  size="sm"
                  className="gap-1 content-compact text-default-secondary hover:text-accent"
                  disabled={aiOff || refreshModels.isPending}
                  aria-busy={refreshModels.isPending || undefined}
                  onClick={handleRefreshModels}
                >
                  {refreshModels.isPending && <Spinner size="sm" tone="inherit" />}
                  {!refreshModels.isPending && <RefreshCw className="h-3 w-3" />}
                  {t_i18n("Refresh models")}
                </Button>
              </Tip>
            </AiDisabledTip>
          )}
          {canCurate && chats && (
            <Button
              variant="ghost"
              size="sm"
              className="gap-1 content-compact text-default-secondary hover:text-accent"
              onClick={isEditingModels ? onCloseEditModels : handleStartEdit}
            >
              <Pencil className="h-3 w-3" />
              {isEditingModels ? t_i18n("Close") : t_i18n("Models")}
            </Button>
          )}
          {canDelete && (
            <Tip content={t_i18n("Delete provider")}>
              <Button
                variant="ghost"
                size="icon"
                className="h-8 w-8 text-default-secondary hover:text-feedback-error-primary"
                aria-label={t_i18n("Delete provider")}
                onClick={onDelete}
              >
                <Trash2 className="h-3.5 w-3.5" />
              </Button>
            </Tip>
          )}
        </div>
      </div>

      {p.vault_secrets && (
        <VaultSecretsNotice status={p.vault_secrets} className="border-t border-elevation-subtle-soft px-3.5 py-2.5" />
      )}

      {isRenaming && canCurate && (
        <div className="border-t border-accent/20 bg-accent/[0.02] px-4 py-3.5 space-y-3">
          <div className="space-y-1.5">
            <label htmlFor={renameInputId} className="content-base font-medium text-default-primary">{t_i18n("Provider Display Name")}</label>
            <Input aria-label={t_i18n("Provider Display Name")}
              id={renameInputId}
              aria-describedby={`${renameInputId}-hint`}
              value={renameDraft}
              onChange={(e) => setRenameDraft(e.target.value)}
              placeholder={PROVIDER_DISPLAY_NAMES[p.provider] || p.provider}
            />
            <p id={`${renameInputId}-hint`} className="content-compact text-default-secondary">
              {t_i18n("Leave empty to fallback to the default provider label.")}
            </p>
          </div>
          <div className="flex justify-end gap-3">
            <Button variant="secondary" onClick={() => setIsRenaming(false)} disabled={isUpdating}>
              {t_i18n("Cancel")}
            </Button>
            <Button className="gap-1.5" onClick={handleSaveRename} disabled={isUpdating} aria-busy={isUpdating || undefined}>
              {isUpdating && <Spinner size="sm" tone="inherit" />}{!isUpdating && <Check className="h-4 w-4" />}
              {t_i18n("Save Name")}
            </Button>
          </div>
        </div>
      )}

      {isEditingAccess && isAdmin && (
        <div className="border-t border-accent/20 bg-accent/[0.02] px-4 py-3.5 space-y-3">
          <PrincipalSharingCard
            isCompanyManaged
            withPermissions={false}
            restrictionCopy={PROVIDER_RESTRICTION_COPY}
            sharedGroupIds={accessDraft.shared_group_ids}
            onChange={(ids) => setAccessDraft((d) => ({ ...d, shared_group_ids: ids ?? [] }))}
            userSharing={{
              userIds: accessDraft.shared_user_ids,
              onUserIdsChange: (ids) => setAccessDraft((d) => ({ ...d, shared_user_ids: ids ?? [] })),
              userPermissions: null,
              onUserPermissionsChange: () => {},
              accessRestricted: accessDraft.access_restricted,
              onAccessRestrictedChange: (value) =>
                setAccessDraft((d) => ({ ...d, access_restricted: value })),
            }}
            disabled={isUpdating}
          />
          {accessDraft.access_restricted && (
            <p className="content-caption text-default-secondary">
              {t_i18n("For someone not listed here, an agent pinned to this provider runs the same model on another provider they may use, or falls back to their next model choice.")}
            </p>
          )}
          <PrincipalSharingCard
            isCompanyManaged
            withPermissions={false}
            restrictionCopy={PROVIDER_EXCLUSION_COPY}
            sharedGroupIds={accessDraft.excluded_group_ids}
            onChange={(ids) => setAccessDraft((d) => ({ ...d, excluded_group_ids: ids ?? [] }))}
            userSharing={{
              userIds: accessDraft.excluded_user_ids,
              onUserIdsChange: (ids) => setAccessDraft((d) => ({ ...d, excluded_user_ids: ids ?? [] })),
              userPermissions: null,
              onUserPermissionsChange: () => {},
              accessRestricted: excluding,
              onAccessRestrictedChange: handleToggleExcluding,
            }}
            disabled={isUpdating}
          />
          <div className="flex justify-end gap-3">
            <Button variant="secondary" onClick={() => setIsEditingAccess(false)} disabled={isUpdating}>
              {t_i18n("Cancel")}
            </Button>
            <Button
              className="gap-1.5"
              onClick={() => onUpdateAccess(accessDraft, () => setIsEditingAccess(false))}
              disabled={!accessChanged || isUpdating}
              aria-busy={isUpdating || undefined}
            >
              {isUpdating && <Spinner size="sm" tone="inherit" />}{!isUpdating && <Check className="h-4 w-4" />}
              {t_i18n("Save Access")}
            </Button>
          </div>
        </div>
      )}

      {/* Model tiles (always visible) */}
      {chats && models.length > 0 && !isEditingModels && (
        <div className="border-t border-elevation-subtle-soft px-3.5 py-2.5">
          <div className="flex flex-wrap gap-1.5">
            {models.map((m) => {
              const isDefault = m === platformDefaultModel;
              const clickable = isAdmin && canSetPlatformDefault && !isDefault;
              return (
                <Tip key={m} content={clickable ? t_i18n("Click to set as platform default") : undefined}>
                  <button
                    className={`inline-flex items-center gap-1.5 rounded-sm px-2.5 py-1 content-caption font-medium transition-colors ${
                      isDefault
                        ? "bg-accent/10 text-accent border border-accent/30"
                        : `bg-elevation-hover text-default-secondary border border-elevation-subtle-soft${clickable ? " hover:border-accent/30 hover:text-accent/80" : ""}`
                    }`}
                    onClick={() => { if (clickable) onSetPlatformDefault(p.provider, p.id, m); }}
                    disabled={!clickable}
                  >
                    <Sparkles className="h-3 w-3 shrink-0 opacity-50" />
                    {m}
                    {isDefault && (
                      <span className="content-caption uppercase tracking-wider opacity-60 ml-0.5">{t_i18n("default")}</span>
                    )}
                  </button>
                </Tip>
              );
            })}
          </div>
        </div>
      )}

      {/* Edit models panel */}
      {isEditingModels && chats && (
        <div className="border-t border-accent/20 bg-accent/[0.02] px-4 py-3.5 space-y-3">
          <div className="flex items-center justify-between">
            <p id={modelsHeadingId} className="content-compact font-medium text-default-secondary">{t_i18n("Manage Available Models")}</p>
            <div className="flex items-center gap-2">
              {localSelected.length > 0 && (
                <>
                  <AiDisabledTip>
                    <Tip content={aiOff ? undefined : t_i18n("Look up the context window of the selected models: in the provider's model list, then in the model catalog. Values already set are kept.")}>
                      <button
                        className="inline-flex items-center gap-1 content-caption text-default-secondary hover:text-accent transition-colors disabled:opacity-50"
                        onClick={handleScanSelected}
                        disabled={pendingScans > 0 || aiOff}
                      >
                        {pendingScans > 0 && <Spinner size="sm" tone="inherit" />}{!(pendingScans > 0) && <ScanSearch className="h-3 w-3" />}
                        {t_i18n("Scan context windows")}
                      </button>
                    </Tip>
                  </AiDisabledTip>
                  {allModelsData?.models && <span className="text-default-disabled content-caption">|</span>}
                </>
              )}
              {allModelsData?.models && (
                <>
                  <button
                    className="content-caption text-default-secondary hover:text-accent transition-colors"
                    onClick={() => handleEnableAll(allModelsData.models ?? [])}
                  >
                    {t_i18n("Enable All")}
                  </button>
                  <span className="text-default-disabled content-caption">|</span>
                  <button
                    className="content-caption text-default-secondary hover:text-accent transition-colors"
                    onClick={() => setChosen([])}
                  >
                    {t_i18n("Disable All")}
                  </button>
                </>
              )}
              {loadingModels && (
                <div className="flex items-center gap-1.5 content-caption text-default-secondary">
                  <Spinner size="sm" tone="inherit" /> {t_i18n("Loading all models...")}
                </div>
              )}
            </div>
          </div>

          {retiredModels.length > 0 && (
            <p role="status" className="flex items-start gap-1.5 content-caption text-feedback-warning-primary">
              <AlertTriangle className="mt-0.5 h-3 w-3 shrink-0" />
              {t_i18n(
                localSelected.length === 0
                  ? "{count, plural, one {# selected model is} other {# selected models are}} no longer offered by the provider and left the selection: {models}. Pick a replacement to enable Save."
                  : "{count, plural, one {# selected model is} other {# selected models are}} no longer offered by the provider and left the selection: {models}. Save to apply.",
                { count: retiredModels.length, models: retiredModels.join(", ") },
              )}
            </p>
          )}

          {allModelsData?.models && (
            <>
            <div className="relative">
              <Search className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-default-secondary" />
              <Input aria-label={t_i18n("Search models...")}
                className="pl-9"
                placeholder={t_i18n("Search models...")}
                value={modelSearch}
                onChange={(e) => setModelSearch(e.target.value)}
              />
            </div>
            <div
              role="group"
              aria-labelledby={modelsHeadingId}
              className="max-h-56 overflow-y-auto rounded-sm bg-elevation-default divide-y divide-[var(--border-elevation-subtle)]"
            >
              {[...allModelsData.models].sort((a, b) => a.localeCompare(b)).filter((m) => !modelSearch || m.toLowerCase().includes(modelSearch.toLowerCase())).map((m) => {
                const isSelected = localSelected.includes(m);
                const isDefault = m === platformDefaultModel;
                return (
                  <div key={m} className={`flex items-center gap-3 px-3 py-2 transition-colors ${isSelected ? "bg-elevation-hover" : ""}`}>
                    <Switch
                      aria-label={m}
                      checked={isSelected}
                      onCheckedChange={() => toggleModel(m)}
                    />
                    <span className={`flex-1 content-compact ${isSelected ? "text-default-primary" : "text-default-secondary"}`}>{m}</span>
                    {isSelected && (
                      <Tip content={contextWindowTip(m)}>
                        <Input
                          type="text"
                          inputMode="decimal"
                          autoComplete="off"
                          spellCheck={false}
                          aria-label={t_i18n("Context window for {model}", { model: m })}
                          aria-invalid={parsedContextWindows[m]?.kind === "invalid"}
                          className={cn("w-24 shrink-0", draftOf(windowDrafts, m)?.source && "[&_input]:text-accent")}
                          placeholder={t_i18n("e.g. 128K")}
                          value={draftOf(windowDrafts, m)?.text ?? ""}
                          onChange={(e) => setModelContextWindow(m, e.target.value)}
                          onClick={(e) => e.stopPropagation()}
                        />
                      </Tip>
                    )}
                    {isSelected && (isDefault || canSetPlatformDefault) && (
                      <button
                        className={`content-caption px-2 py-0.5 rounded-full transition-colors shrink-0 ${
                          isDefault
                            ? "bg-accent/15 text-accent border border-accent/30 font-medium"
                            : "text-default-secondary hover:text-accent hover:bg-elevation-hover border border-transparent"
                        }`}
                        onClick={() => { if (!isDefault && canSetPlatformDefault) onSetPlatformDefault(p.provider, p.id, m); }}
                        disabled={isDefault || !canSetPlatformDefault}
                      >
                        {isDefault ? t_i18n("Default") : t_i18n("Set default")}
                      </button>
                    )}
                  </div>
                );
              })}
            </div>
            </>
          )}

          {localSelected.length > 0 && (
            <p className="content-caption text-default-secondary">
              {t_i18n("{count, plural, one {# model selected} other {# models selected}}", { count: localSelected.length })}
            </p>
          )}

          {invalidContextWindows.length > 0 && (
            <p role="alert" className="flex items-center gap-1.5 content-caption text-feedback-error-primary">
              <AlertTriangle className="h-3 w-3 shrink-0" />
              {t_i18n("Fix the context windows marked in red: a whole number of tokens, or a shorthand such as 128K or 1.05M.")}
            </p>
          )}

          {pendingScans > 0 && (
            <p role="status" className="flex items-center gap-1.5 content-caption text-default-secondary">
              <Spinner size="sm" tone="inherit" className="shrink-0" />
              {t_i18n("Looking up context windows: the models can be saved once it is done.")}
            </p>
          )}

          <div className="flex justify-end gap-3">
            <Button variant="secondary" onClick={onCloseEditModels} disabled={isUpdating}>
              {t_i18n("Cancel")}
            </Button>
            <Button
              className="gap-1.5"
              onClick={handleSave}
              disabled={localSelected.length === 0 || invalidContextWindows.length > 0 || pendingScans > 0 || isUpdating}
              aria-busy={isUpdating || undefined}
            >
              {isUpdating && <Spinner size="sm" tone="inherit" />}{!isUpdating && <Check className="h-4 w-4" />}
              {t_i18n("Save Models")}
            </Button>
          </div>
        </div>
      )}
    </div>
  );
}
