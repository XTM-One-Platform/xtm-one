// SPDX-License-Identifier: Apache-2.0

/**
 * The switch, run-as, status and reason of a source XTM One provisions in its platform (#2434,
 * dev-docs/ctem-signals.md section 4bis): the OpenAEV listener, the OpenCTI live listener and the
 * OpenCTI PIR playbook. One line per integration of the provider. Turning a source on opens its
 * enable dialog, where the administrator picks the run-as user it provisions as; the source is
 * provisioned at once or refused with the reason, which the dialog shows on the field. Turning it
 * off removes what it provisioned. A reader sees the same lines without controls.
 */
import { useEffect, useId, useState } from "react";
import { Chip } from "@filigran/design-system";
import { Button } from "@/components/ui/button";
import { Dialog, DialogBody, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { userSelectOption } from "@/components/ui/entity-select-options";
import { SearchableSelect } from "@/components/ui/searchable-select";
import { Switch } from "@/components/ui/switch";
import { useToast } from "@/components/ui/toast";
import { useUsers } from "@/hooks/api/useAuthApi";
import {
  type CtemPlatformSource,
  type CtemSignalPathSource,
  useDisableCtemPlatformSource,
  useEnableCtemPlatformSource,
} from "@/hooks/api/useCtemSignalsApi";
import { useFormatter } from "@/i18n";
import {
  PLATFORM_SOURCE_STATUS_LABEL,
  PLATFORM_SOURCE_STATUS_TONE,
  enableRefusalText,
  platformSourceReason,
} from "./platform-sources";

/** The key of one platform source on one integration. */
export function platformSourceKey(source: Pick<CtemPlatformSource, "provider" | "kind" | "integration_id">): string {
  return `${source.provider}:${source.kind}:${source.integration_id}`;
}

/** The run-as user of a source at rest: the name, else the email, else a plain "Not set". */
function useRunAsLabel(userId: string | null): string {
  const { t_i18n } = useFormatter();
  const { data: users } = useUsers(Boolean(userId));
  if (!userId) return t_i18n("Not set");
  const user = users?.find((u) => u.id === userId);
  if (!user) return t_i18n("A specific user");
  return user.full_name?.trim() || user.email;
}

/** The enable dialog of one source on one integration: the run-as user, then Turn on. */
export function PlatformSourceEnableDialog({
  source,
  label,
  onClose,
}: {
  source: CtemPlatformSource | null;
  label: string;
  onClose: () => void;
}) {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const { data: users } = useUsers(Boolean(source));
  const enable = useEnableCtemPlatformSource();
  const [runAs, setRunAs] = useState<string>("");
  const [refusal, setRefusal] = useState<string | null>(null);
  const fieldId = useId();
  const errorId = `${fieldId}-error`;

  useEffect(() => {
    setRunAs(source?.run_as_user_id ?? "");
    setRefusal(null);
  }, [source]);

  const submit = () => {
    if (!source || !runAs) return;
    setRefusal(null);
    enable.mutate(
      { provider: source.provider, kind: source.kind, integration_id: source.integration_id, run_as_user_id: runAs },
      {
        onSuccess: () => {
          toast({ title: t_i18n("Source turned on"), variant: "success" });
          onClose();
        },
        onError: (err) => setRefusal(enableRefusalText(err, t_i18n) ?? t_i18n("The source could not be turned on.")),
      },
    );
  };

  return (
    <Dialog open={!!source} onOpenChange={(next) => !next && onClose()}>
      <DialogContent size="md" data-testid="ctem-platform-source-enable">
        <DialogHeader>
          <DialogTitle>{t_i18n("Turn on {source}", { source: label })}</DialogTitle>
          <DialogDescription>
            {t_i18n("XTM One creates the objects that deliver this source in {integration}, as the user you pick. They stay until you turn the source off.", {
              integration: source?.integration_name ?? "",
            })}
          </DialogDescription>
        </DialogHeader>
        <DialogBody className="space-y-1.5">
          <label htmlFor={fieldId} className="content-compact font-medium">
            {t_i18n("Run as")}
          </label>
          <SearchableSelect
            id={fieldId}
            aria-label={t_i18n("Run as")}
            aria-invalid={refusal ? true : undefined}
            aria-describedby={refusal ? errorId : undefined}
            options={(users ?? []).filter((u) => u.is_active).map((u) => userSelectOption(u))}
            value={runAs}
            onValueChange={(value) => {
              setRunAs(value);
              setRefusal(null);
            }}
            disabled={enable.isPending}
            placeholder={t_i18n("Search users...")}
          />
          <p className="content-caption text-default-secondary">
            {t_i18n("Pick a user with a connected integration to this platform and the rights to create its triggers.")}
          </p>
          {refusal && (
            <p id={errorId} role="alert" className="content-compact text-feedback-error-primary" data-testid="ctem-platform-source-refusal">
              {refusal}
            </p>
          )}
        </DialogBody>
        <DialogFooter>
          <Button variant="ghost" onClick={onClose} disabled={enable.isPending}>
            {t_i18n("Cancel")}
          </Button>
          <Button onClick={submit} disabled={!runAs || enable.isPending} data-testid="ctem-platform-source-enable-submit">
            {t_i18n("Turn on")}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function PlatformSourceLine({
  source,
  isAdmin,
  onEnable,
}: {
  source: CtemPlatformSource;
  isAdmin: boolean;
  onEnable: (source: CtemPlatformSource) => void;
}) {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const disable = useDisableCtemPlatformSource();
  const runAs = useRunAsLabel(source.run_as_user_id);
  const reason = platformSourceReason(source, t_i18n);
  const toggle = (on: boolean) => {
    if (on) return onEnable(source);
    if (!source.id) return;
    disable.mutate(source.id, {
      onSuccess: () => toast({ title: t_i18n("Source turned off"), variant: "success" }),
      onError: (err) => toast({ title: t_i18n("The source could not be turned off"), description: err.message, variant: "error" }),
    });
  };
  return (
    <li className="flex flex-col gap-1 content-compact" data-testid="ctem-platform-source" data-status={source.status} data-integration={source.integration_id}>
      <div className="flex min-w-0 items-center gap-2">
        <span className="min-w-0 truncate" title={source.integration_name}>
          {source.integration_name}
        </span>
        <Chip
          label={t_i18n(PLATFORM_SOURCE_STATUS_LABEL[source.status])}
          severity={PLATFORM_SOURCE_STATUS_TONE[source.status]}
          className="shrink-0"
          data-testid="ctem-platform-source-status"
        />
        <Switch
          className="ml-auto shrink-0"
          checked={source.enabled}
          disabled={!isAdmin || disable.isPending || !source.integration_connected}
          onCheckedChange={toggle}
          aria-label={t_i18n("Source on {integration}", { integration: source.integration_name })}
          data-testid="ctem-platform-source-switch"
        />
      </div>
      {source.enabled && (
        <span className="text-default-secondary" data-testid="ctem-platform-source-run-as">
          {t_i18n("Runs as: {user}", { user: runAs })}
        </span>
      )}
      {!source.integration_connected && <span className="text-default-secondary">{t_i18n("The integration is not connected.")}</span>}
      {reason && (
        <span className="text-feedback-error-primary" data-testid="ctem-platform-source-reason">
          {reason}
        </span>
      )}
    </li>
  );
}

/** The platform sources of one path source, one line per integration of its provider. */
export function PlatformSourceControls({
  source,
  platformSources,
  isAdmin,
  onEnable,
}: {
  source: Pick<CtemSignalPathSource, "provider" | "kind">;
  platformSources: readonly CtemPlatformSource[];
  isAdmin: boolean;
  onEnable: (source: CtemPlatformSource) => void;
}) {
  const { t_i18n } = useFormatter();
  const own = platformSources.filter((s) => s.provider === source.provider && s.kind === source.kind);
  return (
    <div className="flex flex-col gap-1.5" data-testid="ctem-platform-sources">
      <p className="content-compact text-default-secondary">
        {t_i18n("XTM One provisions this source in its platform when you turn it on, as the run-as user you pick. Fleets and agents never turn it off.")}
      </p>
      {own.length > 0 && (
        <ul className="flex flex-col gap-2">
          {own.map((s) => (
            <PlatformSourceLine key={platformSourceKey(s)} source={s} isAdmin={isAdmin} onEnable={onEnable} />
          ))}
        </ul>
      )}
    </div>
  );
}
