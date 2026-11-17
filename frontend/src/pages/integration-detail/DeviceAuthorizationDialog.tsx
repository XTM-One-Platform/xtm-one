// SPDX-License-Identifier: Apache-2.0

/**
 * Sign in with a code (OAuth2 device authorization): show the code and where
 * to enter it, then ask the backend — at the interval the API set — whether
 * the user approved. Closing the dialog abandons the pending sign-in.
 */
import { CheckCircle2, ExternalLink, XCircle } from "lucide-react";
import { Button } from "@/components/ui/button";
import { CopyIconButton } from "@/components/ui/copy-icon-button";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { cancelDeviceAuthorization, useDeviceAuthorizationPoll } from "@/hooks/api/useIntegrationApi";
import { useFormatter } from "@/i18n";
import type { DeviceAuthorization, DevicePollResult } from "@/types";
import { Spinner } from "@filigran/design-system";

export function DeviceAuthorizationDialog({
  integrationId,
  flow,
  onClose,
}: {
  integrationId: string;
  flow: DeviceAuthorization | null;
  onClose: () => void;
}) {
  const { t_i18n } = useFormatter();
  const poll = useDeviceAuthorizationPoll(integrationId, flow);
  const result: DevicePollResult | null = poll.error
    ? { status: "failed", message: poll.error.message || null }
    : (poll.data ?? null);
  const finished = result !== null && result.status !== "pending";

  const close = () => {
    if (flow && !finished) void cancelDeviceAuthorization(integrationId, flow.flow_id).catch(() => undefined);
    onClose();
  };

  const verification = flow?.verification_uri_complete ?? flow?.verification_uri;

  return (
    <Dialog open={!!flow} onOpenChange={(open) => { if (!open) close(); }}>
      <DialogContent className="max-w-md">
        <DialogHeader>
          <DialogTitle>{t_i18n("Sign in with a code")}</DialogTitle>
          <DialogDescription>
            {t_i18n("Open the sign-in page, enter this code and approve the access. This window updates by itself.")}
          </DialogDescription>
        </DialogHeader>
        {flow && (
          <div className="space-y-4">
            <div className="flex items-center justify-center gap-2 rounded-sm bg-elevation-hover py-4">
              <span className="font-mono-plex text-2xl font-semibold tracking-[0.2em]">{flow.user_code}</span>
              <CopyIconButton value={flow.user_code} label={t_i18n("Copy the code")} />
            </div>
            {verification && (
              <Button asChild className="w-full gap-2">
                <a href={verification} target="_blank" rel="noreferrer">
                  <ExternalLink className="h-4 w-4" />
                  {t_i18n("Open the sign-in page")}
                </a>
              </Button>
            )}
            <p className="break-all text-center text-[11px] text-default-secondary">{flow.verification_uri}</p>
            <div className="flex items-center justify-center gap-2 content-compact">
              {!finished && (
                <>
                  <Spinner size="sm" tone="inherit" className="text-accent" />
                  <span className="text-default-secondary">{t_i18n("Waiting for the approval…")}</span>
                </>
              )}
              {result?.status === "connected" && (
                <>
                  <CheckCircle2 className="h-4 w-4 text-feedback-success-primary" />
                  <span>{t_i18n("Signed in: the integration is connected.")}</span>
                </>
              )}
              {finished && result?.status !== "connected" && (
                <>
                  <XCircle className="h-4 w-4 text-feedback-error-primary" />
                  <span>{result?.message ?? t_i18n("The sign-in did not complete.")}</span>
                </>
              )}
            </div>
            {!finished && result?.message && (
              <p className="text-center content-caption text-feedback-warning-primary">{result.message}</p>
            )}
            <div className="flex justify-end">
              <Button variant={finished ? "default" : "ghost"} onClick={close}>
                {finished ? t_i18n("Close") : t_i18n("Cancel")}
              </Button>
            </div>
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}
