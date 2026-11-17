// SPDX-License-Identifier: Apache-2.0

import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { SearchableSelect } from "@/components/ui/searchable-select";
import { Switch } from "@/components/ui/switch";
import {
  EXTRA_CONFIG_MASK,
  deriveControllingValue,
  isExtraFieldVisible,
  isSecretExtraKey,
  type ExtraFieldOption,
  type VisibleWhen,
} from "@/lib/extra-fields";
import { useFormatter } from "@/i18n";

/**
 * The inputs for a provider's extra configuration fields: one renderer for
 * the creation form's authentication branches and the configuration card's
 * edit view, so they agree on which values get a password input.
 *
 * Hidden fields (`extra_field_visible_when`) are not rendered and the caller
 * omits them when saving; a field with options is a select; a field the
 * secret rule recognises is a password input until `showSecrets`.
 *
 * Labels are the design-system `Label` (14 px) and inputs keep the 36 px
 * field height in both modes, so these fields match their siblings.
 *
 * `mode="edit"` adds a switch for a schema-declared boolean,
 * and an EMPTY input where the API returned its mask for a stored secret: the
 * mask is not a value anyone can reveal, so the placeholder says one is stored
 * and the mask stays in the caller's state so leaving the field keeps it.
 */
export function ExtraFieldInputs<T extends Record<string, unknown>>({
  fields,
  values,
  onChange,
  options,
  visibleWhen,
  showSecrets,
  mode = "create",
  omitKeys,
  schemaTypeByKey,
  helpFor,
}: {
  /** Field key -> label, as the catalog declares it. */
  fields: Record<string, string>;
  values: T;
  onChange: (next: T) => void;
  options?: Record<string, ExtraFieldOption[]> | null;
  visibleWhen?: VisibleWhen | null;
  showSecrets: boolean;
  mode?: "create" | "edit";
  /** Declared fields a caller does not want here (e.g. a webhook secret the platform manages). */
  omitKeys?: readonly string[];
  /** Custom bundle schema types by key: `boolean` renders a switch, `secret` forces masking. */
  schemaTypeByKey?: ReadonlyMap<string, string>;
  /** The help line under a field (where its value comes from); none when it answers nothing. */
  helpFor?: (key: string) => string | undefined;
}) {
  const { t_i18n } = useFormatter();
  const editing = mode === "edit";
  const omitted = new Set(omitKeys ?? []);
  const set = (key: string, value: unknown) => onChange({ ...values, [key]: value } as T);

  return (
    <>
      {Object.entries(fields)
        .filter(([key]) => !omitted.has(key))
        // Hidden by an unpicked answer; omitted when saving, which clears it.
        .filter(([key]) => isExtraFieldVisible(key, values, visibleWhen, options))
        .map(([key, desc]) => {
          const schemaType = schemaTypeByKey?.get(key);
          if (editing && schemaType === "boolean") {
            return (
              <div key={key} className="flex items-center justify-between rounded-sm bg-elevation-hover px-3 py-2">
                <Label htmlFor={`extra-field-${key}`} className="text-default-primary">{desc}</Label>
                <Switch id={`extra-field-${key}`} aria-label={desc} checked={Boolean(values[key])} onCheckedChange={(checked) => set(key, checked)} />
              </div>
            );
          }
          const choices = options?.[key];
          const current = values[key];
          const storedIsMasked = editing && current === EXTRA_CONFIG_MASK;
          const help = helpFor?.(key);
          return (
            <div key={key} className="space-y-1">
              <Label className="block text-default-primary">{desc}</Label>
              {choices ? (
                <SearchableSelect
                  aria-label={desc}
                  searchable="auto"
                  sort={false}
                  options={choices}
                  value={deriveControllingValue(key, values, visibleWhen, choices)}
                  onValueChange={(v) => set(key, v)}
                  placeholder={t_i18n("Select {field}...", { field: desc.toLowerCase() })}
                />
              ) : (
                <Input aria-label={desc}
                  type={isSecretExtraKey(key, schemaType) && !showSecrets ? "password" : "text"}
                  placeholder={storedIsMasked ? t_i18n("Stored: type to replace") : key}
                  value={typeof current === "string" && !storedIsMasked ? current : ""}
                  onChange={(e) => set(key, e.target.value)}
                />
              )}
              {help && <p className="content-compact text-default-secondary">{help}</p>}
            </div>
          );
        })}
    </>
  );
}
