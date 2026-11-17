// SPDX-License-Identifier: Apache-2.0

import { useMemo, useState } from "react";
import { AlertTriangle, Key } from "lucide-react";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { DateFilterInput } from "@/components/ui/date-filter-input";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { MultiSelect } from "@/components/ui/multi-select";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import { useToast } from "@/components/ui/toast";
import { useAgents } from "@/hooks/api/useAgentApi";
import { useApiKeyPolicy, useCreateApiKey, useUpdateApiKey } from "@/hooks/api/useMiscApi";
import {
  API_KEY_FORM_PROBLEMS,
  API_KEY_QUOTA_PERIOD_LABELS,
  API_KEY_SURFACE_DESCRIPTIONS,
  API_KEY_SURFACE_LABELS,
  type ApiKeyForm,
  type ApiKeyFormProblem,
  DAY_MS,
  type ExpiryChoice,
  allowsNoExpiry,
  apiKeyFormFromKey,
  apiKeyFormProblem,
  buildCreateApiKeyRequest,
  buildUpdateApiKeyRequest,
  dateInputValue,
  expiryPresets,
  latestAllowedExpiry,
  newApiKeyForm,
} from "@/lib/api-keys";
import { useFormatter } from "@/i18n";
import type { ApiKeyPolicy, ApiKeyQuotaPeriod, UserApiKey, UserApiKeyCreated } from "@/types";
import { Spinner } from "@filigran/design-system";

interface ApiKeyDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** The key to change; a new key when absent. */
  apiKey?: UserApiKey | null;
  /** Called with the new key, whose secret is shown once. */
  onCreated?: (key: UserApiKeyCreated) => void;
}

/** Create a personal API key, or change one's name, scopes, expiry and limits. */
export function ApiKeyDialog({ open, onOpenChange, apiKey, onCreated }: ApiKeyDialogProps) {
  const { t_i18n } = useFormatter();
  const editing = !!apiKey;

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-lg">
        <DialogHeader>
          <div className="flex items-center gap-3">
            <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-full bg-accent/10">
              <Key className="h-4 w-4 text-accent" />
            </div>
            <div className="min-w-0">
              <DialogTitle>{editing ? t_i18n("Edit API key") : t_i18n("New API key")}</DialogTitle>
              <DialogDescription>
                {editing
                  ? t_i18n("A narrower scope applies from the key's next request.")
                  : t_i18n("A key acts as you, within the scopes and limits you give it.")}
              </DialogDescription>
            </div>
          </div>
        </DialogHeader>
        <ApiKeyDialogContent apiKey={apiKey ?? null} onClose={() => onOpenChange(false)} onCreated={onCreated} />
      </DialogContent>
    </Dialog>
  );
}

/**
 * Mounted each time the dialog opens: the form is built from a policy read
 * after that, so a maximum lifetime set meanwhile is never offered past.
 */
function ApiKeyDialogContent({ apiKey, onClose, onCreated }: Omit<ApiKeyDialogBodyProps, "policy">) {
  const { t_i18n } = useFormatter();
  const [openedAt] = useState(() => Date.now());
  const { data: policy, isError, dataUpdatedAt } = useApiKeyPolicy({ fresh: true });
  if (policy && dataUpdatedAt >= openedAt) {
    return (
      <ApiKeyDialogBody key={apiKey?.id ?? "new"} policy={policy} apiKey={apiKey} onClose={onClose} onCreated={onCreated} />
    );
  }
  if (isError) {
    return <p className="content-base text-feedback-error-primary">{t_i18n("Could not load what a key may be given. Try again later.")}</p>;
  }
  return (
    <div className="flex justify-center py-8">
      <Spinner size="md" tone="inherit" className="text-default-secondary" label={t_i18n("Loading...")} />
    </div>
  );
}

interface ApiKeyDialogBodyProps {
  policy: ApiKeyPolicy;
  apiKey: UserApiKey | null;
  onClose: () => void;
  onCreated?: (key: UserApiKeyCreated) => void;
}

function problemMax(problem: ApiKeyFormProblem, policy: ApiKeyPolicy): number {
  if (problem === "too_many_agents") return policy.max_agents;
  if (problem === "rate_limit") return policy.max_rate_limit_per_minute;
  return policy.max_quota_limit;
}

function ApiKeyDialogBody({ policy, apiKey, onClose, onCreated }: ApiKeyDialogBodyProps) {
  const { t_i18n, intl } = useFormatter();
  const { toast } = useToast();
  const { data: agents } = useAgents();
  const createKey = useCreateApiKey();
  const updateKey = useUpdateApiKey();
  const [openedAt] = useState(() => Date.now());
  const [initial] = useState<ApiKeyForm>(() => (apiKey ? apiKeyFormFromKey(apiKey, policy) : newApiKeyForm(policy)));
  const [form, setForm] = useState<ApiKeyForm>(initial);
  const [attempted, setAttempted] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const maxDays = policy.max_lifetime_days;
  const latest = latestAllowedExpiry(openedAt, maxDays);
  const problem = apiKeyFormProblem(form, policy, openedAt);
  const pending = createKey.isPending || updateKey.isPending;
  const unchanged =
    apiKey !== null && Object.keys(buildUpdateApiKeyRequest(initial, form, policy, openedAt)).length === 0;

  const agentOptions = useMemo(() => {
    const options = (agents ?? []).map((agent) => ({ value: agent.id, label: agent.name }));
    for (const named of apiKey?.agents ?? []) {
      if (!options.some((option) => option.value === named.id)) {
        options.push({ value: named.id, label: named.name ?? t_i18n("Deleted agent") });
      }
    }
    return options.sort((a, b) => a.label.localeCompare(b.label));
  }, [agents, apiKey, t_i18n]);

  const update = (patch: Partial<ApiKeyForm>) => {
    setForm((current) => ({ ...current, ...patch }));
    setError(null);
  };

  const toggleSurface = (surface: ApiKeyForm["surfaces"][number], on: boolean) =>
    update({
      surfaces: on ? [...form.surfaces, surface] : form.surfaces.filter((entry) => entry !== surface),
    });

  const toggleAgent = (agentId: string) =>
    update({
      agentIds: form.agentIds.includes(agentId)
        ? form.agentIds.filter((entry) => entry !== agentId)
        : [...form.agentIds, agentId],
    });

  const submit = () => {
    setAttempted(true);
    const now = Date.now();
    if (apiKeyFormProblem(form, policy, now) !== null) return;
    const onError = (err: Error) => setError(err.message);
    if (apiKey) {
      const body = buildUpdateApiKeyRequest(initial, form, policy, now);
      if (Object.keys(body).length === 0) {
        onClose();
        return;
      }
      updateKey.mutate(
        { id: apiKey.id, body },
        {
          onSuccess: () => {
            toast({ title: t_i18n("API key updated"), variant: "success" });
            onClose();
          },
          onError,
        },
      );
      return;
    }
    createKey.mutate(buildCreateApiKeyRequest(form, policy, now), {
      onSuccess: (created) => {
        onCreated?.(created);
        onClose();
      },
      onError,
    });
  };

  const expiryOptions: { value: ExpiryChoice; label: string }[] = [
    ...(apiKey
      ? [
          {
            value: "keep" as const,
            label: apiKey.expires_at
              ? t_i18n("Keep: {date}", {
                  date: intl.formatDate(apiKey.expires_at, { year: "numeric", month: "short", day: "numeric" }),
                })
              : t_i18n("Keep: no expiry"),
          },
        ]
      : []),
    ...expiryPresets(maxDays).map((days) => ({
      value: `${days}` as const,
      label: t_i18n("{count, plural, one {# day} other {# days}}", { count: days }),
    })),
    { value: "custom", label: t_i18n("Pick a date") },
    ...(allowsNoExpiry(maxDays) ? [{ value: "never" as const, label: t_i18n("No expiry") }] : []),
  ];

  return (
    <form
      className="space-y-5"
      onSubmit={(event) => {
        event.preventDefault();
        submit();
      }}
    >
      <div className="space-y-1.5">
        <Label htmlFor="api-key-name">{t_i18n("Name")}</Label>
        <Input aria-label={t_i18n("Name")}
          id="api-key-name"
          value={form.name}
          onChange={(event) => update({ name: event.target.value })}
          placeholder={t_i18n("e.g. CI pipeline, Cursor, n8n")}
          maxLength={255}
          autoFocus
        />
      </div>

      <section className="space-y-2">
        <h4 className="content-base font-medium">{t_i18n("Surfaces")}</h4>
        <div className="divide-y divide-[var(--border-elevation-subtle)] rounded-sm border border-elevation-subtle-soft">
          {policy.surfaces.map((surface) => (
            <label key={surface} className="flex cursor-pointer items-start justify-between gap-3 px-3 py-2.5">
              <span className="min-w-0">
                <span className="block content-base">{t_i18n(API_KEY_SURFACE_LABELS[surface])}</span>
                <span className="block content-compact text-default-secondary">
                  {t_i18n(API_KEY_SURFACE_DESCRIPTIONS[surface])}
                </span>
              </span>
              <Switch
                checked={form.surfaces.includes(surface)}
                onCheckedChange={(on) => toggleSurface(surface, on)}
                aria-label={t_i18n(API_KEY_SURFACE_LABELS[surface])}
              />
            </label>
          ))}
        </div>
      </section>

      <section className="space-y-3">
        <h4 className="content-base font-medium">{t_i18n("Access")}</h4>
        <label className="flex cursor-pointer items-start justify-between gap-3">
          <span className="min-w-0">
            <span className="block content-base">{t_i18n("Read-only")}</span>
            <span className="block content-compact text-default-secondary">
              {t_i18n(
                "Platform API: reads only. LLM proxy: models only, no agent. MCP: the tools that change nothing.",
              )}
            </span>
          </span>
          <Switch
            checked={form.readOnly}
            onCheckedChange={(on) => update({ readOnly: on })}
            aria-label={t_i18n("Read-only")}
          />
        </label>
        <label className="flex cursor-pointer items-start justify-between gap-3">
          <span className="min-w-0">
            <span className="block content-base">{t_i18n("Limit to specific agents")}</span>
            <span className="block content-compact text-default-secondary">
              {t_i18n("The key reaches only these agents, wherever an agent is chosen.")}
            </span>
          </span>
          <Switch
            checked={form.restrictAgents}
            onCheckedChange={(on) => update({ restrictAgents: on })}
            aria-label={t_i18n("Limit to specific agents")}
          />
        </label>
        {form.restrictAgents && (
          <MultiSelect
            options={agentOptions}
            selected={form.agentIds}
            onToggle={toggleAgent}
            placeholder={t_i18n("Choose agents")}
            searchPlaceholder={t_i18n("Search agents...")}
            emptyOptionsLabel={t_i18n("No agents available")}
            noMatchLabel={t_i18n("No matching agents")}
          />
        )}
      </section>

      <section className="space-y-2">
        <h4 className="content-base font-medium">{t_i18n("Expiry")}</h4>
        <div className="flex flex-wrap items-center gap-2">
          <Select
            value={form.expiry}
            onValueChange={(value) =>
              update({
                expiry: value as ExpiryChoice,
                expiryDate:
                  value === "custom" && !form.expiryDate ? dateInputValue(openedAt + 30 * DAY_MS) : form.expiryDate,
              })
            }
          >
            <SelectTrigger className="h-9 w-48 content-base" aria-label={t_i18n("Expiry")}>
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {expiryOptions.map((option) => (
                <SelectItem key={option.value} value={option.value}>
                  {option.label}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          {form.expiry === "custom" && (
            <DateFilterInput
              value={form.expiryDate}
              min={dateInputValue(openedAt + DAY_MS)}
              max={latest === null ? undefined : dateInputValue(latest)}
              onChange={(expiryDate) => update({ expiryDate })}
              aria-label={t_i18n("Expiry date")}
            />
          )}
        </div>
        <p className="content-compact text-default-secondary">
          {maxDays > 0
            ? t_i18n(
                "Your administrator limits keys to {count, plural, one {# day} other {# days}}. You are notified 7 days and 1 day before a key expires.",
                { count: maxDays },
              )
            : t_i18n("You are notified 7 days and 1 day before a key expires.")}
        </p>
      </section>

      <section className="space-y-2">
        <h4 className="content-base font-medium">{t_i18n("Limits")}</h4>
        <div className="grid gap-3 sm:grid-cols-2">
          <div className="space-y-1.5">
            <Label htmlFor="api-key-rate" className="text-default-secondary">
              {t_i18n("Requests per minute")}
            </Label>
            <Input aria-label={t_i18n("Requests per minute")}
              id="api-key-rate"
              inputMode="numeric"
              value={form.rateLimit}
              onChange={(event) => update({ rateLimit: event.target.value })}
              placeholder={t_i18n("No limit")}
            />
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="api-key-quota" className="text-default-secondary">
              {t_i18n("Quota")}
            </Label>
            <div className="flex gap-2">
              <Input aria-label={t_i18n("Quota")}
                id="api-key-quota"
                inputMode="numeric"
                value={form.quotaLimit}
                onChange={(event) => update({ quotaLimit: event.target.value })}
                placeholder={t_i18n("No quota")}
                className="min-w-0"
              />
              <Select
                value={form.quotaPeriod}
                onValueChange={(value) => update({ quotaPeriod: value as ApiKeyQuotaPeriod })}
              >
                <SelectTrigger className="h-9 w-32 shrink-0 content-base" aria-label={t_i18n("Quota period")}>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {policy.quota_periods.map((period) => (
                    <SelectItem key={period} value={period}>
                      {t_i18n(API_KEY_QUOTA_PERIOD_LABELS[period])}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
          </div>
        </div>
        <p className="content-compact text-default-secondary">
          {t_i18n(
            "On top of your own quotas. A key over its limit is refused with a 429 while your other keys keep working.",
          )}
        </p>
      </section>

      {(error || (attempted && problem)) && (
        <div
          role="alert"
          className="flex items-start gap-2 rounded-sm border border-feedback-error-primary/30 bg-feedback-error-primary/10 px-3 py-2 content-compact text-feedback-error-primary"
        >
          <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
          <span>
            {error ?? (problem ? t_i18n(API_KEY_FORM_PROBLEMS[problem], { max: problemMax(problem, policy) }) : null)}
          </span>
        </div>
      )}

      <div className="flex justify-end gap-2">
        <Button type="button" variant="ghost" onClick={onClose} disabled={pending}>
          {t_i18n("Cancel")}
        </Button>
        <Button type="submit" disabled={pending || unchanged} aria-busy={pending || undefined}>
          {pending && <Spinner size="sm" tone="inherit" />}
          {apiKey ? t_i18n("Save") : t_i18n("Create key")}
        </Button>
      </div>
    </form>
  );
}
