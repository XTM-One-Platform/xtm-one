// SPDX-License-Identifier: Apache-2.0

import { useState } from "react";
import { AlertTriangle, Info, RefreshCw, Trash2, XCircle } from "lucide-react";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { AiDisabledTip } from "@/components/ui/ai-disabled";
import { Button } from "@/components/ui/button";
import { SearchableSelect } from "@/components/ui/searchable-select";
import { useToast } from "@/components/ui/toast";
import { useDeleteLLMProvider, useLLMProviderDeletionImpact } from "@/hooks/api/useSettingsApi";
import { useAiActionDisabled } from "@/hooks/useAiDisabled";
import { useAgenticAIEnabled } from "@/hooks/usePlatformConfig";
import { useFormatter } from "@/i18n";
import { cn, effectiveProviderName, providerConfigName } from "@/lib/utils";
import type { LLMProvider, ProviderRouteTarget } from "@/types";
import {
  deletionNeedsReplacement,
  describeProviderDeletion,
  type DeletionLine,
  type DeletionLineTone,
} from "./provider-deletion";
import { Spinner } from "@filigran/design-system";

const TONE_ICON: Record<DeletionLineTone, React.ElementType> = {
  info: Info,
  warning: AlertTriangle,
  danger: XCircle,
};

const TONE_CLASS: Record<DeletionLineTone, string> = {
  info: "text-accent",
  warning: "text-feedback-warning-primary",
  danger: "text-feedback-error-primary",
};

// ---------------------------------------------------------------------------
// DeleteProviderDialog — deleting a provider says what replaces it
// ---------------------------------------------------------------------------
export function DeleteProviderDialog({
  provider,
  providers,
  onDeleted,
  onClose,
}: {
  provider: LLMProvider;
  /** Every config, so names match the providers list (ordinals included). */
  providers: LLMProvider[];
  onDeleted?: () => void;
  onClose: () => void;
}) {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  // The Settings tab is a disabled fieldset with AI off, but this dialog is
  // portalled outside it and can outlive a toggle made meanwhile.
  const aiOff = useAiActionDisabled();
  const licensed = useAgenticAIEnabled();
  // `null` until the admin picks one: the backend then applies its own rule.
  const [replacementId, setReplacementId] = useState<string | null>(null);
  const impactQuery = useLLMProviderDeletionImpact(provider.id, replacementId);
  const deleteProvider = useDeleteLLMProvider();
  const impact = impactQuery.data?.provider_id === provider.id ? impactQuery.data : undefined;
  // The delete refused because another change was saved since the preview it
  // was confirmed with; the preview is read again (below) before a new confirm.
  const impactOvertaken = (deleteProvider.error as { status?: number } | null)?.status === 409;
  const providerName = providerConfigName(provider, providers);

  const nameOf = (target: ProviderRouteTarget) => {
    const config = target.provider_id ? providers.find((p) => p.id === target.provider_id) : undefined;
    if (config) return providerConfigName(config, providers);
    return effectiveProviderName({ provider: target.provider ?? "", display_name: target.display_name });
  };

  const lines: DeletionLine[] = impact ? describeProviderDeletion(impact, nameOf, { licensed }) : [];
  const offerReplacement = impact ? deletionNeedsReplacement(impact) : false;
  const busy = deleteProvider.isPending;
  // Never on placeholder data: after a switch it still describes the previous
  // pick, even while a paused (offline) fetch reports nothing in flight.
  const canConfirm =
    !!impact && !impactQuery.isFetching && !impactQuery.isPlaceholderData && !impactQuery.isError && !busy;
  const candidates = (impact?.candidates ?? []).filter(
    (candidate): candidate is ProviderRouteTarget & { provider_id: string } => !!candidate.provider_id,
  );

  const confirm = () => {
    if (!impact) return;
    deleteProvider.mutate(
      // The replacement the admin was offered and the fingerprint of what the
      // dialog showed: the backend applies exactly that or answers 409. With
      // no choice offered the rule picks, and the audit says so.
      {
        id: provider.id,
        replacementProviderId: offerReplacement ? (impact.replacement?.provider_id ?? null) : null,
        expectedImpact: impact.impact_fingerprint,
      },
      {
        onSuccess: () => {
          toast({ title: t_i18n("Provider deleted"), variant: "success" });
          onDeleted?.();
          onClose();
        },
        onError: () => {
          void impactQuery.refetch();
        },
      },
    );
  };

  return (
    <Dialog open onOpenChange={(open) => { if (!open && !busy) onClose(); }}>
      <DialogContent className="max-w-lg">
        <DialogHeader>
          <div className="flex items-start gap-3">
            <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-full bg-feedback-error-primary/10">
              <Trash2 className="h-5 w-5 text-feedback-error-primary" />
            </div>
            <div className="space-y-1">
              <DialogTitle className="text-base">
                {t_i18n("Delete {provider}?", { provider: providerName })}
              </DialogTitle>
              <DialogDescription className="content-base leading-relaxed">
                {t_i18n("Review what happens to everything that uses this provider before you confirm.")}
              </DialogDescription>
            </div>
          </div>
        </DialogHeader>

        <div className="space-y-4 pt-1">
          {!impact && impactQuery.isPending && (
            <div className="flex items-center gap-2 content-base text-default-secondary">
              <Spinner size="sm" tone="inherit" />
              {t_i18n("Checking what uses this provider...")}
            </div>
          )}

          {impactQuery.isError && (
            <div role="alert" className="space-y-2 rounded-sm border border-feedback-error-primary/20 bg-feedback-error-primary/5 p-3">
              <p className="content-base text-feedback-error-primary">
                {t_i18n("Could not check what uses this provider: {error}", {
                  error: impactQuery.error?.message ?? "",
                })}
              </p>
              <Button
                variant="ghost"
                size="sm"
                className="h-7 gap-1.5 content-compact"
                onClick={() => {
                  // A replacement that became unusable (deleted meanwhile) keeps
                  // failing with a 422: fall back to the proposed one. Any other
                  // failure is retried with the admin's pick kept.
                  const status = (impactQuery.error as { status?: number } | null)?.status;
                  if (replacementId && status === 422) setReplacementId(null);
                  else void impactQuery.refetch();
                }}
              >
                <RefreshCw className="h-3 w-3" /> {t_i18n("Try again")}
              </Button>
            </div>
          )}

          {impact && offerReplacement && (
            <div className="space-y-1.5">
              <div className="flex items-center gap-2">
                <label htmlFor="provider-replacement" className="content-compact font-medium text-default-secondary">
                  {t_i18n("Replacement provider")}
                </label>
                {impactQuery.isFetching && <Spinner size="sm" tone="inherit" className="text-default-secondary" />}
              </div>
              <SearchableSelect
                id="provider-replacement"
                aria-label={t_i18n("Replacement provider")}
                options={candidates.map((candidate) => ({
                  value: candidate.provider_id,
                  label: `${nameOf(candidate)} (${candidate.model})`,
                }))}
                value={replacementId ?? impact.replacement?.provider_id ?? undefined}
                onValueChange={(value) => {
                  // An error from the previous pick no longer describes this one.
                  deleteProvider.reset();
                  setReplacementId(value);
                }}
                disabled={busy}
              />
              <p className="text-[11px] text-default-secondary">
                {t_i18n("The platform default and General Assistant chat models move to this provider; the image and embedding defaults follow their own rules, listed below. The oldest remaining provider is proposed; pick another if you prefer.")}
              </p>
            </div>
          )}

          {impact && (
            <ul className="space-y-2.5">
              {lines.map((line) => {
                const Icon = TONE_ICON[line.tone];
                return (
                  <li key={line.key} className="flex items-start gap-2.5">
                    <Icon className={cn("mt-0.5 h-4 w-4 shrink-0", TONE_CLASS[line.tone])} />
                    <div className="space-y-0.5">
                      <p className="content-base leading-relaxed">{t_i18n(line.message, line.values)}</p>
                      {line.names && line.names.length > 0 && (
                        <p className="content-compact text-default-secondary">
                          {line.names.join(", ")}
                          {line.more ? ` ${t_i18n("and {count} more", { count: line.more })}` : ""}
                        </p>
                      )}
                    </div>
                  </li>
                );
              })}
            </ul>
          )}

          {deleteProvider.isError && (
            <p role="alert" className="content-base text-feedback-error-primary">
              {impactOvertaken
                ? t_i18n(
                    "The provider was not deleted: another change was saved in the meantime, so what this deletion does has been read again. Review it, then confirm.",
                  )
                : t_i18n("The provider was not deleted: {error}", { error: deleteProvider.error?.message ?? "" })}
            </p>
          )}
        </div>

        <div className="flex justify-end gap-2 pt-2">
          <Button variant="ghost" size="sm" onClick={onClose} disabled={busy}>
            {t_i18n("Cancel")}
          </Button>
          <AiDisabledTip>
            <Button
              size="sm"
              className="gap-1.5 border-0 bg-feedback-error-primary text-negative-primary hover:bg-feedback-error-primary/90"
              onClick={confirm}
              disabled={!canConfirm || aiOff} aria-busy={busy || undefined}>
              {busy && <Spinner size="sm" tone="inherit" />}{!busy && <Trash2 className="h-3 w-3" />}
              {t_i18n("Delete provider")}
            </Button>
          </AiDisabledTip>
        </div>
      </DialogContent>
    </Dialog>
  );
}
