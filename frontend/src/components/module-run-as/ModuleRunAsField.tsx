// SPDX-License-Identifier: Apache-2.0

import { useEffect, useId, type ReactNode } from "react";

import type { SelectOption } from "@/components/ui/searchable-select";
import { SearchableSelect } from "@/components/ui/searchable-select";
import { SettingsField, settingsFieldHintId } from "@/components/ui/settings-form";
import { useModuleRunAsCandidates, type ModuleRunAsCandidate, type RunAsModule } from "@/hooks/api/useAuthApi";
import { useFormatter } from "@/i18n";

/** The module's name in the field's label and texts. */
export const RUN_AS_MODULE_NAME: Record<RunAsModule, string> = { opencti: "OpenCTI", openaev: "OpenAEV", opencrq: "OpenCRQ" };

/** Whether a provider is one of the modules XTM One acts in per user (#2650). */
export function isRunAsModule(provider: string | null | undefined): provider is RunAsModule {
  return provider === "opencti" || provider === "openaev" || provider === "opencrq";
}

/** One candidate as an option: the caller reads "Me", a user without the module connected is disabled with the reason. */
function candidateOption(candidate: ModuleRunAsCandidate, module: string, t: ReturnType<typeof useFormatter>["t_i18n"]): SelectOption {
  const name = candidate.is_self ? t("Me ({name})", { name: candidate.name }) : candidate.name;
  const reason = candidate.selectable ? undefined : t("{module} not connected", { module });
  const email = candidate.email && candidate.email !== candidate.name ? candidate.email : undefined;
  return {
    value: candidate.id,
    label: name,
    description: reason ?? email,
    keywords: [candidate.email],
    disabled: !candidate.selectable,
  };
}

/**
 * "Act in <module> as" (#2650): the user whose connection, markings and rights XTM One uses in OpenCTI,
 * OpenAEV or OpenCRQ. The caller first and picked by default when they can be; an administrator may pick
 * any active user, and those without the module connected are listed disabled with the reason.
 * Every place that provisions or reads in a module in the background (a signal source, a fleet, a sync) asks it.
 */
export function ModuleRunAsField({
  module,
  value,
  onChange,
  disabled,
  error,
  helper,
  active = true,
  required = true,
}: {
  module: RunAsModule;
  value: string;
  onChange: (userId: string) => void;
  disabled?: boolean;
  error?: ReactNode;
  helper?: string;
  /** False while the host dialog is closed: nothing is read. */
  active?: boolean;
  /** False where an unset value has a default (the evidence health pass reads as the integration's owner). */
  required?: boolean;
}) {
  const { t_i18n } = useFormatter();
  const fieldId = useId();
  const name = RUN_AS_MODULE_NAME[module];
  const { data, isError } = useModuleRunAsCandidates(module, active);
  const candidates = Array.isArray(data) ? data : undefined;
  const self = candidates?.find((candidate) => candidate.is_self && candidate.selectable);

  // Default: the person configuring now, once the list says they can be picked.
  useEffect(() => {
    if (active && !value && self) onChange(self.id);
  }, [active, value, self, onChange]);

  const noneSelectable = candidates !== undefined && !candidates.some((candidate) => candidate.selectable);
  const shownError =
    error ??
    (isError
      ? t_i18n("The users XTM One can act as could not be read.")
      : noneSelectable
        ? t_i18n("Nobody has {module} connected. Connect it in Integrations first.", { module: name })
        : undefined);

  return (
    <SettingsField
      id={fieldId}
      label={t_i18n("Act in {module} as", { module: name })}
      required={required}
      info={t_i18n(
        "The XTM One user whose {module} connection, markings and rights XTM One uses here. You by default; an administrator can pick any active user with {module} connected. If that user is deactivated or loses the connection, this stops until you pick another user.",
        { module: name },
      )}
      helper={helper ?? t_i18n("Only users with {module} connected can be picked.", { module: name })}
      error={shownError ? <span data-testid="module-run-as.error">{shownError}</span> : undefined}
    >
      <SearchableSelect
        id={fieldId}
        aria-label={t_i18n("Act in {module} as", { module: name })}
        aria-describedby={settingsFieldHintId(fieldId)}
        aria-invalid={shownError ? true : undefined}
        options={(candidates ?? []).map((candidate) => candidateOption(candidate, name, t_i18n))}
        sort={false}
        searchable="auto"
        value={value}
        onValueChange={onChange}
        disabled={disabled || candidates === undefined}
        placeholder={t_i18n("Search users...")}
      />
    </SettingsField>
  );
}
