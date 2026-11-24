// SPDX-License-Identifier: Apache-2.0

/**
 * Delete cases, administrators only (#1723, #1950, dev-docs/ctem-cases.md
 * "Admin delete of cases"): from the Cases list (toolbar and row) and from the
 * case page's More actions menu. The click first asks the server what would go
 * (POST /ctem-cases/bulk-delete with `dry_run`), then the confirm dialog names the
 * count and the summary: the open work items cancelled, the timers cancelled, the
 * signals sent back to the scope gate. The delete itself is typed back with the
 * challenge code, since it cannot be undone. A non-administrator never sees it.
 *
 * `useCaseDelete` holds the flow (dry run, dialog, delete, toast) for any trigger;
 * `CasesDeleteAction` draws the list's triggers: `row` the icon button of one list
 * row, without it the toolbar button of the selection.
 */
import { useState, type ReactNode } from "react";
import { Icon } from "@filigran/design-system";
import { Button } from "@/components/ui/button";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { TOOLBAR_CONTROL_HEIGHT } from "@/components/ui/data-table";
import { useToast } from "@/components/ui/toast";
import { Tip } from "@/components/ui/tooltip";
import { useDeleteCtemCases, type CtemCaseDeleteResult } from "@/hooks/api/useCtemCasesApi";
import { useAuth } from "@/hooks/useAuth";
import { useFormatter } from "@/i18n";
import { deletePreviewTotals } from "./cases-delete";

export interface CaseDelete {
  /** False for a non-administrator: draw no trigger. */
  allowed: boolean;
  /** Opens the dialog and asks the server for the dry run. */
  ask: () => void;
  /** The confirm dialog, rendered once by the caller. */
  dialog: ReactNode;
}

export function useCaseDelete(caseIds: string[], onDone: () => void): CaseDelete {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const { isAdmin } = useAuth();
  const remove = useDeleteCtemCases();
  const [preview, setPreview] = useState<CtemCaseDeleteResult | null>(null);
  const [failure, setFailure] = useState<string | null>(null);
  const [open, setOpen] = useState(false);

  const ask = () => {
    setPreview(null);
    setFailure(null);
    setOpen(true);
    remove.mutate(
      { case_ids: caseIds, dry_run: true },
      { onSuccess: (result) => setPreview(result), onError: (err) => setFailure(err.message) },
    );
  };

  const confirm = () =>
    remove.mutate(
      { case_ids: caseIds, dry_run: false },
      {
        onSuccess: (result) => {
          toast({ title: t_i18n("{count, plural, one {# case deleted} other {# cases deleted}}", { count: result.count }), variant: "success" });
          onDone();
        },
        // The server's reason, never a silent failure (#1950).
        onError: (err) => toast({ title: t_i18n("Delete failed"), description: err.message, variant: "error" }),
      },
    );

  const count = caseIds.length;
  const totals = preview ? deletePreviewTotals(preview) : null;

  const dialog = !isAdmin ? null : (
    <ConfirmDialog
      open={open}
      onOpenChange={setOpen}
      title={t_i18n("{count, plural, one {Delete # case?} other {Delete # cases?}}", { count })}
      description={t_i18n(
        "Each case goes with its issues list and history. Its signals go back to the scope gate, and the next case for the same fix opens fresh. This cannot be undone.",
      )}
      confirmLabel={t_i18n("Delete")}
      challenge
      confirmDisabled={!preview || preview.count === 0}
      onConfirm={confirm}
    >
      <div className="space-y-1 content-compact" aria-busy={!preview && !failure} data-testid="ctem-cases-delete-preview">
        {failure ? (
          <p className="text-feedback-error-primary" role="alert">
            {failure}
          </p>
        ) : !preview || !totals ? (
          <p className="text-default-secondary">{t_i18n("Reading what the delete removes")}</p>
        ) : (
          <ul className="list-disc space-y-0.5 pl-5">
            <li>{t_i18n("{count, plural, one {# case and its history} other {# cases and their history}}", { count: preview.count })}</li>
            <li>{t_i18n("{count, plural, one {# open work item cancelled} other {# open work items cancelled}}", { count: totals.workItems })}</li>
            <li>{t_i18n("{count, plural, one {# timer cancelled} other {# timers cancelled}}", { count: totals.timers })}</li>
            <li>{t_i18n("{count, plural, one {# signal sent back to the scope gate} other {# signals sent back to the scope gate}}", { count: totals.signals })}</li>
          </ul>
        )}
      </div>
    </ConfirmDialog>
  );

  return { allowed: isAdmin, ask, dialog };
}

export function CasesDeleteAction({ caseIds, onDone, row = false }: { caseIds: string[]; onDone: () => void; row?: boolean }) {
  const { t_i18n } = useFormatter();
  const remove = useCaseDelete(caseIds, onDone);

  if (!remove.allowed) return null;

  return (
    <>
      {row ? (
        <Tip content={t_i18n("Delete the case")}>
          <Button
            variant="ghost"
            size="icon"
            className="h-8 w-8"
            aria-label={t_i18n("Delete the case")}
            onClick={remove.ask}
            data-testid="ctem-cases-row-delete"
          >
            <Icon name="trash-2" size={16} aria-hidden />
          </Button>
        </Tip>
      ) : (
        <Button variant="destructive" size="sm" className={TOOLBAR_CONTROL_HEIGHT} onClick={remove.ask} data-testid="ctem-cases-bulk-delete">
          {t_i18n("Delete")}
        </Button>
      )}
      {remove.dialog}
    </>
  );
}
