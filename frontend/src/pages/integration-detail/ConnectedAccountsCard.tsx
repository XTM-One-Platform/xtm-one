// SPDX-License-Identifier: Apache-2.0

import { Unplug, Users } from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { CountBadge } from "@/components/ui/count-badge";
import { Button } from "@/components/ui/button";
import { useToast } from "@/components/ui/toast";
import { useConfirm } from "@/components/ui/confirm-dialog";
import { useDeleteUserCredential, useIntegrationCredentials } from "@/hooks/api/useIntegrationApi";
import { INTEGRATION_STATUS } from "@/lib/constants";
import { formatShortDate } from "@/lib/utils";
import { useFormatter } from "@/i18n";
import type { Integration } from "@/types";

/**
 * Who connected their own account to an integration whose people each connect
 * theirs (`credential_mode: "per_user"`), for those who manage it:
 * names and states only - a person's account is theirs, and nobody else reads
 * or uses it - with a Disconnect per person.
 */
export function ConnectedAccountsCard({ integration }: { integration: Integration }) {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const [ConfirmEl, confirm] = useConfirm();
  const { data: people, isLoading } = useIntegrationCredentials(integration.id);
  const disconnect = useDeleteUserCredential();

  const handleDisconnect = async (userId: string) => {
    const confirmed = await confirm({
      title: "Disconnect this person's account?",
      description: "Their chats and assignments will no longer use this integration until they connect their account again.",
      confirmLabel: "Disconnect",
      variant: "danger",
    });
    if (!confirmed) return;
    disconnect.mutate(
      { integrationId: integration.id, userId },
      { onSuccess: () => toast({ title: t_i18n("Account disconnected"), variant: "success" }) },
    );
  };

  const count = people?.length ?? integration.connected_user_count ?? 0;

  return (
    <Card>
      {ConfirmEl}
      <CardHeader className="pb-3">
        <CardTitle className="content-base flex items-center gap-2">
          <Users className="h-4 w-4 text-accent" />
          {t_i18n("People's own accounts")}
          <CountBadge variant="outline">{count}</CountBadge>
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-2">
        <p className="content-compact text-default-secondary">
          {t_i18n("Each person this integration is shared with connects their own account. You see who did, never their credentials.")}
        </p>
        {!isLoading && count === 0 && (
          <p className="content-compact text-default-secondary">{t_i18n("Nobody has connected their own account yet.")}</p>
        )}
        {people?.map((person) => {
          const statusCfg = INTEGRATION_STATUS[person.status] ?? INTEGRATION_STATUS.disconnected;
          const StatusIcon = statusCfg.icon;
          return (
            <div key={person.user_id} className="flex items-center justify-between gap-3 rounded-sm bg-elevation-hover px-3 py-2">
              <div className="min-w-0">
                <p className="content-base truncate">{person.user_name || person.user_email || person.user_id}</p>
                {person.user_name && person.user_email && (
                  <p className="text-[11px] text-default-secondary truncate">{person.user_email}</p>
                )}
              </div>
              <div className="flex items-center gap-2 shrink-0">
                <Badge variant={statusCfg.badge} className="content-caption">
                  <StatusIcon className="h-2.5 w-2.5 mr-0.5" />
                  {t_i18n(statusCfg.label)}
                </Badge>
                {person.connected_at && (
                  <span className="text-[11px] text-default-secondary">{formatShortDate(person.connected_at)}</span>
                )}
                <Button
                  variant="ghost"
                  size="sm"
                  className="h-7 w-7 p-0 text-feedback-error-primary hover:text-feedback-error-primary hover:bg-feedback-error-primary/10"
                  aria-label={t_i18n("Disconnect")}
                  onClick={() => handleDisconnect(person.user_id)}
                  disabled={disconnect.isPending}
                >
                  <Unplug className="h-3.5 w-3.5" />
                </Button>
              </div>
            </div>
          );
        })}
      </CardContent>
    </Card>
  );
}
