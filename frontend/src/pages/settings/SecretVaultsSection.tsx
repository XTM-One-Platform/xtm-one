// SPDX-License-Identifier: Apache-2.0

/**
 * Settings > Access and security > Secret vaults: connect HashiCorp Vault, AWS
 * Secrets Manager, Google Cloud Secret Manager, Azure Key Vault and 1Password, grant them
 * to everyone, groups or users, test them, see where they are used, disable
 * or delete them. Providers declared in the environment (`SECRET_VAULT_<NAME>_*`)
 * are listed read-only. A credential is write-only: the page says it is set,
 * never what it is.
 */
import { useEffect, useMemo, useRef, useState } from "react";
import { Link } from "react-router-dom";
import {
  AlertTriangle,
  CheckCircle2,
  CircleDashed,
  KeyRound,
  Lock,
  Pencil,
  Plus,
  ShieldCheck,
  Trash2,
  Upload,
  Users,
  X,
  XCircle,
} from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { useConfirm } from "@/components/ui/confirm-dialog";
import { Dialog, DialogBody, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { EmptyState } from "@/components/ui/empty-state";
import { userSelectOption } from "@/components/ui/entity-select-options";
import { InfoTip } from "@/components/ui/info-tip";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { SearchableSelect, type SelectOption } from "@/components/ui/searchable-select";
import { SettingsField, SettingsSwitchField, settingsFieldHintId, settingsFieldLabelId } from "@/components/ui/settings-form";
import { Switch } from "@/components/ui/switch";
import { TagInput } from "@/components/ui/tag-input";
import { useToast } from "@/components/ui/toast";
import { Tip } from "@/components/ui/tooltip";
import { VaultTypeLogo } from "@/components/ui/vault-picker";
import { useUsers } from "@/hooks/api/useAuthApi";
import { useGroups } from "@/hooks/api/useGroupApi";
import {
  useCreateSecretVault,
  useDeleteSecretVault,
  useSecretVaults,
  useSecretVaultUsage,
  useTestSecretVault,
  useUpdateSecretVault,
} from "@/hooks/api/useSecretVaultApi";
import { useFormatter } from "@/i18n";
import {
  AWS_REGIONS,
  PROVIDER_NAME_RE,
  STATIC_KEY_METHODS,
  VAULT_AUTH_METHODS,
  VAULT_FAILURE_REASONS,
  VAULT_DEFAULT_METHOD,
  VAULT_TYPES,
  emptyVaultForm,
  requiredVaultFields,
  vaultFormFromProvider,
  vaultFormProblems,
  vaultInputFromForm,
  visibleFields,
  type AccessMode,
  type VaultFieldSpec,
  type VaultForm,
  type VaultFormProblem,
} from "@/lib/secret-vaults";
import { cn, formatShortDateTime } from "@/lib/utils";
import type { SecretVaultConnectionReport, SecretVaultGrant, SecretVaultProvider, SecretVaultType } from "@/types";
import {
  VAULT_ACCESS_LABELS,
  VAULT_FIELD_LABELS,
  VAULT_KV_VERSION_DESCRIPTIONS,
  VAULT_METHOD_HELP,
  VAULT_METHOD_LABELS,
  VAULT_STATUS_LABELS,
  VAULT_STEP_LABELS,
  VAULT_TYPE_DESCRIPTIONS,
  VAULT_TYPE_LABELS,
  VAULT_USE_KINDS,
  vaultFieldHelp,
  vaultFieldInfo,
  vaultUseHref,
} from "./secret-vault-labels";
import { Spinner } from "@filigran/design-system";

type T = (key: string, values?: Record<string, string | number>) => string;

function reasonText(t_i18n: T, reason: string | null | undefined, fallback?: string | null): string {
  const known = VAULT_FAILURE_REASONS[(reason ?? "") as keyof typeof VAULT_FAILURE_REASONS];
  return known ? t_i18n(known) : fallback || reason || "";
}

function label<K extends string>(t_i18n: T, table: Record<K, string>, key: string): string {
  const found = table[key as K];
  return found ? t_i18n(found) : key;
}

/** The name a display name suggests for a new provider: `Corporate Vault` -> `corporate_vault`. */
function slugOf(text: string): string {
  const slug = text
    .toLowerCase()
    .normalize("NFKD")
    .replace(/[^a-z0-9]+/g, "_")
    .replace(/^_+|_+$/g, "")
    .replace(/^[^a-z]+/, "");
  return slug.slice(0, 32);
}

function StatusBadge({ provider }: { provider: SecretVaultProvider }) {
  const { t_i18n } = useFormatter();
  const text = label(t_i18n, VAULT_STATUS_LABELS, provider.status);
  const reason = provider.status_reason ? reasonText(t_i18n, provider.status_reason, provider.status_description) : "";
  const detail = [reason, provider.status_detail].filter(Boolean).join(" ");
  const variant =
    provider.status === "connected"
      ? "success"
      : provider.status === "failing" || provider.status === "misconfigured" || provider.status === "shadowed"
        ? "destructive"
        : provider.status === "disabled"
          ? "secondary"
          : "outline";
  const Icon =
    provider.status === "connected"
      ? CheckCircle2
      : provider.status === "environment"
        ? Lock
        : provider.status === "not_checked"
          ? CircleDashed
          : provider.status === "disabled"
            ? X
            : AlertTriangle;
  const badge = (
    <Badge variant={variant} className="gap-1 content-caption" data-testid="secret-vault-status" data-status={provider.status}>
      <Icon className="h-3 w-3" aria-hidden />
      {text}
    </Badge>
  );
  return detail ? <Tip content={detail}>{badge}</Tip> : badge;
}

function ConnectionReportView({ report }: { report: SecretVaultConnectionReport }) {
  const { t_i18n } = useFormatter();
  return (
    <ol className="space-y-1" aria-label={t_i18n("Connection test")} data-testid="secret-vault-report">
      {report.steps.map((step) => (
        <li key={step.step} className="flex items-start gap-2 content-compact">
          {step.ok === true ? (
            <CheckCircle2 className="mt-0.5 h-4 w-4 shrink-0 text-feedback-success-primary" aria-hidden />
          ) : step.ok === false ? (
            <XCircle className="mt-0.5 h-4 w-4 shrink-0 text-feedback-error-primary" aria-hidden />
          ) : (
            <CircleDashed className="mt-0.5 h-4 w-4 shrink-0 text-default-secondary" aria-hidden />
          )}
          <span>
            <span className="font-medium">{label(t_i18n, VAULT_STEP_LABELS, step.step)}</span>
            {step.ok === false && (
              <span className="text-default-secondary">
                {": "}
                {reasonText(t_i18n, step.reason, step.description)}
                {step.detail ? ` (${step.detail})` : ""}
              </span>
            )}
            {step.ok === null && <span className="text-default-secondary">{`: ${step.detail ?? t_i18n("not run")}`}</span>}
          </span>
        </li>
      ))}
    </ol>
  );
}

// ---------------------------------------------------------------------------
// Where a provider is used
// ---------------------------------------------------------------------------

function UsageDialog({ name, onClose }: { name: string | null; onClose: () => void }) {
  const { t_i18n } = useFormatter();
  const { data: uses, isLoading } = useSecretVaultUsage(name);
  return (
    <Dialog open={!!name} onOpenChange={(open) => !open && onClose()}>
      <DialogContent className="max-w-lg">
        <DialogHeader>
          <DialogTitle>{t_i18n("Where {name} is used", { name: name ?? "" })}</DialogTitle>
          <DialogDescription>{t_i18n("The secret fields that read from this vault. The paths are not shown here.")}</DialogDescription>
        </DialogHeader>
        {isLoading ? (
          <Spinner size="sm" tone="inherit" className="text-default-secondary" label={t_i18n("Loading...")} />
        ) : !uses?.length ? (
          <p className="content-compact text-default-secondary">{t_i18n("No secret field reads from this vault.")}</p>
        ) : (
          <ul className="space-y-1.5" data-testid="secret-vault-uses">
            {uses.map((use) => {
              const href = vaultUseHref(use.kind, use.id);
              return (
                <li key={`${use.kind}:${use.id}:${use.field}`} className="flex items-center justify-between gap-3 content-compact">
                  <span className="min-w-0 truncate">
                    <span className="text-default-secondary">{label(t_i18n, VAULT_USE_KINDS, use.kind)}</span>{" "}
                    {href ? (
                      <Link to={href} className="font-medium text-accent hover:underline" onClick={onClose}>
                        {use.name || use.id}
                      </Link>
                    ) : (
                      <span className="font-medium">{use.name || use.id}</span>
                    )}
                  </span>
                  <Badge variant="outline" className="font-mono-plex content-caption">
                    {use.field}
                  </Badge>
                </li>
              );
            })}
          </ul>
        )}
      </DialogContent>
    </Dialog>
  );
}

// ---------------------------------------------------------------------------
// The create / edit dialog
// ---------------------------------------------------------------------------

/** A credential: write-only. Set, it reads "Set" with Replace and Clear; typing replaces it on save. */
function CredentialInput({
  field,
  form,
  stored,
  onChange,
}: {
  field: VaultFieldSpec;
  form: VaultForm;
  stored: SecretVaultProvider | null;
  onChange: (form: VaultForm) => void;
}) {
  const { t_i18n } = useFormatter();
  const fileRef = useRef<HTMLInputElement>(null);
  const isSet = Boolean(stored?.credentials_set[field.key]) && !form.cleared.includes(field.key);
  const typed = form.credentials[field.key] ?? "";
  const [replacing, setReplacing] = useState(false);
  const name = label(t_i18n, VAULT_FIELD_LABELS, field.key);
  const id = `vault-${field.key}`;
  const required = requiredVaultFields(form).has(field.key);
  const info = t_i18n("Stored encrypted and never shown again. Once saved, the field reads Set: Replace types a new value, Clear removes it on save.");
  const helper = t_i18n("Stored encrypted, never shown again.");
  const helpId = settingsFieldHintId(id);
  const setTyped = (value: string) =>
    onChange({ ...form, credentials: { ...form.credentials, [field.key]: value }, cleared: form.cleared.filter((key) => key !== field.key) });

  if (isSet && !replacing && !typed) {
    return (
      <SettingsField id={id} label={name} info={info} helper={helper} required={required}>
        <div id={id} role="group" aria-describedby={helpId} aria-labelledby={settingsFieldLabelId(id)} data-part="stored-credential" className="flex items-center gap-2">
          <span className="flex h-9 min-w-0 flex-1 items-center gap-2 rounded-sm border border-transparent bg-input-default px-3">
            <ShieldCheck className="h-4 w-4 shrink-0 text-feedback-success-primary" aria-hidden />
            <span className="content-base">{t_i18n("Set (stored credential)")}</span>
          </span>
          <Button type="button" variant="secondary" onClick={() => setReplacing(true)}>
            {t_i18n("Replace")}
          </Button>
          <Button
            type="button"
            variant="secondary"
            onClick={() => onChange({ ...form, cleared: [...form.cleared, field.key] })}
          >
            {t_i18n("Clear")}
          </Button>
        </div>
      </SettingsField>
    );
  }
  if (field.kind === "secret-file") {
    return (
      <SettingsField id={id} label={name} info={info} helper={helper} required={required}>
        <textarea
          id={id}
          aria-labelledby={settingsFieldLabelId(id)}
          aria-describedby={helpId}
          aria-required={required || undefined}
          value={typed}
          onChange={(event) => setTyped(event.target.value)}
          rows={4}
          placeholder={'{ "type": "service_account", ... }'}
          className="w-full rounded-sm border border-elevation-subtle bg-elevation-default px-3 py-2 font-mono-plex content-base focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus"
        />
        <input
          ref={fileRef}
          type="file"
          accept="application/json,.json"
          className="hidden"
          onChange={async (event) => {
            const file = event.target.files?.[0];
            if (file) setTyped(await file.text());
            event.target.value = "";
          }}
        />
        <Button type="button" variant="outline" className="gap-1" onClick={() => fileRef.current?.click()}>
          <Upload className="h-3.5 w-3.5" aria-hidden /> {t_i18n("Upload the key file")}
        </Button>
      </SettingsField>
    );
  }
  return (
    <SettingsField id={id} label={name} info={info} helper={helper} required={required}>
      <Input
        id={id}
        aria-labelledby={settingsFieldLabelId(id)}
        aria-describedby={helpId}
        aria-required={required || undefined}
        type="password"
        autoComplete="new-password"
        value={typed}
        onChange={(event) => setTyped(event.target.value)}
        placeholder={isSet ? t_i18n("Stored: type to replace") : undefined}
      />
    </SettingsField>
  );
}

function SettingInput({
  field,
  form,
  onChange,
  mounts,
}: {
  field: VaultFieldSpec;
  form: VaultForm;
  onChange: (form: VaultForm) => void;
  mounts: { path: string; version: string }[] | null;
}) {
  const { t_i18n } = useFormatter();
  const name = label(t_i18n, VAULT_FIELD_LABELS, field.key);
  const help = vaultFieldHelp(form.type, field.key);
  const infoKey = vaultFieldInfo(field.key);
  const info = infoKey ? t_i18n(infoKey) : undefined;
  const value = form.settings[field.key] ?? "";
  const set = (next: string) => onChange({ ...form, settings: { ...form.settings, [field.key]: next } });
  const id = `vault-${field.key}`;
  const required = requiredVaultFields(form).has(field.key);
  const helpId = help ? settingsFieldHintId(id) : undefined;

  if (field.kind === "select") {
    const choices = field.key === "region" ? AWS_REGIONS : (field.options ?? []);
    return (
      <SettingsField id={id} label={name} info={info} helper={help ? t_i18n(help) : undefined} required={required}>
        <SearchableSelect
          id={id}
          aria-labelledby={settingsFieldLabelId(id)}
          aria-describedby={helpId}
          searchable="auto"
          sort={false}
          clearable={field.key === "region"}
          options={choices.map((choice) => ({
            value: choice,
            label: choice,
            description: field.key === "kv_version" ? label(t_i18n, VAULT_KV_VERSION_DESCRIPTIONS, choice) : undefined,
          }))}
          value={value || null}
          onValueChange={set}
          placeholder={field.key === "region" ? t_i18n("The environment's region") : undefined}
        />
      </SettingsField>
    );
  }
  if (field.kind === "textarea") {
    return (
      <SettingsField id={id} label={name} info={info} helper={help ? t_i18n(help) : undefined} required={required}>
        <textarea
          id={id}
          aria-labelledby={settingsFieldLabelId(id)}
          aria-describedby={helpId}
          value={value}
          onChange={(event) => set(event.target.value)}
          rows={3}
          placeholder={field.placeholder}
          className="w-full rounded-sm border border-elevation-subtle bg-elevation-default px-3 py-2 font-mono-plex content-base focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus"
        />
      </SettingsField>
    );
  }
  return (
    <div className="space-y-2">
      <SettingsField id={id} label={name} info={info} helper={help ? t_i18n(help) : undefined} required={required}>
        <Input
          id={id}
          aria-labelledby={settingsFieldLabelId(id)}
          aria-describedby={helpId}
          aria-required={required || undefined}
          value={value}
          onChange={(event) => set(event.target.value)}
          placeholder={field.placeholder}
          className={field.monospace ? "font-mono-plex" : undefined}
        />
      </SettingsField>
      {field.key === "kv_mount" && mounts && mounts.length > 0 && (
        <div className="flex flex-wrap items-center gap-1.5">
          <span className="content-compact text-default-secondary">{t_i18n("Mounts the token sees:")}</span>
          {mounts.map((mount) => (
            <button
              key={mount.path}
              type="button"
              className="rounded-sm border border-elevation-subtle px-2 py-0.5 font-mono-plex content-compact hover:bg-elevation-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus"
              onClick={() => onChange({ ...form, settings: { ...form.settings, kv_mount: mount.path, kv_version: mount.version } })}
            >
              {mount.path} (v{mount.version})
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

/** How the Missing footer names a grant row with no group or user picked: its kind and its rank among its kind. */
function grantRowName(t_i18n: T, grants: readonly SecretVaultGrant[], index: number): string {
  const principal = grants[index]?.principal;
  const rank = grants.slice(0, index + 1).filter((grant) => grant.principal === principal).length;
  return principal === "user" ? t_i18n("User {n}", { n: rank }) : t_i18n("Group {n}", { n: rank });
}

/** What the Missing footer lists: every problem that stops the save, by the name of its field. */
function missingNames(t_i18n: T, form: VaultForm, problems: readonly VaultFormProblem[]): string[] {
  return problems.flatMap((problem) => {
    if (problem.kind === "required") return [label(t_i18n, VAULT_FIELD_LABELS, problem.field)];
    if (problem.kind === "name") return [t_i18n("Reference name")];
    if (problem.kind === "grant") return [grantRowName(t_i18n, form.grants, problem.index)];
    return [];
  });
}

function GrantsEditor({
  form,
  problems,
  onChange,
}: {
  form: VaultForm;
  problems: readonly VaultFormProblem[];
  onChange: (form: VaultForm) => void;
}) {
  const { t_i18n } = useFormatter();
  const { data: groups } = useGroups();
  const { data: users } = useUsers();
  const groupOptions: SelectOption[] = (groups ?? []).map((group) => ({ value: group.id, label: group.name }));
  const userOptions: SelectOption[] = (users ?? []).map((user) => userSelectOption(user));
  const setGrant = (index: number, grant: SecretVaultGrant) =>
    onChange({ ...form, grants: form.grants.map((existing, at) => (at === index ? grant : existing)) });
  const modes: AccessMode[] = ["admins", "everyone", "grants"];
  const missingGrants = new Set(problems.flatMap((problem) => (problem.kind === "grant" ? [problem.index] : [])));
  const pathsInfo = t_i18n("A path and everything under it, e.g. secret/soc. Press Enter after each one. Empty: every path the platform can read.");

  return (
    <div className="space-y-3">
      <div role="radiogroup" aria-label={t_i18n("Who may use this vault")} className="flex w-fit flex-wrap gap-1 rounded-sm bg-elevation-hover p-1">
        {modes.map((mode) => (
          <button
            key={mode}
            type="button"
            role="radio"
            aria-checked={form.access === mode}
            onClick={() => onChange({ ...form, access: mode })}
            className={cn(
              "rounded-sm px-3 py-1.5 content-compact font-medium transition-all focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus",
              form.access === mode ? "bg-accent/10 text-accent shadow-xs" : "text-default-secondary hover:text-default-primary",
            )}
          >
            {label(t_i18n, VAULT_ACCESS_LABELS, mode)}
          </button>
        ))}
      </div>
      {form.type === "onepassword" && form.access !== "admins" && (
        <p className="content-compact text-default-secondary">
          {t_i18n("A 1Password path is a vault id then an item id: grant a vault by its id, which the secret picker shows next to each vault.")}
        </p>
      )}
      {form.access === "admins" && (
        <p className="content-compact text-default-secondary">
          {t_i18n("Only administrators may pick a secret from this vault. Administrators always have every path.")}
        </p>
      )}
      {form.access === "everyone" && (
        <SettingsField
          id="vault-everyone-paths"
          label={t_i18n("Path prefixes (optional)")}
          info={pathsInfo}
          helper={t_i18n("Press Enter after each path. Empty: every path the platform can read, which the Security Audit reports.")}
        >
          <TagInput
            id="vault-everyone-paths"
            aria-labelledby={settingsFieldLabelId("vault-everyone-paths")}
            aria-describedby={settingsFieldHintId("vault-everyone-paths")}
            value={form.everyonePaths}
            onChange={(everyonePaths) => onChange({ ...form, everyonePaths })}
            placeholder={t_i18n("Add a path…")}
            preserveCase
            suggestFromPlatformTags={false}
          />
        </SettingsField>
      )}
      {form.access === "grants" && (
        <div className="space-y-2">
          {form.grants.length === 0 && (
            <p className="content-compact text-default-secondary">{t_i18n("Add a group or a user, and the paths each may use.")}</p>
          )}
          {form.grants.map((grant, index) => (
            <div key={index} className="space-y-1.5" data-testid="secret-vault-grant">
              {/* The row's controls share one baseline: the error goes under the whole row, never inside a column (UX r3 B1 of #10406). */}
              <div className="grid items-end gap-2 sm:grid-cols-[1.2fr_1.4fr_auto]" data-part="grant-controls">
                <SettingsField
                  id={`vault-grant-${index}`}
                  label={grant.principal === "group" ? t_i18n("Group") : t_i18n("User")}
                  info={grant.principal === "group" ? t_i18n("Every member of the group may pick secrets under the path prefixes beside it.") : t_i18n("This user may pick secrets under the path prefixes beside it.")}
                  required
                >
                  <SearchableSelect
                    id={`vault-grant-${index}`}
                    aria-labelledby={settingsFieldLabelId(`vault-grant-${index}`)}
                    aria-describedby={missingGrants.has(index) ? settingsFieldHintId(`vault-grant-${index}`) : undefined}
                    aria-invalid={missingGrants.has(index) || undefined}
                    options={grant.principal === "group" ? groupOptions : userOptions}
                    value={grant.id}
                    onValueChange={(value) => setGrant(index, { ...grant, id: value })}
                    placeholder={grant.principal === "group" ? t_i18n("Select a group...") : t_i18n("Select a user...")}
                  />
                </SettingsField>
                <SettingsField id={`vault-grant-${index}-paths`} label={t_i18n("Path prefixes")} info={pathsInfo}>
                  <TagInput
                    id={`vault-grant-${index}-paths`}
                    aria-labelledby={settingsFieldLabelId(`vault-grant-${index}-paths`)}
                    value={grant.paths}
                    onChange={(paths) => setGrant(index, { ...grant, paths })}
                    placeholder={t_i18n("Every path")}
                    preserveCase
                    suggestFromPlatformTags={false}
                  />
                </SettingsField>
                <Button
                  type="button"
                  variant="ghost"
                  size="icon"
                  aria-label={t_i18n("Remove")}
                  onClick={() => onChange({ ...form, grants: form.grants.filter((_, at) => at !== index) })}
                >
                  <Trash2 className="h-4 w-4" />
                </Button>
              </div>
              {missingGrants.has(index) ? (
                <p id={settingsFieldHintId(`vault-grant-${index}`)} role="alert" className="content-compact text-feedback-error-primary">
                  {grant.principal === "group" ? t_i18n("Pick a group, or remove the row.") : t_i18n("Pick a user, or remove the row.")}
                </p>
              ) : null}
            </div>
          ))}
          <div className="flex gap-2">
            <Button
              type="button"
              variant="outline"
              className="gap-1"
              onClick={() => onChange({ ...form, grants: [...form.grants, { principal: "group", id: null, paths: [] }] })}
            >
              <Users className="h-3.5 w-3.5" aria-hidden /> {t_i18n("Add a group")}
            </Button>
            <Button
              type="button"
              variant="outline"
              className="gap-1"
              onClick={() => onChange({ ...form, grants: [...form.grants, { principal: "user", id: null, paths: [] }] })}
            >
              <Plus className="h-3.5 w-3.5" aria-hidden /> {t_i18n("Add a user")}
            </Button>
          </div>
        </div>
      )}
      <SettingsSwitchField
        id="vault-browsable"
        className="rounded-sm bg-elevation-hover px-3 py-2"
        label={t_i18n("Browsable")}
        info={t_i18n("On: the picker lists the secret names under the paths a user may use. Off: the user types the path.")}
        helper={t_i18n(
          "Let users who may use it list secret names in the picker. Names can be sensitive: without it, they type the path, still checked against their grant.",
        )}
        checked={form.browsable}
        onCheckedChange={(checked) => onChange({ ...form, browsable: checked })}
      />
    </div>
  );
}

function ProviderDialog({
  open,
  provider,
  defaultCacheSeconds,
  onClose,
}: {
  open: boolean;
  /** The provider edited; `null` creates one. */
  provider: SecretVaultProvider | null;
  defaultCacheSeconds: number;
  onClose: () => void;
}) {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const create = useCreateSecretVault();
  const update = useUpdateSecretVault();
  const test = useTestSecretVault();
  const [form, setForm] = useState<VaultForm>(() => (provider ? vaultFormFromProvider(provider) : emptyVaultForm()));
  const [nameTouched, setNameTouched] = useState(Boolean(provider));
  const [report, setReport] = useState<SecretVaultConnectionReport | null>(null);
  const editing = Boolean(provider);

  useEffect(() => {
    if (!open) return;
    setForm(provider ? vaultFormFromProvider(provider) : emptyVaultForm());
    setNameTouched(Boolean(provider));
    setReport(null);
    test.reset();
    // `test` is a stable mutation object.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, provider]);

  const problems = vaultFormProblems(form, provider);
  const missing = missingNames(t_i18n, form, problems);
  const fields = visibleFields(form.type, form.method);
  const settingFields = fields.filter((field) => !field.credential);
  const credentialFields = fields.filter((field) => field.credential);
  const nameLocked = editing && (provider?.usage_count ?? 0) > 0;
  const saving = create.isPending || update.isPending;

  const setType = (type: SecretVaultType) =>
    setForm({ ...emptyVaultForm(type), name: form.name, displayName: form.displayName, access: form.access, grants: form.grants, everyonePaths: form.everyonePaths, browsable: form.browsable });

  const runTest = () => {
    const body = vaultInputFromForm(form);
    test.mutate(
      { kind: "draft", body: { ...body, name: PROVIDER_NAME_RE.test(body.name) ? body.name : "draft", display_name: body.display_name || "draft", provider_id: provider?.id ?? null } },
      { onSuccess: setReport },
    );
  };

  const save = () => {
    const body = vaultInputFromForm(form);
    const done = (saved: SecretVaultProvider) => {
      toast({ variant: "success", title: editing ? t_i18n("Vault updated") : t_i18n("Vault connected") });
      onClose();
      // A fresh status, from the settings as saved.
      if (saved.id && saved.enabled) test.mutate({ kind: "stored", id: saved.id });
    };
    const failed = (error: Error) => toast({ variant: "error", title: t_i18n("Could not save the vault"), description: error.message });
    if (provider?.id) {
      const { type: _type, ...changes } = body;
      update.mutate(
        { id: provider.id, body: { ...changes, ...(body.cache_seconds === null ? { clear_cache_seconds: true } : {}) } },
        { onSuccess: done, onError: failed },
      );
    } else {
      create.mutate(body, { onSuccess: done, onError: failed });
    }
  };

  const methodHelp = VAULT_METHOD_HELP[form.method as keyof typeof VAULT_METHOD_HELP];

  return (
    <Dialog open={open} onOpenChange={(next) => !next && onClose()}>
      <DialogContent className="max-w-3xl">
        <DialogHeader>
          <DialogTitle>{editing ? t_i18n("Edit {name}", { name: provider?.display_name ?? "" }) : t_i18n("Connect a vault")}</DialogTitle>
          <DialogDescription>
            {t_i18n("The platform reads secrets with this vault's identity, for the people you grant it to. Its credentials are stored encrypted and never shown again.")}
          </DialogDescription>
        </DialogHeader>

        {/* The fields scroll, the header and the actions stay in view. The
            4px inset keeps the fields' focus ring clear of the scroll clip. */}
        <DialogBody className="-m-1 space-y-5 p-1">
          {!editing ? (
            <div className="grid gap-2 sm:grid-cols-2" role="radiogroup" aria-label={t_i18n("Vault type")}>
              {VAULT_TYPES.map((type) => (
                <button
                  key={type}
                  type="button"
                  role="radio"
                  aria-checked={form.type === type}
                  onClick={() => setType(type)}
                  className={cn(
                    "flex items-start gap-3 rounded-sm border px-3 py-2.5 text-left transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus",
                    form.type === type ? "border-accent/50 bg-accent/10" : "border-elevation-subtle hover:bg-elevation-hover",
                  )}
                >
                  <VaultTypeLogo type={type} className="mt-0.5 h-6 w-6" />
                  <span>
                    <span className="block content-compact font-medium">{label(t_i18n, VAULT_TYPE_LABELS, type)}</span>
                    <span className="block content-compact text-default-secondary">{label(t_i18n, VAULT_TYPE_DESCRIPTIONS, type)}</span>
                  </span>
                </button>
              ))}
            </div>
          ) : (
            <div className="flex items-center gap-2">
              <VaultTypeLogo type={form.type} className="h-5 w-5" />
              <span className="content-compact font-medium">{label(t_i18n, VAULT_TYPE_LABELS, form.type)}</span>
            </div>
          )}

          <div className="grid gap-3 sm:grid-cols-2">
            <SettingsField
              id="vault-display-name"
              label={t_i18n("Display name")}
              info={t_i18n("The name people see in the vault list and the secret picker. Typing it fills the reference name until you change that one.")}
              helper={t_i18n("Shown in the vault list and the secret picker.")}
            >
              <Input
                id="vault-display-name"
                aria-labelledby={settingsFieldLabelId("vault-display-name")}
                aria-describedby={settingsFieldHintId("vault-display-name")}
                value={form.displayName}
                onChange={(event) => {
                  const displayName = event.target.value;
                  setForm({ ...form, displayName, ...(nameTouched ? {} : { name: slugOf(displayName) }) });
                }}
                placeholder={t_i18n("Corporate Vault")}
              />
            </SettingsField>
            <SettingsField
              id="vault-reference-name"
              label={t_i18n("Reference name")}
              required
              info={t_i18n("The name secret fields use to reach this vault. Lowercase letters, digits and _, starting with a letter, 32 at most. It cannot change while a secret field references it.")}
              error={form.name && !PROVIDER_NAME_RE.test(form.name) ? t_i18n("Lowercase letters, digits and _, starting with a letter (32 at most).") : undefined}
              helper={
                nameLocked
                  ? t_i18n("Secret fields reference this name: it cannot change while they do.")
                  : t_i18n("References look like vault://{name}/path#key.", { name: form.name || "name" })
              }
            >
              <Input
                id="vault-reference-name"
                aria-labelledby={settingsFieldLabelId("vault-reference-name")}
                aria-describedby={settingsFieldHintId("vault-reference-name")}
                aria-required
                value={form.name}
                onChange={(event) => {
                  setNameTouched(true);
                  setForm({ ...form, name: event.target.value });
                }}
                disabled={nameLocked}
                className="font-mono-plex"
                placeholder="corp"
              />
            </SettingsField>
          </div>

          <section className="space-y-3">
            <h3 className="title-xs">{t_i18n("Connection")}</h3>
            <div className="space-y-1.5">
              <div className="flex items-center gap-1.5">
                <Label id={settingsFieldLabelId("vault-method")} htmlFor="vault-method" className="flex items-center gap-1">
                  {t_i18n("Sign-in method")}
                  <span className="text-feedback-error-primary" aria-label={t_i18n("Required")}>
                    *
                  </span>
                </Label>
                <InfoTip
                  content={
                    <div className="max-w-xs space-y-1 content-compact">
                      {t_i18n("How the platform signs in to the vault. An identity of the platform stores nothing; a token or a key is stored encrypted and must be rotated by hand.")}
                    </div>
                  }
                  label={t_i18n("About this field")}
                />
              </div>
              <SearchableSelect
                id="vault-method"
                aria-labelledby={settingsFieldLabelId("vault-method")}
                aria-describedby={methodHelp ? "vault-method-help" : undefined}
                searchable={false}
                sort={false}
                options={VAULT_AUTH_METHODS[form.type].map((method) => ({
                  value: method,
                  label: label(t_i18n, VAULT_METHOD_LABELS, method),
                  description:
                    method === VAULT_AUTH_METHODS[form.type][0] && VAULT_AUTH_METHODS[form.type].length > 1 && form.type !== "hashicorp"
                      ? t_i18n("Recommended")
                      : undefined,
                }))}
                value={form.method}
                onValueChange={(method) => setForm({ ...form, method: method || VAULT_DEFAULT_METHOD[form.type] })}
              />
              {methodHelp && (
                <p
                  id="vault-method-help"
                  className={cn(
                    "content-compact",
                    STATIC_KEY_METHODS.has(form.method) ? "text-feedback-warning-primary" : "text-default-secondary",
                  )}
                >
                  {STATIC_KEY_METHODS.has(form.method) && <AlertTriangle className="mr-1 inline h-3 w-3" aria-hidden />}
                  {t_i18n(methodHelp)}
                </p>
              )}
            </div>
            <div className="grid gap-3 sm:grid-cols-2">
              {settingFields.map((field) => (
                <div key={field.key} className={cn(field.kind === "textarea" && "sm:col-span-2")}>
                  <SettingInput field={field} form={form} onChange={setForm} mounts={report?.mounts ?? null} />
                </div>
              ))}
              {credentialFields.map((field) => (
                <div key={field.key} className={cn(field.kind === "secret-file" && "sm:col-span-2")}>
                  <CredentialInput field={field} form={form} stored={provider} onChange={setForm} />
                </div>
              ))}
            </div>
            <div className="flex flex-wrap items-start gap-3">
              <Button type="button" variant="outline" onClick={runTest} disabled={test.isPending} className="gap-1" aria-busy={test.isPending || undefined}>
                {test.isPending && <Spinner size="sm" tone="inherit" />}{!test.isPending && <KeyRound className="h-3.5 w-3.5" aria-hidden />}
                {t_i18n("Test connection")}
              </Button>
              {report && <ConnectionReportView report={report} />}
              {test.isError && <p className="content-compact text-feedback-error-primary">{test.error.message}</p>}
            </div>
          </section>

          <section className="space-y-3">
            <h3 className="title-xs">{t_i18n("Who may use it")}</h3>
            <GrantsEditor form={form} problems={problems} onChange={setForm} />
          </section>

          <section className="grid gap-3 sm:grid-cols-2">
            <SettingsField
              id="vault-cache-seconds"
              label={t_i18n("Cache duration (seconds)")}
              info={t_i18n("A shorter cache sees a rotated secret sooner and reads the vault more often. A whole number of seconds, 86400 at most.")}
              helper={t_i18n("How long a value read from this vault stays in memory. Empty: the platform default.")}
              error={problems.some((problem) => problem.kind === "cache") ? t_i18n("A whole number of seconds, 86400 at most.") : undefined}
            >
              <Input
                id="vault-cache-seconds"
                aria-labelledby={settingsFieldLabelId("vault-cache-seconds")}
                aria-describedby={settingsFieldHintId("vault-cache-seconds")}
                value={form.cacheSeconds}
                onChange={(event) => setForm({ ...form, cacheSeconds: event.target.value })}
                placeholder={String(defaultCacheSeconds)}
                inputMode="numeric"
              />
            </SettingsField>
            <SettingsSwitchField
              id="vault-enabled"
              className="self-start rounded-sm bg-elevation-hover px-3 py-2"
              label={t_i18n("Enabled")}
              helper={t_i18n("Secret fields read from this vault while it is on.")}
              info={t_i18n("Off: no secret is read from this vault and the fields that reference it fail until it is on again.")}
              checked={form.enabled}
              onCheckedChange={(enabled) => setForm({ ...form, enabled })}
            />
          </section>
        </DialogBody>

        <DialogFooter>
          {missing.length > 0 && (
            <span className="mr-auto content-compact text-default-secondary">{t_i18n("Missing: {fields}", { fields: missing.join(", ") })}</span>
          )}
          <Button type="button" variant="secondary" onClick={onClose}>
            {t_i18n("Cancel")}
          </Button>
          <Button type="button" onClick={save} disabled={problems.length > 0 || saving}>
            {saving && <Spinner size="sm" tone="inherit" />}
            {editing ? t_i18n("Save") : t_i18n("Connect")}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

// ---------------------------------------------------------------------------
// The list
// ---------------------------------------------------------------------------

function ProviderRow({
  provider,
  onEdit,
  onShowUses,
}: {
  provider: SecretVaultProvider;
  onEdit: () => void;
  onShowUses: () => void;
}) {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const update = useUpdateSecretVault();
  const remove = useDeleteSecretVault();
  const test = useTestSecretVault();
  const [ConfirmEl, confirm] = useConfirm();
  const fromEnvironment = provider.source === "environment";

  const runTest = () =>
    test.mutate(fromEnvironment ? { kind: "environment", name: provider.name } : { kind: "stored", id: provider.id ?? "" }, {
      onError: (error) => toast({ variant: "error", title: t_i18n("Could not test the vault"), description: error.message }),
    });

  const onDelete = async () => {
    if (!provider.id) return;
    const ok = await confirm({
      title: t_i18n("Delete {name}?", { name: provider.display_name }),
      description: t_i18n("Secret fields can no longer pick from it. A vault a secret field reads from cannot be deleted."),
      confirmLabel: t_i18n("Delete"),
      variant: "danger",
    });
    if (!ok) return;
    remove.mutate(provider.id, {
      onSuccess: () => toast({ variant: "success", title: t_i18n("Vault deleted") }),
      onError: (error) => {
        toast({ variant: "error", title: t_i18n("Could not delete the vault"), description: error.message });
        if ((error as { status?: number }).status === 409) onShowUses();
      },
    });
  };

  return (
    <div className="space-y-2 rounded-sm border border-elevation-subtle px-4 py-3" data-testid="secret-vault-row" data-name={provider.name}>
      {ConfirmEl}
      <div className="flex flex-wrap items-center gap-3">
        <VaultTypeLogo type={provider.type} className="h-6 w-6" />
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <span className="content-base font-medium">{provider.display_name}</span>
            <StatusBadge provider={provider} />
            {fromEnvironment && (
              <Tip content={t_i18n("Declared by SECRET_VAULT_{name}_* in the platform's environment: change it there and restart.", { name: provider.name.toUpperCase() })}>
                <Badge variant="outline" className="gap-1 content-caption">
                  <Lock className="h-3 w-3" aria-hidden /> {t_i18n("Read-only")}
                </Badge>
              </Tip>
            )}
          </div>
          <p className="content-compact text-default-secondary">
            <span className="font-mono-plex">vault://{provider.name}/…</span>
            {" · "}
            {label(t_i18n, VAULT_TYPE_LABELS, provider.type)}
            {provider.last_check_at && (
              <>
                {" · "}
                {t_i18n("Tested {when}", { when: formatShortDateTime(provider.last_check_at) })}
              </>
            )}
            {provider.updated_by_name && (
              <>
                {" · "}
                {t_i18n("Changed by {name}", { name: provider.updated_by_name })}
              </>
            )}
          </p>
          {fromEnvironment && (
            <p className="content-compact text-default-secondary">
              {t_i18n("Writers: {writers}. Paths: {paths}.", {
                writers: provider.writers === "all" ? t_i18n("everyone") : t_i18n("administrators"),
                paths: provider.paths.length ? provider.paths.join(", ") : t_i18n("every path"),
              })}
            </p>
          )}
          {!fromEnvironment && (
            <p className="content-compact text-default-secondary">
              {provider.grants.length === 0
                ? t_i18n("Administrators only")
                : provider.grants
                    .map((grant) =>
                      grant.principal === "everyone"
                        ? t_i18n("Everyone")
                        : grant.label || grant.id || "",
                    )
                    .join(", ")}
              {provider.browsable ? ` · ${t_i18n("Browsable")}` : ""}
            </p>
          )}
        </div>
        <Button type="button" variant="ghost" size="sm" onClick={onShowUses} className="gap-1" data-testid="secret-vault-usage">
          {t_i18n("{count, plural, one {# secret field} other {# secret fields}}", { count: provider.usage_count })}
        </Button>
        <Button type="button" variant="outline" size="sm" onClick={runTest} disabled={test.isPending} className="gap-1" aria-busy={test.isPending || undefined}>
          {test.isPending && <Spinner size="sm" tone="inherit" />}{!test.isPending && <KeyRound className="h-3.5 w-3.5" aria-hidden />}
          {t_i18n("Test")}
        </Button>
        {!fromEnvironment && (
          <>
            <Tip content={provider.enabled ? t_i18n("Disable") : t_i18n("Enable")}>
              <Switch
                aria-label={t_i18n("Enabled")}
                checked={provider.enabled}
                disabled={update.isPending}
                onCheckedChange={(enabled) =>
                  provider.id &&
                  update.mutate(
                    { id: provider.id, body: { enabled } },
                    { onError: (error) => toast({ variant: "error", title: t_i18n("Could not save the vault"), description: error.message }) },
                  )
                }
              />
            </Tip>
            <Button type="button" variant="ghost" size="icon" aria-label={t_i18n("Edit")} onClick={onEdit}>
              <Pencil className="h-4 w-4" />
            </Button>
            <Button type="button" variant="ghost" size="icon" aria-label={t_i18n("Delete")} onClick={onDelete} disabled={remove.isPending}>
              <Trash2 className="h-4 w-4" />
            </Button>
          </>
        )}
      </div>
      {test.data && <ConnectionReportView report={test.data} />}
    </div>
  );
}

export function SecretVaultsSection() {
  const { t_i18n } = useFormatter();
  const { data, isLoading } = useSecretVaults();
  const [editing, setEditing] = useState<SecretVaultProvider | null>(null);
  const [creating, setCreating] = useState(false);
  const [usesOf, setUsesOf] = useState<string | null>(null);
  const providers = useMemo(() => data?.providers ?? [], [data]);

  return (
    <Card>
      <CardHeader className="flex flex-row items-start justify-between gap-4">
        <div>
          <CardTitle className="flex items-center gap-2">
            <KeyRound className="h-5 w-5 text-accent" aria-hidden /> {t_i18n("Secret vaults")}
          </CardTitle>
          <CardDescription>
            {t_i18n(
              "Connect your enterprise vaults and grant them to groups and users: anyone allowed then picks a secret from a vault in any secret field, without typing a reference. Secret values are read when they are used and never stored.",
            )}
          </CardDescription>
        </div>
        <Button type="button" onClick={() => setCreating(true)} className="gap-1 shrink-0" data-testid="secret-vault-connect">
          <Plus className="h-4 w-4" aria-hidden /> {t_i18n("Connect a vault")}
        </Button>
      </CardHeader>
      <CardContent className="space-y-3">
        {isLoading ? (
          <Spinner size="md" tone="inherit" className="text-default-secondary" label={t_i18n("Loading...")} />
        ) : providers.length === 0 ? (
          // No CTA of its own: the header's "Connect a vault" is the one action.
          <EmptyState
            variant="inset"
            icon={KeyRound}
            title={t_i18n("No vault connected")}
            description={t_i18n("Connect HashiCorp Vault, AWS Secrets Manager, Google Cloud Secret Manager, Azure Key Vault or 1Password.")}
          />
        ) : (
          providers.map((provider) => (
            <ProviderRow
              key={`${provider.source}:${provider.name}`}
              provider={provider}
              onEdit={() => setEditing(provider)}
              onShowUses={() => setUsesOf(provider.name)}
            />
          ))
        )}
      </CardContent>
      <ProviderDialog
        open={creating || editing !== null}
        provider={editing}
        defaultCacheSeconds={data?.default_cache_seconds ?? 300}
        onClose={() => {
          setCreating(false);
          setEditing(null);
        }}
      />
      <UsageDialog name={usesOf} onClose={() => setUsesOf(null)} />
    </Card>
  );
}
