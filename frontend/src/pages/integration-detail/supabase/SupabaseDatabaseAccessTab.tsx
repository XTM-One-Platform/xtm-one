import { useEffect, useMemo, useState } from "react";
import { Icon, Spinner, Textarea } from "@filigran/design-system";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Switch } from "@/components/ui/switch";
import { SearchableSelect, type SelectOption } from "@/components/ui/searchable-select";
import { RefusalAlert } from "@/components/ui/refusal-alert";
import { useConfirm } from "@/components/ui/confirm-dialog";
import { useToast } from "@/components/ui/toast";
import { useUpdateIntegration } from "@/hooks/api/useIntegrationApi";
import { useSupabaseProbe } from "@/hooks/api/useSupabaseApi";
import { useFormatter } from "@/i18n";
import type { Integration, SupabaseConnectionMode, SupabaseProbeFunction } from "@/types";
import {
  allowedFunctions,
  connectionValue,
  draftFromExtra,
  extraFromDraft,
  functionKey,
  hasStoredPassword,
  needsPassword,
  parseConnection,
  probeBody,
  withoutDatabaseRole,
  type DatabaseDraft,
} from "./supabase-database-access";

/** How each connection is named in the host picker; every value is a catalog key `validate-i18n` checks. @i18n-keys */
const CONNECTION_LABELS = {
  session: "Session pooler",
  transaction: "Transaction pooler",
  direct: "Direct connection (IPv6)",
} as const satisfies Record<SupabaseConnectionMode, string>;

/** Where a probe part failed, as the error's title. @i18n-keys */
const ERROR_TITLES = {
  management: "Supabase account",
  roles: "Roles",
  connections: "Connection hosts",
  database: "Database role",
} as const;

const errorTitle = (part: string): string =>
  part in ERROR_TITLES ? ERROR_TITLES[part as keyof typeof ERROR_TITLES] : "Probe";

function Field({ id, label, hint, children }: { id: string; label: string; hint?: string; children: React.ReactNode }) {
  return (
    <div className="space-y-1.5">
      <label className="content-compact font-medium" htmlFor={id}>{label}</label>
      {children}
      {hint && <p className="text-[11px] text-default-secondary">{hint}</p>}
    </div>
  );
}

function FunctionRow({
  fn, allowed, onToggle, disabled,
}: {
  fn: SupabaseProbeFunction;
  allowed: boolean;
  onToggle: (next: boolean) => void;
  disabled: boolean;
}) {
  const { t_i18n } = useFormatter();
  const key = functionKey(fn);
  return (
    <li className="flex items-start justify-between gap-3 py-2">
      <div className="min-w-0 space-y-0.5">
        <p className="break-all font-mono-plex content-compact">
          {key}({fn.signature}) → {fn.returns}
        </p>
        <div className="flex flex-wrap gap-1.5">
          {fn.security_definer && <Badge variant="outline">{t_i18n("Security definer")}</Badge>}
          {fn.returns_set && <Badge variant="outline">{t_i18n("Returns rows")}</Badge>}
        </div>
        {fn.comment && <p className="content-compact text-default-secondary">{fn.comment}</p>}
      </div>
      <Switch
        aria-label={t_i18n("Allow {name}", { name: key })}
        checked={allowed}
        onCheckedChange={onToggle}
        disabled={disabled}
      />
    </li>
  );
}

// ---------------------------------------------------------------------------
// Supabase Database access - sub-page under /integrations/:id/database: the
// Postgres role the integration's data calls log in as, chosen from what the
// probe reads (projects, roles, hosts, databases, functions), and the
// functions agents may call. Saved through the integration update.
// ---------------------------------------------------------------------------
export function SupabaseDatabaseAccessTab({ integration }: { integration: Integration }) {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const [ConfirmEl, confirm] = useConfirm();
  const probe = useSupabaseProbe(integration.id);
  const update = useUpdateIntegration();
  const stored = integration.extra_config;
  const [draft, setDraft] = useState<DatabaseDraft>(() => draftFromExtra(stored));
  const [allowed, setAllowed] = useState<string[]>(() => allowedFunctions(stored));
  const configured = !!draftFromExtra(stored).role;
  const result = probe.data;
  const { mutate: runProbe } = probe;

  // The first probe reads the stored settings: the choices are there on open.
  useEffect(() => {
    runProbe(probeBody(draftFromExtra(stored)));
    // Once per integration: a later probe is the person's.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [integration.id, runProbe]);

  const set = (patch: Partial<DatabaseDraft>) => setDraft((current) => ({ ...current, ...patch }));

  const projectOptions = useMemo<SelectOption[]>(
    () => (result?.projects ?? []).map((project) => ({
      value: project.ref,
      label: project.name || project.ref,
      description: `${project.ref} · ${project.region}`,
    })),
    [result?.projects],
  );
  const connectionOptions = useMemo<SelectOption[]>(
    () => (result?.connections ?? []).map((connection) => ({
      value: connectionValue(connection.host, connection.port),
      label: `${t_i18n(CONNECTION_LABELS[connection.mode])} - ${connection.host}:${connection.port}`,
    })),
    [result?.connections, t_i18n],
  );
  const roleOptions = useMemo<SelectOption[]>(
    () => (result?.roles ?? []).map((role) => ({
      value: role.name,
      label: role.name,
      description: role.bypass_rls ? t_i18n("Bypasses row level security") : undefined,
      disabled: role.bypass_rls,
    })),
    [result?.roles, t_i18n],
  );
  const databaseOptions = useMemo<SelectOption[]>(
    () => (result?.databases ?? []).map((name) => ({ value: name, label: name })),
    [result?.databases],
  );
  const probedKeys = new Set((result?.functions ?? []).map(functionKey));
  const missing = allowed.filter((key) => result?.functions && !probedKeys.has(key));
  const busy = probe.isPending || update.isPending;
  const passwordNeeded = needsPassword(stored, draft);
  // The project scopes every tool of the integration: never saved empty from here.
  const canSave = !!draft.projectRef.trim() && !!draft.role.trim() && !!draft.host.trim() && !passwordNeeded;

  const toggle = (key: string, next: boolean) =>
    setAllowed((current) => (next ? [...new Set([...current, key])] : current.filter((item) => item !== key)));

  const save = () => {
    update.mutate(
      { id: integration.id, extra_config: extraFromDraft(stored, draft, allowed) },
      {
        onSuccess: () => {
          set({ password: "" });
          toast({ title: t_i18n("Database access saved"), variant: "success" });
        },
        onError: (error) => toast({ title: t_i18n("Database access could not be saved"), description: error.message, variant: "error" }),
      },
    );
  };

  const remove = async () => {
    const confirmed = await confirm({
      title: "Remove the database role?",
      description: "Agents reach the data with the Management API roles again, which bypass row level security, and the free SQL tools come back.",
      confirmLabel: "Remove",
    });
    if (!confirmed) return;
    update.mutate(
      { id: integration.id, extra_config: withoutDatabaseRole(stored) },
      {
        onSuccess: () => {
          setDraft(draftFromExtra(withoutDatabaseRole(stored)));
          setAllowed([]);
          toast({ title: t_i18n("Database role removed"), variant: "success" });
        },
        onError: (error) => toast({ title: t_i18n("Database access could not be saved"), description: error.message, variant: "error" }),
      },
    );
  };

  return (
    <div className="space-y-4">
      {ConfirmEl}
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <Icon name="database" size={16} />
            {t_i18n("Database role")}
            {configured && <Badge variant="outline">{t_i18n("Active")}</Badge>}
          </CardTitle>
          <CardDescription>
            {t_i18n("Agents reach the project's data as this Postgres role, under its grants and row level security. The free SQL tools are then withheld: agents call only the functions allowed below.")}
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="grid gap-4 md:grid-cols-2">
            <Field id="supabase-project" label={t_i18n("Project")}>
              {result?.projects ? (
                <SearchableSelect
                  id="supabase-project"
                  aria-label={t_i18n("Project")}
                  options={projectOptions}
                  value={draft.projectRef}
                  onValueChange={(projectRef) => {
                    // Another project has its own hosts, roles and functions: read them again.
                    const next = { ...draft, projectRef, host: "", role: "", password: "" };
                    setDraft(next);
                    probe.mutate(probeBody(next));
                  }}
                  searchable="auto"
                />
              ) : (
                <Input id="supabase-project" aria-label={t_i18n("Project")} value={draft.projectRef} placeholder="abcdefghijklmnopqrst" onChange={(e) => set({ projectRef: e.target.value })} />
              )}
            </Field>
            <Field id="supabase-host" label={t_i18n("Connection host")} hint={t_i18n("The session pooler works for every network; the direct host is IPv6 only.")}>
              {result?.connections?.length ? (
                <SearchableSelect
                  id="supabase-host"
                  aria-label={t_i18n("Connection host")}
                  options={connectionOptions}
                  value={draft.host ? connectionValue(draft.host, draft.port) : ""}
                  onValueChange={(value) => set(parseConnection(value))}
                  sort={false}
                  searchable="auto"
                />
              ) : (
                <div className="flex gap-2">
                  <Input id="supabase-host" aria-label={t_i18n("Connection host")} value={draft.host} placeholder="aws-0-eu-west-1.pooler.supabase.com" onChange={(e) => set({ host: e.target.value })} />
                  <Input aria-label={t_i18n("Port")} className="w-24" value={draft.port} inputMode="numeric" onChange={(e) => set({ port: e.target.value })} />
                </div>
              )}
            </Field>
            <Field id="supabase-role" label={t_i18n("Role")}>
              {result?.roles ? (
                <SearchableSelect
                  id="supabase-role"
                  aria-label={t_i18n("Role")}
                  options={roleOptions}
                  value={draft.role}
                  onValueChange={(role) => set({ role })}
                  emptyMessage={t_i18n("No login role the agents may use")}
                />
              ) : (
                <Input id="supabase-role" aria-label={t_i18n("Role")} value={draft.role} placeholder="agent_reader" onChange={(e) => set({ role: e.target.value })} />
              )}
            </Field>
            <Field id="supabase-password" label={t_i18n("Password")}>
              <Input
                id="supabase-password"
                aria-label={t_i18n("Password")}
                type="password"
                autoComplete="new-password"
                value={draft.password}
                placeholder={hasStoredPassword(stored) && !passwordNeeded ? t_i18n("Stored: type to replace") : ""}
                onChange={(e) => set({ password: e.target.value })}
              />
              {passwordNeeded && hasStoredPassword(stored) && (
                <p className="text-[11px] text-default-secondary">
                  {t_i18n("Another role, host or project: type its password.")}
                </p>
              )}
            </Field>
            <Field id="supabase-database" label={t_i18n("Database")}>
              {result?.databases?.length ? (
                <SearchableSelect
                  id="supabase-database"
                  aria-label={t_i18n("Database")}
                  options={databaseOptions}
                  value={draft.database}
                  onValueChange={(database) => set({ database })}
                  searchable="auto"
                />
              ) : (
                <Input id="supabase-database" aria-label={t_i18n("Database")} value={draft.database} onChange={(e) => set({ database: e.target.value })} />
              )}
            </Field>
            <Field id="supabase-ca" label={t_i18n("CA certificate (optional)")} hint={t_i18n("From the Supabase dashboard (Database settings, SSL Configuration), when the host's certificate is not trusted by default.")}>
              <Textarea id="supabase-ca" aria-label={t_i18n("CA certificate (optional)")} rows={2} value={draft.caCert} placeholder="-----BEGIN CERTIFICATE-----" onChange={(e) => set({ caCert: e.target.value })} />
            </Field>
          </div>
          <div className="flex flex-wrap items-center justify-end gap-2">
            {configured && (
              <Button variant="ghost" size="sm" onClick={remove} disabled={busy}>{t_i18n("Remove the role")}</Button>
            )}
            <Button variant="outline" size="sm" onClick={() => probe.mutate(probeBody(draft))} disabled={busy}>
              {probe.isPending ? <Spinner size="sm" tone="inherit" label={t_i18n("Probing…")} /> : <Icon name="refresh-cw" size={16} />}
              {t_i18n("Probe")}
            </Button>
            <Button size="sm" onClick={save} disabled={busy || !canSave}>{t_i18n("Save")}</Button>
          </div>
          {probe.error && <RefusalAlert message={t_i18n("The probe failed.")} detail={probe.error.message} />}
          {Object.entries(result?.errors ?? {}).map(([part, message]) => (
            <RefusalAlert key={part} message={`${t_i18n(errorTitle(part))}: ${message}`} />
          ))}
          {result?.role_bypasses_rls && (
            <p className="flex items-center gap-1.5 content-compact text-feedback-warning-primary">
              <Icon name="triangle-alert" size={16} />
              {t_i18n("This role bypasses row level security: every row of the tables it is granted is readable.")}
            </p>
          )}
          {result?.role_bypasses_rls === false && (
            <p className="flex items-center gap-1.5 content-compact text-default-secondary">
              <Icon name="shield-check" size={16} />
              {t_i18n("Row level security applies to this role.")}
            </p>
          )}
        </CardContent>
      </Card>

      {result?.readable_relations && result.readable_relations.length > 0 && (
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2 text-feedback-warning-primary">
              <Icon name="triangle-alert" size={16} />
              {t_i18n("Tables the role can still read directly")}
            </CardTitle>
            <CardDescription>
              {t_i18n("Agents never query them through this integration, but the role holds SELECT on them. Revoke it so the data is reached through the allowed functions only.")}
            </CardDescription>
          </CardHeader>
          <CardContent>
            <p className="break-words font-mono-plex content-compact">
              {result.readable_relations.map((relation) => `${relation.schema}.${relation.name}`).join(", ")}
            </p>
          </CardContent>
        </Card>
      )}

      <Card>
        <CardHeader>
          <CardTitle>{t_i18n("Functions agents may call")}</CardTitle>
          <CardDescription>
            {t_i18n("The functions the role may execute in the project's own schemas. Agents call only those allowed here, as the role; Save to apply.")}
          </CardDescription>
        </CardHeader>
        <CardContent>
          {!result?.functions && (
            <p className="content-compact text-default-secondary">
              {probe.isPending ? t_i18n("Probing…") : t_i18n("Probe with a role and its password to list the functions it may execute.")}
            </p>
          )}
          {result?.functions && result.functions.length === 0 && (
            <p className="content-compact text-default-secondary">{t_i18n("The role may execute no function of the project's own schemas.")}</p>
          )}
          {result?.functions && result.functions.length > 0 && (
            <ul className="divide-y divide-[var(--border-elevation-subtle)]">
              {result.functions.map((fn) => (
                <FunctionRow
                  key={`${functionKey(fn)}(${fn.signature})`}
                  fn={fn}
                  allowed={allowed.includes(functionKey(fn))}
                  onToggle={(next) => toggle(functionKey(fn), next)}
                  disabled={busy}
                />
              ))}
            </ul>
          )}
          {missing.length > 0 && (
            <div className="mt-2 flex flex-wrap items-center gap-2">
              <p className="flex items-center gap-1.5 content-compact text-feedback-warning-primary">
                <Icon name="triangle-alert" size={16} />
                {t_i18n("Allowed but not executable by this role: {names}", { names: missing.join(", ") })}
              </p>
              <Button variant="ghost" size="sm" onClick={() => setAllowed((current) => current.filter((key) => probedKeys.has(key)))} disabled={busy}>
                {t_i18n("Stop allowing them")}
              </Button>
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
