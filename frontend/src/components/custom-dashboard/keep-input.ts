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
 * Keeping an input's value as the dashboard's (its default, saved with it): what the time range input offers
 * those who may edit the dashboard, as OpenCTI saves a dashboard's period with it. One `set` of the input,
 * revision-checked like every edit.
 */
import { useCallback, useEffect, useRef } from "react";

import { useToast } from "@/components/ui/toast";
import { useFormatter } from "@/i18n";
import type { CustomDashboardOperation } from "@/types";

import { keepInputOperation } from "./inputs";
import type { DashboardLive } from "./live";

type Edit = (operations: CustomDashboardOperation[], handlers?: { onSuccess?: () => void; onError?: (error: Error) => void }) => void;

/** The action a page hands its dashboard (`onKeepInput`), or `undefined` for a person who may not edit it. */
export function useKeepInput(definition: unknown, canManage: boolean, edit: Edit): DashboardLive["keepInput"] {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  // The latest definition and edit, read when the action runs: the action itself stays the same.
  const latest = useRef({ definition, edit });
  useEffect(() => {
    latest.current = { definition, edit };
  });
  const keep = useCallback<NonNullable<DashboardLive["keepInput"]>>(
    (input, value) => {
      const operation = keepInputOperation(latest.current.definition, input.id, value);
      if (!operation) return;
      latest.current.edit([operation], {
        onSuccess: () => toast({ title: t_i18n("Kept with the dashboard"), variant: "success" }),
        onError: (error) => toast({ title: t_i18n("The dashboard could not be changed"), description: error.message, variant: "error" }),
      });
    },
    [t_i18n, toast],
  );
  return canManage ? keep : undefined;
}
