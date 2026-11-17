// SPDX-License-Identifier: Apache-2.0

import { ExternalLink, Unplug, UserRound } from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { useToast } from "@/components/ui/toast";
import { useConfirm } from "@/components/ui/confirm-dialog";
import { useDeleteMyCredential } from "@/hooks/api/useIntegrationApi";
import { INTEGRATION_STATUS } from "@/lib/constants";
import { formatShortDate } from "@/lib/utils";
import { useFormatter } from "@/i18n";
import type { Integration, ProviderCatalogItem } from "@/types";

/**
 * The requesting person's own account on an OAuth integration shared without
 * its owner's connected account (`credential_mode: "per_user"`): its
 * state, and Authorize (the page's own flow, signing them in for themselves)
 * or Disconnect. Rendered only for someone who acts with their own account there
 * (`uses_own_credential`); the holder of the integration's own account uses
 * the integration's controls.
 */
export function YourAccountCard({
  integration,
  catalogEntry,
  onAuthorize,
  authorizing = false,
}: {
  integration: Integration;
  catalogEntry?: ProviderCatalogItem;
  onAuthorize: () => void;
  authorizing?: boolean;
}) {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const [ConfirmEl, confirm] = useConfirm();
  const disconnect = useDeleteMyCredential();

  const credential = integration.my_credential ?? null;
  const statusKey = credential ? credential.status : "disconnected";
  const statusCfg = INTEGRATION_STATUS[statusKey] ?? INTEGRATION_STATUS.disconnected;
  const StatusIcon = statusCfg.icon;
  const needsReauthorize = credential?.status === "reauthorize";
  const provider = catalogEntry?.name || integration.provider;

  const handleDisconnect = async () => {
    const confirmed = await confirm({
      title: "Disconnect your account?",
      description: "Your chats and assignments will no longer use this integration until you connect your account again.",
      confirmLabel: "Disconnect",
      variant: "danger",
    });
    if (!confirmed) return;
    disconnect.mutate(integration.id, {
      onSuccess: () => toast({ title: t_i18n("Your account is disconnected"), variant: "success" }),
    });
  };

  return (
    <Card>
      {ConfirmEl}
      <CardHeader className="pb-3">
        <CardTitle className="content-base flex items-center gap-2">
          <UserRound className="h-4 w-4 text-accent" />
          {t_i18n("Your account")}
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-3">
        <p className="content-compact text-default-secondary">
          {t_i18n("This integration is shared without its owner's connected account: authorize it with your own. Your chats and assignments act with yours, never with anyone else's.")}
        </p>
        <div className="flex items-center justify-between gap-3">
          <Badge variant={statusCfg.badge} className="content-caption">
            <StatusIcon className="h-2.5 w-2.5 mr-0.5" />
            {t_i18n(statusCfg.label)}
          </Badge>
          {credential?.connected_at && (
            <span className="text-[11px] text-default-secondary">
              {t_i18n("Connected on {date}", { date: formatShortDate(credential.connected_at) })}
            </span>
          )}
        </div>

        <Button className="w-full gap-2" onClick={onAuthorize} disabled={authorizing || !integration.has_client_id}>
          <ExternalLink className="h-4 w-4" />
          {credential && !needsReauthorize
            ? t_i18n("Reconnect your account with {provider}", { provider })
            : t_i18n("Connect your account with {provider}", { provider })}
        </Button>

        {credential && (
          <Button
            variant="ghost"
            size="sm"
            className="gap-1.5 text-feedback-error-primary hover:text-feedback-error-primary hover:bg-feedback-error-primary/10"
            onClick={handleDisconnect}
            disabled={disconnect.isPending}
          >
            <Unplug className="h-3.5 w-3.5" />
            {t_i18n("Disconnect my account")}
          </Button>
        )}
      </CardContent>
    </Card>
  );
}
