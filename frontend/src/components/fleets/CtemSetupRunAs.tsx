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
 * The "Run checks as" pick of the guided CTEM loop setup, on its Modules step (dev-docs/platform-health.md
 * 12.3): whom Platform Health reads the modules as. Saved on its own (`PUT /health/settings/run-as`), with the
 * same refusal as the setting (a user without access to a registered module, the modules named); the step may
 * be skipped, and the module cards on the Health page then ask for it.
 */
import { useState } from "react";

import { Button } from "@/components/ui/button";
import { useHealthSettings, useSaveHealthRunAs } from "@/hooks/api/useHealthApi";
import { useFormatter } from "@/i18n";
import { HealthRunAsPicker, runAsRefusalText } from "@/pages/settings/HealthRunAsBlock";

export function CtemSetupRunAs() {
  const { t_i18n } = useFormatter();
  const settings = useHealthSettings();
  const save = useSaveHealthRunAs();
  const stored = settings.data?.module_run_as_user_id ?? null;
  const [picked, setPicked] = useState<string | null>(null);
  const value = picked ?? stored;
  const errorId = "ctem-setup-run-as-error";
  // The refusal in the reader's language, by its code (UX r1 B3); any other failure in general words.
  const error = save.isError ? (runAsRefusalText(save.error, t_i18n) ?? t_i18n("The run-as user was not saved.")) : null;
  return (
    <div className="space-y-2 border-t border-elevation-subtle-soft pt-3 content-compact" data-testid="ctem-loop-setup-run-as">
      <p className="font-medium">{t_i18n("Run checks as")}</p>
      <p className="text-default-secondary">
        {t_i18n("Platform Health reads the modules as this user to check them. Pick a user with access to every connected module.")}
      </p>
      <div className="flex flex-wrap items-start gap-2">
        <div className="min-w-64 flex-1">
          <HealthRunAsPicker
            value={value}
            onChange={(userId) => {
              setPicked(userId);
              save.reset();
            }}
            disabled={save.isPending}
            invalid={Boolean(error)}
            describedBy={error ? errorId : undefined}
          />
        </div>
        <Button
          type="button"
          variant="secondary"
          disabled={!picked || picked === stored || save.isPending}
          aria-busy={save.isPending || undefined}
          onClick={() => picked && save.mutate(picked, { onSuccess: () => setPicked(null) })}
          data-testid="ctem-loop-setup-run-as-save"
        >
          {t_i18n("Save")}
        </Button>
      </div>
      {error ? (
        <p id={errorId} role="alert" className="text-feedback-error-primary" data-testid="ctem-loop-setup-run-as-error">
          {error}
        </p>
      ) : stored && !picked ? (
        <p className="text-default-secondary" data-testid="ctem-loop-setup-run-as-saved">{t_i18n("Saved. The next check round reads the modules as this user.")}</p>
      ) : null}
    </div>
  );
}
