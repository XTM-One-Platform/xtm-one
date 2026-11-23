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
import { useId } from "react";
import { Icon } from "@filigran/design-system";

import { userSelectOption } from "@/components/ui/entity-select-options";
import { SearchableSelect } from "@/components/ui/searchable-select";
import { SettingsField } from "@/components/ui/settings-form";
import { SettingsReadField } from "@/components/ui/settings-read-view";
import { useUsers } from "@/hooks/api/useAuthApi";
import { useIsXtmOne } from "@/hooks/usePlatformConfig";
import { useFormatter } from "@/i18n";

import { BlockTitle, type BlockProps } from "./health-block";

const RunAsGlyph = ({ className }: { className?: string }) => <Icon name="user" size={16} className={className} aria-hidden />;

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

export function HealthRunAsBlock({ draft, editing, onChange }: BlockProps) {
  const { t_i18n } = useFormatter();
  const fieldId = useId();
  const isXtmOne = useIsXtmOne();
  const label = useRunAsLabel(draft.module_run_as_user_id);
  if (!isXtmOne) return null;
  const info = t_i18n(
    "The module checks read OpenCTI, OpenAEV and OpenCRQ as this user. Pick a user with access to every registered module: the save is refused otherwise. Not set: no module check runs.",
  );
  return (
    <div data-testid="health.run-as">
      <BlockTitle
        icon={RunAsGlyph}
        title={t_i18n("Run checks as")}
        hint={t_i18n("The XTM One administrator often has no access to the modules: choose a user who has.")}
      />
      <div className="rounded-sm bg-elevation-default p-3.5">
        {editing ? (
          <SettingsField id={fieldId} label={t_i18n("User")} info={info}>
            <HealthRunAsPicker
              id={fieldId}
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
