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

import { useCallback } from "react";
import { useToast } from "@/components/ui/toast";
import { useFormatter } from "@/i18n";

/**
 * The toast of a launch the server refused, with its reason (no output family, no
 * dataset, a model no provider serves, AI switched off). `useTriggerEvaluation`
 * suppresses the app's global error toast, so every caller shows this one.
 */
export function useTriggerFailedToast(): (error: unknown) => void {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  return useCallback(
    (error: unknown) =>
      toast({
        title: t_i18n("Failed to trigger evaluation"),
        description: error instanceof Error ? error.message : t_i18n("Unknown error"),
        variant: "error",
      }),
    [t_i18n, toast],
  );
}
