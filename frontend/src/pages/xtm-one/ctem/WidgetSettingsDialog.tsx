// SPDX-License-Identifier: Apache-2.0
/**
 * The settings of one command center widget (dev-docs/ctem-stage-viz.md 8.3,
 * slice S2f), opened from the gear on its edit handle: each context field it
 * reads, "From dashboard" by default or a value of its own. Saving writes the
 * widget's `settings` into the person's layout; "From dashboard" removes it.
 */
import { useState } from "react";
import { Button } from "@/components/ui/button";
import { Dialog, DialogBody, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { useFormatter } from "@/i18n";
import type { CtemContextPeriod } from "@/types";
import { CONTEXT_PERIODS } from "./dashboard-context";

const FROM_DASHBOARD = "__dashboard__";

export interface WidgetSettingsDialogProps {
  /** The widget's name; null: the dialog is closed. */
  name: string | null;
  /** The widget's own period; null: it reads the dashboard's. */
  period: CtemContextPeriod | null;
  /** The dashboard's period, shown beside "From dashboard". */
  dashboardPeriod: CtemContextPeriod;
  onSave: (period: CtemContextPeriod | null) => void;
  onClose: () => void;
}

export function WidgetSettingsDialog({ name, period, dashboardPeriod, onSave, onClose }: WidgetSettingsDialogProps) {
  const { t_i18n } = useFormatter();
  const days = (count: number) => t_i18n("{count, plural, one {Last # day} other {Last # days}}", { count });
  // The value picked in the dialog; undefined: what the widget holds.
  const [picked, setPicked] = useState<string | undefined>(undefined);
  const value = picked ?? (period === null ? FROM_DASHBOARD : String(period));
  const close = () => {
    setPicked(undefined);
    onClose();
  };

  return (
    <Dialog open={name !== null} onOpenChange={(open) => { if (!open) close(); }}>
      <DialogContent size="sm" data-testid="ctem-widget-settings">
        <DialogHeader>
          <DialogTitle>{t_i18n("Settings of {name}", { name: name ?? "" })}</DialogTitle>
          <DialogDescription>
            {t_i18n("This widget reads the dashboard context. Give it a value of its own to override it.")}
          </DialogDescription>
        </DialogHeader>
        <DialogBody>
          <div className="space-y-1.5">
            <Label id="ctem-widget-period-label">{t_i18n("Period")}</Label>
            <Select value={value} onValueChange={setPicked}>
              <SelectTrigger aria-labelledby="ctem-widget-period-label" data-testid="ctem-widget-period">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value={FROM_DASHBOARD}>
                  {t_i18n("From dashboard ({value})", { value: days(dashboardPeriod) })}
                </SelectItem>
                {CONTEXT_PERIODS.map((p) => (
                  <SelectItem key={p} value={String(p)}>
                    {days(p)}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
        </DialogBody>
        <DialogFooter>
          <Button variant="outline" onClick={close}>
            {t_i18n("Cancel")}
          </Button>
          <Button
            onClick={() => {
              onSave(value === FROM_DASHBOARD ? null : (Number(value) as CtemContextPeriod));
              close();
            }}
            data-testid="ctem-widget-settings-save"
          >
            {t_i18n("Save")}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
