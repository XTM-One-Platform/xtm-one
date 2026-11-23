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

/**
 * Settings > Platform Health > Checks, "Run checks as" (dev-docs/platform-health.md 12.3): whom the
 * module checks read OpenCTI, OpenAEV and OpenCRQ as. The XTM One administrator generally has no
 * access to the modules, so an administrator picks a user who has; there is no other fallback. The
 * save refuses a user without access to a registered module and names the modules. XTM One modes only.
 */
import { Icon } from "@filigran/design-system";

import { userSelectOption } from "@/components/ui/entity-select-options";
import { SearchableSelect } from "@/components/ui/searchable-select";
import { SettingsField, settingsFieldHintId } from "@/components/ui/settings-form";
import { SECTION_BAR_ANCHOR_CLASS } from "@/components/ui/section-index";
import { SettingsReadField } from "@/components/ui/settings-read-view";
import { useUsers } from "@/hooks/api/useAuthApi";
import { useIsXtmOne } from "@/hooks/usePlatformConfig";
import { useFormatter } from "@/i18n";

import { BlockTitle, type BlockProps } from "./health-block";

const RunAsGlyph = ({ className }: { className?: string }) => <Icon name="user" size={16} className={className} aria-hidden />;

/** The anchor of the block: the module cards' "Set the run-as user" link lands on it (UX r1 B2). */
export const HEALTH_RUN_AS_ANCHOR = "health-settings-run-as";
/** The id of the settings picker, focused when a save refuses its user (UX r1 B1). */
export const HEALTH_RUN_AS_FIELD_ID = "health-settings-run-as-user";

type Translate = ReturnType<typeof useFormatter>["t_i18n"];

/**
 * A refused run-as user in the reader's language, read by the 400's code (`run_as.py`), or `null`
 * for any other error. The server's English line only for a code this build does not know.
 */
export function runAsRefusalText(error: unknown, t_i18n: Translate): string | null {
  const detail = (error as { detail?: unknown } | null)?.detail;
  if (!detail || typeof detail !== "object") return null;
  const { code, modules, message } = detail as { code?: unknown; modules?: unknown; message?: unknown };
  if (code === "run_as_missing_module_access") {
    const names = Array.isArray(modules) ? modules.filter((m): m is string => typeof m === "string").join(", ") : "";
    return t_i18n(
      "This user has no access to {modules}: Platform Health runs the module checks as this user. Give them access to {modules}, or pick a user who has it.",
      { modules: names },
    );
  }
  if (code === "run_as_inactive") return t_i18n("The run-as user does not exist or is deactivated.");
  return typeof code === "string" && code.startsWith("run_as_") && typeof message === "string" ? message : null;
}

/** The user picker of the setting and of the onboarding step: active users, searched by name or email. */
export function HealthRunAsPicker({
  value,
  onChange,
  disabled = false,
  id,
  invalid = false,
  describedBy,
}: {
  value: string | null;
  onChange: (userId: string) => void;
  disabled?: boolean;
  id?: string;
  /** The save refused this user: the field is marked invalid for assistive tech (13bis.5). */
  invalid?: boolean;
  describedBy?: string;
}) {
  const { t_i18n } = useFormatter();
  const { data: users } = useUsers();
  return (
    <SearchableSelect
      id={id}
      aria-label={t_i18n("Run checks as")}
      aria-invalid={invalid || undefined}
      aria-describedby={describedBy}
      options={(users ?? []).filter((u) => u.is_active).map((u) => userSelectOption(u))}
      value={value ?? ""}
      onValueChange={onChange}
      disabled={disabled}
      placeholder={t_i18n("Search users...")}
    />
  );
}

/** The user a stored id names, read at rest: the name and email, or a plain "Not set". */
function useRunAsLabel(userId: string | null | undefined): string {
  const { t_i18n } = useFormatter();
  const { data: users } = useUsers(Boolean(userId));
  if (!userId) return t_i18n("Not set");
  const user = users?.find((u) => u.id === userId);
  if (!user) return t_i18n("A specific user");
  return user.full_name?.trim() ? `${user.full_name} (${user.email})` : user.email;
}

export function HealthRunAsBlock({ draft, editing, onChange, runAsError }: BlockProps) {
  const { t_i18n } = useFormatter();
  const fieldId = HEALTH_RUN_AS_FIELD_ID;
  const isXtmOne = useIsXtmOne();
  const label = useRunAsLabel(draft.module_run_as_user_id);
  if (!isXtmOne) return null;
  const info = t_i18n(
    "The module checks read OpenCTI, OpenAEV and OpenCRQ as this user. Pick a user with access to every registered module: the save is refused otherwise. Not set: no module check runs.",
  );
  return (
    <div id={HEALTH_RUN_AS_ANCHOR} className={SECTION_BAR_ANCHOR_CLASS} data-testid="health.run-as">
      <BlockTitle
        icon={RunAsGlyph}
        title={t_i18n("Run checks as")}
        hint={t_i18n("The XTM One administrator often has no access to the modules: choose a user who has.")}
      />
      <div className="rounded-sm bg-elevation-default p-3.5">
        {editing ? (
          <SettingsField id={fieldId} label={t_i18n("User")} info={info} error={runAsError ?? undefined}>
            <HealthRunAsPicker
              id={fieldId}
              invalid={Boolean(runAsError)}
              describedBy={runAsError ? settingsFieldHintId(fieldId) : undefined}
              value={draft.module_run_as_user_id ?? null}
              onChange={(userId) => onChange({ ...draft, module_run_as_user_id: userId })}
            />
          </SettingsField>
        ) : (
          <SettingsReadField label={t_i18n("User")} info={info} value={label} />
        )}
      </div>
    </div>
  );
}
