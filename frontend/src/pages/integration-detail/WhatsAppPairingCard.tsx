// SPDX-License-Identifier: Apache-2.0

import { AlertTriangle, QrCode, Smartphone } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { useWhatsAppPairing } from "@/hooks/api/useIntegrationApi";
import { useFormatter } from "@/i18n";
import type { Integration, WhatsAppPairingErrorCode, WhatsAppPairingState } from "@/types";
import { CopyableUrl } from "./CopyableUrl";
import { Spinner } from "@filigran/design-system";

const BADGE: Record<WhatsAppPairingState, "success" | "warning" | "destructive" | "secondary"> = {
  connected: "success",
  qr: "warning",
  starting: "secondary",
  sync_error: "warning",
  banned: "destructive",
  unknown: "secondary",
};

/**
 * On the Configuration tab of a `whatsapp` integration on the linked-device gateway backend:
 * the live pairing state of the number, the login QR to scan while it is not paired, and
 * the gateway webhook URL to set in the gateway's channel settings.
 *
 * The route reads the gateway through the client the agent tools use, so the state here is
 * what `get_whatsapp_pairing_status` answers. The QR and the webhook URL (it carries the
 * webhook secret) come back only for a user who may manage the integration. Polls every
 * 5 seconds while a QR is shown or the channel is starting, every minute otherwise.
 */
export function WhatsAppPairingCard({ integration, baseUrl }: { integration: Integration; baseUrl: string }) {
  const { t_i18n } = useFormatter();
  const { data, isLoading } = useWhatsAppPairing(integration.id);

  const labels: Record<WhatsAppPairingState, string> = {
    connected: t_i18n("Paired"),
    qr: t_i18n("Waiting for a scan"),
    starting: t_i18n("Starting"),
    sync_error: t_i18n("Out of sync"),
    banned: t_i18n("Banned"),
    unknown: t_i18n("Unknown"),
  };
  const hints: Record<WhatsAppPairingState, string> = {
    connected: t_i18n("The number is paired and online."),
    qr: t_i18n("On the phone of the dedicated number, open WhatsApp > Settings > Linked devices > Link a device and scan this code."),
    starting: t_i18n("The gateway channel is starting. This card refreshes on its own."),
    sync_error: t_i18n("The gateway lost sync with the phone. Keep the phone online; pair again if it stays."),
    banned: t_i18n("WhatsApp banned this number. Pair another, dedicated number."),
    unknown: t_i18n("The gateway did not report a pairing state."),
  };
  // The backend answers a code; the raw English detail only goes in the tooltip.
  const errors: Record<WhatsAppPairingErrorCode, string> = {
    gateway_unreachable: t_i18n("The gateway could not be reached. Check its base URL and that the service is up."),
    no_channel_token: t_i18n("No channel token yet. Paste the gateway's channel token into API key."),
    token_unreadable: t_i18n("The channel token could not be read. Save it again in API key."),
    unknown_vendor: t_i18n("This gateway vendor is not supported."),
  };

  const state = data?.state ?? "unknown";
  const errorText = data?.error
    ? data.error_code
      ? errors[data.error_code]
      : t_i18n("The gateway could not be read.")
    : null;
  // A viewer is never sent the QR: the scan instruction is for a manager only.
  const hint =
    state === "qr" && !data?.qr && !data?.can_manage
      ? t_i18n("The number is waiting to be paired. A user who can manage this integration scans the QR code here.")
      : hints[state];

  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="flex flex-wrap items-center gap-2 content-base">
          <Smartphone className="h-4 w-4 text-accent" />
          {t_i18n("WhatsApp pairing")}
          {data && <Badge variant={BADGE[state]}>{labels[state]}</Badge>}
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-4 content-base">
        {isLoading && (
          <p className="flex items-center gap-2 content-compact text-default-secondary">
            <Spinner size="sm" tone="inherit" />
            {t_i18n("Reading the gateway...")}
          </p>
        )}

        {data && (
          <>
            {errorText ? (
              <div className="rounded-sm bg-feedback-alert-primary/5 p-3 flex items-start gap-2.5">
                <AlertTriangle className="h-3.5 w-3.5 text-feedback-warning-primary shrink-0 mt-0.5" />
                <p
                  className="content-compact text-feedback-warning-primary leading-relaxed break-words"
                  title={data.error ?? undefined}
                >
                  {errorText}
                </p>
              </div>
            ) : (
              <p className="content-compact text-default-secondary leading-relaxed">{hint}</p>
            )}

            {state === "qr" && data.qr && (
              <div className="flex flex-col items-center gap-2">
                {/* A QR code is read dark on light in every theme: the quiet zone token stays white. */}
                <img
                  src={data.qr}
                  alt={t_i18n("WhatsApp pairing QR code")}
                  className="h-56 w-56 rounded-sm bg-qr-quiet-zone p-2"
                />
                <p className="flex items-center gap-1.5 content-caption text-default-secondary">
                  <QrCode className="h-3 w-3" />
                  {t_i18n("The code changes every few seconds; this card follows it.")}
                </p>
              </div>
            )}

            <div className="space-y-2">
              <p className="content-compact font-medium">{t_i18n("Gateway webhook URL")}</p>
              {data.webhook_path ? (
                <>
                  <p className="content-compact text-default-secondary leading-relaxed">
                    {t_i18n("Add this URL as the webhook of the gateway channel, with the events messages (POST and PATCH) and groups. It carries the webhook secret: keep it private.")}
                  </p>
                  <CopyableUrl url={`${baseUrl}${data.webhook_path}`} />
                </>
              ) : (
                <p className="content-compact text-default-secondary leading-relaxed">
                  {!data.has_webhook_secret
                    ? t_i18n("Set a webhook secret on this integration first: without one, the gateway's deliveries are refused.")
                    : t_i18n("Only a user who can manage this integration sees the webhook URL: it carries the webhook secret.")}
                </p>
              )}
            </div>
          </>
        )}
      </CardContent>
    </Card>
  );
}
