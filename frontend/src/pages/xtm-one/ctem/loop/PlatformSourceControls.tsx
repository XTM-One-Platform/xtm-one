// SPDX-License-Identifier: Apache-2.0

/**
 * The switch, run-as, status and reason of a source XTM One provisions in its platform (#2434,
 * dev-docs/ctem-signals.md section 4bis): the OpenAEV listener, the OpenCTI live listener and the
 * OpenCTI PIR playbook. One line per integration of the provider. Turning a source on opens its
 * enable dialog, where the administrator picks the run-as user it provisions as; the source is
 * provisioned at once or refused with the reason, which the dialog shows on the field. Turning it
 * off removes what it provisioned. A reader sees the same lines without controls.
 */
import { type ReactNode, useEffect, useId, useState } from "react";
import { Chip, Icon } from "@filigran/design-system";
import { isRunAsModule, ModuleRunAsField } from "@/components/module-run-as/ModuleRunAsField";
import { Button } from "@/components/ui/button";
import { Dialog, DialogBody, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { userSelectOption } from "@/components/ui/entity-select-options";
import { SearchableSelect } from "@/components/ui/searchable-select";
import { SettingsField, settingsFieldHintId } from "@/components/ui/settings-form";
import { useToast } from "@/components/ui/toast";
import { Tip } from "@/components/ui/tooltip";
import { useUsers } from "@/hooks/api/useAuthApi";
import {
  type CtemLegacyOpenctiReport,
  type CtemPlatformSource,
  type CtemPlatformSourceObject,
  type CtemSignalPathSource,
  useDisableCtemPlatformSource,
  useEnableCtemPlatformSource,
  useRemoveLegacyOpenctiObjects,
} from "@/hooks/api/useCtemSignalsApi";
import { useFormatter } from "@/i18n";
import { formatList } from "@/lib/format-list";
import { LabelledSwitch } from "./LabelledSwitch";
import {
  PLATFORM_NAME,
  PLATFORM_OBJECT_COUNT_TEXT,
  PLATFORM_OBJECT_KIND_ONE,
  PLATFORM_OBJECT_KIND_TEXT,
  PLATFORM_OBJECT_OPEN_LABEL,
  PLATFORM_SOURCE_STATUS_LABEL,
  PLATFORM_SOURCE_STATUS_TONE,
  enableRefusalText,
  legacyCleanupCount,
  moduleEventText,
  platformObjectHref,
  platformObjectLinks,
  sourceReasonLine,
} from "./platform-sources";

/**
 * One labelled button per object the source holds in its platform (#2583 point 2): opens it there in a new tab.
 * Objects that open the same page (several triggers in one list) share one button. The kind is written on the
 * button, never an icon alone, so two links of one row never look the same (UX advisory A1 of #2592); two links
 * of one kind (the PIR playbooks of each path) also carry the path they serve (#2627).
 */
function PlatformObjectLinks({ source }: { source: CtemPlatformSource }) {
  const { t_i18n } = useFormatter();
  const seen = new Set<string>();
  const links = platformObjectLinks(source).filter((link) => !seen.has(link.href) && seen.add(link.href));
  if (links.length === 0) return null;
  const platform = PLATFORM_NAME[source.provider] ?? source.provider;
  const perKind = links.reduce<Record<string, number>>((n, link) => ({ ...n, [link.kind]: (n[link.kind] ?? 0) + 1 }), {});
  return (
    // UX round 2 B1: -ml-2 cancels the ghost sm padding, so the first action starts on the value column edge.
    <span className="-ml-2 flex min-w-0 flex-wrap items-center gap-2" data-testid="ctem-platform-source-objects">
      {links.map((link) => {
        const path = perKind[link.kind] > 1 && link.pathLabel ? t_i18n(link.pathLabel) : null;
        const open = t_i18n(PLATFORM_OBJECT_OPEN_LABEL[link.kind], { platform });
        const kind = t_i18n(PLATFORM_OBJECT_KIND_TEXT[link.kind]);
        const label = path ? t_i18n("{label}: {value}", { label: open, value: path }) : open;
        return (
          <Tip key={link.href} content={label}>
            <Button asChild variant="ghost" size="sm" className="shrink-0">
              <a href={link.href} target="_blank" rel="noopener noreferrer" aria-label={label} data-testid="ctem-platform-source-object-link" data-kind={link.kind}>
                <span>{path ? t_i18n("{label}: {value}", { label: kind, value: path }) : kind}</span>
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
  const { data: users } = useUsers(Boolean(source) && !isRunAsModule(source?.provider));
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
          <DialogTitle>{source?.enabled ? t_i18n("Change the run-as user of {source}", { source: label }) : t_i18n("Turn on {source}", { source: label })}</DialogTitle>
          <DialogDescription>
            {t_i18n("XTM One creates the objects that deliver this source in {integration}, as the user you pick. They stay until you turn the source off.", {
              integration: source?.integration_name ?? "",
            })}
          </DialogDescription>
        </DialogHeader>
        <DialogBody>
          {/* #2650: who XTM One acts as in the module, asked before any object is created there; the caller by default. */}
          {isRunAsModule(source?.provider) ? (
            <ModuleRunAsField
              module={source.provider}
              value={runAs}
              onChange={(value) => {
                setRunAs(value);
                setRefusal(null);
              }}
              active={Boolean(source)}
              disabled={enable.isPending}
              helper={t_i18n("XTM One creates the notifier and its triggers, or the playbook, with this user's connection.")}
              error={refusal ? <span data-testid="ctem-platform-source-refusal">{refusal}</span> : undefined}
            />
          ) : (
            <SettingsField
              id={fieldId}
              label={t_i18n("Run as")}
              required
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
          )}
        </DialogBody>
        <DialogFooter>
          <Button variant="ghost" onClick={onClose} disabled={enable.isPending}>
            {t_i18n("Cancel")}
          </Button>
          <Button onClick={submit} disabled={!runAs || enable.isPending} data-testid="ctem-platform-source-enable-submit">
            {source?.enabled ? t_i18n("Save") : t_i18n("Turn on")}
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
  // The reason ended by its full stop, then its fix in the reader's language (UX round 1 on #2653, B2).
  const reason = sourceReasonLine(source, null, t_i18n);
  const toggle = (on: boolean) => {
    if (on) return onEnable(source);
    if (!source.id) return;
    disable.mutate(source.id, {
      onSuccess: () => toast({ title: t_i18n("Source turned off"), variant: "success" }),
      onError: (err) => toast({ title: t_i18n("The source could not be turned off"), description: err.message, variant: "error" }),
    });
  };
  // One aligned row (#2740): name and state, then the labelled switch at the row's end; under them the run-as and
  // the Open in actions as key-value lines of one grid, so no link floats in a sentence.
  return (
    <li
      className="grid grid-cols-[minmax(0,1fr)_auto] items-center gap-x-4 gap-y-1 py-2 content-compact [&+&]:border-t [&+&]:border-elevation-subtle-soft"
      data-testid="ctem-platform-source"
      data-status={source.status}
      data-integration={source.integration_id}
    >
      <span className="flex min-w-0 items-center gap-2">
        <Tip content={source.integration_name}>
          <span className="min-w-0 truncate font-medium" data-testid="ctem-platform-source-name">
            {source.integration_name}
          </span>
        </Tip>
        <Chip
          label={t_i18n(PLATFORM_SOURCE_STATUS_LABEL[source.status])}
          severity={PLATFORM_SOURCE_STATUS_TONE[source.status]}
          className="shrink-0"
          data-testid="ctem-platform-source-status"
        />
      </span>
      {/* UX round 1 B3: the one LabelledSwitch of the panel, label next to its switch at the row's end. */}
      <LabelledSwitch
        className="justify-self-end"
        label={t_i18n("Provisioned")}
        checked={source.enabled}
        disabled={!isAdmin || disable.isPending || !source.integration_connected}
        onCheckedChange={toggle}
        ariaLabel={t_i18n("Source on {integration}", { integration: source.integration_name })}
        testId="ctem-platform-source-switch"
      />
      {source.enabled && (
        <dl className="col-span-2 grid grid-cols-[minmax(0,8rem)_minmax(0,1fr)] items-center gap-x-3 gap-y-1">
          <dt className="text-default-secondary">{t_i18n("Runs as")}</dt>
          <dd className="flex min-w-0 items-center gap-2">
            <span className="min-w-0 truncate" data-testid="ctem-platform-source-run-as">
              {runAs}
            </span>
            {/* #2650: the run-as stays changeable; the source is provisioned again as the new user. */}
            {isAdmin && source.integration_connected && (
              <Button
                variant="ghost"
                size="sm"
                className="shrink-0"
                onClick={() => onEnable(source)}
                aria-label={t_i18n("Change the run-as user of {integration}", { integration: source.integration_name })}
                data-testid="ctem-platform-source-run-as-change"
              >
                {t_i18n("Change")}
              </Button>
            )}
          </dd>
          {platformObjectLinks(source).length > 0 && (
            <>
              <dt className="text-default-secondary">{t_i18n("Open in {platform}", { platform: PLATFORM_NAME[source.provider] ?? source.provider })}</dt>
              <dd className="min-w-0">
                <PlatformObjectLinks source={source} />
              </dd>
            </>
          )}
        </dl>
      )}
      {!source.integration_connected && <span className="col-span-2 text-default-secondary">{t_i18n("The integration is not connected.")}</span>}
      {reason && (
        <span className="col-span-2 text-feedback-error-primary" data-testid="ctem-platform-source-reason">
          {reason}
        </span>
      )}
    </li>
  );
}

/** How many objects of each kind, in words: "1 notifier and 8 triggers" (#2740). */
function useObjectSummary(objects: readonly CtemPlatformSourceObject[]): string {
  const { t_i18n, locale } = useFormatter();
  const counts = new Map<CtemPlatformSourceObject["kind"], number>();
  for (const o of objects) counts.set(o.kind, (counts.get(o.kind) ?? 0) + 1);
  return formatList(
    [...counts.entries()].map(([kind, count]) => t_i18n(PLATFORM_OBJECT_COUNT_TEXT[kind], { count })),
    locale,
  );
}

/**
 * One collapsible group header (UX round 2 B2 on #2743): a muted summary line, then the same ghost Show all / Hide
 * toggle, so the source's own objects and the fleets' objects read with one pattern.
 */
function CollapsibleSummary({
  lead,
  summary,
  open,
  onToggle,
  collapsible = true,
  summaryTestId,
  toggleTestId,
}: {
  lead?: ReactNode;
  summary: string;
  open: boolean;
  onToggle: () => void;
  collapsible?: boolean;
  summaryTestId: string;
  toggleTestId: string;
}) {
  const { t_i18n } = useFormatter();
  return (
    <div className="flex min-w-0 items-center gap-2">
      {lead}
      <span className="min-w-0 text-default-secondary" data-testid={summaryTestId}>
        {summary}
      </span>
      {collapsible && (
        <Button variant="ghost" size="sm" className="shrink-0" aria-expanded={open} onClick={onToggle} data-testid={toggleTestId}>
          <Icon name={open ? "chevron-up" : "chevron-down"} size={16} aria-hidden />
          {open ? t_i18n("Hide") : t_i18n("Show all")}
        </Button>
      )}
    </div>
  );
}

/** The objects one integration's source holds, then the fleets' objects on it, collapsed apart (#2740). */
function PlatformObjectsGroup({ line, named }: { line: CtemPlatformSource; named: boolean }) {
  const { t_i18n, locale } = useFormatter();
  const own = line.objects ?? [];
  const others = line.fleet_objects ?? [];
  const [showAll, setShowAll] = useState(own.length <= 3);
  const [showOthers, setShowOthers] = useState(false);
  const summary = useObjectSummary(own);
  const platform = PLATFORM_NAME[line.provider] ?? line.provider;
  // One note per module event other owners also listen to: each owner gets its own delivery and run (#2640).
  const shared = new Map<string, string[]>();
  for (const o of own) if (o.event && (o.shared_with ?? []).length > 0) shared.set(o.event, o.shared_with ?? []);
  return (
    <div className="flex min-w-0 flex-col gap-1.5 content-compact" data-testid="ctem-platform-source-object-group" data-integration={line.integration_id}>
      {own.length > 0 && (
        <CollapsibleSummary
          lead={named ? <span className="min-w-0 truncate font-medium">{line.integration_name}</span> : undefined}
          summary={t_i18n("{summary}, owned by Signals", { summary })}
          open={showAll}
          onToggle={() => setShowAll((v) => !v)}
          collapsible={own.length > 3}
          summaryTestId="ctem-platform-source-object-summary"
          toggleTestId="ctem-platform-source-objects-toggle"
        />
      )}
      {own.length > 0 && showAll && (
        <ul className="flex min-w-0 flex-col" data-testid="ctem-platform-source-object-names">
          {own.map((o) => (
            <PlatformObjectRow key={`${o.kind}:${o.id}`} object={o} source={line} />
          ))}
        </ul>
      )}
      {others.length > 0 && (
        <div className="flex min-w-0 flex-col gap-1" data-testid="ctem-platform-source-fleet-group">
          <CollapsibleSummary
            summary={t_i18n("Other XTM One objects in {platform}, not managed here: {count, plural, one {# object} other {# objects}}", {
              platform,
              count: others.length,
            })}
            open={showOthers}
            onToggle={() => setShowOthers((v) => !v)}
            summaryTestId="ctem-platform-source-fleet-summary"
            toggleTestId="ctem-platform-source-fleet-toggle"
          />
          {showOthers && (
            <>
              {[...shared.entries()].map(([event, owners]) => (
                <span key={event} className="text-default-secondary" data-testid="ctem-platform-source-object-shared">
                  {t_i18n(
                    "{count, plural, one {{owners} also listens to the \"{event}\" event with its own trigger} other {{owners} also listen to the \"{event}\" event, each with its own trigger}}: each owner gets its own delivery and starts its own run, as its own run-as user.",
                    { count: owners.length, owners: formatList(owners, locale), event: moduleEventText(t_i18n, event) },
                  )}
                </span>
              ))}
              <ul className="flex min-w-0 flex-col" data-testid="ctem-platform-source-fleet-objects">
                {others.map((o) => (
                  <PlatformObjectRow key={`${o.kind}:${o.id}`} object={o} source={line} />
                ))}
              </ul>
            </>
          )}
        </div>
      )}
    </div>
  );
}

/**
 * One object a source or a fleet holds in its platform (#2640, #2740), on one aligned row: its kind, what tells it
 * apart (the module event a trigger listens to, the path a playbook serves, else its name), its owner and its Open
 * in link. The full name stays in the row's tooltip.
 */
function PlatformObjectRow({ object, source }: { object: CtemPlatformSourceObject; source: CtemPlatformSource }) {
  const { t_i18n } = useFormatter();
  const name = object.name ?? "";
  const what = object.event ? moduleEventText(t_i18n, object.event) : object.path_label ? t_i18n(object.path_label) : name;
  const href = platformObjectHref(source, object);
  const platform = PLATFORM_NAME[source.provider] ?? source.provider;
  return (
    <li
      className="grid min-h-8 grid-cols-[minmax(0,6rem)_minmax(0,1fr)_minmax(0,12rem)_5rem] items-center gap-x-3 border-t border-elevation-subtle-soft"
      data-testid="ctem-platform-source-object"
      data-kind={object.kind}
      data-owner={object.owner ?? ""}
    >
      <span className="truncate text-default-secondary">{t_i18n(PLATFORM_OBJECT_KIND_ONE[object.kind])}</span>
      <Tip content={name || what}>
        <span className="min-w-0 truncate" data-testid="ctem-platform-source-object-what">
          {what}
        </span>
      </Tip>
      <span className="min-w-0 truncate" data-testid="ctem-platform-source-object-owner">
        {object.owner ?? ""}
      </span>
      <span className="justify-self-end">
        {href && (
          <Button asChild variant="ghost" size="sm">
            <a
              href={href}
              target="_blank"
              rel="noopener noreferrer"
              aria-label={t_i18n(PLATFORM_OBJECT_OPEN_LABEL[object.kind], { platform })}
              data-testid="ctem-platform-source-object-open"
            >
              <span>{t_i18n("Open")}</span>
              <Icon name="external-link" size={16} aria-hidden />
            </a>
          </Button>
        )}
      </span>
    </li>
  );
}

/** One integration's line of the legacy objects report: what was found, removed or why it could not be read. */
function LegacyReportLine({ report, names, dryRun }: { report: CtemLegacyOpenctiReport; names: Record<string, string>; dryRun: boolean }) {
  const { t_i18n } = useFormatter();
  const objects = report.objects ?? [];
  const integration = names[report.integration_id] ?? report.integration_id;
  return (
    <li className="flex min-w-0 flex-col gap-1" data-testid="ctem-legacy-report">
      <span className="font-medium">{integration}</span>
      {report.error ? (
        <span className="text-feedback-error-primary">{report.error}</span>
      ) : objects.length === 0 ? (
        <span className="text-default-secondary">{t_i18n("No legacy object left.")}</span>
      ) : (
        <>
          <span className="text-default-secondary">
            {dryRun
              ? t_i18n("{count, plural, one {# object would be removed:} other {# objects would be removed:}}", { count: objects.length })
              : t_i18n("{count, plural, =0 {No object removed.} one {# object removed.} other {# objects removed.}}", { count: report.removed ?? 0 })}
          </span>
          <ul className="flex min-w-0 flex-col">
            {objects.map((o) => (
              <li key={o.id} className="min-w-0 truncate" title={o.name}>
                {o.name}
              </li>
            ))}
          </ul>
          {(report.errors ?? []).map((e) => (
            <span key={e} className="text-feedback-error-primary">
              {e}
            </span>
          ))}
        </>
      )}
    </li>
  );
}

/**
 * Admin "Remove legacy OpenCTI objects" (#2583 point 4): the dialog lists first (dry run) what the former
 * Threat response holders left in OpenCTI, per integration a Signals source is on for; Remove deletes them.
 */
function LegacyOpenctiCleanup({ pending, integrationNames }: { pending: number; integrationNames: Record<string, string> }) {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const run = useRemoveLegacyOpenctiObjects();
  const [open, setOpen] = useState(false);
  const [removed, setRemoved] = useState(false);
  const report = run.data?.integrations ?? [];
  const found = report.reduce((n, r) => n + (r.objects?.length ?? 0), 0);
  // Nothing left to remove (#2627): no destructive action, the dialog only closes.
  const nothingLeft = removed || (!run.isPending && run.data !== undefined && found === 0);
  // XTM One holds no legacy object any more: the action hides, once its dialog is closed.
  if (pending === 0 && !open) return null;
  const start = () => {
    setOpen(true);
    setRemoved(false);
    run.mutate({ dryRun: true });
  };
  const remove = () =>
    run.mutate(
      { dryRun: false },
      {
        onSuccess: () => {
          setRemoved(true);
          toast({ title: t_i18n("Legacy OpenCTI objects removed"), variant: "success" });
        },
        onError: (err) => toast({ title: t_i18n("The legacy OpenCTI objects could not be removed"), description: err.message, variant: "error" }),
      },
    );
  return (
    <>
      <Button variant="ghost" size="sm" className="self-start" onClick={start} data-testid="ctem-legacy-cleanup">
        {t_i18n("Remove legacy OpenCTI objects ({count})", { count: pending })}
      </Button>
      <Dialog open={open} onOpenChange={(next) => !next && !run.isPending && setOpen(false)}>
        <DialogContent size="md" data-testid="ctem-legacy-cleanup-dialog">
          <DialogHeader>
            <DialogTitle>{t_i18n("Remove legacy OpenCTI objects")}</DialogTitle>
            <DialogDescription>
              {t_i18n("The triggers, playbooks and notifiers the former Threat response automations left in OpenCTI. The Signals sources replace them; nothing else is touched.")}
            </DialogDescription>
          </DialogHeader>
          <DialogBody>
            {run.isPending && !run.data ? (
              <span className="text-default-secondary">{t_i18n("Reading OpenCTI...")}</span>
            ) : run.isError && !run.data ? (
              <span className="text-feedback-error-primary">{run.error.message}</span>
            ) : report.length === 0 ? (
              <span className="text-default-secondary">{t_i18n("No OpenCTI integration has a Signals source on.")}</span>
            ) : (
              <ul className="flex flex-col gap-3 content-compact">
                {report.map((r) => (
                  <LegacyReportLine key={r.integration_id} report={r} names={integrationNames} dryRun={!removed} />
                ))}
              </ul>
            )}
          </DialogBody>
          <DialogFooter>
            <Button variant="ghost" onClick={() => setOpen(false)} disabled={run.isPending}>
              {nothingLeft ? t_i18n("Close") : t_i18n("Cancel")}
            </Button>
            {!nothingLeft && (
              <Button variant="destructive" onClick={remove} disabled={run.isPending || found === 0} data-testid="ctem-legacy-cleanup-confirm">
                {t_i18n("Remove {count, plural, one {# object} other {# objects}}", { count: found })}
              </Button>
            )}
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}

/**
 * The Integrations section of a source XTM One provisions (#2740): one aligned row per integration of its
 * provider, its state, run-as, Open in actions and labelled switch; the legacy cleanup action under them.
 */
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
  const own = platformSources.filter((s) => s.provider === source.provider && s.kind === source.kind);
  return (
    <div className="flex flex-col gap-1.5" data-testid="ctem-platform-sources">
      {own.length > 0 && (
        <ul className="flex flex-col">
          {own.map((s) => (
            <PlatformSourceLine key={platformSourceKey(s)} source={s} isAdmin={isAdmin} onEnable={onEnable} />
          ))}
        </ul>
      )}
      {isAdmin && source.provider === "opencti" && source.kind === "listener" && (
        <LegacyOpenctiCleanup
          pending={legacyCleanupCount(source, own)}
          integrationNames={Object.fromEntries(own.map((s) => [s.integration_id, s.integration_name]))}
        />
      )}
    </div>
  );
}

/**
 * The Objects section of a source XTM One provisions (#2740): per enabled integration, the objects it holds in
 * its platform summarized by kind, then one distinguishable row each (what it listens to or serves, its owner,
 * its Open in link), collapsed past three; the fleets' objects on the same integration in their own group.
 */
export function PlatformSourceObjects({
  source,
  platformSources,
}: {
  source: Pick<CtemSignalPathSource, "provider" | "kind">;
  platformSources: readonly CtemPlatformSource[];
}) {
  const lines = platformSources.filter(
    (s) => s.provider === source.provider && s.kind === source.kind && ((s.objects ?? []).length > 0 || (s.fleet_objects ?? []).length > 0),
  );
  if (lines.length === 0) return null;
  return (
    <div className="flex flex-col gap-3" data-testid="ctem-platform-source-blast-radius">
      {lines.map((line) => (
        <PlatformObjectsGroup key={platformSourceKey(line)} line={line} named={lines.length > 1} />
      ))}
    </div>
  );
}
