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

/** The frame every Settings > Agents and automations > Platform Health block shares (#8254, #8261): one draft of the whole settings, one Edit / Save. */
import { SettingsGroupDescription, SettingsGroupTitle } from "@/components/ui/settings-form";
import type { HealthSettings } from "@/types";

export type BlockProps = {
  draft: HealthSettings;
  editing: boolean;
  onChange: (next: HealthSettings) => void;
};

export function BlockTitle({ icon: Icon, title, hint }: { icon: React.ComponentType<{ className?: string }>; title: string; hint: string }) {
  return (
    <div className="mb-2">
      <SettingsGroupTitle icon={Icon}>{title}</SettingsGroupTitle>
      <SettingsGroupDescription>{hint}</SettingsGroupDescription>
    </div>
  );
}
