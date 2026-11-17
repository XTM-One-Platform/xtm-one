// SPDX-License-Identifier: Apache-2.0

import { useId, useState } from "react";
import { IconButton } from "@filigran/design-system";
import { Plus, Trash2, Globe, Shield, History } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { isUnlimitedOverride, overrideHasLimit, setUnlimitedOverride } from "@/components/ui/quota-override";
import { useFormatter } from "@/i18n";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { SearchableSelect } from "@/components/ui/searchable-select";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { formatCompactNumber, QUOTA_NOT_APPLICABLE_LABEL } from "@/lib/utils";
import type { QuotaTypeConfig, QuotaLegacyNotices, AvailableModel } from "@/types";

interface QuotaTypeEditorProps {
  title: string;
  description: string;
  value: QuotaTypeConfig | null | undefined;
  onChange: (v: QuotaTypeConfig | null) => void;
  availableModels?: AvailableModel[];
  showPerModel?: boolean;
  disabled?: boolean;
  /** License ceiling per period: `null` is uncapped. */
  maxLimits?: Record<string, number | null>;
  limitLabel?: string;
  limitHint?: string;
  clearLabel?: string;
  showPlatformLimit?: boolean;
  /** When true, only the platform-wide limit input is disabled (per-user default remains editable). */
  platformLimitDisabled?: boolean;
  /** Custom hint text below the platform-wide limit input. */
  platformLimitHint?: string;
  platformCap?: number | null;
  platformCapPeriod?: string;
  /** When true, the deployment is CE-only (no Filigran license, no EE platform).
   *  Swap "Unlimited" / "No platform limit" placeholders for "Not Applicable"
   *  so empty inputs don't suggest the cap is just unset. */
  ceOnly?: boolean;
  /** User / group override editors: label of the explicit Unlimited switch.
   *  When set, an empty limit field INHERITS and the switch is the only
   *  way to say "no limit". Unset for the platform default, the last tier. */
  unlimitedLabel?: string;
  unlimitedHint?: string;
  /** Placeholder of an empty limit field in an override editor. */
  inheritPlaceholder?: string;
  /** Code from the backend when this value was saved before the current
   *  quota rules and its next save would change what it does. */
  legacyNotice?: string | null;
}

function QuotaTypeEditor({
  title,
  description,
  value,
  onChange,
  availableModels,
  showPerModel = false,
  disabled = false,
  maxLimits,
  limitLabel = "Default limit per user",
  limitHint,
  clearLabel = "Clear quota (inherit from parent)",
  showPlatformLimit = false,
  platformLimitDisabled = false,
  platformLimitHint,
  platformCap,
  platformCapPeriod,
  ceOnly = false,
  unlimitedLabel,
  unlimitedHint,
  inheritPlaceholder,
  legacyNotice,
}: QuotaTypeEditorProps) {
  const { t_i18n } = useFormatter();
  const legacyNoticeText = legacyNotice === "legacy_zero_under_pool"
    ? t_i18n("Saved before the quota rules update: today the shared license pool applies instead of this 0. Saving applies the current rule, where 0 blocks every agentic action.")
    : legacyNotice === "legacy_group_no_limit"
      ? t_i18n("Saved before the quota rules update: today this group sets no overall limit of its own, so a member's other group with a number takes precedence. Saving applies the current rule, where Unlimited outranks every group limit.")
      : null;
  const unlimitedSwitchId = useId();
  const [newModelName, setNewModelName] = useState("");

  const isOverride = unlimitedLabel != null;
  const unlimited = isOverride && isUnlimitedOverride(value);
  const platformLimit = value?.platform_limit ?? null;
  const defaultLimit = value?.default ?? null;
  const period = value?.period ?? "monthly";
  const perModel = value?.per_model ?? {};
  const maxLimit = maxLimits?.[period] || 0;

  const setField = (patch: Partial<QuotaTypeConfig>) => {
    onChange({
      platform_limit: showPlatformLimit ? platformLimit : undefined,
      default: defaultLimit,
      period,
      per_model: showPerModel ? perModel : undefined,
      ...value,
      ...patch,
    });
  };

  const addModelOverride = (model: string) => {
    if (!model || model in perModel) return;
    setField({ per_model: { ...perModel, [model]: 100 } });
    setNewModelName("");
  };

  const removeModelOverride = (model: string) => {
    const next = { ...perModel };
    delete next[model];
    setField({ per_model: Object.keys(next).length ? next : null });
  };

  const setModelLimit = (model: string, limit: number) => {
    setField({ per_model: { ...perModel, [model]: limit } });
  };

  const hasQuota = overrideHasLimit(value) || unlimited;

  // An empty field inherits in an override editor, unless per-model limits
  // keep the override alive: its default is then no overall limit.
  const emptyLimitPlaceholder = ceOnly
    ? QUOTA_NOT_APPLICABLE_LABEL
    : !isOverride
      ? "Unlimited"
      : unlimited
        ? unlimitedLabel
        : Object.keys(perModel).length > 0
          ? t_i18n("No overall limit")
          : (inheritPlaceholder ?? t_i18n("Inherit"));

  const PERIOD_MAP: Record<string, string> = { daily: "day", monthly: "month", yearly: "year" };

  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="text-base">{title}</CardTitle>
        <p className="content-base text-default-secondary">{description}</p>
      </CardHeader>
      <CardContent className="space-y-4">
        {legacyNoticeText && (
          <div className="flex items-start gap-2 rounded-sm bg-feedback-alert-primary/[0.06] border border-feedback-alert-primary/20 p-3">
            <History className="h-4 w-4 text-feedback-warning-primary shrink-0 mt-0.5" />
            <p className="content-compact text-default-secondary">{legacyNoticeText}</p>
          </div>
        )}

        {platformCap != null && platformCap > 0 && (
          <div className="flex items-center gap-2 rounded-sm bg-accent/5 border border-accent/10 p-3">
            <Shield className="h-4 w-4 text-accent shrink-0" />
            <p className="content-compact text-default-secondary">
              Platform-wide cap: <span className="font-semibold text-accent">{formatCompactNumber(platformCap)}</span> / {platformCapPeriod ?? "month"}.
              This hard limit applies across all users and cannot be overridden.
            </p>
          </div>
        )}

        {showPlatformLimit && (
          <div className="space-y-2">
            <Label className="flex items-center gap-1.5">
              <Globe className="h-3.5 w-3.5 text-accent" />
              Platform-wide limit (all users combined)
            </Label>
            <Input aria-label="Platform-wide limit (all users combined)"
              type="number"
              min={1}
              placeholder={ceOnly ? QUOTA_NOT_APPLICABLE_LABEL : "No platform limit"}
              value={platformLimit ?? ""}
              onChange={(e) => {
                const v = e.target.value ? parseInt(e.target.value, 10) : null;
                // "No platform limit" has one spelling, null: a ceiling of 0
                // was always read as uncapped, so it is never stored.
                setField({ platform_limit: v != null && v > 0 ? v : null });
              }}
              disabled={disabled || platformLimitDisabled}
            />
            <p className="content-compact text-default-secondary">
              {platformLimitHint ?? "Hard cap on total usage across ALL users combined. When set, this limit cannot be overridden by group or user quotas."}
            </p>
          </div>
        )}

        {isOverride && (
          <div className="flex items-start justify-between gap-4">
            <div className="space-y-0.5">
              <Label htmlFor={unlimitedSwitchId}>{unlimitedLabel}</Label>
              {unlimitedHint && <p className="content-compact text-default-secondary">{unlimitedHint}</p>}
            </div>
            <Switch
              id={unlimitedSwitchId}
              checked={unlimited}
              onCheckedChange={(on) => onChange(setUnlimitedOverride(value, on))}
              disabled={disabled}
            />
          </div>
        )}

        <div className="grid grid-cols-2 gap-4">
          <div className="space-y-2">
            <Label>{limitLabel}</Label>
            <Input aria-label={limitLabel}
              type="number"
              min={0}
              max={maxLimit && maxLimit > 0 ? maxLimit : undefined}
              placeholder={emptyLimitPlaceholder}
              value={unlimited ? "" : (defaultLimit ?? "")}
              onChange={(e) => {
                let v = e.target.value ? parseInt(e.target.value, 10) : null;
                if (v != null && maxLimit && maxLimit > 0 && v > maxLimit) v = maxLimit;
                setField({ default: v });
              }}
              disabled={disabled || unlimited}
            />
            <p className="content-compact text-default-secondary">
              {maxLimit && maxLimit > 0
                ? `Cannot exceed ${maxLimit} (license limit)`
                : limitHint ?? (showPerModel ? "Applies to each user individually, across all models combined" : "Max executions per user per period")}
            </p>
          </div>
          <div className="space-y-2">
            <Label>Reset period</Label>
            <Select value={period} onValueChange={(v) => setField({ period: v as "monthly" | "daily" | "yearly" })} disabled={disabled}>
              <SelectTrigger className="w-full">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="daily">Daily</SelectItem>
                <SelectItem value="monthly">Monthly</SelectItem>
                <SelectItem value="yearly">Yearly</SelectItem>
              </SelectContent>
            </Select>
          </div>
        </div>

        {showPlatformLimit && platformLimit != null && platformLimit > 0 && defaultLimit != null && defaultLimit > 0 && (
          <p className="content-compact text-default-secondary">
            With this configuration, each user can use up to {formatCompactNumber(defaultLimit)} / {PERIOD_MAP[period] ?? period}, but the entire platform is hard-capped at {formatCompactNumber(platformLimit)} / {PERIOD_MAP[period] ?? period} total.
          </p>
        )}

        {showPerModel && (
          <div className="space-y-3">
            <Label>Per-model overrides</Label>
            {Object.entries(perModel).map(([model, limit]) => (
              <div key={model} className="flex items-center gap-2">
                <code className="flex-1 truncate rounded-sm bg-elevation-highlight px-2 py-1 content-base">{model}</code>
                <Input aria-label={model}
                  type="number"
                  min={0}
                  className="w-28"
                  value={limit}
                  onChange={(e) => setModelLimit(model, parseInt(e.target.value, 10) || 0)}
                  disabled={disabled}
                />
                <IconButton variant="default" priority="tertiary" aria-label="Remove model override" icon={<Trash2 className="h-4 w-4 text-feedback-error-primary" />} onClick={() => removeModelOverride(model)} disabled={disabled} />
              </div>
            ))}
            <div className="flex items-center gap-2">
              {availableModels && availableModels.length > 0 ? (
                // Keyed on the overrides: each addition starts an empty field again.
                <SearchableSelect
                  key={Object.keys(perModel).length}
                  aria-label="Add model override"
                  className="flex-1"
                  options={availableModels
                    .filter((m) => !(m.model in perModel))
                    .map((m) => ({ value: m.model, label: m.model, keywords: [m.provider] }))}
                  value={newModelName}
                  onValueChange={(v) => addModelOverride(v)}
                  disabled={disabled || unlimited}
                  placeholder="Add model override..."
                />
              ) : (
                <>
                  <Input aria-label="Model name..."
                    placeholder="Model name..."
                    value={newModelName}
                    onChange={(e) => setNewModelName(e.target.value)}
                    className="flex-1"
                    disabled={disabled || unlimited}
                  />
                  <IconButton
                    variant="default"
                    priority="secondary"
                    aria-label="Add model override"
                    icon={<Plus className="h-4 w-4" />}
                    onClick={() => addModelOverride(newModelName)}
                    disabled={disabled || unlimited || !newModelName}
                  />
                </>
              )}
            </div>
          </div>
        )}

        {hasQuota && (
          <Button
            variant="ghost"
            size="sm"
            className="text-feedback-error-primary"
            onClick={() => onChange(null)}
            disabled={disabled}
          >
            {clearLabel}
          </Button>
        )}
      </CardContent>
    </Card>
  );
}

interface QuotaEditorProps {
  llmProxy: QuotaTypeConfig | null | undefined;
  agentic: QuotaTypeConfig | null | undefined;
  onChangeLLMProxy: (v: QuotaTypeConfig | null) => void;
  onChangeAgentic: (v: QuotaTypeConfig | null) => void;
  availableModels?: AvailableModel[];
  disabled?: boolean;
  hideLLMProxy?: boolean;
  agenticReadOnly?: boolean;
  agenticBanner?: string;
  agenticMaxLimits?: Record<string, number | null>;
  showPlatformLimit?: boolean;
  /** Disable only the platform-wide limit input on the agentic card (per-user default stays editable). */
  agenticPlatformLimitDisabled?: boolean;
  agenticPlatformLimitHint?: string;
  platformLLMProxyCap?: number | null;
  platformLLMProxyCapPeriod?: string;
  platformAgenticCap?: number | null;
  platformAgenticCapPeriod?: string;
  /** Override card titles and descriptions per quota type. */
  llmProxyTitle?: string;
  llmProxyDescription?: string;
  agenticTitle?: string;
  agenticDescription?: string;
  /** Override the limit input label and hint for both quota type cards. */
  limitLabel?: string;
  limitHint?: string;
  /** Override the clear-quota button label. */
  clearLabel?: string;
  /** When true, the deployment runs Community Edition — placeholders read
   *  "Not Applicable" instead of "Unlimited" / "No platform limit". Combine
   *  with ``disabled`` to fully lock the editor while AI is not licensed
   *  (see ``useIsCommunityEdition``). */
  ceOnly?: boolean;
  /** User / group override editors: show the explicit Unlimited switch, and
   *  let an empty limit field inherit. */
  unlimitedLabel?: string;
  unlimitedHint?: string;
  inheritPlaceholder?: string;
  /** Legacy-value notices from the backend, one per quota type. */
  notices?: QuotaLegacyNotices;
}

export function QuotaEditor({
  llmProxy,
  agentic,
  onChangeLLMProxy,
  onChangeAgentic,
  availableModels,
  disabled = false,
  hideLLMProxy = false,
  agenticReadOnly = false,
  agenticBanner,
  agenticMaxLimits,
  showPlatformLimit = false,
  agenticPlatformLimitDisabled = false,
  agenticPlatformLimitHint,
  platformLLMProxyCap,
  platformLLMProxyCapPeriod,
  platformAgenticCap,
  platformAgenticCapPeriod,
  llmProxyTitle,
  llmProxyDescription,
  agenticTitle,
  agenticDescription,
  limitLabel,
  limitHint,
  clearLabel,
  ceOnly = false,
  unlimitedLabel,
  unlimitedHint,
  inheritPlaceholder,
  notices,
}: QuotaEditorProps) {
  return (
    <div className="space-y-4">
      {agenticBanner && (
        <div className="rounded-sm bg-accent/5 border border-accent/10 p-3 content-compact text-default-secondary">
          {agenticBanner}
        </div>
      )}
      <QuotaTypeEditor
        title={agenticTitle ?? "Agentic Execution Quotas"}
        description={agenticDescription ?? "Configure the platform-wide hard cap and default per-user limits for agent executions (chat messages + assignment runs). Users or groups can override the per-user default."}
        value={agentic}
        onChange={onChangeAgentic}
        disabled={disabled || agenticReadOnly}
        maxLimits={agenticMaxLimits}
        showPlatformLimit={showPlatformLimit}
        platformLimitDisabled={agenticPlatformLimitDisabled}
        platformLimitHint={agenticPlatformLimitHint}
        platformCap={showPlatformLimit ? undefined : platformAgenticCap}
        platformCapPeriod={platformAgenticCapPeriod}
        limitLabel={limitLabel}
        limitHint={limitHint}
        clearLabel={clearLabel}
        ceOnly={ceOnly}
        unlimitedLabel={unlimitedLabel}
        unlimitedHint={unlimitedHint}
        inheritPlaceholder={inheritPlaceholder}
        legacyNotice={notices?.agentic}
      />
      {!hideLLMProxy && (
        <QuotaTypeEditor
          title={llmProxyTitle ?? "LLM Proxy Quotas"}
          description={llmProxyDescription ?? "Configure the platform-wide hard cap and default per-user limits for LLM proxy requests. Users or groups can override the per-user default."}
          value={llmProxy}
          onChange={onChangeLLMProxy}
          availableModels={availableModels}
          showPerModel
          disabled={disabled}
          showPlatformLimit={showPlatformLimit}
          platformCap={showPlatformLimit ? undefined : platformLLMProxyCap}
          platformCapPeriod={platformLLMProxyCapPeriod}
          limitLabel={limitLabel}
          limitHint={limitHint}
          clearLabel={clearLabel}
          ceOnly={ceOnly}
          unlimitedLabel={unlimitedLabel}
          unlimitedHint={unlimitedHint}
          inheritPlaceholder={inheritPlaceholder}
          legacyNotice={notices?.llm_proxy}
        />
      )}
    </div>
  );
}
