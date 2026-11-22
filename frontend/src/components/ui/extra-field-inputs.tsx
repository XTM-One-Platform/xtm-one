// SPDX-License-Identifier: Apache-2.0

import { useId } from "react";
import { InfoTip } from "@/components/ui/info-tip";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { SearchableSelect } from "@/components/ui/searchable-select";
import { Switch } from "@/components/ui/switch";
import { TagInput } from "@/components/ui/tag-input";
import {
  EXTRA_CONFIG_MASK,
  deriveControllingValue,
  domainList,
  extraFieldLabels,
  extraFieldProblem,
  fieldNameInSentence,
  isExtraFieldVisible,
  isSecretExtraKey,
  type ExtraFieldLabel,
  type ExtraFieldInputSpec,
  type ExtraFieldOption,
  type VisibleWhen,
} from "@/lib/extra-fields";
import { useFormatter } from "@/i18n";

/** A field's label at body size, its info bubble (the catalog's explanation) and its "Optional" mark. */
function ExtraFieldLabelRow({ htmlFor, label }: { htmlFor?: string; label: ExtraFieldLabel }) {
  const { t_i18n } = useFormatter();
  return (
    <div className="flex items-center gap-1">
      <Label htmlFor={htmlFor} className="text-default-primary">{label.name}</Label>
      {label.detail && <InfoTip label={t_i18n("About {field}", { field: label.name })} content={label.detail} />}
      {label.optional && <span className="content-compact text-default-secondary">{t_i18n("Optional")}</span>}
    </div>
  );
}

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
 * A field the catalog types (`inputs`, its `extra_field_inputs`) gets its own
 * control: a `domains` list is a tag input (stored one entry per line) whose
 * entries that are not domain names are marked, an `integer` a number field
 * within its bounds; either shows its inline error under the field, tied by
 * `aria-describedby` and `aria-invalid`, and the caller refuses the save
 * (`hasInvalidExtraField`). Its `help` is the help line.
 *
 * The catalog label is split ({@link extraFieldLabels}): its name is the
 * label, its explanation is the info bubble beside it, a field it says may stay
 * empty is marked "Optional", and the placeholder is a sentence on the name,
 * never the raw key. A label that does not open with a name (a number, a
 * sentence, a group shared by several fields) is named off its key and kept
 * whole in the bubble, so every field of a provider has its own name. The caller's generic `helpFor` line (where a credential
 * comes from) is for text inputs only: a select answers its own question.
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
  inputs,
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
  /** The typed controls the catalog declares (`extra_field_inputs`). */
  inputs?: Record<string, ExtraFieldInputSpec> | null;
}) {
  const { t_i18n } = useFormatter();
  const idBase = useId();
  const editing = mode === "edit";
  const omitted = new Set(omitKeys ?? []);
  const set = (key: string, value: unknown) => onChange({ ...values, [key]: value } as T);
  const labels = extraFieldLabels(fields);

  return (
    <>
      {Object.entries(fields)
        .filter(([key]) => !omitted.has(key))
        // Hidden by an unpicked answer; omitted when saving, which clears it.
        .filter(([key]) => isExtraFieldVisible(key, values, visibleWhen, options))
        .map(([key]) => {
          const schemaType = schemaTypeByKey?.get(key);
          const label = labels[key];
          const name = label.name;
          if (editing && schemaType === "boolean") {
            return (
              <div key={key} className="flex items-center justify-between rounded-sm bg-elevation-hover px-3 py-2">
                <ExtraFieldLabelRow htmlFor={`extra-field-${key}`} label={label} />
                <Switch id={`extra-field-${key}`} aria-label={name} checked={Boolean(values[key])} onCheckedChange={(checked) => set(key, checked)} />
              </div>
            );
          }
          const choices = options?.[key];
          const current = values[key];
          const storedIsMasked = editing && current === EXTRA_CONFIG_MASK;
          const spec = inputs?.[key];
          // The catalog's own words, like the field labels (served in English).
          const help = spec?.help ?? (choices ? undefined : helpFor?.(key));
          if (spec && (spec.kind === "domains" || spec.kind === "integer")) {
            const problem = extraFieldProblem(current, spec);
            const fieldId = `${idBase}-${key}`;
            const helpId = `${fieldId}-help`;
            const errorId = `${fieldId}-error`;
            const describedBy = [problem ? errorId : null, help ? helpId : null].filter(Boolean).join(" ") || undefined;
            const message = !problem
              ? null
              : "badDomains" in problem
                ? t_i18n("Not a domain name: {entries}", { entries: problem.badDomains.join(", ") })
                : t_i18n("Enter a whole number from {min} to {max}.", { min: spec.minimum ?? 0, max: spec.maximum ?? 0 });
            return (
              <div key={key} className="space-y-1">
                <ExtraFieldLabelRow htmlFor={fieldId} label={label} />
                {spec.kind === "domains" ? (
                  <TagInput
                    id={fieldId}
                    aria-label={name}
                    aria-describedby={describedBy}
                    aria-invalid={Boolean(problem)}
                    invalidTags={problem && "badDomains" in problem ? problem.badDomains : undefined}
                    suggestFromPlatformTags={false}
                    placeholder="example.com"
                    value={domainList(current)}
                    onChange={(next) => set(key, next.join("\n"))}
                  />
                ) : (
                  <Input
                    id={fieldId}
                    aria-label={name}
                    aria-describedby={describedBy}
                    aria-invalid={Boolean(problem)}
                    type="number"
                    inputMode="numeric"
                    min={spec.minimum ?? undefined}
                    max={spec.maximum ?? undefined}
                    step={1}
                    value={typeof current === "string" || typeof current === "number" ? String(current) : ""}
                    onChange={(e) => set(key, e.target.value)}
                  />
                )}
                {message && <p id={errorId} className="content-compact text-feedback-error-primary">{message}</p>}
                {help && <p id={helpId} className="content-compact text-default-secondary">{help}</p>}
              </div>
            );
          }
          const secret = isSecretExtraKey(key, schemaType);
          const sentenceName = fieldNameInSentence(name);
          return (
            <div key={key} className="space-y-1">
              <ExtraFieldLabelRow label={label} />
              {choices ? (
                <SearchableSelect
                  aria-label={name}
                  searchable="auto"
                  sort={false}
                  options={choices}
                  value={deriveControllingValue(key, values, visibleWhen, choices)}
                  onValueChange={(v) => set(key, v)}
                  placeholder={t_i18n("Select {field}...", { field: sentenceName })}
                />
              ) : (
                <Input aria-label={name}
                  type={secret && !showSecrets ? "password" : "text"}
                  placeholder={storedIsMasked
                    ? t_i18n("Stored: type to replace")
                    : secret
                      ? t_i18n("Paste {field}...", { field: sentenceName })
                      : t_i18n("Enter {field}...", { field: sentenceName })}
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
