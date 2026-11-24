// SPDX-License-Identifier: Apache-2.0

/**
 * The administrator switch that pauses and resumes CTEM case opening (#1951,
 * dev-docs/ctem-cases.md section 4.6). Off: no confirmed signal opens a case,
 * they wait. On again: the waiting signals open their cases at once (the
 * server drains them), and the Cases list fills as they open.
 *
 * A discreet control (Sam, 24 Nov 2026): administrators only, anyone else sees
 * nothing in every state. On Cases it sits in the table toolbar beside Group by
 * and Views, at their 32 px height, border and label; on the Signal sources
 * header it is the same control. Its tooltip is one line, with the waiting
 * count while it is off. `CaseOpeningOffEmptyState` is the Cases empty state
 * while it is off: the standard empty line and one muted line under it.
 */
import { filterPillClass } from "@/components/ui/filter-pill";
import { Switch } from "@/components/ui/switch";
import { useToast } from "@/components/ui/toast";
import { Tip } from "@/components/ui/tooltip";
import { useCtemCaseOpening, useSetCtemCaseOpening } from "@/hooks/api/useCtemCasesApi";
import { useAuth } from "@/hooks/useAuth";
import { useFormatter } from "@/i18n";

/** Turn case opening on or off, with a one-line toast. */
function useToggleCaseOpening() {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const set = useSetCtemCaseOpening();
  const toggle = (on: boolean) =>
    set.mutate(
      { on },
      {
        onSuccess: (result) => toast({ title: result.on ? t_i18n("Case opening on") : t_i18n("Case opening off"), variant: "success" }),
        onError: (err) => toast({ title: t_i18n("Case opening was not changed"), description: err.message, variant: "error" }),
      },
    );
  return { toggle, pending: set.isPending };
}

export function CaseOpeningSwitch({ testId = "ctem-case-opening" }: { testId?: string }) {
  const { t_i18n } = useFormatter();
  const { isAdmin } = useAuth();
  const { data } = useCtemCaseOpening();
  const { toggle, pending } = useToggleCaseOpening();
  if (!isAdmin) return null;
  const id = `${testId}-switch`;
  const tip =
    data && !data.on
      ? t_i18n("{count, plural, =0 {Off: no confirmed signal is waiting} one {Off: # confirmed signal is waiting} other {Off: # confirmed signals are waiting}}", { count: data.waiting })
      : t_i18n("Open a case for each confirmed signal");
  // Drawn before the setting is read (disabled, on), so the toolbar never shifts when it loads.
  return (
    <Tip content={tip}>
      <label htmlFor={id} className={filterPillClass(false, "cursor-pointer whitespace-nowrap")} data-testid={testId}>
        <Switch id={id} checked={data?.on ?? true} disabled={!data || pending} onCheckedChange={toggle} />
        {t_i18n("Case opening")}
      </label>
    </Tip>
  );
}

/** The Cases empty state while case opening is off: the standard empty line, and one muted line under it. */
export function CaseOpeningOffEmptyState({ label }: { label: string }) {
  const { t_i18n } = useFormatter();
  return (
    <div className="py-10 text-center content-compact text-default-secondary" data-testid="ctem-case-opening-off-state">
      <p>{label}</p>
      <p>{t_i18n("Case opening is off")}</p>
    </div>
  );
}
