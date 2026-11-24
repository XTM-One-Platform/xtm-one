// SPDX-License-Identifier: Apache-2.0

/**
 * Delete cases from the Cases list, administrators only (#1723, dev-docs/ctem-cases.md
 * "Admin delete of cases"). The click first asks the server what would go
 * (POST /ctem-cases/bulk-delete with `dry_run`), then the confirm dialog names the
 * count and the summary: the open work items cancelled, the timers cancelled, the
 * signals sent back to the scope gate. The delete itself is typed back with the
 * challenge code, since it cannot be undone. A non-administrator never sees it.
 *
 * `row` draws the icon button of one list row; without it, the toolbar button of
 * the selection.
 */
import { useState } from "react";
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

export function CasesDeleteAction({ caseIds, onDone, row = false }: { caseIds: string[]; onDone: () => void; row?: boolean }) {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const { isAdmin } = useAuth();
  const remove = useDeleteCtemCases();
  const [preview, setPreview] = useState<CtemCaseDeleteResult | null>(null);
  const [failure, setFailure] = useState<string | null>(null);
  const [open, setOpen] = useState(false);

  if (!isAdmin) return null;

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
        onError: (err) => toast({ title: t_i18n("Delete failed"), description: err.message, variant: "error" }),
      },
    );

  const count = caseIds.length;
  const totals = preview ? deletePreviewTotals(preview) : null;
  const label = t_i18n("Delete");

  return (
    <>
      {row ? (
        <Tip content={t_i18n("Delete the case")}>
          <Button
            variant="ghost"
            size="icon"
            className="h-8 w-8"
            aria-label={t_i18n("Delete the case")}
            onClick={ask}
            data-testid="ctem-cases-row-delete"
          >
            <Icon name="trash-2" size={16} aria-hidden />
          </Button>
        </Tip>
      ) : (
        <Button variant="destructive" size="sm" className={TOOLBAR_CONTROL_HEIGHT} onClick={ask} data-testid="ctem-cases-bulk-delete">
          {label}
        </Button>
      )}
      <ConfirmDialog
        open={open}
        onOpenChange={setOpen}
        title={t_i18n("{count, plural, one {Delete # case?} other {Delete # cases?}}", { count })}
        description={t_i18n(
          "Each case goes with its issues list and history. Its signals go back to the scope gate, and the next case for the same fix opens fresh. This cannot be undone.",
        )}
        confirmLabel={label}
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
    </>
  );
}
