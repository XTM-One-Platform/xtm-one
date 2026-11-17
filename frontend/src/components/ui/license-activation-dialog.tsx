// SPDX-License-Identifier: Apache-2.0

import { useRef, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Icon } from "@filigran/design-system";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { invalidateApprovalUsage } from "@/hooks/api/useToolApprovals";
import { api } from "@/lib/api";

interface LicenseActivationDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  platformName?: string;
}

/**
 * Shared license-activation dialog.
 *
 * Wraps the textarea + ``/settings/general-assistant`` mutation +
 * inline error display pattern in a single Dialog so callers that
 * already have a contextual "Activate License" button only need to
 * own the open/closed state.
 *
 * Current adopters:
 *   - ``CtemTrialSplash`` (XTM One CE-only home splash)
 *   - ``LicenseStatusBanner`` in ``QuotasSection`` (admin license banner)
 *
 * The Copilot-mode lockdown ``LicenseRequired`` and the per-feature
 * ``AgenticGateDialog`` / ``AgenticGatePage`` keep their own inline
 * activation forms — they need richer in-context UX (license status
 * sub-card, env-var notice, "update license" toggle, gate-specific
 * copy) that doesn't fit the simple dialog wrapper above.
 *
 * The dialog stays open until the backend accepts the PEM — invalid
 * licenses surface a 400 with a user-facing ``detail`` that we render
 * inline so the admin can correct the input.
 */
export function LicenseActivationDialog({
  open,
  onOpenChange,
  platformName = "the platform",
}: LicenseActivationDialogProps) {
  const [pem, setPem] = useState("");
  const [error, setError] = useState<string | null>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const qc = useQueryClient();

  const saveLicense = useMutation({
    // Invalid PEMs are surfaced inline (the destructive banner above the
    // textarea) so the admin can correct the input without losing
    // context. Suppress the app-level MutationCache toast in
    // ``main.tsx`` to avoid duplicating the same message in a global
    // toast — otherwise the admin gets two contradictory-looking
    // surfaces for the same 400 response.
    meta: { suppressGlobalErrorToast: true },
    mutationFn: (license: string) =>
      api.put("/settings/general-assistant", { enterprise_license: license }),
    onMutate: () => setError(null),
    onSuccess: () => {
      setPem("");
      if (textareaRef.current) textareaRef.current.value = "";
      onOpenChange(false);
      // Refetch platform config so the rest of the UI picks up the
      // new tier / quotas / feature flags without a hard reload.
      qc.refetchQueries({ queryKey: ["platform-config"] });
      invalidateApprovalUsage(qc);
    },
    onError: (err: Error) => setError(err.message || "Failed to validate license"),
  });

  return (
    <Dialog
      open={open}
      onOpenChange={(v) => {
        onOpenChange(v);
        if (!v) setError(null);
      }}
    >
      <DialogContent className="max-w-md">
        <DialogHeader>
          <div className="flex items-start gap-3">
            <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-full bg-filigran-ia-primary/10">
              <Icon name="shield-check" size={20} className="text-filigran-ia-primary" />
            </div>
            <div className="space-y-1">
              <DialogTitle className="text-base">Activate Enterprise License</DialogTitle>
              <DialogDescription className="content-base leading-relaxed">
                Paste your Filigran license PEM certificate to activate {platformName} Enterprise features.
              </DialogDescription>
            </div>
          </div>
        </DialogHeader>

        <div className="space-y-3 pt-2">
          <textarea
            ref={textareaRef}
            defaultValue={pem}
            onChange={(e) => {
              setPem(e.target.value);
              if (error) setError(null);
            }}
            placeholder={"-----BEGIN CERTIFICATE-----\nPaste your Filigran license PEM...\n-----END CERTIFICATE-----"}
            className="w-full h-32 rounded-sm bg-elevation-default border border-elevation-subtle px-3 py-2 content-base font-mono-plex placeholder:text-default-secondary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus resize-none"
          />
          {error && (
            <div
              role="alert"
              className="flex items-start gap-2 rounded-sm border border-feedback-error-primary/30 bg-feedback-error-primary/10 p-2 content-base text-feedback-error-primary"
            >
              <Icon name="triangle-alert" size={16} className="shrink-0 mt-0.5" />
              <span className="leading-relaxed">{error}</span>
            </div>
          )}
          <Button
            className="w-full"
            onClick={() => {
              const val = textareaRef.current?.value || pem;
              if (!val.trim()) {
                setError("Paste a license PEM certificate before activating.");
                return;
              }
              saveLicense.mutate(val);
            }}
            disabled={saveLicense.isPending}
          >
            {saveLicense.isPending ? "Validating..." : "Activate License"}
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}
