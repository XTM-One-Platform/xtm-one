// SPDX-License-Identifier: Apache-2.0

import { useEffect, useState, type ReactNode } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import {
  Bot,
  Code2,
  Globe,
  KeyRound,
  MessageSquare,
  MonitorCog,
  Network,
  Pencil,
  Plug2,
  RefreshCw,
  RotateCcw,
  Server,
  ShieldAlert,
  SlidersHorizontal,
  Wrench,
  type LucideIcon,
} from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { EmptyState } from "@/components/ui/empty-state";
import { EnvLockBadge } from "@/components/ui/env-lock-badge";
import { InfoTip } from "@/components/ui/info-tip";
import { Input } from "@/components/ui/input";
import { ReadOnlyBadge } from "@/components/ui/read-only-badge";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { SettingsGroupTitle, SettingsSaveBar } from "@/components/ui/settings-form";
import { TagInput } from "@/components/ui/tag-input";
import { Skeleton } from "@/components/ui/skeleton";
import { Switch } from "@/components/ui/switch";
import { Tip } from "@/components/ui/tooltip";
import { useToast } from "@/components/ui/toast";
import { useSecurityOptions, useUpdateSecurityOptions } from "@/hooks/api/useSettingsApi";
import { useAuth } from "@/hooks/useAuth";
import { useFormatter } from "@/i18n";
import { cn } from "@/lib/utils";
import type { SecurityOption, SecurityOptionValue } from "@/types";

import { ConfigCode, ConfigText } from "./ConfigText";
import { ScannedToolsField, ScannedToolsSummary } from "./ScannedToolsField";
import {
  SECURITY_OPTION_CHOICE_LABELS,
  SECURITY_OPTION_LIST_EXAMPLES,
  auditFindingPath,
  changedValues,
  displayValue,
  fieldText,
  groupLabel,
  groupOptions,
  intProblem,
  invalidListEntries,
  listProblem,
  lockMessage,
  optionAnchorId,
  optionDescription,
  optionInfo,
  optionLabel,
  parseInteger,
  parseList,
  targetedOptionKey,
} from "./security-options";

// ---------------------------------------------------------------------------
// Settings > Access and security > Security options: platform settings whose
// environment variable, when set, wins and locks the field ("Env" badge, as
// for the General Assistant's env-managed models). Admin-only, like the
// endpoint.
// ---------------------------------------------------------------------------

/**
 * A group about one of the Security audit's categories wears that category's
 * icon. No inherited keys: a group named `constructor` finds nothing here.
 */
const GROUP_ICONS: Readonly<Record<string, LucideIcon | undefined>> = Object.freeze(
  Object.assign(Object.create(null) as Record<string, LucideIcon>, {
    network: Server,
    accounts: KeyRound,
    oauth: Plug2,
    a2a: Network,
    webhooks: Globe,
    widget: MessageSquare,
    agents: Bot,
    runners: MonitorCog,
    features: Wrench,
    sandbox: Code2,
  }),
);

export function SecurityOptionsSection() {
  const { isAdmin } = useAuth();
  const query = useSecurityOptions(isAdmin);
  const { hash } = useLocation();

  if (!isAdmin) return null;
  if (query.isPending) return <OptionsSkeleton />;
  if (!query.data) {
    return <OptionsUnavailable error={query.error} retrying={query.isFetching} onRetry={() => void query.refetch()} />;
  }
  if (query.data.options.length === 0) {
    return (
      <EmptyState
        icon={SlidersHorizontal}
        title="No security options reported"
        description="The platform reported no security option."
      />
    );
  }
  return <OptionsCard options={query.data.options} target={targetedOptionKey(hash)} />;
}

/** The text the number and list fields start from: the value as it would be typed. */
function fieldTexts(options: SecurityOption[]): Record<string, string> {
  return Object.fromEntries(options.map((option) => [option.key, fieldText(option, option.value)]));
}

function OptionsSkeleton() {
  const { t_i18n } = useFormatter();
  return (
    <div className="space-y-6" role="status" aria-label={t_i18n("Loading the security options...")} data-testid="security-options-loading">
      <Skeleton className="h-24 w-full rounded-sm" />
      <Skeleton className="h-72 w-full rounded-sm" />
      <Skeleton className="h-44 w-full rounded-sm" />
    </div>
  );
}

function OptionsUnavailable({ error, retrying, onRetry }: { error: Error | null; retrying: boolean; onRetry: () => void }) {
  return (
    <div data-testid="security-options-error">
      <EmptyState
        icon={ShieldAlert}
        title="The security options could not be loaded"
        description={error?.message || "The platform did not return the security options."}
        actionLabel={retrying ? "Loading..." : "Try again"}
        actionIcon={RefreshCw}
        actionVariant="default"
        onAction={onRetry}
      />
    </div>
  );
}

function OptionsCard({ options, target }: { options: SecurityOption[]; target: string | null }) {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const navigate = useNavigate();
  const { pathname, search } = useLocation();
  const update = useUpdateSecurityOptions();
  // A link to one option's field (`#option-<key>`, the Security audit's) opens
  // the card on that field, ready to change; a locked one is only shown.
  const opensOnTarget = options.some((option) => option.key === target && !option.locked);
  const [editing, setEditing] = useState(opensOnTarget);
  const [draft, setDraft] = useState<Record<string, SecurityOptionValue>>({});
  // What is typed in the number and list fields, kept as typed.
  const [texts, setTexts] = useState<Record<string, string>>(() => (opensOnTarget ? fieldTexts(options) : {}));

  useEffect(() => {
    if (!target) return;
    const row = document.getElementById(optionAnchorId(target));
    if (!row) return;
    row.scrollIntoView({ block: "center" });
    (document.getElementById(rowIds(target).control) ?? row).focus({ preventScroll: true });
  }, [target]);

  const valueOf = (option: SecurityOption): SecurityOptionValue =>
    option.key in draft ? draft[option.key] : option.value;
  const editable = options.some((option) => !option.locked);
  const invalid = options.some(
    (option) =>
      !option.locked && (intProblem(option, valueOf(option)) !== null || listProblem(option, valueOf(option)) !== null),
  );
  const dirty = Object.keys(changedValues(options, draft)).length > 0;

  const startEditing = () => {
    setDraft({});
    setTexts(fieldTexts(options));
    setEditing(true);
  };

  // Done with the option the link pointed at: a reload must not reopen it.
  const stopEditing = () => {
    setEditing(false);
    if (target) navigate({ pathname, search }, { replace: true });
  };

  const setText = (key: string, text: string) => setTexts((current) => ({ ...current, [key]: text }));
  const setValue = (key: string, value: SecurityOptionValue) => setDraft((current) => ({ ...current, [key]: value }));

  const save = () => {
    const values = changedValues(options, draft);
    if (Object.keys(values).length === 0) {
      stopEditing();
      return;
    }
    update.mutate(
      { values },
      {
        onSuccess: stopEditing,
        onError: (error) => toast({ variant: "error", title: t_i18n("Could not save the security options"), description: error.message }),
      },
    );
  };

  const reset = (option: SecurityOption) => {
    setValue(option.key, null);
    setText(option.key, fieldText(option, option.default));
  };

  const shown = (option: SecurityOption, value: SecurityOptionValue) => {
    const text = displayValue(option, value);
    return option.kind === "bool" || option.kind === "choice" || text === "None" ? t_i18n(text) : text;
  };

  const control = (option: SecurityOption, ids: RowIds) => {
    const value = valueOf(option);
    const labelled = { id: ids.control, "aria-labelledby": ids.label, "aria-describedby": ids.description };
    if (option.kind === "tools") {
      return (
        <ScannedToolsField
          labelledBy={ids.label}
          describedBy={ids.description}
          value={Array.isArray(value) ? value : Array.isArray(option.default) ? option.default : []}
          onChange={(next) => setValue(option.key, next)}
        />
      );
    }
    if (option.kind === "bool") {
      return (
        <Switch
          {...labelled}
          checked={Boolean(value ?? option.default)}
          onCheckedChange={(checked) => setValue(option.key, checked)}
        />
      );
    }
    if (option.kind === "choice") {
      return (
        <Select value={String(value ?? option.default)} onValueChange={(choice) => setValue(option.key, choice)}>
          <SelectTrigger {...labelled} className="h-9 w-full sm:w-auto sm:min-w-72 sm:max-w-md">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {option.choices.map((choice) => (
              <SelectItem key={choice} value={choice}>
                {t_i18n((SECURITY_OPTION_CHOICE_LABELS as Record<string, string>)[choice] ?? choice)}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      );
    }
    if (option.kind === "int") {
      const problem = intProblem(option, value);
      return (
        <div className="flex flex-col items-end gap-1">
          <Input
            {...labelled}
            aria-describedby={problem ? `${ids.description} ${ids.problem}` : ids.description}
            type="number"
            className="h-9 w-28"
            min={option.off_value ?? option.minimum ?? undefined}
            max={option.maximum ?? undefined}
            value={texts[option.key] ?? ""}
            onChange={(event) => {
              setText(option.key, event.target.value);
              setValue(option.key, parseInteger(event.target.value));
            }}
            aria-invalid={problem !== null}
          />
          {problem && (
            <span id={ids.problem} className="content-compact text-feedback-error-primary">
              {t_i18n(problem, {
                off: option.off_value ?? 0,
                minimum: option.minimum ?? 0,
                maximum: option.maximum ?? 0,
              })}
            </span>
          )}
        </div>
      );
    }
    // A list is a chip per entry (Sam's form standard: a tag input, never a
    // free-text list). A pasted "a, b" still splits into two chips.
    const entries = Array.isArray(value) ? value : Array.isArray(option.default) ? option.default : [];
    const problem = listProblem(option, value);
    return (
      <div className="flex w-full flex-col gap-1 sm:w-72">
        <TagInput
          aria-labelledby={ids.label}
          aria-describedby={problem ? `${ids.description} ${ids.problem}` : ids.description}
          aria-invalid={problem !== null}
          invalidTags={invalidListEntries(option, value)}
          value={entries}
          placeholder={SECURITY_OPTION_LIST_EXAMPLES[option.kind]}
          preserveCase
          suggestFromPlatformTags={false}
          onChange={(next) => setValue(option.key, [...new Set(parseList(next.join("\n")))])}
        />
        {problem && (
          <span id={ids.problem} className="content-compact text-feedback-error-primary">
            {t_i18n(problem, {
              entries: invalidListEntries(option, value).join(", "),
              example: SECURITY_OPTION_LIST_EXAMPLES[option.kind] ?? "",
            })}
          </span>
        )}
      </div>
    );
  };

  return (
    <Card data-testid="security-options" className="layer-1 bg-elevation-default backdrop-blur-sm">
      <CardHeader className="flex flex-col gap-3 space-y-0 sm:flex-row sm:items-start sm:justify-between">
        <div className="space-y-1.5">
          <CardTitle className="flex items-center gap-2">
            <SlidersHorizontal className="h-4 w-4 text-accent" />
            {t_i18n("Security options")}
          </CardTitle>
          <CardDescription>
            {t_i18n("Platform-wide security switches. An option set by its environment variable is shown read-only: the variable wins until it is removed.")}
          </CardDescription>
        </div>
        {!editing && (
          <div className="flex shrink-0 items-center gap-2">
            <Button
              variant="secondary"
              size="sm"
              className="gap-1.5"
              onClick={startEditing}
              disabled={!editable}
              data-testid="security-options-configure"
            >
              <Pencil className="h-3 w-3" />
              {t_i18n("Configure")}
            </Button>
          </div>
        )}
      </CardHeader>
      <CardContent className="space-y-6">
        {groupOptions(options).map(([group, items]) => (
          <OptionGroup key={group} group={group} count={items.length}>
            {items.map((option) => (
              <OptionRow
                key={option.key}
                option={option}
                targeted={option.key === target}
                value={shown(option, option.value)}
                summary={
                  option.kind === "tools" ? (
                    <ScannedToolsSummary entries={Array.isArray(option.value) ? option.value : []} />
                  ) : null
                }
                defaultValue={shown(option, option.default)}
                control={editing && !option.locked ? (ids) => control(option, ids) : null}
                onReset={editing && !option.locked && option.source === "database" ? () => reset(option) : null}
              />
            ))}
          </OptionGroup>
        ))}
      </CardContent>
      {editing && (
        <SettingsSaveBar dirty={dirty} saving={update.isPending} saveDisabled={invalid} onSave={save} onCancel={stopEditing} />
      )}
    </Card>
  );
}

function OptionGroup({ group, count, children }: { group: string; count: number; children: ReactNode }) {
  const { t_i18n } = useFormatter();
  const headingId = `security-options-group-${group}`;
  const Icon = GROUP_ICONS[group] ?? SlidersHorizontal;
  return (
    <section aria-labelledby={headingId} data-testid="security-options-group" data-group={group}>
      <SettingsGroupTitle id={headingId} icon={Icon} className="mb-3">
        {t_i18n(groupLabel(group))}
        <span className="rounded-sm bg-elevation-hover px-1.5 content-compact font-medium tabular-nums text-default-secondary">{count}</span>
      </SettingsGroupTitle>
      <ul className="space-y-3">{children}</ul>
    </section>
  );
}

interface RowIds {
  control: string;
  label: string;
  description: string;
  problem: string;
}

function rowIds(key: string): RowIds {
  const base = `security-option-${key}`;
  return { control: `${base}-control`, label: `${base}-label`, description: `${base}-description`, problem: `${base}-problem` };
}

function OptionRow({
  option,
  targeted,
  value,
  summary,
  defaultValue,
  control,
  onReset,
}: {
  option: SecurityOption;
  /** The row the location's `#option-<key>` points at. */
  targeted: boolean;
  /** The value and the default as the row reads them (translated). */
  value: string;
  /** Shown instead of `value` when set: a list of tools, laid out under the row's text. */
  summary: ReactNode;
  defaultValue: string;
  /** The field, while the card is being configured and the option is not locked. */
  control: ((ids: RowIds) => ReactNode) | null;
  onReset: (() => void) | null;
}) {
  const { t_i18n } = useFormatter();
  const navigate = useNavigate();
  const locked = lockMessage(option, t_i18n);
  const description = optionDescription(option.key);
  const info = optionInfo(option, t_i18n);
  const findingPath = auditFindingPath(option.key);
  const ids = rowIds(option.key);
  // A list of tools takes the row's width, under its text.
  const wide = option.kind === "tools";
  const field = control ? (
    <>
      {control(ids)}
      {onReset && (
        <Tip content={t_i18n("Reset to the default")}>
          <Button variant="ghost" size="icon" className="h-8 w-8" aria-label={t_i18n("Reset to the default")} onClick={onReset}>
            <RotateCcw className="h-3.5 w-3.5" />
          </Button>
        </Tip>
      )}
    </>
  ) : (
    summary ?? (
      <Badge variant="outline" className="max-w-xs whitespace-normal break-words text-left content-caption font-medium">
        {value}
      </Badge>
    )
  );
  return (
    <li
      id={optionAnchorId(option.key)}
      tabIndex={-1}
      data-testid="security-option"
      data-option-key={option.key}
      data-source={option.source}
      data-targeted={targeted || undefined}
      className={cn(
        "rounded-sm border p-4 transition-colors focus-visible:outline-none",
        targeted ? "border-accent/60 bg-accent/5" : "border-elevation-subtle-soft layer-1 bg-elevation-default",
      )}
    >
      <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
        <div className="min-w-0 flex-1 space-y-1.5">
          <div className="flex flex-wrap items-center gap-2">
            <h5 id={ids.label} className="content-base font-medium text-default-primary">
              {t_i18n(optionLabel(option.key))}
            </h5>
            <InfoTip
              label={t_i18n("About this field")}
              content={
                <div className="max-w-xs space-y-1 content-compact" data-testid="security-option-info">
                  {description && <p>{t_i18n(description)}</p>}
                  {info.map((line) => (
                    <p key={line}>{line}</p>
                  ))}
                  <p>{t_i18n("Environment variable: {name}", { name: option.env_var })}</p>
                </div>
              }
            />
            {locked &&
              (option.locked_reason === "platform_mode" ? (
                <ReadOnlyBadge tooltip={locked} />
              ) : (
                <EnvLockBadge tooltip={locked} />
              ))}
          </div>
          <div id={ids.description} className="space-y-1">
            {description && <p className="content-compact leading-relaxed text-default-secondary">{t_i18n(description)}</p>}
            {locked ? (
              <p className="content-compact leading-relaxed text-feedback-warning-primary light:text-feedback-warning-primary">
                <ConfigText text={locked} />
              </p>
            ) : (
              <p className="flex flex-wrap items-center gap-x-2 gap-y-1 content-compact text-default-secondary">
                <span>
                  {option.source === "database" ? t_i18n("Default: {value}", { value: defaultValue }) : t_i18n("Default value")}
                </span>
                <ConfigCode>{option.env_var}</ConfigCode>
              </p>
            )}
          </div>
          {findingPath && (
            <Tip content={t_i18n("Open the security audit on the finding this option decides")}>
              <Button
                variant="link"
                size="sm"
                className="h-auto gap-1.5 px-0 py-0 content-compact font-medium text-accent"
                onClick={() => navigate(findingPath)}
                data-testid="security-option-audit-link"
                data-option={option.key}
              >
                <ShieldAlert className="h-3.5 w-3.5" />
                {t_i18n("Security audit")}
              </Button>
            </Tip>
          )}
        </div>
        {!wide && <div className="flex shrink-0 items-center gap-2 sm:justify-end">{field}</div>}
      </div>
      {wide && <div className="mt-3 flex items-start gap-2">{field}</div>}
    </li>
  );
}
