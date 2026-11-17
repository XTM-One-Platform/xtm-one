// SPDX-License-Identifier: Apache-2.0

/**
 * The Email signature setting of an integration whose tools send email:
 * whether what it sends is signed, and the signature integration the
 * person's signature is read from - picked automatically when none is chosen
 * (the one bound to the agent alongside, else the only one its user may use).
 * An agent's binding may override both, on the agent's page.
 */
import { PenLine } from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Label } from "@/components/ui/label";
import { SearchableSelect } from "@/components/ui/searchable-select";
import { Switch } from "@/components/ui/switch";
import { useToast } from "@/components/ui/toast";
import { useIntegrationEmailSignature } from "@/hooks/api/useEmailSignatureApi";
import { useUpdateIntegration } from "@/hooks/api/useIntegrationApi";
import { useFormatter } from "@/i18n";
import { NO_SIGNATURE_INTEGRATION_CHOSEN, signatureOptions } from "@/lib/email-signatures";
import type { Integration, IntegrationEmailSignature, UpdateIntegrationRequest } from "@/types";

function statusLine(t_i18n: ReturnType<typeof useFormatter>["t_i18n"], data: IntegrationEmailSignature): string {
  const { resolved } = data;
  if (!data.enabled) return t_i18n("Off: what its tools send carries no signature.");
  if (resolved.integration_name) {
    return data.integration_id
      ? t_i18n("Its tools sign with {name}.", { name: resolved.integration_name })
      : t_i18n("Its tools sign with {name}, picked automatically.", { name: resolved.integration_name });
  }
  if (data.integration_id) return t_i18n("The chosen signature integration is gone: choose another.");
  if (data.signature_integrations.length > 1) {
    return t_i18n("Several signature integrations are available: choose one, or bind one to each agent alongside this integration.");
  }
  return t_i18n("No signature integration is available to you yet: connect one, then choose it here.");
}

export function EmailSignatureCard({ integration, canManage }: { integration: Integration; canManage: boolean }) {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const update = useUpdateIntegration();
  const { data } = useIntegrationEmailSignature(integration.id, !!integration.signature);
  if (!integration.signature || !data) return null;
  const editable = canManage && data.can_manage && !update.isPending;

  const save = (patch: UpdateIntegrationRequest) =>
    update.mutate(
      { id: integration.id, ...patch },
      {
        onSuccess: () => toast({ title: t_i18n("Email signature saved"), variant: "success" }),
        onError: (error) =>
          toast({ title: t_i18n("The email signature could not be saved"), description: error.message, variant: "error" }),
      },
    );
  const automaticLabel = !data.integration_id && data.resolved.integration_name
    ? t_i18n("Automatic ({name})", { name: data.resolved.integration_name })
    : t_i18n("Automatic");

  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="content-base flex items-center gap-2">
          <PenLine className="h-4 w-4 text-accent" />
          {t_i18n("Email signature")}
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-3">
        <div className="flex items-center justify-between gap-3 rounded-sm bg-elevation-hover px-3 py-2">
          <div className="min-w-0">
            <Label htmlFor="email-signature-enabled" className="block text-default-primary">
              {t_i18n("Sign what its tools send")}
            </Label>
            <p id="email-signature-enabled-help" className="content-compact text-default-secondary">
              {t_i18n("New emails, drafts and replies its tools write end with the person's signature.")}
            </p>
          </div>
          <Switch
            id="email-signature-enabled"
            aria-describedby="email-signature-enabled-help"
            checked={data.enabled}
            disabled={!editable}
            onCheckedChange={(checked) => save({ signature_enabled: checked })}
          />
        </div>
        <div className="space-y-1.5">
          <Label htmlFor="email-signature-integration" className="block text-default-primary">
            {t_i18n("Signature integration")}
          </Label>
          <SearchableSelect
            id="email-signature-integration"
            aria-describedby="email-signature-integration-status email-signature-integration-override"
            aria-label={t_i18n("Signature integration")}
            searchable="auto"
            pinned={[{ value: NO_SIGNATURE_INTEGRATION_CHOSEN, label: automaticLabel }]}
            options={signatureOptions(data.signature_integrations, data.integration_id, data.integration_name, t_i18n("Unavailable integration"))}
            value={data.integration_id ?? NO_SIGNATURE_INTEGRATION_CHOSEN}
            disabled={!editable || !data.enabled}
            onValueChange={(value) =>
              save({ signature_integration_id: value === NO_SIGNATURE_INTEGRATION_CHOSEN ? null : value })
            }
          />
        </div>
        <p id="email-signature-integration-status" className="content-compact text-default-secondary">{statusLine(t_i18n, data)}</p>
        <p id="email-signature-integration-override" className="content-compact text-default-secondary">
          {t_i18n("An agent can override this for its binding, in the Email signatures card of its settings.")}
        </p>
      </CardContent>
    </Card>
  );
}
