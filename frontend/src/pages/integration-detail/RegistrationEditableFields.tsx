// SPDX-License-Identifier: Apache-2.0

import { useState } from "react";
import { Icon } from "@filigran/design-system";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { SearchableSelect } from "@/components/ui/searchable-select";
import { useToast } from "@/components/ui/toast";
import { useUpdateIntegration } from "@/hooks/api/useIntegrationApi";
import { useFormatter } from "@/i18n";
import { EXTRA_CONFIG_MASK, isSecretExtraKey, type ExtraFieldOption } from "@/lib/extra-fields";
import type { Integration } from "@/types";

/**
 * The few extra fields a registration leaves to the deployment (the OpenAEV tenant, the
 * webhook secret OpenAEV and OpenCTI send), edited with the card's Edit / Save Changes /
 * Cancel while the rest stays read-only. The shared card cannot be reused: its edit view
 * also edits name, sharing and approval.
 *
 * A secret is never shown: the API returns it as the mask, the read view says "Stored",
 * the edit view is a password input left empty ("Stored: type to replace"), and an empty
 * input sends the mask back, which the API reads as "keep the stored value".
 *
 * A field the catalog declares with choices (OpenCTI Live writes) is a select; unset, it
 * reads as its first choice, the catalog's default.
 *
 * Labels, controls, help lines and buttons follow the shared edit view (ExtraFieldInputs and
 * the configuration card's footer): 14 px labels bound to their control, 36 px controls and
 * buttons, Cancel then the primary action on the right, and a help line always visible.
 */
export function RegistrationEditableFields({ integration, fieldKeys, labels, canEdit, options }: { integration: Integration; fieldKeys: string[]; labels: Record<string, string>; canEdit: boolean; options?: Record<string, ExtraFieldOption[]> | null }) {
  const { t_i18n } = useFormatter();
  const update = useUpdateIntegration();
  const { toast } = useToast();
  const [editing, setEditing] = useState(false);
  const stored = (key: string) => {
    const value = integration.extra_config?.[key];
    return typeof value === "string" ? value : "";
  };
  const choicesOf = (key: string) => (options?.[key]?.length ? options[key] : undefined);
  // The value a field holds now: a choice field left unset holds its default (first) choice.
  const current = (key: string) => stored(key) || choicesOf(key)?.[0]?.value || "";
  const isSecret = (key: string) => isSecretExtraKey(key);
  const storedSecret = (key: string) => isSecret(key) && stored(key) !== "";
  const [values, setValues] = useState<Record<string, string>>({});
  // What a save sends for a key: an untouched stored secret goes back as the mask.
  const submitted = (key: string) => {
    const text = (values[key] ?? "").trim();
    return text === "" && storedSecret(key) ? EXTRA_CONFIG_MASK : text;
  };
  const changed = fieldKeys.some((key) => submitted(key) !== current(key));

  const startEditing = () => {
    setValues(Object.fromEntries(fieldKeys.map((key) => [key, isSecret(key) ? "" : current(key)])));
    setEditing(true);
  };

  const save = () => {
    update.mutate(
      { id: integration.id, extra_config: Object.fromEntries(fieldKeys.map((key) => [key, submitted(key)])) },
      {
        onSuccess: () => {
          toast({ title: t_i18n("Integration updated"), description: t_i18n("Changes saved successfully."), variant: "success" });
          setEditing(false);
        },
        onError: (error) => toast({ title: t_i18n("Error"), description: error.message, variant: "error" }),
      },
    );
  };

  const readView = (key: string) => {
    if (isSecret(key)) return storedSecret(key) ? t_i18n("Stored") : "—";
    const choice = choicesOf(key)?.find((o) => o.value === current(key));
    if (choice) return choice.label;
    return stored(key) || "—";
  };

  // Every field here is the deployment's own: say it survives re-registration, and for
  // Live writes that it is the default of every bound agent, which an agent binding can
  // override on the agent's Tooling tab (#11958).
  const help = (key: string) =>
    key === "live_writes"
      ? t_i18n("Default for bound agents; an agent can override it on its Tooling tab. Kept when the platform registers it again.")
      : t_i18n("Kept when the platform registers this integration again.");

  const placeholder = (key: string) => {
    if (!isSecret(key)) return t_i18n("Empty: chosen automatically");
    return storedSecret(key) ? t_i18n("Stored: type to replace") : t_i18n("Not set");
  };

  return (
    <div className="space-y-3">
      {fieldKeys.map((key, index) => (
        <div key={key}>
          {/* The Edit button sits at the right of the first label, where the
              configuration card puts its own at the right of its header. */}
          <div className="flex items-center justify-between mb-1 gap-2">
            <Label id={`registration-label-${key}`} htmlFor={editing ? `registration-field-${key}` : undefined} className="block text-default-primary">{labels[key] || key}</Label>
            {index === 0 && canEdit && !editing && (
              <Button variant="secondary" className="gap-1.5" onClick={startEditing}>
                <Icon name="pencil" size={16} /> {t_i18n("Edit")}
              </Button>
            )}
          </div>
          {editing && choicesOf(key) ? (
            <SearchableSelect
              id={`registration-field-${key}`}
              aria-labelledby={`registration-label-${key}`}
              aria-describedby={`registration-help-${key}`}
              searchable="auto"
              sort={false}
              options={choicesOf(key) ?? []}
              value={values[key] ?? ""}
              onValueChange={(v) => setValues({ ...values, [key]: v })}
            />
          ) : editing ? (
            <Input
              aria-label={labels[key] || key}
              aria-describedby={`registration-help-${key}`}
              id={`registration-field-${key}`}
              type={isSecret(key) ? "password" : "text"}
              autoComplete={isSecret(key) ? "new-password" : undefined}
              value={values[key] ?? ""}
              placeholder={placeholder(key)}
              onChange={(e) => setValues({ ...values, [key]: e.target.value })}
            />
          ) : (
            <div className="content-base font-mono-plex bg-elevation-highlight px-3 py-2 rounded-sm break-all">{readView(key)}</div>
          )}
          <p id={`registration-help-${key}`} className="mt-1 content-compact text-default-secondary">{help(key)}</p>
        </div>
      ))}
      {canEdit && editing && (
        <div className="flex justify-end gap-2 pt-1">
          <Button variant="secondary" onClick={() => setEditing(false)} disabled={update.isPending}>{t_i18n("Cancel")}</Button>
          <Button onClick={save} disabled={!changed || update.isPending}>
            {update.isPending ? t_i18n("Saving...") : t_i18n("Save changes")}
          </Button>
        </div>
      )}
    </div>
  );
}
