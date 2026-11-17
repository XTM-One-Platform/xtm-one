// SPDX-License-Identifier: Apache-2.0

/**
 * Secrets stored as vault references.
 *
 * - `VaultSecretsNotice` - on an entity: which secret fields are read from a
 *   vault or from a secret variable, the last failed read of each
 *   (field, provider, reason - never a path or a value), and the fields whose
 *   stored reference does not parse.
 *
 * A secret field itself is a `SecretField` (`secret-field.tsx`), which
 * picks a reference from a vault instead of having one typed.
 */
import type { ReactNode } from "react";
import { AlertTriangle, KeyRound, Variable as VariableIcon } from "lucide-react";
import { Badge, BadgeFocusTrigger } from "@/components/ui/badge";
import { Tip } from "@/components/ui/tooltip";
import { useVariables } from "@/hooks/api/useVariableApi";
import { useCustomVariablesEnabled } from "@/hooks/usePlatformConfig";
import { useFormatter } from "@/i18n";
import { VAULT_FAILURE_REASONS } from "@/lib/secret-vaults";
import { cn, formatShortDateTime } from "@/lib/utils";
import type { VaultSecretStatus } from "@/types";

/** The secret fields that take a reference, read by `t_i18n(VAULT_FIELD_LABELS[field])`. @i18n-keys */
const VAULT_FIELD_LABELS = {
  api_key: "API key",
  client_secret: "Client secret",
  headers: "Headers",
  env: "Environment variables",
  value: "Value",
  oidc_client_secret: "Client secret",
  public_key: "Public Key",
  secret_key: "Secret Key",
  credentials: "Sign-in credentials",
} as const;

type FieldLabels = Partial<Record<string, string>>;

/**
 * One reference badge: a tab stop with a visible focus ring, so its tooltip
 * opens on focus as on hover (`Tip` opens on focus, Escape closes it).
 */
function ReferenceBadge({ tip, children }: { tip: string; children: ReactNode }) {
  return (
    <Tip content={tip}>
      <BadgeFocusTrigger data-testid="secret-reference-badge">
        <Badge variant="outline" className="content-caption font-mono-plex">
          {children}
        </Badge>
      </BadgeFocusTrigger>
    </Tip>
  );
}

/**
 * A row: icon and label on the first line, the badges wrapping among themselves, never under the icon.
 * The label group is as tall as a badge (`min-h-6`, 24 px) so label and badge text share one centre line.
 */
function ReferenceRow({ icon, label, children, testId }: { icon: ReactNode; label: string; children: ReactNode; testId?: string }) {
  return (
    <div className="flex items-start gap-1.5 content-compact text-default-secondary" data-testid={testId}>
      <span className="flex shrink-0 items-center gap-1.5 min-h-6">
        {icon}
        <span>{label}</span>
      </span>
      <span className="flex min-w-0 flex-wrap items-center gap-1.5">{children}</span>
    </div>
  );
}

function useFieldLabel(fieldLabels?: FieldLabels) {
  const { t_i18n } = useFormatter();
  return (field: string): string => {
    const override = fieldLabels?.[field];
    if (override) return override;
    const known = VAULT_FIELD_LABELS[field as keyof typeof VAULT_FIELD_LABELS];
    return known ? t_i18n(known) : field;
  };
}

export function VaultSecretsNotice({
  status,
  fieldLabels,
  className,
}: {
  status?: VaultSecretStatus | null;
  /** Translated labels for fields whose generic name is wrong here (a channel bot's `api_key` is its token). */
  fieldLabels?: FieldLabels;
  className?: string;
}) {
  const { t_i18n } = useFormatter();
  const labelOf = useFieldLabel(fieldLabels);
  const references = Object.entries(status?.references ?? {});
  const variableRefs = Object.entries(status?.variables ?? {});
  const variablesEnabled = useCustomVariablesEnabled();
  // Names only, of the variables this viewer may see; any other is shown by its id.
  const { data: variables } = useVariables({ enabled: variablesEnabled && variableRefs.length > 0 });
  const invalid = status?.invalid_references ?? [];
  if (references.length === 0 && invalid.length === 0 && variableRefs.length === 0) return null;
  // While the list loads, a placeholder: never the raw id, never a wrong "cannot see".
  const variableName = (id: string) => {
    if (variablesEnabled && variables === undefined) return "…";
    return variables?.find((variable) => variable.id === id)?.name ?? t_i18n("a variable you cannot see");
  };
  const failures = status?.failures ?? [];
  // A secret variable that cannot be read is no vault failure: its own copy, no raw provider.
  const isVariableFailure = (failure: { provider: string; reason: string }) => failure.reason === "variable_unavailable";
  const variableFailures = failures.filter(isVariableFailure).length;
  const vaultFailures = failures.length - variableFailures;
  const failing = invalid.length + failures.length;

  return (
    <div className={cn("space-y-2", className)} data-testid="vault-secrets-notice">
      {references.length > 0 && (
        <ReferenceRow icon={<KeyRound className="h-3.5 w-3.5 text-accent shrink-0" />} label={t_i18n("Read from a vault:")}>
          {references.map(([field, providers]) => (
            <ReferenceBadge
              key={field}
              tip={t_i18n("Read from the vault provider \"{provider}\" each time it is used. The value is never stored or shown here.", { provider: providers.join(", ") })}
            >
              {labelOf(field)} · {providers.join(", ")}
            </ReferenceBadge>
          ))}
        </ReferenceRow>
      )}
      {variableRefs.length > 0 && (
        <ReferenceRow icon={<VariableIcon className="h-3.5 w-3.5 text-accent shrink-0" />} label={t_i18n("Read from a variable:")} testId="variable-secrets">
          {variableRefs.map(([field, ids]) => (
            <ReferenceBadge
              key={field}
              tip={t_i18n("Read from the secret variable each time it is used, as the integration's owner sees it. The value is never stored or shown here.")}
            >
              {labelOf(field)} · {ids.map(variableName).join(", ")}
            </ReferenceBadge>
          ))}
        </ReferenceRow>
      )}
      {failing > 0 && (
        <div className="rounded-sm border border-feedback-alert-primary/30 bg-feedback-alert-primary/5 p-3 space-y-1.5" role="alert">
          <div className="flex items-center gap-2">
            <AlertTriangle className="h-4 w-4 text-feedback-warning-primary shrink-0" />
            <p className="content-compact font-medium text-feedback-warning-primary">
              {variableFailures > 0
                ? t_i18n("{count, plural, one {A secret could not be read} other {# secrets could not be read}}", { count: failing })
                : t_i18n("{count, plural, one {A secret could not be read from its vault} other {# secrets could not be read from their vault}}", { count: failing })}
            </p>
          </div>
          <ul className="space-y-1">
            {invalid.map((field) => (
              <li key={`invalid:${field}`} className="content-caption text-default-secondary" data-testid="vault-invalid-reference">
                <span className="font-medium">{labelOf(field)}</span>: {t_i18n(VAULT_FAILURE_REASONS.invalid_reference)}
              </li>
            ))}
            {failures.map((failure) => {
              const reason = VAULT_FAILURE_REASONS[failure.reason as keyof typeof VAULT_FAILURE_REASONS];
              return (
                <li key={`failure:${failure.field}`} className="content-caption text-default-secondary">
                  <span className="font-medium">{labelOf(failure.field)}</span>
                  {isVariableFailure(failure) ? "" : ` (${failure.provider})`}:{" "}
                  {reason ? t_i18n(reason) : failure.description || failure.reason}{" "}
                  <span className="text-default-secondary">{formatShortDateTime(failure.at)}</span>
                </li>
              );
            })}
          </ul>
          {vaultFailures > 0 && (
            <p className="content-caption text-default-secondary">
              {t_i18n("Calls that need it fail until the vault answers again. An administrator finds the details in the server logs and in the Security Audit.")}
            </p>
          )}
          {variableFailures > 0 && (
            <p className="content-caption text-default-secondary" data-testid="variable-failure-hint">
              {t_i18n("Calls that need it fail until the credential references a secret variable its owner can read.")}
            </p>
          )}
        </div>
      )}
    </div>
  );
}
