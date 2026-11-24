// SPDX-License-Identifier: Apache-2.0

/**
 * The administrator switch that pauses and resumes CTEM case opening (#1951,
 * dev-docs/ctem-cases.md section 4.6). Off: no confirmed signal opens a case,
 * they wait. On again: the waiting signals open their cases at once (the
 * server drains them), and the Cases list fills as they open.
 *
 * Drawn on the Cases header and on the Signal sources header. An
 * administrator gets the switch; anyone else sees nothing while it is on and a
 * read-only "Case opening paused" badge while it is off.
 * `CaseOpeningPausedState` is the Cases empty state while it is off.
 */
import { Icon } from "@filigran/design-system";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Switch } from "@/components/ui/switch";
import { useToast } from "@/components/ui/toast";
import { Tip } from "@/components/ui/tooltip";
import { useCtemCaseOpening, useSetCtemCaseOpening } from "@/hooks/api/useCtemCasesApi";
import { useAuth } from "@/hooks/useAuth";
import { useFormatter } from "@/i18n";

/** Turn case opening on or off, with the toast that says what happens. */
function useToggleCaseOpening() {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const set = useSetCtemCaseOpening();
  const toggle = (on: boolean) =>
    set.mutate(
      { on },
      {
        onSuccess: (result) => {
          if (!result.on) {
            toast({ title: t_i18n("Case opening paused"), variant: "success" });
            return;
          }
          toast({
            title: result.draining
              ? t_i18n("{count, plural, one {Case opening on: opening cases for # waiting signal} other {Case opening on: opening cases for # waiting signals}}", { count: result.waiting })
              : t_i18n("Case opening on"),
            variant: "success",
          });
        },
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
  if (!data) return null;
  if (!isAdmin) {
    return data.on ? null : (
      <Badge variant="warning" data-testid={`${testId}-paused`}>
        {t_i18n("Case opening paused")}
      </Badge>
    );
  }
  const id = `${testId}-switch`;
  return (
    <Tip content={t_i18n("Off: confirmed signals wait and open no case. On: every waiting signal opens its case at once.")}>
      <label htmlFor={id} className="flex items-center gap-2 content-body-sm whitespace-nowrap" data-testid={testId}>
        <Switch id={id} checked={data.on} disabled={pending} onCheckedChange={toggle} />
        {t_i18n("Case opening")}
      </label>
    </Tip>
  );
}

/** The Cases empty state while case opening is paused: what waits, and Turn on for an administrator. */
export function CaseOpeningPausedState() {
  const { t_i18n } = useFormatter();
  const { isAdmin } = useAuth();
  const { data } = useCtemCaseOpening();
  const { toggle, pending } = useToggleCaseOpening();
  if (!data || data.on) return null;
  return (
    <div className="flex flex-col items-center gap-3 py-10 text-center" data-testid="ctem-case-opening-paused-state">
      <Icon name="clock" size={24} aria-hidden className="text-default-secondary" />
      <p className="content-body font-medium">{t_i18n("Case opening is paused.")}</p>
      <p className="content-body-sm text-default-secondary">
        {t_i18n("{count, plural, =0 {No confirmed signal is waiting.} one {# confirmed signal is waiting.} other {# confirmed signals are waiting.}}", { count: data.waiting })}
      </p>
      {isAdmin ? (
        <Button variant="secondary" disabled={pending} onClick={() => toggle(true)} data-testid="ctem-case-opening-turn-on">
          {t_i18n("Turn on")}
        </Button>
      ) : null}
    </div>
  );
}
