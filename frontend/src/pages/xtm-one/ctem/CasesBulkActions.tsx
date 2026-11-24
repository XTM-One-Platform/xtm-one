// SPDX-License-Identifier: Apache-2.0

/**
 * The bulk actions of the Cases list (dev-docs/ctem-cases.md section 7.2, S5c of
 * #11545): Assign owner, Set due date, Accept risk, Merge, then Start as the
 * filled primary. Each goes through POST /ctem-cases/bulk (or /merge), so every
 * case keeps the rules of a single change: accepted risk needs an owner, a
 * reason and an expiry; a refused case is named with its rule, never dropped.
 *
 * `only` draws one of the actions alone ("assign" or "due") for one case, with the
 * same label and the same dialog: the next action of the "Act on this first" band
 * (#1176, UX round 1 on #1182, B3).
 */
import { useState } from "react";
import { Textarea } from "@filigran/design-system";
import { Button } from "@/components/ui/button";
import { TOOLBAR_CONTROL_HEIGHT } from "@/components/ui/data-table";
import { DateFilterInput } from "@/components/ui/date-filter-input";
import { Dialog, DialogBody, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { SettingsField, settingsFieldHintId, settingsFieldLabelId } from "@/components/ui/settings-form";
import { useToast } from "@/components/ui/toast";
import { useMe, useUserDirectory } from "@/hooks/api/useAuthApi";
import {
  useBulkUpdateCtemCases,
  useMergeCtemCases,
  type CtemCaseBulkResult,
  type CtemCaseSummary,
  type CtemCaseUpdate,
} from "@/hooks/api/useCtemCasesApi";
import { useFormatter } from "@/i18n";
import type { UserDirectoryItem } from "@/types";

type BulkDialog = "assign" | "due" | "accept" | "merge" | null;

const REASON_MAX = 500;

function inDays(days: number): string {
  return new Date(Date.now() + days * 86_400_000).toISOString().slice(0, 10);
}

/** A person search over the non-admin directory: type two letters, pick one. */
function OwnerPicker({ id, value, onChange }: { id: string; value: UserDirectoryItem | null; onChange: (u: UserDirectoryItem | null) => void }) {
  const { t_i18n } = useFormatter();
  const [query, setQuery] = useState("");
  const { data: people } = useUserDirectory(query);
  if (value) {
    return (
      <div className="flex items-center justify-between gap-2 content-base">
        <span className="min-w-0 truncate">{value.full_name || value.email}</span>
        <Button variant="ghost" size="sm" onClick={() => onChange(null)}>
          {t_i18n("Change")}
        </Button>
      </div>
    );
  }
  return (
    <div className="space-y-1">
      <Input
        id={id}
        aria-labelledby={settingsFieldLabelId(id)}
        placeholder={t_i18n("Search a person")}
        value={query}
        onChange={(e) => setQuery(e.target.value)}
      />
      {query.trim().length >= 2 && (
        <ul className="max-h-48 overflow-y-auto rounded-sm border border-elevation-subtle" role="listbox" aria-label={t_i18n("People")}>
          {(people ?? []).length === 0 ? (
            <li className="px-3 py-2 content-compact text-default-secondary">{t_i18n("No match")}</li>
          ) : (
            (people ?? []).map((p) => (
              <li key={p.id} role="option" aria-selected={false}>
                <button
                  type="button"
                  className="w-full px-3 py-2 text-left content-base hover:bg-elevation-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus"
                  onClick={() => onChange(p)}
                >
                  {p.full_name || p.email}
                  <span className="ml-2 text-default-secondary">{p.email}</span>
                </button>
              </li>
            ))
          )}
        </ul>
      )}
    </div>
  );
}

export function CasesBulkActions({
  cases,
  onDone,
  only,
}: {
  cases: CtemCaseSummary[];
  onDone: () => void;
  only?: "assign" | "due";
}) {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const { data: me } = useMe();
  const bulk = useBulkUpdateCtemCases();
  const merge = useMergeCtemCases();
  const [dialog, setDialog] = useState<BulkDialog>(null);
  const [owner, setOwner] = useState<UserDirectoryItem | null>(null);
  const [due, setDue] = useState(inDays(30));
  const [reason, setReason] = useState("");
  const [expiry, setExpiry] = useState(inDays(90));
  const [target, setTarget] = useState("");
  const [formError, setFormError] = useState<string | null>(null);

  const count = cases.length;
  const open = (kind: BulkDialog) => {
    setOwner(null);
    setDue(inDays(30));
    setReason("");
    setExpiry(inDays(90));
    setTarget(cases[0]?.id ?? "");
    setFormError(null);
    setDialog(kind);
  };

  // A refusal of every case is written in the dialog when one is open; Start opens
  // none, so it gets the same lines as an error toast and keeps the selection.
  const report = (result: CtemCaseBulkResult, fromDialog = true) => {
    if (result.failed.length === 0) {
      toast({ title: t_i18n("{count, plural, one {# case updated} other {# cases updated}}", { count: result.updated.length }), variant: "success" });
      setDialog(null);
      onDone();
      return;
    }
    const title = (id: string) => cases.find((c) => c.id === id)?.title ?? id;
    const lines = result.failed.map((f) => `${title(f.id)}: ${f.reason}`).join("\n");
    const refused = t_i18n("{count, plural, one {# case refused} other {# cases refused}}", { count: result.failed.length });
    if (result.updated.length === 0) {
      if (fromDialog) setFormError(lines);
      else toast({ title: refused, description: lines, variant: "error" });
      return;
    }
    toast({ title: refused, description: lines, variant: "info" });
    setDialog(null);
    onDone();
  };

  const apply = (change: CtemCaseUpdate) =>
    bulk.mutate({ ...change, case_ids: cases.map((c) => c.id) }, { onSuccess: (result) => report(result), onError: (err) => setFormError(err.message) });

  // Start keeps an owner already set and gives the caller the unowned ones (the case page rule).
  const start = () => {
    const unowned = cases.filter((c) => !c.owner_user_id).map((c) => c.id);
    const owned = cases.filter((c) => c.owner_user_id).map((c) => c.id);
    const runs: Promise<CtemCaseBulkResult>[] = [];
    if (unowned.length) runs.push(bulk.mutateAsync({ case_ids: unowned, state: "remediating", owner_user_id: me?.id }));
    if (owned.length) runs.push(bulk.mutateAsync({ case_ids: owned, state: "remediating" }));
    Promise.all(runs)
      .then((results) =>
        report({ updated: results.flatMap((r) => r.updated), failed: results.flatMap((r) => r.failed) }, false),
      )
      .catch((err: Error) => toast({ title: t_i18n("Start failed"), description: err.message, variant: "error" }));
  };

  const confirm = () => {
    if (dialog === "assign" && owner) apply({ owner_user_id: owner.id });
    else if (dialog === "due") apply({ due_at: `${due}T23:59:59Z` });
    else if (dialog === "accept" && owner)
      apply({ state: "accepted_risk", owner_user_id: owner.id, reason: reason.trim(), accepted_until: `${expiry}T23:59:59Z` });
    else if (dialog === "merge" && target)
      merge.mutate(
        { target_id: target, case_ids: cases.filter((c) => c.id !== target).map((c) => c.id) },
        {
          onSuccess: () => {
            toast({ title: t_i18n("{count, plural, one {# case merged} other {# cases merged}}", { count: count - 1 }), variant: "success" });
            setDialog(null);
            onDone();
          },
          onError: (err) => setFormError(err.message),
        },
      );
  };

  const titles: Record<Exclude<BulkDialog, null>, string> = {
    assign: t_i18n("Assign owner"),
    due: t_i18n("Set due date"),
    accept: t_i18n("Accept risk"),
    merge: t_i18n("{count, plural, one {Merge # case} other {Merge # cases}}", { count }),
  };
  const pending = bulk.isPending || merge.isPending;
  const canConfirm =
    !pending &&
    ((dialog === "assign" && owner !== null) ||
      (dialog === "due" && Boolean(due)) ||
      (dialog === "accept" && owner !== null && Boolean(reason.trim()) && Boolean(expiry)) ||
      (dialog === "merge" && Boolean(target)));

  return (
    <>
      {only === "assign" && (
        <Button size="sm" variant="ghost" className={TOOLBAR_CONTROL_HEIGHT} onClick={() => open("assign")} data-testid="ctem-cases-single-assign">
          {t_i18n("Assign owner")}
        </Button>
      )}
      {only === "due" && (
        <Button size="sm" variant="ghost" className={TOOLBAR_CONTROL_HEIGHT} onClick={() => open("due")} data-testid="ctem-cases-single-due">
          {t_i18n("Set due date")}
        </Button>
      )}
      {!only && (
        <>
          <Button size="sm" variant="ghost" className={TOOLBAR_CONTROL_HEIGHT} onClick={() => open("assign")} data-testid="ctem-cases-bulk-assign">
            {t_i18n("Assign owner")}
          </Button>
          <Button size="sm" variant="ghost" className={TOOLBAR_CONTROL_HEIGHT} onClick={() => open("due")}>
            {t_i18n("Set due date")}
          </Button>
          <Button size="sm" variant="ghost" className={TOOLBAR_CONTROL_HEIGHT} onClick={() => open("accept")}>
            {t_i18n("Accept risk")}
          </Button>
          <Button
            size="sm"
            variant="ghost"
            className={TOOLBAR_CONTROL_HEIGHT}
            onClick={() => open("merge")}
            disabled={count < 2}
            title={count < 2 ? t_i18n("Select 2 cases or more to merge them") : undefined}
            data-testid="ctem-cases-bulk-merge"
          >
            {t_i18n("Merge")}
          </Button>
          <Button size="sm" className={TOOLBAR_CONTROL_HEIGHT} onClick={start} disabled={pending} data-testid="ctem-cases-bulk-start">
            {t_i18n("Start")}
          </Button>
        </>
      )}

      <Dialog open={dialog !== null} onOpenChange={(o) => !o && setDialog(null)}>
        <DialogContent size="md">
          <DialogHeader>
            <DialogTitle>{dialog ? titles[dialog] : ""}</DialogTitle>
            <DialogDescription>{t_i18n("{count, plural, one {Applies to # case} other {Applies to # cases}}", { count })}</DialogDescription>
          </DialogHeader>
          <DialogBody className="space-y-4">
            {(dialog === "assign" || dialog === "accept") && (
              <SettingsField
                id="bulk-owner"
                label={t_i18n("Owner")}
                helper={t_i18n("The owner must see at least one issue of each case.")}
                required
              >
                <OwnerPicker id="bulk-owner" value={owner} onChange={setOwner} />
              </SettingsField>
            )}
            {dialog === "due" && (
              <SettingsField id="bulk-due" label={t_i18n("Due date")} required>
                <DateFilterInput
                  id="bulk-due"
                  aria-labelledby={settingsFieldLabelId("bulk-due")}
                  min={inDays(0)}
                  value={due}
                  onChange={setDue}
                />
              </SettingsField>
            )}
            {dialog === "accept" && (
              <>
                <SettingsField id="bulk-reason" label={t_i18n("Reason")} required>
                  <Textarea
                    id="bulk-reason"
                    aria-labelledby={settingsFieldLabelId("bulk-reason")}
                    rows={3}
                    maxLength={REASON_MAX}
                    value={reason}
                    onChange={(e) => setReason(e.target.value)}
                  />
                </SettingsField>
                <SettingsField
                  id="bulk-expiry"
                  label={t_i18n("Expires on")}
                  helper={t_i18n("The cases return to New on that day.")}
                  required
                >
                  <DateFilterInput
                    id="bulk-expiry"
                    aria-labelledby={settingsFieldLabelId("bulk-expiry")}
                    aria-describedby={settingsFieldHintId("bulk-expiry")}
                    min={inDays(1)}
                    value={expiry}
                    onChange={setExpiry}
                  />
                </SettingsField>
              </>
            )}
            {dialog === "merge" && (
              <SettingsField
                id="bulk-target"
                label={t_i18n("Case to keep")}
                helper={t_i18n("It keeps its state, owner and history, and takes the issues and history of the others.")}
                required
              >
                <Select value={target} onValueChange={setTarget}>
                  <SelectTrigger id="bulk-target" aria-labelledby={settingsFieldLabelId("bulk-target")}>
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {cases.map((c) => (
                      <SelectItem key={c.id} value={c.id}>
                        {c.title}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </SettingsField>
            )}
            {formError && (
              <p className="whitespace-pre-line content-compact text-feedback-error-primary" role="alert">
                {formError}
              </p>
            )}
          </DialogBody>
          <DialogFooter>
            <Button variant="outline" onClick={() => setDialog(null)}>
              {t_i18n("Cancel")}
            </Button>
            <Button onClick={confirm} disabled={!canConfirm}>
              {dialog ? titles[dialog] : ""}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}
