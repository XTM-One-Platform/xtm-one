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
import { Chip, Icon } from "@filigran/design-system";
import { Button } from "@/components/ui/button";
import { Dialog, DialogBody, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { userSelectOption } from "@/components/ui/entity-select-options";
import { SearchableSelect } from "@/components/ui/searchable-select";
import { SettingsField, settingsFieldHintId } from "@/components/ui/settings-form";
import { Switch } from "@/components/ui/switch";
import { useToast } from "@/components/ui/toast";
import { Tip } from "@/components/ui/tooltip";
import { useUsers } from "@/hooks/api/useAuthApi";
import {
  type CtemPlatformSource,
  type CtemSignalPathSource,
  useDisableCtemPlatformSource,
  useEnableCtemPlatformSource,
} from "@/hooks/api/useCtemSignalsApi";
import { useFormatter } from "@/i18n";
import {
  PLATFORM_OBJECT_OPEN_LABEL,
  PLATFORM_SOURCE_STATUS_LABEL,
  PLATFORM_SOURCE_STATUS_TONE,
  enableRefusalText,
  platformObjectLinks,
  platformSourceReason,
} from "./platform-sources";

/** The platform's name on an Open in label. */
const PLATFORM_NAME: Record<string, string> = { opencti: "OpenCTI", openaev: "OpenAEV", opencrq: "OpenCRQ" };

/**
 * One icon button per object the source holds in its platform (#2583 point 2): opens it there in a new tab.
 * Objects that open the same page (several triggers in one list) share one button.
 */
function PlatformObjectLinks({ source }: { source: CtemPlatformSource }) {
  const { t_i18n } = useFormatter();
  const seen = new Set<string>();
  const links = platformObjectLinks(source).filter((link) => !seen.has(link.href) && seen.add(link.href));
  if (links.length === 0) return null;
  const platform = PLATFORM_NAME[source.provider] ?? source.provider;
  return (
    <span className="flex shrink-0 items-center" data-testid="ctem-platform-source-objects">
      {links.map((link) => {
        const label = t_i18n(PLATFORM_OBJECT_OPEN_LABEL[link.kind], { platform });
        return (
          <Tip key={link.href} content={label}>
            <Button asChild variant="ghost" size="icon" className="size-7">
              <a href={link.href} target="_blank" rel="noopener noreferrer" aria-label={label} data-testid="ctem-platform-source-object-link" data-kind={link.kind}>
                <Icon name="external-link" size={16} aria-hidden />
              </a>
            </Button>
          </Tip>
        );
      })}
    </span>
  );
}

/** The key of one platform source on one integration. */
function platformSourceKey(source: Pick<CtemPlatformSource, "provider" | "kind" | "integration_id">): string {
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

/** The run-as users of a path table row (#2506), by name: one, or the first and how many more. */
export function RunAsName({ ids }: { ids: string }) {
  const [first, ...rest] = ids.split(",");
  const name = useRunAsLabel(first);
  return (
    <span className="truncate" data-testid="ctem-path-source-run-as">
      {rest.length > 0 ? `${name} +${rest.length}` : name}
    </span>
  );
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
        <DialogBody>
          {/* The run-as is the only field and Turn on needs it: marked required, its info bubble says who it is (#2442 UX r1 B2). */}
          <SettingsField
            id={fieldId}
            label={t_i18n("Run as")}
            required
            info={t_i18n(
              "The XTM One user this source provisions as. XTM One uses that user's {integration} connection to create the objects that deliver this source (a notifier and its triggers, or a playbook). If that user is deactivated or loses the connection, the source turns to Error and stops delivering until you pick another user.",
              { integration: source?.integration_name ?? "" },
            )}
            helper={t_i18n("Pick a user with a connected integration to this platform and the rights to create its triggers.")}
            error={refusal ? <span data-testid="ctem-platform-source-refusal">{refusal}</span> : undefined}
          >
            <SearchableSelect
              id={fieldId}
              aria-label={t_i18n("Run as")}
              aria-invalid={refusal ? true : undefined}
              aria-describedby={settingsFieldHintId(fieldId)}
              options={(users ?? []).filter((u) => u.is_active).map((u) => userSelectOption(u))}
              value={runAs}
              onValueChange={(value) => {
                setRunAs(value);
                setRefusal(null);
              }}
              disabled={enable.isPending}
              placeholder={t_i18n("Search users...")}
            />
          </SettingsField>
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
        <span className="ml-auto" />
        {source.enabled && <PlatformObjectLinks source={source} />}
        <Switch
          className="shrink-0"
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
