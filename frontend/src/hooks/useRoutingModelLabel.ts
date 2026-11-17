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

import { useGeneralAssistantConfig } from "@/hooks/api/useSettingsApi";
import { useFormatter } from "@/i18n";

/**
 * What an agent on the platform routing model actually runs on: the
 * configured routing model, or — when none is configured — the platform
 * default, said out loud so an administrator knows to configure one.
 */
export function useRoutingModelLabel(): { label: string; configured: boolean } {
  const { t_i18n } = useFormatter();
  const { data: gaConfig } = useGeneralAssistantConfig();
  const model = gaConfig?.routing_model;
  return model
    ? { label: t_i18n("Platform routing model ({model})", { model }), configured: true }
    : {
        label: t_i18n("Platform routing model (none configured: platform default model)"),
        configured: false,
      };
}
