// SPDX-License-Identifier: Apache-2.0

import { useEffect, useState, type ReactNode } from "react";
import { SettingsGroupDescription, SettingsGroupTitle, SettingsSaveBar } from "@/components/ui/settings-form";
import type { LucideIcon } from "lucide-react";
import { Info, Pencil } from "lucide-react";
import { Button } from "@/components/ui/button";
import { EnvLockBadge } from "@/components/ui/env-lock-badge";
import { Input } from "@/components/ui/input";
import { ModelListRefreshing, ModelSelect } from "@/components/ui/model-select";
import { SearchableSelect } from "@/components/ui/searchable-select";
import { useFormatter } from "@/i18n";
import { modelChoiceValue, parseModelChoice } from "@/lib/utils";
import type { AvailableModel, LLMProvider } from "@/types";

interface PlatformModelSettingProps {
  /** Unique prefix for the picker's label and hint ids. */
  fieldId: string;
  icon: LucideIcon;
  /** Classes of the icon tile beside the current choice. */
  tileClassName: string;
  iconClassName: string;
  title: string;
  description: string;
  /** Shown instead of the choice when no model can be picked. */
  emptyMessage: string;
  pickHint: string;
  /** The picker's entry for "no model", and the current label when none is set. */
  noneLabel: string;
  models: AvailableModel[];
  providers?: LLMProvider[] | null;
  model?: string | null;
  providerId?: string | null;
  /** Shown beside the current label when no model is set. */
  unsetBadge?: ReactNode;
  setSubtitle: string;
  unsetSubtitle: string;
  envLocked: boolean;
  canEdit: boolean;
  isSaving: boolean;
  /**
   * Providers that serve this capability without listing their models: the
   * model id is typed for them.
   */
  typedProviders?: { id: string; label: string }[];
  /**
   * Keep a saved model its provider does not list on Configure: a decision
   * listing may be partial or have failed. Otherwise Save drops it.
   */
  keepUnlisted?: boolean;
  /** Why a provider offered no model, one line each. */
  notes?: string[];
  /** Persist the choice; ``model`` is "" to clear it. Call ``done`` on success. */
  onSave: (model: string, providerId: string | null, done: () => void) => void;
  /** Called as the picker opens: a list discovered from the providers is asked again. */
  onOpen?: () => void;
  /** The providers are being asked for the list again. */
  refreshing?: boolean;
  /** The list has not answered yet: an empty one means nothing so far. */
  loading?: boolean;
}

/** One platform-wide model choice (routing, decision) in Settings → AI. */
export function PlatformModelSetting({
  fieldId,
  icon: Icon,
  tileClassName,
  iconClassName,
  title,
  description,
  emptyMessage,
  pickHint,
  noneLabel,
  models,
  providers,
  model,
  providerId,
  unsetBadge,
  setSubtitle,
  unsetSubtitle,
  envLocked,
  canEdit,
  isSaving,
  typedProviders = [],
  keepUnlisted = false,
  notes = [],
  onSave,
  onOpen,
  refreshing = false,
  loading = false,
}: PlatformModelSettingProps) {
  const { t_i18n } = useFormatter();
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState("");
  const [typedModel, setTypedModel] = useState("");
  const [typedProvider, setTypedProvider] = useState("");
  const [initialSnapshot, setInitialSnapshot] = useState("");
  const pickable = models.length > 0 || typedProviders.length > 0;

  // The lock arrives with the config, after a Configure click may have opened
  // the form: drop the edit the moment the field turns out env-managed.
  useEffect(() => {
    if (envLocked) setEditing(false);
  }, [envLocked]);

  const startEditing = () => {
    const listed = models.some((m) => m.model === model && (!providerId || m.provider_id === providerId));
    const typedHere = !listed && !!providerId && typedProviders.some((p) => p.id === providerId);
    // An unlisted choice whose provider config was deleted is dropped all the same.
    const kept = keepUnlisted && (!providerId || !providers || providers.some((p) => p.id === providerId));
    const initial = {
      draft: !typedHere && (listed || kept) ? modelChoiceValue(model, providerId) : "",
      typedModel: typedHere ? (model ?? "") : "",
      typedProvider: typedHere ? (providerId ?? "") : (typedProviders[0]?.id ?? ""),
    };
    setDraft(initial.draft);
    setTypedModel(initial.typedModel);
    setTypedProvider(initial.typedProvider);
    setInitialSnapshot(JSON.stringify(initial));
    setEditing(true);
    onOpen?.();
  };
  const save = () => {
    if (typedModel.trim() && typedProvider) {
      onSave(typedModel.trim(), typedProvider, () => setEditing(false));
      return;
    }
    const choice = parseModelChoice(draft);
    onSave(choice.model || "", choice.providerId, () => setEditing(false));
  };

  return (
    <div>
      <div className="flex items-center justify-between mb-3">
        <div>
          <SettingsGroupTitle icon={Icon}>{title}</SettingsGroupTitle>
          <SettingsGroupDescription>{description}</SettingsGroupDescription>
        </div>
        <div className="flex gap-2 shrink-0 ml-4">
          {envLocked ? (
            <EnvLockBadge />
          ) : canEdit && (pickable || model) ? (
            !editing ? (
              <Button variant="secondary" size="sm" className="gap-1.5" onClick={startEditing}>
                <Pencil className="h-3 w-3" /> {t_i18n("Configure")}
              </Button>
            ) : null
          ) : null}
        </div>
      </div>

      {!pickable && !model && loading ? (
        <ModelListRefreshing active />
      ) : !pickable && !model ? (
        <div className="rounded-sm bg-elevation-hover border border-elevation-subtle-soft p-4 flex items-start gap-3 backdrop-blur-md shadow-inner">
          <div className="mt-0.5 shrink-0 rounded-full bg-elevation-hover p-1">
            <Info className="h-3 w-3 text-default-secondary" />
          </div>
          <p className="text-[13px] text-default-secondary leading-relaxed">{emptyMessage}</p>
        </div>
      ) : (
        <div className="space-y-3">
          <div className="flex items-center justify-between rounded-sm bg-elevation-default p-3.5">
            <div className="flex items-center gap-3">
              <div className={`flex h-8 w-8 items-center justify-center rounded-sm ${tileClassName}`}>
                <Icon className={`h-4 w-4 ${iconClassName}`} />
              </div>
              <div>
                <div className="flex items-center gap-2">
                  <p className="content-base font-medium">{model || noneLabel}</p>
                  {!model && unsetBadge}
                </div>
                <span className="content-compact text-default-secondary">{model ? setSubtitle : unsetSubtitle}</span>
              </div>
            </div>
          </div>
          {editing && (
            <div className="space-y-2">
              <label htmlFor={`${fieldId}-model`} className="content-base font-medium text-default-primary">{title}</label>
              <p id={`${fieldId}-model-hint`} className="content-compact text-default-secondary">{pickHint}</p>
              <ModelListRefreshing active={refreshing} />
              <ModelSelect
                id={`${fieldId}-model`}
                aria-label={title}
                aria-describedby={`${fieldId}-model-hint`}
                models={models}
                providers={providers}
                pinned={[{ value: "__none__", label: noneLabel }]}
                value={draft || "__none__"}
                onValueChange={(v) => setDraft(v === "__none__" ? "" : v)}
                disabled={!!typedModel.trim()}
              />
              {typedProviders.length > 0 && (
                <div className="space-y-1.5 pt-1" data-testid={`${fieldId}-typed`}>
                  <label htmlFor={`${fieldId}-typed-model`} className="content-base font-medium text-default-primary">
                    {t_i18n("Model id")}
                  </label>
                  <p id={`${fieldId}-typed-hint`} className="content-compact text-default-secondary">
                    {t_i18n("{providers} serve it without listing their models: type the model id the provider gives. It is used instead of the list.", {
                      providers: typedProviders.map((p) => p.label).join(", "),
                    })}
                  </p>
                  <div className="flex items-start gap-2">
                    {typedProviders.length > 1 && (
                      <SearchableSelect
                        aria-label={t_i18n("Provider")}
                        className="w-48 shrink-0"
                        options={typedProviders.map((p) => ({ value: p.id, label: p.label }))}
                        value={typedProvider}
                        onValueChange={setTypedProvider}
                        placeholder={t_i18n("Provider")}
                        searchable="auto"
                      />
                    )}
                    <Input
                      aria-label={t_i18n("Model id")}
                      id={`${fieldId}-typed-model`}
                      aria-describedby={`${fieldId}-typed-hint`}
                      value={typedModel}
                      maxLength={200}
                      onChange={(event) => setTypedModel(event.target.value)}
                      placeholder={t_i18n("The model id")}
                    />
                  </div>
                </div>
              )}
              <SettingsSaveBar
                dirty={JSON.stringify({ draft, typedModel, typedProvider }) !== initialSnapshot}
                saving={isSaving}
                onSave={save}
                onCancel={() => setEditing(false)}
              />
            </div>
          )}
        </div>
      )}
      {notes.length > 0 && (
        <ul className="mt-2 space-y-1" data-testid={`${fieldId}-notes`}>
          {notes.map((note, i) => (
            <li key={i} className="content-caption text-default-secondary">{note}</li>
          ))}
        </ul>
      )}
    </div>
  );
}
