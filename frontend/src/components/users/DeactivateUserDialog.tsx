// SPDX-License-Identifier: Apache-2.0

/**
 * The administrator's "Disable" confirmation (#122): before the cascade runs,
 * it reads `GET /users/{id}/deactivation-preview` and lists what will happen
 * to the account's keys, live work, agents and shared objects, and who takes
 * the shared objects over. A refusal the backend would answer (platform admin,
 * yourself, already deactivated) is shown and the confirm button stays off.
 */
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { useDeactivationPreview } from "@/hooks/api/useProvisioningApi";
import { useFormatter } from "@/i18n";
import { PREVIEW_LINES, REFUSAL_LABELS } from "@/lib/provisioning-labels";
import { Spinner } from "@filigran/design-system";

const REFUSALS: Readonly<Record<string, string>> = REFUSAL_LABELS;

interface Props {
  userId: string;
  userName: string;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onConfirm: () => void;
}

export function DeactivateUserDialog({ userId, userName, open, onOpenChange, onConfirm }: Props) {
  const { t_i18n } = useFormatter();
  const { data: preview, isLoading, isError } = useDeactivationPreview(userId, open);
  const successor = preview?.successor?.name ?? "";
  const lines = preview
    ? (Object.keys(PREVIEW_LINES) as (keyof typeof PREVIEW_LINES)[])
        .filter((key) => (preview.counts[key] ?? 0) > 0)
        .map((key) => ({ key, text: t_i18n(PREVIEW_LINES[key], { count: preview.counts[key], successor }) }))
    : [];
  const refusal = preview?.refusal ?? null;

  return (
    <ConfirmDialog
      open={open}
      onOpenChange={onOpenChange}
      title={t_i18n("Disable {name}?", { name: userName })}
      description={t_i18n("The account can no longer sign in and everything it runs stops now. Enabling it again restores its agents and assignments; revoked keys and stopped runs stay ended.")}
      confirmLabel={t_i18n("Disable account")}
      variant="danger"
      confirmDisabled={!preview || !!refusal}
      onConfirm={onConfirm}
    >
      <div className="space-y-2" data-testid="deactivation-preview">
        {isLoading && (
          <p className="flex items-center gap-2 content-compact text-default-secondary">
            <Spinner size="sm" tone="inherit" />
            {t_i18n("Reading what the account owns")}
          </p>
        )}
        {isError && (
          <p className="content-compact text-feedback-error-primary">{t_i18n("The preview could not be read. Try again.")}</p>
        )}
        {refusal && (
          <p className="content-compact text-feedback-error-primary" data-testid="deactivation-refusal">
            {t_i18n(REFUSALS[refusal] ?? "The account cannot be disabled")}
          </p>
        )}
        {preview && !refusal && (
          <>
            <p className="content-compact font-medium text-default-primary">{t_i18n("What happens")}</p>
            {lines.length === 0 ? (
              <p className="content-compact text-default-secondary">{t_i18n("The account owns nothing that runs or is shared: only its sign-in stops.")}</p>
            ) : (
              <ul className="list-disc space-y-1 pl-5 content-compact text-default-secondary">
                {lines.map((line) => (
                  <li key={line.key}>{line.text}</li>
                ))}
              </ul>
            )}
            {preview.kept_without_successor > 0 && (
              <p className="content-compact text-feedback-warning-tertiary">
                {t_i18n(
                  "{count, plural, one {# shared object stays with the account: no other active administrator can take it over.} other {# shared objects stay with the account: no other active administrator can take them over.}}",
                  { count: preview.kept_without_successor },
                )}
              </p>
            )}
            <p className="content-compact text-default-secondary">
              {t_i18n("Every administrator is notified and the moved objects are listed for review in Settings > Access and security > Provisioning.")}
            </p>
          </>
        )}
      </div>
    </ConfirmDialog>
  );
}
